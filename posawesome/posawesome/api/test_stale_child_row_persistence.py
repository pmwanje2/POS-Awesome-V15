import copy
import importlib.util
import json
import pathlib
import sys
import types
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_ORIGINAL_MODULES = dict(sys.modules)
_MANAGED_PREFIXES = (
    "frappe",
    "erpnext",
    "posawesome",
)


def tearDownModule():
    for name in list(sys.modules):
        if name.startswith(_MANAGED_PREFIXES) and name not in _ORIGINAL_MODULES:
            sys.modules.pop(name, None)
    for name, module in _ORIGINAL_MODULES.items():
        if name.startswith(_MANAGED_PREFIXES):
            sys.modules[name] = module


class AttrDict(dict):
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__

    def update(self, other=None, **kwargs):
        if other:
            super().update(other)
        if kwargs:
            super().update(kwargs)
        return self

    def as_dict(self):
        return dict(self)

    def precision(self, _fieldname):
        return 2


class FakeDoc:
    def __init__(self, **kwargs):
        object.__setattr__(self, "_data", dict(kwargs))
        if "flags" not in self._data:
            self._data["flags"] = types.SimpleNamespace(
                ignore_permissions=False,
                ignore_pricing_rule=False,
            )

    def __getattr__(self, name):
        try:
            return self._data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self._data[name] = value

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value

    def is_new(self):
        return not bool(self._data.get("name")) or bool(self._data.get("__islocal"))

    def check_permission(self, _ptype):
        return True

    def update(self, other=None, **kwargs):
        incoming = {}
        if other:
            if isinstance(other, dict):
                incoming.update(other)
            else:
                incoming.update(getattr(other, "_data", {}))
        if kwargs:
            incoming.update(kwargs)
        for key, value in incoming.items():
            if isinstance(value, list):
                self._data[key] = [
                    AttrDict(row) if isinstance(row, dict) and not isinstance(row, AttrDict) else row
                    for row in value
                ]
            else:
                self._data[key] = value
        return self

    def precision(self, _fieldname):
        return 2

    def as_dict(self):
        result = {}
        for key, value in self._data.items():
            if key in {"flags", "meta"} or callable(value):
                continue
            if isinstance(value, list):
                result[key] = [
                    dict(row.as_dict())
                    if hasattr(row, "as_dict") and callable(row.as_dict)
                    else (dict(row) if isinstance(row, dict) else row)
                    for row in value
                ]
            else:
                result[key] = value
        return result


SHIFT_NAME = "POS-OPEN-2026-0001"
INVOICE_NAME = "ACC-PSINV-2026-02730"
STALE_ROW_NAME = "6ldjpi6b7k"
LIVE_ROW_NAME = "6ldqh91dvq"


def _make_open_shift(**overrides):
    data = {
        "name": SHIFT_NAME,
        "pos_profile": "JB Till 1",
        "company": "JB Supermarket",
        "user": "cashier@example.com",
        "docstatus": 1,
        "status": "Open",
        "pos_closing_shift": None,
    }
    data.update(overrides)
    return FakeDoc(**data)


def _install_framework_stubs():
    frappe_module = types.ModuleType("frappe")
    frappe_utils = types.ModuleType("frappe.utils")
    frappe_exceptions = types.ModuleType("frappe.exceptions")
    frappe_background_jobs = types.ModuleType("frappe.utils.background_jobs")

    class _FrappeDict(AttrDict):
        pass

    class ValidationError(Exception):
        pass

    class TimestampMismatchError(Exception):
        pass

    class QueryDeadlockError(Exception):
        pass

    def _throw(message, exc=None, *args, **kwargs):
        raise (exc or ValidationError)(message)

    frappe_utils.cint = lambda value: int(value or 0)
    frappe_utils.flt = lambda value, precision=None: round(float(value or 0), precision or 2)
    frappe_utils.getdate = lambda value: value
    frappe_utils.nowdate = lambda: "2026-10-30"
    frappe_utils.money_in_words = lambda value, currency=None: f"{value} {currency or ''}".strip()

    frappe_module._dict = _FrappeDict
    frappe_module._ = lambda text: text
    frappe_module.PermissionError = PermissionError
    frappe_module.ValidationError = ValidationError
    frappe_module.throw = _throw
    frappe_module.whitelist = lambda *args, **kwargs: (lambda fn: fn)
    frappe_module.log_error = lambda *args, **kwargs: None
    frappe_module.get_value = lambda *args, **kwargs: None
    frappe_module.get_cached_value = lambda *args, **kwargs: None
    frappe_module.get_cached_doc = lambda *args, **kwargs: _FrappeDict()
    frappe_module.get_meta = lambda *args, **kwargs: types.SimpleNamespace(
        has_field=lambda _fieldname: False
    )
    frappe_module.flags = types.SimpleNamespace(ignore_account_permission=False)
    frappe_module.session = types.SimpleNamespace(user="cashier@example.com")

    publish_realtime_calls = []
    frappe_module.publish_realtime = lambda *args, **kwargs: publish_realtime_calls.append(
        {"args": args, "kwargs": kwargs}
    )
    frappe_module._publish_realtime_calls = publish_realtime_calls

    frappe_module.db = types.SimpleNamespace(
        get_value=lambda *args, **kwargs: None,
        get_values=lambda *args, **kwargs: (),
        exists=lambda *args, **kwargs: False,
        has_column=lambda *args, **kwargs: False,
        count=lambda *args, **kwargs: 0,
        rollback=lambda: None,
        commit=lambda: None,
    )
    frappe_module.get_doc = lambda *args, **kwargs: None

    frappe_exceptions.TimestampMismatchError = TimestampMismatchError
    frappe_exceptions.QueryDeadlockError = QueryDeadlockError
    frappe_exceptions.ValidationError = ValidationError
    frappe_background_jobs.enqueue = lambda *args, **kwargs: None

    sys.modules["frappe"] = frappe_module
    sys.modules["frappe.utils"] = frappe_utils
    sys.modules["frappe.exceptions"] = frappe_exceptions
    sys.modules["frappe.utils.background_jobs"] = frappe_background_jobs

    return frappe_module, TimestampMismatchError, ValidationError


def _install_dependency_stubs():
    sales_invoice_module = types.ModuleType("erpnext.accounts.doctype.sales_invoice.sales_invoice")
    sales_invoice_module.get_bank_cash_account = lambda *args, **kwargs: {"account": "Cash - JB"}
    sys.modules["erpnext.accounts.doctype.sales_invoice.sales_invoice"] = sales_invoice_module

    processing_utils = types.ModuleType("posawesome.posawesome.api.invoice_processing.utils")
    processing_utils._get_return_validity_settings = lambda *_args, **_kwargs: (False, 0)
    processing_utils._validate_return_window = lambda *_args, **_kwargs: None
    processing_utils._resolve_effective_price_list = lambda *_args, **_kwargs: None
    processing_utils._build_invoice_remarks = lambda *_args, **_kwargs: ""
    processing_utils._set_return_valid_upto = lambda *_args, **_kwargs: None
    processing_utils.get_latest_rate = lambda *_args, **_kwargs: (1, "2026-10-30")
    processing_utils.resolve_erpnext_currency_rates = lambda *_args, **_kwargs: {
        "conversion_rate": 1.0,
        "plc_conversion_rate": 1.0,
        "exchange_rate_date": None,
    }
    sys.modules["posawesome.posawesome.api.invoice_processing.utils"] = processing_utils

    stock_module = types.ModuleType("posawesome.posawesome.api.invoice_processing.stock")
    stock_module._strip_client_freebies_from_payload = lambda *_args, **_kwargs: None
    stock_module._validate_stock_on_invoice = lambda *_args, **_kwargs: None
    stock_module._apply_item_name_overrides = lambda *_args, **_kwargs: None
    stock_module._deduplicate_free_items = lambda *_args, **_kwargs: None
    stock_module._merge_duplicate_taxes = lambda *_args, **_kwargs: None
    stock_module._auto_set_return_batches = lambda *_args, **_kwargs: None
    stock_module._collect_stock_errors = lambda *_args, **_kwargs: []
    stock_module._should_block = lambda *_args, **_kwargs: False
    sys.modules["posawesome.posawesome.api.invoice_processing.stock"] = stock_module

    payment_utils_module = types.ModuleType("posawesome.posawesome.api.payment_processing.utils")
    payment_utils_module.get_bank_cash_account = lambda *_args, **_kwargs: {"account": "Cash - JB"}
    sys.modules["posawesome.posawesome.api.payment_processing.utils"] = payment_utils_module

    utilities_module = types.ModuleType("posawesome.posawesome.api.utilities")
    utilities_module.ensure_child_doctype = lambda *_args, **_kwargs: None
    utilities_module.set_batch_nos_for_bundels = lambda *_args, **_kwargs: None
    sys.modules["posawesome.posawesome.api.utilities"] = utilities_module

    payments_module = types.ModuleType("posawesome.posawesome.api.payments")
    payments_module.redeeming_customer_credit = lambda *_args, **_kwargs: None
    sys.modules["posawesome.posawesome.api.payments"] = payments_module

    sale_controls_module = types.ModuleType("posawesome.posawesome.api.item_sale_controls")
    sale_controls_module.collect_item_sale_control_errors = lambda *_args, **_kwargs: []
    sale_controls_module.validate_invoice_item_sale_controls = lambda *_args, **_kwargs: None
    sys.modules["posawesome.posawesome.api.item_sale_controls"] = sale_controls_module

    terminal_state_module = types.ModuleType("posawesome.posawesome.api.terminal_state")
    terminal_state_module.get_active_terminal_cashier = lambda *_args, **_kwargs: "cashier@example.com"
    terminal_state_module.validate_assigned_terminal_cashier = (
        lambda _pos_profile=None, cashier=None: cashier or "cashier@example.com"
    )
    sys.modules["posawesome.posawesome.api.terminal_state"] = terminal_state_module

    pos_access_module = types.ModuleType("posawesome.posawesome.api.pos_access")
    pos_access_module.get_authorized_pos_profile = lambda pos_profile=None, company=None: AttrDict(
        name=pos_profile or "JB Till 1",
        company=company or "JB Supermarket",
        create_pos_invoice_instead_of_sales_invoice=1,
    )
    pos_access_module.require_pos_supervisor_or_manager = lambda: "manager@example.com"
    sys.modules["posawesome.posawesome.api.pos_access"] = pos_access_module

    gift_cards_module = types.ModuleType("posawesome.posawesome.api.gift_cards")
    gift_cards_module.apply_invoice_gift_card_redemptions = lambda *_args, **_kwargs: None
    sys.modules["posawesome.posawesome.api.gift_cards"] = gift_cards_module


def _install_package_stubs():
    for name in (
        "posawesome.posawesome.api.idempotency",
        "posawesome.posawesome.api.tax_contracts",
        "posawesome.posawesome.api.payment_currency",
    ):
        sys.modules.pop(name, None)

    package_paths = {
        "posawesome": REPO_ROOT / "posawesome",
        "posawesome.posawesome": REPO_ROOT / "posawesome" / "posawesome",
        "posawesome.posawesome.api": REPO_ROOT / "posawesome" / "posawesome" / "api",
        "posawesome.posawesome.api.invoice_processing": (
            REPO_ROOT / "posawesome" / "posawesome" / "api" / "invoice_processing"
        ),
    }
    for name, path in package_paths.items():
        module = types.ModuleType(name)
        module.__path__ = [str(path)]
        sys.modules[name] = module


def _load_creation_module():
    module_name = "posawesome.posawesome.api.invoice_processing.creation"
    file_path = REPO_ROOT / "posawesome" / "posawesome" / "api" / "invoice_processing" / "creation.py"
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _make_draft_pos_invoice(**overrides):
    data = {
        "doctype": "POS Invoice",
        "name": INVOICE_NAME,
        "docstatus": 0,
        "pos_profile": "JB Till 1",
        "company": "JB Supermarket",
        "customer": "Walk-In Customer",
        "customer_name": "Walk-In Customer",
        "currency": "UGX",
        "conversion_rate": 1,
        "posa_pos_opening_shift": SHIFT_NAME,
        "is_return": 0,
        "return_against": None,
        "redeem_loyalty_points": 0,
        "loyalty_program": None,
        "cost_center": None,
        "write_off_amount": 0,
        "rounded_total": 10000,
        "grand_total": 10000,
        "paid_amount": 10000,
        "base_paid_amount": 10000,
        "remarks": "",
        "modified": "2026-10-30 10:00:00",
        "items": [
            AttrDict(
                name=LIVE_ROW_NAME,
                item_code="SUGAR-1KG",
                qty=1,
                rate=10000,
                parent=INVOICE_NAME,
                parenttype="POS Invoice",
                parentfield="items",
            )
        ],
        "payments": [
            AttrDict(
                name="pay-live-1",
                mode_of_payment="Cash",
                type="Cash",
                amount=10000,
                base_amount=10000,
                parent=INVOICE_NAME,
                parenttype="POS Invoice",
                parentfield="payments",
            )
        ],
        "taxes": [],
    }
    data.update(overrides)
    return FakeDoc(**data)


class TestStaleChildRowPersistence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frappe, cls.TimestampMismatchError, cls.ValidationError = _install_framework_stubs()
        _install_dependency_stubs()
        _install_package_stubs()
        cls.creation = _load_creation_module()

    def setUp(self):
        self.frappe._publish_realtime_calls.clear()
        self.frappe.session = types.SimpleNamespace(user="cashier@example.com")
        self.frappe.flags = types.SimpleNamespace(ignore_account_permission=False)
        self.frappe.get_value = lambda *args, **kwargs: None
        self.frappe.get_meta = lambda *args, **kwargs: types.SimpleNamespace(
            has_field=lambda _fieldname: False
        )
        self.frappe.db = types.SimpleNamespace(
            get_value=lambda *args, **kwargs: None,
            get_values=lambda *args, **kwargs: (),
            exists=lambda *args, **kwargs: False,
            has_column=lambda *args, **kwargs: False,
            count=lambda *args, **kwargs: 0,
            rollback=lambda: None,
            commit=lambda: None,
        )
        self.frappe.get_doc = lambda *args, **kwargs: None

    def test_get_mutable_invoice_doc_strips_stale_child_row_names_and_marks_local(self):
        live_draft = _make_draft_pos_invoice()
        shift_doc = _make_open_shift()

        self.frappe.db.exists = lambda doctype, name: doctype == "POS Invoice" and name == INVOICE_NAME
        self.frappe.get_doc = lambda doctype, name, **kwargs: live_draft

        incoming_payload = {
            "doctype": "POS Invoice",
            "name": INVOICE_NAME,
            "pos_profile": "JB Till 1",
            "company": "JB Supermarket",
            "customer": "Walk-In Customer",
            "items": [
                {
                    "name": STALE_ROW_NAME,
                    "item_code": "SUGAR-1KG",
                    "qty": 2,
                    "rate": 10000,
                    "parent": INVOICE_NAME,
                    "parenttype": "POS Invoice",
                    "parentfield": "items",
                }
            ],
            "payments": [
                {
                    "name": "pay-stale-0",
                    "mode_of_payment": "Cash",
                    "type": "Cash",
                    "amount": 20000,
                    "parent": INVOICE_NAME,
                    "parenttype": "POS Invoice",
                    "parentfield": "payments",
                }
            ],
        }
        original_snapshot = copy.deepcopy(incoming_payload)

        updated_doc = self.creation._get_mutable_invoice_doc(
            incoming_payload,
            "POS Invoice",
            "JB Till 1",
            "JB Supermarket",
            opening_shift_doc=shift_doc,
        )

        item_row = updated_doc.items[0]
        self.assertNotIn("name", item_row)
        self.assertEqual(item_row.get("__islocal"), 1)
        self.assertNotIn("parent", item_row)
        self.assertNotIn("parenttype", item_row)
        self.assertNotIn("parentfield", item_row)

        payment_row = updated_doc.payments[0]
        self.assertNotIn("name", payment_row)
        self.assertEqual(payment_row.get("__islocal"), 1)
        self.assertNotIn("parent", payment_row)
        self.assertEqual(incoming_payload, original_snapshot)

    def test_get_mutable_invoice_doc_preserves_valid_live_child_row_names(self):
        live_draft = _make_draft_pos_invoice()
        shift_doc = _make_open_shift()

        self.frappe.db.exists = lambda doctype, name: doctype == "POS Invoice" and name == INVOICE_NAME
        self.frappe.get_doc = lambda doctype, name, **kwargs: live_draft

        incoming_payload = {
            "doctype": "POS Invoice",
            "name": INVOICE_NAME,
            "pos_profile": "JB Till 1",
            "company": "JB Supermarket",
            "customer": "Walk-In Customer",
            "items": [
                {
                    "name": LIVE_ROW_NAME,
                    "item_code": "SUGAR-1KG",
                    "qty": 3,
                    "rate": 10000,
                    "parent": INVOICE_NAME,
                    "parenttype": "POS Invoice",
                    "parentfield": "items",
                },
                {
                    "name": STALE_ROW_NAME,
                    "item_code": "SALT-500G",
                    "qty": 1,
                    "rate": 2000,
                    "parent": INVOICE_NAME,
                    "parenttype": "POS Invoice",
                    "parentfield": "items",
                },
            ],
            "payments": [
                {
                    "name": "pay-live-1",
                    "mode_of_payment": "Cash",
                    "type": "Cash",
                    "amount": 32000,
                    "parent": INVOICE_NAME,
                    "parenttype": "POS Invoice",
                    "parentfield": "payments",
                }
            ],
        }

        updated_doc = self.creation._get_mutable_invoice_doc(
            incoming_payload,
            "POS Invoice",
            "JB Till 1",
            "JB Supermarket",
            opening_shift_doc=shift_doc,
        )

        live_item = updated_doc.items[0]
        stale_item = updated_doc.items[1]
        self.assertEqual(live_item.get("name"), LIVE_ROW_NAME)
        self.assertFalse(live_item.get("__islocal"))

        self.assertNotIn("name", stale_item)
        self.assertEqual(stale_item.get("__islocal"), 1)

        self.assertEqual(updated_doc.payments[0].get("name"), "pay-live-1")
        self.assertFalse(updated_doc.payments[0].get("__islocal"))

    def test_sanitize_helper_discovers_dynamic_meta_table_fields_without_mutating_caller(self):
        live_draft = _make_draft_pos_invoice(
            custom_posa_lines=[AttrDict(name="custom-live-1", value="ok")],
            meta=types.SimpleNamespace(
                get_table_fields=lambda: [
                    types.SimpleNamespace(fieldname="items"),
                    types.SimpleNamespace(fieldname="custom_posa_lines"),
                ]
            ),
        )
        payload = {
            "custom_posa_lines": [
                {"name": "custom-live-1", "value": "updated"},
                {"name": "custom-stale-9", "value": "recreated", "parent": INVOICE_NAME},
            ]
        }
        original_snapshot = copy.deepcopy(payload)

        sanitized = self.creation._sanitize_stale_child_row_names(live_draft, payload)

        self.assertIsNot(sanitized, payload)
        self.assertEqual(payload, original_snapshot)
        self.assertEqual(sanitized["custom_posa_lines"][0].get("name"), "custom-live-1")
        self.assertFalse(sanitized["custom_posa_lines"][0].get("__islocal"))
        self.assertNotIn("name", sanitized["custom_posa_lines"][1])
        self.assertEqual(sanitized["custom_posa_lines"][1].get("__islocal"), 1)
        self.assertNotIn("parent", sanitized["custom_posa_lines"][1])

    def test_sanitize_covers_gift_card_redemptions_and_posa_change_returns(self):
        live_draft = _make_draft_pos_invoice(
            gift_card_redemptions=[AttrDict(name="gc-live-1", gift_card="GC-001")],
            posa_change_returns=[AttrDict(name="chg-live-1", amount=500)],
        )
        payload = {
            "gift_card_redemptions": [
                {"name": "gc-live-1", "gift_card": "GC-001"},
                {"name": "gc-stale-9", "gift_card": "GC-002", "parent": INVOICE_NAME},
            ],
            "posa_change_returns": [
                {"name": "chg-live-1", "amount": 500},
                {"name": "chg-stale-9", "amount": 1000, "parent": INVOICE_NAME},
            ],
        }
        original_snapshot = copy.deepcopy(payload)

        sanitized = self.creation._sanitize_stale_child_row_names(live_draft, payload)

        self.assertEqual(payload, original_snapshot)
        self.assertEqual(sanitized["gift_card_redemptions"][0].get("name"), "gc-live-1")
        self.assertNotIn("name", sanitized["gift_card_redemptions"][1])
        self.assertEqual(sanitized["gift_card_redemptions"][1].get("__islocal"), 1)
        self.assertNotIn("parent", sanitized["gift_card_redemptions"][1])

        self.assertEqual(sanitized["posa_change_returns"][0].get("name"), "chg-live-1")
        self.assertNotIn("name", sanitized["posa_change_returns"][1])
        self.assertEqual(sanitized["posa_change_returns"][1].get("__islocal"), 1)
        self.assertNotIn("parent", sanitized["posa_change_returns"][1])

    def test_sanitize_handles_unhashable_bool_empty_islocal_duplicate_and_cross_invoice_names(self):
        live_draft = _make_draft_pos_invoice(
            items=[
                AttrDict(name=LIVE_ROW_NAME, item_code="SUGAR-1KG", parent=INVOICE_NAME),
                AttrDict(name=1, item_code="NUMERIC-ID-ITEM", parent=INVOICE_NAME),
            ]
        )
        payload = {
            "items": [
                {"name": [], "item_code": "UNHASHABLE-LIST", "parent": INVOICE_NAME},
                {"name": {"nested": 1}, "item_code": "UNHASHABLE-DICT", "parent": INVOICE_NAME},
                {"name": True, "item_code": "BOOL-TRUE-COLLISION", "parent": INVOICE_NAME},
                {"name": None, "item_code": "NONE-NAME", "parent": INVOICE_NAME},
                {"name": "", "item_code": "EMPTY-NAME", "parent": INVOICE_NAME},
                {
                    "name": LIVE_ROW_NAME,
                    "__islocal": 1,
                    "item_code": "LIVE-NAME-MARKED-LOCAL",
                    "parent": INVOICE_NAME,
                },
                {"name": LIVE_ROW_NAME, "item_code": "LIVE-FIRST", "parent": INVOICE_NAME},
                {"name": LIVE_ROW_NAME, "item_code": "LIVE-DUPLICATE", "parent": INVOICE_NAME},
                {
                    "name": "row-on-other-invoice",
                    "item_code": "CROSS-INVOICE",
                    "parent": "ACC-PSINV-OTHER",
                },
            ]
        }

        sanitized = self.creation._sanitize_stale_child_row_names(live_draft, payload)
        rows = sanitized["items"]

        # Unhashable list / dict, bool True, None, "", live+__islocal=1 -> all stripped & __islocal=1
        for idx in (0, 1, 2, 3, 4, 5):
            with self.subTest(idx=idx, item_code=rows[idx]["item_code"]):
                self.assertNotIn("name", rows[idx])
                self.assertEqual(rows[idx].get("__islocal"), 1)
                self.assertNotIn("parent", rows[idx])

        # First genuine live row keeps its name; duplicate live row is stripped & marked local
        self.assertEqual(rows[6].get("name"), LIVE_ROW_NAME)
        self.assertFalse(rows[6].get("__islocal"))
        self.assertNotIn("name", rows[7])
        self.assertEqual(rows[7].get("__islocal"), 1)

        # Cross-invoice row is stripped & marked local
        self.assertNotIn("name", rows[8])
        self.assertNotIn("parent", rows[8])
        self.assertEqual(rows[8].get("__islocal"), 1)

    def test_build_fresh_invoice_payload_and_new_till_draft_strip_child_identity_and_audit_fields(self):
        source_payload = {
            "doctype": "POS Invoice",
            "name": "ACC-PSINV-SUBMITTED-001",
            "docstatus": 1,
            "pos_profile": "JB Till 1",
            "company": "JB Supermarket",
            "posa_pos_opening_shift": SHIFT_NAME,
            "items": [
                {
                    "name": "old-item-row-1",
                    "item_code": "SUGAR-1KG",
                    "qty": 1,
                    "parent": "ACC-PSINV-SUBMITTED-001",
                    "parenttype": "POS Invoice",
                    "parentfield": "items",
                    "docstatus": 1,
                    "creation": "2026-10-29 09:00:00",
                    "owner": "other.cashier@example.com",
                    "modified": "2026-10-29 09:05:00",
                    "modified_by": "other.cashier@example.com",
                }
            ],
            "payments": [
                {
                    "name": "old-pay-row-1",
                    "mode_of_payment": "Cash",
                    "amount": 10000,
                    "parent": "ACC-PSINV-SUBMITTED-001",
                    "parenttype": "POS Invoice",
                    "parentfield": "payments",
                    "docstatus": 1,
                    "creation": "2026-10-29 09:00:00",
                    "owner": "other.cashier@example.com",
                    "modified": "2026-10-29 09:05:00",
                    "modified_by": "other.cashier@example.com",
                }
            ],
            "gift_card_redemptions": [
                {
                    "name": "old-gc-row-1",
                    "gift_card": "GC-001",
                    "parent": "ACC-PSINV-SUBMITTED-001",
                    "docstatus": 1,
                    "owner": "other.cashier@example.com",
                }
            ],
        }
        original_snapshot = copy.deepcopy(source_payload)

        fresh = self.creation._build_fresh_invoice_payload(source_payload, "POS Invoice")
        self.assertEqual(source_payload, original_snapshot)

        audit_fields = (
            "name",
            "parent",
            "parenttype",
            "parentfield",
            "docstatus",
            "creation",
            "owner",
            "modified",
            "modified_by",
        )
        for table_field in ("items", "payments", "gift_card_redemptions"):
            row = fresh[table_field][0]
            for field in audit_fields:
                self.assertNotIn(field, row, f"{table_field}[0] retained {field}")
            self.assertEqual(row.get("__islocal"), 1)

        # Also verify _new_till_draft sanitizes brand-new payloads passed directly (e.g. returns)
        captured_payloads = []
        shift_doc = _make_open_shift()

        def fake_get_doc(payload):
            captured_payloads.append(payload)
            return FakeDoc(
                doctype="POS Invoice",
                name=None,
                docstatus=0,
                pos_profile="JB Till 1",
                company="JB Supermarket",
                posa_pos_opening_shift=SHIFT_NAME,
                items=payload.get("items", []),
            )

        self.frappe.get_doc = fake_get_doc
        new_draft_input = {
            "doctype": "POS Invoice",
            "pos_profile": "JB Till 1",
            "company": "JB Supermarket",
            "is_return": 1,
            "items": [
                {
                    "name": "orig-sale-item-1",
                    "item_code": "SUGAR-1KG",
                    "qty": -1,
                    "parent": "ACC-PSINV-ORIG",
                    "parenttype": "POS Invoice",
                    "parentfield": "items",
                    "docstatus": 1,
                    "owner": "orig@example.com",
                    "creation": "2026-10-29 08:00:00",
                    "modified": "2026-10-29 08:00:00",
                    "modified_by": "orig@example.com",
                }
            ],
        }
        new_draft_snapshot = copy.deepcopy(new_draft_input)

        self.creation._new_till_draft(
            new_draft_input,
            "JB Till 1",
            "JB Supermarket",
            shift_doc,
        )

        self.assertEqual(new_draft_input, new_draft_snapshot)
        self.assertEqual(len(captured_payloads), 1)
        new_item = captured_payloads[0]["items"][0]
        for field in audit_fields:
            self.assertNotIn(field, new_item)
        self.assertEqual(new_item.get("__islocal"), 1)

    def test_merge_draft_state_preserves_ignore_pricing_rule_flag_for_normal_invoices(self):
        source_doc = _make_draft_pos_invoice(
            is_return=0,
            flags=types.SimpleNamespace(ignore_permissions=True, ignore_pricing_rule=True),
        )
        latest_doc = _make_draft_pos_invoice(
            is_return=0,
            flags=types.SimpleNamespace(ignore_permissions=False, ignore_pricing_rule=False),
        )
        self.frappe.db.get_values = lambda *args, **kwargs: ((LIVE_ROW_NAME,),)

        merged = self.creation._merge_draft_state_onto_latest_doc(source_doc, latest_doc)

        self.assertIs(merged, latest_doc)
        self.assertTrue(merged.flags.ignore_permissions)
        self.assertTrue(merged.flags.ignore_pricing_rule)

    def test_save_draft_retry_on_timestamp_mismatch_sanitizes_stale_child_row_names_with_for_update(self):
        stale_doc = _make_draft_pos_invoice(
            items=[
                AttrDict(
                    name=STALE_ROW_NAME,
                    item_code="SUGAR-1KG",
                    qty=1,
                    rate=10000,
                    parent=INVOICE_NAME,
                    parenttype="POS Invoice",
                    parentfield="items",
                )
            ]
        )
        latest_doc = _make_draft_pos_invoice(
            modified="2026-10-30 10:00:05",
            items=[
                AttrDict(
                    name=LIVE_ROW_NAME,
                    item_code="SUGAR-1KG",
                    qty=1,
                    rate=10000,
                    parent=INVOICE_NAME,
                    parenttype="POS Invoice",
                    parentfield="items",
                )
            ],
        )

        def fail_with_mismatch():
            raise self.TimestampMismatchError("modified mismatch")

        saved_items_snapshots = []

        def latest_save():
            saved_items_snapshots.append([dict(row) for row in latest_doc.items])
            return latest_doc

        stale_doc.save = fail_with_mismatch
        latest_doc.save = latest_save

        get_doc_calls = []
        get_values_calls = []

        def fake_get_doc(doctype, name, **kwargs):
            get_doc_calls.append((doctype, name, dict(kwargs)))
            return latest_doc

        def fake_get_values(doctype, filters=None, fieldname=None, **kwargs):
            get_values_calls.append((doctype, dict(filters or {}), fieldname, dict(kwargs)))
            return ((LIVE_ROW_NAME,),)

        self.frappe.db.get_value = lambda *args, **kwargs: None
        self.frappe.db.get_values = fake_get_values
        self.frappe.get_doc = fake_get_doc

        result = self.creation._save_draft_with_latest_timestamp(stale_doc)

        self.assertIs(result, latest_doc)
        self.assertEqual(len(saved_items_snapshots), 1)
        saved_item = saved_items_snapshots[0][0]
        self.assertNotIn("name", saved_item)
        self.assertEqual(saved_item.get("__islocal"), 1)
        self.assertNotIn("parent", saved_item)
        self.assertTrue(any(call[2].get("for_update") is True for call in get_doc_calls))
        self.assertTrue(any(call[3].get("for_update") is True for call in get_values_calls))

    def test_save_draft_concurrent_modified_drift_uses_for_update_and_sanitizes_stale_rows(self):
        stale_doc = _make_draft_pos_invoice(
            modified="2026-10-30 10:00:00",
            items=[
                AttrDict(
                    name=STALE_ROW_NAME,
                    item_code="SUGAR-1KG",
                    qty=1,
                    rate=10000,
                    parent=INVOICE_NAME,
                    parenttype="POS Invoice",
                    parentfield="items",
                )
            ],
        )
        latest_doc = _make_draft_pos_invoice(
            modified="2026-10-30 10:00:05",
            items=[
                AttrDict(
                    name=LIVE_ROW_NAME,
                    item_code="SUGAR-1KG",
                    qty=1,
                    rate=10000,
                    parent=INVOICE_NAME,
                    parenttype="POS Invoice",
                    parentfield="items",
                )
            ],
        )

        saved_docs = []
        stale_doc.save = lambda: saved_docs.append(("stale", [dict(r) for r in stale_doc.items]))
        latest_doc.save = lambda: saved_docs.append(("latest", [dict(r) for r in latest_doc.items]))

        get_value_calls = []
        get_doc_calls = []

        def fake_get_value(doctype, name, field, **kwargs):
            get_value_calls.append((doctype, name, field, dict(kwargs)))
            return "2026-10-30 10:00:05" if field == "modified" else None

        def fake_get_doc(doctype, name, **kwargs):
            get_doc_calls.append((doctype, name, dict(kwargs)))
            return latest_doc

        self.frappe.db.get_value = fake_get_value
        self.frappe.db.get_values = lambda *args, **kwargs: ((LIVE_ROW_NAME,),)
        self.frappe.get_doc = fake_get_doc

        result = self.creation._save_draft_with_latest_timestamp(stale_doc)

        self.assertIs(result, latest_doc)
        self.assertEqual(len(saved_docs), 1)
        self.assertEqual(saved_docs[0][0], "latest")
        saved_item = saved_docs[0][1][0]
        self.assertNotIn("name", saved_item)
        self.assertEqual(saved_item.get("__islocal"), 1)
        self.assertTrue(any(call[3].get("for_update") is True for call in get_value_calls))
        self.assertTrue(any(call[2].get("for_update") is True for call in get_doc_calls))

    def test_save_draft_reloads_when_current_modified_is_none_and_never_overwrites_modified_on_failed_reload(
        self,
    ):
        # Case 1: current_modified is None -> must reload via for_update=True, NOT blindly copy modified
        doc_without_modified = _make_draft_pos_invoice(
            modified=None,
            items=[AttrDict(name=STALE_ROW_NAME, item_code="SUGAR-1KG", qty=1, parent=INVOICE_NAME)],
        )
        latest_doc = _make_draft_pos_invoice(
            modified="2026-10-30 10:00:05",
            items=[AttrDict(name=LIVE_ROW_NAME, item_code="SUGAR-1KG", qty=1, parent=INVOICE_NAME)],
        )
        saved_targets = []
        doc_without_modified.save = lambda: saved_targets.append("stale")
        latest_doc.save = lambda: saved_targets.append("latest")

        get_doc_calls = []

        def fake_get_doc(doctype, name, **kwargs):
            get_doc_calls.append((doctype, name, dict(kwargs)))
            return latest_doc

        self.frappe.db.get_value = lambda doctype, name, field, **kwargs: "2026-10-30 10:00:05"
        self.frappe.db.get_values = lambda *args, **kwargs: ((LIVE_ROW_NAME,),)
        self.frappe.get_doc = fake_get_doc

        result = self.creation._save_draft_with_latest_timestamp(doc_without_modified)

        self.assertIs(result, latest_doc)
        self.assertEqual(saved_targets, ["latest"])
        self.assertTrue(any(call[2].get("for_update") is True for call in get_doc_calls))
        self.assertNotIn("name", latest_doc.items[0])
        self.assertEqual(latest_doc.items[0].get("__islocal"), 1)

        # Case 2: reload returns None -> must NOT overwrite modified to bypass Frappe's check_if_latest()
        stale_doc = _make_draft_pos_invoice(modified="2026-10-30 10:00:00")
        modified_seen_by_save = []

        def save_checks_timestamp():
            modified_seen_by_save.append(stale_doc.modified)
            if stale_doc.modified != "2026-10-30 10:00:05":
                raise self.TimestampMismatchError("stale timestamp preserved")

        stale_doc.save = save_checks_timestamp
        self.frappe.get_doc = lambda doctype, name, **kwargs: None

        with self.assertRaises(self.TimestampMismatchError):
            self.creation._save_draft_with_latest_timestamp(stale_doc, retries=0)

        self.assertEqual(modified_seen_by_save, ["2026-10-30 10:00:00"])

    def test_save_draft_repeatable_read_uses_locking_db_child_names_when_doc_children_are_stale(self):
        # Even if Frappe's load_children() returned stale row A under REPEATABLE READ,
        # the locking DB child query sees committed row B and strips stale row A.
        stale_doc = _make_draft_pos_invoice(
            modified="2026-10-30 10:00:00",
            items=[AttrDict(name=STALE_ROW_NAME, item_code="SUGAR-1KG", qty=1, parent=INVOICE_NAME)],
        )
        # latest_doc has updated modified, but its non-locking children still show STALE_ROW_NAME!
        latest_doc_stale_children = _make_draft_pos_invoice(
            modified="2026-10-30 10:00:05",
            items=[AttrDict(name=STALE_ROW_NAME, item_code="SUGAR-1KG", qty=1, parent=INVOICE_NAME)],
        )
        saved_items = []
        latest_doc_stale_children.save = lambda: saved_items.extend(
            [dict(r) for r in latest_doc_stale_children.items]
        )

        self.frappe.db.get_value = lambda doctype, name, field, **kwargs: "2026-10-30 10:00:05"
        self.frappe.get_doc = lambda doctype, name, **kwargs: latest_doc_stale_children
        # Locking DB query returns the true committed live row B
        self.frappe.db.get_values = lambda doctype, filters=None, fieldname=None, **kwargs: (
            ((LIVE_ROW_NAME,),) if kwargs.get("for_update") else ((STALE_ROW_NAME,),)
        )

        self.creation._save_draft_with_latest_timestamp(stale_doc)

        self.assertEqual(len(saved_items), 1)
        self.assertNotIn("name", saved_items[0])
        self.assertEqual(saved_items[0].get("__islocal"), 1)

    def test_submit_invoice_once_sanitizes_stale_child_row_name_and_preserves_live_name(self):
        live_draft = _make_draft_pos_invoice()
        shift_doc = _make_open_shift()
        submitted_items_snapshot = []

        def fake_save():
            return live_draft

        def fake_submit():
            submitted_items_snapshot.extend([dict(row) for row in live_draft.items])
            live_draft.docstatus = 1

        live_draft.save = fake_save
        live_draft.submit = fake_submit

        self.frappe.db.exists = lambda doctype, name: (
            (doctype == "POS Invoice" and name == INVOICE_NAME)
            or (doctype == "POS Opening Shift" and name == SHIFT_NAME)
        )
        self.frappe.db.get_value = lambda *args, **kwargs: None
        self.frappe.db.get_values = lambda doctype, filters=None, fieldname=None, **kwargs: (
            tuple((f"row-{i}",) for i in range(len(live_draft.items)))
            if doctype == "POS Invoice Item"
            else ()
        )

        def fake_get_doc(doctype, name=None, **kwargs):
            if doctype == "POS Opening Shift":
                return shift_doc
            if doctype == "POS Invoice" and name == INVOICE_NAME:
                return live_draft
            return None

        self.frappe.get_doc = fake_get_doc
        self.frappe.get_value = lambda *args, **kwargs: 0

        invoice_payload = {
            "doctype": "POS Invoice",
            "name": INVOICE_NAME,
            "pos_profile": "JB Till 1",
            "company": "JB Supermarket",
            "posa_pos_opening_shift": SHIFT_NAME,
            "customer": "Walk-In Customer",
            "currency": "UGX",
            "items": [
                {
                    "name": LIVE_ROW_NAME,
                    "item_code": "SUGAR-1KG",
                    "qty": 1,
                    "rate": 10000,
                    "parent": INVOICE_NAME,
                    "parenttype": "POS Invoice",
                    "parentfield": "items",
                },
                {
                    "name": STALE_ROW_NAME,
                    "item_code": "RICE-1KG",
                    "qty": 1,
                    "rate": 5000,
                    "parent": INVOICE_NAME,
                    "parenttype": "POS Invoice",
                    "parentfield": "items",
                },
            ],
            "payments": [
                {
                    "name": "pay-live-1",
                    "mode_of_payment": "Cash",
                    "type": "Cash",
                    "amount": 15000,
                    "base_amount": 15000,
                }
            ],
        }

        result = self.creation._submit_invoice_once(
            json.dumps(invoice_payload),
            json.dumps({}),
            submit_in_background=False,
        )

        self.assertEqual(result["docstatus"], 1)
        self.assertEqual(len(submitted_items_snapshot), 2)
        self.assertEqual(submitted_items_snapshot[0].get("name"), LIVE_ROW_NAME)
        self.assertFalse(submitted_items_snapshot[0].get("__islocal"))
        self.assertNotIn("name", submitted_items_snapshot[1])
        self.assertEqual(submitted_items_snapshot[1].get("__islocal"), 1)
        self.assertNotIn("parent", submitted_items_snapshot[1])

    def test_save_draft_throws_validation_error_when_db_item_count_is_zero(self):
        invoice_doc = _make_draft_pos_invoice()
        invoice_doc.save = lambda: invoice_doc

        get_values_calls = []

        def fake_get_values(doctype, filters=None, fieldname=None, **kwargs):
            get_values_calls.append((doctype, dict(filters or {}), fieldname, dict(kwargs)))
            return ()

        self.frappe.db.get_values = fake_get_values

        with self.assertRaises(self.ValidationError):
            self.creation._save_draft_with_latest_timestamp(invoice_doc)

        self.assertEqual(
            get_values_calls,
            [
                (
                    "POS Invoice Item",
                    {
                        "parent": INVOICE_NAME,
                        "parenttype": "POS Invoice",
                        "parentfield": "items",
                    },
                    "name",
                    {"for_update": True},
                )
            ],
        )

    def test_save_draft_passes_when_db_item_count_matches(self):
        invoice_doc = _make_draft_pos_invoice()
        invoice_doc.save = lambda: invoice_doc

        get_values_calls = []

        def fake_get_values(doctype, filters=None, fieldname=None, **kwargs):
            get_values_calls.append((doctype, dict(filters or {}), fieldname, dict(kwargs)))
            return ((LIVE_ROW_NAME,),)

        self.frappe.db.get_values = fake_get_values

        result = self.creation._save_draft_with_latest_timestamp(invoice_doc)

        self.assertIs(result, invoice_doc)
        self.assertEqual(
            get_values_calls,
            [
                (
                    "POS Invoice Item",
                    {
                        "parent": INVOICE_NAME,
                        "parenttype": "POS Invoice",
                        "parentfield": "items",
                    },
                    "name",
                    {"for_update": True},
                )
            ],
        )

    def test_submit_invoice_once_throws_validation_error_when_db_item_count_is_zero_on_submit(self):
        live_draft = _make_draft_pos_invoice()
        shift_doc = _make_open_shift()

        live_draft.save = lambda: live_draft
        live_draft.submit = lambda: setattr(live_draft, "docstatus", 1)

        self.frappe.db.exists = lambda doctype, name: (
            (doctype == "POS Invoice" and name == INVOICE_NAME)
            or (doctype == "POS Opening Shift" and name == SHIFT_NAME)
        )
        self.frappe.db.get_value = lambda *args, **kwargs: None

        get_values_calls = []

        def fake_get_values(doctype, filters=None, fieldname=None, **kwargs):
            get_values_calls.append((doctype, dict(filters or {}), fieldname, dict(kwargs)))
            if filters and filters.get("docstatus") == 1:
                return ()
            return ((LIVE_ROW_NAME,),)

        self.frappe.db.get_values = fake_get_values
        self.frappe.get_doc = lambda doctype, name=None, **kwargs: (
            shift_doc if doctype == "POS Opening Shift" else live_draft
        )
        self.frappe.get_value = lambda *args, **kwargs: 0

        invoice_payload = {
            "doctype": "POS Invoice",
            "name": INVOICE_NAME,
            "pos_profile": "JB Till 1",
            "company": "JB Supermarket",
            "posa_pos_opening_shift": SHIFT_NAME,
            "customer": "Walk-In Customer",
            "currency": "UGX",
            "items": [
                {
                    "name": LIVE_ROW_NAME,
                    "item_code": "SUGAR-1KG",
                    "qty": 1,
                    "rate": 10000,
                }
            ],
            "payments": [
                {
                    "name": "pay-live-1",
                    "mode_of_payment": "Cash",
                    "type": "Cash",
                    "amount": 10000,
                    "base_amount": 10000,
                }
            ],
        }

        with self.assertRaises(self.ValidationError):
            self.creation._submit_invoice_once(
                json.dumps(invoice_payload),
                json.dumps({}),
                submit_in_background=False,
            )

        self.assertIn(
            (
                "POS Invoice Item",
                {
                    "parent": INVOICE_NAME,
                    "parenttype": "POS Invoice",
                    "parentfield": "items",
                    "docstatus": 1,
                },
                "name",
                {"for_update": True},
            ),
            get_values_calls,
        )

    def test_assert_persisted_invoice_items_enforces_non_empty_docstatus_and_sales_invoice_and_meta(
        self,
    ):
        # Empty items allowed on draft save, rejected on submit
        empty_draft = _make_draft_pos_invoice(docstatus=0, items=[])
        self.frappe.db.get_values = lambda *args, **kwargs: ()
        self.creation._assert_persisted_invoice_items(empty_draft, require_non_empty=False)

        empty_submitted = _make_draft_pos_invoice(docstatus=1, items=[])
        with self.assertRaises(self.ValidationError):
            self.creation._assert_persisted_invoice_items(empty_submitted, require_non_empty=True)

        # Unsubmitted docstatus rejected on submit
        unsubmitted_doc = _make_draft_pos_invoice(docstatus=0)
        self.frappe.db.get_values = lambda *args, **kwargs: ((LIVE_ROW_NAME,),)
        with self.assertRaises(self.ValidationError):
            self.creation._assert_persisted_invoice_items(unsubmitted_doc, require_non_empty=True)

        # Partial persistence rejected on submit
        partial_submitted = _make_draft_pos_invoice(
            docstatus=1,
            items=[
                AttrDict(name="row-1", item_code="A"),
                AttrDict(name="row-2", item_code="B"),
            ],
        )
        self.frappe.db.get_values = lambda *args, **kwargs: (("row-1",),)
        with self.assertRaises(self.ValidationError):
            self.creation._assert_persisted_invoice_items(partial_submitted, require_non_empty=True)

        # Sales Invoice mode uses Sales Invoice Item + full filters + for_update=True
        sales_inv = _make_draft_pos_invoice(
            doctype="Sales Invoice",
            name="ACC-SINV-2026-00001",
            docstatus=1,
            items=[AttrDict(name="si-row-1", item_code="A")],
        )
        calls = []

        def capture_sales(doctype, filters=None, fieldname=None, **kwargs):
            calls.append((doctype, dict(filters or {}), fieldname, dict(kwargs)))
            return (("si-row-1",),)

        self.frappe.db.get_values = capture_sales
        self.creation._assert_persisted_invoice_items(sales_inv, require_non_empty=True)
        self.assertEqual(
            calls,
            [
                (
                    "Sales Invoice Item",
                    {
                        "parent": "ACC-SINV-2026-00001",
                        "parenttype": "Sales Invoice",
                        "parentfield": "items",
                        "docstatus": 1,
                    },
                    "name",
                    {"for_update": True},
                )
            ],
        )

        # Custom child doctype resolved from meta.get_field("items").options
        custom_meta_inv = _make_draft_pos_invoice(
            doctype="POS Invoice",
            name="ACC-PSINV-META-1",
            docstatus=1,
            items=[AttrDict(name="meta-row-1", item_code="A")],
            meta=types.SimpleNamespace(
                get_field=lambda fieldname: (
                    types.SimpleNamespace(options="Custom POS Line Item")
                    if fieldname == "items"
                    else None
                )
            ),
        )
        meta_calls = []

        def capture_meta(doctype, filters=None, fieldname=None, **kwargs):
            meta_calls.append((doctype, dict(filters or {}), fieldname, dict(kwargs)))
            return (("meta-row-1",),)

        self.frappe.db.get_values = capture_meta
        self.creation._assert_persisted_invoice_items(custom_meta_inv, require_non_empty=True)
        self.assertEqual(meta_calls[0][0], "Custom POS Line Item")
        self.assertEqual(meta_calls[0][3], {"for_update": True})

    def test_submit_in_background_job_rolls_back_when_persisted_item_count_is_zero(self):
        live_draft = _make_draft_pos_invoice()
        shift_doc = _make_open_shift()
        rollback_calls = []

        live_draft.save = lambda: live_draft
        live_draft.submit = lambda: setattr(live_draft, "docstatus", 1)

        self.frappe.db.exists = lambda doctype, name: doctype == "POS Opening Shift" and name == SHIFT_NAME
        self.frappe.db.rollback = lambda: rollback_calls.append(True)
        self.frappe.db.get_values = lambda doctype, filters=None, fieldname=None, **kwargs: (
            () if (filters or {}).get("docstatus") == 1 else ((LIVE_ROW_NAME,),)
        )
        self.frappe.get_doc = lambda doctype, name=None, **kwargs: (
            shift_doc if doctype == "POS Opening Shift" else live_draft
        )

        self.creation.submit_in_background_job(
            {
                "invoice": INVOICE_NAME,
                "doctype": "POS Invoice",
                "data": {},
                "payments": [],
                "user": "cashier@example.com",
                "opening_shift": SHIFT_NAME,
                "opening_user": "cashier@example.com",
            }
        )

        self.assertEqual(len(rollback_calls), 1)
        events = [call["args"][0] for call in self.frappe._publish_realtime_calls]
        self.assertIn("pos_invoice_submit_error", events)
        self.assertNotIn("pos_invoice_processed", events)
        error_payload = self.frappe._publish_realtime_calls[0]["args"][1]
        self.assertIn("POS Invoice Item", error_payload.get("error", ""))


if __name__ == "__main__":
    unittest.main()
