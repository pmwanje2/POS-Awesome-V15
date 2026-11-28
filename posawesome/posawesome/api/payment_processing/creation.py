import frappe
import erpnext
from frappe import _
from frappe.utils import nowdate, flt
from erpnext.accounts.party import get_party_account
from erpnext.accounts.utils import get_account_currency
from erpnext.setup.utils import get_exchange_rate
from posawesome.posawesome.api.erpnext_compat import resolve_get_party_bank_account
from posawesome.posawesome.api.idempotency import doctype_supports_client_request_id
from posawesome.posawesome.api.payment_processing.utils import (
    get_bank_cash_account,
    set_paid_amount_and_received_amount
)


def get_party_bank_account(*args, **kwargs):
    """Call the ERPNext-version-specific bank-account helper lazily."""
    return resolve_get_party_bank_account()(*args, **kwargs)


def create_payment_entry(
    company,
    amount,
    currency,
    mode_of_payment,
    customer=None,
    party=None,
    party_type="Customer",
    payment_type="Receive",
    exchange_rate=None,
    reference_date=None,
    reference_no=None,
    posting_date=None,
    cost_center=None,
    submit=0,
    client_request_id=None,
    bank_account=None,
    payment_currency=None,
    exchange_rate_source=None,
    allow_manual_rate=False,
    invoice_currency=None,
):
    date = nowdate() if not posting_date else posting_date
    party = party or customer

    # Cache commonly used values
    company_doc = frappe.get_cached_doc("Company", company)
    company_currency = company_doc.default_currency
    letter_head = company_doc.default_letter_head

    # Get party account and currency
    party_account = get_party_account(party_type, party, company)
    if not party_account:
        frappe.throw(_(
            "No default {0} account set for {1}"
        ).format("receivable" if party_type == "Customer" else "payable", party))
    party_account_currency = get_account_currency(party_account)

    # Get bank details BEFORE validation
    bank = get_bank_cash_account(company, mode_of_payment, bank_account=bank_account)
    if not bank:
        frappe.throw(_("Bank/Cash account not found for mode of payment {0}").format(mode_of_payment))

    payment_currency = payment_currency or bank.account_currency
    invoice_currency = invoice_currency or currency or party_account_currency

    manual_rate_active = bool(
        allow_manual_rate
        and exchange_rate_source == "manual"
        and exchange_rate
        and flt(exchange_rate) > 0
    )

    rate_purpose = "for_buying" if payment_type == "Pay" else "for_selling"

    account_to_company_rate = (
        1
        if bank.account_currency == company_currency
        else flt(
            get_exchange_rate(
                bank.account_currency,
                company_currency,
                date,
                rate_purpose,
            )
        )
    )
    if account_to_company_rate <= 0:
        frappe.throw(
            _("No exchange rate is available for {0} to {1} on {2}.").format(
                bank.account_currency, company_currency, date
            )
        )

    if payment_currency == company_currency:
        payment_to_company_rate = 1
    elif manual_rate_active:
        payment_to_company_rate = flt(exchange_rate)
    else:
        payment_to_company_rate = flt(
            get_exchange_rate(payment_currency, company_currency, date, rate_purpose)
        )
    if payment_to_company_rate <= 0:
        frappe.throw(
            _("No exchange rate is available for {0} to {1} on {2}.").format(
                payment_currency, company_currency, date
            )
        )

    if payment_currency == invoice_currency:
        payment_to_invoice_rate = 1
    elif manual_rate_active:
        invoice_to_company_rate = (
            1
            if invoice_currency == company_currency
            else flt(get_exchange_rate(invoice_currency, company_currency, date, rate_purpose))
        )
        if invoice_to_company_rate <= 0:
            frappe.throw(
                _("No exchange rate is available for {0} to {1} on {2}.").format(
                    invoice_currency, company_currency, date
                )
            )
        payment_to_invoice_rate = flt(
            payment_to_company_rate / invoice_to_company_rate
        )
    else:
        payment_to_invoice_rate = flt(
            get_exchange_rate(payment_currency, invoice_currency, date, rate_purpose)
        )
        if payment_to_invoice_rate <= 0:
            frappe.throw(
                _("No exchange rate is available for {0} to {1} on {2}.").format(
                    payment_currency, invoice_currency, date
                )
            )

    if payment_currency == bank.account_currency:
        payment_to_account_rate = 1
    elif manual_rate_active:
        payment_to_account_rate = flt(
            payment_to_company_rate / account_to_company_rate
        )
    else:
        payment_to_account_rate = flt(
            get_exchange_rate(payment_currency, bank.account_currency, date, rate_purpose)
        )
    if payment_to_account_rate <= 0:
        frappe.throw(
            _("No exchange rate is available for {0} to {1} on {2}.").format(
                payment_currency, bank.account_currency, date
            )
        )

    # Create payment entry with metadata only
    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = payment_type
    pe.company = company
    pe.cost_center = cost_center or erpnext.get_default_cost_center(company)
    pe.posting_date = date
    pe.mode_of_payment = mode_of_payment
    pe.party_type = party_type
    pe.party = party
    pe.paid_from = party_account if payment_type == "Receive" else bank.account
    pe.paid_to = party_account if payment_type == "Pay" else bank.account
    pe.paid_from_account_currency = (
        party_account_currency if payment_type == "Receive" else bank.account_currency
    )
    pe.paid_to_account_currency = party_account_currency if payment_type == "Pay" else bank.account_currency
    pe.letter_head = letter_head
    pe.reference_date = reference_date
    pe.reference_no = reference_no

    if client_request_id and doctype_supports_client_request_id("Payment Entry"):
        pe.posa_client_request_id = client_request_id

    # Set bank account if available
    if pe.party_type in ["Customer", "Supplier"]:
        party_bank_account = get_party_bank_account(pe.party_type, pe.party)
        if party_bank_account:
            pe.bank_account = party_bank_account
            pe.set_bank_account_data()

    # Let ERPNext fill missing metadata (party name, contact, defaults)
    pe.setup_party_account_field()
    pe.set_missing_values()

    # NOW override with our multi-currency calculations
    precision = flt(frappe.db.get_default("currency_precision") or 2)
    original_amount = flt(amount)
    bank_amount = flt(original_amount * payment_to_account_rate, precision)

    if party_account_currency != bank.account_currency:
        bank_to_base = account_to_company_rate
        party_to_base = flt(get_exchange_rate(party_account_currency, company_currency, date, rate_purpose))

        if payment_type == "Receive":
            pe.received_amount = bank_amount
            pe.source_exchange_rate = party_to_base
            pe.target_exchange_rate = bank_to_base
            pe.paid_amount = flt(bank_amount * bank_to_base / party_to_base, precision)
        else:  # Pay
            pe.paid_amount = bank_amount
            pe.source_exchange_rate = bank_to_base
            pe.target_exchange_rate = party_to_base
            pe.received_amount = flt(bank_amount * bank_to_base / party_to_base, precision)

        pe.base_paid_amount = flt(pe.paid_amount * pe.source_exchange_rate, precision)
        pe.base_received_amount = flt(pe.received_amount * pe.target_exchange_rate, precision)
    else:
        paid_amount, received_amount = set_paid_amount_and_received_amount(
            party_account_currency,
            bank,
            bank_amount,
            payment_type,
            None,
            account_to_company_rate,
        )
        pe.paid_amount = paid_amount
        pe.received_amount = received_amount
        pe.source_exchange_rate = account_to_company_rate
        pe.target_exchange_rate = account_to_company_rate
        pe.base_paid_amount = flt(
            paid_amount * account_to_company_rate, precision
        )
        pe.base_received_amount = flt(
            received_amount * account_to_company_rate, precision
        )

    metadata = {
        "posa_payment_currency": payment_currency,
        "posa_original_amount": original_amount,
        "posa_invoice_currency": invoice_currency,
        "posa_exchange_rate": payment_to_invoice_rate,
        "posa_company_exchange_rate": payment_to_company_rate,
        "posa_rate_date": date,
        "posa_rate_source": (
            "manual"
            if manual_rate_active
            else "same_currency"
            if payment_currency == company_currency
            else "currency_exchange"
        ),
        "posa_account_currency": bank.account_currency,
        "posa_account_amount": bank_amount,
    }
    for fieldname, value in metadata.items():
        if pe.meta.has_field(fieldname):
            pe.set(fieldname, value)

    if submit:
        pe.insert(ignore_permissions=True)
        pe.submit()

    return pe
