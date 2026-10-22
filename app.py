"""
Return Label Manager — a small app that runs on your own computer.

Start it:
    cd ~/Downloads/return_label_app
    export SHOPIFY_STORE=b607ea-2b.myshopify.com
    export SHOPIFY_CLIENT_ID=...
    export SHOPIFY_CLIENT_SECRET=...
    streamlit run app.py
"""
import copy
import datetime
import io
import json
import os
import time

import pandas as pd
import requests
import streamlit as st

# ---------------- Settings ----------------
API_VERSION = "2026-04"
NAMESPACE, KEY = "custom", "product_labels"
BATCH_SIZE = 25  # Shopify allows up to 25 metafields per write

TEMPLATES = {
    "non_returnable": {
        "type": "return",
        "title": "Non",
        "sub_title": "Returnable",
        "heading": "Non Returnable",
        "sub_heading": "This product is not eligible for returns. No questions asked replacement for any quality concerns.",
        "icon_url": "https://cdn.shopify.com/s/files/1/0872/6095/4939/files/non_returnable.png?v=1765284801",
    },
    "returnable": {
        "type": "return",
        "title": "3 Days",
        "sub_title": "Returnable",
        "heading": "Easy Returns",
        "sub_heading": "You may return this product within 3 days of purchase. Product needs to be unopened/unused/in its original condition to be eligible. Please note that return charges of INR 199 shall apply to the refund.",
        "icon_url": "https://cdn.shopify.com/s/files/1/0872/6095/4939/files/Return.png?v=1761551571",
    },
}

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

NICE = {
    "returnable": "Returnable",
    "non_returnable": "Non Returnable",
    "missing": "No return label",
    "empty": "No labels at all",
    "invalid": "Broken JSON",
}

SHOP = os.environ.get("SHOPIFY_STORE", "")
CLIENT_ID = os.environ.get("SHOPIFY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
GQL_URL = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"


# ---------------- Shopify ----------------
def get_token():
    """Get a token with client ID + secret; reuse it for 20 hours (it lasts 24)."""
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
    """Run a GraphQL call, waiting and retrying when Shopify says 'slow down'."""
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
    raise RuntimeError("Shopify kept asking us to slow down. Wait a minute and try again.")


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


def load_store(on_progress):
    """Read every variant once and group by product: {product_id: {...}}."""
    products, cursor, seen = {}, None, 0
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
        seen += len(page["nodes"])
        on_progress(seen)
        if not page["pageInfo"]["hasNextPage"]:
            return products
        cursor = page["pageInfo"]["endCursor"]


# ---------------- Label logic (same as the tested script) ----------------
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


def status_of_entry(entry):
    if entry is None:
        return "missing"
    text = f"{entry.get('title', '')} {entry.get('heading', '')}".lower()
    return "non_returnable" if "non" in text else "returnable"


def current_status(mf):
    if not mf or not (mf.get("value") or "").strip():
        return "empty"
    try:
        return status_of_entry(find_return(label_list(json.loads(mf["value"]))))
    except Exception:
        return "invalid"


def new_value(mf, target):
    if not mf or not (mf.get("value") or "").strip():
        data = copy.deepcopy(DEFAULT_LABELS)
    else:
        data = json.loads(mf["value"])
    labels = label_list(data)
    entry = find_return(labels)
    if entry is None:
        gi = next((i for i, x in enumerate(labels) if x.get("type") == "genuine"), -1)
        labels.insert(gi + 1, dict(TEMPLATES[target]))
    else:
        entry.update(TEMPLATES[target])
    return data


# ---------------- Sheet reading ----------------
def read_skus(uploaded, pasted):
    skus = []
    if uploaded is not None:
        name = uploaded.name.lower()
        raw = uploaded.getvalue()
        if name.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(raw), dtype=str)
        else:
            text = raw.decode("utf-8-sig", errors="replace")
            sep = "\t" if "\t" in text.splitlines()[0] else ","
            df = pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False)
        col = next((c for c in df.columns if str(c).strip().lower() == "sku"), df.columns[0])
        skus += df[col].tolist()
    if pasted:
        skus += pasted.replace(",", "\n").splitlines()
    clean = []
    for s in skus:
        s = str(s).strip()
        if s and s.lower() != "nan" and s not in clean:
            clean.append(s)
    return clean


# ---------------- Plan ----------------
def build_plan(products, sheet_skus, mode, chosen):
    sheet = set(sheet_skus)
    rows, found = [], set()
    for p in products.values():
        in_sheet = p["skus"] & sheet
        found |= in_sheet
        cur = current_status(p["mf"])
        base = {
            "SKU(s)": ", ".join(sorted(p["skus"])) or "(none)",
            "Product": p["title"],
            "Shopify status": p["status"].title(),
            "Now shows": NICE[cur],
            "product_id": p["id"],
        }
        if mode == "sheet":
            if not in_sheet:
                continue
            target = chosen
        else:
            if not p["skus"]:
                rows.append({**base, "Will show": "", "Action": "Skip: product has no SKU"})
                continue
            target = "non_returnable" if in_sheet else "returnable"

        if in_sheet and in_sheet != p["skus"]:
            action = "Skip: some variants are in the sheet, some aren't"
        elif cur == "invalid":
            action = "Skip: labels JSON is broken, fix by hand"
        elif cur == target:
            action = "Already correct"
        else:
            action = "Will change"
        rows.append({**base, "Will show": NICE[target], "Action": action, "target": target})

    missing = [s for s in sheet_skus if s not in found]
    return pd.DataFrame(rows), missing


# ---------------- Page ----------------
st.set_page_config(page_title="Return Label Manager", page_icon="🏷️", layout="wide")
st.title("Return label manager")
st.caption(f"Store: {SHOP or 'not set'} · Field: {NAMESPACE}.{KEY}")

if not (SHOP and CLIENT_ID and CLIENT_SECRET):
    st.error("Set SHOPIFY_STORE, SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET in Terminal before starting the app.")
    st.stop()

st.subheader("1. Add your SKUs")
c1, c2 = st.columns(2)
uploaded = c1.file_uploader("Upload a sheet (CSV, TSV or Excel) with a SKU column", type=["csv", "tsv", "txt", "xlsx", "xls"])
pasted = c2.text_area("Or paste SKUs, one per line", height=140)

try:
    sheet_skus = read_skus(uploaded, pasted)
except Exception as e:
    st.error(f"Couldn't read that sheet: {e}")
    sheet_skus = []
if sheet_skus:
    st.success(f"{len(sheet_skus)} SKUs loaded.")

st.subheader("2. Choose what to do")
mode_label = st.radio(
    "What should change?",
    [
        "Only the SKUs in my sheet",
        "Whole store: SKUs in my sheet are Non Returnable, every other SKU is Returnable",
    ],
)
mode = "sheet" if mode_label.startswith("Only") else "sync"
chosen = None
if mode == "sheet":
    chosen = st.radio("Mark the SKUs in my sheet as", ["non_returnable", "returnable"], format_func=lambda x: NICE[x], horizontal=True)

st.subheader("3. Preview")
if st.button("Check Shopify", type="primary", disabled=not sheet_skus):
    bar = st.empty()
    try:
        products = load_store(lambda n: bar.info(f"Reading products from Shopify… {n} variants so far"))
        bar.empty()
        st.session_state["products"] = products
        st.session_state["loaded_at"] = datetime.datetime.now().strftime("%H:%M")
        st.session_state.pop("results", None)
    except Exception as e:
        bar.empty()
        st.error(str(e))

products = st.session_state.get("products")
if products and sheet_skus:
    plan, missing = build_plan(products, sheet_skus, mode, chosen)
    st.caption(f"Read {len(products)} products from Shopify at {st.session_state['loaded_at']}. Nothing has been changed yet.")

    if plan.empty:
        st.warning("None of these SKUs matched a product in Shopify.")
    else:
        to_change = plan[plan["Action"] == "Will change"]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Will change", len(to_change))
        m2.metric("Already correct", int((plan["Action"] == "Already correct").sum()))
        m3.metric("Skipped", int(plan["Action"].str.startswith("Skip").sum()))
        m4.metric("SKUs not found", len(missing))

        show_all = st.checkbox("Show every product, not just the ones that will change")
        visible = plan if show_all else plan[plan["Action"] != "Already correct"]
        st.dataframe(visible.drop(columns=["product_id", "target"], errors="ignore"), use_container_width=True, hide_index=True)
        if missing:
            with st.expander(f"{len(missing)} SKUs from your sheet aren't in Shopify"):
                st.write(", ".join(missing))

        st.download_button(
            "Download preview as CSV",
            plan.drop(columns=["product_id", "target"], errors="ignore").to_csv(index=False),
            file_name="return_label_preview.csv",
        )

        st.subheader("4. Apply to Shopify")
        if to_change.empty:
            st.info("Everything already matches. Nothing to apply.")
        else:
            test_mode = st.checkbox("Test run: apply to only a few products first", value=True)
            limit = st.number_input("How many?", min_value=1, value=1, step=1, disabled=not test_mode)
            batch = to_change.head(int(limit)) if test_mode else to_change
            confirm = st.checkbox(f"I've checked the preview. Change {len(batch)} product(s) on the live store.")

            if st.button(f"Apply to {len(batch)} product(s)", disabled=not confirm):
                os.makedirs("backups", exist_ok=True)
                stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                backup = {pid: products[pid]["mf"] for pid in batch["product_id"]}
                backup_path = f"backups/backup_{stamp}.json"
                with open(backup_path, "w", encoding="utf-8") as f:
                    json.dump(backup, f, indent=2, ensure_ascii=False)

                results, rows = [], batch.to_dict("records")
                progress = st.progress(0.0)
                for start in range(0, len(rows), BATCH_SIZE):
                    chunk = rows[start:start + BATCH_SIZE]
                    payload = []
                    for r in chunk:
                        mf = products[r["product_id"]]["mf"]
                        payload.append({
                            "ownerId": r["product_id"], "namespace": NAMESPACE, "key": KEY,
                            "type": (mf or {}).get("type") or "json",
                            "value": json.dumps(new_value(mf, r["target"]), ensure_ascii=False),
                        })
                    try:
                        errs = gql(SET_MUTATION, {"m": payload})["metafieldsSet"]["userErrors"]
                        bad = {}
                        for e in errs:
                            idx = next((int(x) for x in (e.get("field") or []) if str(x).isdigit()), None)
                            bad[idx] = e["message"]
                        for i, r in enumerate(chunk):
                            msg = bad.get(i) or (bad.get(None) if None in bad else None)
                            results.append({**r, "Result": f"Failed: {msg}" if msg else "Done"})
                    except Exception as e:
                        for r in chunk:
                            results.append({**r, "Result": f"Failed: {e}"})
                    progress.progress(min(1.0, (start + len(chunk)) / len(rows)))

                st.session_state["results"] = pd.DataFrame(results)
                st.session_state["backup_path"] = backup_path
                st.session_state.pop("products", None)  # force a fresh check next time
                st.rerun()

res = st.session_state.get("results")
if res is not None:
    done = int((res["Result"] == "Done").sum())
    failed = len(res) - done
    (st.success if not failed else st.warning)(f"Applied: {done} changed, {failed} failed. Backup saved to {st.session_state['backup_path']}.")
    st.dataframe(res[["SKU(s)", "Product", "Now shows", "Will show", "Result"]], use_container_width=True, hide_index=True)
    st.download_button("Download results as CSV", res.drop(columns=["product_id", "target"], errors="ignore").to_csv(index=False), file_name="return_label_results.csv")
    st.info("Click Check Shopify again to confirm the new labels.")
