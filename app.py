"""
Return Labels, for category managers.

Sign in with your work email. Search for products or upload a sheet
(SKU, Return, Return_days), set Return and Return days in the table, click Apply.
Every change is saved on the product in Shopify with who made it and when.

Settings (Streamlit Secrets, or .streamlit/secrets.toml on a Mac):
    SHOPIFY_STORE, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET
    [users]
    "name@home-run.co" = "password"
"""
import copy
import datetime
import hmac
import html
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
NS = "custom"
LABELS_KEY = "product_labels"
HISTORY_KEY = "return_label_history"  # who changed what, kept on each product
HISTORY_KEEP = 20                     # changes kept per product
BATCH_PRODUCTS = 12                   # 2 metafields per product; Shopify takes 25 per call
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
EDITOR_HEIGHT = 390
PAGE_SIZE = 10
GRID_WIDTHS = [1.1, 4, 1.5, 1.3, 1.4, 1.1]

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


def md(text):
    """Show text as-is (no accidental bold/links from product names)."""
    return re.sub(r"([\\`*_{}\[\]<>()#+\-.!|~])", r"\\\1", str(text))


def cfg(name):
    if os.environ.get(name):
        return os.environ[name]
    try:
        return str(st.secrets.get(name, ""))
    except Exception:
        return ""


def load_users():
    try:
        return {str(k).strip().lower(): str(v) for k, v in dict(st.secrets.get("users", {})).items()}
    except Exception:
        return {}


SHOP = cfg("SHOPIFY_STORE")
CLIENT_ID = cfg("SHOPIFY_CLIENT_ID")
CLIENT_SECRET = cfg("SHOPIFY_CLIENT_SECRET")
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
        text = r.text
        if "application_cannot_be_found" in text:
            raise RuntimeError("Shopify doesn't recognise SHOPIFY_CLIENT_ID. Copy the Client ID again from the Dev Dashboard into Secrets.")
        if "invalid_client" in text or "client_secret" in text.lower():
            raise RuntimeError("Shopify rejected the client secret. Copy the Client secret again from the Dev Dashboard into Secrets.")
        if r.status_code == 404:
            raise RuntimeError(f"Shopify couldn't find the store '{SHOP}'. Check SHOPIFY_STORE in Secrets (it should end in .myshopify.com).")
        title = re.search(r"<title>(.*?)</title>", text, re.S)
        raise RuntimeError(f"Shopify login failed ({r.status_code}): {(title.group(1) if title else text[:200]).strip()}")
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
            if any((e.get("extensions") or {}).get("code") == "ACCESS_DENIED" for e in errors):
                raise RuntimeError(
                    "The Shopify app in Secrets doesn't have permission to read and update products. "
                    "In the Dev Dashboard, make sure its active version has the scopes read_products and write_products, "
                    "that it's installed on the store, and that SHOPIFY_CLIENT_ID belongs to that app. Then reboot this app and sign in again."
                )
            raise RuntimeError(f"Shopify error: {errors}")
        return body["data"]
    raise RuntimeError("Shopify is busy. Wait a minute and try again.")


VARIANTS_QUERY = """
query($cursor: String) {
  productVariants(first: 250, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      sku
      product {
        id title status
        labels: metafield(namespace: "custom", key: "product_labels") { type value }
        history: metafield(namespace: "custom", key: "return_label_history") { value }
      }
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


def parse_history(mf):
    try:
        data = json.loads((mf or {}).get("value") or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def load_store():
    products, cursor = {}, None
    while True:
        page = gql(VARIANTS_QUERY, {"cursor": cursor})["productVariants"]
        for v in page["nodes"]:
            p = v["product"]
            rec = products.setdefault(p["id"], {
                "id": p["id"], "title": p["title"], "status": p["status"], "skus": set(),
                "mf": p["labels"], "history": parse_history(p["history"]),
            })
            sku = (v["sku"] or "").strip()
            if sku:
                rec["skus"].add(sku)
        if not page["pageInfo"]["hasNextPage"]:
            return products
        cursor = page["pageInfo"]["endCursor"]


def reload_products():
    with st.spinner("Getting the latest labels from Shopify…"):
        st.session_state["products"] = load_store()


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
    """(state, return_entry); state is 'empty', 'invalid' or 'ok'."""
    if not mf or not (mf.get("value") or "").strip():
        return "empty", None
    try:
        return "ok", find_return(label_list(json.loads(mf["value"])))
    except Exception:
        return "invalid", None


def current_rule(state, entry):
    """Current label as (Return, Return days) for the table: ('Yes', 3), ('No', None) or (None, None)."""
    if state != "ok" or entry is None:
        return None, None
    text = f"{entry.get('title', '')} {entry.get('heading', '')}".lower()
    if "non" in text:
        return "No", None
    m = re.search(r"\d+", str(entry.get("title", "")))
    return "Yes", (int(m.group()) if m else None)


def describe(state, entry):
    if state == "empty":
        return "No labels yet"
    ret, days = current_rule(state, entry)
    if ret is None:
        return "No return label"
    if ret == "No":
        return "Non Returnable"
    if days is None:
        return "Returnable"
    return f"Returnable, {days} day" if days == 1 else f"Returnable, {days} days"


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
def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def pick(columns, *names):
    by_norm = {norm(c): c for c in columns}
    return next((by_norm[n] for n in names if n in by_norm), None)


def to_days(value):
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return None
    try:
        return int(float(s))
    except ValueError:
        return "bad"


def read_sheet(uploaded):
    """Returns ([(sku, Return, days)], notes)."""
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
        raise ValueError(f"The sheet is missing: {', '.join(missing)}. It needs the columns SKU, Return and Return_days.")

    items, notes = {}, []
    for i, r in df.iterrows():
        sku = str(r[c_sku]).strip()
        if not sku or sku.lower() == "nan":
            continue
        row = i + 2
        ret_raw = norm(r[c_ret])
        ret = "Yes" if ret_raw in ("yes", "y", "true") else "No" if ret_raw in ("no", "n", "false") else None
        if ret is None:
            notes.append(f"{sku}: row {row} has Return '{r[c_ret]}'. Pick Yes or No in the table.")
        days = to_days(r[c_days])
        if days == "bad":
            notes.append(f"{sku}: row {row} has Return_days '{r[c_days]}', which isn't a number.")
            days = None
        if sku in items and items[sku] != (ret, days):
            notes.append(f"{sku}: listed more than once with different values, using row {row}.")
        items[sku] = (ret, days)
    return [(s, r, d) for s, (r, d) in items.items()], notes


# ---------------- Table ----------------
def build_table(products, items):
    """items: [(sku, Return, days)] -> (DataFrame for the editor, notes)."""
    sku_to_pid = {s: p["id"] for p in products.values() for s in p["skus"]}
    rows, notes = [], []
    for sku, ret, days in items:
        pid = sku_to_pid.get(sku)
        if pid is None:
            notes.append(f"{sku}: not found in Shopify.")
            continue
        p = products[pid]
        if p["status"] in ("DRAFT", "ARCHIVED"):
            notes.append(f"{sku}: {p['status'].title()} product, not changed.")
            continue
        state, entry = read_current(p["mf"])
        if state == "invalid":
            notes.append(f"{sku}: its labels in Shopify are broken. Fix them in Shopify admin.")
            continue
        cur_ret, cur_days = current_rule(state, entry)
        if ret == "No" and not days:
            days = 0  # non-returnable always has a 0-day window
        rows.append({
            "SKU": sku,
            "Product": p["title"],
            "Current status": describe(state, entry).split(",")[0],
            "Current return days": 0.0 if cur_ret == "No" else (float(cur_days) if cur_days is not None else None),
            "Return": ret,
            "Return days": float(days) if days is not None else None,
            "pid": pid,
        })
    df = pd.DataFrame(rows, columns=["SKU", "Product", "Current status", "Current return days", "Return", "Return days", "pid"])
    return df, notes


def search_items(products, query, limit=200):
    q = query.strip().lower()
    items = []
    for p in products.values():
        if p["status"] in ("DRAFT", "ARCHIVED"):
            continue
        if q in p["title"].lower() or any(q in s.lower() for s in p["skus"]):
            ret, days = current_rule(*read_current(p["mf"]))
            if ret == "No":
                days = 0
            items += [(s, ret, days) for s in sorted(p["skus"])]
            if len(items) >= limit:
                break
    return items


def on_return_change(ret_key, days_key):
    """Global rule: Non Returnable means a 0-day window."""
    if st.session_state.get(ret_key) == "No":
        st.session_state[days_key] = "0"
    elif str(st.session_state.get(days_key, "")).strip() == "0":
        st.session_state[days_key] = ""


def clean(ret, days):
    """Normalise a row: Return -> 'Yes'/'No'/None, days -> int, None, or 'bad'."""
    ret = ret if ret in ("Yes", "No") else None
    if days is None or (isinstance(days, float) and pd.isna(days)):
        return ret, None
    text = str(days).strip()
    if not text:
        return ret, None
    try:
        n = float(text)
    except ValueError:
        return ret, "bad"
    if n != int(n) or not 0 <= n <= 365:
        return ret, "bad"
    return ret, int(n)


def plan_changes(products, edited, base, only_edited):
    """Check the table and work out what to write. only_edited: skip rows the user didn't touch (search)."""
    errors, notes, per_product = [], [], {}
    for i, row in edited.iterrows():
        ret, days = clean(row["Return"], row["Return days"])
        if only_edited and (ret, days) == clean(base.loc[i, "Return"], base.loc[i, "Return days"]):
            continue
        sku = row["SKU"]
        if days == "bad":
            errors.append(f"{sku}: Return days must be a whole number (0 to 365).")
            continue
        if ret is None:
            if days:
                errors.append(f"{sku}: choose Yes or No.")
            continue
        if ret == "No" and days:
            errors.append(f"{sku}: it's non-returnable, so its return window must be 0, not {days}.")
        elif ret == "Yes" and days is None:
            errors.append(f"{sku}: it's returnable, so enter the return window in days.")
        else:
            per_product.setdefault(row["pid"], {})[sku] = (ret == "Yes", days or 0)

    writes = []
    for pid, rules in per_product.items():
        values = set(rules.values())
        if any(not r for r, _ in values):
            returnable, days = False, 0
        else:
            returnable, days = True, min(d for _, d in values)
        p = products[pid]
        state, entry = read_current(p["mf"])
        block = target_block(returnable, days)
        if entry is not None and all(entry.get(k) == v for k, v in block.items()):
            continue
        if len(values) > 1:
            notes.append(f"{p['title']}: variants have different values, so the whole product gets {describe('ok', block)}.")
        writes.append({
            "pid": pid, "block": block, "skus": ", ".join(sorted(rules)), "n": len(rules),
            "before": describe(state, entry), "after": describe("ok", block),
        })
    return writes, errors, notes


def apply_changes(products, writes, user):
    when = datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M")
    failed = []
    for start in range(0, len(writes), BATCH_PRODUCTS):
        chunk = writes[start:start + BATCH_PRODUCTS]
        payload = []
        for w in chunk:
            p = products[w["pid"]]
            entry = {"at": when, "by": user, "skus": w["skus"], "before": w["before"], "after": w["after"]}
            payload += [
                {"ownerId": w["pid"], "namespace": NS, "key": LABELS_KEY,
                 "type": (p["mf"] or {}).get("type") or "json",
                 "value": json.dumps(new_value(p["mf"], w["block"]), ensure_ascii=False)},
                {"ownerId": w["pid"], "namespace": NS, "key": HISTORY_KEY, "type": "json",
                 "value": json.dumps(([entry] + p["history"])[:HISTORY_KEEP], ensure_ascii=False)},
            ]
        try:
            errs = gql(SET_MUTATION, {"m": payload})["metafieldsSet"]["userErrors"]
            if errs:  # Shopify saves all of a batch or none of it
                failed += [(w["skus"], errs[0]["message"]) for w in chunk]
        except Exception as e:
            failed += [(w["skus"], str(e)) for w in chunk]
    return failed


def history_table(products):
    rows = [{
        "When (IST)": h.get("at", ""), "User": h.get("by", ""), "SKU": h.get("skus", ""),
        "Product": p["title"], "Before": h.get("before", ""), "After": h.get("after", ""),
    } for p in products.values() for h in p["history"]]
    df = pd.DataFrame(rows, columns=["When (IST)", "User", "SKU", "Product", "Before", "After"])
    return df.sort_values("When (IST)", ascending=False)


# ---------------- Page ----------------
st.set_page_config(page_title="Return labels", page_icon="🏷️", layout="wide")
st.markdown("""
<style>
.block-container {padding-top: 2.5rem; padding-bottom: 0.5rem}
/* table toolbar: show/hide columns, download, search, fullscreen */
[data-testid="stElementToolbar"] {display: none !important}
/* column menu: sort, format, autosize, pin, hide */
[data-testid="stDataFrameColumnMenu"],
div:has(> [data-testid="stDataFrameColumnMenu"]) {display: none !important}
/* compact upload box, same height as the search box */
[data-testid="stFileUploaderDropzone"] {padding: 0.3rem 0.75rem}
[data-testid="stFileUploaderDropzoneInstructions"] small,
[data-testid="stFileUploaderDropzoneInstructions"] svg {display: none}
[data-testid="InputInstructions"] {display: none}
/* compact rows in the edit grid; text cells as tall as the dropdowns so rows line up */
.st-key-grid [data-testid="stVerticalBlock"] {gap: 0.35rem}
.st-key-grid [data-testid="stMarkdownContainer"] {min-height: 2.5rem; display: flex; align-items: center}
.st-key-grid [data-testid="stMarkdownContainer"] p {margin: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis}
</style>
""", unsafe_allow_html=True)

USERS = load_users()
if not (SHOP and CLIENT_ID and CLIENT_SECRET):
    st.error("Shopify isn't connected. Add SHOPIFY_STORE, SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET to the app's Secrets.")
    st.stop()
if not USERS:
    st.error("No users are set up yet. Add a [users] section with each person's email and password to the app's Secrets.")
    st.stop()

# ---- Sign in ----
if "user" not in st.session_state:
    _, mid, _ = st.columns([1, 1, 1])
    with mid:
        st.subheader("Return labels")
        with st.form("login"):
            email = st.text_input("Work email")
            password = st.text_input("Password", type="password")
            signed = st.form_submit_button("Sign in", type="primary", **STRETCH)
        if signed:
            e = email.strip().lower()
            if e in USERS and hmac.compare_digest(password, USERS[e]):
                st.session_state["user"] = e
                st.rerun()
            st.error("That email and password don't match.")
    st.stop()

user = st.session_state["user"]
if "products" not in st.session_state:
    try:
        reload_products()
    except Exception as e:
        st.error(f"Couldn't load products: {e}")
        st.stop()
products = st.session_state["products"]
st.session_state.setdefault("editor_v", 0)

st.session_state.setdefault("upload_v", 0)


def clear_all():
    """Clear button: empty the search box, the sheet, and the table."""
    st.session_state["q"] = ""
    st.session_state.pop("source", None)
    st.session_state["upload_v"] += 1


if st.session_state.pop("reset_query", False):  # a sheet was just loaded
    st.session_state["q"] = ""

head, who, out = st.columns([6, 2.5, 0.8], vertical_alignment="center")
head.markdown("### Return labels")
who.markdown(f"<div style='text-align:right;opacity:.7'>{html.escape(user)}</div>", unsafe_allow_html=True)
if out.button("Sign out", type="tertiary"):
    st.session_state.clear()
    st.rerun()

tab_update, tab_history = st.tabs(["Update", "History"])

with tab_update:
    left, right = st.columns(2, vertical_alignment="top")
    with left:
        with st.form("search", border=False):
            q_col, s_col, c_col = st.columns([6, 1, 1], vertical_alignment="bottom")
            query = q_col.text_input("Search by product name or SKU", key="q",
                                     placeholder="For example: Maha cement, or SKYJQ")
            go = s_col.form_submit_button("Search", **STRETCH)
            c_col.form_submit_button("Clear", on_click=clear_all, **STRETCH)
    with right:
        uploaded = st.file_uploader("Or upload a sheet with SKU, Return (Yes/No) and Return_days",
                                    type=["csv", "tsv", "txt", "xlsx", "xls"],
                                    key=f"upload_{st.session_state['upload_v']}")

    if go and query.strip():
        st.session_state["source"] = {"kind": "search", "query": query.strip()}
        st.session_state["editor_v"] += 1
    if uploaded is not None:
        # Read the sheet once, then empty the upload box so the page layout stays the same.
        try:
            reload_products()
            items, sheet_notes = read_sheet(uploaded)
            st.session_state["source"] = {"kind": "sheet", "name": uploaded.name, "items": items, "notes": sheet_notes}
            st.session_state["editor_v"] += 1
            st.session_state["reset_query"] = True
        except Exception as e:
            st.session_state["flash"] = {"text": str(e), "failed": True}
        st.session_state["upload_v"] += 1
        st.rerun()

    flash = st.session_state.pop("flash", None)
    if flash:
        (st.error if flash["failed"] else st.success)(flash["text"])

    src = st.session_state.get("source")
    if not src:
        st.info("Search for a product or upload a sheet. You can then set Return and Return days in the table and apply.")
    else:
        if src["kind"] == "search":
            base, notes = build_table(products, search_items(products, src["query"]))
            label = f"matching “{src['query']}”"
        else:
            base, notes = build_table(products, src["items"])
            notes = src["notes"] + notes
            label = f"from {src['name']}"

        if base.empty:
            st.info(f"No active products {label}.")
        else:
            v = st.session_state["editor_v"]
            if st.session_state.get("edits_v") != v:  # new search, new sheet, or just applied
                st.session_state["edits_v"] = v
                st.session_state["edits"] = {
                    i: (r["Return"], "" if pd.isna(r["Return days"]) else str(int(r["Return days"])))
                    for i, r in base.reset_index(drop=True).iterrows()
                }
                st.session_state["page"] = 0
            edits = st.session_state["edits"]
            base = base.reset_index(drop=True)
            pages = max(1, -(-len(base) // PAGE_SIZE))
            page = min(st.session_state.get("page", 0), pages - 1)
            first = page * PAGE_SIZE

            with st.container(key="grid"):
                head_cells = st.columns(GRID_WIDTHS)
                for cell, title in zip(head_cells, ["SKU", "Product", "Current status", "Current return days", "New: Return", "New: Return days"]):
                    cell.markdown(f"**{title}**")
                for i in range(first, min(first + PAGE_SIZE, len(base))):
                    r = base.iloc[i]
                    ret0, days0 = edits[i]
                    cells = st.columns(GRID_WIDTHS, vertical_alignment="center")
                    cells[0].markdown(md(r["SKU"]))
                    cells[1].markdown(md(r["Product"]))
                    cells[2].markdown(md(r["Current status"]))
                    cells[3].markdown("" if pd.isna(r["Current return days"]) else str(int(r["Current return days"])))
                    ret_key, days_key = f"ret_{v}_{i}", f"days_{v}_{i}"
                    if days_key not in st.session_state:
                        st.session_state[days_key] = days0
                    ret = cells[4].selectbox(
                        "New: Return", ["Yes", "No"], key=ret_key, label_visibility="collapsed",
                        index=["Yes", "No"].index(ret0) if ret0 in ("Yes", "No") else None, placeholder="Choose",
                        on_change=on_return_change, args=(ret_key, days_key),
                    )
                    days = cells[5].text_input(
                        "New: Return days", key=days_key, label_visibility="collapsed", placeholder="Days",
                    )
                    edits[i] = (ret, days)

            edited = base.copy()
            edited["Return"] = [edits[i][0] for i in range(len(base))]
            edited["Return days"] = [edits[i][1] for i in range(len(base))]
            writes, errors, plan_notes = plan_changes(products, edited, base, only_edited=src["kind"] == "search")
            notes += plan_notes
            n_skus = sum(w["n"] for w in writes)

            info_col, prev_col, page_col, next_col, btn_col = st.columns([5, 0.5, 1, 0.5, 1.4], vertical_alignment="center")
            summary = f"{len(base)} SKU{'s' if len(base) != 1 else ''} {label}. **{n_skus} will change.**"
            if errors:
                summary += f" Fix {len(errors)} error{'s' if len(errors) != 1 else ''} to apply."
            elif not writes:
                summary += " Everything already matches Shopify."
            info_col.markdown(summary)
            if pages > 1:
                if prev_col.button("‹", key="prev", disabled=page == 0, **STRETCH):
                    st.session_state["page"] = page - 1
                    st.rerun()
                page_col.markdown(f"<div style='text-align:center'>{page + 1} / {pages}</div>", unsafe_allow_html=True)
                if next_col.button("›", key="next", disabled=page >= pages - 1, **STRETCH):
                    st.session_state["page"] = page + 1
                    st.rerun()
            if btn_col.button(f"Apply {n_skus} change{'s' if n_skus != 1 else ''}", type="primary",
                              disabled=bool(errors) or not writes, **STRETCH):
                with st.spinner("Updating Shopify…"):
                    failed = apply_changes(products, writes, user)
                done = len(writes) - len(failed)
                text = f"Updated {done} product{'s' if done != 1 else ''} on Shopify."
                if failed:
                    text += " Failed: " + "; ".join(f"{s} ({m})" for s, m in failed[:5])
                st.session_state["flash"] = {"text": text, "failed": bool(failed)}
                reload_products()
                st.session_state["editor_v"] += 1
                st.rerun()

            if errors:
                st.error("\n".join(f"- {e}" for e in errors[:6]) + (f"\n- …and {len(errors) - 6} more" if len(errors) > 6 else ""))
            if notes:
                with st.expander(f"{len(notes)} note{'s' if len(notes) != 1 else ''}"):
                    st.markdown("\n".join(f"- {n}" for n in notes))

with tab_history:
    hist = history_table(products)
    if hist.empty:
        st.info("No changes yet. Every update made in this app is listed here with who made it and when.")
    else:
        cap_col, dl_col = st.columns([5, 1], vertical_alignment="center")
        cap_col.caption(f"{len(hist)} change{'s' if len(hist) != 1 else ''}, newest first. Up to {HISTORY_KEEP} are kept per product.")
        dl_col.download_button("Download", hist.to_csv(index=False), "return_label_history.csv", **STRETCH)
        st.dataframe(hist, hide_index=True, height=EDITOR_HEIGHT + 90, **STRETCH)
