import importlib.util
import pathlib
import sys
import types
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
_ORIGINAL_MODULES = dict(sys.modules)


def tearDownModule():
    managed_prefixes = (
        "frappe",
        "erpnext",
        "posawesome",
    )
    for name in list(sys.modules):
        if name.startswith(managed_prefixes) and name not in _ORIGINAL_MODULES:
            sys.modules.pop(name, None)
    for name, module in _ORIGINAL_MODULES.items():
        if name.startswith(managed_prefixes):
            sys.modules[name] = module


def _install_stubs():
    frappe_module = types.ModuleType("frappe")
    frappe_module._ = lambda text: text
    frappe_module.throw = lambda message, *args, **kwargs: (_ for _ in ()).throw(Exception(message))
    frappe_module.get_cached_value = lambda *args, **kwargs: None
    frappe_module.db = types.SimpleNamespace(
        get_single_value=lambda *args, **kwargs: None,
        get_value=lambda *args, **kwargs: None,
    )
    sys.modules["frappe"] = frappe_module

    frappe_utils = types.ModuleType("frappe.utils")
    frappe_utils.cint = lambda value: int(value or 0)
    frappe_utils.flt = lambda value, precision=None: (
        float(value or 0) if precision is None else round(float(value or 0), int(precision))
    )
    frappe_utils.cstr = lambda value: str(value or "")
    frappe_utils.getdate = lambda value=None: value
    frappe_utils.nowdate = lambda: "2026-11-28"
    sys.modules["frappe.utils"] = frappe_utils

    erpnext_batch = types.ModuleType("erpnext.stock.doctype.batch.batch")
    erpnext_batch.get_batch_qty = lambda *args, **kwargs: 0
    sys.modules["erpnext.stock.doctype.batch.batch"] = erpnext_batch

    items_module = types.ModuleType("posawesome.posawesome.api.items")
    items_module.get_bulk_stock_availability = lambda *args, **kwargs: {}
    items_module.get_stock_availability = lambda *args, **kwargs: 0
    sys.modules["posawesome.posawesome.api.items"] = items_module

    processing_utils = types.ModuleType("posawesome.posawesome.api.invoice_processing.utils")
    processing_utils._sanitize_item_name = lambda name: str(name or "").strip()
    sys.modules["posawesome.posawesome.api.invoice_processing.utils"] = processing_utils


def _load_module():
    module_name = "posawesome.posawesome.api.invoice_processing.stock"
    file_path = REPO_ROOT / "posawesome" / "posawesome" / "api" / "invoice_processing" / "stock.py"
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class TestStripClientFreebies(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_stubs()
        cls.stock = _load_module()

    def test_strip_client_freebies_drops_pricing_rule_free_rows_and_clears_unverified_free_flag(self):
        payload = {
            "is_return": 0,
            "items": [
                {
                    "item_code": "PAID-1",
                    "qty": 1,
                    "rate": 500,
                    "pricing_rules": '["PR-FREE"]',
                    "source_rule": "PR-FREE",
                    "pricing_rule": "PR-FREE",
                },
                {
                    "item_code": "AUTO-FREE",
                    "qty": 1,
                    "rate": 0,
                    "is_free_item": 1,
                    "auto_free_source": "PR-FREE::PAID-1::AUTO-FREE",
                },
                {
                    "item_code": "RULE-FREE",
                    "qty": 1,
                    "rate": 0,
                    "is_free_item": 1,
                    "pricing_rules": "PR-FREE",
                },
                {
                    "item_code": "FORGED-FREE",
                    "qty": 1,
                    "rate": 0,
                    "is_free_item": 1,
                },
                {
                    "item_code": "OFFER-FREE",
                    "qty": 1,
                    "rate": 0,
                    "is_free_item": 1,
                    "posa_is_offer": 1,
                },
            ],
        }

        self.stock._strip_client_freebies_from_payload(payload)

        codes = [row["item_code"] for row in payload["items"]]
        self.assertEqual(codes, ["PAID-1", "FORGED-FREE", "OFFER-FREE"])
        paid_row, forged_row, offer_row = payload["items"]
        self.assertEqual(forged_row.get("is_free_item"), 0)
        self.assertEqual(offer_row.get("is_free_item"), 0)
        self.assertFalse(paid_row.get("pricing_rules"))
        self.assertFalse(paid_row.get("source_rule"))
        self.assertFalse(paid_row.get("pricing_rule"))

    def test_strip_client_freebies_preserves_return_invoice_free_flags(self):
        payload = {
            "is_return": 1,
            "items": [
                {
                    "item_code": "RET-FREE",
                    "qty": -1,
                    "rate": 0,
                    "is_free_item": 1,
                    "locked_price": 1,
                }
            ],
        }

        self.stock._strip_client_freebies_from_payload(payload)

        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["is_free_item"], 1)

    def test_merge_duplicate_taxes_preserves_distinct_chained_row_id_rows_for_zero_net_floor_check(
        self,
    ):
        class InvoiceStub(dict):
            def set(self, key, value):
                self[key] = value

            def calculate_taxes_and_totals(self):
                return None

        taxes = [
            {
                "charge_type": "On Item Quantity",
                "account_head": "Excise 1 - CO",
                "rate": 10.0,
                "included_in_print_rate": 1,
            },
            {
                "charge_type": "On Item Quantity",
                "account_head": "Excise 2 - CO",
                "rate": 20.0,
                "included_in_print_rate": 1,
            },
            {
                "charge_type": "On Previous Row Amount",
                "account_head": "VAT - CO",
                "row_id": "1",
                "rate": 10.0,
                "included_in_print_rate": 1,
            },
            {
                "charge_type": "On Previous Row Amount",
                "account_head": "VAT - CO",
                "row_id": "2",
                "rate": 10.0,
                "included_in_print_rate": 1,
            },
        ]
        invoice_doc = InvoiceStub(
            company="Test Co",
            currency="USD",
            conversion_rate=1.0,
            taxes=list(taxes),
        )

        self.stock._merge_duplicate_taxes(invoice_doc, recalculate=False)

        self.assertEqual(len(invoice_doc["taxes"]), 4)
        self.assertEqual(
            [row.get("row_id") for row in invoice_doc["taxes"][2:]],
            ["1", "2"],
        )

        # Verify end-to-end that the preserved chained rows yield 0 net at gross 33
        # and are rejected against a 0.50 floor.
        frappe_mod = sys.modules["frappe"]
        frappe_mod.get_meta = lambda _dt: types.SimpleNamespace(has_field=lambda _f: False)
        frappe_mod.db.get_value = lambda doctype, *args, **kwargs: (
            "USD" if doctype == "Company" else None
        )
        frappe_mod.db.exists = lambda *args, **kwargs: False

        def fake_get_all(doctype, *args, **kwargs):
            if doctype == "Item":
                return [{"item_code": "LOW", "stock_uom": "Nos", "is_stock_item": 1}]
            if doctype == "Bin":
                return [
                    {
                        "item_code": "LOW",
                        "warehouse": "Shop Floor - JBS",
                        "valuation_rate": 0.50,
                    }
                ]
            return []

        frappe_mod.get_all = fake_get_all

        controls_path = REPO_ROOT / "posawesome" / "posawesome" / "api" / "item_sale_controls.py"
        spec = importlib.util.spec_from_file_location(
            "posawesome.posawesome.api.item_sale_controls", controls_path
        )
        controls = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controls)

        errors = controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "LOW",
                    "item_name": "Double Chained Tax Item",
                    "qty": 1,
                    "rate": 33.0,
                    "amount": 33.0,
                    "uom": "Nos",
                    "warehouse": "Shop Floor - JBS",
                }
            ],
            invoice_doc=invoice_doc,
        )
        self.assertEqual(len(errors), 1)
        self.assertAlmostEqual(errors[0]["selling_rate"], 0.0, places=2)

    def test_merge_duplicate_taxes_remaps_downstream_row_id_references_after_dropping_duplicates(
        self,
    ):
        class InvoiceStub(dict):
            def set(self, key, value):
                self[key] = value

            def calculate_taxes_and_totals(self):
                return None

        taxes = [
            {
                "idx": 1,
                "charge_type": "On Net Total",
                "account_head": "Tax A - CO",
                "rate": 10.0,
                "included_in_print_rate": 0,
            },
            {
                "idx": 2,
                "charge_type": "On Net Total",
                "account_head": "Tax A - CO",
                "rate": 10.0,
                "included_in_print_rate": 0,
            },
            {
                "idx": 3,
                "charge_type": "On Net Total",
                "account_head": "Tax B - CO",
                "rate": 20.0,
                "included_in_print_rate": 0,
            },
            {
                "idx": 4,
                "charge_type": "On Previous Row Amount",
                "account_head": "Tax C - CO",
                "row_id": "3",
                "rate": 10.0,
                "included_in_print_rate": 0,
            },
        ]
        invoice_doc = InvoiceStub(
            company="Test Co",
            currency="USD",
            conversion_rate=1.0,
            taxes=[dict(row) for row in taxes],
        )

        self.stock._merge_duplicate_taxes(invoice_doc, recalculate=False)

        merged = invoice_doc["taxes"]
        self.assertEqual(len(merged), 3)
        self.assertEqual([row.get("account_head") for row in merged], ["Tax A - CO", "Tax B - CO", "Tax C - CO"])
        self.assertEqual([row.get("idx") for row in merged], [1, 2, 3])
        self.assertEqual(merged[2].get("row_id"), "2")

        # Verify downstream tax calculation preserves C = 10% of B (20%) = 2.00 on 100 net
        # instead of dropping to 0.00 / self-referencing row 3.
        controls_path = REPO_ROOT / "posawesome" / "posawesome" / "api" / "item_sale_controls.py"
        spec = importlib.util.spec_from_file_location(
            "posawesome.posawesome.api.item_sale_controls", controls_path
        )
        controls = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controls)

        _incl_a, _incl_b, all_a, all_b = controls._row_tax_params(
            {"item_code": "ITEM-1"},
            invoice_doc,
        )
        self.assertAlmostEqual(all_a, 0.32, places=6)
        self.assertAlmostEqual(all_b, 0.0, places=6)

    def test_merge_duplicate_taxes_distinguishes_on_previous_row_total_vs_amount_when_dropping_duplicate(
        self,
    ):
        class InvoiceStub(dict):
            def set(self, key, value):
                self[key] = value

            def calculate_taxes_and_totals(self):
                return None

        taxes = [
            {
                "idx": 1,
                "charge_type": "On Net Total",
                "account_head": "Tax A - CO",
                "rate": 10.0,
                "included_in_print_rate": 0,
            },
            {
                "idx": 2,
                "charge_type": "On Net Total",
                "account_head": "Tax B - CO",
                "rate": 20.0,
                "included_in_print_rate": 0,
            },
            {
                "idx": 3,
                "charge_type": "On Net Total",
                "account_head": "Tax A - CO",
                "rate": 10.0,
                "included_in_print_rate": 0,
            },
            {
                "idx": 4,
                "charge_type": "On Previous Row Total",
                "account_head": "Tax C - CO",
                "row_id": "3",
                "rate": 10.0,
                "included_in_print_rate": 0,
            },
            {
                "idx": 5,
                "charge_type": "On Previous Row Amount",
                "account_head": "Tax D - CO",
                "row_id": "3",
                "rate": 10.0,
                "included_in_print_rate": 0,
            },
        ]
        invoice_doc = InvoiceStub(
            company="Test Co",
            currency="USD",
            conversion_rate=1.0,
            taxes=[dict(row) for row in taxes],
        )

        self.stock._merge_duplicate_taxes(invoice_doc, recalculate=False)

        merged = invoice_doc["taxes"]
        self.assertEqual(len(merged), 4)
        self.assertEqual(
            [row.get("account_head") for row in merged],
            ["Tax A - CO", "Tax B - CO", "Tax C - CO", "Tax D - CO"],
        )
        self.assertEqual([row.get("idx") for row in merged], [1, 2, 3, 4])
        # On Previous Row Total of dropped row 3 must point to last surviving row before 3 (row 2: B, total=130 -> 13.00).
        self.assertEqual(merged[2].get("row_id"), "2")
        # On Previous Row Amount of dropped row 3 must point to surviving duplicate of A (row 1: A, amount=10 -> 1.00).
        self.assertEqual(merged[3].get("row_id"), "1")

        controls_path = REPO_ROOT / "posawesome" / "posawesome" / "api" / "item_sale_controls.py"
        spec = importlib.util.spec_from_file_location(
            "posawesome.posawesome.api.item_sale_controls", controls_path
        )
        controls = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controls)

        total_only_doc = InvoiceStub(
            company="Test Co",
            currency="USD",
            conversion_rate=1.0,
            taxes=merged[:3],
        )
        _incl_a, _incl_b, total_only_a, _total_only_b = controls._row_tax_params(
            {"item_code": "ITEM-1"},
            total_only_doc,
        )
        # On 100 net: A=10 + B=20 + C=10% of 130 (13.00) = 43.00
        self.assertAlmostEqual(total_only_a, 0.43, places=6)

        _incl_a, _incl_b, full_a, _full_b = controls._row_tax_params(
            {"item_code": "ITEM-1"},
            invoice_doc,
        )
        # Adding D=10% of A (1.00) -> 44.00
        self.assertAlmostEqual(full_a, 0.44, places=6)


if __name__ == "__main__":
    unittest.main()
