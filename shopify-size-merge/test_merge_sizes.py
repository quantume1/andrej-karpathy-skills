import copy
import re
import unittest

from merge_sizes import (ApplyError, Size, apply_group, build_plan, handle_without_size,
                         normalize_title, parse_size)

HANDLE = ("pitcher-of-flowers-odilon-redon-framed-poster-30x40-cm-12x16-inches-"
          "odilon-redon-framed-art-print")


def variant(pid, i, value, option, price):
    return {"id": f"{pid}/v{i}", "title": value, "price": price, "compareAtPrice": "249.99",
            "sku": f"SKU-{pid}-{i}", "barcode": None, "inventoryPolicy": "CONTINUE", "taxable": True,
            "selectedOptions": [{"name": option, "value": value}],
            "inventoryItem": {"tracked": False, "requiresShipping": True, "unitCost": {"amount": "55.0"},
                              "measurement": {"weight": {"value": 1.2, "unit": "KILOGRAMS"}}}}


def product(pid, title, handle, sizes=None, price="199.99", created="2024-01-01",
            ptype="Framed Poster", prices=None):
    if sizes is None:  # one size, no options (Shopify's "Default Title")
        options, variants = [{"name": "Title"}], [variant(pid, 0, "Default Title", "Title", price)]
    else:
        prices = prices or [price] * len(sizes)
        options = [{"name": "Size"}]
        variants = [variant(pid, i, s, "Size", prices[i]) for i, s in enumerate(sizes)]
    return {"id": pid, "title": title, "handle": handle, "productType": ptype, "createdAt": created,
            "status": "ACTIVE", "options": options, "variants": {"nodes": variants}}


class Parsing(unittest.TestCase):
    def test_size_from_handle(self):
        self.assertEqual(parse_size(HANDLE), Size("30x40cm", "30 × 40 cm / 12 × 16 in", 1200))

    def test_size_from_option_values(self):
        self.assertEqual(parse_size("40 × 50 cm").key, "40x50cm")
        self.assertEqual(parse_size("50x70cm / 20x28in").key, "50x70cm")
        self.assertEqual(parse_size("12×16″").key, "12x16in")
        self.assertIsNone(parse_size("Pitcher of Flowers"))

    def test_title_without_size(self):
        self.assertEqual(normalize_title("PITCHER OF FLOWERS — Framed Poster 30x40 cm"),
                         normalize_title("Pitcher of Flowers — Framed Poster"))

    def test_handle_without_size(self):
        self.assertEqual(handle_without_size(HANDLE),
                         "pitcher-of-flowers-odilon-redon-framed-poster-odilon-redon-framed-art-print")


class Planning(unittest.TestCase):
    def test_single_size_product_merges_into_multi_size_product(self):
        single = product("A", "PITCHER OF FLOWERS — Framed Poster", HANDLE, created="2023-01-01")
        multi = product("B", "Pitcher of Flowers — Framed Poster", "pitcher-of-flowers-framed",
                        sizes=["30 × 40 cm", "40 × 50 cm", "50 × 70 cm"],
                        prices=["189.99", "229.99", "289.99"], created="2024-05-01")
        other = product("C", "Starry Night — Framed Poster", "starry-night")
        unframed = product("D", "Pitcher of Flowers — Poster 30x40 cm", "pitcher-poster", ptype="Poster")
        plan = build_plan([single, multi, other, unframed])

        self.assertEqual(len(plan), 1)
        g = plan[0]
        self.assertEqual(g["keeper"]["id"], "B")  # most sizes wins
        self.assertEqual([d["id"] for d in g["duplicates"]], ["A"])
        self.assertEqual(g["adds"], [])
        self.assertEqual(g["sizes_after"], ["30 × 40 cm", "40 × 50 cm", "50 × 70 cm"])
        self.assertIn("199.99", g["warnings"][0])  # same size, different price is reported

    def test_two_single_size_products_keep_the_oldest(self):
        a = product("A", "Pitcher of Flowers — Framed Poster", HANDLE, created="2023-01-01")
        b = product("B", "Pitcher of Flowers — Framed Poster 50x70 cm", "pitcher-50x70-cm",
                    price="279.99", created="2024-01-01")
        g = build_plan([b, a])[0]
        self.assertEqual(g["keeper"]["id"], "A")
        self.assertEqual(g["adds"], ["50 × 70 cm"])
        self.assertEqual(g["sizes_after"], ["30 × 40 cm / 12 × 16 in", "50 × 70 cm"])
        self.assertEqual(g["suggested_handle"],
                         "pitcher-of-flowers-odilon-redon-framed-poster-odilon-redon-framed-art-print")

    def test_unsupported_options_are_skipped(self):
        a = product("A", "Pitcher of Flowers — Framed Poster", HANDLE)
        b = product("B", "Pitcher of Flowers — Framed Poster", "b", sizes=["40 × 50 cm"])
        b["options"].append({"name": "Frame"})
        plan = build_plan([a, b])
        self.assertIn("only a single Size option", plan[0]["skip"])


class FakeShop:
    """Just enough of Shopify's Admin API to run apply_group."""

    def __init__(self, *products, fail=()):
        self.products = {p["id"]: copy.deepcopy(p) for p in products}
        self.calls, self.fail = [], set(fail)

    def gql(self, query, variables=None):
        op = re.search(r"(?:query|mutation)\s+(\w+)", query)[1]
        v = variables or {}
        self.calls.append((op, v))
        if op == "Product":
            return {"product": copy.deepcopy(self.products.get(v["id"]))}
        field = re.search(r"\{\s*(\w+)\(", query)[1]
        if op in self.fail:
            return {field: {"userErrors": [{"field": None, "message": "boom"}]}}
        if op == "OptionsCreate":
            p = self.products[v["productId"]]
            value = v["options"][0]["values"][0]["name"]
            p["options"] = [{"name": v["options"][0]["name"]}]
            p["variants"]["nodes"][0]["selectedOptions"] = [{"name": p["options"][0]["name"], "value": value}]
            return {field: {"userErrors": []}}
        if op == "VariantsCreate":
            p, created = self.products[v["productId"]], []
            for nv in v["variants"]:
                ov = nv["optionValues"][0]
                node = {"id": f"{p['id']}/new{len(p['variants']['nodes'])}", "title": ov["name"],
                        "price": nv["price"], "selectedOptions": [{"name": ov["optionName"], "value": ov["name"]}]}
                p["variants"]["nodes"].append(node)
                created.append({"id": node["id"], "title": node["title"]})
            return {field: {"productVariants": created, "userErrors": []}}
        if op == "ProductUpdate":
            p = self.products[v["product"]["id"]]
            p["handle"] = v["product"].get("handle", p["handle"])
            p["status"] = v["product"].get("status", p["status"])
            return {field: {"product": {"id": p["id"], "handle": p["handle"]}, "userErrors": []}}
        return {field: {"userErrors": []}}  # OptionsReorder, RedirectCreate

    def ops(self):
        return [op for op, _ in self.calls]

    def vars(self, op):
        return [v for o, v in self.calls if o == op]


def group(keeper, *dups, new_handle=None):
    return {"keeper": {"id": keeper}, "duplicates": [{"id": d} for d in dups], "new_handle": new_handle}


class Applying(unittest.TestCase):
    def setUp(self):
        self.a = product("A", "Pitcher of Flowers — Framed Poster", HANDLE)
        self.b = product("B", "Pitcher of Flowers — Framed Poster 50x70 cm", "pitcher-50x70-cm",
                         price="279.99")

    def test_happy_path_order_and_copied_fields(self):
        shop = FakeShop(self.a, self.b)
        apply_group(shop, group("A", "B"), log=lambda *_: None)

        self.assertEqual(shop.ops(), ["Product", "Product", "OptionsCreate", "VariantsCreate", "Product",
                                      "OptionsReorder", "ProductUpdate", "RedirectCreate"])
        new = shop.vars("VariantsCreate")[0]["variants"][0]
        self.assertEqual(new["optionValues"], [{"optionName": "Size", "name": "50 × 70 cm"}])
        self.assertEqual((new["price"], new["compareAtPrice"]), ("279.99", "249.99"))
        self.assertEqual(new["inventoryItem"]["sku"], "SKU-B-0")
        self.assertEqual(new["inventoryItem"]["cost"], "55.0")
        self.assertNotIn("barcode", new)
        keeper = shop.products["A"]["variants"]["nodes"]
        self.assertEqual([v["id"] for v in keeper], ["A/v0", "A/new1"])  # original variant kept
        self.assertEqual(shop.products["B"]["status"], "ARCHIVED")
        self.assertEqual(shop.vars("RedirectCreate")[0]["urlRedirect"],
                         {"path": "/products/pitcher-50x70-cm", "target": f"/products/{HANDLE}"})

    def test_failed_variant_create_never_archives(self):
        shop = FakeShop(self.a, self.b, fail={"VariantsCreate"})
        with self.assertRaises(ApplyError):
            apply_group(shop, group("A", "B"), log=lambda *_: None)
        self.assertNotIn("ProductUpdate", shop.ops())
        self.assertEqual(shop.products["B"]["status"], "ACTIVE")

    def test_new_handle_is_the_redirect_target(self):
        shop = FakeShop(self.a, self.b)
        apply_group(shop, group("A", "B", new_handle="pitcher-of-flowers-framed-poster"), log=lambda *_: None)
        rename = shop.vars("ProductUpdate")[0]["product"]
        self.assertEqual(rename, {"id": "A", "handle": "pitcher-of-flowers-framed-poster",
                                  "redirectNewHandle": True})
        self.assertEqual(shop.vars("RedirectCreate")[0]["urlRedirect"]["target"],
                         "/products/pitcher-of-flowers-framed-poster")

    def test_exact_duplicate_is_archived_without_adding_variants(self):
        twin = product("C", "Pitcher of Flowers — Framed Poster", "pitcher-copy-30x40-cm")
        shop = FakeShop(self.a, twin)
        apply_group(shop, group("A", "C"), log=lambda *_: None)
        self.assertNotIn("VariantsCreate", shop.ops())
        self.assertNotIn("OptionsCreate", shop.ops())
        self.assertEqual(shop.products["C"]["status"], "ARCHIVED")


if __name__ == "__main__":
    unittest.main()
