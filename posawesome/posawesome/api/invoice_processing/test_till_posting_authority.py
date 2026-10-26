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


class TestTillPostingAuthority(unittest.TestCase):
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
        self.frappe.session = types.SimpleNamespace(user=SESSION_USER)
        self.frappe.flags = types.SimpleNamespace(ignore_account_permission=False)
        self.frappe.db.has_column = lambda doctype, fieldname: True
        self.frappe.db.exists = self._exists
        self.frappe.db.get_value = lambda *args, **kwargs: None
        self.frappe.db.set_value = lambda *args, **kwargs: None
        self.frappe.get_value = lambda *args, **kwargs: 0
        self.frappe.get_cached_value = lambda *args, **kwargs: None
        self.frappe.get_doc = self._get_doc
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
            invoice = PermissionRecordingInvoice(**_invoice_fields(**payload, name=name))
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


class SubmittedInvoiceWithoutCancel(edits_harness.FakeInvoice):
    def check_permission(self, permission_type):
        self.setdefault("permission_checks", []).append(permission_type)
        if permission_type not in self.get("granted", ()):
            raise PermissionError(f"No {permission_type} permission")


class TestSubmittedEditNeedsCancelPermission(unittest.TestCase):
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
