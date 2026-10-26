import importlib.util
import json
import pathlib
import sys
import types
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_ORIGINAL_MODULES = dict(sys.modules)


def tearDownModule():
    for name in list(sys.modules):
        if name.startswith(("frappe", "posawesome")) and name not in _ORIGINAL_MODULES:
            sys.modules.pop(name, None)
    for name, module in _ORIGINAL_MODULES.items():
        if name.startswith(("frappe", "posawesome")):
            sys.modules[name] = module


class AttrDict(dict):
    __getattr__ = dict.get

    def as_dict(self):
        return AttrDict(self)


def _load_exchange_module():
    frappe = types.ModuleType("frappe")
    frappe._ = lambda text: text
    frappe._dict = lambda value=None: AttrDict(value or {})
    frappe.parse_json = json.loads
    frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
    frappe.throw = lambda message, *args, **kwargs: (_ for _ in ()).throw(ValueError(message))
    frappe.db = types.SimpleNamespace(exists=lambda *args, **kwargs: False)
    sys.modules["frappe"] = frappe

    frappe_utils = types.ModuleType("frappe.utils")
    frappe_utils.cint = lambda value: int(value or 0)
    frappe_utils.cstr = lambda value: "" if value is None else str(value)
    frappe_utils.flt = lambda value, *_args, **_kwargs: float(value or 0)
    sys.modules["frappe.utils"] = frappe_utils

    frappe_model = types.ModuleType("frappe.model")
    frappe_document = types.ModuleType("frappe.model.document")
    frappe_document.Document = type("Document", (), {})
    sys.modules["frappe.model"] = frappe_model
    sys.modules["frappe.model.document"] = frappe_document

    creation = types.ModuleType("posawesome.posawesome.api.invoice_processing.creation")
    creation.submit_invoice = lambda *args, **kwargs: None
    sys.modules[creation.__name__] = creation

    pos_access = types.ModuleType("posawesome.posawesome.api.pos_access")
    pos_access.get_authorized_pos_profile = lambda *args, **kwargs: None
    sys.modules[pos_access.__name__] = pos_access

    module_name = "posawesome.posawesome.api.exchange"
    module_path = REPO_ROOT / "posawesome" / "posawesome" / "api" / "exchange.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_pos_item_exchange_module():
    module_name = "posawesome.posawesome.doctype.pos_item_exchange.pos_item_exchange"
    module_path = (
        REPO_ROOT
        / "posawesome"
        / "posawesome"
        / "doctype"
        / "pos_item_exchange"
        / "pos_item_exchange.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class TestExchangeValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exchange = _load_exchange_module()
        cls.exchange_doctype = _load_pos_item_exchange_module()

    def setUp(self):
        self.profile = AttrDict(
            name="Main POS",
            company="Example Co",
            posa_allow_return=1,
            posa_allow_item_exchange=1,
            create_pos_invoice_instead_of_sales_invoice=0,
        )
        self.return_invoice = AttrDict(
            doctype="Sales Invoice",
            company="Example Co",
            pos_profile="Main POS",
            customer="Customer A",
            currency="PKR",
            is_return=1,
            return_against="SINV-0001",
            items=[{"item_code": "OLD", "qty": -1}],
            payments=[{"amount": 0}],
        )
        self.sale_invoice = AttrDict(
            doctype="Sales Invoice",
            company="Example Co",
            pos_profile="Main POS",
            customer="Customer A",
            currency="PKR",
            is_return=0,
            items=[{"item_code": "NEW", "qty": 1}],
            payments=[{"amount": 500}],
        )

    def test_cashier_exchange_uses_system_credit_journal_without_journal_entry_permission(self):
        journals = []
        exchanges = []
        submitted = []
        frappe = sys.modules["frappe"]
        return_doc = AttrDict(
            name="SINV-RETURN",
            doctype="Sales Invoice",
            docstatus=1,
            is_return=1,
            return_against="SINV-0001",
            company="Example Co",
            pos_profile="Main POS",
            customer="Customer A",
            currency="PKR",
            outstanding_amount=-100,
            rounded_total=100,
            conversion_rate=1,
            posting_date="2026-10-24",
            debit_to="Debtors - EC",
            items=[],
            reload=lambda: None,
        )
        sale_doc = AttrDict(
            name="SINV-SALE",
            doctype="Sales Invoice",
            docstatus=1,
            is_return=0,
            company="Example Co",
            pos_profile="Main POS",
            customer="Customer A",
            currency="PKR",
            outstanding_amount=100,
            rounded_total=100,
            conversion_rate=1,
            posting_date="2026-10-24",
            debit_to="Debtors - EC",
            items=[],
            reload=lambda: None,
        )

        class FakeJournalEntry(AttrDict):
            def __init__(self, values):
                super().__init__(values)
                self.flags = types.SimpleNamespace(
                    ignore_permissions=False,
                    ignore_mandatory=False,
                    ignore_exchange_rate=False,
                    skip_remarks_creation=False,
                )
                self.accounts = [AttrDict(row) for row in values["accounts"]]

            def submit(self):
                submitted.append(self)
                if not self.flags.ignore_permissions:
                    raise PermissionError("Journal Entry submit permission required")
                self.name = "JV-EXCHANGE-1"
                self.docstatus = 1
                return_doc["outstanding_amount"] = 0
                sale_doc["outstanding_amount"] = 0

        class FakeExchange(AttrDict):
            def insert(self, ignore_permissions=False):
                self["name"] = "POS-EXCH-00001"
                self["insert_ignore_permissions"] = ignore_permissions

        def get_doc(*args):
            if len(args) == 1 and isinstance(args[0], dict):
                values = args[0]
                if values["doctype"] == "Journal Entry":
                    journal = FakeJournalEntry(values)
                    journals.append(journal)
                    return journal
                exchange_doc = FakeExchange(values)
                exchanges.append(exchange_doc)
                return exchange_doc
            documents = {return_doc.name: return_doc, sale_doc.name: sale_doc}
            return documents[args[1]]

        frappe.get_doc = get_doc
        frappe.get_cached_value = lambda *args: "PKR"
        frappe.get_all = lambda *args, **kwargs: [
            AttrDict(parent="JV-EXCHANGE-1", reference_name=return_doc.name),
            AttrDict(parent="JV-EXCHANGE-1", reference_name=sale_doc.name),
        ]
        self.exchange.get_authorized_pos_profile = lambda pos_profile, company=None: self.profile
        self.exchange._existing_exchange = lambda client_request_id: None
        self.exchange._validate_original_invoice = lambda return_invoice, profile: None

        def submit_invoice(payload, _data, submit_in_background=False):
            payload = json.loads(payload)
            return {
                "doctype": "Sales Invoice",
                "name": "SINV-RETURN" if payload.get("is_return") else "SINV-SALE",
            }

        self.exchange.submit_invoice = submit_invoice

        erpnext = types.ModuleType("erpnext")
        def get_default_cost_center(company):
            self.assertEqual(company, "Example Co")
            return "Main - EC"

        erpnext.get_default_cost_center = get_default_cost_center
        accounts = types.ModuleType("erpnext.accounts")
        doctype = types.ModuleType("erpnext.accounts.doctype")
        reconciliation = types.ModuleType(
            "erpnext.accounts.doctype.payment_reconciliation"
        )
        payment_reconciliation = types.ModuleType(
            "erpnext.accounts.doctype.payment_reconciliation.payment_reconciliation"
        )
        def reconcile_dr_cr_note(dr_cr_notes, company):
            for inv in dr_cr_notes:
                jv = frappe.get_doc(
                    {
                        "doctype": "Journal Entry",
                        "voucher_type": "Credit Note",
                        "posting_date": inv.get("debit_or_credit_note_posting_date"),
                        "company": company,
                        "accounts": [],
                    }
                )
                jv.is_system_generated = True
                jv.submit()

        payment_reconciliation.reconcile_dr_cr_note = reconcile_dr_cr_note
        sys.modules.update(
            {
                "erpnext": erpnext,
                "erpnext.accounts": accounts,
                "erpnext.accounts.doctype": doctype,
                "erpnext.accounts.doctype.payment_reconciliation": reconciliation,
                "erpnext.accounts.doctype.payment_reconciliation.payment_reconciliation": payment_reconciliation,
            }
        )

        result = self.exchange.submit_item_exchange(
            self.return_invoice,
            self.sale_invoice,
            pos_profile="Main POS",
            client_request_id="exchange-1",
        )

        self.assertEqual(result["exchange_reference"], "POS-EXCH-00001")
        self.assertEqual(len(exchanges), 1)
        self.assertEqual(exchanges[0].reconciliation_journal, "JV-EXCHANGE-1")
        self.assertEqual(len(journals), 1)
        journal = journals[0]
        self.assertEqual(journal.voucher_type, "Credit Note")
        self.assertEqual(journal.is_system_generated, 1)
        self.assertIsNone(journal.remark)
        self.assertTrue(journal.flags.ignore_permissions)
        self.assertTrue(journal.flags.ignore_mandatory)
        self.assertTrue(journal.flags.ignore_exchange_rate)
        self.assertTrue(journal.flags.skip_remarks_creation)
        self.assertEqual(
            journal.accounts,
            [
                AttrDict(
                    account="Debtors - EC",
                    party="Customer A",
                    party_type="Customer",
                    credit_in_account_currency=100,
                    reference_type="Sales Invoice",
                    reference_name="SINV-SALE",
                    cost_center="Main - EC",
                    exchange_rate=1,
                ),
                AttrDict(
                    account="Debtors - EC",
                    party="Customer A",
                    party_type="Customer",
                    debit_in_account_currency=100,
                    reference_type="Sales Invoice",
                    reference_name="SINV-RETURN",
                    cost_center="Main - EC",
                    exchange_rate=1,
                ),
            ],
        )
        self.assertEqual(len(submitted), 1)

    def test_exchange_refuses_unassigned_profile_before_invoice_or_journal(self):
        calls = []
        self.exchange.get_authorized_pos_profile = lambda *args, **kwargs: (_ for _ in ()).throw(
            PermissionError("POS Profile is not assigned to this user")
        )
        self.exchange.submit_invoice = lambda *args, **kwargs: calls.append("invoice")

        with self.assertRaisesRegex(PermissionError, "not assigned"):
            self.exchange.submit_item_exchange(
                self.return_invoice,
                self.sale_invoice,
                pos_profile="Unassigned POS",
                client_request_id="exchange-2",
            )

        self.assertEqual(calls, [])

    def test_exchange_refuses_unsubmitted_invoice_before_credit_journal(self):
        journals = []
        frappe = sys.modules["frappe"]
        return_doc = AttrDict(
            name="SINV-RETURN",
            doctype="Sales Invoice",
            docstatus=1,
            is_return=1,
            return_against="SINV-0001",
        )
        sale_doc = AttrDict(
            name="SINV-SALE",
            doctype="Sales Invoice",
            docstatus=0,
            is_return=0,
        )
        frappe.get_doc = lambda *args: (
            journals.append(args[0])
            if len(args) == 1
            else (return_doc if args[1] == "SINV-RETURN" else sale_doc)
        )
        self.exchange.get_authorized_pos_profile = lambda pos_profile, company=None: self.profile
        self.exchange._existing_exchange = lambda client_request_id: None
        self.exchange._validate_original_invoice = lambda return_invoice, profile: None
        self.exchange.submit_invoice = lambda payload, _data, submit_in_background=False: {
            "doctype": "Sales Invoice",
            "name": "SINV-RETURN" if json.loads(payload).get("is_return") else "SINV-SALE",
        }

        with self.assertRaisesRegex(ValueError, "submitted synchronously"):
            self.exchange.submit_item_exchange(
                self.return_invoice,
                self.sale_invoice,
                pos_profile="Main POS",
                client_request_id="exchange-3",
            )

        self.assertEqual(journals, [])

    def test_accepts_linked_sales_invoice_exchange(self):
        self.exchange._validate_exchange_payload(
            self.return_invoice,
            self.sale_invoice,
            self.profile,
        )

    def test_rejects_cross_customer_exchange(self):
        self.sale_invoice["customer"] = "Customer B"
        with self.assertRaisesRegex(ValueError, "same customer"):
            self.exchange._validate_exchange_payload(
                self.return_invoice,
                self.sale_invoice,
                self.profile,
            )

    def test_rejects_cash_refund_inside_return_credit(self):
        self.return_invoice["payments"] = [{"amount": -100}]
        with self.assertRaisesRegex(ValueError, "direct cash refund"):
            self.exchange._validate_exchange_payload(
                self.return_invoice,
                self.sale_invoice,
                self.profile,
            )

    def test_rejects_any_nonzero_cash_refund_inside_return_credit(self):
        self.return_invoice["payments"] = [{"amount": -0.0001}]
        with self.assertRaisesRegex(ValueError, "direct cash refund"):
            self.exchange._validate_exchange_payload(
                self.return_invoice,
                self.sale_invoice,
                self.profile,
            )

    def test_rejects_exchange_when_returns_are_disabled(self):
        self.profile["posa_allow_return"] = 0
        with self.assertRaisesRegex(ValueError, "Returns are not enabled"):
            self.exchange._validate_exchange_payload(
                self.return_invoice,
                self.sale_invoice,
                self.profile,
            )

    def test_rejects_pos_invoice_mode_until_reconciliation_is_supported(self):
        self.profile["create_pos_invoice_instead_of_sales_invoice"] = 1
        with self.assertRaisesRegex(ValueError, "requires the POS Profile to create Sales Invoices"):
            self.exchange._validate_exchange_payload(
                self.return_invoice,
                self.sale_invoice,
                self.profile,
            )

    def test_accepts_fully_closed_exchange_settlement(self):
        return_doc = AttrDict(outstanding_amount=0)
        sale_doc = AttrDict(outstanding_amount=0)
        self.exchange._validate_final_settlement(return_doc, sale_doc, 1500, 1600)

    def test_accepts_remaining_customer_credit(self):
        return_doc = AttrDict(outstanding_amount=-400)
        sale_doc = AttrDict(outstanding_amount=0)
        self.exchange._validate_final_settlement(return_doc, sale_doc, 2000, 1600)

    def test_rejects_unpaid_replacement_difference(self):
        return_doc = AttrDict(outstanding_amount=0)
        sale_doc = AttrDict(outstanding_amount=100)
        with self.assertRaisesRegex(ValueError, "did not close correctly"):
            self.exchange._validate_final_settlement(return_doc, sale_doc, 1500, 1600)

    def test_settlement_tolerance_uses_document_currency_precision(self):
        class PrecisionDoc(AttrDict):
            def precision(self, _fieldname):
                return 3

        within_rounding = PrecisionDoc(outstanding_amount=0.0004)
        closed_return = PrecisionDoc(outstanding_amount=0)
        self.exchange._validate_final_settlement(
            closed_return,
            within_rounding,
            50,
            50,
        )

        outside_rounding = PrecisionDoc(outstanding_amount=0.0006)
        with self.assertRaisesRegex(ValueError, "did not close correctly"):
            self.exchange._validate_final_settlement(
                closed_return,
                outside_rounding,
                50,
                50,
            )

    def test_accepts_submitted_return_and_replacement_documents(self):
        return_doc = AttrDict(is_return=1, return_against="SINV-0001")
        sale_doc = AttrDict(is_return=0)
        self.exchange._validate_submitted_exchange_documents(
            return_doc,
            sale_doc,
            "SINV-0001",
        )

    def test_rejects_exchange_when_return_document_is_a_normal_sale(self):
        return_doc = AttrDict(is_return=0, return_against="SINV-0001")
        sale_doc = AttrDict(is_return=0)
        with self.assertRaisesRegex(ValueError, "not created as a return invoice"):
            self.exchange._validate_submitted_exchange_documents(
                return_doc,
                sale_doc,
                "SINV-0001",
            )

    def test_rejects_exchange_when_return_link_is_wrong(self):
        return_doc = AttrDict(is_return=1, return_against="SINV-OTHER")
        sale_doc = AttrDict(is_return=0)
        with self.assertRaisesRegex(ValueError, "not linked"):
            self.exchange._validate_submitted_exchange_documents(
                return_doc,
                sale_doc,
                "SINV-0001",
            )

    def test_existing_exchange_must_match_authorized_pos_profile(self):
        exchange_doc = AttrDict(company="Other Co", pos_profile="Other POS")
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.exchange._authorize_existing_exchange(exchange_doc, self.profile)

    def test_recovers_completed_exchange_by_idempotency_key(self):
        exchange_doc = AttrDict(
            name="POS-EXCH-00001",
            status="Completed",
            company="Example Co",
            pos_profile="Main POS",
            invoice_type="Sales Invoice",
            return_invoice="SINV-RETURN",
            replacement_invoice="SINV-NEW",
            return_total=50,
            sale_total=50,
            difference_amount=0,
            allocated_amount=50,
            settlement_type="Even Exchange",
        )
        return_doc = AttrDict(name="SINV-RETURN", items=[])
        sale_doc = AttrDict(name="SINV-NEW", items=[])
        frappe = sys.modules["frappe"]
        self.exchange._existing_exchange = lambda request_id: exchange_doc
        self.exchange.get_authorized_pos_profile = lambda *args, **kwargs: self.profile
        frappe.get_doc = lambda doctype, name: (
            return_doc if name == "SINV-RETURN" else sale_doc
        )

        result = self.exchange.get_item_exchange("exchange-request-1", "Main POS")

        self.assertEqual(result["exchange_reference"], "POS-EXCH-00001")
        self.assertEqual(result["exchange_status"], "Completed")
        self.assertEqual(result["return_invoice"], "SINV-RETURN")
        self.assertEqual(result["replacement_invoice"], "SINV-NEW")
        self.assertEqual(result["exchange_summary"]["currency_precision"], 2)

    def test_cancel_exchange_reverses_linked_documents_in_dependency_order(self):
        cancelled = []

        class LinkedDoc(AttrDict):
            def cancel(self):
                cancelled.append((self.doctype, self.name))
                self["docstatus"] = 2

        class ExchangeDoc(AttrDict):
            def check_permission(self, permission_type):
                self["checked_permission"] = permission_type

            def db_set(self, fieldname, value, update_modified=False):
                self[fieldname] = value
                self["update_modified"] = update_modified

        exchange_doc = ExchangeDoc(
            name="POS-EXCH-00001",
            status="Completed",
            invoice_type="Sales Invoice",
            reconciliation_journal="JV-0001",
            replacement_invoice="SINV-NEW",
            return_invoice="SINV-RETURN",
        )
        documents = {
            ("Journal Entry", "JV-0001"): LinkedDoc(
                doctype="Journal Entry",
                name="JV-0001",
                docstatus=1,
                flags=types.SimpleNamespace(ignore_permissions=False),
            ),
            ("Sales Invoice", "SINV-NEW"): LinkedDoc(
                doctype="Sales Invoice",
                name="SINV-NEW",
                docstatus=1,
                flags=types.SimpleNamespace(ignore_permissions=False),
            ),
            ("Sales Invoice", "SINV-RETURN"): LinkedDoc(
                doctype="Sales Invoice",
                name="SINV-RETURN",
                docstatus=1,
                flags=types.SimpleNamespace(ignore_permissions=False),
            ),
        }
        frappe = sys.modules["frappe"]
        frappe.get_doc = lambda doctype, name: (
            exchange_doc
            if (doctype, name) == ("POS Item Exchange", exchange_doc.name)
            else documents[(doctype, name)]
        )
        frappe.db.exists = lambda doctype, name: (doctype, name) in documents
        frappe.db.get_value = lambda doctype, name, fieldname: documents[
            (doctype, name)
        ].get(fieldname)

        result = self.exchange_doctype.cancel_item_exchange(exchange_doc.name)

        self.assertEqual(
            cancelled,
            [
                ("Journal Entry", "JV-0001"),
                ("Sales Invoice", "SINV-NEW"),
                ("Sales Invoice", "SINV-RETURN"),
            ],
        )
        self.assertEqual(exchange_doc.status, "Cancelled")
        self.assertEqual(exchange_doc.checked_permission, "delete")
        self.assertTrue(exchange_doc.update_modified)
        self.assertEqual(result["status"], "Cancelled")
        self.assertTrue(
            all(doc.flags.ignore_permissions for doc in documents.values())
        )

    def test_delete_requires_cancelled_exchange_with_no_submitted_documents(self):
        exchange_doc = AttrDict(
            status="Completed",
            invoice_type="Sales Invoice",
            replacement_invoice="SINV-NEW",
        )
        with self.assertRaisesRegex(ValueError, "Cancel the item exchange"):
            self.exchange_doctype.POSItemExchange.on_trash(exchange_doc)

        exchange_doc["status"] = "Cancelled"
        frappe = sys.modules["frappe"]
        frappe.db.exists = lambda doctype, name: True
        frappe.db.get_value = lambda doctype, name, fieldname: 1
        with self.assertRaisesRegex(ValueError, "linked documents are submitted"):
            self.exchange_doctype.POSItemExchange.on_trash(exchange_doc)

        frappe.db.get_value = lambda doctype, name, fieldname: 2
        self.exchange_doctype.POSItemExchange.on_trash(exchange_doc)


if __name__ == "__main__":
    unittest.main()
