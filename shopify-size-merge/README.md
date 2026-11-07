# Shopify size merge

This tool finds products that are the same artwork in different sizes, for example
*Pitcher of Flowers — Framed Poster* 30×40 as one product and 50×70 as another.
It merges each group into **one product with a Size option**, then archives the
extra products and adds 301 redirects from their URLs to the product you kept.

It needs Python 3.8 or newer and nothing else to install.

## 1. Create the app (once)

1. In the Shopify **Dev Dashboard**, create an app and install it on your store.
2. Give it these Admin API scopes: `read_products`, `write_products`,
   `read_online_store_navigation` and `write_online_store_navigation`
   (the last two are for the URL redirects).
3. Copy the app's **Client ID** and **Client secret**.

## 2. Make a plan (changes nothing)

1. Open `merge_sizes.py` in PyCharm and fill in the **SETTINGS** at the top:
   `STORE`, `CLIENT_ID` and `CLIENT_SECRET`. Leave `MODE = "plan"`.
2. Right-click `merge_sizes.py` and choose **Run 'merge_sizes'**.

**This repository is public.** Never commit or push `merge_sizes.py` with your Client
secret in it: delete the secret again before you commit.

To read fewer products, change `QUERY`, for example
`QUERY = "status:active product_type:'Framed Poster'"`.

This writes `plan.json` and prints every group:

```
MERGE  'Pitcher of Flowers — Framed Poster'  ->  sizes: 30 × 40 cm, 40 × 50 cm, 50 × 70 cm
       keep   /products/pitcher-of-flowers-...
       archive + 301  /products/pitcher-of-flowers-...-30x40-cm-...
```

### How it groups and chooses

- Products are grouped when their **title is the same after removing the size**,
  they have the **same product type** and the **same `format:` tag** (`format:fine_art`
  and `format:fine-art` count as the same). A Framed Poster is never merged with a Poster,
  and Fine Art is never merged with a Framed Poster.
- A group is merged only if **every product has the same main image** (same original
  file size and pixel dimensions). The same title can be two different paintings:
  Monet painted about 250 *Water Lilies*. A group with different images, or a product
  with no image, is marked `SKIP`. If your size listings use a different mockup per
  size, those groups are skipped too: merge them by hand after checking the images.
- 30×40 and 40×30 count as the same size.
- The **keeper** is the product with the most sizes. On a tie, it's the oldest one,
  because its URL has usually built up the most ranking.
- If the keeper already has a size, its own price stays. Any duplicate with a
  different price for that size is listed as a `warning`.
- Groups it can't handle safely are marked `SKIP` with the reason. Examples: products
  with a second option such as Frame colour, or no size found in the title or URL.
  Merge those by hand.

### Edit `plan.json` if you want

- **Different keeper:** swap the `keeper` entry with one of the `duplicates` entries.
  For example, keep the product with the most clicks in Google Search Console.
- **Remove the size from the URL:** copy `suggested_handle` into `new_handle`.
  Shopify adds the 301 from the old URL. Only do this on products that don't rank
  well yet.
- **Leave a group out:** set `"skip": "manual"`.

## 3. Apply, starting small

In the SETTINGS, set `MODE = "apply"` and keep `LIMIT = 1`. Then click **Run**.

For each group, the tool:

1. adds the missing sizes to the keeper, copying price, compare-at price, SKU, cost,
   weight and barcode. Sizes read from a title or URL are written in the store's style,
   for example `50x70 cm / 20x28"`;
2. re-reads the keeper and checks every size is there before going on;
3. sorts the sizes from small to large, and adds a `size:50x70cm` tag for each added
   size if the keeper already has `size:` tags;
4. changes the URL, if `new_handle` is set;
5. archives the duplicates (they are archived, not deleted, so you can undo it);
6. adds the 301 redirects.

If anything fails, that group stops before any duplicate is archived. Finished
groups are marked `"done": true` in `plan.json`, so running it again skips them.

**After your first group, check these by hand before running more groups:**

- The product page shows the size picker with the right prices.
- The old URL redirects to the kept product.
- The order shows the size you need. Add a test item to the cart: the cart and
  checkout should show the size name (for example "50x70 cm") and the right price.
- Move the reviews from the archived products to the kept product in your
  review app.

Then set `LIMIT = 5`, check Google Search Console and Merchant Center for about 2 weeks,
and do the rest with `LIMIT = None`.

## Tests (optional)

You don't need `test_merge_sizes.py` to merge anything. It checks the script itself, on
made-up products, without touching your store: for example that two different paintings
with the same title are never merged. Run it only after someone changes the script:
right-click it and choose **Run 'Python tests in test_merge_sizes'**.
