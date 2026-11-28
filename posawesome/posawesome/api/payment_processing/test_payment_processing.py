import json
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import Mock, patch

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_ORIGINAL_MODULES = dict(sys.modules)


def tearDownModule():
    managed_prefixes = ("frappe", "erpnext", "posawesome")
    for name in list(sys.modules):
        if name.startswith(managed_prefixes) and name not in _ORIGINAL_MODULES:
            sys.modules.pop(name, None)
    for name, module in _ORIGINAL_MODULES.items():
        if name.startswith(managed_prefixes):
            sys.modules[name] = module


class AttrDict(dict):
    __getattr__ = dict.get


class FakePaymentEntry:
    def __init__(self, name="ACC-PAY-TEST-0001", paid_amount=0):
        self.name = name
        self.paid_amount = paid_amount
        self.amount = paid_amount
        self.references = []
        self.total_allocated_amount = None
        self.unallocated_amount = None
        self.difference_amount = None
        self.saved = False
        self.submitted = False

    def append(self, fieldname, value):
        if fieldname == "references":
            self.references.append(AttrDict(value))

    def save(self, ignore_permissions=False):
        self.saved = ignore_permissions

    def submit(self):
        self.submitted = True

    def get(self, key, default=None):
        return getattr(self, key, default)


def _install_framework_stubs():
    frappe_module = types.ModuleType("frappe")
    frappe_utils = types.ModuleType("frappe.utils")
    frappe_utils.nowdate = lambda: "2026-03-13"
    frappe_utils.getdate = lambda value: value
    frappe_utils.flt = lambda value, precision=None: float(value or 0)
    frappe_utils.fmt_money = lambda value, currency=None: f"{currency or ''} {value}".strip()
    frappe_utils.cint = lambda value: int(value or 0)

    frappe_module._ = lambda text: text
    frappe_module._dict = lambda value: AttrDict(value)
    frappe_module.throw = lambda message, *args, **kwargs: (_ for _ in ()).throw(Exception(message))
    frappe_module.whitelist = lambda *args, **kwargs: (lambda fn: fn)
    frappe_module.log_error = lambda *args, **kwargs: None
    frappe_module.logger = lambda: types.SimpleNamespace(info=lambda *args, **kwargs: None)
    frappe_module.msgprint = lambda *args, **kwargs: None
    frappe_module.PermissionError = PermissionError
    frappe_module.session = types.SimpleNamespace(user="cashier@example.com")
    frappe_module.get_cached_value = lambda *args, **kwargs: None
    frappe_module.get_list = lambda *args, **kwargs: []
    frappe_module.get_doc = lambda *args, **kwargs: None
    frappe_module.get_cached_doc = lambda *args, **kwargs: None
    frappe_module.new_doc = lambda *args, **kwargs: None
    frappe_module.db = types.SimpleNamespace(
        sql=lambda *args, **kwargs: [], get_value=lambda *args, **kwargs: None
    )
    frappe_module.utils = frappe_utils

    sys.modules["frappe"] = frappe_module
    sys.modules["frappe.utils"] = frappe_utils

    pos_access_module = types.ModuleType("posawesome.posawesome.api.pos_access")
    pos_access_module.get_authorized_pos_profile = lambda pos_profile=None, company=None: AttrDict(
        {
            "name": pos_profile or "Main POS",
            "company": company or "Test Company",
            "posa_use_pos_awesome_payments": 1,
            "posa_allow_make_new_payments": 1,
            "posa_allow_reconcile_payments": 1,
            "posa_allow_mpesa_reconcile_payments": 1,
            "cost_center": "Main - TC",
        }
    )
    sys.modules["posawesome.posawesome.api.pos_access"] = pos_access_module

    terminal_state_module = types.ModuleType("posawesome.posawesome.api.terminal_state")
    terminal_state_module.get_active_terminal_cashier = (
        lambda *_args, **_kwargs: "cashier@example.com"
    )
    sys.modules["posawesome.posawesome.api.terminal_state"] = terminal_state_module

    erpnext_module = types.ModuleType("erpnext")
    erpnext_module.get_default_cost_center = lambda company: "Main - TC"
    sys.modules["erpnext"] = erpnext_module

    accounts_party = types.ModuleType("erpnext.accounts.party")
    accounts_party.get_party_account = lambda *args, **kwargs: "Debtors - TC"
    sys.modules["erpnext.accounts.party"] = accounts_party

    accounts_utils = types.ModuleType("erpnext.accounts.utils")
    accounts_utils.get_outstanding_invoices = lambda *args, **kwargs: []
    accounts_utils.reconcile_against_document = lambda *args, **kwargs: None
    accounts_utils.get_account_currency = lambda *args, **kwargs: "USD"
    sys.modules["erpnext.accounts.utils"] = accounts_utils

    setup_utils = types.ModuleType("erpnext.setup.utils")
    setup_utils.get_exchange_rate = lambda *args, **kwargs: 1
    sys.modules["erpnext.setup.utils"] = setup_utils

    bank_account_module = types.ModuleType("erpnext.accounts.doctype.bank_account.bank_account")
    bank_account_module.get_party_bank_account = lambda *args, **kwargs: None
    sys.modules["erpnext.accounts.doctype.bank_account.bank_account"] = bank_account_module

    journal_entry_module = types.ModuleType("erpnext.accounts.doctype.journal_entry.journal_entry")
    journal_entry_module.get_default_bank_cash_account = (
        lambda company, account_type, mode_of_payment=None, account=None: types.SimpleNamespace(
            account="Cash - TC",
            account_currency="USD",
            get=lambda key, default=None: getattr(
                types.SimpleNamespace(account="Cash - TC", account_currency="USD"), key, default
            ),
        )
    )
    sys.modules["erpnext.accounts.doctype.journal_entry.journal_entry"] = journal_entry_module

    payment_reconciliation_module = types.ModuleType(
        "erpnext.accounts.doctype.payment_reconciliation.payment_reconciliation"
    )
    payment_reconciliation_module.reconcile_dr_cr_note = lambda *args, **kwargs: None
    sys.modules["erpnext.accounts.doctype.payment_reconciliation.payment_reconciliation"] = (
        payment_reconciliation_module
    )

    accounts_controller_module = types.ModuleType("erpnext.controllers.accounts_controller")
    accounts_controller_module.get_advance_payment_entries_for_regional = lambda *args, **kwargs: []
    sys.modules["erpnext.controllers.accounts_controller"] = accounts_controller_module

    mpesa_module = types.ModuleType("posawesome.posawesome.api.m_pesa")
    mpesa_module.get_authorized_mpesa_payment = lambda *args, **kwargs: None
    mpesa_module.submit_mpesa_payment = lambda *args, **kwargs: None
    sys.modules["posawesome.posawesome.api.m_pesa"] = mpesa_module


def _install_package_stubs():
    package_paths = {
        "posawesome": REPO_ROOT / "posawesome",
        "posawesome.posawesome": REPO_ROOT / "posawesome" / "posawesome",
        "posawesome.posawesome.api": REPO_ROOT / "posawesome" / "posawesome" / "api",
        "posawesome.posawesome.api.payment_processing": (
            REPO_ROOT / "posawesome" / "posawesome" / "api" / "payment_processing"
        ),
    }
    for name, path in package_paths.items():
        module = types.ModuleType(name)
        module.__path__ = [str(path)]
        sys.modules[name] = module


def _load_module(module_name, file_path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class TestPosPaymentProcessing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_framework_stubs()
        _install_package_stubs()
        sys.modules.pop("posawesome.posawesome.api.idempotency", None)
        payment_processing_dir = REPO_ROOT / "posawesome" / "posawesome" / "api" / "payment_processing"
        _load_module(
            "posawesome.posawesome.api.payment_processing.utils",
            payment_processing_dir / "utils.py",
        )
        cls.creation = _load_module(
            "posawesome.posawesome.api.payment_processing.creation",
            payment_processing_dir / "creation.py",
        )
        cls.data = _load_module(
            "posawesome.posawesome.api.payment_processing.data",
            payment_processing_dir / "data.py",
        )
        cls.processor = _load_module(
            "posawesome.posawesome.api.payment_processing.processor",
            payment_processing_dir / "processor.py",
        )
        cls.reconciliation = _load_module(
            "posawesome.posawesome.api.payment_processing.reconciliation",
            payment_processing_dir / "reconciliation.py",
        )

    def setUp(self):
        self.real_authorize_payment_request = self.processor._authorize_payment_request

        def authorize_legacy_fixture(data):
            profile = AttrDict(dict(data.get("pos_profile") or {}))
            profile["name"] = data.get("pos_profile_name") or "Main POS"
            profile["company"] = data.get("company") or "Test Company"
            data.pos_profile = profile
            data.pos_profile_name = profile["name"]
            data.company = profile["company"]
            return profile, "cashier@example.com"

        self.authorization_patcher = patch.object(
            self.processor,
            "_authorize_payment_request",
            side_effect=authorize_legacy_fixture,
        )
        self.authorization_patcher.start()
        self.addCleanup(self.authorization_patcher.stop)

    def _payment_request_data(self, **overrides):
        data = AttrDict(
            {
                "pos_profile_name": "Main POS",
                "pos_profile": {"name": "Main POS"},
                "company": "Test Company",
                "pos_opening_shift_name": "POS-OPEN-0001",
            }
        )
        data.update(overrides)
        return data

    def test_supplier_payment_reconciliation_row_uses_payable_direction(self):
        row = self.processor._build_payment_reconciliation_row(
            AttrDict(
                paid_from="Bank - TC",
                paid_to="Creditors - TC",
                cost_center="Main - TC",
            ),
            "ACC-PAY-SUP-0001",
            {
                "name": "PINV-0001",
                "voucher_type": "Purchase Invoice",
                "due_date": "2026-07-31",
            },
            "Supplier",
            "SUPP-0001",
            200,
            125,
            300,
        )

        self.assertEqual(row["against_voucher_type"], "Purchase Invoice")
        self.assertEqual(row["against_voucher"], "PINV-0001")
        self.assertEqual(row["account"], "Creditors - TC")
        self.assertEqual(row["party_type"], "Supplier")
        self.assertEqual(row["party"], "SUPP-0001")
        self.assertEqual(row["dr_or_cr"], "debit_in_account_currency")
        self.assertEqual(row["allocated_amount"], 125)

    def test_payment_replay_matches_original_tender_currency_and_amount(self):
        existing = [
            AttrDict(
                name="ACC-PAY-USD",
                docstatus=1,
                mode_of_payment="Cash",
                paid_amount=29.93,
                received_amount=0.105,
                posa_payment_currency="USD",
                posa_original_amount=29.93,
            ),
            AttrDict(
                name="ACC-PAY-PKR",
                docstatus=1,
                mode_of_payment="Cash",
                paid_amount=29.93,
                received_amount=29.93,
                posa_payment_currency="PKR",
                posa_original_amount=29.93,
            ),
        ]

        matched, missing, unmatched = self.processor._partition_payment_methods(
            existing,
            [
                {
                    "mode_of_payment": "Cash",
                    "amount": 29.93,
                    "original_amount": 29.93,
                    "payment_currency": "PKR",
                }
            ],
        )

        self.assertEqual([entry.name for entry in matched], ["ACC-PAY-PKR"])
        self.assertEqual(missing, [])
        self.assertEqual([entry.name for entry in unmatched], ["ACC-PAY-USD"])

    def test_create_payment_entry_persists_original_tender_metadata(self):
        class Meta:
            @staticmethod
            def has_field(_fieldname):
                return True

        class PaymentEntry(AttrDict):
            meta = Meta()

            def setup_party_account_field(self):
                return None

            def set_missing_values(self):
                return None

            def set(self, fieldname, value):
                self[fieldname] = value

        payment_entry = PaymentEntry()
        company_doc = AttrDict(
            default_currency="PKR",
            default_letter_head="Letter Head",
        )
        bank = AttrDict(
            account="Cash PKR - TC",
            account_currency="PKR",
            account_type="Cash",
        )
        fake_db = types.SimpleNamespace(get_default=lambda _key: 2)

        with (
            patch.object(self.creation.frappe, "get_cached_doc", return_value=company_doc),
            patch.object(self.creation.frappe, "new_doc", return_value=payment_entry),
            patch.object(self.creation.frappe, "db", fake_db),
            patch.object(self.creation, "get_party_account", return_value="Debtors - TC"),
            patch.object(self.creation, "get_account_currency", return_value="PKR"),
            patch.object(self.creation, "get_bank_cash_account", return_value=bank),
            patch.object(self.creation, "get_party_bank_account", return_value=None),
            patch.object(
                self.creation,
                "get_exchange_rate",
                side_effect=lambda source, target, *_args: (
                    0.003508772 if (source, target) == ("PKR", "USD") else 1
                ),
            ),
        ):
            result = self.creation.create_payment_entry(
                company="Test Company",
                amount=29.93,
                currency="USD",
                invoice_currency="USD",
                mode_of_payment="Cash",
                customer="Customer 727",
                payment_currency="PKR",
                bank_account="Cash PKR - TC",
                posting_date="2026-08-21",
            )

        self.assertEqual(result.posa_payment_currency, "PKR")
        self.assertEqual(result.posa_original_amount, 29.93)
        self.assertEqual(result.posa_invoice_currency, "USD")
        self.assertAlmostEqual(result.posa_exchange_rate, 0.003508772)
        self.assertEqual(result.posa_company_exchange_rate, 1)
        self.assertEqual(result.posa_account_currency, "PKR")
        self.assertEqual(result.posa_account_amount, 29.93)
        self.assertEqual(result.base_paid_amount, 29.93)
        self.assertEqual(result.base_received_amount, 29.93)

    def test_create_payment_entry_converts_foreign_tender_into_account_currency(self):
        class Meta:
            @staticmethod
            def has_field(_fieldname):
                return True

        class PaymentEntry(AttrDict):
            meta = Meta()

            def setup_party_account_field(self):
                return None

            def set_missing_values(self):
                return None

            def set(self, fieldname, value):
                self[fieldname] = value

        payment_entry = PaymentEntry()
        company_doc = AttrDict(
            default_currency="PKR",
            default_letter_head="Letter Head",
        )
        bank = AttrDict(
            account="Cash PKR - TC",
            account_currency="PKR",
            account_type="Cash",
        )
        fake_db = types.SimpleNamespace(get_default=lambda _key: 2)

        with (
            patch.object(self.creation.frappe, "get_cached_doc", return_value=company_doc),
            patch.object(self.creation.frappe, "new_doc", return_value=payment_entry),
            patch.object(self.creation.frappe, "db", fake_db),
            patch.object(self.creation, "get_party_account", return_value="Debtors - TC"),
            patch.object(self.creation, "get_account_currency", return_value="PKR"),
            patch.object(self.creation, "get_bank_cash_account", return_value=bank),
            patch.object(self.creation, "get_party_bank_account", return_value=None),
            patch.object(
                self.creation,
                "get_exchange_rate",
                side_effect=lambda source, target, *_args: (
                    285 if (source, target) == ("USD", "PKR") else 1
                ),
            ),
        ):
            result = self.creation.create_payment_entry(
                company="Test Company",
                amount=10,
                currency="USD",
                invoice_currency="USD",
                mode_of_payment="Cash",
                customer="Customer 727",
                payment_currency="USD",
                bank_account="Cash PKR - TC",
                posting_date="2026-08-21",
            )

        self.assertEqual(result.posa_payment_currency, "USD")
        self.assertEqual(result.posa_original_amount, 10)
        self.assertEqual(result.posa_exchange_rate, 1)
        self.assertEqual(result.posa_company_exchange_rate, 285)
        self.assertEqual(result.posa_account_currency, "PKR")
        self.assertEqual(result.posa_account_amount, 2850)
        self.assertEqual(result.received_amount, 2850)
        self.assertEqual(result.base_received_amount, 2850)

    @patch("posawesome.posawesome.api.payment_processing.processor.create_payment_entry")
    @patch("posawesome.posawesome.api.payment_processing.processor.frappe")
    def test_process_pos_payment_rejects_currency_outside_profile_allow_list(
        self,
        mock_frappe,
        mock_create_payment_entry,
    ):
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.get_cached_value.return_value = "PKR"
        mock_frappe.throw.side_effect = lambda message, *_args, **_kwargs: (_ for _ in ()).throw(
            Exception(message)
        )
        mock_frappe.db = types.SimpleNamespace(
            has_column=lambda *_args: False,
            sql=lambda *_args, **_kwargs: [],
            get_value=lambda *_args, **_kwargs: None,
        )

        result = self.processor.process_pos_payment(
            json.dumps(
                {
                    "customer": "Customer 727",
                    "company": "Test Company",
                    "currency": "USD",
                    "pos_profile_name": "Main POS",
                    "pos_opening_shift_name": "POS-OPEN-0001",
                    "selected_invoices": [],
                    "selected_payments": [],
                    "selected_mpesa_payments": [],
                    "payment_methods": [
                        {
                            "mode_of_payment": "Cash",
                            "amount": 10,
                            "original_amount": 10,
                            "payment_currency": "EUR",
                        }
                    ],
                    "total_selected_payments": 0,
                    "total_selected_mpesa_payments": 0,
                    "total_payment_methods": 10,
                    "pos_profile": {
                        "posa_use_pos_awesome_payments": 1,
                        "posa_allow_make_new_payments": 1,
                        "posa_allow_reconcile_payments": 0,
                        "posa_allow_mpesa_reconcile_payments": 0,
                        "posa_enable_multi_currency_payments": 1,
                        "posa_allowed_currencies": [
                            {"currency": "PKR", "allow_for_payments": 1},
                            {"currency": "USD", "allow_for_payments": 1},
                        ],
                    },
                }
            )
        )

        mock_create_payment_entry.assert_not_called()
        self.assertTrue(any("EUR" in error for error in result["errors"]))

    def test_selected_mpesa_reference_is_reloaded_and_normalized_from_server(self):
        profile = AttrDict(
            name="Main POS",
            company="Test Company",
        )
        register = AttrDict(
            name="MPRE-0001",
            transamount=150,
            company="Test Company",
            mode_of_payment="M-Pesa",
        )
        authorize = Mock(return_value=register)
        original_authorize = self.processor.get_authorized_mpesa_payment
        self.processor.get_authorized_mpesa_payment = authorize
        self.addCleanup(
            setattr,
            self.processor,
            "get_authorized_mpesa_payment",
            original_authorize,
        )

        rows = self.processor._load_authorized_mpesa_payments(
            [
                {
                    "name": "MPRE-0001",
                    "amount": 999999,
                    "company": "Other Company",
                    "mode_of_payment": "Forged Mode",
                    "customer": "CUST-OTHER",
                }
            ],
            "CUST-0001",
            "Customer",
            profile,
            allow_completed_replay=True,
        )

        authorize.assert_called_once_with(
            "MPRE-0001",
            "CUST-0001",
            profile,
            allow_submitted=True,
        )
        self.assertEqual(
            {key: value for key, value in rows[0].items() if key != "_doc"},
            {
                "name": "MPRE-0001",
                "amount": 150.0,
                "company": "Test Company",
                "mode_of_payment": "M-Pesa",
            },
        )
        self.assertIs(rows[0]["_doc"], register)

    def test_supplier_cannot_submit_mpesa_reconciliation_reference(self):
        with self.assertRaisesRegex(Exception, "only available for Customer receipts"):
            self.processor._load_authorized_mpesa_payments(
                [{"name": "MPRE-0001"}],
                "SUPP-0001",
                "Supplier",
                AttrDict(name="Main POS", company="Test Company"),
            )

    def _opening_shift(self, **overrides):
        opening = AttrDict(
            {
                "name": "POS-OPEN-0001",
                "pos_profile": "Main POS",
                "company": "Test Company",
                "user": "cashier@example.com",
                "docstatus": 1,
                "status": "Open",
                "pos_closing_shift": None,
            }
        )
        opening.update(overrides)
        opening["check_permission"] = Mock()
        return opening

    def test_authorization_rejects_forged_embedded_profile_before_lookup(self):
        authorize_profile = Mock()
        with patch.object(
            self.processor,
            "get_authorized_pos_profile",
            authorize_profile,
        ):
            with self.assertRaisesRegex(Exception, "POS Profile values do not match"):
                self.real_authorize_payment_request(
                    self._payment_request_data(
                        pos_profile={
                            "name": "Other POS",
                            "posa_use_pos_awesome_payments": 1,
                        }
                    )
                )

        authorize_profile.assert_not_called()

    def test_authorization_rejects_unassigned_profile_before_terminal_access(self):
        terminal_cashier = Mock()
        with (
            patch.object(
                self.processor,
                "get_authorized_pos_profile",
                side_effect=PermissionError("unassigned profile"),
            ),
            patch.object(
                self.processor,
                "get_active_terminal_cashier",
                terminal_cashier,
            ),
        ):
            with self.assertRaisesRegex(PermissionError, "unassigned profile"):
                self.real_authorize_payment_request(self._payment_request_data())

        terminal_cashier.assert_not_called()

    def test_authorization_propagates_forged_company_rejection(self):
        authorize_profile = Mock(side_effect=PermissionError("forged company"))
        with patch.object(
            self.processor,
            "get_authorized_pos_profile",
            authorize_profile,
        ):
            with self.assertRaisesRegex(PermissionError, "forged company"):
                self.real_authorize_payment_request(
                    self._payment_request_data(company="Other Company")
                )

        authorize_profile.assert_called_once_with("Main POS", company="Other Company")

    def test_authorization_rejects_locked_terminal_before_opening_lookup(self):
        canonical_profile = AttrDict(name="Main POS", company="Test Company")
        opening_lookup = Mock()
        with (
            patch.object(
                self.processor,
                "get_authorized_pos_profile",
                return_value=canonical_profile,
            ),
            patch.object(
                self.processor,
                "get_active_terminal_cashier",
                side_effect=PermissionError("terminal locked"),
            ),
            patch.object(self.processor.frappe, "get_doc", opening_lookup),
        ):
            with self.assertRaisesRegex(PermissionError, "terminal locked"):
                self.real_authorize_payment_request(self._payment_request_data())

        opening_lookup.assert_not_called()

    def test_authorization_rejects_opening_shift_from_another_profile(self):
        canonical_profile = AttrDict(name="Main POS", company="Test Company")
        with (
            patch.object(
                self.processor,
                "get_authorized_pos_profile",
                return_value=canonical_profile,
            ),
            patch.object(
                self.processor,
                "get_active_terminal_cashier",
                return_value="cashier@example.com",
            ),
            patch.object(
                self.processor.frappe,
                "get_doc",
                return_value=self._opening_shift(pos_profile="Other POS"),
            ),
        ):
            with self.assertRaisesRegex(Exception, "does not belong to this POS Profile"):
                self.real_authorize_payment_request(self._payment_request_data())

    def test_authorization_uses_canonical_profile_settings_not_client_flags(self):
        canonical_profile = AttrDict(
            name="Main POS",
            company="Test Company",
            posa_use_pos_awesome_payments=0,
            posa_allow_make_new_payments=0,
            cost_center="Canonical - TC",
        )
        data = self._payment_request_data(
            pos_profile={
                "name": "Main POS",
                "posa_use_pos_awesome_payments": 1,
                "posa_allow_make_new_payments": 1,
                "cost_center": "Forged - XX",
            }
        )
        with (
            patch.object(
                self.processor,
                "get_authorized_pos_profile",
                return_value=canonical_profile,
            ) as authorize_profile,
            patch.object(
                self.processor,
                "get_active_terminal_cashier",
                return_value="cashier@example.com",
            ),
            patch.object(
                self.processor.frappe,
                "get_doc",
                return_value=self._opening_shift(),
            ),
        ):
            resolved_profile, cashier = self.real_authorize_payment_request(data)

        authorize_profile.assert_called_once_with("Main POS", company="Test Company")
        self.assertIs(resolved_profile, canonical_profile)
        self.assertIs(data.pos_profile, canonical_profile)
        self.assertEqual(data.pos_profile.get("cost_center"), "Canonical - TC")
        self.assertEqual(cashier, "cashier@example.com")

    def test_selected_invoice_is_loaded_and_normalized_from_server_state(self):
        invoice_doc = AttrDict(
            name="ACC-SINV-0001",
            docstatus=1,
            company="Test Company",
            customer="Customer 727",
            is_return=0,
            outstanding_amount=125,
            conversion_rate=1.25,
            due_date="2026-04-30",
            posting_date="2026-04-01",
            currency="USD",
            check_permission=Mock(),
        )
        with patch.object(self.processor.frappe, "get_doc", return_value=invoice_doc):
            rows = self.processor._load_authorized_outstanding_invoices(
                [
                    {
                        "name": "ACC-SINV-0001",
                        "voucher_type": "Sales Invoice",
                        "outstanding_amount": 20,
                        "conversion_rate": 99,
                    }
                ],
                "Test Company",
                "Customer",
                "Customer 727",
            )

        self.assertEqual(rows[0]["outstanding_amount"], 125)
        self.assertEqual(rows[0]["conversion_rate"], 1.25)
        invoice_doc.check_permission.assert_called_once_with("read")

    def test_selected_invoice_rejects_cross_scope_cancelled_and_overstated_rows(self):
        base = {
            "name": "ACC-SINV-0001",
            "docstatus": 1,
            "company": "Test Company",
            "customer": "Customer 727",
            "is_return": 0,
            "outstanding_amount": 100,
        }
        cases = (
            ({"company": "Other Company"}, 50, "selected company"),
            ({"customer": "Other Customer"}, 50, "selected Customer"),
            ({"docstatus": 2}, 50, "submitted and not cancelled"),
            ({}, 101, "exceeds its current available amount"),
        )
        for overrides, requested_amount, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                invoice_doc = AttrDict({**base, **overrides})
                with (
                    patch.object(self.processor.frappe, "get_doc", return_value=invoice_doc),
                    self.assertRaisesRegex(Exception, expected_error),
                ):
                    self.processor._load_authorized_outstanding_invoices(
                        [
                            {
                                "name": "ACC-SINV-0001",
                                "voucher_type": "Sales Invoice",
                                "outstanding_amount": requested_amount,
                            }
                        ],
                        "Test Company",
                        "Customer",
                        "Customer 727",
                    )

    def test_completed_payment_replay_accepts_stale_client_amount_but_keeps_server_zero(self):
        invoice_doc = AttrDict(
            name="ACC-SINV-PAID-0001",
            docstatus=1,
            company="Test Company",
            customer="Customer 727",
            is_return=0,
            outstanding_amount=0,
        )
        payment_doc = AttrDict(
            name="ACC-PAY-ALLOCATED-0001",
            docstatus=1,
            company="Test Company",
            party_type="Customer",
            party="Customer 727",
            payment_type="Receive",
            unallocated_amount=0,
        )

        def get_doc(doctype, name):
            return invoice_doc if doctype == "Sales Invoice" else payment_doc

        with patch.object(self.processor.frappe, "get_doc", side_effect=get_doc):
            invoices = self.processor._load_authorized_outstanding_invoices(
                [{"name": invoice_doc.name, "outstanding_amount": 100}],
                "Test Company",
                "Customer",
                "Customer 727",
                allow_completed_replay=True,
            )
            payments = self.processor._load_authorized_reconciliation_payments(
                [{"name": payment_doc.name, "voucher_type": "Payment Entry", "amount": 100}],
                "Test Company",
                "Customer",
                "Customer 727",
                allow_completed_replay=True,
            )

        self.assertEqual(invoices[0]["outstanding_amount"], 0)
        self.assertEqual(payments[0]["unallocated_amount"], 0)

    def test_selected_payment_is_loaded_and_normalized_from_server_state(self):
        payment_doc = AttrDict(
            name="ACC-PAY-0001",
            docstatus=1,
            company="Test Company",
            party_type="Customer",
            party="Customer 727",
            payment_type="Receive",
            unallocated_amount=80,
            check_permission=Mock(),
        )
        with patch.object(self.processor.frappe, "get_doc", return_value=payment_doc):
            rows = self.processor._load_authorized_reconciliation_payments(
                [{"name": "ACC-PAY-0001", "voucher_type": "Payment Entry", "amount": 25}],
                "Test Company",
                "Customer",
                "Customer 727",
            )

        self.assertEqual(rows[0]["unallocated_amount"], 80)
        self.assertEqual(rows[0]["voucher_type"], "Payment Entry")
        payment_doc.check_permission.assert_called_once_with("read")

    def test_selected_payment_rejects_forged_party_direction_and_amount(self):
        base = {
            "name": "ACC-PAY-0001",
            "docstatus": 1,
            "company": "Test Company",
            "party_type": "Customer",
            "party": "Customer 727",
            "payment_type": "Receive",
            "unallocated_amount": 80,
        }
        cases = (
            ({"party": "Other Customer"}, 20, "selected Customer"),
            ({"payment_type": "Pay"}, 20, "incompatible payment direction"),
            ({"docstatus": 2}, 20, "submitted and not cancelled"),
            ({}, 81, "exceeds its current available amount"),
        )
        for overrides, requested_amount, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                payment_doc = AttrDict({**base, **overrides})
                with (
                    patch.object(self.processor.frappe, "get_doc", return_value=payment_doc),
                    self.assertRaisesRegex(Exception, expected_error),
                ):
                    self.processor._load_authorized_reconciliation_payments(
                        [
                            {
                                "name": "ACC-PAY-0001",
                                "voucher_type": "Payment Entry",
                                "amount": requested_amount,
                            }
                        ],
                        "Test Company",
                        "Customer",
                        "Customer 727",
                    )

    def test_credit_note_selection_requires_a_submitted_customer_return(self):
        credit_note = AttrDict(
            name="ACC-SINV-RET-0001",
            docstatus=1,
            company="Test Company",
            customer="Customer 727",
            is_return=0,
            outstanding_amount=-50,
        )
        with (
            patch.object(self.processor.frappe, "get_doc", return_value=credit_note),
            self.assertRaisesRegex(Exception, "is not a credit note"),
        ):
            self.processor._load_authorized_reconciliation_payments(
                [
                    {
                        "name": "ACC-SINV-RET-0001",
                        "voucher_type": "Sales Invoice",
                        "outstanding_amount": -50,
                    }
                ],
                "Test Company",
                "Customer",
                "Customer 727",
            )

    def test_locked_terminal_blocks_payment_replay_lookup(self):
        self.processor._authorize_payment_request.side_effect = PermissionError("terminal locked")
        replay_lookup = Mock()
        payload = {
            "customer": "Customer 727",
            "company": "Test Company",
            "currency": "USD",
            "pos_profile_name": "Main POS",
            "pos_opening_shift_name": "POS-OPEN-0001",
            "selected_invoices": [],
            "selected_payments": [],
            "selected_mpesa_payments": [],
            "payment_methods": [{"mode_of_payment": "Cash", "amount": 100}],
            "total_selected_invoices": 0,
            "total_selected_payments": 0,
            "total_selected_mpesa_payments": 0,
            "total_payment_methods": 100,
            "pos_profile": {"name": "Main POS"},
        }

        with patch.object(
            self.processor,
            "find_payment_entries_by_client_request_id",
            replay_lookup,
        ):
            with self.assertRaisesRegex(PermissionError, "terminal locked"):
                self.processor.process_pos_payment(json.dumps(payload))

        replay_lookup.assert_not_called()

    def test_payment_request_replay_lookup_is_scoped_to_company_and_party(self):
        get_list = Mock(return_value=[])
        original_get_list = self.processor.frappe.get_list
        self.processor.frappe.get_list = get_list
        self.addCleanup(setattr, self.processor.frappe, "get_list", original_get_list)

        result = self.processor.find_payment_entries_by_client_request_id(
            "pay-scope-001",
            company="Test Company",
            party_type="Customer",
            party="Customer 727",
        )

        self.assertEqual(result, [])
        self.assertEqual(
            get_list.call_args.kwargs["filters"],
            {
                "posa_client_request_id": "pay-scope-001",
                "company": "Test Company",
                "party_type": "Customer",
                "party": "Customer 727",
            },
        )

    def test_party_bank_account_uses_lazy_erpnext_compat_resolver(self):
        resolved_helper = Mock(return_value="BANK-ACCOUNT-0001")

        with patch.object(
            self.creation,
            "resolve_get_party_bank_account",
            return_value=resolved_helper,
        ) as resolver:
            result = self.creation.get_party_bank_account("Customer", "CUST-0001")

        resolver.assert_called_once_with()
        resolved_helper.assert_called_once_with("Customer", "CUST-0001")
        self.assertEqual(result, "BANK-ACCOUNT-0001")

    @patch("posawesome.posawesome.api.payment_processing.processor.create_payment_entry")
    @patch("posawesome.posawesome.api.payment_processing.processor.frappe")
    def test_process_pos_payment_keeps_new_payment_unallocated_without_selected_invoices(
        self,
        mock_frappe,
        mock_create_payment_entry,
    ):
        fake_payment_entry = FakePaymentEntry(paid_amount=100)
        fake_payment_entry.difference_amount = 100
        mock_create_payment_entry.return_value = fake_payment_entry
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.log_error = Mock()
        mock_frappe.msgprint = Mock()

        payload = {
            "customer": "Customer 727",
            "company": "Test Company",
            "currency": "USD",
            "pos_profile_name": "Main POS",
            "pos_opening_shift_name": "POS-OPEN-0001",
            "selected_invoices": [],
            "selected_payments": [],
            "selected_mpesa_payments": [],
            "payment_methods": [{"mode_of_payment": "Cash", "amount": 100}],
            "total_selected_invoices": 0,
            "total_selected_payments": 0,
            "total_selected_mpesa_payments": 0,
            "total_payment_methods": 100,
            "exchange_rate": None,
            "pos_profile": {
                "posa_use_pos_awesome_payments": 1,
                "posa_allow_make_new_payments": 1,
                "posa_allow_reconcile_payments": 1,
                "posa_allow_mpesa_reconcile_payments": 0,
                "cost_center": "Main - TC",
            },
        }

        self.processor.process_pos_payment(json.dumps(payload))

        self.assertEqual(fake_payment_entry.references, [])
        self.assertEqual(fake_payment_entry.total_allocated_amount, 0)
        self.assertEqual(fake_payment_entry.unallocated_amount, 100)
        self.assertEqual(fake_payment_entry.difference_amount, 100)

    @patch("posawesome.posawesome.api.payment_processing.processor.create_payment_entry")
    @patch("posawesome.posawesome.api.payment_processing.processor.frappe")
    def test_process_pos_payment_uses_payload_posting_date_for_new_entries(
        self,
        mock_frappe,
        mock_create_payment_entry,
    ):
        fake_payment_entry = FakePaymentEntry(paid_amount=100)
        mock_create_payment_entry.return_value = fake_payment_entry
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.log_error = Mock()
        mock_frappe.msgprint = Mock()

        payload = {
            "customer": "Customer 727",
            "company": "Test Company",
            "currency": "USD",
            "pos_profile_name": "Main POS",
            "pos_opening_shift_name": "POS-OPEN-0001",
            "posting_date": "2026-03-29",
            "selected_invoices": [],
            "selected_payments": [],
            "selected_mpesa_payments": [],
            "payment_methods": [{"mode_of_payment": "Cash", "amount": 100}],
            "total_selected_invoices": 0,
            "total_selected_payments": 0,
            "total_selected_mpesa_payments": 0,
            "total_payment_methods": 100,
            "exchange_rate": None,
            "pos_profile": {
                "posa_use_pos_awesome_payments": 1,
                "posa_allow_make_new_payments": 1,
                "posa_allow_reconcile_payments": 1,
                "posa_allow_mpesa_reconcile_payments": 0,
                "cost_center": "Main - TC",
            },
        }

        self.processor.process_pos_payment(json.dumps(payload))

        mock_create_payment_entry.assert_called_once()
        self.assertEqual(
            mock_create_payment_entry.call_args.kwargs["posting_date"],
            "2026-03-29",
        )
        self.assertEqual(
            mock_create_payment_entry.call_args.kwargs["reference_date"],
            "2026-03-29",
        )

    @patch("posawesome.posawesome.api.payment_processing.processor.create_payment_entry")
    @patch("posawesome.posawesome.api.payment_processing.processor.frappe")
    def test_process_pos_payment_passes_supplier_pay_context_to_creation(
        self,
        mock_frappe,
        mock_create_payment_entry,
    ):
        fake_payment_entry = FakePaymentEntry(paid_amount=250)
        mock_create_payment_entry.return_value = fake_payment_entry
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.log_error = Mock()
        mock_frappe.msgprint = Mock()

        payload = {
            "customer": "Supp-001",
            "party": "Supp-001",
            "party_type": "Supplier",
            "payment_type": "Pay",
            "company": "Test Company",
            "currency": "USD",
            "pos_profile_name": "Main POS",
            "pos_opening_shift_name": "POS-OPEN-0001",
            "posting_date": "2026-03-30",
            "selected_invoices": [],
            "selected_payments": [],
            "selected_mpesa_payments": [],
            "payment_methods": [{"mode_of_payment": "Bank", "amount": 250}],
            "total_selected_invoices": 0,
            "total_selected_payments": 0,
            "total_selected_mpesa_payments": 0,
            "total_payment_methods": 250,
            "exchange_rate": None,
            "pos_profile": {
                "posa_use_pos_awesome_payments": 1,
                "posa_allow_make_new_payments": 1,
                "posa_allow_reconcile_payments": 0,
                "posa_allow_mpesa_reconcile_payments": 0,
                "cost_center": "Main - TC",
            },
        }

        self.processor.process_pos_payment(json.dumps(payload))

        mock_create_payment_entry.assert_called_once()
        self.assertEqual(
            mock_create_payment_entry.call_args.kwargs["party_type"],
            "Supplier",
        )
        self.assertEqual(
            mock_create_payment_entry.call_args.kwargs["payment_type"],
            "Pay",
        )
        self.assertEqual(
            mock_create_payment_entry.call_args.kwargs["party"],
            "Supp-001",
        )

    @patch("posawesome.posawesome.api.payment_processing.processor.find_payment_entries_by_client_request_id")
    @patch("posawesome.posawesome.api.payment_processing.processor.create_payment_entry")
    @patch("posawesome.posawesome.api.payment_processing.processor.frappe")
    def test_process_pos_payment_returns_existing_entries_for_same_client_request_id(
        self,
        mock_frappe,
        mock_create_payment_entry,
        mock_find_existing_entries,
    ):
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_find_existing_entries.return_value = [
            {
                "name": "ACC-PAY-IDEMP-0001",
                "paid_amount": 100,
                "received_amount": 100,
                "posting_date": "2026-03-30",
                "mode_of_payment": "Cash",
                "party": "Customer 727",
                "party_type": "Customer",
                "docstatus": 1,
                "posa_client_request_id": "pay-fixed-001",
            }
        ]

        result = self.processor.process_pos_payment(
            json.dumps(
                {
                    "client_request_id": "pay-fixed-001",
                    "customer": "Customer 727",
                    "company": "Test Company",
                    "currency": "USD",
                    "pos_profile_name": "Main POS",
                    "pos_opening_shift_name": "POS-OPEN-0001",
                    "selected_invoices": [],
                    "selected_payments": [],
                    "selected_mpesa_payments": [],
                    "payment_methods": [{"mode_of_payment": "Cash", "amount": 100}],
                    "total_selected_invoices": 0,
                    "total_selected_payments": 0,
                    "total_selected_mpesa_payments": 0,
                    "total_payment_methods": 100,
                    "pos_profile": {
                        "posa_use_pos_awesome_payments": 1,
                        "posa_allow_make_new_payments": 1,
                        "posa_allow_reconcile_payments": 0,
                        "posa_allow_mpesa_reconcile_payments": 0,
                        "cost_center": "Main - TC",
                    },
                }
            )
        )

        self.assertTrue(result["replayed"])
        self.assertEqual(result["new_payments_entry"][0]["name"], "ACC-PAY-IDEMP-0001")
        mock_create_payment_entry.assert_not_called()

    @patch("posawesome.posawesome.api.payment_processing.processor.find_payment_entries_by_client_request_id")
    @patch("posawesome.posawesome.api.payment_processing.processor.create_payment_entry")
    @patch("posawesome.posawesome.api.payment_processing.processor.frappe")
    def test_process_pos_payment_finishes_partial_replay_before_returning_cached_result(
        self,
        mock_frappe,
        mock_create_payment_entry,
        mock_find_existing_entries,
    ):
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.log_error = Mock()
        mock_frappe.msgprint = Mock()
        mock_find_existing_entries.return_value = [
            {
                "name": "ACC-PAY-IDEMP-0001",
                "paid_amount": 100,
                "received_amount": 100,
                "posting_date": "2026-03-30",
                "mode_of_payment": "Cash",
                "party": "Customer 727",
                "party_type": "Customer",
                "docstatus": 1,
                "posa_client_request_id": "pay-fixed-001",
            }
        ]
        mock_create_payment_entry.return_value = FakePaymentEntry(
            name="ACC-PAY-IDEMP-0002",
            paid_amount=50,
        )

        result = self.processor.process_pos_payment(
            json.dumps(
                {
                    "client_request_id": "pay-fixed-001",
                    "customer": "Customer 727",
                    "company": "Test Company",
                    "currency": "USD",
                    "pos_profile_name": "Main POS",
                    "pos_opening_shift_name": "POS-OPEN-0001",
                    "selected_invoices": [],
                    "selected_payments": [],
                    "selected_mpesa_payments": [],
                    "payment_methods": [
                        {"mode_of_payment": "Cash", "amount": 100},
                        {"mode_of_payment": "Card", "amount": 50},
                    ],
                    "total_selected_invoices": 0,
                    "total_selected_payments": 0,
                    "total_selected_mpesa_payments": 0,
                    "total_payment_methods": 150,
                    "pos_profile": {
                        "posa_use_pos_awesome_payments": 1,
                        "posa_allow_make_new_payments": 1,
                        "posa_allow_reconcile_payments": 0,
                        "posa_allow_mpesa_reconcile_payments": 0,
                        "cost_center": "Main - TC",
                    },
                }
            )
        )

        self.assertNotIn("replayed", result)
        mock_create_payment_entry.assert_called_once()
        self.assertEqual(
            mock_create_payment_entry.call_args.kwargs["mode_of_payment"],
            "Card",
        )
        self.assertEqual(
            mock_create_payment_entry.call_args.kwargs["amount"],
            50,
        )
        self.assertEqual(
            [entry.get("name") for entry in result["all_payments_entry"]],
            ["ACC-PAY-IDEMP-0001", "ACC-PAY-IDEMP-0002"],
        )

    @patch("posawesome.posawesome.api.payment_processing.processor.find_payment_entries_by_client_request_id")
    @patch("posawesome.posawesome.api.payment_processing.processor.create_payment_entry")
    @patch("posawesome.posawesome.api.payment_processing.processor.frappe")
    def test_process_pos_payment_rejects_retries_when_matching_draft_entries_exist(
        self,
        mock_frappe,
        mock_create_payment_entry,
        mock_find_existing_entries,
    ):
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.throw.side_effect = lambda message: (_ for _ in ()).throw(Exception(message))
        mock_find_existing_entries.return_value = [
            {
                "name": "ACC-PAY-IDEMP-DRAFT-0001",
                "paid_amount": 100,
                "received_amount": 100,
                "posting_date": "2026-03-30",
                "mode_of_payment": "Cash",
                "party": "Customer 727",
                "party_type": "Customer",
                "docstatus": 0,
                "posa_client_request_id": "pay-fixed-draft-001",
            }
        ]

        with self.assertRaisesRegex(Exception, "draft Payment Entry records pending review"):
            self.processor.process_pos_payment(
                json.dumps(
                    {
                        "client_request_id": "pay-fixed-draft-001",
                        "customer": "Customer 727",
                        "company": "Test Company",
                        "currency": "USD",
                        "pos_profile_name": "Main POS",
                        "pos_opening_shift_name": "POS-OPEN-0001",
                        "selected_invoices": [],
                        "selected_payments": [],
                        "selected_mpesa_payments": [],
                        "payment_methods": [{"mode_of_payment": "Cash", "amount": 100}],
                        "total_selected_invoices": 0,
                        "total_selected_payments": 0,
                        "total_selected_mpesa_payments": 0,
                        "total_payment_methods": 100,
                        "pos_profile": {
                            "posa_use_pos_awesome_payments": 1,
                            "posa_allow_make_new_payments": 1,
                            "posa_allow_reconcile_payments": 0,
                            "posa_allow_mpesa_reconcile_payments": 0,
                            "cost_center": "Main - TC",
                        },
                    }
                )
            )

        mock_create_payment_entry.assert_not_called()

    @patch("posawesome.posawesome.api.payment_processing.processor.find_payment_entries_by_client_request_id")
    @patch("posawesome.posawesome.api.payment_processing.processor.frappe")
    def test_process_pos_payment_keeps_first_time_reconciliation_validation_active(
        self,
        mock_frappe,
        mock_find_existing_entries,
    ):
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.log_error = Mock()
        mock_frappe.msgprint = Mock()
        mock_find_existing_entries.return_value = []
        mock_frappe.get_doc.side_effect = lambda doctype, name: AttrDict(
            {
                "doctype": doctype,
                "name": name,
                "unallocated_amount": 0,
                "paid_from": "Cash - TC",
                "cost_center": "Main - TC",
            }
        )

        result = self.processor.process_pos_payment(
            json.dumps(
                {
                    "client_request_id": "pay-first-pass-001",
                    "customer": "Customer 727",
                    "company": "Test Company",
                    "currency": "USD",
                    "pos_profile_name": "Main POS",
                    "pos_opening_shift_name": "POS-OPEN-0001",
                    "selected_invoices": [],
                    "selected_payments": [{"name": "ACC-PAY-0009", "voucher_type": "Payment Entry"}],
                    "selected_mpesa_payments": [],
                    "payment_methods": [],
                    "total_selected_invoices": 0,
                    "total_selected_payments": 1,
                    "total_selected_mpesa_payments": 0,
                    "total_payment_methods": 0,
                    "pos_profile": {
                        "posa_use_pos_awesome_payments": 1,
                        "posa_allow_make_new_payments": 0,
                        "posa_allow_reconcile_payments": 1,
                        "posa_allow_mpesa_reconcile_payments": 0,
                        "cost_center": "Main - TC",
                    },
                }
            )
        )

        self.assertIn(
            "Payment ACC-PAY-0009 is already fully allocated",
            result["errors"],
        )

    @patch("posawesome.posawesome.api.payment_processing.processor.find_payment_entries_by_client_request_id")
    @patch("posawesome.posawesome.api.payment_processing.processor.frappe")
    def test_process_pos_payment_replay_preserves_completed_reconciliation_summary(
        self,
        mock_frappe,
        mock_find_existing_entries,
    ):
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.log_error = Mock()
        mock_frappe.msgprint = Mock()
        mock_find_existing_entries.return_value = [
            {
                "name": "ACC-PAY-IDEMP-0001",
                "paid_amount": 100,
                "received_amount": 100,
                "posting_date": "2026-03-30",
                "mode_of_payment": "Cash",
                "party": "Customer 727",
                "party_type": "Customer",
                "docstatus": 1,
                "posa_client_request_id": "pay-fixed-005",
            }
        ]
        mock_frappe.get_doc.side_effect = lambda doctype, name: AttrDict(
            {
                "doctype": doctype,
                "name": name,
                "unallocated_amount": 0,
                "paid_amount": 60,
                "posting_date": "2026-03-30",
                "party": "Customer 727",
                "party_type": "Customer",
                "docstatus": 1,
            }
        )

        result = self.processor.process_pos_payment(
            json.dumps(
                {
                    "client_request_id": "pay-fixed-005",
                    "customer": "Customer 727",
                    "company": "Test Company",
                    "currency": "USD",
                    "pos_profile_name": "Main POS",
                    "pos_opening_shift_name": "POS-OPEN-0001",
                    "selected_invoices": [],
                    "selected_payments": [
                        {
                            "name": "ACC-PAY-RECON-0001",
                            "voucher_type": "Payment Entry",
                            "unallocated_amount": 60,
                        }
                    ],
                    "selected_mpesa_payments": [],
                    "payment_methods": [{"mode_of_payment": "Cash", "amount": 100}],
                    "total_selected_invoices": 0,
                    "total_selected_payments": 1,
                    "total_selected_mpesa_payments": 0,
                    "total_payment_methods": 100,
                    "pos_profile": {
                        "posa_use_pos_awesome_payments": 1,
                        "posa_allow_make_new_payments": 1,
                        "posa_allow_reconcile_payments": 1,
                        "posa_allow_mpesa_reconcile_payments": 0,
                        "cost_center": "Main - TC",
                    },
                }
            )
        )

        self.assertTrue(result["replayed"])
        self.assertEqual(
            result["reconciled_payments"],
            [{"payment_entry": "ACC-PAY-RECON-0001", "allocated_amount": 60}],
        )
        self.assertTrue(all(isinstance(entry, dict) for entry in result["all_payments_entry"]))

    @patch("posawesome.posawesome.api.payment_processing.processor.create_payment_entry")
    @patch("posawesome.posawesome.api.payment_processing.processor.frappe")
    def test_process_pos_payment_passes_client_request_id_to_new_payment_entries(
        self,
        mock_frappe,
        mock_create_payment_entry,
    ):
        fake_payment_entry = FakePaymentEntry(paid_amount=100)
        mock_create_payment_entry.return_value = fake_payment_entry
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.log_error = Mock()
        mock_frappe.msgprint = Mock()
        mock_frappe.get_list.return_value = []

        self.processor.process_pos_payment(
            json.dumps(
                {
                    "client_request_id": "pay-fixed-002",
                    "customer": "Customer 727",
                    "company": "Test Company",
                    "currency": "USD",
                    "pos_profile_name": "Main POS",
                    "pos_opening_shift_name": "POS-OPEN-0001",
                    "selected_invoices": [],
                    "selected_payments": [],
                    "selected_mpesa_payments": [],
                    "payment_methods": [{"mode_of_payment": "Cash", "amount": 100}],
                    "total_selected_invoices": 0,
                    "total_selected_payments": 0,
                    "total_selected_mpesa_payments": 0,
                    "total_payment_methods": 100,
                    "exchange_rate": None,
                    "pos_profile": {
                        "posa_use_pos_awesome_payments": 1,
                        "posa_allow_make_new_payments": 1,
                        "posa_allow_reconcile_payments": 0,
                        "posa_allow_mpesa_reconcile_payments": 0,
                        "cost_center": "Main - TC",
                    },
                }
            )
        )

        self.assertEqual(
            mock_create_payment_entry.call_args.kwargs["client_request_id"],
            "pay-fixed-002",
        )

    @patch("posawesome.posawesome.api.payment_processing.processor.get_account_currency")
    @patch("posawesome.posawesome.api.payment_processing.processor.create_payment_entry")
    @patch("posawesome.posawesome.api.payment_processing.processor.frappe")
    def test_process_pos_payment_sets_reference_exchange_gain_loss(
        self,
        mock_frappe,
        mock_create_payment_entry,
        mock_get_account_currency,
    ):
        fake_payment_entry = FakePaymentEntry(paid_amount=100)
        fake_payment_entry.received_amount = 100
        fake_payment_entry.paid_to_account_currency = "USD"
        fake_payment_entry.paid_from_account_currency = "USD"
        fake_payment_entry.company_currency = "USD"
        fake_payment_entry.party_account_currency = "USD"
        fake_payment_entry.target_exchange_rate = 1.5
        fake_payment_entry.source_exchange_rate = 1.5
        mock_create_payment_entry.return_value = fake_payment_entry
        mock_get_account_currency.return_value = "USD"
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.log_error = Mock()
        mock_frappe.msgprint = Mock()
        mock_frappe.get_cached_value.return_value = "USD"
        mock_frappe.db = types.SimpleNamespace(
            get_default=lambda key: 2,
            has_column=lambda doctype, fieldname: True,
            sql=lambda *args, **kwargs: [],
            get_value=lambda *args, **kwargs: None,
        )
        mock_frappe.get_cached_doc.return_value = types.SimpleNamespace(
            currency="USD",
            conversion_rate=1.2,
            rounded_total=100,
            grand_total=100,
            outstanding_amount=100,
        )

        result = self.processor.process_pos_payment(
            json.dumps(
                {
                    "customer": "Customer 727",
                    "company": "Test Company",
                    "currency": "USD",
                    "pos_profile_name": "Main POS",
                    "pos_opening_shift_name": "POS-OPEN-0001",
                    "selected_invoices": [
                        {
                            "name": "SINV-0001",
                            "outstanding_amount": 100,
                            "conversion_rate": 1.2,
                            "currency": "USD",
                        }
                    ],
                    "selected_payments": [],
                    "selected_mpesa_payments": [],
                    "payment_methods": [{"mode_of_payment": "Cash", "amount": 100}],
                    "total_selected_invoices": 100,
                    "total_selected_payments": 0,
                    "total_selected_mpesa_payments": 0,
                    "total_payment_methods": 100,
                    "exchange_rate": 1.5,
                    "pos_profile": {
                        "posa_use_pos_awesome_payments": 1,
                        "posa_allow_make_new_payments": 1,
                        "posa_allow_reconcile_payments": 1,
                        "posa_allow_mpesa_reconcile_payments": 0,
                        "cost_center": "Main - TC",
                    },
                }
            )
        )

        self.assertEqual(fake_payment_entry.references[0].exchange_gain_loss, 30)
        self.assertEqual(result["net_gain_loss"], 30)
        self.assertEqual(result["exchange_gain_loss_summary"][0]["amount"], 30)

    @patch("posawesome.posawesome.api.payment_processing.processor.create_payment_entry")
    @patch("posawesome.posawesome.api.payment_processing.processor.frappe")
    def test_process_pos_payment_skips_replay_lookup_when_custom_field_is_missing(
        self,
        mock_frappe,
        mock_create_payment_entry,
    ):
        fake_payment_entry = FakePaymentEntry(paid_amount=100)
        mock_create_payment_entry.return_value = fake_payment_entry
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.log_error = Mock()
        mock_frappe.msgprint = Mock()
        mock_frappe.db = types.SimpleNamespace(
            has_column=lambda doctype, fieldname: False,
            sql=lambda *args, **kwargs: [],
            get_value=lambda *args, **kwargs: None,
        )

        self.processor.process_pos_payment(
            json.dumps(
                {
                    "client_request_id": "pay-fixed-003",
                    "customer": "Customer 727",
                    "company": "Test Company",
                    "currency": "USD",
                    "pos_profile_name": "Main POS",
                    "pos_opening_shift_name": "POS-OPEN-0001",
                    "selected_invoices": [],
                    "selected_payments": [],
                    "selected_mpesa_payments": [],
                    "payment_methods": [{"mode_of_payment": "Cash", "amount": 100}],
                    "total_selected_invoices": 0,
                    "total_selected_payments": 0,
                    "total_selected_mpesa_payments": 0,
                    "total_payment_methods": 100,
                    "exchange_rate": None,
                    "pos_profile": {
                        "posa_use_pos_awesome_payments": 1,
                        "posa_allow_make_new_payments": 1,
                        "posa_allow_reconcile_payments": 0,
                        "posa_allow_mpesa_reconcile_payments": 0,
                        "cost_center": "Main - TC",
                    },
                }
            )
        )

        self.assertEqual(
            mock_create_payment_entry.call_args.kwargs["client_request_id"],
            "pay-fixed-003",
        )

    @patch("posawesome.posawesome.api.payment_processing.processor.create_payment_entry")
    @patch("posawesome.posawesome.api.payment_processing.processor.frappe")
    def test_process_pos_payment_does_not_query_missing_client_request_column(
        self,
        mock_frappe,
        mock_create_payment_entry,
    ):
        fake_payment_entry = FakePaymentEntry(paid_amount=100)
        mock_create_payment_entry.return_value = fake_payment_entry
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.log_error = Mock()
        mock_frappe.msgprint = Mock()
        mock_frappe.db = types.SimpleNamespace(
            has_column=lambda doctype, fieldname: False,
            sql=lambda *args, **kwargs: [],
            get_value=lambda *args, **kwargs: None,
        )
        mock_frappe.get_list.side_effect = AssertionError(
            "replay lookup should be skipped when the field is missing"
        )

        self.processor.process_pos_payment(
            json.dumps(
                {
                    "client_request_id": "pay-fixed-004",
                    "customer": "Customer 727",
                    "company": "Test Company",
                    "currency": "USD",
                    "pos_profile_name": "Main POS",
                    "pos_opening_shift_name": "POS-OPEN-0001",
                    "selected_invoices": [],
                    "selected_payments": [],
                    "selected_mpesa_payments": [],
                    "payment_methods": [{"mode_of_payment": "Cash", "amount": 100}],
                    "total_selected_invoices": 0,
                    "total_selected_payments": 0,
                    "total_selected_mpesa_payments": 0,
                    "total_payment_methods": 100,
                    "exchange_rate": None,
                    "pos_profile": {
                        "posa_use_pos_awesome_payments": 1,
                        "posa_allow_make_new_payments": 1,
                        "posa_allow_reconcile_payments": 0,
                        "posa_allow_mpesa_reconcile_payments": 0,
                        "cost_center": "Main - TC",
                    },
                }
            )
        )

        self.assertEqual(
            mock_create_payment_entry.call_args.kwargs["client_request_id"],
            "pay-fixed-004",
        )

    @patch("posawesome.posawesome.api.payment_processing.data.get_advance_payment_entries_for_regional")
    @patch("posawesome.posawesome.api.payment_processing.data.get_party_account")
    @patch("posawesome.posawesome.api.payment_processing.data.frappe")
    def test_get_unallocated_payments_excludes_pay_type_customer_entries(
        self,
        mock_frappe,
        mock_get_party_account,
        mock_regional_entries,
    ):
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.get_cached_value.return_value = "Customer 727"
        mock_get_party_account.return_value = "Debtors - TC"
        mock_regional_entries.return_value = []
        mock_frappe.db.sql.return_value = []
        mock_frappe.get_list.side_effect = [[], []]

        rows = self.data.get_unallocated_payments(
            customer="Customer 727",
            company="Test Company",
            currency="USD",
            include_all_currencies=True,
        )

        self.assertEqual(rows, [])
        self.assertGreaterEqual(mock_frappe.get_list.call_count, 1)
        payment_entry_calls = [
            call
            for call in mock_frappe.get_list.call_args_list
            if call.args and call.args[0] == "Payment Entry"
        ]
        self.assertGreaterEqual(len(payment_entry_calls), 1)
        for call in payment_entry_calls:
            self.assertEqual(call.kwargs["filters"]["payment_type"], "Receive")

    @patch(
        "posawesome.posawesome.api.payment_processing.data.get_erpnext_outstanding_invoices",
        side_effect=AssertionError("legacy outstanding helper should not be called"),
        create=True,
    )
    @patch("posawesome.posawesome.api.payment_processing.data.get_party_account")
    @patch("posawesome.posawesome.api.payment_processing.data.frappe")
    def test_get_outstanding_invoices_queries_only_open_sales_invoices(
        self,
        mock_frappe,
        mock_get_party_account,
        mock_legacy_helper,
    ):
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.get_cached_value.return_value = "Customer 727"
        mock_get_party_account.return_value = "Debtors - TC"

        def fake_get_list(doctype, filters=None, fields=None, order_by=None, **kwargs):
            self.assertEqual(doctype, "Sales Invoice")
            self.assertEqual(filters["customer"], "Customer 727")
            self.assertEqual(filters["company"], "Test Company")
            self.assertEqual(filters["docstatus"], 1)
            self.assertEqual(filters["outstanding_amount"], (">", 0))
            self.assertEqual(filters["currency"], "USD")
            self.assertEqual(filters["pos_profile"], "Main POS")
            self.assertIn("outstanding_amount", fields)
            self.assertEqual(order_by, "posting_date desc, name desc")
            return [
                AttrDict(
                    {
                        "name": "SINV-OPEN-0001",
                        "posting_date": "2026-03-12",
                        "due_date": "2026-03-15",
                        "outstanding_amount": 125,
                        "base_rounded_total": 125,
                        "grand_total": 125,
                        "currency": "USD",
                        "pos_profile": "Main POS",
                        "customer_name": "Customer 727",
                    }
                )
            ]

        mock_frappe.get_list.side_effect = fake_get_list

        rows = self.data.get_outstanding_invoices(
            customer="Customer 727",
            company="Test Company",
            currency="USD",
            pos_profile="Main POS",
            include_all_currencies=False,
        )

        self.assertEqual(
            [(row.get("voucher_type"), row.get("voucher_no")) for row in rows],
            [("Sales Invoice", "SINV-OPEN-0001")],
        )
        self.assertEqual(rows[0].get("outstanding_amount"), 125)
        self.assertEqual(rows[0].get("customer_name"), "Customer 727")
        mock_legacy_helper.assert_not_called()

    @patch("posawesome.posawesome.api.payment_processing.data.frappe")
    def test_get_outstanding_invoices_queries_purchase_invoices_for_supplier_mode(
        self,
        mock_frappe,
    ):
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.get_cached_value.return_value = "Supplier ABC"

        def fake_get_list(doctype, filters=None, fields=None, order_by=None, **kwargs):
            self.assertEqual(doctype, "Purchase Invoice")
            self.assertEqual(filters["supplier"], "SUPP-0001")
            self.assertEqual(filters["company"], "Test Company")
            self.assertEqual(filters["docstatus"], 1)
            self.assertEqual(filters["outstanding_amount"], (">", 0))
            self.assertEqual(filters["currency"], "USD")
            self.assertIn("outstanding_amount", fields)
            self.assertEqual(order_by, "posting_date desc, name desc")
            return [
                AttrDict(
                    {
                        "name": "PINV-OPEN-0001",
                        "posting_date": "2026-03-18",
                        "due_date": "2026-03-22",
                        "outstanding_amount": 340,
                        "rounded_total": 340,
                        "grand_total": 340,
                        "currency": "USD",
                        "supplier_name": "Supplier ABC",
                    }
                )
            ]

        mock_frappe.get_list.side_effect = fake_get_list

        rows = self.data.get_outstanding_invoices(
            customer="SUPP-0001",
            company="Test Company",
            currency="USD",
            include_all_currencies=False,
            party_type="Supplier",
        )

        self.assertEqual(
            [(row.get("voucher_type"), row.get("voucher_no")) for row in rows],
            [("Purchase Invoice", "PINV-OPEN-0001")],
        )
        self.assertEqual(rows[0].get("customer_name"), "Supplier ABC")
        self.assertEqual(rows[0].get("party_name"), "Supplier ABC")
        self.assertEqual(rows[0].get("party_type"), "Supplier")

    @patch("posawesome.posawesome.api.payment_processing.data.get_party_account")
    @patch("posawesome.posawesome.api.payment_processing.data.frappe")
    def test_get_unallocated_payments_queries_supplier_payments_in_supplier_mode(
        self,
        mock_frappe,
        mock_get_party_account,
    ):
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.get_cached_value.return_value = "Supplier ABC"
        mock_get_party_account.return_value = "Creditors - TC"

        def fake_get_list(doctype, filters=None, fields=None, order_by=None, **kwargs):
            self.assertEqual(doctype, "Payment Entry")
            self.assertEqual(filters["party"], "SUPP-0001")
            self.assertEqual(filters["company"], "Test Company")
            self.assertEqual(filters["party_type"], "Supplier")
            self.assertEqual(filters["payment_type"], "Pay")
            self.assertEqual(filters["paid_to_account_currency"], "USD")
            self.assertEqual(order_by, "posting_date asc")
            return [
                AttrDict(
                    {
                        "name": "ACC-PAY-0009",
                        "paid_amount": 150,
                        "customer_name": "Supplier ABC",
                        "received_amount": 150,
                        "posting_date": "2026-03-16",
                        "unallocated_amount": 150,
                        "mode_of_payment": "Bank",
                        "currency": "USD",
                        "account": "Creditors - TC",
                    }
                )
            ]

        mock_frappe.get_list.side_effect = fake_get_list

        rows = self.data.get_unallocated_payments(
            customer="SUPP-0001",
            company="Test Company",
            currency="USD",
            include_all_currencies=False,
            party_type="Supplier",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].get("voucher_type"), "Payment Entry")
        self.assertEqual(rows[0].get("customer_name"), "Supplier ABC")
        self.assertEqual(rows[0].get("party_name"), "Supplier ABC")
        self.assertEqual(rows[0].get("party_type"), "Supplier")

    @patch("posawesome.posawesome.api.payment_processing.reconciliation.reconcile_against_document")
    @patch("posawesome.posawesome.api.payment_processing.reconciliation.frappe")
    @patch("posawesome.posawesome.api.payment_processing.reconciliation.get_unallocated_payments")
    @patch("posawesome.posawesome.api.payment_processing.reconciliation.get_outstanding_invoices")
    def test_auto_reconcile_customer_invoices_uses_supplier_semantics_when_requested(
        self,
        mock_get_outstanding_invoices,
        mock_get_unallocated_payments,
        mock_frappe,
        mock_reconcile_against_document,
    ):
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.get_doc.return_value = AttrDict(
            {
                "paid_to": "Creditors - TC",
                "cost_center": "Main - TC",
                "get": lambda key, default=None: {
                    "unallocated_amount": 200,
                }.get(key, default),
            }
        )

        mock_get_outstanding_invoices.return_value = [
            AttrDict(
                {
                    "voucher_no": "PINV-0001",
                    "voucher_type": "Purchase Invoice",
                    "posting_date": "2026-03-10",
                    "outstanding_amount": 200,
                }
            )
        ]
        mock_get_unallocated_payments.return_value = [
            AttrDict(
                {
                    "name": "ACC-PAY-0010",
                    "voucher_type": "Payment Entry",
                    "posting_date": "2026-03-11",
                    "unallocated_amount": 200,
                    "account": "Creditors - TC",
                }
            )
        ]

        result = self.reconciliation.auto_reconcile_customer_invoices(
            customer="SUPP-0001",
            company="Test Company",
            currency="USD",
            party_type="Supplier",
        )

        self.assertEqual(result["total_allocated"], 200)
        mock_reconcile_against_document.assert_called_once()
        reconcile_row = mock_reconcile_against_document.call_args.args[0][0]
        self.assertEqual(reconcile_row["party_type"], "Supplier")
        self.assertEqual(reconcile_row["dr_or_cr"], "debit_in_account_currency")
        self.assertEqual(reconcile_row["against_voucher_type"], "Purchase Invoice")

    def test_payment_currency_normalization_uses_for_selling_and_matches_three_currency_parity(self):
        class Row(dict):
            __getattr__ = dict.get
            __setattr__ = dict.__setitem__

            def precision(self, _fieldname):
                return 2

        captured_purposes = []

        def fake_get_latest_rate(from_currency, to_currency, **kwargs):
            purpose = kwargs.get("purpose")
            captured_purposes.append((from_currency, to_currency, purpose))
            if (from_currency, to_currency) == ("USD", "UGX"):
                # Newer buying-only rate is 4050; selling rate is 3700.
                return (3700.0, "2026-11-27") if purpose == "for_selling" else (4050.0, "2026-11-28")
            if (from_currency, to_currency) == ("EUR", "USD"):
                return (1.1, "2026-11-28") if purpose == "for_selling" else (1.25, "2026-11-28")
            if (from_currency, to_currency) == ("EUR", "UGX"):
                return (4200.0, "2026-11-28") if purpose == "for_selling" else (4500.0, "2026-11-28")
            return (1.0, "2026-11-28")

        fake_invoice_utils = types.ModuleType("posawesome.posawesome.api.invoice_processing.utils")
        fake_invoice_utils.get_latest_rate = fake_get_latest_rate

        profile = Row(
            name="POS-UGX-MC",
            posa_enable_multi_currency_payments=1,
            posa_enable_multi_currency_change=1,
            posa_allow_manual_payment_exchange_rate=0,
            posa_allowed_currencies=[
                Row(currency="UGX", allow_for_payments=1, allow_for_change=1),
                Row(currency="USD", allow_for_payments=1, allow_for_change=1),
                Row(currency="EUR", allow_for_payments=1, allow_for_change=1),
            ],
        )

        with (
            patch.dict(
                sys.modules,
                {"posawesome.posawesome.api.invoice_processing.utils": fake_invoice_utils},
            ),
            patch.object(self.creation.frappe, "get_cached_doc", return_value=profile),
            patch.object(
                self.creation.frappe,
                "get_cached_value",
                side_effect=lambda doctype, _name, _field: "UGX" if doctype in {"Company", "Account"} else None,
            ),
            patch.object(
                self.creation.frappe,
                "format_value",
                side_effect=lambda value, *_args: str(value),
                create=True,
            ),
        ):
            sys.modules.pop("posawesome.posawesome.api.payment_currency", None)
            from posawesome.posawesome.api.payment_currency import (
                normalize_change_returns,
                normalize_invoice_payment_currencies,
            )

            # Case 1 (P1-3): USD 10 tender + USD 1 change on UGX invoice must use for_selling (3,700), not newer buying-only (4,050).
            ugx_invoice = Row(
                company="Uganda Co",
                currency="UGX",
                conversion_rate=1.0,
                posting_date="2026-11-28",
                pos_profile=profile.name,
                rounded_total=33300.0,
                grand_total=33300.0,
                payments=[
                    Row(
                        type="Cash",
                        account="Cash - UG",
                        posa_payment_currency="USD",
                        posa_original_amount=10.0,
                        amount=0.0,
                        base_amount=0.0,
                    )
                ],
                posa_change_returns=[
                    Row(
                        currency="USD",
                        original_amount=1.0,
                    )
                ],
            )
            normalize_invoice_payment_currencies(ugx_invoice, profile_doc=profile)
            normalize_change_returns(ugx_invoice, profile_doc=profile)

            self.assertEqual(ugx_invoice.payments[0].amount, 37000.0)
            self.assertEqual(ugx_invoice.payments[0].base_amount, 37000.0)
            self.assertEqual(ugx_invoice.posa_change_returns[0].invoice_amount, 3700.0)
            self.assertEqual(ugx_invoice.posa_change_returns[0].base_amount, 3700.0)

            # Case 2 (P1-4): Three-currency EUR 10 tender on USD invoice (conversion_rate=3700) in UGX company.
            usd_invoice = Row(
                company="Uganda Co",
                currency="USD",
                conversion_rate=3700.0,
                posting_date="2026-11-28",
                pos_profile=profile.name,
                rounded_total=11.0,
                grand_total=11.0,
                payments=[
                    Row(
                        type="Cash",
                        account="Cash - UG",
                        posa_payment_currency="EUR",
                        posa_original_amount=10.0,
                        amount=0.0,
                        base_amount=0.0,
                    )
                ],
            )
            normalize_invoice_payment_currencies(usd_invoice, profile_doc=profile)
            self.assertAlmostEqual(usd_invoice.payments[0].amount, 11.0, places=6)
            self.assertAlmostEqual(usd_invoice.payments[0].base_amount, 40700.0, places=6)
            self.assertAlmostEqual(usd_invoice.payments[0].posa_company_exchange_rate, 4070.0, places=6)

        self.assertTrue(captured_purposes)
        for _from_c, _to_c, purpose in captured_purposes:
            self.assertEqual(purpose, "for_selling")

    @patch("posawesome.posawesome.api.payment_processing.data.get_exchange_rate")
    @patch("posawesome.posawesome.api.payment_processing.data.frappe")
    def test_get_outstanding_invoices_multiplies_third_currency_forward_rate(
        self,
        mock_frappe,
        mock_get_exchange_rate,
    ):
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.get_precision.return_value = 2

        def fake_get_cached_value(doctype, name, fieldname):
            if doctype == "Company" and fieldname == "default_currency":
                return "UGX"
            if doctype == "Customer" and fieldname == "customer_name":
                return "Euro Debtor"
            return None

        mock_frappe.get_cached_value.side_effect = fake_get_cached_value
        mock_frappe.get_list.return_value = [
            AttrDict(
                {
                    "name": "SINV-3CCY-0001",
                    "posting_date": "2026-11-28",
                    "due_date": "2026-12-05",
                    "outstanding_amount": 10.0,
                    "conversion_rate": 3700.0,
                    "rounded_total": 11.0,
                    "grand_total": 11.0,
                    "currency": "USD",
                    "party_account_currency": "EUR",
                    "pos_profile": "POS-USD",
                    "customer_name": "Euro Debtor",
                }
            )
        ]
        # Forward rate: 1 EUR = 1.1 USD -> EUR 10 outstanding = USD 11.00 (not 10 / 1.1 = 9.09).
        mock_get_exchange_rate.return_value = 1.1

        rows = self.data.get_outstanding_invoices(
            customer="CUST-EUR",
            company="Uganda Co",
            currency="USD",
            pos_profile="POS-USD",
            include_all_currencies=True,
        )

        self.assertEqual(len(rows), 1)
        mock_get_exchange_rate.assert_called_once_with("EUR", "USD", "2026-11-28", "for_selling")
        self.assertAlmostEqual(rows[0]["outstanding_amount_in_invoice_currency"], 11.0, places=2)

    @patch("posawesome.posawesome.api.payment_processing.data.frappe")
    def test_get_outstanding_invoices_returns_company_currency_on_each_row(
        self,
        mock_frappe,
    ):
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.get_precision.return_value = 2

        def fake_get_cached_value(doctype, name, fieldname):
            if doctype == "Company" and fieldname == "default_currency":
                return "UGX"
            if doctype == "Customer" and fieldname == "customer_name":
                return "USD Customer"
            return None

        mock_frappe.get_cached_value.side_effect = fake_get_cached_value
        mock_frappe.get_list.return_value = [
            AttrDict(
                {
                    "name": "SINV-USD-CO-UGX",
                    "posting_date": "2026-11-28",
                    "due_date": "2026-12-05",
                    "outstanding_amount": 10.0,
                    "conversion_rate": 3700.0,
                    "rounded_total": 10.0,
                    "grand_total": 10.0,
                    "currency": "USD",
                    "party_account_currency": "USD",
                    "pos_profile": "POS-USD",
                    "customer_name": "USD Customer",
                }
            )
        ]

        rows = self.data.get_outstanding_invoices(
            customer="CUST-USD",
            company="Uganda Co",
            currency="USD",
            pos_profile="POS-USD",
            include_all_currencies=False,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].get("company_currency"), "UGX")

    @patch("posawesome.posawesome.api.payment_processing.data.get_exchange_rate")
    @patch("posawesome.posawesome.api.payment_processing.data.frappe")
    def test_get_outstanding_invoices_refuses_unrelated_conversion_rate_on_missing_third_currency_quote(
        self,
        mock_frappe,
        mock_get_exchange_rate,
    ):
        mock_frappe._dict.side_effect = lambda value: AttrDict(value)
        mock_frappe.get_precision.return_value = 2

        def fake_get_cached_value(doctype, name, fieldname):
            if doctype == "Company" and fieldname == "default_currency":
                return "UGX"
            if doctype == "Customer" and fieldname == "customer_name":
                return "Euro Debtor"
            return None

        mock_frappe.get_cached_value.side_effect = fake_get_cached_value
        mock_frappe.get_list.return_value = [
            AttrDict(
                {
                    "name": "SINV-3CCY-NOQUOTE",
                    "posting_date": "2026-11-28",
                    "due_date": "2026-12-05",
                    "outstanding_amount": 10.0,
                    "conversion_rate": 3700.0,
                    "rounded_total": 11.0,
                    "grand_total": 11.0,
                    "currency": "USD",
                    "party_account_currency": "EUR",
                    "pos_profile": "POS-USD",
                    "customer_name": "Euro Debtor",
                }
            )
        ]
        # Missing EUR->USD selling quote must NOT divide 10 by unrelated USD->UGX rate 3700.
        mock_get_exchange_rate.return_value = 0

        rows = self.data.get_outstanding_invoices(
            customer="CUST-EUR",
            company="Uganda Co",
            currency="USD",
            pos_profile="POS-USD",
            include_all_currencies=True,
        )

        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].get("outstanding_amount_in_invoice_currency"))
        self.assertTrue(rows[0].get("outstanding_conversion_unavailable"))
        self.assertEqual(rows[0].get("outstanding_amount"), 10.0)
        self.assertEqual(rows[0].get("party_account_currency"), "EUR")


if __name__ == "__main__":
    unittest.main()
