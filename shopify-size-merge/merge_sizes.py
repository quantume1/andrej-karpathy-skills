#!/usr/bin/env python3
"""
Merge Shopify products that are the same artwork in different sizes into ONE
product with a Size option.

  python merge_sizes.py plan                # read-only: writes plan.json, changes nothing
  python merge_sizes.py apply --limit 1     # merge the first group in plan.json

Environment variables:
  SHOPIFY_STORE          your-store.myshopify.com (the myshopify domain, not zephyeer.com)
  SHOPIFY_CLIENT_ID      from your app in the Shopify Dev Dashboard
  SHOPIFY_CLIENT_SECRET

What "apply" does for each group, in this order. It stops that group at the
first error, so a duplicate is only archived once the keeper has all its sizes.
  1. adds the missing sizes as variants on the keeper product (copies price,
     compare-at price, SKU, cost, weight and barcode from the duplicate)
  2. re-reads the keeper and checks every size is there and no old variant is gone
  3. sorts the sizes small -> large
  4. if you set "new_handle" in plan.json: changes the keeper's URL (Shopify adds the 301)
  5. archives each duplicate (archived, not deleted, so you can undo it)
  6. adds a 301 redirect /products/<duplicate-handle> -> keeper
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

API_VERSION = "2026-01"
VARIANTS_PAGE = 20  # products with this many variants or more are left for manual work

CM_RE = re.compile(r"(\d{2,3})\s*[x×]\s*(\d{2,3})\s*cm", re.I)
IN_RE = re.compile(r"(\d{1,2})\s*[x×]\s*(\d{1,2})\s*(?:inch|in\b|\"|”|″)", re.I)
SIZE_TEXT_RE = re.compile(r"\d+\s*[x×]\s*\d+\s*(?:cm|inch(?:es)?|in\b|\"|”|″)?", re.I)


class Size(NamedTuple):
    key: str  # "30x40cm": two sizes with the same key are the same size
    label: str  # what customers see, e.g. "30 × 40 cm / 12 × 16 in"
    area: float  # for sorting small -> large


class Skip(Exception):
    """This group can't be merged automatically; do it by hand."""


class ApplyError(Exception):
    """Shopify refused a change, or the result didn't check out."""


# ---------------------------------------------------------------- parsing

def parse_size(text):
    text = (text or "").replace("-", " ")
    cm, inch = CM_RE.search(text), IN_RE.search(text)
    if cm:
        w, h = int(cm[1]), int(cm[2])
        label = f"{w} × {h} cm" + (f" / {inch[1]} × {inch[2]} in" if inch else "")
        return Size(f"{w}x{h}cm", label, w * h)
    if inch:
        w, h = int(inch[1]), int(inch[2])
        return Size(f"{w}x{h}in", f"{w} × {h} in", w * h * 6.4516)
    return None


def normalize_title(title):
    """'PITCHER OF FLOWERS — Framed Poster 30x40 cm' -> 'pitcher of flowers framed poster'"""
    t = SIZE_TEXT_RE.sub(" ", title.lower())
    return " ".join(re.sub(r"\W+", " ", t).split())


def handle_without_size(handle):
    return "-".join(normalize_title(handle.replace("-", " ")).split())


def group_key(p):
    return f"{normalize_title(p['title'])} | {(p.get('productType') or '').lower()}"


def product_sizes(p):
    """Return (size option name, or None if the product has no options, [(variant, Size), ...])."""
    variants = p["variants"]["nodes"]
    names = [o["name"] for o in p["options"]]
    if len(variants) >= VARIANTS_PAGE:
        raise Skip(f"'{p['title']}' has {VARIANTS_PAGE}+ variants")
    if names == ["Title"] and len(variants) == 1:
        size = parse_size(p["title"]) or parse_size(p["handle"])
        if not size:
            raise Skip(f"no size found in the title or URL of '{p['title']}'")
        return None, [(variants[0], size)]
    if len(names) != 1:
        raise Skip(f"'{p['title']}' has options {names}; only a single Size option is supported")
    out = []
    for v in variants:
        value = v["selectedOptions"][0]["value"]
        size = parse_size(value)
        if not size:
            raise Skip(f"'{p['title']}': option value '{value}' is not a size")
        out.append((v, size._replace(label=value)))  # keep the label already in the store
    return names[0], out


# ---------------------------------------------------------------- planning

def compute_merge(keeper, duplicates):
    option_name, keeper_sizes = product_sizes(keeper)
    have = {s.key: (v, s) for v, s in keeper_sizes}
    to_add, warnings = [], []
    for d in duplicates:
        for v, s in product_sizes(d)[1]:
            if s.key in have:
                kept = have[s.key][0]["price"]
                if v["price"] != kept:
                    warnings.append(f"{s.label}: '{d['title']}' costs {v['price']}, "
                                    f"merged product keeps {kept}")
                continue
            have[s.key] = (v, s)
            to_add.append((v, s))
    return {
        "option_name": option_name or "Size",
        # a product with no options first needs a Size option for its one variant
        "create_option_with": keeper_sizes[0][1].label if option_name is None and to_add else None,
        "to_add": to_add,
        "sizes_after": sorted((s for _, s in have.values()), key=lambda s: s.area),
        "warnings": warnings,
    }


def brief(p):
    return {"id": p["id"], "title": p["title"], "handle": p["handle"]}


def build_plan(products):
    groups = {}
    for p in products:
        groups.setdefault(group_key(p), []).append(p)
    plan = []
    for ps in groups.values():
        if len(ps) < 2:
            continue
        try:
            counts = {p["id"]: len(product_sizes(p)[1]) for p in ps}
            # keeper = the product with the most sizes; on a tie, the oldest URL
            keeper, *dups = sorted(ps, key=lambda p: (-counts[p["id"]], p["createdAt"]))
            m = compute_merge(keeper, dups)
        except Skip as e:
            plan.append({"skip": str(e), "products": [brief(p) for p in ps]})
            continue
        suggested = handle_without_size(keeper["handle"])
        plan.append({
            "keeper": brief(keeper),
            "duplicates": [brief(d) for d in dups],
            "sizes_after": [s.label for s in m["sizes_after"]],
            "adds": [s.label for _, s in m["to_add"]],
            "warnings": m["warnings"],
            "suggested_handle": suggested if suggested != keeper["handle"] else None,
            "new_handle": None,
            "skip": None,
            "done": False,
        })
    return sorted(plan, key=lambda g: (g["skip"] is not None, (g.get("keeper") or {}).get("title", "")))


# ---------------------------------------------------------------- Shopify API

LIST_QUERY = """query Products($cursor: String, $q: String) {
  products(first: 10, after: $cursor, query: $q) {
    nodes { id title handle productType createdAt options { name }
      variants(first: %d) { nodes { id title price selectedOptions { name value } } } }
    pageInfo { hasNextPage endCursor } } }""" % VARIANTS_PAGE

PRODUCT_QUERY = """query Product($id: ID!) {
  product(id: $id) { id title handle productType createdAt options { name }
    variants(first: %d) { nodes {
      id title price compareAtPrice sku barcode inventoryPolicy taxable
      selectedOptions { name value }
      inventoryItem { tracked requiresShipping unitCost { amount }
                      measurement { weight { value unit } } } } } } }""" % VARIANTS_PAGE

OPTIONS_CREATE = """mutation OptionsCreate($productId: ID!, $options: [OptionCreateInput!]!) {
  productOptionsCreate(productId: $productId, options: $options, variantStrategy: LEAVE_AS_IS) {
    userErrors { field message code } } }"""

VARIANTS_CREATE = """mutation VariantsCreate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkCreate(productId: $productId, variants: $variants) {
    productVariants { id title } userErrors { field message code } } }"""

OPTIONS_REORDER = """mutation OptionsReorder($productId: ID!, $options: [OptionReorderInput!]!) {
  productOptionsReorder(productId: $productId, options: $options) {
    userErrors { field message code } } }"""

PRODUCT_UPDATE = """mutation ProductUpdate($product: ProductUpdateInput!) {
  productUpdate(product: $product) { product { id handle } userErrors { field message } } }"""

REDIRECT_CREATE = """mutation RedirectCreate($urlRedirect: UrlRedirectInput!) {
  urlRedirectCreate(urlRedirect: $urlRedirect) { urlRedirect { id } userErrors { field message code } } }"""


class Shopify:
    def __init__(self, store, client_id, client_secret):
        body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                       "client_id": client_id, "client_secret": client_secret}).encode()
        req = urllib.request.Request(f"https://{store}/admin/oauth/access_token", data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req) as r:
                self.token = json.load(r)["access_token"]
        except urllib.error.HTTPError as e:
            sys.exit(f"Could not get an access token ({e.code}): {e.read().decode()[:300]}")
        self.url = f"https://{store}/admin/api/{API_VERSION}/graphql.json"

    def gql(self, query, variables=None):
        payload = json.dumps({"query": query, "variables": variables or {}}).encode()
        for attempt in range(6):
            req = urllib.request.Request(self.url, data=payload, headers={
                "Content-Type": "application/json", "X-Shopify-Access-Token": self.token})
            try:
                with urllib.request.urlopen(req) as r:
                    data = json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                sys.exit(f"Shopify HTTP {e.code}: {e.read().decode()[:500]}")
            errors = data.get("errors")
            if errors and attempt < 5 and any(
                    (err.get("extensions") or {}).get("code") == "THROTTLED" for err in errors):
                time.sleep(2 ** attempt)
                continue
            if errors:
                sys.exit(f"Shopify GraphQL error: {errors}")
            return data["data"]


def mutate(client, query, variables, field):
    res = client.gql(query, variables)[field]
    if res.get("userErrors"):
        raise ApplyError(f"{field}: {res['userErrors']}")
    return res


def fetch_all_products(client, search):
    products, cursor = [], None
    while True:
        page = client.gql(LIST_QUERY, {"cursor": cursor, "q": search})["products"]
        products += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return products
        cursor = page["pageInfo"]["endCursor"]


def fetch_product(client, pid):
    p = client.gql(PRODUCT_QUERY, {"id": pid})["product"]
    if p is None:
        raise ApplyError(f"product {pid} not found")
    return p


def variant_input(v, size, option_name):
    inv = v["inventoryItem"]
    item = {"sku": v["sku"], "tracked": inv["tracked"], "requiresShipping": inv["requiresShipping"]}
    if inv.get("unitCost"):
        item["cost"] = inv["unitCost"]["amount"]
    weight = (inv.get("measurement") or {}).get("weight")
    if weight:
        item["measurement"] = {"weight": {"value": weight["value"], "unit": weight["unit"]}}
    out = {"optionValues": [{"optionName": option_name, "name": size.label}],
           "price": v["price"], "compareAtPrice": v["compareAtPrice"], "barcode": v["barcode"],
           "inventoryPolicy": v["inventoryPolicy"], "taxable": v["taxable"], "inventoryItem": item}
    return {k: val for k, val in out.items() if val is not None}


def apply_group(client, group, log=print):
    """Merge one plan group."""
    keeper = fetch_product(client, group["keeper"]["id"])
    dups = [fetch_product(client, d["id"]) for d in group["duplicates"]]
    m = compute_merge(keeper, dups)
    pid, opt = keeper["id"], m["option_name"]
    old_ids = {v["id"] for v in keeper["variants"]["nodes"]}
    for w in m["warnings"]:
        log(f"  warning: {w}")

    # 1. add the missing sizes
    if m["create_option_with"]:
        mutate(client, OPTIONS_CREATE, {"productId": pid, "options": [
            {"name": opt, "values": [{"name": m["create_option_with"]}]}]}, "productOptionsCreate")
    if m["to_add"]:
        mutate(client, VARIANTS_CREATE, {"productId": pid, "variants": [
            variant_input(v, s, opt) for v, s in m["to_add"]]}, "productVariantsBulkCreate")
        log(f"  added sizes: {', '.join(s.label for _, s in m['to_add'])}")

    # 2. check before touching the duplicates
    after = fetch_product(client, pid)
    have = sorted(s.key for _, s in product_sizes(after)[1])
    want = sorted(s.key for s in m["sizes_after"])
    if have != want or not old_ids <= {v["id"] for v in after["variants"]["nodes"]}:
        raise ApplyError(f"check failed: keeper has {have}, expected {want}. Duplicates NOT archived.")

    # 3. sizes small -> large (cosmetic, so a failure is only a warning)
    if len(m["sizes_after"]) > 1:
        try:
            mutate(client, OPTIONS_REORDER, {"productId": pid, "options": [
                {"name": opt, "values": [{"name": s.label} for s in m["sizes_after"]]}]},
                "productOptionsReorder")
        except ApplyError as e:
            log(f"  warning: could not sort sizes: {e}")

    # 4. optional new URL for the keeper
    handle = after["handle"]
    if group.get("new_handle"):
        res = mutate(client, PRODUCT_UPDATE, {"product": {
            "id": pid, "handle": group["new_handle"], "redirectNewHandle": True}}, "productUpdate")
        handle = res["product"]["handle"]
        log(f"  URL changed: /products/{after['handle']} -> /products/{handle} (301 added by Shopify)")

    # 5 + 6. archive duplicates and redirect their URLs to the keeper
    for d in dups:
        mutate(client, PRODUCT_UPDATE, {"product": {"id": d["id"], "status": "ARCHIVED"}}, "productUpdate")
        path, target = f"/products/{d['handle']}", f"/products/{handle}"
        try:
            mutate(client, REDIRECT_CREATE, {"urlRedirect": {"path": path, "target": target}},
                   "urlRedirectCreate")
            log(f"  archived '{d['title']}', 301 {path} -> {target}")
        except ApplyError as e:
            log(f"  archived '{d['title']}' but the REDIRECT FAILED, add it by hand: "
                f"{path} -> {target} ({e})")


# ---------------------------------------------------------------- command line

def env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Set the {name} environment variable first.")
    return value


def cmd_plan(client, args):
    products = fetch_all_products(client, args.query)
    plan = build_plan(products)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    todo = [g for g in plan if not g["skip"]]
    print(f"Read {len(products)} products: {len(todo)} groups to merge, "
          f"{len(plan) - len(todo)} need manual work.\n")
    for g in plan:
        if g["skip"]:
            print(f"SKIP   {g['skip']}")
            continue
        print(f"MERGE  '{g['keeper']['title']}'  ->  sizes: {', '.join(g['sizes_after'])}")
        print(f"       keep   /products/{g['keeper']['handle']}")
        for d in g["duplicates"]:
            print(f"       archive + 301  /products/{d['handle']}")
        for w in g["warnings"]:
            print(f"       warning: {w}")
    print(f"\nNothing was changed. Review {args.out}, then run:  python merge_sizes.py apply --limit 1")


def cmd_apply(client, args):
    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)
    todo = [g for g in plan if not g.get("skip") and not g.get("done")][:args.limit]
    for i, g in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {g['keeper']['title']}")
        try:
            apply_group(client, g)
        except (ApplyError, Skip) as e:
            print(f"  STOPPED: {e}")
            continue
        g["done"] = True
        with open(args.plan, "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=2, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="read the store and write plan.json (changes nothing)")
    p.add_argument("--query", default="status:active",
                   help="Shopify product search, e.g. \"status:active product_type:'Framed Poster'\"")
    p.add_argument("--out", default="plan.json")
    a = sub.add_parser("apply", help="merge the groups in plan.json")
    a.add_argument("--plan", default="plan.json")
    a.add_argument("--limit", type=int, help="merge only this many groups (start with 1)")
    args = ap.parse_args()
    client = Shopify(env("SHOPIFY_STORE"), env("SHOPIFY_CLIENT_ID"), env("SHOPIFY_CLIENT_SECRET"))
    (cmd_plan if args.cmd == "plan" else cmd_apply)(client, args)


if __name__ == "__main__":
    main()
