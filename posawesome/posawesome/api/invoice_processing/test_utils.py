import datetime
import importlib.util
import pathlib
import sys
import types
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
_ORIGINAL_MODULES = dict(sys.modules)


class _AttrDict(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


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


def _apply_order_by(rows, order_by):
    ordered = list(rows)
    if not order_by:
        return ordered
    clauses = [part.strip() for part in str(order_by).split(",") if part.strip()]
    for clause in reversed(clauses):
        tokens = clause.split()
        field = tokens[0]
        desc = len(tokens) > 1 and tokens[1].lower() == "desc"
        ordered.sort(key=lambda row: row.get(field), reverse=desc)
    return ordered


def _matches_filters(row, filters):
    for field, expected in (filters or {}).items():
        actual = row.get(field)
        if isinstance(expected, list) and len(expected) == 2:
            op, target = expected
            if op == "<=":
                if not (str(actual) <= str(target)):
                    return False
            elif op == "between":
                start, end = target
                if not (str(start) <= str(actual) <= str(end)):
                    return False
            else:
                if actual != target:
                    return False
        elif actual != expected:
            return False
    return True


def _install_stubs():
    frappe_module = types.ModuleType("frappe")
    frappe_module._ = lambda text: text
    frappe_module.whitelist = lambda *args, **kwargs: (
        args[0] if args and callable(args[0]) else (lambda fn: fn)
    )
    frappe_module.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
    frappe_module.throw = lambda message, *args, **kwargs: (_ for _ in ()).throw(Exception(message))
    frappe_module.get_cached_doc = lambda *args, **kwargs: _AttrDict(allow_stale=1)
    frappe_module.get_all = lambda *args, **kwargs: []
    frappe_module.flags = types.SimpleNamespace(mute_messages=False)
    frappe_module.message_log = []
    frappe_module.db = types.SimpleNamespace(get_value=lambda *args, **kwargs: None)
    sys.modules["frappe"] = frappe_module

    frappe_utils = types.ModuleType("frappe.utils")
    frappe_utils.add_days = lambda date_str, days: (
        datetime.date.fromisoformat(str(date_str)) + datetime.timedelta(days=int(days))
    ).isoformat()
    frappe_utils.cint = lambda value: int(value or 0)
    frappe_utils.flt = lambda value, precision=None: (
        float(value or 0) if precision is None else round(float(value or 0), int(precision))
    )
    frappe_utils.formatdate = lambda value: str(value or "")
    frappe_utils.getdate = lambda value=None: str(value or "2026-11-27")
    frappe_utils.nowdate = lambda: "2026-11-27"
    frappe_utils.strip_html_tags = lambda value: str(value or "")
    sys.modules["frappe.utils"] = frappe_utils

    erpnext_pkg = types.ModuleType("erpnext")
    erpnext_setup = types.ModuleType("erpnext.setup")
    erpnext_utils = types.ModuleType("erpnext.setup.utils")
    erpnext_utils.get_exchange_rate = lambda *args, **kwargs: 0.0
    sys.modules["erpnext"] = erpnext_pkg
    sys.modules["erpnext.setup"] = erpnext_setup
    sys.modules["erpnext.setup.utils"] = erpnext_utils

    return frappe_module


def _load_module():
    module_name = "posawesome.posawesome.api.invoice_processing.utils"
    file_path = REPO_ROOT / "posawesome" / "posawesome" / "api" / "invoice_processing" / "utils.py"
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class TestInvoiceCurrencyRates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frappe = _install_stubs()
        cls.utils = _load_module()

    def _install_exchange_rows(self, rows):
        def fake_get_all(doctype, filters=None, fields=None, order_by=None, limit=None, **kwargs):
            if doctype != "Currency Exchange":
                return []
            matched = [row for row in rows if _matches_filters(row, filters)]
            ordered = _apply_order_by(matched, order_by)
            if limit is not None:
                ordered = ordered[: int(limit)]
            return ordered

        self.frappe.get_all = fake_get_all

    def test_get_latest_rate_prefers_same_day_purpose_specific_over_later_dual_purpose_rate(self):
        rows = [
            _AttrDict(
                name="CX-0001",
                date="2026-11-27",
                from_currency="USD",
                to_currency="UGX",
                for_selling=1,
                for_buying=0,
                exchange_rate=3700.0,
                creation="2026-11-27 08:00:00",
            ),
            _AttrDict(
                name="CX-0002",
                date="2026-11-27",
                from_currency="USD",
                to_currency="UGX",
                for_selling=1,
                for_buying=1,
                exchange_rate=4200.0,
                creation="2026-11-27 12:00:00",
            ),
            _AttrDict(
                name="CX-0003",
                date="2026-11-27",
                from_currency="EUR",
                to_currency="UGX",
                for_selling=0,
                for_buying=1,
                exchange_rate=3500.0,
                creation="2026-11-27 08:00:00",
            ),
            _AttrDict(
                name="CX-0004",
                date="2026-11-27",
                from_currency="EUR",
                to_currency="UGX",
                for_selling=1,
                for_buying=1,
                exchange_rate=4200.0,
                creation="2026-11-27 12:00:00",
            ),
        ]
        self._install_exchange_rows(rows)

        selling_rate, selling_date = self.utils.get_latest_rate(
            "USD",
            "UGX",
            transaction_date="2026-11-27",
            purpose="for_selling",
            allow_external=False,
        )
        self.assertEqual(selling_rate, 3700.0)
        self.assertEqual(selling_date, "2026-11-27")

        buying_rate, buying_date = self.utils.get_latest_rate(
            "EUR",
            "UGX",
            transaction_date="2026-11-27",
            purpose="for_buying",
            allow_external=False,
        )
        self.assertEqual(buying_rate, 3500.0)
        self.assertEqual(buying_date, "2026-11-27")

    def test_resolve_erpnext_currency_rates_preserves_posting_date_for_price_list_fx_lookup(self):
        rows = [
            _AttrDict(
                name="CX-USD-26",
                date="2026-11-26",
                from_currency="USD",
                to_currency="UGX",
                for_selling=1,
                for_buying=0,
                exchange_rate=3700.0,
                creation="2026-11-26 09:00:00",
            ),
            _AttrDict(
                name="CX-EUR-26",
                date="2026-11-26",
                from_currency="EUR",
                to_currency="UGX",
                for_selling=1,
                for_buying=0,
                exchange_rate=4000.0,
                creation="2026-11-26 09:00:00",
            ),
            _AttrDict(
                name="CX-EUR-27",
                date="2026-11-27",
                from_currency="EUR",
                to_currency="UGX",
                for_selling=1,
                for_buying=0,
                exchange_rate=4500.0,
                creation="2026-11-27 09:00:00",
            ),
        ]
        self._install_exchange_rows(rows)

        rates = self.utils.resolve_erpnext_currency_rates(
            invoice_currency="USD",
            price_list_currency="EUR",
            company_currency="UGX",
            posting_date="2026-11-27",
        )
        self.assertEqual(rates["conversion_rate"], 3700.0)
        self.assertEqual(rates["plc_conversion_rate"], 4500.0)
        self.assertEqual(rates["exchange_rate_date"], "2026-11-26")


if __name__ == "__main__":
    unittest.main()
