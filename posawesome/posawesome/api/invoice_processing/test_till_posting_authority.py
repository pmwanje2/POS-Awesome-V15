"""Till posting authority in Sales Invoice mode.

SAP Customer Checkout model: a receipt posts under the till's technical
authority (an authorized POS Profile plus the cashier's own open POS Opening
Shift). Cashiers keep read/print on invoices and never need write. Reversing
a submitted receipt is a separate right: the native cancel permission.
"""

import importlib.util
import json
import pathlib
import sys
import types
import unittest
from unittest.mock import Mock

HERE = pathlib.Path(__file__).resolve().parent
API_DIR = HERE.parent
_ORIGINAL_MODULES = dict(sys.modules)
_MANAGED_PREFIXES = (
    "frappe",
    "erpnext.accounts.doctype.sales_invoice.sales_invoice",
    "posawesome",
    "_posa_till_authority_",
)


def tearDownModule():
    for name in list(sys.modules):
        if name.startswith(_MANAGED_PREFIXES) and name not in _ORIGINAL_MODULES:
            sys.modules.pop(name, None)
    for name, module in _ORIGINAL_MODULES.items():
        if name.startswith(_MANAGED_PREFIXES):
            sys.modules[name] = module


def _load_harness(alias, path):
    """Reuse an existing Frappe-stub test loader without registering its tests."""

    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


creation_harness = _load_harness(
    "_posa_till_authority_creation_harness", HERE / "test_creation.py"
)
edits_harness = _load_harness(
    "_posa_till_authority_edits_harness",
    API_DIR / "test_submitted_invoice_shift_security.py",
)
FakeDoc = creation_harness.FakeDoc

SESSION_USER = "test@example.com"
SHIFT_NAME = "POS-OPEN-0001"


class PermissionRecordingInvoice(FakeDoc):
    """Invoice double whose native permission check mirrors a DocPerm grant."""

    def __init__(self, granted=("read", "print"), **kwargs):
        super().__init__(**kwargs)
        object.__setattr__(self, "_granted", set(granted))
        object.__setattr__(self, "permission_checks", [])
        object.__setattr__(self, "save_count", 0)
        object.__setattr__(self, "submit_count", 0)

    def check_permission(self, permission_type):
        self.permission_checks.append(permission_type)
        if permission_type not in self._granted:
            raise PermissionError(f"No {permission_type} permission on {self.get('name')}")

    def is_new(self):
        return self.save_count == 0 and not self.get("_persisted")

    def save(self):
        object.__setattr__(self, "save_count", self.save_count + 1)
        return self

    def submit(self):
        object.__setattr__(self, "submit_count", self.submit_count + 1)
        self.docstatus = 1


def _invoice_fields(**overrides):
    base = {
        "doctype": "Sales Invoice",
        "name": None,
        "pos_profile": "Main POS",
        "company": "Test Company",
        "currency": "USD",
        "posting_date": "2026-10-26",
        "set_posting_time": 0,
        "customer": "CUST-0001",
        "customer_name": "Customer 1",
        "cost_center": None,
        "is_return": 0,
        "return_against": None,
        "items": [],
        "payments": [],
        "taxes": [],
        "flags": types.SimpleNamespace(ignore_pricing_rule=False, ignore_permissions=False),
        "paid_amount": 0,
        "base_paid_amount": 0,
        "conversion_rate": 1,
        "plc_conversion_rate": 1,
        "price_list_currency": "USD",
        "total": 0,
        "discount_amount": 0,
        "net_total": 0,
        "grand_total": 0,
        "rounded_total": 0,
        "write_off_amount": 0,
        "docstatus": 0,
        "redeem_loyalty_points": 0,
        "loyalty_program": None,
        "loyalty_redemption_account": None,
        "loyalty_redemption_cost_center": None,
        "remarks": "",
        "posa_pos_opening_shift": SHIFT_NAME,
    }
    base.update(overrides)
    return base


def _shift(**overrides):
    base = {
        "name": SHIFT_NAME,
        "pos_profile": "Main POS",
        "company": "Test Company",
        "user": SESSION_USER,
        "docstatus": 1,
        "status": "Open",
        "pos_closing_shift": None,
    }
    base.update(overrides)
    return FakeDoc(**base)


class TillAuthorityHarness(unittest.TestCase):
    """Shared Frappe-stub harness; holds no tests of its own."""

    @classmethod
    def setUpClass(cls):
        cls.frappe, cls.enqueue_calls = creation_harness._install_framework_stubs()
        creation_harness._install_dependency_stubs()
        creation_harness._install_package_stubs()
        sys.modules.pop("posawesome.posawesome.api.idempotency", None)
        cls.creation = creation_harness._load_module()

    def setUp(self):
        self.enqueue_calls.clear()
        self.invoices = {}
        self.shifts = {SHIFT_NAME: _shift()}
        self.created = []
        self.new_invoice_grants = ("read", "print")
        self.realtime_events = []
        self.frappe.session = types.SimpleNamespace(user=SESSION_USER)
        self.frappe.flags = types.SimpleNamespace(ignore_account_permission=False)
        self.frappe.db.has_column = lambda doctype, fieldname: True
        self.frappe.db.exists = self._exists
        self.frappe.db.get_value = lambda *args, **kwargs: None
        self.frappe.db.set_value = lambda *args, **kwargs: None
        self.frappe.get_value = lambda *args, **kwargs: 0
        self.frappe.get_cached_value = lambda *args, **kwargs: None
        self.frappe.get_doc = self._get_doc
        self.frappe.publish_realtime = lambda event, payload=None, **kwargs: self.realtime_events.append(
            (event, payload)
        )
        self._patch("get_active_terminal_cashier", lambda *_args, **_kwargs: "cashier@example.com")
        self._patch("_process_post_submit_payments", lambda *_args, **_kwargs: None)
        self._patch("_apply_invoice_gift_card_settlement", lambda *_args, **_kwargs: None)

    def _patch(self, attribute, value):
        original = getattr(self.creation, attribute)
        setattr(self.creation, attribute, value)
        self.addCleanup(setattr, self.creation, attribute, original)

    def _exists(self, doctype, name=None, *args, **kwargs):
        if doctype in {"Sales Invoice", "POS Invoice"}:
            return name in self.invoices
        return True

    def _get_doc(self, *args, **kwargs):
        if len(args) == 1 and isinstance(args[0], dict):
            payload = dict(args[0])
            payload.pop("name", None)
            name = f"ACC-SINV-TILL-{len(self.created) + 1:04d}"
            invoice = PermissionRecordingInvoice(
                granted=self.new_invoice_grants,
                **_invoice_fields(**payload, name=name),
            )
            self.created.append(invoice)
            self.invoices[name] = invoice
            return invoice
        doctype, name = args[0], args[1]
        if doctype == "POS Opening Shift":
            if name not in self.shifts:
                raise Exception(f"POS Opening Shift {name} not found")
            return self.shifts[name]
        if doctype in {"Sales Invoice", "POS Invoice"} and name in self.invoices:
            return self.invoices[name]
        raise AssertionError(f"unexpected get_doc call: {args}")

    def _parked_draft(self, name="ACC-SINV-PARKED-0001", granted=("read", "print"), **overrides):
        draft = PermissionRecordingInvoice(
            granted=granted,
            **_invoice_fields(name=name, _persisted=True, **overrides),
        )
        self.invoices[name] = draft
        return draft

    def _sale_payload(self, **overrides):
        payload = {
            "doctype": "Sales Invoice",
            "pos_profile": "Main POS",
            "company": "Test Company",
            "currency": "USD",
            "customer": "CUST-0001",
            "items": [],
            "payments": [],
            "posa_pos_opening_shift": SHIFT_NAME,
        }
        payload.update(overrides)
        return payload

    def _all_saved(self):
        return [doc for doc in self.invoices.values() if doc.save_count or doc.submit_count]


class TestTillPostingAuthority(TillAuthorityHarness):
    def test_read_only_cashier_sale_posts_under_till_authority(self):
        result = self.creation.submit_invoice(
            json.dumps(self._sale_payload()),
            json.dumps({}),
            submit_in_background=0,
        )

        self.assertEqual(result["docstatus"], 1)
        self.assertEqual(len(self.created), 1)
        sale = self.created[0]
        self.assertEqual(sale.submit_count, 1)
        self.assertEqual(sale.docstatus, 1)
        self.assertNotIn("write", sale.permission_checks)
        self.assertNotIn("submit", sale.permission_checks)
        self.assertFalse(getattr(self.frappe.flags, "ignore_permissions", False))

    def test_read_only_cashier_updates_own_parked_draft_without_write(self):
        draft = self._parked_draft(customer="CUST-0001")

        result = self.creation.update_invoice(
            json.dumps(
                self._sale_payload(name=draft.name, customer="CUST-0002")
            )
        )

        self.assertEqual(result["name"], draft.name)
        self.assertEqual(draft.customer, "CUST-0002")
        self.assertEqual(draft.save_count, 1)
        self.assertEqual(set(draft.permission_checks), {"read"})
        self.assertFalse(getattr(self.frappe.flags, "ignore_permissions", False))

    def test_read_only_cashier_submits_own_parked_draft_without_write(self):
        draft = self._parked_draft()

        result = self.creation.submit_invoice(
            json.dumps(self._sale_payload(name=draft.name)),
            json.dumps({}),
            submit_in_background=0,
        )

        self.assertEqual(result["name"], draft.name)
        self.assertEqual(result["docstatus"], 1)
        self.assertEqual(draft.submit_count, 1)
        self.assertEqual(set(draft.permission_checks), {"read"})
        self.assertFalse(getattr(self.frappe.flags, "ignore_permissions", False))

    def test_unassigned_profile_is_refused_with_nothing_saved(self):
        draft = self._parked_draft(granted=("read", "write", "submit"))
        self._patch(
            "get_authorized_pos_profile",
            Mock(side_effect=PermissionError("POS Profile is not assigned to you")),
        )

        for call in (
            lambda: self.creation.submit_invoice(
                json.dumps(self._sale_payload()), json.dumps({}), submit_in_background=0
            ),
            lambda: self.creation.update_invoice(json.dumps(self._sale_payload(name=draft.name))),
        ):
            with self.assertRaisesRegex(PermissionError, "not assigned"):
                call()

        self.assertEqual(self.created, [])
        self.assertEqual(self._all_saved(), [])

    def test_missing_closed_or_other_users_shift_is_refused(self):
        cases = {
            "missing": (None, "POS Opening Shift is required"),
            "unknown": ("POS-OPEN-GONE", "not found"),
            "closed": (
                _shift(name="POS-OPEN-CLOSED", status="Closed", pos_closing_shift="POS-CLOSE-1"),
                "is not open",
            ),
            "other user": (
                _shift(name="POS-OPEN-OTHER-USER", user="other@example.com"),
                "does not belong to the current user",
            ),
        }
        for label, (shift, message) in cases.items():
            with self.subTest(shift=label):
                self.created.clear()
                self.invoices.clear()
                shift_name = None
                if isinstance(shift, FakeDoc):
                    self.shifts[shift.name] = shift
                    shift_name = shift.name
                elif shift:
                    shift_name = shift
                draft = self._parked_draft(
                    granted=("read", "write", "submit"),
                    posa_pos_opening_shift=shift_name,
                )
                for payload in (
                    self._sale_payload(posa_pos_opening_shift=shift_name),
                    self._sale_payload(name=draft.name, posa_pos_opening_shift=shift_name),
                ):
                    with self.assertRaisesRegex(Exception, message):
                        self.creation.submit_invoice(
                            json.dumps(payload), json.dumps({}), submit_in_background=0
                        )
                    with self.assertRaisesRegex(Exception, message):
                        self.creation.update_invoice(json.dumps(payload))
                self.assertEqual(self.created, [])
                self.assertEqual(self._all_saved(), [])

    def test_another_shifts_draft_is_refused_even_with_write(self):
        draft = self._parked_draft(
            granted=("read", "write", "submit", "cancel"),
            posa_pos_opening_shift="POS-OPEN-EARLIER",
            customer="CUST-0001",
        )

        with self.assertRaisesRegex(Exception, "another POS Opening Shift"):
            self.creation.update_invoice(
                json.dumps(self._sale_payload(name=draft.name, customer="CUST-0002"))
            )
        with self.assertRaisesRegex(Exception, "another POS Opening Shift"):
            self.creation.submit_invoice(
                json.dumps(self._sale_payload(name=draft.name, customer="CUST-0002")),
                json.dumps({}),
                submit_in_background=0,
            )

        self.assertEqual(draft.customer, "CUST-0001")
        self.assertEqual(draft.save_count, 0)
        self.assertEqual(draft.submit_count, 0)
        self.assertEqual(self.created, [])

    def test_till_draft_authority_requires_shift_and_draft(self):
        draft = self._parked_draft()
        submitted = self._parked_draft(name="ACC-SINV-SUBMITTED-0001", docstatus=1)
        assert_authority = self.creation._assert_till_draft_authority

        with self.assertRaisesRegex(Exception, "POS Opening Shift is required"):
            assert_authority(
                draft, pos_profile="Main POS", company="Test Company", opening_shift_doc=None
            )
        with self.assertRaisesRegex(Exception, "Only a draft"):
            assert_authority(
                submitted,
                pos_profile="Main POS",
                company="Test Company",
                opening_shift_doc=self.shifts[SHIFT_NAME],
            )
        with self.assertRaisesRegex(Exception, "not available for the selected POS Profile"):
            assert_authority(
                draft,
                pos_profile="Other POS",
                company="Test Company",
                opening_shift_doc=self.shifts[SHIFT_NAME],
            )
        self.assertIs(
            assert_authority(
                draft,
                pos_profile="Main POS",
                company="Test Company",
                opening_shift_doc=self.shifts[SHIFT_NAME],
            ),
            draft,
        )
        self.assertEqual(set(draft.permission_checks), {"read"})


SERVER_OWNED_CLIENT_VALUES = {
    "is_consolidated": 1,
    "consolidated_invoice": "ACC-SINV-CONSOLIDATED-0001",
    "pos_closing_entry": "POS-CLOSE-0001",
    "is_created_using_pos": 1,
    "docstatus": 1,
    "status": "Paid",
    "owner": "manager@example.com",
    "creation": "2026-01-01 00:00:00",
    "modified_by": "manager@example.com",
    "amended_from": "ACC-SINV-OLD-0001",
    "posa_is_printed": 1,
    "posa_below_cost_override_by": "supervisor@example.com",
    "posa_below_cost_override_details": "[]",
    "posa_redeemed_customer_credit": 500,
    "posa_remaining_customer_credit_balance": 500,
    "posa_return_valid_upto": "2099-12-31",
}


class TestTillPayloadCannotSetServerOwnedFields(TillAuthorityHarness):
    """P1a: a till payload never sets fields the server or ERPNext owns.

    JB's Sales Invoice controls (JB-PRC-001, the staff discount cap, JBRC-003,
    JB-RET-001) skip consolidated invoices; ERPNext sets is_consolidated only
    in POS Invoice Merge Log. A till sale must never arrive pre-marked.
    """

    def assert_server_owned_fields_not_taken_from_client(self, doc, server_sets=()):
        for fieldname, client_value in SERVER_OWNED_CLIENT_VALUES.items():
            if fieldname == "docstatus" or fieldname in server_sets:
                continue
            with self.subTest(field=fieldname):
                self.assertNotEqual(doc.get(fieldname), client_value)

    def test_new_till_draft_strips_server_owned_fields(self):
        result = self.creation.update_invoice(
            json.dumps(self._sale_payload(**SERVER_OWNED_CLIENT_VALUES))
        )

        sale = self.invoices[result["name"]]
        self.assertEqual(sale.save_count, 1)
        self.assertEqual(sale.docstatus, 0)
        self.assert_server_owned_fields_not_taken_from_client(sale)

    def test_submitted_till_sale_cannot_be_marked_consolidated(self):
        result = self.creation.submit_invoice(
            json.dumps(self._sale_payload(**SERVER_OWNED_CLIENT_VALUES)),
            json.dumps({}),
            submit_in_background=0,
        )

        sale = self.invoices[result["name"]]
        self.assertEqual(sale.submit_count, 1)
        self.assertFalse(sale.get("is_consolidated"))
        # Submission itself marks the receipt printed.
        self.assert_server_owned_fields_not_taken_from_client(sale, server_sets={"posa_is_printed"})

    def test_parked_draft_update_and_submit_strip_server_owned_fields(self):
        draft = self._parked_draft()

        self.creation.update_invoice(
            json.dumps(self._sale_payload(name=draft.name, **SERVER_OWNED_CLIENT_VALUES))
        )
        self.assert_server_owned_fields_not_taken_from_client(draft)

        self.creation.submit_invoice(
            json.dumps(self._sale_payload(name=draft.name, **SERVER_OWNED_CLIENT_VALUES)),
            json.dumps({}),
            submit_in_background=0,
        )
        self.assertEqual(draft.submit_count, 1)
        self.assert_server_owned_fields_not_taken_from_client(draft, server_sets={"posa_is_printed"})

    def test_stored_consolidated_draft_is_refused_at_the_till(self):
        draft = self._parked_draft(is_consolidated=1)

        with self.assertRaisesRegex(Exception, "consolidated invoice cannot be changed at the till"):
            self.creation.submit_invoice(
                json.dumps(self._sale_payload(name=draft.name)),
                json.dumps({}),
                submit_in_background=0,
            )
        with self.assertRaisesRegex(Exception, "consolidated invoice cannot be changed at the till"):
            self.creation.update_invoice(json.dumps(self._sale_payload(name=draft.name)))

        self.assertEqual(draft.save_count, 0)
        self.assertEqual(draft.submit_count, 0)

    def test_merge_item_markers_are_stripped_from_sales_invoice_rows(self):
        payload = self._sale_payload(
            items=[
                {
                    "item_code": "ITEM-1",
                    "qty": 1,
                    "pos_invoice": "POS-INV-0001",
                    "pos_invoice_item": "row-1",
                    "sales_invoice_item": "si-row-1",
                }
            ]
        )

        self.creation._strip_server_owned_invoice_fields(payload, doctype="Sales Invoice")

        self.assertEqual(
            payload["items"],
            [{"item_code": "ITEM-1", "qty": 1, "sales_invoice_item": "si-row-1"}],
        )

    def test_return_against_is_dropped_from_a_sale(self):
        result = self.creation.update_invoice(
            json.dumps(self._sale_payload(is_return=0, return_against="ACC-SINV-OTHER-0001"))
        )

        self.assertFalse(self.invoices[result["name"]].get("return_against"))

    def _stub_return_originals(self, originals):
        returns_name = "posawesome.posawesome.api.invoice_processing.returns"
        returns_stub = types.ModuleType(returns_name)
        returns_stub.validate_return_items = lambda *_args, **_kwargs: {"valid": True}
        previous_returns = sys.modules.get(returns_name)
        sys.modules[returns_name] = returns_stub
        self.addCleanup(
            lambda: sys.modules.__setitem__(returns_name, previous_returns)
            if previous_returns
            else sys.modules.pop(returns_name, None)
        )

        def get_value(doctype, name, fieldname=None, *args, **kwargs):
            if doctype in {"Sales Invoice", "POS Invoice"} and isinstance(fieldname, (list, tuple)):
                row = originals.get(name)
                return types.SimpleNamespace(**row) if row else None
            return None

        self.frappe.db.get_value = get_value

    def test_sale_drops_return_against_when_client_omits_is_return(self):
        """The effective document decides: an omitted flag leaves a sale a sale."""

        def fresh_update():
            result = self.creation.update_invoice(
                json.dumps(self._sale_payload(return_against="ACC-SINV-OTHER-0001"))
            )
            return self.invoices[result["name"]]

        def fresh_submit():
            result = self.creation.submit_invoice(
                json.dumps(self._sale_payload(return_against="ACC-SINV-OTHER-0001")),
                json.dumps({}),
                submit_in_background=0,
            )
            return self.invoices[result["name"]]

        def parked_update():
            draft = self._parked_draft(name="ACC-SINV-PARKED-0011", is_pos=0)
            self.creation.update_invoice(
                json.dumps(
                    self._sale_payload(
                        name=draft.name, is_pos=0, return_against="ACC-SINV-OTHER-0001"
                    )
                )
            )
            return draft

        def parked_submit():
            draft = self._parked_draft(name="ACC-SINV-PARKED-0012", is_pos=0)
            result = self.creation.submit_invoice(
                json.dumps(
                    self._sale_payload(
                        name=draft.name, is_pos=0, return_against="ACC-SINV-OTHER-0001"
                    )
                ),
                json.dumps({}),
                submit_in_background=0,
            )
            self.assertEqual(result["docstatus"], 1)
            return draft

        for label, run in {
            "fresh sale update": fresh_update,
            "fresh sale submit": fresh_submit,
            "parked sale update": parked_update,
            "parked sale submit": parked_submit,
        }.items():
            with self.subTest(path=label):
                sale = run()
                self.assertFalse(int(sale.get("is_return") or 0))
                self.assertFalse(sale.get("return_against"))

    def test_partial_update_of_a_stored_return_keeps_its_return_against(self):
        self._stub_return_originals(
            {"ACC-SINV-ORIG-0001": {"docstatus": 1, "is_return": 0, "company": "Test Company"}}
        )
        stored = self._parked_draft(
            name="ACC-SINV-RET-DRAFT-0001",
            is_return=1,
            return_against="ACC-SINV-ORIG-0001",
        )

        self.creation.update_invoice(
            json.dumps(self._sale_payload(name=stored.name, customer="CUST-0002"))
        )
        self.assertEqual(stored.is_return, 1)
        self.assertEqual(stored.return_against, "ACC-SINV-ORIG-0001")

        self.creation.submit_invoice(
            json.dumps(self._sale_payload(name=stored.name)),
            json.dumps({}),
            submit_in_background=0,
        )
        self.assertEqual(stored.submit_count, 1)
        self.assertEqual(stored.is_return, 1)
        self.assertEqual(stored.return_against, "ACC-SINV-ORIG-0001")

    def test_partial_update_of_a_stored_return_cannot_repoint_it(self):
        self._stub_return_originals(
            {
                "ACC-SINV-ORIG-0001": {"docstatus": 1, "is_return": 0, "company": "Test Company"},
                "ACC-SINV-OTHERCO-0001": {"docstatus": 1, "is_return": 0, "company": "Other Company"},
            }
        )
        stored = self._parked_draft(
            name="ACC-SINV-RET-DRAFT-0002",
            is_return=1,
            return_against="ACC-SINV-ORIG-0001",
        )

        for call in (
            lambda: self.creation.update_invoice(
                json.dumps(
                    self._sale_payload(name=stored.name, return_against="ACC-SINV-OTHERCO-0001")
                )
            ),
            lambda: self.creation.submit_invoice(
                json.dumps(
                    self._sale_payload(name=stored.name, return_against="ACC-SINV-OTHERCO-0001")
                ),
                json.dumps({}),
                submit_in_background=0,
            ),
        ):
            with self.assertRaisesRegex(Exception, "submitted sale of this company"):
                call()
        self.assertEqual(stored.save_count, 0)
        self.assertEqual(stored.submit_count, 0)

    def test_return_must_reference_a_submitted_sale_of_this_company(self):
        returns_name = "posawesome.posawesome.api.invoice_processing.returns"
        returns_stub = types.ModuleType(returns_name)
        returns_stub.validate_return_items = lambda *_args, **_kwargs: {"valid": True}
        previous_returns = sys.modules.get(returns_name)
        sys.modules[returns_name] = returns_stub
        self.addCleanup(
            lambda: sys.modules.__setitem__(returns_name, previous_returns)
            if previous_returns
            else sys.modules.pop(returns_name, None)
        )
        originals = {
            "ACC-SINV-DRAFT-0001": {"docstatus": 0, "is_return": 0, "company": "Test Company"},
            "ACC-SINV-RETURN-0001": {"docstatus": 1, "is_return": 1, "company": "Test Company"},
            "ACC-SINV-OTHERCO-0001": {"docstatus": 1, "is_return": 0, "company": "Other Company"},
        }

        def get_value(doctype, name, fieldname=None, *args, **kwargs):
            # Only the original-invoice lookup asks for several fields at once.
            if doctype in {"Sales Invoice", "POS Invoice"} and isinstance(fieldname, (list, tuple)):
                row = originals.get(name)
                return types.SimpleNamespace(**row) if row else None
            return None

        self.frappe.db.get_value = get_value
        for original in [*originals, "ACC-SINV-MISSING-0001"]:
            with self.subTest(original=original):
                with self.assertRaisesRegex(Exception, "submitted sale of this company"):
                    self.creation.update_invoice(
                        json.dumps(self._sale_payload(is_return=1, return_against=original))
                    )
        self.assertEqual(self._all_saved(), [])

    def test_trusted_submitted_amendment_keeps_its_amended_from_link(self):
        self.frappe.flags.posa_trusted_shift_audit = {
            "source": "submitted_amendment",
            "source_invoice_name": "ACC-SINV-OLD-0001",
        }

        result = self.creation.update_invoice(
            json.dumps(self._sale_payload(amended_from="ACC-SINV-OLD-0001"))
        )

        self.assertEqual(self.invoices[result["name"]].get("amended_from"), "ACC-SINV-OLD-0001")


class TestNewTillDraftReadScope(TillAuthorityHarness):
    """P2: a new till draft passes the same read/scope gate before its first save."""

    def test_new_draft_without_invoice_read_is_refused_before_save(self):
        self.new_invoice_grants = ()
        submitted = self._parked_draft(name="ACC-SINV-SUBMITTED-0002", docstatus=1)

        for label, payload in {
            "new": self._sale_payload(),
            "stale name": self._sale_payload(name="ACC-SINV-GONE-0001"),
            "submitted source": self._sale_payload(name=submitted.name),
        }.items():
            for call in (
                lambda: self.creation.update_invoice(json.dumps(payload)),
                lambda: self.creation.submit_invoice(
                    json.dumps(payload), json.dumps({}), submit_in_background=0
                ),
            ):
                with self.subTest(source=label):
                    with self.assertRaisesRegex(PermissionError, "No read permission"):
                        call()

        self.assertTrue(self.created)
        for invoice in self.created:
            self.assertEqual(invoice.save_count, 0)
            self.assertEqual(invoice.submit_count, 0)
            self.assertIn("read", invoice.permission_checks)

    def test_new_draft_is_bound_to_the_validated_shift_before_save(self):
        result = self.creation.update_invoice(json.dumps(self._sale_payload()))

        sale = self.invoices[result["name"]]
        self.assertEqual(sale.posa_pos_opening_shift, SHIFT_NAME)
        self.assertEqual(sale.permission_checks, ["read"])


class TestQueuedSubmissionRechecksTillAuthority(TillAuthorityHarness):
    """P1b: a queued submission re-checks the till authority when it runs."""

    def _close_shift(self):
        self.shifts[SHIFT_NAME] = _shift(status="Closed", pos_closing_shift="POS-CLOSE-0001")

    def _worker_kwargs(self, draft, **overrides):
        kwargs = {
            "invoice": draft.name,
            "doctype": "Sales Invoice",
            "data": {},
            "payments": [],
            "user": SESSION_USER,
            "cashier": "cashier@example.com",
            "ledger_name": None,
            "opening_shift": SHIFT_NAME,
            "opening_user": SESSION_USER,
        }
        kwargs.update(overrides)
        return kwargs

    def assert_refused(self, draft, message):
        self.assertEqual(draft.submit_count, 0)
        errors = [payload for event, payload in self.realtime_events if event == "pos_invoice_submit_error"]
        self.assertEqual(len(errors), 1)
        self.assertRegex(errors[0]["error"], message)

    def test_queued_draft_still_submits_while_shift_is_open(self):
        draft = self._parked_draft()

        self.creation.submit_in_background_job(self._worker_kwargs(draft))

        self.assertEqual(draft.submit_count, 1)
        self.assertIn("read", draft.permission_checks)

    def test_queued_draft_is_refused_after_shift_closes(self):
        draft = self._parked_draft()
        self._close_shift()

        self.creation.submit_in_background_job(self._worker_kwargs(draft))

        self.assert_refused(draft, "is not open")

    def test_queued_draft_rebound_to_another_shift_is_refused(self):
        self.shifts["POS-OPEN-FOREIGN"] = _shift(name="POS-OPEN-FOREIGN")
        draft = self._parked_draft(posa_pos_opening_shift="POS-OPEN-FOREIGN")

        self.creation.submit_in_background_job(self._worker_kwargs(draft))

        self.assert_refused(draft, "another POS Opening Shift")

    def test_queued_draft_with_another_cashiers_shift_is_refused(self):
        self.shifts[SHIFT_NAME] = _shift(user="other@example.com")
        draft = self._parked_draft()

        self.creation.submit_in_background_job(self._worker_kwargs(draft))

        self.assert_refused(draft, "does not belong to the current user")

    def test_queued_payload_is_refused_after_shift_closes(self):
        draft = self._parked_draft()
        self._close_shift()

        self.creation.submit_payload_in_background_job(
            {
                "invoice": self._sale_payload(name=draft.name),
                "data": {},
                "doctype": "Sales Invoice",
                "ledger_name": None,
                "cashier": "cashier@example.com",
                "user": SESSION_USER,
                "opening_shift": SHIFT_NAME,
                "opening_user": SESSION_USER,
            }
        )

        self.assert_refused(draft, "is not open")

    def test_queued_payload_is_refused_when_stored_draft_was_rebound(self):
        self.shifts["POS-OPEN-FOREIGN"] = _shift(name="POS-OPEN-FOREIGN")
        draft = self._parked_draft(posa_pos_opening_shift="POS-OPEN-FOREIGN")

        self.creation.submit_payload_in_background_job(
            {
                "invoice": self._sale_payload(name=draft.name),
                "data": {},
                "doctype": "Sales Invoice",
                "ledger_name": None,
                "cashier": "cashier@example.com",
                "user": SESSION_USER,
                "opening_shift": SHIFT_NAME,
                "opening_user": SESSION_USER,
            }
        )

        self.assert_refused(draft, "another POS Opening Shift")


class SubmittedInvoiceWithoutCancel(edits_harness.FakeInvoice):
    def check_permission(self, permission_type):
        self.setdefault("permission_checks", []).append(permission_type)
        if permission_type not in self.get("granted", ()):
            raise PermissionError(f"No {permission_type} permission")


class TestSubmittedEditNeedsCancelPermission(unittest.TestCase):
    def _edit_metadata(self, granted, doctype="Sales Invoice"):
        module, frappe = edits_harness._load_module({})
        doc = edits_harness.AttrDict(
            doctype=doctype,
            name="INVOICE-1",
            docstatus=1,
            pos_profile="Main POS",
        )
        module._hours_since = lambda _value: 1
        permission_checks = []

        def has_permission(checked_doctype, ptype, doc=None):
            permission_checks.append((checked_doctype, ptype, doc))
            return ptype in granted

        frappe.has_permission = has_permission
        context = {
            "root_info": {doc.name: {"original_creation": "2026-10-26"}},
            "profile_settings": {"Main POS": {"feature_enabled": True, "edit_window_hours": 24}},
        }
        metadata = module.get_submitted_invoice_edit_metadata(doc, list_context=context)
        return metadata, permission_checks

    def test_edit_offer_requires_native_cancel_on_the_actual_invoice(self):
        for doctype in ("Sales Invoice", "POS Invoice"):
            with self.subTest(doctype=doctype):
                metadata, checks = self._edit_metadata(granted=("read", "print"), doctype=doctype)

                self.assertFalse(metadata["can_edit_submitted_invoice"])
                self.assertIn("cancel", metadata["edit_block_reason"].lower())
                self.assertEqual(checks, [(doctype, "cancel", "INVOICE-1")])

    def test_native_cancel_holder_is_still_offered_edit(self):
        metadata, checks = self._edit_metadata(granted=("read", "cancel"))

        self.assertTrue(metadata["can_edit_submitted_invoice"])
        self.assertIsNone(metadata["edit_block_reason"])
        self.assertEqual(checks, [("Sales Invoice", "cancel", "INVOICE-1")])

    def _run_edit(self, granted):
        captured = {}
        module, frappe = edits_harness._load_module(captured)
        savepoints = []
        frappe.db.savepoint = lambda name: savepoints.append(name)
        original = SubmittedInvoiceWithoutCancel(
            doctype="Sales Invoice",
            name="ACC-SINV-ORIGINAL-0001",
            docstatus=1,
            pos_profile="Main POS",
            company="Test Company",
            posa_pos_opening_shift=SHIFT_NAME,
            creation="2026-10-26",
            flags=types.SimpleNamespace(),
            granted=granted,
        )
        amended = edits_harness.FakeInvoice(
            doctype="Sales Invoice",
            name="ACC-SINV-AMENDED-0001",
            docstatus=1,
            pos_profile="Main POS",
            company="Test Company",
        )
        frappe.get_doc = lambda _doctype, name: original if name == original.name else amended
        module.assert_submitted_invoice_edit_allowed = lambda *_args, **_kwargs: {
            "can_edit_submitted_invoice": True
        }
        module.get_submitted_invoice_edit_metadata = lambda *_args: {}
        module.build_submitted_invoice_amendment_payload = lambda *_args, **_kwargs: {
            "doctype": "Sales Invoice",
            "amended_from": original.name,
            "pos_profile": "Main POS",
            "company": "Test Company",
            "posa_pos_opening_shift": SHIFT_NAME,
        }
        call = lambda: module.submit_submitted_invoice_edit(  # noqa: E731
            "Sales Invoice",
            original.name,
            client_request_id="submitted-edit-cancel-001",
            pos_profile="Main POS",
            company="Test Company",
        )
        return call, original, captured, savepoints

    def test_cashier_without_cancel_cannot_edit_submitted_invoice(self):
        call, original, captured, savepoints = self._run_edit(granted=("read", "print"))

        with self.assertRaisesRegex(PermissionError, "No cancel permission"):
            call()

        self.assertFalse(original.get("cancelled"))
        self.assertNotIn("submitted_invoice", captured)
        self.assertEqual(savepoints, [])
        self.assertIn("cancel", original.permission_checks)

    def test_supervisor_with_cancel_edits_submitted_invoice(self):
        call, original, captured, savepoints = self._run_edit(granted=("read", "cancel"))

        result = call()

        self.assertTrue(original.get("cancelled"))
        self.assertEqual(result["name"], "ACC-SINV-AMENDED-0001")
        self.assertEqual(savepoints, ["submitted_invoice_edit"])
        self.assertEqual(original.permission_checks[-1], "cancel")


if __name__ == "__main__":
    unittest.main()
