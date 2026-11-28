import importlib.util
import pathlib
import sys
import types
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_ORIGINAL_MODULES = dict(sys.modules)


def tearDownModule():
    managed_prefixes = ("frappe", "erpnext.setup.utils", "posawesome")
    for name in list(sys.modules):
        if name.startswith(managed_prefixes) and name not in _ORIGINAL_MODULES:
            sys.modules.pop(name, None)
    for name, module in _ORIGINAL_MODULES.items():
        if name.startswith(managed_prefixes):
            sys.modules[name] = module


class FakeGiftCard:
    def __init__(self, code="GC-0001", balance=0, status="Active"):
        self.doctype = "POS Gift Card"
        self.name = code
        self.gift_card_code = code
        self.company = "Test Company"
        self.currency = "PKR"
        self.current_balance = balance
        self.status = status
        self.transactions = []
        self.flags = types.SimpleNamespace(ignore_permissions=False)
        self.saved = False

    def append(self, fieldname, value):
        target = getattr(self, fieldname)
        row = dict(value)
        target.append(row)
        return row

    def save(self, ignore_permissions=False):
        self.saved = True
        return self


class FakeInvoice:
    def __init__(
        self,
        doctype="Sales Invoice",
        name="ACC-SINV-0001",
        company="Test Company",
        posting_date="2026-04-05",
        pos_profile="Main POS",
        debit_to="1310 - Debtors - TC",
        customer="CUST-0001",
        grand_total=800,
    ):
        self.doctype = doctype
        self.name = name
        self.company = company
        self.posting_date = posting_date
        self.pos_profile = pos_profile
        self.debit_to = debit_to
        self.customer = customer
        self.grand_total = grand_total
        self.rounded_total = grand_total
        self.gift_card_redemptions = []
        self.payments = []
        self.flags = types.SimpleNamespace(ignore_permissions=False)

    def append(self, fieldname, value):
        row = dict(value)
        getattr(self, fieldname).append(row)
        return row

    def save(self, ignore_permissions=False):
        return self


class FakeJournalEntry:
    def __init__(self, payload=None):
        payload = payload or {}
        self.doctype = payload.get("doctype", "Journal Entry")
        self.voucher_type = payload.get("voucher_type", "Journal Entry")
        self.posting_date = payload.get("posting_date")
        self.company = payload.get("company")
        self.user_remark = payload.get("user_remark")
        self.accounts = []
        self.flags = types.SimpleNamespace(ignore_permissions=False)
        self.saved = False
        self.submitted = False

    def append(self, fieldname, value):
        target = getattr(self, fieldname)
        row = dict(value)
        target.append(row)
        return row

    def set_missing_values(self):
        return None

    def save(self, ignore_permissions=False):
        self.saved = True
        return self

    def submit(self):
        self.submitted = True
        return self


class FakeModeOfPayment:
    def __init__(self, name="Gift Card", mode_type="General"):
        self.doctype = "Mode of Payment"
        self.name = name
        self.mode_of_payment = name
        self.type = mode_type
        self.accounts = []
        self.flags = types.SimpleNamespace(ignore_permissions=False)
        self.saved = False

    def append(self, fieldname, value):
        target = getattr(self, fieldname)
        row = dict(value)
        target.append(row)
        return row

    def save(self, ignore_permissions=False):
        self.saved = True
        return self


def _install_stubs():
    frappe_module = types.ModuleType("frappe")
    frappe_utils_module = types.ModuleType("frappe.utils")
    employees_module = types.ModuleType("posawesome.posawesome.api.employees")
    pos_access_module = types.ModuleType("posawesome.posawesome.api.pos_access")
    terminal_state_module = types.ModuleType("posawesome.posawesome.api.terminal_state")
    erpnext_setup_utils_module = types.ModuleType("erpnext.setup.utils")
    erpnext_setup_utils_module.get_exchange_rate = lambda *_args, **_kwargs: 1
    utilities_module = types.ModuleType("posawesome.posawesome.api.utilities")
    utilities_module.resolve_erpnext_currency_rates = lambda *_args, **_kwargs: {
        "conversion_rate": 1.0,
        "plc_conversion_rate": 1.0,
        "exchange_rate_date": None,
    }

    state = {
        "cards": {},
        "new_docs": [],
        "journal_entries": [],
        "mode_of_payments": {},
        "terminal_users": {"Main POS": ["supervisor@example.com", "cashier@example.com"]},
        "user_docs": {
            "supervisor@example.com": types.SimpleNamespace(
                name="supervisor@example.com",
                full_name="Supervisor",
                enabled=1,
                posa_is_pos_supervisor=1,
            ),
            "cashier@example.com": types.SimpleNamespace(
                name="cashier@example.com",
                full_name="Cashier",
                enabled=1,
                posa_is_pos_supervisor=0,
            ),
        },
        "invoices": {"ACC-SINV-0001": FakeInvoice()},
        "active_cashier": "supervisor@example.com",
        "pos_profiles": {
            "Main POS": types.SimpleNamespace(
                name="Main POS",
                company="Test Company",
                cost_center="Main - TC",
                posa_use_gift_cards=1,
                posa_default_source_account="1110 - Cash - TC",
                posa_gift_card_liability_account="2190 - Gift Card Liability - TC",
            )
        },
        "companies": {
            "Test Company": types.SimpleNamespace(
                name="Test Company",
                default_cash_account="1110 - Cash - TC",
                cost_center="Main - TC",
            )
        },
    }

    frappe_module._ = lambda text: text
    frappe_module.whitelist = lambda *args, **kwargs: (lambda fn: fn)
    frappe_module.throw = lambda message: (_ for _ in ()).throw(Exception(message))
    frappe_module.generate_hash = lambda: "GCODE12345"
    frappe_module.utils = types.SimpleNamespace(now_datetime=lambda: "2026-04-05 12:00:00")
    frappe_utils_module.nowdate = lambda: "2026-04-05"
    frappe_module.session = types.SimpleNamespace(user="administrator@example.com")

    def _new_doc(doctype):
        if doctype == "POS Gift Card":
            doc = FakeGiftCard(code=f"GC-{len(state['new_docs']) + 1:04d}")
            state["new_docs"].append(doc)
            return doc
        if doctype == "Mode of Payment":
            doc = FakeModeOfPayment()
            state["mode_of_payments"][doc.name] = doc
            state["new_docs"].append(doc)
            return doc
        raise AssertionError(f"Unexpected doctype: {doctype}")

    def _get_doc(doctype, name=None):
        if isinstance(doctype, dict):
            if doctype.get("doctype") == "Journal Entry":
                entry = FakeJournalEntry(doctype)
                state["journal_entries"].append(entry)
                return entry
            raise AssertionError(f"Unexpected dict get_doc doctype: {doctype.get('doctype')}")
        if doctype == "POS Gift Card":
            if name not in state["cards"]:
                raise AssertionError(f"Unknown gift card: {name}")
            return state["cards"][name]
        if doctype == "Mode of Payment":
            if name not in state["mode_of_payments"]:
                raise AssertionError(f"Unknown mode of payment: {name}")
            return state["mode_of_payments"][name]
        if doctype == "User":
            if name not in state["user_docs"]:
                raise AssertionError(f"Unknown user: {name}")
            return state["user_docs"][name]
        if doctype == "Sales Invoice":
            if name not in state["invoices"]:
                raise AssertionError(f"Unknown invoice: {name}")
            return state["invoices"][name]
        raise AssertionError(f"Unexpected get_doc doctype: {doctype}")

    frappe_module.new_doc = _new_doc
    frappe_module.get_doc = _get_doc
    frappe_module.get_cached_doc = lambda doctype, name: (
        state["pos_profiles"][name] if doctype == "POS Profile" else state["companies"][name]
    )
    frappe_module.get_cached_value = lambda *args, **kwargs: None
    frappe_module.get_value = lambda doctype, name, fieldname: (
        getattr(state["pos_profiles"][name], fieldname, None)
        if doctype == "POS Profile"
        else getattr(state["companies"][name], fieldname, None)
    )
    frappe_module.flags = types.SimpleNamespace(ignore_account_permission=False)
    frappe_module.db = types.SimpleNamespace(
        exists=lambda doctype, name=None: bool(
            (
                doctype == "POS Gift Card"
                and (
                    (isinstance(name, dict) and name.get("gift_card_code") in state["cards"])
                    or (isinstance(name, str) and name in state["cards"])
                )
            )
            or (doctype == "Mode of Payment" and isinstance(name, str) and name in state["mode_of_payments"])
        )
    )

    employees_module._resolve_profile_name = lambda pos_profile=None: str(pos_profile or "").strip()
    employees_module._ensure_terminal_user = lambda profile_name, user: state["terminal_users"].get(
        profile_name, []
    )
    employees_module._get_user_doc = lambda user: state["user_docs"][user]
    employees_module._is_pos_supervisor = lambda user_doc: bool(
        getattr(user_doc, "posa_is_pos_supervisor", 0)
    )
    pos_access_module.get_authorized_pos_profile = (
        lambda pos_profile=None: state["pos_profiles"][str(pos_profile or "").strip()]
    )
    pos_access_module.user_can_manage_pos = (
        lambda user: bool(getattr(state["user_docs"].get(user), "posa_is_pos_supervisor", 0))
    )
    terminal_state_module.get_active_terminal_cashier = (
        lambda pos_profile=None: state["active_cashier"]
    )
    utilities_module.ensure_child_doctype = lambda *args, **kwargs: None

    sys.modules["frappe"] = frappe_module
    sys.modules["frappe.utils"] = frappe_utils_module
    sys.modules["posawesome.posawesome.api.employees"] = employees_module
    sys.modules["posawesome.posawesome.api.pos_access"] = pos_access_module
    sys.modules["posawesome.posawesome.api.terminal_state"] = terminal_state_module
    sys.modules["erpnext.setup.utils"] = erpnext_setup_utils_module
    sys.modules["posawesome.posawesome.api.utilities"] = utilities_module
    return state


def _install_package_stubs():
    package_paths = {
        "posawesome": REPO_ROOT / "posawesome",
        "posawesome.posawesome": REPO_ROOT / "posawesome" / "posawesome",
        "posawesome.posawesome.api": REPO_ROOT / "posawesome" / "posawesome" / "api",
    }
    for name, path in package_paths.items():
        module = types.ModuleType(name)
        module.__path__ = [str(path)]
        sys.modules[name] = module


def _load_module():
    module_name = "posawesome.posawesome.api.gift_cards"
    file_path = REPO_ROOT / "posawesome" / "posawesome" / "api" / "gift_cards.py"
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class TestGiftCardApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = _install_stubs()
        _install_package_stubs()
        cls.module = _load_module()

    def setUp(self):
        self.state["cards"].clear()
        self.state["new_docs"].clear()
        self.state["journal_entries"].clear()
        self.state["mode_of_payments"].clear()
        self.state["active_cashier"] = "supervisor@example.com"
        profile_doc = self.state["pos_profiles"]["Main POS"]
        profile_doc.posa_use_gift_cards = 1
        profile_doc.posa_default_source_account = "1110 - Cash - TC"
        profile_doc.posa_gift_card_liability_account = "2190 - Gift Card Liability - TC"
        for invoice_doc in self.state["invoices"].values():
            invoice_doc.gift_card_redemptions = []
            invoice_doc.payments = []

    def test_issue_gift_card_requires_supervisor(self):
        self.state["active_cashier"] = "cashier@example.com"
        with self.assertRaises(Exception) as ctx:
            self.module.issue_gift_card(
                pos_profile="Main POS",
                cashier="cashier@example.com",
                company="Test Company",
                initial_amount=500,
                gift_card_code="GC-NEW-01",
            )

        self.assertIn("POS supervisor", str(ctx.exception))

    def test_client_cashier_is_ignored_in_favor_of_server_terminal_state(self):
        result = self.module.issue_gift_card(
            pos_profile="Main POS",
            cashier="cashier@example.com",
            company="Test Company",
            initial_amount=0,
            gift_card_code="GC-SERVER-CASHIER",
        )

        self.assertEqual(result["gift_card_code"], "GC-SERVER-CASHIER")
        self.assertEqual(
            self.state["new_docs"][-1].issued_by,
            "supervisor@example.com",
        )

    def test_issue_gift_card_uses_profile_company_and_rejects_forged_company(self):
        with self.assertRaisesRegex(Exception, "does not match the authorized POS Profile"):
            self.module.issue_gift_card(
                pos_profile="Main POS",
                cashier="supervisor@example.com",
                company="Other Company",
                initial_amount=0,
                gift_card_code="GC-FORGED-COMPANY",
            )

        result = self.module.issue_gift_card(
            pos_profile="Main POS",
            cashier="supervisor@example.com",
            company=None,
            initial_amount=0,
            gift_card_code="GC-CANONICAL-COMPANY",
        )
        self.assertEqual(result["company"], "Test Company")

    def test_top_up_updates_balance_and_transaction_history(self):
        existing = FakeGiftCard(code="GC-0001", balance=500, status="Active")
        self.state["cards"][existing.gift_card_code] = existing

        result = self.module.top_up_gift_card(
            pos_profile="Main POS",
            cashier="supervisor@example.com",
            gift_card_code="GC-0001",
            amount=250,
        )

        self.assertEqual(result["gift_card_code"], "GC-0001")
        self.assertEqual(result["current_balance"], 750)
        self.assertEqual(len(existing.transactions), 1)
        self.assertEqual(existing.transactions[0]["transaction_type"], "Top Up")
        self.assertEqual(existing.transactions[0]["amount"], 250)
        self.assertEqual(existing.transactions[0]["balance_after"], 750)
        self.assertEqual(len(self.state["journal_entries"]), 1)
        self.assertTrue(self.state["journal_entries"][0].submitted)
        self.assertEqual(
            self.state["journal_entries"][0].accounts[0]["account"],
            "1110 - Cash - TC",
        )
        self.assertEqual(
            self.state["journal_entries"][0].accounts[1]["account"],
            "2190 - Gift Card Liability - TC",
        )

    def test_top_up_rejects_card_from_another_company(self):
        existing = FakeGiftCard(code="GC-OTHER-COMPANY", balance=500, status="Active")
        existing.company = "Other Company"
        self.state["cards"][existing.gift_card_code] = existing

        with self.assertRaisesRegex(Exception, "does not belong to company Test Company"):
            self.module.top_up_gift_card(
                pos_profile="Main POS",
                cashier="supervisor@example.com",
                gift_card_code="GC-OTHER-COMPANY",
                amount=250,
            )

        self.assertEqual(existing.current_balance, 500)
        self.assertEqual(self.state["journal_entries"], [])

    def test_apply_invoice_gift_card_redemptions_records_invoice_rows(self):
        existing = FakeGiftCard(code="GC-0002", balance=800, status="Active")
        self.state["cards"][existing.gift_card_code] = existing
        invoice_doc = self.state["invoices"]["ACC-SINV-0001"]

        result = self.module.apply_invoice_gift_card_redemptions(
            invoice_doc,
            [{"gift_card_code": "GC-0002", "amount": 300, "cashier": "cashier@example.com"}],
        )

        self.assertEqual(result, 300)
        self.assertEqual(existing.current_balance, 500)
        self.assertEqual(len(existing.transactions), 0)
        self.assertEqual(len(self.state["journal_entries"]), 0)
        self.assertEqual(len(invoice_doc.gift_card_redemptions), 1)
        self.assertEqual(invoice_doc.gift_card_redemptions[0]["gift_card_code"], "GC-0002")
        self.assertEqual(invoice_doc.gift_card_redemptions[0]["redeemed_amount"], 300)
        self.assertEqual(invoice_doc.gift_card_redemptions[0]["balance_before"], 800)
        self.assertEqual(invoice_doc.gift_card_redemptions[0]["balance_after"], 500)
        self.assertEqual(invoice_doc.gift_card_redemptions[0]["status"], "Applied")
        self.assertEqual(len(invoice_doc.payments), 1)
        self.assertEqual(invoice_doc.payments[0]["account"], "2190 - Gift Card Liability - TC")
        self.assertEqual(invoice_doc.payments[0]["amount"], 300)

    def test_apply_invoice_gift_card_redemptions_skips_validation_when_gift_cards_disabled_and_no_rows(self):
        profile_doc = self.state["pos_profiles"]["Main POS"]
        profile_doc.posa_use_gift_cards = 0
        profile_doc.posa_gift_card_liability_account = ""
        invoice_doc = self.state["invoices"]["ACC-SINV-0001"]

        result = self.module.apply_invoice_gift_card_redemptions(invoice_doc, [])

        self.assertEqual(result, 0)
        self.assertEqual(invoice_doc.gift_card_redemptions, [])
        self.assertEqual(invoice_doc.payments, [])

    def test_apply_invoice_gift_card_redemptions_skips_validation_when_enabled_but_no_rows(self):
        profile_doc = self.state["pos_profiles"]["Main POS"]
        profile_doc.posa_use_gift_cards = 1
        profile_doc.posa_gift_card_liability_account = ""
        invoice_doc = self.state["invoices"]["ACC-SINV-0001"]

        result = self.module.apply_invoice_gift_card_redemptions(
            invoice_doc,
            [{"gift_card_code": "GC-EMPTY", "amount": 0}],
        )

        self.assertEqual(result, 0)
        self.assertEqual(invoice_doc.gift_card_redemptions, [])
        self.assertEqual(invoice_doc.payments, [])

    def test_apply_invoice_gift_card_redemptions_requires_liability_account_when_redemption_is_attempted(
        self,
    ):
        existing = FakeGiftCard(code="GC-0006", balance=300, status="Active")
        self.state["cards"][existing.gift_card_code] = existing
        profile_doc = self.state["pos_profiles"]["Main POS"]
        profile_doc.posa_use_gift_cards = 1
        profile_doc.posa_gift_card_liability_account = ""
        invoice_doc = self.state["invoices"]["ACC-SINV-0001"]

        with self.assertRaises(Exception) as ctx:
            self.module.apply_invoice_gift_card_redemptions(
                invoice_doc,
                [{"gift_card_code": "GC-0006", "amount": 100, "cashier": "cashier@example.com"}],
            )

        self.assertIn("gift card liability account", str(ctx.exception))

    def test_restore_invoice_gift_card_redemptions_restores_balance(self):
        existing = FakeGiftCard(code="GC-0002", balance=500, status="Active")
        self.state["cards"][existing.gift_card_code] = existing
        invoice_doc = self.state["invoices"]["ACC-SINV-0001"]
        invoice_doc.gift_card_redemptions = [
            {
                "gift_card_code": "GC-0002",
                "redeemed_amount": 300,
                "balance_before": 800,
                "balance_after": 500,
                "status": "Applied",
            }
        ]

        restored = self.module.restore_invoice_gift_card_redemptions(invoice_doc)

        self.assertEqual(restored, 300)
        self.assertEqual(existing.current_balance, 800)
        self.assertEqual(invoice_doc.gift_card_redemptions[0]["status"], "Cancelled")

    def test_apply_invoice_gift_card_redemptions_blocks_inactive_card(self):
        existing = FakeGiftCard(code="GC-0003", balance=800, status="Inactive")
        self.state["cards"][existing.gift_card_code] = existing

        with self.assertRaises(Exception) as ctx:
            self.module.apply_invoice_gift_card_redemptions(
                self.state["invoices"]["ACC-SINV-0001"],
                [{"gift_card_code": "GC-0003", "amount": 100, "cashier": "cashier@example.com"}],
            )

        self.assertIn("active gift cards", str(ctx.exception))

    def test_apply_invoice_gift_card_redemptions_creates_mode_of_payment_account_mapping(self):
        existing = FakeGiftCard(code="GC-0004", balance=500, status="Active")
        self.state["cards"][existing.gift_card_code] = existing
        invoice_doc = self.state["invoices"]["ACC-SINV-0001"]

        self.module.apply_invoice_gift_card_redemptions(
            invoice_doc,
            [{"gift_card_code": "GC-0004", "amount": 200, "cashier": "cashier@example.com"}],
        )

        self.assertIn("Gift Card", self.state["mode_of_payments"])
        gift_mode = self.state["mode_of_payments"]["Gift Card"]
        self.assertEqual(gift_mode.type, "Cash")
        self.assertEqual(len(gift_mode.accounts), 1)
        self.assertEqual(gift_mode.accounts[0]["company"], "Test Company")
        self.assertEqual(gift_mode.accounts[0]["default_account"], "2190 - Gift Card Liability - TC")

    def test_apply_invoice_gift_card_redemptions_is_idempotent_for_saved_drafts(self):
        existing = FakeGiftCard(code="GC-0005", balance=500, status="Active")
        self.state["cards"][existing.gift_card_code] = existing
        invoice_doc = self.state["invoices"]["ACC-SINV-0001"]
        rows = [{"gift_card_code": "GC-0005", "amount": 300, "cashier": "cashier@example.com"}]

        first_result = self.module.apply_invoice_gift_card_redemptions(invoice_doc, rows)
        second_result = self.module.apply_invoice_gift_card_redemptions(invoice_doc, rows)

        self.assertEqual(first_result, 300)
        self.assertEqual(second_result, 300)
        self.assertEqual(existing.current_balance, 200)
        self.assertEqual(len(invoice_doc.gift_card_redemptions), 1)
        self.assertEqual(invoice_doc.gift_card_redemptions[0]["gift_card_code"], "GC-0005")
        self.assertEqual(invoice_doc.gift_card_redemptions[0]["balance_before"], 500)
        self.assertEqual(invoice_doc.gift_card_redemptions[0]["balance_after"], 200)
        self.assertEqual(len(invoice_doc.payments), 1)
        self.assertEqual(invoice_doc.payments[0]["amount"], 300)

    def test_issue_gift_card_with_initial_amount_creates_liability_entry(self):
        result = self.module.issue_gift_card(
            pos_profile="Main POS",
            cashier="supervisor@example.com",
            company="Test Company",
            initial_amount=500,
            gift_card_code="GC-ISSUE-01",
        )

        self.assertEqual(result["current_balance"], 500)
        self.assertEqual(len(self.state["journal_entries"]), 1)
        self.assertEqual(
            self.state["journal_entries"][0].accounts[0]["account"],
            "1110 - Cash - TC",
        )
        self.assertEqual(
            self.state["journal_entries"][0].accounts[1]["account"],
            "2190 - Gift Card Liability - TC",
        )

    def test_enrich_je_accounts_fails_closed_when_foreign_selling_rate_is_missing(self):
        frappe_mod = sys.modules["frappe"]
        erpnext_utils = sys.modules["erpnext.setup.utils"]
        orig_get_cached_value = frappe_mod.get_cached_value
        orig_get_exchange_rate = erpnext_utils.get_exchange_rate
        orig_utils = frappe_mod.utils
        captured_calls = []

        try:
            frappe_mod.utils = types.SimpleNamespace(
                now_datetime=lambda: "2026-04-05 12:00:00",
                flt=lambda value, *_args, **_kwargs: float(value or 0),
            )
            frappe_mod.get_cached_value = lambda doctype, name, fieldname: (
                "UGX"
                if doctype == "Company" and fieldname == "default_currency"
                else "USD"
                if doctype == "Account" and fieldname == "account_currency"
                else None
            )

            def missing_selling_rate(from_c, to_c, tx_date, args=None):
                captured_calls.append((from_c, to_c, tx_date, args))
                return 0

            erpnext_utils.get_exchange_rate = missing_selling_rate

            accounts = [{"account": "1110 - Cash USD - TC", "debit_in_account_currency": 10}]
            with self.assertRaisesRegex(Exception, "No selling exchange rate"):
                self.module._enrich_je_accounts_with_currency("Test Company", accounts, "2026-11-28")

            self.assertEqual(captured_calls, [("USD", "UGX", "2026-11-28", "for_selling")])
            self.assertNotEqual(accounts[0].get("exchange_rate"), 1)

            # When a positive selling rate exists, it is populated directly.
            erpnext_utils.get_exchange_rate = lambda from_c, to_c, tx_date, args=None: (
                3700.0 if args == "for_selling" else 4050.0
            )
            valid_accounts = [{"account": "1110 - Cash USD - TC", "debit_in_account_currency": 10}]
            multi = self.module._enrich_je_accounts_with_currency(
                "Test Company", valid_accounts, "2026-11-28"
            )
            self.assertEqual(multi, 1)
            self.assertEqual(valid_accounts[0]["exchange_rate"], 3700.0)
        finally:
            frappe_mod.get_cached_value = orig_get_cached_value
            erpnext_utils.get_exchange_rate = orig_get_exchange_rate
            frappe_mod.utils = orig_utils

    def test_create_gift_card_je_preserves_valid_selling_rate_of_one_through_erpnext_set_exchange_rate(
        self,
    ):
        class FlagsDict(dict):
            def __getattr__(self, key):
                return self.get(key)

            def __setattr__(self, key, value):
                self[key] = value

        class PinnedERPNextJournalEntry(FakeJournalEntry):
            """Mirror pinned ERPNext v16.60.0 JournalEntry.set_exchange_rate (lines 1030-1062)."""

            def __init__(self, payload=None):
                super().__init__(payload)
                self.company_currency = "EUR"
                self.flags = FlagsDict(ignore_permissions=False)

            def get(self, key, default=None):
                return getattr(self, key, default)

            def set_exchange_rate(self):
                for d in self.accounts:
                    if d.get("account_currency") == self.company_currency:
                        d["exchange_rate"] = 1
                    elif not d.get("exchange_rate") or d.get("exchange_rate") == 1:
                        ignore_exchange_rate = False
                        if self.get("flags") and self.flags.get("ignore_exchange_rate"):
                            ignore_exchange_rate = True
                        if not ignore_exchange_rate:
                            # ERPNext journal_entry.py:1796 unqualified lookup picks buying-only 1.05
                            d["exchange_rate"] = 1.05

            def save(self, ignore_permissions=False):
                self.set_exchange_rate()
                return super().save(ignore_permissions=ignore_permissions)

        frappe_mod = sys.modules["frappe"]
        erpnext_utils = sys.modules["erpnext.setup.utils"]
        orig_get_cached_value = frappe_mod.get_cached_value
        orig_get_exchange_rate = erpnext_utils.get_exchange_rate
        orig_get_doc = frappe_mod.get_doc
        orig_utils = frappe_mod.utils
        created_jes = []

        try:
            frappe_mod.utils = types.SimpleNamespace(
                now_datetime=lambda: "2026-11-28 12:00:00",
                flt=lambda value, *_args, **_kwargs: float(value or 0),
            )
            frappe_mod.get_cached_value = lambda doctype, name, fieldname: (
                "EUR"
                if doctype == "Company" and fieldname == "default_currency"
                else "USD"
                if doctype == "Account" and fieldname == "account_currency"
                else None
            )
            # Valid 1:1 selling parity (1.00), whereas ERPNext unqualified lookup would return 1.05.
            erpnext_utils.get_exchange_rate = lambda from_c, to_c, tx_date, args=None: (
                1.0 if args == "for_selling" else 1.05
            )

            def fake_get_doc(doctype, name=None):
                if isinstance(doctype, dict) and doctype.get("doctype") == "Journal Entry":
                    je = PinnedERPNextJournalEntry(doctype)
                    created_jes.append(je)
                    return je
                return orig_get_doc(doctype, name)

            frappe_mod.get_doc = fake_get_doc

            je = self.module._create_gift_card_journal_entry(
                "Test Company",
                "2026-11-28",
                "Parity gift card JE",
                [
                    {"account": "1110 - Cash USD - TC", "debit_in_account_currency": 50},
                    {"account": "2190 - Gift Card USD - TC", "credit_in_account_currency": 50},
                ],
            )

            self.assertEqual(len(created_jes), 1)
            self.assertEqual(je.accounts[0]["exchange_rate"], 1.0)
            self.assertEqual(je.accounts[1]["exchange_rate"], 1.0)
            self.assertTrue(je.flags.get("ignore_exchange_rate"))
        finally:
            frappe_mod.get_cached_value = orig_get_cached_value
            erpnext_utils.get_exchange_rate = orig_get_exchange_rate
            frappe_mod.get_doc = orig_get_doc
            frappe_mod.utils = orig_utils


if __name__ == "__main__":
    unittest.main()
