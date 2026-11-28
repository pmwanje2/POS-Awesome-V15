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


if __name__ == "__main__":
    unittest.main()
