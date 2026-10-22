# Return Labels

An internal app for category managers to set the **return badge** on HomeRun products. Upload a sheet, check what will change, and apply it to Shopify in one click. The mobile app reads the same field, so changes show up there too.

---

## The sheet

One row per SKU, with three columns:

| SKU | Return | Return_days |
|---|---|---|
| HZKY8 | No | |
| SKYJQ | Yes | 7 |
| QMJFF | Yes | 3 |

- **SKU:** must match Shopify exactly, including capital letters.
- **Return:** `Yes` or `No` (capitals don't matter).
- **Return_days:** the return window in days. When Return is `Yes`, use any whole number (for example 0, 3 or 7); that's exactly what the badge will say. When Return is `No`, use `0` or leave it empty.
- CSV or Excel. The file name doesn't matter, and other columns are ignored. For Excel, only the first tab is read.

Only the SKUs in the sheet are changed. Every other product is left as it is.

---

## What the badge says

**Return = Yes**, for example with Return_days = 7:

- Title: `7 Days` · Sub title: `Returnable` · Heading: `Easy Returns`
- Text: *You may return this product within 7 days of purchase. Product needs to be unopened/unused/in its original condition to be eligible.*

**Return = No:**

- Title: `Non` · Sub title: `Returnable` · Heading: `Non Returnable`
- Text: *This product is not eligible for returns. No questions asked replacement for any quality concerns.*

Only the Return block in the product's **Product Labels** field (`custom.product_labels`) is changed. The Genuine and Replacement badges are left alone. If a product has no labels yet, all three badges are created.

---

## Using the app

### Sign in

Each person signs in with their own work email and password, so every change is recorded under their name. Passwords are set by whoever maintains the app (see Setup).

### Update tab

There are two ways to fill the table:

- **Search** by product name or SKU. The table shows the matching SKUs with their current values. Change **New: Return** (Yes/No) and **New: Return days** for the ones you want to update. Rows you don't touch aren't changed.
- **Upload a sheet** with SKU, Return and Return_days. The table is filled from the sheet, and you can still correct any value in it before applying. The line under the table shows which file it came from.

Click **Clear** to empty the search box and the table.

The table shows 10 rows at a time. Use **‹** and **›** under it to move between pages; your changes are kept. The line under the table shows how many SKUs will change in total. Click **Apply** to update Shopify.

The app follows one global rule: **non-returnable means a return window of 0.** Picking **No** in the table fills the days box with 0 automatically, and picking **Yes** clears it so you can type the days.

The app won't apply anything while there are errors:

- **Return = No with days above 0** (for example 3 or 7). Set the days to 0.
- **Return = Yes with the days left empty.** Enter the return window (0 is allowed).
- **Days that aren't a whole number.**

Notes (SKUs not found in Shopify, Draft or Archived products, and so on) appear under the table but don't block applying.

### History tab

Every change made in the app: when (IST), who, which SKU, the product, and the label before and after. Click **Download** to get it as a CSV.

History is saved on each product in Shopify (`custom.return_label_history`, the last 20 changes per product), so it survives app restarts. Changes made directly in Shopify admin aren't listed.

---

## Rules the app follows

- **Draft and Archived products are never changed.** They show up as notes.
- **One badge per product.** All variants of a product show the same badge. If a product's variants get different values, the whole product becomes **Non Returnable** when any variant is `No`, and otherwise uses the **shortest** return window. This is listed as a note.
- Products with no labels yet get all three badges (Genuine, Return, Replacement).
- Combo or set products **without a SKU** can't be found or updated. Add a SKU to them in Shopify first.

---

## Undo

Search for the SKU, set it back to the value in the **Before** column of the History tab, and apply.

---

## Setup (for whoever maintains the app)

### Shopify credentials

1. Shopify admin → **Settings → Apps and sales channels → Develop apps → Build apps in Dev Dashboard**.
2. The app needs the scopes `read_products,write_products` and must be installed on the HomeRun store.
3. Copy the **Client ID** and **Client secret** from **Overview → Credentials**. Never share the secret in chat or screenshots. If it leaks, rotate it in the app's **Settings**.

### Secrets and users

All settings live in Secrets. `.streamlit/secrets.toml.example` shows the format:

```toml
SHOPIFY_STORE = "b607ea-2b.myshopify.com"
SHOPIFY_CLIENT_ID = "paste_client_id_here"
SHOPIFY_CLIENT_SECRET = "paste_client_secret_here"

[users]
"anushka@home-run.co" = "choose-a-password"
"category.manager@home-run.co" = "another-password"
```

- **To add a person:** add a line under `[users]` with their email and a password, then save. They can sign in right away.
- **To remove a person:** delete their line.
- Emails must be in quotes, and capitals don't matter.

### Online (Streamlit Community Cloud)

1. Put `app.py`, `requirements.txt`, `README.md`, `.gitignore` and `.streamlit/secrets.toml.example` in a **private** GitHub repo. Never commit the real `secrets.toml`.
2. On share.streamlit.io, create the app with main file `app.py`.
3. Paste your secrets into the app's **Settings → Secrets**.
4. In **Share** settings, limit viewers to your team.

Vercel and Netlify can't run this app (they give `PYTHON_ENTRYPOINT_NOT_FOUND`). Use Streamlit Community Cloud, Render, or Railway.

### On a Mac

1. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` in the app folder and fill in the real values.
2. Run:
   ```bash
   cd ~/Downloads/return_label_app
   pip3 install -r requirements.txt
   python3 -m streamlit run app.py
   ```

The `NotOpenSSLWarning` in Terminal is harmless.

### Changing the badge text

All text and icons are at the top of `app.py`: `returnable_block()` for Returnable, `NON_RETURNABLE` for Non Returnable, and `DEFAULT_LABELS` for the Genuine and Replacement badges on products with no labels. `API_VERSION` should be updated about once a year.

---

## Limitations

- Different badges per variant aren't possible with the current Product Labels field; that would need a variant-level field read by the mobile app.
- The app doesn't update `custom.rr_config` (the return rules used for actual returns). If that field drives returns, keep it in sync separately.
- Passwords are stored in Secrets, so whoever manages Secrets can see them. Give each person their own password and change it if someone leaves.