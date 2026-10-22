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
- **Return_days:** the return window in days. Required when Return is `Yes`. Ignored when Return is `No`.
- CSV or Excel. The file name doesn't matter, and other columns are ignored. For Excel, only the first tab is read.
- Click **Sample sheet** in the app to download a ready-made template.

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

Everything is on one page.

### Search

Type part of a product name or a SKU and click **Search** (or press Enter). The results show each product's SKU, return status, return window, and Shopify status. Click **Clear search** to hide them.

### Update from a sheet

1. Upload the sheet. The app fetches the latest labels from Shopify straight away, so the preview is always current.
2. Four numbers appear: **SKUs that will change**, **Already correct**, **Draft or archived, not changed**, and **Need attention**.
3. A spreadsheet-style table lists every SKU in the sheet with:
   - **Current status** and **New status** (Returnable, Non Returnable, or No labels yet)
   - **Current return window** and **New return window** (for example `3 days` → `7 days`; `—` means not returnable)
   - **Change**: Status, Return window, Wording only, or No change

   Highlighted cells are the values that will change. SKUs that change are listed first.
4. Click **Apply changes to N SKUs on Shopify**.

After applying, the app reloads from Shopify, so the table shows the new labels and every row reads "No change". Download the **report** and the **backup of old labels** from the green banner.

---

## Rules the app follows

- **Draft and Archived products are never changed.** They're counted under "Draft or archived, not changed".
- **One badge per product.** All variants of a product show the same badge. If a product's variants have different values in the sheet:
  - if any variant is `No`, the whole product becomes **Non Returnable**;
  - if all are `Yes` with different days, the **shortest** window is used.
  These products also appear under "Need attention" so you can check them.
- **"Wording only"** in the Change column means the status and return window stay the same, but the badge text is updated to the current template.

---

## Need attention: what each message means

| Message | What to do |
|---|---|
| Not found in Shopify | Check the SKU's spelling and capital letters. |
| Return should be Yes or No | Fix the value in that row. |
| Return is Yes but Return_days is empty or not a number | Add the number of days. |
| Listed more than once with different values | Keep one row for that SKU. |
| Variants have different values | Check that the chosen badge is right for the whole product. |
| Labels in Shopify are broken | Open the product's Product Labels field in Shopify admin and fix or clear it. |

Combo or set products **without a SKU** can't be updated from a sheet. Add a SKU to them in Shopify first.

---

## Undo

The **backup of old labels** (JSON) has the previous Product Labels for every changed product, keyed by product ID. To undo one product, copy its old `value` into the product's Product Labels field in Shopify admin. If the old value is `null`, the product had no labels before, so clear the field.

---

## Setup (for whoever maintains the app)

### Shopify credentials

1. Shopify admin → **Settings → Apps and sales channels → Develop apps → Build apps in Dev Dashboard**.
2. The app needs the scopes `read_products,write_products` and must be installed on the HomeRun store.
3. Copy the **Client ID** and **Client secret** from **Overview → Credentials**. Never share the secret in chat or screenshots. If it leaks, rotate it in the app's **Settings**.

### Online (Streamlit Community Cloud)

1. Put `app.py`, `requirements.txt`, `README.md` and `.gitignore` in a **private** GitHub repo. Never commit credentials.
2. On share.streamlit.io, create the app with main file `app.py`.
3. In the app's **Settings → Secrets**, add:
   ```toml
   SHOPIFY_STORE = "b607ea-2b.myshopify.com"
   SHOPIFY_CLIENT_ID = "paste_client_id_here"
   SHOPIFY_CLIENT_SECRET = "paste_client_secret_here"
   APP_PASSWORD = "choose_a_strong_password"
   ```
4. In **Share** settings, limit viewers to your team.

**Always set `APP_PASSWORD`.** Anyone who can open the app can change the live store.

Vercel and Netlify can't run this app (they give `PYTHON_ENTRYPOINT_NOT_FOUND`). Use Streamlit Community Cloud, Render, or Railway.

### On a Mac

```bash
cd ~/Downloads/return_label_app
pip3 install -r requirements.txt
export SHOPIFY_STORE=b607ea-2b.myshopify.com
export SHOPIFY_CLIENT_ID=paste_client_id_here
export SHOPIFY_CLIENT_SECRET=paste_client_secret_here
python3 -m streamlit run app.py
```

Run the `export` lines again in every new Terminal window.

### Changing the badge text

All text and icons are at the top of `app.py`: `returnable_block()` for Returnable, `NON_RETURNABLE` for Non Returnable, and `DEFAULT_LABELS` for the Genuine and Replacement badges on products with no labels. `API_VERSION` should be updated about once a year.

---

## Limitations

- Different badges per variant aren't possible with the current Product Labels field; that would need a variant-level field read by the mobile app.
- The app doesn't update `custom.rr_config` (the return rules used for actual returns). If that field drives returns, keep it in sync separately.
- There's no test mode or confirmation step: clicking Apply changes the live store straight away. Check the table before clicking.