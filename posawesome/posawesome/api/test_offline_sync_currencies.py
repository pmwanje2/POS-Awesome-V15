import importlib.util
import pathlib
import sys
import types
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(
    0,
    str(REPO_ROOT / "posawesome" / "posawesome" / "api" / "test_support"),
)

from offline_sync_harness import (
    install_offline_sync_package_stubs,
    load_offline_sync_common,
)


class AttrDict(dict):
    __getattr__ = dict.get

    def as_dict(self):
        return dict(self)


def _install_stubs():
    install_offline_sync_package_stubs()

    frappe_module = types.ModuleType("frappe")
    frappe_module._ = lambda text: text
    frappe_module.throw = lambda message: (_ for _ in ()).throw(Exception(message))
    frappe_module.whitelist = lambda *args, **kwargs: (lambda fn: fn)

    def fake_get_all(doctype, **kwargs):
        if doctype == "Currency":
            return [
                {"name": "PKR", "enabled": 1, "modified": "2026-04-09T10:00:00"},
                {"name": "USD", "enabled": 1, "modified": "2026-04-09T10:02:00"},
                {"name": "GBP", "enabled": 1, "modified": "2026-04-09T10:03:00"},
                {"name": "EUR", "enabled": 0, "modified": "2026-04-09T10:03:00"},
            ]
        if doctype == "Price List":
            return [
                {"name": "Retail", "currency": "PKR"},
                {"name": "Export", "currency": "USD"},
            ]
        if doctype == "Currency Exchange":
            filters = kwargs.get("filters") or {}
            from_filter = filters.get("from_currency")
            to_filter = filters.get("to_currency")
            from_currencies = (
                from_filter[1]
                if isinstance(from_filter, (list, tuple)) and from_filter[0] == "in"
                else [from_filter]
            )
            to_currencies = (
                to_filter[1]
                if isinstance(to_filter, (list, tuple)) and to_filter[0] == "in"
                else [to_filter]
            )
            return [
                AttrDict(
                    {
                        "name": f"FX-{from_currency}-{to_currency}",
                        "from_currency": from_currency,
                        "to_currency": to_currency,
                        "exchange_rate": 279.5,
                        "date": "2026-04-09",
                        "modified": "2026-04-09T10:04:00",
                    }
                )
                for from_currency in from_currencies
                for to_currency in to_currencies
                if from_currency and to_currency and from_currency != to_currency
            ]
        return []

    frappe_module.get_all = fake_get_all
    frappe_module.db = AttrDict(
        {
            "get_value": lambda doctype, name, fieldname: (
                "PKR" if doctype == "Company" and fieldname == "default_currency" else None
            )
        }
    )
    frappe_module.get_cached_doc = lambda doctype, name: AttrDict(
        {
            "name": name,
            "company": "Test Co",
            "currency": "PKR",
            "selling_price_list": "Retail",
            "posa_allow_multi_currency": 1,
            "modified": "2026-04-09T10:01:00",
            "payments": [
                {"mode_of_payment": "Cash"},
                {"mode_of_payment": "Card"},
            ],
        }
    )
    sys.modules["frappe"] = frappe_module

    invoice_utils_module = types.ModuleType("posawesome.posawesome.api.invoice_processing.utils")
    invoice_utils_module.get_available_currencies = lambda: [
        {"name": "PKR"},
        {"name": "USD"},
    ]
    invoice_utils_module.get_latest_rate = lambda from_currency, to_currency, **kwargs: (
        279.5,
        "2026-04-09",
    )
    invoice_utils_module.get_price_list_currency = lambda price_list: "PKR"
    sys.modules["posawesome.posawesome.api.invoice_processing.utils"] = invoice_utils_module

    payment_utils_module = types.ModuleType("posawesome.posawesome.api.payment_processing.utils")
    payment_utils_module.get_mode_of_payment_accounts = lambda company, modes: {
        "Cash": {"account_currency": "PKR"},
        "Card": {"account_currency": "GBP"},
    }
    sys.modules["posawesome.posawesome.api.payment_processing.utils"] = payment_utils_module

    api_utils_module = types.ModuleType("posawesome.posawesome.api.utils")
    api_utils_module.get_active_pos_profile = lambda user=None: {
        "name": "POS-TEST",
        "company": "Test Co",
        "currency": "PKR",
        "selling_price_list": "Retail",
        "posa_allow_multi_currency": 1,
        "modified": "2026-04-09T10:01:00",
        "payments": [
            {"mode_of_payment": "Cash"},
            {"mode_of_payment": "Card"},
        ],
    }
    sys.modules["posawesome.posawesome.api.utils"] = api_utils_module
    sys.modules["posawesome.posawesome.api.offline_sync.common"] = load_offline_sync_common()


def _load_module():
    module_name = "test_offline_sync_currencies_target"
    file_path = REPO_ROOT / "posawesome" / "posawesome" / "api" / "offline_sync" / "currencies.py"
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class TestOfflineSyncCurrencies(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_stubs()
        cls.module = _load_module()

    def test_sync_currency_scope_returns_enabled_currencies_pairs_and_deletes(self):
        response = self.module.sync_currency_scope(
            pos_profile="POS-TEST",
            watermark="2026-04-09T09:59:00",
            currency_pairs='[{"from_currency":"USD","to_currency":"PKR"}]',
        )

        self.assertEqual(
            [item["key"] for item in response["changes"]],
            [
                "currency_options",
                "currency_rate::FX-USD-PKR",
                "exchange_rate::USD::PKR",
            ],
        )
        self.assertEqual(
            response["changes"][0]["data"],
            [{"name": "PKR"}, {"name": "USD"}, {"name": "GBP"}],
        )
        self.assertEqual(response["changes"][1]["data"]["date"], "2026-04-09")
        self.assertEqual(response["changes"][2]["data"]["exchange_rate"], 279.5)
        self.assertEqual(
            response["deleted"],
            [{"key": "currency::EUR"}],
        )
        self.assertEqual(response["next_watermark"], "2026-04-09T10:04:00")
        self.assertEqual(response["schema_version"], self.module.SYNC_SCHEMA_VERSION)
        self.assertIn("next_watermark", response)
        self.assertIn("has_more", response)

    def test_startup_sync_is_metadata_only_when_no_pairs_are_supplied(self):
        response = self.module.sync_currency_scope(
            pos_profile="POS-TEST",
            watermark=None,
            currency_pairs=None,
        )

        pair_keys = {
            row["key"]
            for row in response["changes"]
            if row["key"].startswith("exchange_rate::")
        }
        self.assertEqual(pair_keys, set())

    def test_paginates_startup_currency_metadata(self):
        first = self.module.sync_currency_scope(
            pos_profile="POS-TEST",
            watermark=None,
            currency_pairs=None,
            offset=0,
            limit=2,
        )
        second = self.module.sync_currency_scope(
            pos_profile="POS-TEST",
            watermark=None,
            currency_pairs=None,
            offset=2,
            limit=10,
        )

        self.assertFalse(first["has_more"])
        self.assertIsNone(first["next_offset"])
        self.assertEqual(first["next_watermark"], "2026-04-09T10:03:00")
        self.assertFalse(second["has_more"])
        self.assertEqual(second["next_watermark"], "2026-04-09T10:03:00")

    def test_sync_currency_scope_skips_unchanged_results_for_current_watermark(self):
        response = self.module.sync_currency_scope(
            pos_profile="POS-TEST",
            watermark="2026-04-09T10:04:00",
            currency_pairs='[{"from_currency":"USD","to_currency":"PKR"}]',
        )

        self.assertEqual(response["changes"], [])
        self.assertEqual(response["deleted"], [])
        self.assertEqual(response["next_watermark"], "2026-04-09T10:04:00")
        self.assertEqual(response["schema_version"], self.module.SYNC_SCHEMA_VERSION)
        self.assertIn("next_watermark", response)
        self.assertIn("has_more", response)

    def test_sync_currency_scope_requests_and_emits_purpose_flags_and_name(self):
        orig_get_all = self.module.frappe.get_all
        orig_get_latest_rate = self.module.get_latest_rate
        captured_fields = []
        latest_rate_calls = []

        def fake_get_all(doctype, **kwargs):
            if doctype == "Currency Exchange":
                captured_fields.append(list(kwargs.get("fields") or []))
                return [
                    AttrDict(
                        {
                            "name": "FX-USD-UGX-BUY",
                            "from_currency": "USD",
                            "to_currency": "UGX",
                            "exchange_rate": 4050.0,
                            "date": "2026-04-08",
                            "for_buying": 1,
                            "for_selling": 0,
                            "modified": "2026-04-08T10:00:00",
                        }
                    ),
                    AttrDict(
                        {
                            "name": "FX-USD-UGX-SELL",
                            "from_currency": "USD",
                            "to_currency": "UGX",
                            "exchange_rate": 3700.0,
                            "date": "2026-04-09",
                            "for_buying": 0,
                            "for_selling": 1,
                            "modified": "2026-04-09T10:00:00",
                        }
                    ),
                ]
            return orig_get_all(doctype, **kwargs)

        def fake_get_latest_rate(from_currency, to_currency, **kwargs):
            latest_rate_calls.append((from_currency, to_currency, kwargs))
            return 3700.0, "2026-04-09"

        self.module.frappe.get_all = fake_get_all
        self.module.get_latest_rate = fake_get_latest_rate
        try:
            response = self.module.sync_currency_scope(
                pos_profile="POS-TEST",
                watermark=None,
                currency_pairs='[{"from_currency":"USD","to_currency":"UGX"}]',
            )
        finally:
            self.module.frappe.get_all = orig_get_all
            self.module.get_latest_rate = orig_get_latest_rate

        self.assertEqual(len(captured_fields), 1)
        for required_field in ("name", "for_buying", "for_selling"):
            self.assertIn(required_field, captured_fields[0])
        rate_changes = {
            row["key"]: row["data"]
            for row in response["changes"]
            if row["key"].startswith("currency_rate::")
        }
        self.assertEqual(rate_changes["currency_rate::FX-USD-UGX-BUY"]["for_buying"], 1)
        self.assertEqual(rate_changes["currency_rate::FX-USD-UGX-BUY"]["for_selling"], 0)
        self.assertEqual(rate_changes["currency_rate::FX-USD-UGX-SELL"]["for_buying"], 0)
        self.assertEqual(rate_changes["currency_rate::FX-USD-UGX-SELL"]["for_selling"], 1)
        self.assertEqual(latest_rate_calls[0][2].get("purpose"), "for_selling")

    def test_sync_currency_scope_emits_accounts_settings_stale_rate_policy(self):
        orig_get_cached_doc = self.module.frappe.get_cached_doc

        def fake_get_cached_doc(doctype, name=None):
            if doctype == "Accounts Settings":
                return AttrDict({"allow_stale": 0, "stale_days": 30})
            return orig_get_cached_doc(doctype, name)

        self.module.frappe.get_cached_doc = fake_get_cached_doc
        try:
            response = self.module.sync_currency_scope(
                pos_profile="POS-TEST",
                watermark=None,
                currency_pairs=None,
            )
        finally:
            self.module.frappe.get_cached_doc = orig_get_cached_doc

        self.assertEqual(
            response.get("currency_settings"),
            {"allow_stale": 0, "stale_days": 30},
        )

    def test_sync_currency_scope_auto_discovers_foreign_till_and_buying_list_pairs_when_currency_pairs_empty(
        self,
    ):
        orig_get_cached_doc = self.module.frappe.get_cached_doc
        orig_get_value = self.module.frappe.db.get_value
        orig_get_all = self.module.frappe.get_all
        orig_get_pl_currency = self.module.get_price_list_currency

        def fake_get_cached_doc(doctype, name=None):
            if doctype == "POS Profile":
                return AttrDict(
                    {
                        "name": "POS-USD-UGX",
                        "company": "Uganda Co",
                        "currency": "USD",
                        "selling_price_list": "USD Selling",
                        "buying_price_list": "EUR Buying",
                        "posa_allow_multi_currency": 0,
                        "modified": "2026-04-09T10:01:00",
                        "payments": [{"mode_of_payment": "Cash"}],
                    }
                )
            return orig_get_cached_doc(doctype, name)

        def fake_get_value(doctype, name, fieldname=None):
            if doctype == "Company" and fieldname == "default_currency":
                return "UGX"
            if doctype == "Price List" and name == "EUR Buying" and fieldname == "currency":
                return "EUR"
            if doctype == "Price List" and name == "USD Selling" and fieldname == "currency":
                return "USD"
            return orig_get_value(doctype, name, fieldname)

        def fake_get_all(doctype, **kwargs):
            if doctype == "Currency Exchange":
                filters = kwargs.get("filters") or {}
                from_vals = filters.get("from_currency")[1]
                to_vals = filters.get("to_currency")[1]
                rows = [
                    AttrDict(
                        {
                            "name": "FX-USD-UGX-BUY",
                            "from_currency": "USD",
                            "to_currency": "UGX",
                            "exchange_rate": 4050.0,
                            "date": "2026-04-09",
                            "for_buying": 1,
                            "for_selling": 0,
                            "modified": "2026-04-09T10:05:00",
                        }
                    ),
                    AttrDict(
                        {
                            "name": "FX-UGX-EUR-SELL",
                            "from_currency": "UGX",
                            "to_currency": "EUR",
                            "exchange_rate": 1.0 / 4300.0,
                            "date": "2026-04-09",
                            "for_buying": 0,
                            "for_selling": 1,
                            "modified": "2026-04-09T10:05:00",
                        }
                    ),
                ]
                return [
                    r
                    for r in rows
                    if r["from_currency"] in from_vals and r["to_currency"] in to_vals
                ]
            if doctype == "Item Price":
                return [{"currency": "EUR"}]
            return orig_get_all(doctype, **kwargs)

        def fake_get_pl_currency(price_list):
            if price_list == "USD Selling":
                return "USD"
            if price_list == "EUR Buying":
                return "EUR"
            return orig_get_pl_currency(price_list)

        self.module.frappe.get_cached_doc = fake_get_cached_doc
        self.module.frappe.db.get_value = fake_get_value
        self.module.frappe.get_all = fake_get_all
        self.module.get_price_list_currency = fake_get_pl_currency
        try:
            response = self.module.sync_currency_scope(
                pos_profile="POS-USD-UGX",
                watermark=None,
                currency_pairs=[],
            )
        finally:
            self.module.frappe.get_cached_doc = orig_get_cached_doc
            self.module.frappe.db.get_value = orig_get_value
            self.module.frappe.get_all = orig_get_all
            self.module.get_price_list_currency = orig_get_pl_currency

        keys = {row["key"] for row in response["changes"]}
        self.assertIn("currency_rate::FX-USD-UGX-BUY", keys)
        self.assertIn("currency_rate::FX-UGX-EUR-SELL", keys)

    def test_sync_currency_scope_backfills_newly_expanded_pairs_older_than_watermark(self):
        orig_get_cached_doc = self.module.frappe.get_cached_doc
        orig_get_value = self.module.frappe.db.get_value
        orig_get_all = self.module.frappe.get_all
        orig_get_pl_currency = self.module.get_price_list_currency

        def fake_get_cached_doc(doctype, name=None):
            if doctype == "POS Profile":
                return AttrDict(
                    {
                        "name": "POS-EXPAND",
                        "company": "Uganda Co",
                        "currency": "USD",
                        "selling_price_list": "USD Selling",
                        "buying_price_list": "EUR Buying",
                        "posa_allow_multi_currency": 0,
                        "modified": "2026-04-09T09:00:00",
                        "payments": [{"mode_of_payment": "Cash"}],
                    }
                )
            return orig_get_cached_doc(doctype, name)

        def fake_get_value(doctype, name, fieldname=None):
            if doctype == "Company" and fieldname == "default_currency":
                return "UGX"
            if doctype == "Price List" and name == "EUR Buying" and fieldname == "currency":
                return "EUR"
            if doctype == "Price List" and name == "USD Selling" and fieldname == "currency":
                return "USD"
            return orig_get_value(doctype, name, fieldname)

        def fake_get_all(doctype, **kwargs):
            if doctype == "Currency Exchange":
                filters = kwargs.get("filters") or {}
                from_vals = filters.get("from_currency")[1]
                to_vals = filters.get("to_currency")[1]
                rows = [
                    AttrDict(
                        {
                            "name": "FX-USD-UGX-OLD",
                            "from_currency": "USD",
                            "to_currency": "UGX",
                            "exchange_rate": 3700.0,
                            "date": "2026-04-08",
                            "for_buying": 0,
                            "for_selling": 1,
                            "modified": "2026-04-09T09:30:00",
                        }
                    ),
                    AttrDict(
                        {
                            "name": "FX-EUR-UGX-PRE-WM",
                            "from_currency": "EUR",
                            "to_currency": "UGX",
                            "exchange_rate": 4300.0,
                            "date": "2026-04-08",
                            "for_buying": 1,
                            "for_selling": 0,
                            "modified": "2026-04-09T09:00:00",
                        }
                    ),
                ]
                return [
                    r
                    for r in rows
                    if r["from_currency"] in from_vals and r["to_currency"] in to_vals
                ]
            if doctype == "Item Price":
                return [{"currency": "EUR"}]
            return orig_get_all(doctype, **kwargs)

        self.module.frappe.get_cached_doc = fake_get_cached_doc
        self.module.frappe.db.get_value = fake_get_value
        self.module.frappe.get_all = fake_get_all
        self.module.get_price_list_currency = (
            lambda pl: "EUR" if pl == "EUR Buying" else "USD" if pl == "USD Selling" else orig_get_pl_currency(pl)
        )
        try:
            # Delta sync after watermark=10:10:00 expanding with EUR<->UGX in backfill_pairs:
            # existing EUR row (09:00:00 <= watermark) MUST be returned while unchanged USD row stays skipped.
            response = self.module.sync_currency_scope(
                pos_profile="POS-EXPAND",
                watermark="2026-04-09T10:10:00",
                currency_pairs=[
                    {"from_currency": "USD", "to_currency": "UGX"},
                    {"from_currency": "UGX", "to_currency": "USD"},
                    {"from_currency": "EUR", "to_currency": "UGX"},
                    {"from_currency": "UGX", "to_currency": "EUR"},
                ],
                backfill_pairs=[
                    {"from_currency": "EUR", "to_currency": "UGX"},
                    {"from_currency": "UGX", "to_currency": "EUR"},
                ],
            )
            # First sync with partial client list (only USD<->UGX) also includes server-discovered EUR floor pair.
            first_sync = self.module.sync_currency_scope(
                pos_profile="POS-EXPAND",
                watermark=None,
                currency_pairs=[
                    {"from_currency": "USD", "to_currency": "UGX"},
                    {"from_currency": "UGX", "to_currency": "USD"},
                ],
            )
        finally:
            self.module.frappe.get_cached_doc = orig_get_cached_doc
            self.module.frappe.db.get_value = orig_get_value
            self.module.frappe.get_all = orig_get_all
            self.module.get_price_list_currency = orig_get_pl_currency

        keys = {row["key"] for row in response["changes"]}
        self.assertIn("currency_rate::FX-EUR-UGX-PRE-WM", keys)
        self.assertNotIn("currency_rate::FX-USD-UGX-OLD", keys)
        self.assertEqual(response["next_watermark"], "2026-04-09T10:10:00")
        self.assertIn({"from_currency": "EUR", "to_currency": "UGX"}, response.get("synced_pairs") or [])

        first_keys = {row["key"] for row in first_sync["changes"]}
        self.assertIn("currency_rate::FX-EUR-UGX-PRE-WM", first_keys)


if __name__ == "__main__":
    unittest.main()
