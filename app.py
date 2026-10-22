"""
Return Label Manager, for category managers.

The sheet has three columns:
    SKU | Return (Yes/No) | Return_days

Run on a Mac:
    export SHOPIFY_STORE=... SHOPIFY_CLIENT_ID=... SHOPIFY_CLIENT_SECRET=...
    python3 -m streamlit run app.py
Online (Streamlit Cloud): put the same values, plus APP_PASSWORD, in Secrets.
"""
import copy
import datetime
import hmac
import io
import json
import os
import re
import time

import pandas as pd
import requests
import streamlit as st

# ---------------- Settings ----------------
API_VERSION = "2026-04"
NAMESPACE, KEY = "custom", "product_labels"
BATCH_SIZE = 25  # Shopify allows up to 25 metafields per write

RETURN_ICON = "https://cdn.shopify.com/s/files/1/0872/6095/4939/files/Return.png?v=1761551571"

NON_RETURNABLE = {
    "type": "return",
    "title": "Non",
    "sub_title": "Returnable",
    "heading": "Non Returnable",
    "sub_heading": "This product is not eligible for returns. No questions asked replacement for any quality concerns.",
    "icon_url": "https://cdn.shopify.com/s/files/1/0872/6095/4939/files/non_returnable.png?v=1765284801",
}


def returnable_block(days):
    """Returnable label with the return window filled in from the sheet."""
    unit = "Day" if days == 1 else "Days"
    return {
        "type": "return",
        "title": f"{days} {unit}",
        "sub_title": "Returnable",
        "heading": "Easy Returns",
        "sub_heading": (
            f"You may return this product within {days} {unit.lower()} of purchase. "
            "Product needs to be unopened/unused/in its original condition to be eligible."
        ),
        "icon_url": RETURN_ICON,
    }


def target_block(returnable, days):
    return returnable_block(days) if returnable else dict(NON_RETURNABLE)


# Used only when a product has no Product Labels at all.
DEFAULT_LABELS = {
    "assurance_labels": [
        {
            "type": "genuine",
            "title": "100%",
            "sub_title": "Genuine",
            "heading": "100% Genuine Guarantee",
            "sub_heading": "Every product is 100% genuine sourced directly from the brand or their authorised distributors",
            "icon_url": "https://cdn.shopify.com/s/files/1/0872/6095/4939/files/100_Genuine.png?v=1761551572",
        },
        {
            "type": "replacement",
            "title": "7 Day",
            "sub_title": "Replacement",
            "heading": "HomeRun Assurance",
            "sub_heading": "No questions asked replacement within 7 days of purchase in case of any quality concerns.",
            "icon_url": "https://cdn.shopify.com/s/files/1/0872/6095/4939/files/Replacement.png?v=1761551572",
        },
    ]
}

# Full-width setting that works on both new and older Streamlit versions.
_ver = tuple(int(x) for x in st.__version__.split(".")[:2])
STRETCH = {"width": "stretch"} if _ver >= (1, 52) else {"use_container_width": True}

SAMPLE_SHEET = "SKU,Return,Return_days\nHZKY8,No,\nSKYJQ,Yes,7\nQMJFF,Yes,3\n"


def cfg(name):
    """Read a setting from Terminal (export) or from the host's Secrets."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        return str(st.secrets.get(name, ""))
    except Exception:
        return ""


SHOP = cfg("SHOPIFY_STORE")
CLIENT_ID = cfg("SHOPIFY_CLIENT_ID")
CLIENT_SECRET = cfg("SHOPIFY_CLIENT_SECRET")
APP_PASSWORD = cfg("APP_PASSWORD")
GQL_URL = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"


# ---------------- Shopify ----------------
def get_token():
    tok = st.session_state.get("token")
    if tok and time.time() - tok["at"] < 20 * 3600:
        return tok["value"]
    r = requests.post(
        f"https://{SHOP}/admin/oauth/access_token",
        data={"grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Shopify login failed ({r.status_code}): {r.text[:300]}")
    st.session_state["token"] = {"value": r.json()["access_token"], "at": time.time()}
    return st.session_state["token"]["value"]


def gql(query, variables):
    for attempt in range(8):
        r = requests.post(
            GQL_URL,
            json={"query": query, "variables": variables},
            headers={"X-Shopify-Access-Token": get_token(), "Content-Type": "application/json"},
            timeout=60,
        )
        if r.status_code == 429:
            time.sleep(2 + attempt * 2)
            continue
        if r.status_code in (401, 403):
            raise RuntimeError(f"Shopify refused access ({r.status_code}). Check the app is installed on {SHOP}.")
        r.raise_for_status()
        body = r.json()
        errors = body.get("errors")
        if errors:
            if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors):
                time.sleep(2 + attempt * 2)
                continue
            raise RuntimeError(f"Shopify error: {errors}")
        return body["data"]
    raise RuntimeError("Shopify is busy. Wait a minute and try again.")


VARIANTS_QUERY = """
query($cursor: String, $ns: String!, $key: String!) {
  productVariants(first: 250, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      sku
      product { id title status metafield(namespace: $ns, key: $key) { type value } }
    }
  }
}
"""

SET_MUTATION = """
mutation($m: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $m) {
    metafields { id }
    userErrors { field message }
  }
}
"""


def load_store():
    products, cursor = {}, None
    while True:
        page = gql(VARIANTS_QUERY, {"cursor": cursor, "ns": NAMESPACE, "key": KEY})["productVariants"]
        for v in page["nodes"]:
            p = v["product"]
            rec = products.setdefault(p["id"], {
                "id": p["id"], "title": p["title"], "status": p["status"],
                "skus": set(), "mf": p["metafield"],
            })
            sku = (v["sku"] or "").strip()
            if sku:
                rec["skus"].add(sku)
        if not page["pageInfo"]["hasNextPage"]:
            return products
        cursor = page["pageInfo"]["endCursor"]


# ---------------- Label logic ----------------
def label_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and any(isinstance(x, dict) and "type" in x for x in v):
                return v
    raise ValueError("no label list found")


def find_return(labels):
    return next((x for x in labels if isinstance(x, dict) and x.get("type") == "return"), None)


def read_current(mf):
    """Returns (state, return_entry). state is 'empty', 'invalid' or 'ok'."""
    if not mf or not (mf.get("value") or "").strip():
        return "empty", None
    try:
        return "ok", find_return(label_list(json.loads(mf["value"])))
    except Exception:
        return "invalid", None


def describe(entry):
    if entry is None:
        return "No return label"
    text = f"{entry.get('title', '')} {entry.get('heading', '')}".lower()
    if "non" in text:
        return "Non Returnable"
    m = re.search(r"\d+", str(entry.get("title", "")))
    if not m:
        return "Returnable"
    n = int(m.group())
    return f"Returnable, {n} day" if n == 1 else f"Returnable, {n} days"


def describe_product(p):
    state, entry = read_current(p["mf"])
    if state == "empty":
        return "No labels yet"
    if state == "invalid":
        return "Broken labels, fix in Shopify"
    return describe(entry)


def new_value(mf, block):
    if not mf or not (mf.get("value") or "").strip():
        data = copy.deepcopy(DEFAULT_LABELS)
    else:
        data = json.loads(mf["value"])
    labels = label_list(data)
    entry = find_return(labels)
    if entry is None:
        gi = next((i for i, x in enumerate(labels) if x.get("type") == "genuine"), -1)
        labels.insert(gi + 1, dict(block))
    else:
        entry.update(block)
    return data


# ---------------- Sheet ----------------
YES = {"yes", "y", "true", "1", "returnable"}
NO = {"no", "n", "false", "0", "nonreturnable", "notreturnable"}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def pick(columns, *names):
    by_norm = {norm(c): c for c in columns}
    return next((by_norm[n] for n in names if n in by_norm), None)


def read_sheet(uploaded):
    """Returns ({sku: (returnable, days)}, [problems])."""
    raw = uploaded.getvalue()
    if uploaded.name.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(raw), dtype=str).fillna("")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
        sep = "\t" if "\t" in text.splitlines()[0] else ","
        df = pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False)

    c_sku = pick(df.columns, "sku")
    c_ret = pick(df.columns, "return", "returnstatus", "returnable")
    c_days = pick(df.columns, "returndays", "returnwindow", "returnwindowdays", "days")
    missing = [n for n, c in (("SKU", c_sku), ("Return", c_ret), ("Return_days", c_days)) if c is None]
    if missing:
        raise ValueError(
            f"The sheet is missing: {', '.join(missing)}. "
            "It needs three columns: SKU, Return (Yes/No) and Return_days. Download the sample sheet to see the format."
        )

    rules, problems, conflicted = {}, [], set()
    for i, r in df.iterrows():
        sku = str(r[c_sku]).strip()
        if not sku or sku.lower() == "nan":
            continue
        row = i + 2  # header is row 1
        ret = norm(r[c_ret])
        if ret in YES:
            try:
                days = int(float(str(r[c_days]).strip()))
            except ValueError:
                days = 0
            if days <= 0:
                problems.append({"SKU": sku, "Issue": f"Row {row}: Return is Yes but Return_days is empty or not a number"})
                continue
            rule = (True, days)
        elif ret in NO:
            rule = (False, 0)  # Return_days is ignored for No
        else:
            problems.append({"SKU": sku, "Issue": f"Row {row}: Return should be Yes or No, found '{r[c_ret]}'"})
            continue
        if sku in rules and rules[sku] != rule:
            conflicted.add(sku)
        rules[sku] = rule

    for sku in conflicted:
        rules.pop(sku, None)
        problems.append({"SKU": sku, "Issue": "Listed more than once with different values, fix the sheet"})
    return rules, problems


# ---------------- Plan ----------------
def status_and_window(state, entry):
    """What a label shows, split into (status, return window)."""
    if state == "empty":
        return "No labels yet", ""
    if entry is None:
        return "No return label", ""
    text = f"{entry.get('title', '')} {entry.get('heading', '')}".lower()
    if "non" in text:
        return "Non Returnable", "—"
    m = re.search(r"\d+", str(entry.get("title", "")))
    if not m:
        return "Returnable", ""
    n = int(m.group())
    return "Returnable", f"{n} day" if n == 1 else f"{n} days"


def build_plan(products, rules):
    """Returns (table rows per SKU, writes per product, draft/archived SKU count, problems)."""
    sku_to_pid = {s: p["id"] for p in products.values() for s in p["skus"]}
    problems, per_product = [], {}
    for sku, rule in rules.items():
        pid = sku_to_pid.get(sku)
        if pid is None:
            problems.append({"SKU": sku, "Issue": "Not found in Shopify"})
        else:
            per_product.setdefault(pid, {})[sku] = rule

    table, writes, inactive = [], [], 0
    for pid, sku_rules in per_product.items():
        p = products[pid]
        if p["status"] in ("DRAFT", "ARCHIVED"):
            inactive += len(sku_rules)
            continue

        values = set(sku_rules.values())
        if any(not ret for ret, _ in values):
            returnable, days = False, 0  # one Non Returnable variant makes the product Non Returnable
        else:
            returnable, days = True, min(d for _, d in values)  # shortest window is the safe one

        state, entry = read_current(p["mf"])
        if state == "invalid":
            problems.append({"SKU": ", ".join(sorted(sku_rules)), "Issue": f"{p['title']}: labels in Shopify are broken, fix them by hand"})
            continue

        block = target_block(returnable, days)
        cur_status, cur_window = status_and_window(state, entry)
        new_status, new_window = status_and_window("ok", block)
        unchanged = entry is not None and all(entry.get(k) == v for k, v in block.items())
        if unchanged:
            change = "No change"
        elif cur_status != new_status:
            change = "Status"
        elif cur_window != new_window:
            change = "Return window"
        else:
            change = "Wording only"

        if len(values) > 1:
            problems.append({"SKU": ", ".join(sorted(sku_rules)),
                             "Issue": f"{p['title']}: variants have different values in the sheet, using {new_status} {new_window}".strip()})

        for sku in sorted(sku_rules):
            table.append({
                "SKU": sku,
                "Product": p["title"],
                "Current status": cur_status,
                "New status": new_status,
                "Current return window": cur_window,
                "New return window": new_window,
                "Change": change,
            })
        if not unchanged:
            writes.append({
                "product_id": pid, "block": block,
                "SKU": ", ".join(sorted(sku_rules)), "Product": p["title"],
                "Now": f"{cur_status} {cur_window}".strip(), "After": f"{new_status} {new_window}".strip(),
            })
    table.sort(key=lambda r: r["Change"] == "No change")  # changes first
    return table, writes, inactive, problems


def apply_changes(products, rows):
    backup = {r["product_id"]: products[r["product_id"]]["mf"] for r in rows}
    results = []
    for start in range(0, len(rows), BATCH_SIZE):
        chunk = rows[start:start + BATCH_SIZE]
        payload = [{
            "ownerId": r["product_id"], "namespace": NAMESPACE, "key": KEY,
            "type": (products[r["product_id"]]["mf"] or {}).get("type") or "json",
            "value": json.dumps(new_value(products[r["product_id"]]["mf"], r["block"]), ensure_ascii=False),
        } for r in chunk]
        try:
            errs = gql(SET_MUTATION, {"m": payload})["metafieldsSet"]["userErrors"]
            bad = {}
            for e in errs:
                idx = next((int(x) for x in (e.get("field") or []) if str(x).isdigit()), None)
                bad[idx] = e["message"]
            for i, r in enumerate(chunk):
                msg = bad.get(i) or bad.get(None)
                results.append({"SKU": r["SKU"], "Product": r["Product"], "Before": r["Now"], "After": r["After"],
                                "Result": f"Failed: {msg}" if msg else "Done"})
        except Exception as e:
            for r in chunk:
                results.append({"SKU": r["SKU"], "Product": r["Product"], "Before": r["Now"], "After": r["After"],
                                "Result": f"Failed: {e}"})
    return pd.DataFrame(results), json.dumps(backup, indent=2, ensure_ascii=False)


def search(products, query, limit=100):
    q = query.strip().lower()
    out = []
    for p in products.values():
        if q in p["title"].lower() or any(q in s.lower() for s in p["skus"]):
            state, entry = read_current(p["mf"])
            status, window = ("Broken labels", "") if state == "invalid" else status_and_window(state, entry)
            out.append({
                "SKU": ", ".join(sorted(p["skus"])) or "(no SKU)",
                "Product": p["title"],
                "Return status": status,
                "Return window": window,
                "Shopify status": p["status"].title(),
            })
            if len(out) >= limit:
                break
    return pd.DataFrame(out)


def table_height(n, cap=320):
    return min(38 + 35 * max(n, 1), cap)


HIGHLIGHT = "background-color: #FFF1C2; color: #1F1F1F; font-weight: 600"


def highlight_changes(row):
    styles = [""] * len(row)
    cols = list(row.index)
    if row["Current status"] != row["New status"]:
        styles[cols.index("New status")] = HIGHLIGHT
    if row["Current return window"] != row["New return window"]:
        styles[cols.index("New return window")] = HIGHLIGHT
    return styles


def load_products(message):
    with st.spinner(message):
        st.session_state["products"] = load_store()
        st.session_state["loaded_at"] = datetime.datetime.now().strftime("%d %b, %H:%M")


# ---------------- Page ----------------
st.set_page_config(page_title="Return labels", page_icon="🏷️", layout="wide")

if APP_PASSWORD and not st.session_state.get("signed_in"):
    st.title("Return labels")
    pw = st.text_input("Password", type="password")
    if st.button("Sign in", type="primary"):
        if hmac.compare_digest(pw, APP_PASSWORD):
            st.session_state["signed_in"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()

if not (SHOP and CLIENT_ID and CLIENT_SECRET):
    st.error("Shopify isn't connected. Add SHOPIFY_STORE, SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET to the app's settings.")
    st.stop()

if "products" not in st.session_state:
    try:
        load_products("Loading products from Shopify…")
    except Exception as e:
        st.error(f"Couldn't load products: {e}")
        st.stop()

st.title("Return labels")
st.caption(f"{len(st.session_state['products'])} products, loaded {st.session_state['loaded_at']}")

# ---- Search ----
with st.form("search", border=False):
    q_col, b_col = st.columns([5, 1], vertical_alignment="bottom")
    query = q_col.text_input("Search by product name or SKU", placeholder="For example: Maha cement, or SKYJQ")
    searched = b_col.form_submit_button("Search", **STRETCH)
if searched:
    st.session_state["query"] = query.strip()
q = st.session_state.get("query", "")
if q:
    found = search(st.session_state["products"], q)
    r_col, c_col = st.columns([5, 1], vertical_alignment="center")
    if found.empty:
        r_col.info(f"No products match “{q}”. Check the spelling or try part of the name.")
    else:
        r_col.caption(f"{len(found)} result{'s' if len(found) != 1 else ''} for “{q}”" + (" (first 100)" if len(found) == 100 else ""))
    if c_col.button("Clear search", **STRETCH):
        st.session_state["query"] = ""
        st.rerun()
    if not found.empty:
        st.dataframe(found, hide_index=True, height=table_height(len(found), 230), **STRETCH)

st.divider()

# ---- Result of the last apply ----
res = st.session_state.get("results")
if res is not None:
    done = int((res["Result"] == "Done").sum())
    failed = len(res) - done
    if failed:
        st.warning(f"Updated {done} products. {failed} failed, see the report.")
    else:
        st.success(f"Updated {done} products on Shopify. The table below now shows the new labels.")
    b1, b2, b3 = st.columns(3)
    b1.download_button("Download report", res.to_csv(index=False), "return_labels_report.csv", **STRETCH)
    b2.download_button("Download backup of old labels", st.session_state["backup_json"],
                       st.session_state["backup_name"], **STRETCH)
    if b3.button("Close", **STRETCH):
        st.session_state.pop("results", None)
        st.rerun()

# ---- Upload ----
up_col, sample_col = st.columns([5, 1], vertical_alignment="center")
uploaded = up_col.file_uploader("Upload sheet with SKU, Return (Yes/No) and Return_days",
                                type=["csv", "tsv", "txt", "xlsx", "xls"])
sample_col.download_button("Sample sheet", SAMPLE_SHEET, "return_labels_sample.csv", **STRETCH)

if uploaded is None:
    st.info("Upload a sheet to see what will change. Nothing changes on Shopify until you click Apply.")
else:
    # Fetch the latest labels whenever a new sheet is uploaded, so the preview is never stale.
    sheet_key = (uploaded.name, uploaded.size)
    if st.session_state.get("loaded_for") != sheet_key:
        try:
            load_products("Getting the latest labels from Shopify…")
            st.session_state["loaded_for"] = sheet_key
        except Exception as e:
            st.error(f"Couldn't load products: {e}")
    products = st.session_state["products"]

    try:
        rules, sheet_problems = read_sheet(uploaded)
    except Exception as e:
        st.error(str(e))
        rules, sheet_problems = None, []

    if rules is not None:
        table, writes, inactive, plan_problems = build_plan(products, rules)
        problems = sheet_problems + plan_problems
        changing = sum(1 for r in table if r["Change"] != "No change")

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("SKUs that will change", changing)
        m2.metric("Already correct", len(table) - changing)
        m3.metric("Draft or archived, not changed", inactive)
        m4.metric("Need attention", len(problems))

        if table:
            view = pd.DataFrame(table)
            st.dataframe(view.style.apply(highlight_changes, axis=1), hide_index=True,
                         height=table_height(len(view)), **STRETCH)
            st.caption("Highlighted cells are the values that will change.")

        if writes:
            if st.button(f"Apply changes to {changing} SKUs on Shopify", type="primary", **STRETCH):
                with st.spinner(f"Updating {len(writes)} products…"):
                    report, backup_json = apply_changes(products, writes)
                st.session_state["results"] = report
                st.session_state["backup_json"] = backup_json
                st.session_state["backup_name"] = f"backup_{datetime.datetime.now():%Y%m%d_%H%M%S}.json"
                st.session_state.pop("products", None)  # reload so the table shows the new labels
                st.rerun()
        elif table and not problems:
            st.success("Everything in this sheet already matches Shopify.")

        if problems:
            with st.expander(f"{len(problems)} SKUs need attention"):
                st.dataframe(pd.DataFrame(problems), hide_index=True,
                             height=table_height(len(problems), 260), **STRETCH)