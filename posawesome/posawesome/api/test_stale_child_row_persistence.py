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
        self.frappe.get_doc = lambda doctype, name: live_draft

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

    def test_get_mutable_invoice_doc_preserves_valid_live_child_row_names(self):
        live_draft = _make_draft_pos_invoice()
        shift_doc = _make_open_shift()

        self.frappe.db.exists = lambda doctype, name: doctype == "POS Invoice" and name == INVOICE_NAME
        self.frappe.get_doc = lambda doctype, name: live_draft

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

    def test_sanitize_helper_discovers_dynamic_meta_table_fields(self):
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

        self.creation._sanitize_stale_child_row_names(live_draft, payload)

        self.assertEqual(payload["custom_posa_lines"][0].get("name"), "custom-live-1")
        self.assertFalse(payload["custom_posa_lines"][0].get("__islocal"))
        self.assertNotIn("name", payload["custom_posa_lines"][1])
        self.assertEqual(payload["custom_posa_lines"][1].get("__islocal"), 1)
        self.assertNotIn("parent", payload["custom_posa_lines"][1])

    def test_save_draft_retry_on_timestamp_mismatch_sanitizes_stale_child_row_names(self):
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

        self.frappe.db.get_value = lambda *args, **kwargs: None
        self.frappe.db.count = lambda doctype, filters: len(latest_doc.items)
        self.frappe.get_doc = lambda doctype, name: latest_doc

        result = self.creation._save_draft_with_latest_timestamp(stale_doc)

        self.assertIs(result, latest_doc)
        self.assertEqual(len(saved_items_snapshots), 1)
        saved_item = saved_items_snapshots[0][0]
        self.assertNotIn("name", saved_item)
        self.assertEqual(saved_item.get("__islocal"), 1)
        self.assertNotIn("parent", saved_item)

    def test_save_draft_concurrent_modified_drift_sanitizes_stale_child_row_names(self):
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

        self.frappe.db.get_value = lambda doctype, name, field: (
            "2026-10-30 10:00:05" if field == "modified" else None
        )
        self.frappe.db.count = lambda doctype, filters: 1
        self.frappe.get_doc = lambda doctype, name: latest_doc

        result = self.creation._save_draft_with_latest_timestamp(stale_doc)

        self.assertIs(result, latest_doc)
        self.assertEqual(len(saved_docs), 1)
        self.assertEqual(saved_docs[0][0], "latest")
        saved_item = saved_docs[0][1][0]
        self.assertNotIn("name", saved_item)
        self.assertEqual(saved_item.get("__islocal"), 1)

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
        self.frappe.db.count = lambda doctype, filters: (
            len(live_draft.items) if doctype == "POS Invoice Item" else 0
        )

        def fake_get_doc(doctype, name=None):
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

        count_calls = []

        def fake_count(doctype, filters=None):
            count_calls.append((doctype, dict(filters or {})))
            return 0

        self.frappe.db.count = fake_count

        with self.assertRaises(self.ValidationError):
            self.creation._save_draft_with_latest_timestamp(invoice_doc)

        self.assertEqual(
            count_calls,
            [("POS Invoice Item", {"parent": INVOICE_NAME})],
        )

    def test_save_draft_passes_when_db_item_count_matches(self):
        invoice_doc = _make_draft_pos_invoice()
        invoice_doc.save = lambda: invoice_doc

        count_calls = []

        def fake_count(doctype, filters=None):
            count_calls.append((doctype, dict(filters or {})))
            return 1

        self.frappe.db.count = fake_count

        result = self.creation._save_draft_with_latest_timestamp(invoice_doc)

        self.assertIs(result, invoice_doc)
        self.assertEqual(
            count_calls,
            [("POS Invoice Item", {"parent": INVOICE_NAME})],
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

        count_calls = []

        def fake_count(doctype, filters=None):
            count_calls.append((doctype, dict(filters or {})))
            if filters and filters.get("docstatus") == 1:
                return 0
            return len(live_draft.items)

        self.frappe.db.count = fake_count
        self.frappe.get_doc = lambda doctype, name=None: (
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
            ("POS Invoice Item", {"parent": INVOICE_NAME, "docstatus": 1}),
            count_calls,
        )

    def test_assert_persisted_invoice_items_enforces_non_empty_and_docstatus_on_submit(self):
        empty_submitted = _make_draft_pos_invoice(docstatus=1, items=[])
        self.frappe.db.count = lambda doctype, filters=None: 0
        with self.assertRaises(self.ValidationError):
            self.creation._assert_persisted_invoice_items(empty_submitted, require_non_empty=True)

        unsubmitted_doc = _make_draft_pos_invoice(docstatus=0)
        self.frappe.db.count = lambda doctype, filters=None: 1
        with self.assertRaises(self.ValidationError):
            self.creation._assert_persisted_invoice_items(unsubmitted_doc, require_non_empty=True)

        partial_submitted = _make_draft_pos_invoice(
            docstatus=1,
            items=[
                AttrDict(name="row-1", item_code="A"),
                AttrDict(name="row-2", item_code="B"),
            ],
        )
        self.frappe.db.count = lambda doctype, filters=None: 1
        with self.assertRaises(self.ValidationError):
            self.creation._assert_persisted_invoice_items(partial_submitted, require_non_empty=True)

        self.frappe.db.count = lambda doctype, filters=None: 2 if (filters or {}).get("docstatus") == 1 else 0
        self.creation._assert_persisted_invoice_items(partial_submitted, require_non_empty=True)

    def test_submit_in_background_job_rolls_back_when_persisted_item_count_is_zero(self):
        live_draft = _make_draft_pos_invoice()
        shift_doc = _make_open_shift()
        rollback_calls = []

        live_draft.save = lambda: live_draft
        live_draft.submit = lambda: setattr(live_draft, "docstatus", 1)

        self.frappe.db.exists = lambda doctype, name: doctype == "POS Opening Shift" and name == SHIFT_NAME
        self.frappe.db.rollback = lambda: rollback_calls.append(True)
        self.frappe.db.count = lambda doctype, filters=None: (
            0 if (filters or {}).get("docstatus") == 1 else len(live_draft.items)
        )
        self.frappe.get_doc = lambda doctype, name=None: (
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
