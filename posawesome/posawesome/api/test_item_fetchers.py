import importlib.util
import pathlib
import sys
import types
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
STUBBED_MODULE_NAMES = (
    "frappe",
    "frappe.query_builder",
    "frappe.query_builder.functions",
    "frappe.utils",
    "frappe.utils.caching",
    "erpnext.setup.utils",
)


class AttrDict(dict):
    __getattr__ = dict.get


def _install_stubs():
    frappe_module = types.ModuleType("frappe")
    frappe_module._dict = lambda value=None: AttrDict(value or {})
    frappe_module.get_all = lambda *args, **kwargs: []
    frappe_module.get_cached_value = lambda *args, **kwargs: None
    frappe_module.get_value = lambda *args, **kwargs: None
    frappe_module.log_error = lambda *args, **kwargs: None

    class _Db:
        def has_column(self, doctype, fieldname):
            if doctype == "Item" and fieldname in {"valuation_rate", "default_bom"}:
                return True
            if doctype == "BOM" and fieldname in {
                "base_total_cost",
                "total_cost",
                "raw_material_cost",
                "operating_cost",
                "quantity",
            }:
                return True
            return False

        def get_value(self, doctype, name, fieldname=None):
            if doctype == "Company" and fieldname == "default_currency":
                return "PKR"
            return None

    frappe_module.db = _Db()
    frappe_qb = types.SimpleNamespace(from_=lambda *args, **kwargs: None)
    frappe_module.qb = frappe_qb
    sys.modules["frappe"] = frappe_module

    frappe_qb_module = types.ModuleType("frappe.query_builder")
    frappe_qb_module.DocType = lambda name: name
    sys.modules["frappe.query_builder"] = frappe_qb_module

    frappe_qb_functions = types.ModuleType("frappe.query_builder.functions")
    frappe_qb_functions.Sum = lambda field: field
    sys.modules["frappe.query_builder.functions"] = frappe_qb_functions

    frappe_utils = types.ModuleType("frappe.utils")
    frappe_utils.cint = lambda value: int(value or 0)
    frappe_utils.flt = lambda value, *_args, **_kwargs: float(value or 0)
    frappe_utils.nowdate = lambda: "2026-04-17"
    sys.modules["frappe.utils"] = frappe_utils

    frappe_cache = types.ModuleType("frappe.utils.caching")
    frappe_cache.redis_cache = lambda ttl=None: (lambda fn: fn)
    sys.modules["frappe.utils.caching"] = frappe_cache

    erpnext_utils = types.ModuleType("erpnext.setup.utils")
    erpnext_utils.get_exchange_rate = lambda *args, **kwargs: 1
    sys.modules["erpnext.setup.utils"] = erpnext_utils


def _load_module():
    module_name = "test_item_fetchers_target"
    file_path = REPO_ROOT / "posawesome" / "posawesome" / "api" / "item_fetchers.py"
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class TestItemFetchers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_modules = {name: sys.modules.get(name) for name in STUBBED_MODULE_NAMES}
        _install_stubs()
        cls.module = _load_module()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("test_item_fetchers_target", None)
        for name, original in cls.original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original

    def test_get_bom_costs_prefers_item_default_bom(self):
        meta_rows = [
            AttrDict({"name": "ITEM-001", "default_bom": "BOM-DEFAULT"}),
            AttrDict({"name": "ITEM-002", "default_bom": None}),
        ]

        default_rows = [
            AttrDict(
                {
                    "name": "BOM-DEFAULT",
                    "item": "ITEM-001",
                    "is_active": 1,
                    "docstatus": 1,
                    "is_default": 0,
                    "quantity": 2,
                    "base_total_cost": 50,
                }
            )
        ]
        fallback_rows = [
            AttrDict(
                {
                    "name": "BOM-FALLBACK",
                    "item": "ITEM-002",
                    "is_active": 1,
                    "docstatus": 1,
                    "is_default": 1,
                    "quantity": 5,
                    "base_total_cost": 200,
                }
            )
        ]

        self.module.frappe.get_all = lambda doctype, filters=None, **kwargs: (
            default_rows if filters and filters.get("name") else fallback_rows
        )

        result = self.module.get_bom_costs(meta_rows)

        self.assertEqual(result["ITEM-001"]["bom"], "BOM-DEFAULT")
        self.assertEqual(result["ITEM-001"]["rate"], 25.0)
        self.assertEqual(result["ITEM-002"]["bom"], "BOM-FALLBACK")
        self.assertEqual(result["ITEM-002"]["rate"], 40.0)

    def test_merge_item_row_exposes_bom_cost_metadata(self):
        lookup = self.module.ItemLookupData(
            price_map={},
            buying_price_map={},
            stock_map={},
            meta_map={"ITEM-001": AttrDict({"name": "ITEM-001", "stock_uom": "Nos"})},
            uom_map={},
            barcode_map={},
            batch_map={},
            serial_map={},
            bom_map={"ITEM-001": {"rate": 33, "bom": "BOM-ITEM-001", "source": "bom"}},
        )

        row = self.module.merge_item_row(
            {"item_code": "ITEM-001"},
            lookup,
            "PKR",
            1,
        )

        self.assertEqual(row["manufacturing_cost"], 33)
        self.assertEqual(row["manufacturing_cost_source"], "bom")
        self.assertEqual(row["manufacturing_bom"], "BOM-ITEM-001")

    def test_merge_item_row_exposes_base_currency_buying_prices_by_uom(self):
        lookup = self.module.ItemLookupData(
            price_map={},
            buying_price_map={
                "ITEM-001": {
                    "Nos": AttrDict(
                        {
                            "price_list_rate": 2,
                            "currency": "USD",
                            "price_list": "Standard Buying",
                        }
                    ),
                    "Box": AttrDict(
                        {
                            "price_list_rate": 18,
                            "currency": "USD",
                            "price_list": "Standard Buying",
                        }
                    ),
                }
            },
            stock_map={},
            meta_map={"ITEM-001": AttrDict({"name": "ITEM-001", "stock_uom": "Nos"})},
            uom_map={"ITEM-001": [{"uom": "Box", "conversion_factor": 10}]},
            barcode_map={},
            batch_map={},
            serial_map={},
            bom_map={},
        )

        row = self.module.merge_item_row(
            {"item_code": "ITEM-001", "uom": "Box"},
            lookup,
            "PKR",
            1,
            buying_exchange_rate=280,
        )

        self.assertEqual(
            row["_buying_prices_by_uom"]["Nos"]["base_price_list_rate"],
            560,
        )
        self.assertEqual(
            row["_buying_prices_by_uom"]["Box"]["base_price_list_rate"],
            5040,
        )

    def test_fetch_barcodes_includes_standard_uom_field(self):
        calls = []

        def fake_get_all(doctype, **kwargs):
            calls.append((doctype, kwargs))
            return [AttrDict({"parent": "ITEM-001", "barcode": "BOX-001", "uom": "Box"})]

        self.module.frappe.get_all = fake_get_all

        rows = self.module._fetch_barcodes(("ITEM-001",))

        self.assertEqual(rows[0].uom, "Box")
        self.assertEqual(calls[0][0], "Item Barcode")
        self.assertIn("uom", calls[0][1]["fields"])
        self.assertNotIn("posa_uom", calls[0][1]["fields"])

    def test_cached_lookup_none_values_degrade_to_empty_collections(self):
        lookup_names = (
            "get_item_prices",
            "get_bin_qty",
            "get_item_meta",
            "get_uoms",
            "get_barcodes",
            "get_bom_costs",
            "get_batches",
            "get_serials",
        )
        originals = {name: getattr(self.module, name) for name in lookup_names}
        try:
            for name in lookup_names:
                setattr(self.module, name, lambda *args, **kwargs: None)

            aggregator = self.module.ItemDetailAggregator(
                {
                    "name": "POS Profile",
                    "company": "MedPlus Pharmacy",
                    "currency": "PKR",
                    "selling_price_list": "Standard Selling",
                    "warehouse": "Main Store - MP",
                    "posa_use_server_cache": 1,
                }
            )
            aggregator._resolve_buying_price_list = lambda: "Standard Buying"

            lookup = aggregator._prepare_lookup(["ITEM-001"])

            self.assertEqual(lookup.price_map, {})
            self.assertEqual(lookup.buying_price_map, {})
            self.assertEqual(lookup.stock_map, {})
            self.assertEqual(lookup.meta_map, {})
            self.assertEqual(lookup.bom_map, {})
        finally:
            for name, original in originals.items():
                setattr(self.module, name, original)

    def test_fetch_bin_qty_requests_valuation_rate_for_concrete_warehouse(self):
        calls = []

        def fake_get_all(doctype, **kwargs):
            calls.append((doctype, kwargs))
            return [AttrDict({"item_code": "QB-524", "actual_qty": 15, "valuation_rate": 4500})]

        self.module.frappe.get_all = fake_get_all

        rows = self.module._fetch_bin_qty("Shop Floor - JBS", ("QB-524",))

        self.assertEqual(rows[0].valuation_rate, 4500)
        self.assertEqual(calls[0][0], "Bin")
        self.assertIn("valuation_rate", calls[0][1]["fields"])

    def test_merge_item_row_prefers_warehouse_bin_valuation_over_zero_item_valuation(self):
        lookup = self.module.ItemLookupData(
            price_map={},
            buying_price_map={},
            stock_map={"QB-524": 15},
            meta_map={
                "QB-524": AttrDict(
                    {
                        "name": "QB-524",
                        "stock_uom": "Nos",
                        "valuation_rate": 0,
                        "last_purchase_rate": 0,
                    }
                )
            },
            uom_map={},
            barcode_map={},
            batch_map={},
            serial_map={},
            bom_map={},
            valuation_map={"QB-524": 4500.0},
        )

        row = self.module.merge_item_row(
            {"item_code": "QB-524"},
            lookup,
            "UGX",
            1,
        )

        self.assertEqual(row["valuation_rate"], 4500.0)
        self.assertEqual(row["warehouse_valuation_rate"], 4500.0)

    def test_merge_item_row_normalizes_ugx_buying_rate_for_usd_invoice(self):
        lookup = self.module.ItemLookupData(
            price_map={
                "ITEM-MC": {
                    "Nos": AttrDict(
                        {
                            "price_list_rate": 15.0,
                            "currency": "USD",
                            "price_list": "USD Selling",
                        }
                    )
                }
            },
            buying_price_map={
                "ITEM-MC": {
                    "Nos": AttrDict(
                        {
                            "price_list_rate": 37000.0,
                            "currency": "UGX",
                            "price_list": "Standard Buying",
                        }
                    )
                }
            },
            stock_map={},
            meta_map={"ITEM-MC": AttrDict({"name": "ITEM-MC", "stock_uom": "Nos"})},
            uom_map={},
            barcode_map={},
            batch_map={},
            serial_map={},
            bom_map={},
        )

        row = self.module.merge_item_row(
            {"item_code": "ITEM-MC"},
            lookup,
            "USD",
            exchange_rate=3700.0,
            buying_exchange_rate=1.0,
        )

        self.assertAlmostEqual(row["trade_price"], 10.0)
        self.assertAlmostEqual(row["buying_rate"], 10.0)
        self.assertAlmostEqual(row["base_buying_rate"], 37000.0)
        self.assertEqual(row["buying_rate_currency"], "USD")
        self.assertEqual(row["buying_price_currency"], "UGX")
        self.assertAlmostEqual(
            row["_buying_prices_by_uom"]["Nos"]["base_price_list_rate"],
            37000.0,
        )

    def test_merge_item_row_normalizes_usd_buying_rate_for_ugx_invoice(self):
        lookup = self.module.ItemLookupData(
            price_map={
                "ITEM-MC": {
                    "Nos": AttrDict(
                        {
                            "price_list_rate": 40000.0,
                            "currency": "UGX",
                            "price_list": "UGX Selling",
                        }
                    )
                }
            },
            buying_price_map={
                "ITEM-MC": {
                    "Nos": AttrDict(
                        {
                            "price_list_rate": 10.0,
                            "currency": "USD",
                            "price_list": "USD Buying",
                        }
                    )
                }
            },
            stock_map={},
            meta_map={"ITEM-MC": AttrDict({"name": "ITEM-MC", "stock_uom": "Nos"})},
            uom_map={},
            barcode_map={},
            batch_map={},
            serial_map={},
            bom_map={},
        )

        row = self.module.merge_item_row(
            {"item_code": "ITEM-MC"},
            lookup,
            "UGX",
            exchange_rate=1.0,
            buying_exchange_rate=3700.0,
        )

        self.assertAlmostEqual(row["trade_price"], 37000.0)
        self.assertAlmostEqual(row["buying_rate"], 37000.0)
        self.assertEqual(row["buying_rate_currency"], "UGX")
        self.assertEqual(row["buying_price_currency"], "USD")

    def test_merge_item_row_omits_unconvertible_cross_currency_buying_rate(self):
        lookup = self.module.ItemLookupData(
            price_map={
                "ITEM-MC": {
                    "Nos": AttrDict(
                        {
                            "price_list_rate": 15.0,
                            "currency": "USD",
                            "price_list": "USD Selling",
                        }
                    )
                }
            },
            buying_price_map={
                "ITEM-MC": {
                    "Nos": AttrDict(
                        {
                            "price_list_rate": 37000.0,
                            "currency": "UGX",
                            "price_list": "Standard Buying",
                        }
                    )
                }
            },
            stock_map={},
            meta_map={"ITEM-MC": AttrDict({"name": "ITEM-MC", "stock_uom": "Nos"})},
            uom_map={},
            barcode_map={},
            batch_map={},
            serial_map={},
            bom_map={},
        )

        row = self.module.merge_item_row(
            {"item_code": "ITEM-MC"},
            lookup,
            "USD",
            exchange_rate=3700.0,
            buying_exchange_rate=None,
        )

        self.assertIsNone(row["trade_price"])
        self.assertIsNone(row["buying_rate"])
        self.assertIsNone(row["buying_rate_currency"])
        self.assertEqual(row["buying_price_currency"], "UGX")


if __name__ == "__main__":
    unittest.main()
