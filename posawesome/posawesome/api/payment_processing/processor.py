import frappe
import json
from frappe import _
from frappe.utils import nowdate, flt, fmt_money, cint
from erpnext.accounts.party import get_party_account
from erpnext.accounts.doctype.payment_reconciliation.payment_reconciliation import reconcile_dr_cr_note
from erpnext.accounts.utils import get_account_currency, reconcile_against_document
from erpnext.setup.utils import get_exchange_rate
from posawesome.posawesome.api.m_pesa import (
    get_authorized_mpesa_payment,
    submit_mpesa_payment,
)
from posawesome.posawesome.api.payment_processing.creation import create_payment_entry
from posawesome.posawesome.api.idempotency import (
    find_payment_entries_by_client_request_id,
    normalize_client_request_id,
)
from posawesome.posawesome.api.pos_access import get_authorized_pos_profile
from posawesome.posawesome.api.terminal_state import get_active_terminal_cashier


def _permission_denied(message):
    frappe.throw(message, getattr(frappe, "PermissionError", PermissionError))


def _profile_name(value):
    if isinstance(value, dict):
        return str(value.get("name") or "").strip()
    if value is not None and not isinstance(value, str) and hasattr(value, "get"):
        return str(value.get("name") or "").strip()
    return str(value or "").strip()


def _validate_pos_opening_shift(opening_name, profile_name, company):
    opening_doc = frappe.get_doc("POS Opening Shift", opening_name)
    if (
        str(_get_value(opening_doc, "pos_profile") or "").strip() != profile_name
        or str(_get_value(opening_doc, "company") or "").strip() != company
    ):
        _permission_denied(_("The POS Opening Shift does not belong to this POS Profile."))

    session_user = str(getattr(getattr(frappe, "session", None), "user", "") or "").strip()
    opening_user = str(_get_value(opening_doc, "user") or "").strip()
    if not session_user or (opening_user and opening_user != session_user):
        _permission_denied(_("The POS Opening Shift does not belong to the current user."))

    status = str(_get_value(opening_doc, "status") or "").strip()
    if (
        cint(_get_value(opening_doc, "docstatus")) != 1
        or (status and status != "Open")
        or _get_value(opening_doc, "pos_closing_shift")
    ):
        _permission_denied(_("The POS Opening Shift is not open."))
    return opening_doc


def _authorize_payment_request(data):
    requested_name = str(data.get("pos_profile_name") or "").strip()
    embedded_name = _profile_name(data.get("pos_profile"))
    if requested_name and embedded_name and requested_name != embedded_name:
        _permission_denied(_("The payment POS Profile values do not match."))

    profile_doc = get_authorized_pos_profile(
        requested_name or embedded_name or None,
        company=data.get("company"),
    )
    profile_name = str(profile_doc.get("name") or "").strip()
    company = str(profile_doc.get("company") or "").strip()
    if not profile_name or not company:
        _permission_denied(_("The authorized POS Profile must have a company."))

    cashier = get_active_terminal_cashier(profile_name)
    _validate_pos_opening_shift(
        data.get("pos_opening_shift_name"),
        profile_name,
        company,
    )

    data.pos_profile_name = profile_name
    data.company = company
    data.pos_profile = profile_doc
    return profile_doc, cashier


def _amounts_match(left, right):
    return abs(flt(left) - flt(right)) < 0.0001


def _get_entry_amount(entry):
    return flt(entry.get("paid_amount")) or flt(entry.get("received_amount"))


def _allowed_tender_currencies(profile_doc, invoice_currency, company_currency):
    configured = {
        row.get("currency")
        for row in (profile_doc.get("posa_allowed_currencies") or [])
        if row.get("currency") and cint(row.get("allow_for_payments"))
    }
    return configured or {invoice_currency, company_currency}


def _get_value(source, key, default=None):
    if isinstance(source, dict):
        return source.get(key, default)

    getter = getattr(source, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            pass

    return getattr(source, key, default)


def _expected_lookup_errors():
    errors = [PermissionError]

    for attr_name in ("DoesNotExistError", "PermissionError"):
        error_type = getattr(frappe, attr_name, None)
        if isinstance(error_type, type) and error_type not in errors:
            errors.append(error_type)

    return tuple(errors)


def _check_document_read_permission(doc):
    check_permission = getattr(doc, "check_permission", None)
    if callable(check_permission):
        check_permission("read")


def _assert_document_scope(doc, doctype, company, party_type, party, party_field=None):
    _check_document_read_permission(doc)
    if cint(_get_value(doc, "docstatus")) != 1:
        frappe.throw(_("{0} {1} must be submitted and not cancelled.").format(doctype, _get_value(doc, "name")))

    if str(_get_value(doc, "company") or "").strip() != str(company or "").strip():
        _permission_denied(_("{0} does not belong to the selected company.").format(doctype))

    party_field = party_field or ("supplier" if party_type == "Supplier" else "customer")
    if str(_get_value(doc, party_field) or "").strip() != str(party or "").strip():
        _permission_denied(_("{0} does not belong to the selected {1}.").format(doctype, party_type))
    return doc


def _assert_client_amount_not_overstated(row, authoritative_amount, label, allow_stale=False):
    if allow_stale:
        return

    requested_amount = _requested_reconciled_amount(row)
    if requested_amount > abs(flt(authoritative_amount)) + 0.0001:
        frappe.throw(
            _("The requested amount for {0} exceeds its current available amount.").format(label)
        )


def _load_authorized_outstanding_invoices(
    invoices,
    company,
    party_type,
    party,
    allow_completed_replay=False,
):
    expected_doctype = "Purchase Invoice" if party_type == "Supplier" else "Sales Invoice"
    party_type = "Supplier" if party_type == "Supplier" else "Customer"
    authorized = []

    for row in invoices or []:
        row = row or {}
        invoice_name = row.get("voucher_no") or row.get("name")
        requested_doctype = str(row.get("voucher_type") or expected_doctype).strip()
        if not invoice_name:
            frappe.throw(_("An invoice reference is required."))
        if requested_doctype != expected_doctype:
            _permission_denied(_("The selected invoice type is not valid for this party."))

        invoice_doc = _assert_document_scope(
            frappe.get_doc(expected_doctype, invoice_name),
            expected_doctype,
            company,
            party_type,
            party,
        )
        if cint(_get_value(invoice_doc, "is_return")):
            frappe.throw(_("Return invoice {0} cannot be selected as an outstanding invoice.").format(invoice_name))

        outstanding = flt(_get_value(invoice_doc, "outstanding_amount"))
        if outstanding < 0 or (outstanding == 0 and not allow_completed_replay):
            frappe.throw(_("Invoice {0} has no outstanding amount.").format(invoice_name))
        _assert_client_amount_not_overstated(
            row,
            outstanding,
            invoice_name,
            allow_stale=allow_completed_replay,
        )

        authorized.append(
            {
                "name": invoice_name,
                "voucher_no": invoice_name,
                "voucher_type": expected_doctype,
                "outstanding_amount": outstanding,
                "conversion_rate": flt(_get_value(invoice_doc, "conversion_rate")) or 1,
                "due_date": _get_value(invoice_doc, "due_date") or _get_value(invoice_doc, "posting_date"),
                "posting_date": _get_value(invoice_doc, "posting_date"),
                "currency": _get_value(invoice_doc, "currency"),
            }
        )

    return authorized


def _load_authorized_reconciliation_payments(
    selected_payments,
    company,
    party_type,
    party,
    allow_completed_replay=False,
):
    party_type = "Supplier" if party_type == "Supplier" else "Customer"
    expected_payment_type = "Pay" if party_type == "Supplier" else "Receive"
    authorized = []

    for row in selected_payments or []:
        row = row or {}
        payment_name = row.get("name") or row.get("voucher_no")
        if not payment_name:
            frappe.throw(_("A payment reference is required."))

        requested_type = str(row.get("voucher_type") or "").strip()
        is_credit_note = bool(cint(row.get("is_credit_note")) or requested_type == "Sales Invoice")
        requested_type = requested_type or ("Sales Invoice" if is_credit_note else "Payment Entry")
        if is_credit_note:
            if party_type != "Customer" or requested_type not in {"", "Sales Invoice"}:
                _permission_denied(_("The selected credit note is not valid for this party."))
            credit_note = _assert_document_scope(
                frappe.get_doc("Sales Invoice", payment_name),
                "Sales Invoice",
                company,
                "Customer",
                party,
            )
            if not cint(_get_value(credit_note, "is_return")):
                frappe.throw(_("Sales Invoice {0} is not a credit note.").format(payment_name))
            outstanding = flt(_get_value(credit_note, "outstanding_amount"))
            if outstanding > 0:
                frappe.throw(_("Credit note {0} has an invalid outstanding amount.").format(payment_name))
            _assert_client_amount_not_overstated(
                row,
                abs(outstanding),
                payment_name,
                allow_stale=allow_completed_replay,
            )
            authorized.append(
                {
                    "name": payment_name,
                    "voucher_type": "Sales Invoice",
                    "is_credit_note": 1,
                    "outstanding_amount": outstanding,
                }
            )
            continue

        if requested_type != "Payment Entry":
            _permission_denied(_("The selected payment reference type is not supported."))

        payment_doc = _assert_document_scope(
            frappe.get_doc("Payment Entry", payment_name),
            "Payment Entry",
            company,
            party_type,
            party,
            party_field="party",
        )
        if str(_get_value(payment_doc, "party_type") or "").strip() != party_type:
            _permission_denied(_("Payment Entry does not belong to the selected party type."))
        if str(_get_value(payment_doc, "payment_type") or "").strip() != expected_payment_type:
            _permission_denied(_("Payment Entry has an incompatible payment direction."))

        unallocated = flt(_get_value(payment_doc, "unallocated_amount"))
        if unallocated < 0:
            frappe.throw(_("Payment Entry {0} has an invalid unallocated amount.").format(payment_name))
        _assert_client_amount_not_overstated(
            row,
            unallocated,
            payment_name,
            allow_stale=allow_completed_replay,
        )
        authorized.append(
            {
                "name": payment_name,
                "voucher_type": "Payment Entry",
                "is_credit_note": 0,
                "unallocated_amount": unallocated,
            }
        )

    return authorized


def _build_payment_reconciliation_row(
    pe_doc,
    payment_name,
    invoice,
    party_type,
    party,
    unallocated,
    allocation,
    outstanding_before,
):
    is_supplier = party_type == "Supplier"
    return {
        "voucher_type": "Payment Entry",
        "voucher_no": payment_name,
        "voucher_detail_no": None,
        "against_voucher_type": invoice.get("voucher_type")
        or ("Purchase Invoice" if is_supplier else "Sales Invoice"),
        "against_voucher": invoice["name"],
        "account": _get_value(pe_doc, "paid_to" if is_supplier else "paid_from"),
        "party_type": party_type,
        "party": party,
        "dr_or_cr": "debit_in_account_currency" if is_supplier else "credit_in_account_currency",
        "unreconciled_amount": unallocated,
        "unadjusted_amount": unallocated,
        "allocated_amount": allocation,
        "grand_total": outstanding_before,
        "outstanding_amount": outstanding_before,
        "exchange_rate": 1,
        "due_date": invoice.get("due_date"),
        "is_advance": 0,
        "difference_amount": 0,
        "cost_center": _get_value(pe_doc, "cost_center"),
    }


def _to_public_entry(entry):
    return {
        "doctype": _get_value(entry, "doctype"),
        "name": _get_value(entry, "name"),
        "paid_amount": _get_value(entry, "paid_amount"),
        "received_amount": _get_value(entry, "received_amount"),
        "amount": _get_value(entry, "amount"),
        "posting_date": _get_value(entry, "posting_date"),
        "mode_of_payment": _get_value(entry, "mode_of_payment"),
        "party": _get_value(entry, "party"),
        "party_type": _get_value(entry, "party_type"),
        "docstatus": _get_value(entry, "docstatus"),
        "posa_client_request_id": _get_value(entry, "posa_client_request_id"),
        "unallocated_amount": _get_value(entry, "unallocated_amount"),
        "outstanding_amount": _get_value(entry, "outstanding_amount"),
    }



def _to_public_entries(entries):
    return [_to_public_entry(entry) for entry in entries or []]


def _requested_reconciled_amount(payment):
    for fieldname in ("allocated_amount", "amount", "unallocated_amount", "outstanding_amount"):
        amount = abs(flt(payment.get(fieldname)))
        if amount > 0:
            return amount
    return 0


def _get_currency_precision():
    try:
        precision = flt(frappe.db.get_default("currency_precision"))
    except Exception:
        precision = 0
    return precision or 2


def _build_completed_reconciliation_summaries(selected_payments, completed_documents):
    completed_by_name = {
        _get_value(document, "name"): document
        for document in completed_documents or []
        if _get_value(document, "name")
    }
    summaries = []

    for payment in selected_payments or []:
        payment_name = payment.get("name")
        document = completed_by_name.get(payment_name)
        if not document:
            continue

        allocated_amount = _requested_reconciled_amount(payment)
        if not allocated_amount and (
            _get_value(document, "doctype") == "Payment Entry"
            or payment.get("voucher_type") != "Sales Invoice"
        ):
            allocated_amount = max(
                flt(_get_value(document, "paid_amount")) - flt(_get_value(document, "unallocated_amount")),
                0,
            )

        summaries.append(
            {
                "payment_entry": payment_name,
                "allocated_amount": allocated_amount,
            }
        )

    return summaries


def _partition_payment_methods(existing_entries, payment_methods):
    unmatched_entries = list(existing_entries or [])
    matched_entries = []
    missing_payment_methods = []

    for payment_method in payment_methods or []:
        amount = flt(
            payment_method.get("original_amount")
            if payment_method.get("original_amount") not in (None, "")
            else payment_method.get("amount")
        )
        if not amount:
            continue

        mode_of_payment = payment_method.get("mode_of_payment")
        payment_currency = payment_method.get("payment_currency")
        matched_index = next(
            (
                index
                for index, entry in enumerate(unmatched_entries)
                if cint(entry.get("docstatus")) == 1
                and entry.get("mode_of_payment") == mode_of_payment
                and _amounts_match(
                    entry.get("posa_original_amount")
                    if entry.get("posa_original_amount") not in (None, "")
                    else _get_entry_amount(entry),
                    amount,
                )
                and (
                    not payment_currency
                    or not entry.get("posa_payment_currency")
                    or entry.get("posa_payment_currency") == payment_currency
                )
            ),
            None,
        )

        if matched_index is None:
            missing_payment_methods.append(payment_method)
            continue

        matched_entries.append(unmatched_entries.pop(matched_index))

    return matched_entries, missing_payment_methods, unmatched_entries


def _partition_completed_mpesa_payments(selected_mpesa_payments, customer):
    completed_entries = []
    pending_payments = []
    lookup_errors = _expected_lookup_errors()

    for mpesa_payment in selected_mpesa_payments or []:
        payment_name = mpesa_payment.get("name")
        if not payment_name:
            pending_payments.append(mpesa_payment)
            continue

        try:
            mpesa_doc = mpesa_payment.get("_doc") or frappe.get_doc(
                "Mpesa Payment Register", payment_name
            )
            linked_customer = getattr(mpesa_doc, "customer", None)
            payment_entry_name = getattr(mpesa_doc, "payment_entry", None)
            if (
                cint(getattr(mpesa_doc, "docstatus", 0)) == 1
                and payment_entry_name
                and (not linked_customer or linked_customer == customer)
            ):
                completed_entries.append(frappe.get_doc("Payment Entry", payment_entry_name))
                continue
        except lookup_errors:
            pending_payments.append(mpesa_payment)
            continue
        except Exception as err:
            frappe.log_error(
                "Unexpected M-Pesa replay lookup failure for {0}: {1}\nContext: {2}".format(
                    payment_name,
                    str(err),
                    json.dumps(mpesa_payment, default=str),
                ),
                "POS Payment Replay Check Error",
            )
            raise

        pending_payments.append(mpesa_payment)

    return completed_entries, pending_payments


def _load_authorized_mpesa_payments(
    selected_mpesa_payments,
    customer,
    party_type,
    profile_doc,
    allow_completed_replay=False,
):
    if selected_mpesa_payments and party_type != "Customer":
        _permission_denied(_("M-Pesa reconciliation is only available for Customer receipts."))

    authorized = []
    for row in selected_mpesa_payments or []:
        payment_name = (row or {}).get("name")
        if not payment_name:
            frappe.throw(_("An M-Pesa payment reference is required."))
        doc = get_authorized_mpesa_payment(
            payment_name,
            customer,
            profile_doc,
            allow_submitted=allow_completed_replay,
        )
        authorized.append(
            {
                "name": doc.name,
                "amount": flt(_get_value(doc, "transamount")),
                "company": _get_value(doc, "company"),
                "mode_of_payment": _get_value(doc, "mode_of_payment"),
                "_doc": doc,
            }
        )
    return authorized


def _partition_completed_reconciliations(selected_payments):
    completed_docs = []
    pending_payments = []
    lookup_errors = _expected_lookup_errors()

    for payment in selected_payments or []:
        payment_name = payment.get("name")
        if not payment_name:
            pending_payments.append(payment)
            continue

        is_credit_note = cint(payment.get("is_credit_note")) or payment.get("voucher_type") == "Sales Invoice"
        doctype = "Sales Invoice" if is_credit_note else "Payment Entry"

        try:
            payment_doc = frappe.get_doc(doctype, payment_name)
            if is_credit_note:
                if _amounts_match(abs(flt(getattr(payment_doc, "outstanding_amount", 0))), 0):
                    completed_docs.append(payment_doc)
                    continue
            elif flt(getattr(payment_doc, "unallocated_amount", 0)) <= 0:
                completed_docs.append(payment_doc)
                continue
        except lookup_errors:
            pending_payments.append(payment)
            continue
        except Exception as err:
            frappe.log_error(
                "Unexpected payment replay lookup failure for {0}: {1}\nContext: {2}".format(
                    payment_name,
                    str(err),
                    json.dumps(payment, default=str),
                ),
                "POS Payment Replay Check Error",
            )
            raise

        pending_payments.append(payment)

    return completed_docs, pending_payments


@frappe.whitelist()
def process_pos_payment(payload):
    data = json.loads(payload)
    data = frappe._dict(data)
    client_request_id = normalize_client_request_id(data.get("client_request_id"))

    party = data.get("party") or data.get("customer")
    party_type = data.get("party_type") or "Customer"
    payment_type = data.get("payment_type") or "Receive"

    # validate data
    if not party:
        frappe.throw(_("Party is required"))
    if not data.company:
        frappe.throw(_("Company is required"))
    if not data.currency:
        frappe.throw(_("Currency is required"))
    if not data.pos_profile_name:
        frappe.throw(_("POS Profile is required"))
    if not data.pos_opening_shift_name:
        frappe.throw(_("POS Opening Shift is required"))
    if party_type not in {"Customer", "Supplier"}:
        frappe.throw(_("Only Customer and Supplier payments are supported."))
    expected_payment_type = "Pay" if party_type == "Supplier" else "Receive"
    if payment_type != expected_payment_type:
        _permission_denied(_("The payment direction is not valid for the selected party type."))

    profile_doc, _authoritative_cashier = _authorize_payment_request(data)
    if not profile_doc.get("posa_use_pos_awesome_payments"):
        frappe.throw(_("POS Awesome Payments is not enabled for this POS Profile"))

    company = data.company
    currency = data.currency
    customer = party
    pos_opening_shift_name = data.pos_opening_shift_name
    allow_make_new_payments = profile_doc.get("posa_allow_make_new_payments")
    allow_reconcile_payments = profile_doc.get("posa_allow_reconcile_payments")
    allow_mpesa_reconcile_payments = profile_doc.get("posa_allow_mpesa_reconcile_payments")
    posting_date = data.get("posting_date") or nowdate()
    company_currency = (
        frappe.get_cached_value("Company", company, "default_currency") or currency
    )
    multi_currency_payments_enabled = bool(
        cint(profile_doc.get("posa_enable_multi_currency_payments"))
    )
    allowed_tender_currencies = _allowed_tender_currencies(
        profile_doc, currency, company_currency
    )
    selected_mpesa_payments = list(data.selected_mpesa_payments or [])
    selected_payments = list(data.selected_payments or [])
    payment_methods = list(data.payment_methods or [])
    existing_entries = find_payment_entries_by_client_request_id(
        client_request_id,
        company=company,
        party_type=party_type,
        party=party,
    )
    matched_existing_entries, pending_payment_methods, unmatched_existing_entries = (
        _partition_payment_methods(
            existing_entries,
            payment_methods,
        )
    )
    draft_entries = [entry for entry in unmatched_existing_entries if cint(entry.get("docstatus")) == 0]
    if draft_entries:
        draft_names = ", ".join(entry.get("name") for entry in draft_entries if entry.get("name"))
        frappe.throw(
            _("Payment request {0} has draft Payment Entry records pending review: {1}").format(
                client_request_id or _("unknown request"),
                draft_names or _("draft payment entries"),
            )
        )

    is_replay_attempt = bool(existing_entries)
    selected_invoices = _load_authorized_outstanding_invoices(
        data.selected_invoices,
        company,
        party_type,
        party,
        allow_completed_replay=is_replay_attempt,
    )
    selected_payments = _load_authorized_reconciliation_payments(
        selected_payments,
        company,
        party_type,
        party,
        allow_completed_replay=is_replay_attempt,
    )
    data.selected_invoices = selected_invoices
    data.selected_payments = selected_payments
    selected_mpesa_payments = _load_authorized_mpesa_payments(
        selected_mpesa_payments,
        customer,
        party_type,
        profile_doc,
        allow_completed_replay=is_replay_attempt,
    )

    completed_mpesa_entries, pending_mpesa_payments = ([], [])
    if is_replay_attempt and allow_mpesa_reconcile_payments and data.total_selected_mpesa_payments > 0:
        completed_mpesa_entries, pending_mpesa_payments = _partition_completed_mpesa_payments(
            selected_mpesa_payments,
            customer,
        )
    else:
        pending_mpesa_payments = selected_mpesa_payments

    completed_reconciliations, pending_selected_payments = ([], [])
    if is_replay_attempt and allow_reconcile_payments and data.total_selected_payments > 0:
        completed_reconciliations, pending_selected_payments = _partition_completed_reconciliations(
            selected_payments
        )
    else:
        pending_selected_payments = selected_payments

    completed_reconciliation_summaries = _build_completed_reconciliation_summaries(
        selected_payments,
        completed_reconciliations,
    )
    cached_entries = list(matched_existing_entries) + completed_mpesa_entries + completed_reconciliations
    if (
        existing_entries
        and not pending_payment_methods
        and not unmatched_existing_entries
        and not pending_mpesa_payments
        and not pending_selected_payments
    ):
        return {
            "new_payments_entry": _to_public_entries(matched_existing_entries),
            "all_payments_entry": _to_public_entries(cached_entries),
            "reconciled_payments": completed_reconciliation_summaries,
            "errors": [],
            "replayed": True,
        }

    # Work only with the authoritative server snapshot so client fields cannot
    # expand an invoice's allocatable amount or switch its voucher type.
    remaining_invoices = [dict(invoice) for invoice in selected_invoices]

    new_payments_entry = []
    all_payments_entry = list(cached_entries)
    reconciled_payments = list(completed_reconciliation_summaries)
    errors = []
    exchange_gain_loss_summary = []
    net_gain_loss = 0

    # first process mpesa payments
    if (
        allow_mpesa_reconcile_payments
        and len(pending_mpesa_payments) > 0
        and data.total_selected_mpesa_payments > 0
    ):
        for mpesa_payment in pending_mpesa_payments:
            try:
                new_mpesa_payment = submit_mpesa_payment(
                    mpesa_payment.get("name"),
                    customer,
                    pos_profile=profile_doc.get("name"),
                )
                new_payments_entry.append(new_mpesa_payment)
                all_payments_entry.append(new_mpesa_payment)
            except Exception as e:
                errors.append(str(e))

    # then reconcile selected payments with invoices
    if allow_reconcile_payments and len(pending_selected_payments) > 0 and data.total_selected_payments > 0:
        for pay in pending_selected_payments:
            payment_name = pay.get("name")
            is_credit_note = cint(pay.get("is_credit_note")) or pay.get("voucher_type") == "Sales Invoice"

            if is_credit_note:
                try:
                    credit_note_doc = frappe.get_doc("Sales Invoice", payment_name)
                    outstanding_credit = abs(flt(credit_note_doc.outstanding_amount))
                    if outstanding_credit <= 0:
                        errors.append(_("Credit note {0} is already fully allocated").format(payment_name))
                        continue

                    total_outstanding = sum(inv["outstanding_amount"] for inv in remaining_invoices)
                    if total_outstanding <= 0:
                        errors.append(
                            _("No outstanding invoices available for allocation of credit note {0}").format(
                                payment_name
                            )
                        )
                        continue

                    remaining_credit = outstanding_credit
                    note_entries = []
                    cost_center = getattr(credit_note_doc, "cost_center", None)
                    if not cost_center:
                        try:
                            cost_center = (
                                credit_note_doc.items[0].cost_center if credit_note_doc.items else None
                            )
                        except Exception:
                            cost_center = None

                    receivable_account = credit_note_doc.debit_to or get_party_account(
                        "Customer", customer, company
                    )

                    for inv in remaining_invoices:
                        if remaining_credit <= 0:
                            break
                        if inv["outstanding_amount"] <= 0:
                            continue

                        allocation = min(remaining_credit, inv["outstanding_amount"])
                        if allocation <= 0:
                            continue

                        note_entries.append(
                            frappe._dict(
                                {
                                    "voucher_type": "Sales Invoice",
                                    "voucher_no": payment_name,
                                    "voucher_detail_no": None,
                                    "against_voucher_type": inv.get("voucher_type") or "Sales Invoice",
                                    "against_voucher": inv["name"],
                                    "account": receivable_account,
                                    "party_type": "Customer",
                                    "party": customer,
                                    "dr_or_cr": "credit_in_account_currency",
                                    "unreconciled_amount": remaining_credit,
                                    "unadjusted_amount": outstanding_credit,
                                    "allocated_amount": allocation,
                                    "difference_amount": 0,
                                    "difference_account": None,
                                    "difference_posting_date": None,
                                    "exchange_rate": flt(credit_note_doc.conversion_rate) or 1,
                                    "debit_or_credit_note_posting_date": credit_note_doc.posting_date,
                                    "cost_center": cost_center,
                                    "currency": credit_note_doc.currency or currency,
                                }
                            )
                        )

                        inv["outstanding_amount"] -= allocation
                        remaining_credit -= allocation

                    allocated_credit = outstanding_credit - remaining_credit
                    if allocated_credit <= 0:
                        errors.append(_("No allocation made for credit note {0}").format(payment_name))
                        continue

                    reconcile_dr_cr_note(note_entries, company)

                    reconciled_payments.append(
                        {
                            "payment_entry": payment_name,
                            "allocated_amount": allocated_credit,
                        }
                    )
                    all_payments_entry.append(credit_note_doc)

                    if remaining_credit > 0:
                        errors.append(
                            _("Credit note {0} still has an unapplied balance of {1}").format(
                                payment_name,
                                fmt_money(remaining_credit, currency=credit_note_doc.currency or currency),
                            )
                        )

                except Exception as e:
                    errors.append(str(e))
                    frappe.log_error(
                        f"Error allocating credit note {payment_name}: {str(e)}",
                        "POS Payment Error",
                    )
                continue

            try:
                pe_doc = frappe.get_doc("Payment Entry", payment_name)
                unallocated = flt(pe_doc.unallocated_amount)
                if unallocated <= 0:
                    errors.append(_("Payment {0} is already fully allocated").format(payment_name))
                    continue

                total_outstanding = sum(inv["outstanding_amount"] for inv in remaining_invoices)
                if total_outstanding <= 0:
                    errors.append(
                        _("No outstanding invoices available for allocation of payment {0}").format(
                            payment_name
                        )
                    )
                    continue

                if unallocated > total_outstanding:
                    errors.append(
                        _("Allocation amount for payment {0} exceeds outstanding invoices").format(
                            payment_name
                        )
                    )
                    continue

                entry_list = []
                remaining_amount = unallocated
                for inv in remaining_invoices:
                    if remaining_amount <= 0:
                        break
                    if inv["outstanding_amount"] <= 0:
                        continue
                    allocation = min(remaining_amount, inv["outstanding_amount"])
                    if allocation <= 0:
                        continue
                    outstanding_before = inv["outstanding_amount"]
                    entry_list.append(
                        frappe._dict(
                            _build_payment_reconciliation_row(
                                pe_doc,
                                payment_name,
                                inv,
                                party_type,
                                party,
                                unallocated,
                                allocation,
                                outstanding_before,
                            )
                        )
                    )
                    inv["outstanding_amount"] -= allocation
                    remaining_amount -= allocation

                total_allocated = unallocated - remaining_amount
                if total_allocated <= 0:
                    errors.append(_("No allocation made for payment {0}").format(payment_name))
                    continue

                reconcile_against_document(entry_list)

                pe_doc.reload()

                allocated_after = unallocated - flt(pe_doc.unallocated_amount)
                reconciled_payments.append(
                    {
                        "payment_entry": payment_name,
                        "allocated_amount": allocated_after,
                    }
                )
                all_payments_entry.append(pe_doc)
            except Exception as e:
                errors.append(str(e))
                frappe.log_error(
                    f"Error allocating payment {payment_name}: {str(e)}",
                    "POS Payment Error",
                )

    # then process the new payments and allocate invoices
    if allow_make_new_payments and len(pending_payment_methods) > 0 and data.total_payment_methods > 0:
        for payment_method in pending_payment_methods:
            try:
                amount = flt(
                    payment_method.get("original_amount")
                    if payment_method.get("original_amount") not in (None, "")
                    else payment_method.get("amount")
                )
                if not amount:
                    continue
                mode_of_payment = payment_method.get("mode_of_payment")
                payment_currency = payment_method.get("payment_currency")
                if (
                    multi_currency_payments_enabled
                    and payment_currency
                    and payment_currency not in allowed_tender_currencies
                ):
                    frappe.throw(
                        _("Currency {0} is not allowed for payments in POS Profile {1}.").format(
                            payment_currency, profile_doc.get("name")
                        )
                    )
                payment_entry = create_payment_entry(
                    company=company,
                    currency=currency,
                    amount=amount,
                    mode_of_payment=mode_of_payment,
                    customer=customer,
                    party=party,
                    party_type=party_type,
                    payment_type=payment_type,
                    exchange_rate=payment_method.get("exchange_rate"),
                    posting_date=posting_date,
                    reference_no=data.get("reference_no") or pos_opening_shift_name,
                    reference_date=data.get("reference_date") or posting_date,
                    cost_center=profile_doc.get("cost_center"),
                    submit=0,
                    client_request_id=client_request_id,
                    bank_account=payment_method.get("bank_account"),
                    payment_currency=payment_currency,
                    exchange_rate_source=payment_method.get("rate_source"),
                    allow_manual_rate=bool(
                        cint(profile_doc.get("posa_allow_manual_payment_exchange_rate"))
                    ),
                    invoice_currency=currency,
                )

                party_account = get_party_account(party_type, party, company)
                party_account_currency = get_account_currency(party_account)

                first_inv = remaining_invoices[0] if remaining_invoices else {}
                exchange_rate_val = flt(payment_method.get("exchange_rate"))
                precision = _get_currency_precision()

                bank_currency = (
                    getattr(payment_entry, "paid_to_account_currency", None)
                    if payment_type == "Receive"
                    else getattr(payment_entry, "paid_from_account_currency", None)
                ) or currency
                bank_amount = (
                    getattr(payment_entry, "received_amount", None)
                    if payment_type == "Receive"
                    else getattr(payment_entry, "paid_amount", None)
                )
                bank_amount = flt(bank_amount or getattr(payment_entry, "amount", 0) or amount, precision)

                company_currency = (
                    frappe.get_cached_value("Company", company, "default_currency")
                    or getattr(payment_entry, "company_currency", None)
                    or currency
                )

                rate_purpose = "for_buying" if payment_type == "Pay" else "for_selling"

                # Convert bank amount to party currency ONCE
                if bank_currency == party_account_currency:
                    remaining_party = flt(bank_amount, precision)
                elif bank_currency == company_currency:
                    comp_to_party = flt(
                        get_exchange_rate(company_currency, party_account_currency, posting_date, rate_purpose)
                    )
                    remaining_party = flt(bank_amount * comp_to_party, precision)
                else:
                    bank_to_party = flt(
                        get_exchange_rate(bank_currency, party_account_currency, posting_date, rate_purpose)
                    )
                    remaining_party = flt(bank_amount * bank_to_party, precision)

                total_allocated = 0

                for inv in remaining_invoices:
                    if remaining_party <= 0:
                        break
                    if inv["outstanding_amount"] <= 0:
                        continue

                    voucher_type = inv.get("voucher_type") or "Sales Invoice"

                    # Fetch from DB for accurate party-currency amounts (ERPNext pattern)
                    inv_doc = frappe.get_cached_doc(voucher_type, inv["name"])
                    inv_currency = inv_doc.currency
                    inv_conv_rate = flt(inv_doc.conversion_rate)

                    # Get amounts in party account currency
                    company_currency = getattr(payment_entry, "company_currency", None) or company_currency
                    party_account_currency = (
                        getattr(payment_entry, "party_account_currency", None) or party_account_currency
                    )

                    # Calculate reference details as per ERPNext's get_reference_details
                    # All amounts must be in party account currency
                    if inv_currency == party_account_currency:
                        # Invoice currency matches party account currency
                        total_amount = flt(inv_doc.rounded_total or inv_doc.grand_total, precision)
                        outstanding_amount = flt(inv_doc.outstanding_amount, precision)
                        exchange_rate = inv_conv_rate
                    elif party_account_currency == company_currency:
                        # Party account in company currency — use base amounts
                        total_amount = flt(inv_doc.base_rounded_total or inv_doc.base_grand_total, precision)
                        outstanding_amount = flt(getattr(inv_doc, 'base_outstanding_amount', 0) or inv_doc.outstanding_amount * inv_conv_rate, precision)
                        exchange_rate = 1
                    else:
                        # Party account in third currency (different from both invoice and company)
                        inv_to_party = flt(
                            get_exchange_rate(inv_currency, party_account_currency, posting_date, rate_purpose)
                        )
                        total_amount = flt((inv_doc.rounded_total or inv_doc.grand_total) * inv_to_party, precision)
                        outstanding_amount = flt(inv_doc.outstanding_amount * inv_to_party, precision)
                        exchange_rate = flt(
                            get_exchange_rate(party_account_currency, company_currency, posting_date, rate_purpose)
                        )
                    inv_outstanding_party = outstanding_amount

                    inv_total_party = total_amount

                    if inv_outstanding_party <= 0:
                        continue

                    allocation = min(remaining_party, inv_outstanding_party)

                    if allocation <= 0:
                        continue

                    payment_entry.append(
                        "references",
                        {
                            "reference_doctype": voucher_type,
                            "reference_name": inv["name"],
                            "total_amount": total_amount,
                            "outstanding_amount": outstanding_amount,
                            "allocated_amount": allocation,
                            "exchange_rate": exchange_rate,
                        },
                    )

                    remaining_party -= allocation
                    total_allocated = flt(total_allocated + allocation, precision)

                payment_entry.total_allocated_amount = total_allocated

                # For multi-currency payments, set the party-currency amount
                # (paid_amount for Receive, received_amount for Pay) to total_allocated
                # so ERPNext's validate() → set_exchange_gain_loss() creates the
                # exchange gain/loss deduction row automatically when base amounts
                # differ due to exchange rate. For single-currency, keep existing behavior.
                is_multi_currency = party_account_currency != bank_currency

                if payment_type == "Receive":
                    if is_multi_currency:
                        payment_entry.paid_amount = flt(total_allocated, precision)
                        party_amount = flt(total_allocated, precision)
                        payment_entry.unallocated_amount = 0
                    else:
                        party_amount = flt(payment_entry.paid_amount or amount, precision)
                        payment_entry.unallocated_amount = flt(party_amount - total_allocated, precision)
                else:  # Pay
                    if is_multi_currency:
                        payment_entry.received_amount = flt(total_allocated, precision)
                        party_amount = flt(total_allocated, precision)
                        payment_entry.unallocated_amount = 0
                    else:
                        party_amount = flt(payment_entry.received_amount or amount, precision)
                        payment_entry.unallocated_amount = flt(party_amount - total_allocated, precision)

                invoice_exchange_rate = flt(first_inv.get("conversion_rate", 0))
                ref_names = ", ".join(r.reference_name for r in payment_entry.references)
                verb = "received" if payment_type == "Receive" else "paid"
                party_label = "from" if payment_type == "Receive" else "to"
                party_label_amount = party_amount
                invoice_type = "Sales Invoice" if payment_type == "Receive" else "Purchase Invoice"
                reference_no_str = data.get("reference_no") or pos_opening_shift_name
                reference_date_str = data.get("reference_date") or posting_date

                if invoice_exchange_rate and not _amounts_match(invoice_exchange_rate, exchange_rate_val):
                    rate_note = f"\nExchange Rate: 1 {bank_currency} = {exchange_rate_val} {party_account_currency}"
                else:
                    rate_note = ""

                payment_entry.remarks = (
                    f"Amount {bank_currency} {flt(bank_amount)} {verb} {party_label} {party}\n"
                    f"Transaction reference no {reference_no_str or ''} dated {reference_date_str or ''}\n"
                    f"Amount {party_account_currency} {flt(party_label_amount)} against {invoice_type} {ref_names}{rate_note}"
                )

                pe_exchange_rate = (
                    getattr(payment_entry, "source_exchange_rate", None)
                    if payment_type == "Receive"
                    else getattr(payment_entry, "target_exchange_rate", None)
                )

                # Build a map of reference rows by invoice name
                ref_map = {}
                for ref in payment_entry.references:
                    ref_map[ref.reference_name] = ref

                # Calculate gain/loss for ERPNext reconciliation and UI notification
                exchange_gain_loss_summary = []
                net_gain_loss = 0
                for inv in remaining_invoices:
                    inv_rate = flt(inv.get("conversion_rate")) or 1
                    if inv_rate and pe_exchange_rate and inv_rate != pe_exchange_rate:
                        ref = ref_map.get(inv["name"])
                        if ref and ref.allocated_amount:
                            # Gain/loss in company currency using ERPNext pattern:
                            # base at payment rate - base at reference/invoice rate
                            # allocated_amount is in party account currency
                            ref_rate = flt(ref.exchange_rate) or 1
                            allocated_base = flt(ref.allocated_amount * pe_exchange_rate, precision)
                            allocated_base_at_ref_rate = flt(ref.allocated_amount * ref_rate, precision)
                            gl_value = allocated_base - allocated_base_at_ref_rate
                            ref.exchange_gain_loss = flt(gl_value, precision)
                        else:
                            inv_doc = frappe.get_cached_doc(inv.get("voucher_type") or "Sales Invoice", inv["name"])
                            # Fallback to full invoice amount in company currency
                            allocated_base = flt(inv_doc.base_rounded_total or inv_doc.base_grand_total, precision)
                            gl_value = 0
                        if gl_value:
                            amount = abs(gl_value)
                            gl_type = "gain" if gl_value > 0 else "loss"
                            exchange_gain_loss_summary.append({
                                "payment": payment_entry.name,
                                "invoice": inv["name"],
                                "amount": amount,
                                "currency": company_currency,
                                "type": gl_type
                            })
                            net_gain_loss += gl_value

                # Add gain/loss info to Payment Entry remarks (with deduplication)
                if exchange_gain_loss_summary:
                    gl_parts = []
                    for item in exchange_gain_loss_summary:
                        gl_parts.append(f"{item['type'].title()}: {item['amount']} {item['currency']}")
                    gl_remark = f"Exchange Gain/Loss: {'; '.join(gl_parts)}"
                    if gl_remark not in (payment_entry.remarks or ""):
                        payment_entry.remarks += f"\n{gl_remark}"

                payment_entry.save(ignore_permissions=True)
                frappe.flags.ignore_permissions = True
                try:
                    payment_entry.submit()
                finally:
                    frappe.flags.ignore_permissions = False

                new_payments_entry.append(payment_entry)
                all_payments_entry.append(payment_entry)
            except Exception as e:
                errors.append(str(e))
                frappe.log_error(f"Error creating payment entry: {str(e)}", "POS Payment Error")

    # Old allocation logic disabled
    # then show the results
    msg = ""
    if len(new_payments_entry) > 0:
        msg += "<h4>New Payments</h4>"
        msg += "<table class='table table-bordered'>"
        msg += "<thead><tr><th>Payment Entry</th><th>Amount</th></tr></thead>"
        msg += "<tbody>"
        for payment_entry in new_payments_entry:
            msg += "<tr><td>{0}</td><td>{1}</td></tr>".format(
                payment_entry.get("name"),
                payment_entry.get("paid_amount") or payment_entry.get("amount"),
            )
        msg += "</tbody>"
        msg += "</table>"
    if len(reconciled_payments) > 0:
        msg += "<h4>Reconciled Payments</h4>"
        msg += "<table class='table table-bordered'>"
        msg += "<thead><tr><th>Payment Entry</th><th>Allocated</th></tr></thead>"
        msg += "<tbody>"
        for payment in reconciled_payments:
            msg += "<tr><td>{0}</td><td>{1}</td></tr>".format(
                payment.get("payment_entry"),
                payment.get("allocated_amount"),
            )
        msg += "</tbody>"
        msg += "</table>"
    if len(errors) > 0:
        msg += "<h4>Errors</h4>"
        msg += "<table class='table table-bordered'>"
        msg += "<thead><tr><th>Error</th></tr></thead>"
        msg += "<tbody>"
        for error in errors:
            msg += "<tr><td>{0}</td></tr>".format(error)
        msg += "</tbody>"
        msg += "</table>"
    if len(msg) > 0:
        frappe.msgprint(msg)

    return {
        "new_payments_entry": _to_public_entries(new_payments_entry),
        "all_payments_entry": _to_public_entries(all_payments_entry),
        "reconciled_payments": reconciled_payments,
        "errors": errors,
        "exchange_gain_loss_summary": exchange_gain_loss_summary,
        "net_gain_loss": net_gain_loss,
    }
