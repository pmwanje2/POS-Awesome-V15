import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, nowdate


LOCKED_FIELD = "retailmind_locked_for_sale"
NON_DISCOUNTABLE_FIELD = "retailmind_non_discountable"
CONTROLLED_FIELD = "retailmind_controlled_item"
SHORT_NAME_FIELD = "retailmind_short_name"
PHARMACY_SEARCH_FIELDS = (
    "retailmind_old_pos_pack",
    "retailmind_old_pos_company_code",
    "retailmind_old_pos_generic_code",
    "retailmind_old_pos_generic_name",
    "retailmind_old_pos_rack",
    "retailmind_units_per_pack",
)
_APPLICABLE_FOR_INVOICE_FIELDS = {
    "Customer": "customer",
    "Customer Group": "customer_group",
    "Territory": "territory",
    "Sales Partner": "sales_partner",
    "Campaign": "campaign",
}
SALE_FLOOR_POLICY_FIELDS = (
    "posa_enable_below_cost_guard",
    "posa_below_cost_action",
    "posa_minimum_margin_percentage",
    "posa_missing_cost_action",
)
SALE_FLOOR_ACTIONS = {"Block", "POS Supervisor Override", "Warning Only"}
MISSING_COST_ACTIONS = {"Allow", "Block"}
# Floor errors that no below-cost action (Warning Only, supervisor override) relaxes.
UNOVERRIDABLE_FLOOR_REASONS = {"missing_buying_price", "invalid_uom"}


def item_control_fields():
    return [
        LOCKED_FIELD,
        NON_DISCOUNTABLE_FIELD,
        CONTROLLED_FIELD,
        SHORT_NAME_FIELD,
    ]


def item_has_field(fieldname):
    try:
        return bool(frappe.get_meta("Item").has_field(fieldname))
    except Exception:
        return False


def installed_item_control_fields():
    return [field for field in item_control_fields() if item_has_field(field)]


def installed_item_search_fields():
    return [
        field
        for field in [*item_control_fields(), *PHARMACY_SEARCH_FIELDS]
        if item_has_field(field)
    ]


def get_item_control_flags(item_codes):
    codes = sorted({str(code).strip() for code in item_codes or [] if code})
    if not codes:
        return {}

    fields = ["item_code", "item_name", *installed_item_control_fields()]
    rows = frappe.get_all(
        "Item",
        filters={"item_code": ["in", codes]},
        fields=fields,
        limit_page_length=len(codes),
    )
    return {row.get("item_code"): row for row in rows if row.get("item_code")}


def collect_item_sale_control_errors(items, is_return=False):
    if is_return:
        return []

    item_rows = list(items or [])
    flags_by_code = get_item_control_flags(
        [row.get("item_code") if hasattr(row, "get") else None for row in item_rows]
    )
    errors = []

    for row in item_rows:
        item_code = row.get("item_code") if hasattr(row, "get") else None
        if not item_code:
            continue

        flags = flags_by_code.get(item_code) or {}
        item_name = row.get("item_name") or flags.get("item_name") or item_code

        if flags.get(LOCKED_FIELD):
            errors.append(
                {
                    "item_code": item_code,
                    "item_name": item_name,
                    "policy": "block",
                    "reason": "locked_for_sale",
                    "message": _("Item {0} is locked for sale.").format(item_name),
                }
            )
            continue

        if flags.get(NON_DISCOUNTABLE_FIELD) and (
            abs(flt(row.get("discount_percentage"))) > 0.0001
            or abs(flt(row.get("discount_amount"))) > 0.0001
        ):
            errors.append(
                {
                    "item_code": item_code,
                    "item_name": item_name,
                    "policy": "block",
                    "reason": "non_discountable",
                    "message": _("Item {0} does not allow POS discounts.").format(item_name),
                }
            )

    return errors


def _resolve_buying_price_list(pos_profile=None):
    if pos_profile and frappe.get_meta("POS Profile").has_field("buying_price_list"):
        profile_buying = frappe.db.get_value("POS Profile", pos_profile, "buying_price_list")
        if profile_buying:
            return profile_buying
    return (
        frappe.db.get_single_value("Buying Settings", "buying_price_list")
        or frappe.db.get_value("Price List", {"buying": 1}, "name")
        or ("Standard Buying" if frappe.db.exists("Price List", "Standard Buying") else None)
    )


def _get_buying_price_rows(item_codes, pos_profile=None):
    codes = sorted({str(code).strip() for code in item_codes or [] if code})
    if not codes:
        return None, []

    price_list = _resolve_buying_price_list(pos_profile)
    if not price_list:
        return None, []

    rows = frappe.get_all(
        "Item Price",
        filters={
            "price_list": price_list,
            "item_code": ["in", codes],
            "buying": 1,
        },
        fields=[
            "name",
            "item_code",
            "price_list",
            "price_list_rate",
            "currency",
            "uom",
            "customer",
            "supplier",
            "valid_from",
            "valid_upto",
            "modified",
        ],
        order_by="valid_from desc, modified desc",
        limit_page_length=0,
    )
    return price_list, list(rows or [])


def resolve_sale_floor_policy(pos_profile=None):
    """Return POS Profile policy with defaults that preserve the existing hard block."""

    values = {}
    if pos_profile:
        meta = frappe.get_meta("POS Profile")
        installed_fields = [field for field in SALE_FLOOR_POLICY_FIELDS if meta.has_field(field)]
        if installed_fields:
            values = frappe.db.get_value(
                "POS Profile",
                pos_profile,
                installed_fields,
                as_dict=True,
            ) or {}

    enabled_value = values.get("posa_enable_below_cost_guard")
    enabled = True if enabled_value in (None, "") else bool(cint(enabled_value))
    action = cstr(values.get("posa_below_cost_action") or "Block").strip()
    if action not in SALE_FLOOR_ACTIONS:
        action = "Block"
    missing_cost_action = cstr(values.get("posa_missing_cost_action") or "Allow").strip()
    if missing_cost_action not in MISSING_COST_ACTIONS:
        missing_cost_action = "Allow"
    return {
        "enabled": enabled,
        "action": action,
        "minimum_margin_percentage": max(
            flt(values.get("posa_minimum_margin_percentage")), 0
        ),
        "missing_cost_action": missing_cost_action,
    }
def _is_within_validity_window(row, posting_date):
    """Return whether a row's valid_from/valid_upto dates include the posting date."""

    date_text = str(posting_date or nowdate())[:10]
    valid_from = str(row.get("valid_from") or "")[:10]
    valid_upto = str(row.get("valid_upto") or "")[:10]
    return (not valid_from or valid_from <= date_text) and (
        not valid_upto or valid_upto >= date_text
    )


def _is_price_row_valid(row, posting_date):
    """Return whether an Item Price is general-purpose and active on the invoice date."""

    if row.get("customer") or row.get("supplier"):
        return False
    return _is_within_validity_window(row, posting_date)


def _get_item_uom_context(item_rows):
    """Load authoritative stock UOMs, conversion factors, and item cost metadata."""

    codes = sorted(
        {
            str(row.get("item_code")).strip()
            for row in item_rows
            if hasattr(row, "get") and row.get("item_code")
        }
    )
    if not codes:
        return {}, {}, {}

    item_meta = frappe.get_all(
        "Item",
        filters={"item_code": ["in", codes]},
        fields=[
            "item_code",
            "stock_uom",
            "variant_of",
            "is_stock_item",
            "last_purchase_rate",
            "valuation_rate",
        ],
        limit_page_length=len(codes),
    )
    item_info = {}
    stock_uoms = {}
    templates = {}
    for row in item_meta or []:
        code = row.get("item_code")
        if not code:
            continue
        item_info[code] = row
        stock_uoms[code] = row.get("stock_uom")
        if row.get("variant_of"):
            templates[code] = row.get("variant_of")
    conversions = frappe.get_all(
        "UOM Conversion Detail",
        filters={"parent": ["in", sorted({*codes, *templates.values()})], "parenttype": "Item"},
        fields=["parent", "uom", "conversion_factor"],
        limit_page_length=0,
    )
    conversion_factors = {
        (row.get("parent"), row.get("uom")): flt(row.get("conversion_factor"))
        for row in conversions or []
        if row.get("parent") and row.get("uom") and flt(row.get("conversion_factor")) > 0
    }
    # A variant without its own UOM row uses its template's, as ERPNext's
    # get_conversion_factor does.
    for item_code, template in templates.items():
        for (parent, uom), factor in list(conversion_factors.items()):
            if parent == template:
                conversion_factors.setdefault((item_code, uom), factor)
    for item_code, stock_uom in stock_uoms.items():
        if stock_uom:
            conversion_factors[(item_code, stock_uom)] = 1.0
    return stock_uoms, conversion_factors, item_info


def _get_bin_valuation_map(item_rows, pos_profile=None, invoice_doc=None):
    """Load authoritative warehouse Bin.valuation_rate (SAP VPRS / moving average)."""

    codes = sorted(
        {
            str(row.get("item_code")).strip()
            for row in item_rows
            if hasattr(row, "get") and row.get("item_code")
        }
    )
    if not codes:
        return {}, None

    default_warehouse = None
    if invoice_doc and hasattr(invoice_doc, "get"):
        default_warehouse = invoice_doc.get("set_warehouse") or invoice_doc.get("warehouse")
    if not default_warehouse and pos_profile:
        default_warehouse = frappe.db.get_value("POS Profile", pos_profile, "warehouse")

    warehouses = sorted(
        {
            str(row.get("warehouse") or default_warehouse).strip()
            for row in item_rows
            if hasattr(row, "get") and (row.get("warehouse") or default_warehouse)
        }
    )
    if not warehouses:
        return {}, default_warehouse

    bin_rows = frappe.get_all(
        "Bin",
        filters={
            "item_code": ["in", codes],
            "warehouse": ["in", warehouses],
        },
        fields=["item_code", "warehouse", "valuation_rate"],
        limit_page_length=0,
    )
    bin_map = {}
    for row in bin_rows or []:
        code = row.get("item_code")
        wh = row.get("warehouse")
        val = flt(row.get("valuation_rate"))
        if code and wh and val > 0:
            bin_map[(code, wh)] = val
    return bin_map, default_warehouse


def _resolve_floor_rates(
    rate,
    source_currency,
    invoice_currency,
    company,
    company_currency,
    posting_date,
    invoice_conversion_rate=None,
):
    """Return ``(invoice_rate, base_rate, invoice_to_company)`` for a floor candidate."""

    rate = flt(rate)
    if rate <= 0 or not source_currency or not invoice_currency:
        return rate, rate, 1.0

    if source_currency == invoice_currency:
        if not company_currency or invoice_currency == company_currency:
            return rate, rate, 1.0
        invoice_to_company = flt(invoice_conversion_rate)
        if invoice_to_company <= 0:
            from erpnext.setup.utils import get_exchange_rate

            invoice_to_company = flt(
                get_exchange_rate(invoice_currency, company_currency, posting_date)
            )
        if invoice_to_company <= 0:
            frappe.throw(
                _(
                    "Cannot validate the buying floor because the exchange rate between {0} and {1} is missing."
                ).format(invoice_currency, company_currency)
            )
        return rate, rate * invoice_to_company, invoice_to_company

    if not company_currency:
        company_currency = (
            frappe.db.get_value("Company", company, "default_currency") if company else None
        )
    if not company_currency:
        frappe.throw(
            _("Cannot validate the buying floor because the company currency is missing.")
        )

    from erpnext.setup.utils import get_exchange_rate

    source_to_company = (
        1.0
        if source_currency == company_currency
        else flt(get_exchange_rate(source_currency, company_currency, posting_date))
    )
    invoice_to_company = (
        1.0
        if invoice_currency == company_currency
        else flt(invoice_conversion_rate)
        or flt(get_exchange_rate(invoice_currency, company_currency, posting_date))
    )
    if source_to_company <= 0 or invoice_to_company <= 0:
        frappe.throw(
            _(
                "Cannot validate the buying floor because the exchange rate between {0} and {1} is missing."
            ).format(source_currency, invoice_currency)
        )
    base_rate = rate * source_to_company
    return base_rate / invoice_to_company, base_rate, invoice_to_company


def _convert_floor_to_invoice_currency(
    rate,
    source_currency,
    invoice_currency,
    company,
    posting_date,
    invoice_conversion_rate=None,
):
    """Normalize a buying-list rate into the invoice currency via company currency."""

    company_currency = (
        frappe.db.get_value("Company", company, "default_currency") if company else None
    )
    invoice_rate, _base_rate, _invoice_to_company = _resolve_floor_rates(
        rate,
        source_currency,
        invoice_currency,
        company,
        company_currency,
        posting_date,
        invoice_conversion_rate=invoice_conversion_rate,
    )
    return invoice_rate


def resolve_buying_floors(items, pos_profile=None, invoice_doc=None):
    """Resolve authoritative per-line cost floors across buying list, warehouse valuation, and LPR.

    Client-provided ``trade_price``/``buying_rate``/``conversion_factor`` values are
    intentionally ignored.
    Evaluates (1) active Buying Item Price, (2) warehouse Bin.valuation_rate (stock items,
    matching SAP VPRS / ERPNext SellingController), and (3) Item.last_purchase_rate,
    selecting the highest normalized floor in company currency.
    """

    item_rows = list(items or [])
    if not item_rows:
        return {}

    price_list, price_rows = _get_buying_price_rows(
        [row.get("item_code") for row in item_rows if hasattr(row, "get")],
        pos_profile=pos_profile,
    )

    posting_date = (
        invoice_doc.get("posting_date") if invoice_doc and hasattr(invoice_doc, "get") else None
    ) or nowdate()
    invoice_currency = (
        invoice_doc.get("currency") if invoice_doc and hasattr(invoice_doc, "get") else None
    )
    company = invoice_doc.get("company") if invoice_doc and hasattr(invoice_doc, "get") else None
    invoice_conversion_rate = (
        invoice_doc.get("conversion_rate")
        if invoice_doc and hasattr(invoice_doc, "get")
        else None
    )
    company_currency = (
        frappe.db.get_value("Company", company, "default_currency") if company else None
    )
    price_list_currency = (
        frappe.db.get_value("Price List", price_list, "currency") if price_list else None
    )
    stock_uoms, conversion_factors, item_info = _get_item_uom_context(item_rows)
    bin_valuations, default_warehouse = _get_bin_valuation_map(
        item_rows, pos_profile=pos_profile, invoice_doc=invoice_doc
    )

    rows_by_item = {}
    for price_row in price_rows or []:
        if flt(price_row.get("price_list_rate")) <= 0 or not _is_price_row_valid(
            price_row, posting_date
        ):
            continue
        if price_list_currency and price_row.get("currency") not in (None, "", price_list_currency):
            continue
        rows_by_item.setdefault(price_row.get("item_code"), []).append(price_row)

    floors = {}
    for index, row in enumerate(item_rows):
        item_code = row.get("item_code") if hasattr(row, "get") else None
        if not item_code:
            continue
        selected_uom = row.get("uom") or stock_uoms.get(item_code)
        stock_uom = stock_uoms.get(item_code) or row.get("stock_uom") or selected_uom
        row_qty = flt(row.get("qty"))
        row_stock_qty = flt(row.get("stock_qty"))
        stock_qty_factor = (
            abs(row_stock_qty) / abs(row_qty) if row_qty > 0 and row_stock_qty > 0 else 0.0
        )
        if not selected_uom or selected_uom == stock_uom:
            # StockController.reset_conversion_factor forces a stock-UOM row's conversion_factor
            # to 1.0, while an explicit stock_qty > qty still moves more stock units.
            master_factor = 1.0
            factor = max(1.0, stock_qty_factor)
        else:
            # ERPNext's get_conversion_factor order: the Item, its variant template,
            # then the global UOM Conversion Factor table.
            master_factor = conversion_factors.get((item_code, selected_uom))
            if master_factor is None:
                from erpnext.stock.doctype.item.item import get_uom_conv_factor

                master_factor = flt(get_uom_conv_factor(selected_uom, stock_uom))
            if master_factor <= 0:
                floors[index] = {"invalid_uom": True, "uom": selected_uom}
                continue
            # The stock ledger moves qty x conversion_factor (or explicit stock_qty), so never
            # floor on fewer stock units than will actually leave the shelf.
            factor = max(master_factor, flt(row.get("conversion_factor")), stock_qty_factor)

        candidates_floors = []

        # 1) Authoritative Buying Price List
        price_candidates = rows_by_item.get(item_code, [])
        exact = next(
            (candidate for candidate in price_candidates if candidate.get("uom") == selected_uom),
            None,
        )
        base = next(
            (
                candidate
                for candidate in price_candidates
                if candidate.get("uom") in (stock_uom, None, "")
            ),
            None,
        )
        selected_price = exact or base
        if selected_price:
            floor = flt(selected_price.get("price_list_rate"))
            floor *= factor / master_factor if exact else factor
            source_currency = (
                selected_price.get("currency") or price_list_currency or invoice_currency
            )
            normalized_floor, base_floor, inv_to_co = _resolve_floor_rates(
                floor,
                source_currency,
                invoice_currency or source_currency,
                company,
                company_currency,
                posting_date,
                invoice_conversion_rate=invoice_conversion_rate,
            )
            if normalized_floor is not None and normalized_floor > 0:
                candidates_floors.append(
                    {
                        "rate": normalized_floor,
                        "base_rate": base_floor,
                        "invoice_to_company": inv_to_co,
                        "company_currency": company_currency or invoice_currency or source_currency,
                        "source_rate": flt(selected_price.get("price_list_rate")),
                        "source_currency": source_currency,
                        "invoice_currency": invoice_currency or source_currency,
                        "uom": selected_uom,
                        "price_list": price_list,
                        "item_price": selected_price.get("name"),
                        "cost_source": "buying_price_list",
                        "cost_label": "Buying Price",
                        "warehouse": None,
                    }
                )

        meta = item_info.get(item_code) or {}
        is_stock_item = bool(cint(meta.get("is_stock_item", 1)))
        cost_currency = company_currency or invoice_currency

        # 2) Warehouse Bin.valuation_rate (stock items only, matching ERPNext validate_selling_price)
        warehouse = row.get("warehouse") or default_warehouse
        if is_stock_item and warehouse:
            bin_val = flt(bin_valuations.get((item_code, warehouse)))
            if bin_val > 0:
                normalized_val, base_val, inv_to_co = _resolve_floor_rates(
                    bin_val * factor,
                    cost_currency,
                    invoice_currency or cost_currency,
                    company,
                    company_currency,
                    posting_date,
                    invoice_conversion_rate=invoice_conversion_rate,
                )
                if normalized_val is not None and normalized_val > 0:
                    candidates_floors.append(
                        {
                            "rate": normalized_val,
                            "base_rate": base_val,
                            "invoice_to_company": inv_to_co,
                            "company_currency": company_currency or invoice_currency or cost_currency,
                            "source_rate": bin_val,
                            "source_currency": cost_currency,
                            "invoice_currency": invoice_currency or cost_currency,
                            "uom": selected_uom,
                            "price_list": None,
                            "item_price": None,
                            "cost_source": "warehouse_valuation",
                            "cost_label": "Warehouse Valuation",
                            "warehouse": warehouse,
                        }
                    )

        # 3) Item.last_purchase_rate (matching ERPNext validate_selling_price)
        lpr = flt(meta.get("last_purchase_rate"))
        if lpr > 0:
            normalized_lpr, base_lpr, inv_to_co = _resolve_floor_rates(
                lpr * factor,
                cost_currency,
                invoice_currency or cost_currency,
                company,
                company_currency,
                posting_date,
                invoice_conversion_rate=invoice_conversion_rate,
            )
            if normalized_lpr is not None and normalized_lpr > 0:
                candidates_floors.append(
                    {
                        "rate": normalized_lpr,
                        "base_rate": base_lpr,
                        "invoice_to_company": inv_to_co,
                        "company_currency": company_currency or invoice_currency or cost_currency,
                        "source_rate": lpr,
                        "source_currency": cost_currency,
                        "invoice_currency": invoice_currency or cost_currency,
                        "uom": selected_uom,
                        "price_list": None,
                        "item_price": None,
                        "cost_source": "last_purchase_rate",
                        "cost_label": "Last Purchase Rate",
                        "warehouse": warehouse,
                    }
                )

        if candidates_floors:
            floors[index] = max(candidates_floors, key=lambda entry: flt(entry.get("base_rate")))
    return floors


def _applied_pricing_rule_names(value):
    """Parse a row's ``pricing_rules`` exactly as ERPNext's get_applied_pricing_rules does.

    ERPNext compares rule names unstripped, so a padded name is not the rule it applied.
    """

    text = cstr(value)
    if not text:
        return set()
    if text.startswith("["):
        try:
            names = json.loads(text)
        except ValueError:
            return set()
    else:
        names = text.split(",")
    return {cstr(name) for name in names if cstr(name)}


def _field_precision(doc, fieldname):
    """Frappe's field precision when ``doc`` is a Document, else no rounding."""

    precision = getattr(doc, "precision", None)
    return precision(fieldname) if callable(precision) else None


def _resolve_currency_precision(row, invoice_doc, fieldname="base_net_rate"):
    """Resolve Frappe currency precision from the item row or parent invoice Document."""

    row_precision = getattr(row, "precision", None)
    if callable(row_precision):
        val = row_precision(fieldname)
        if val is not None:
            return cint(val)
    doc_precision = getattr(invoice_doc, "precision", None)
    if callable(doc_precision):
        try:
            val = doc_precision(fieldname, row)
        except TypeError:
            val = doc_precision(fieldname)
        if val is not None:
            return cint(val)
    return None


def _parse_item_tax_rate(row):
    """Parse ``item_tax_rate`` JSON/dict overrides mapping account_head -> rate."""

    raw = row.get("item_tax_rate") if hasattr(row, "get") else None
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _row_inclusive_tax_params(row, invoice_doc):
    """Return ``(tax_fraction, per_unit_tax)`` for tax-inclusive rows on ``invoice_doc``."""

    if not invoice_doc or not hasattr(invoice_doc, "get"):
        return 0.0, 0.0
    taxes = invoice_doc.get("taxes") or []
    if not taxes:
        return 0.0, 0.0

    item_tax_map = _parse_item_tax_rate(row)
    tax_fractions = []
    running_fraction = 0.0
    per_unit_tax = 0.0

    for tax in taxes:
        if not hasattr(tax, "get"):
            tax_fractions.append(0.0)
            continue
        if not cint(tax.get("included_in_print_rate")):
            tax_fractions.append(0.0)
            continue
        charge_type = cstr(tax.get("charge_type")).strip()
        account_head = tax.get("account_head")
        tax_rate = (
            flt(item_tax_map[account_head])
            if account_head and account_head in item_tax_map
            else flt(tax.get("rate"))
        )
        if charge_type == "On Net Total":
            current_fraction = tax_rate / 100.0
            running_fraction += current_fraction
            tax_fractions.append(current_fraction)
        elif charge_type == "On Previous Row Amount":
            ref_idx = cint(tax.get("row_id")) - 1
            prev_fraction = tax_fractions[ref_idx] if 0 <= ref_idx < len(tax_fractions) else 0.0
            current_fraction = (tax_rate / 100.0) * prev_fraction
            running_fraction += current_fraction
            tax_fractions.append(current_fraction)
        elif charge_type == "On Previous Row Total":
            ref_idx = cint(tax.get("row_id")) - 1
            prev_total_fraction = (
                1.0 + sum(tax_fractions[: ref_idx + 1])
                if 0 <= ref_idx < len(tax_fractions)
                else 1.0
            )
            current_fraction = (tax_rate / 100.0) * prev_total_fraction
            running_fraction += current_fraction
            tax_fractions.append(current_fraction)
        elif charge_type == "On Item Quantity":
            per_unit_tax += tax_rate
            tax_fractions.append(0.0)
        else:
            tax_fractions.append(0.0)

    return max(running_fraction, 0.0), max(per_unit_tax, 0.0)


def _rule_matches_invoice_party(rule, invoice_doc, has_doc):
    """Return whether a Pricing Rule's ``applicable_for`` matches the invoice."""

    applicable_for = cstr(rule.get("applicable_for")).strip()
    if not applicable_for:
        return True
    invoice_field = _APPLICABLE_FOR_INVOICE_FIELDS.get(applicable_for)
    if not invoice_field:
        return False
    expected = cstr(rule.get(invoice_field)).strip()
    if not expected:
        return True
    actual = cstr(invoice_doc.get(invoice_field)).strip() if has_doc else ""
    return actual == expected


def _server_applied_free_row_indexes(item_rows, invoice_doc=None):
    """Return the free rows ERPNext itself applied from an active promotional rule.

    ERPNext marks a promotional free row with ``is_free_item`` and the single
    applying rule name (``get_product_discount_rule``). Unless the invoice ignores
    pricing rules, every validate rewrites each paid row's ``pricing_rules`` from
    ERPNext's own evaluation, re-sizes the first free row per (item, rule) and
    removes free rows of unmatched Transaction rules. A client can forge a free
    row, but not an active selling Product rule for that item nor the rule's
    application to a paid row, so only those free rows skip the cost floor.
    """

    has_doc = invoice_doc and hasattr(invoice_doc, "get")
    if has_doc and cint(invoice_doc.get("ignore_pricing_rule")):
        return set()

    # Raw value: ERPNext keys free rows on the unstripped Small Text, so a padded
    # name escapes both remove_free_item and the free-row re-sizing.
    free_rules = {
        index: cstr(row.get("pricing_rules"))
        for index, row in enumerate(item_rows)
        if hasattr(row, "get")
        and row.get("item_code")
        and cint(row.get("is_free_item"))
        and cstr(row.get("pricing_rules"))
    }
    if not free_rules:
        return set()

    rules = {
        rule.get("name"): rule
        for rule in frappe.get_all(
            "Pricing Rule",
            filters={"name": ["in", sorted(set(free_rules.values()))]},
            fields=[
                "name",
                "disable",
                "selling",
                "price_or_product_discount",
                "apply_on",
                "same_item",
                "free_item",
                "free_qty",
                "is_recursive",
                "free_item_uom",
                "currency",
                "applicable_for",
                "customer",
                "customer_group",
                "territory",
                "sales_partner",
                "campaign",
                "company",
                "valid_from",
                "valid_upto",
                "min_qty",
                "max_qty",
                "min_amt",
                "max_amt",
                "condition",
            ],
            limit_page_length=0,
        )
        or []
        if rule.get("name")
    }
    paid_items_by_rule = {}
    paid_qty = paid_amount = 0.0
    for row in item_rows:
        if (
            hasattr(row, "get")
            and row.get("item_code")
            and not cint(row.get("is_free_item"))
            and flt(row.get("qty")) > 0
        ):
            paid_qty += flt(row.get("qty"))
            # ERPNext's doc.total: rounded line amounts, then a rounded sum.
            paid_amount += flt(
                flt(row.get("rate")) * flt(row.get("qty")), _field_precision(row, "amount")
            )
            for rule_name in _applied_pricing_rule_names(row.get("pricing_rules")):
                paid_items_by_rule.setdefault(rule_name, set()).add(row.get("item_code"))
    paid_amount = flt(paid_amount, _field_precision(invoice_doc, "total"))

    posting_date = invoice_doc.get("posting_date") if has_doc else None
    company = invoice_doc.get("company") if has_doc else None
    invoice_currency = cstr(invoice_doc.get("currency")).strip() if has_doc else ""
    authorized = set()
    seen = set()
    for index, rule_name in free_rules.items():
        free_row = item_rows[index]
        item_code = free_row.get("item_code")
        rule = rules.get(rule_name)
        if (
            not rule
            or (item_code, rule_name) in seen
            or cint(rule.get("disable"))
            or not cint(rule.get("selling"))
            or rule.get("price_or_product_discount") != "Product"
            or (rule.get("company") and rule.get("company") != company)
            or (
                cstr(rule.get("currency")).strip()
                and invoice_currency
                and cstr(rule.get("currency")).strip() != invoice_currency
            )
            or not _rule_matches_invoice_party(rule, invoice_doc, has_doc)
            # Pricing Rule shares valid_from/valid_upto date semantics with Item Price,
            # while party eligibility is checked via _rule_matches_invoice_party above.
            or not _is_within_validity_window(rule, posting_date)
            # ERPNext evaluates a condition over doc.as_dict(), forged free rows and
            # the totals they inflate included, so a conditional rule fails closed.
            or cstr(rule.get("condition"))
        ):
            continue
        free_item_uom = cstr(rule.get("free_item_uom")).strip()
        row_uom = cstr(free_row.get("uom") or free_row.get("stock_uom")).strip()
        if free_item_uom and row_uom and row_uom != free_item_uom:
            continue
        free_qty = flt(rule.get("free_qty"))
        if not cint(rule.get("is_recursive")) and free_qty > 0 and flt(free_row.get("qty")) > free_qty:
            continue
        if rule.get("apply_on") == "Transaction":
            # ERPNext's own check (filter_pricing_rules_for_qty_amount) counts free
            # rows in total_qty/total, so re-check the thresholds on paid rows only.
            # A zero max_* is unbounded, as in ERPNext.
            gives_item = (
                item_code == rule.get("free_item")
                and paid_qty >= flt(rule.get("min_qty"))
                and (not flt(rule.get("max_qty")) or paid_qty <= flt(rule.get("max_qty")))
                and paid_amount >= flt(rule.get("min_amt"))
                and (not flt(rule.get("max_amt")) or paid_amount <= flt(rule.get("max_amt")))
            )
        else:
            paid_items = paid_items_by_rule.get(rule_name) or set()
            gives_item = bool(paid_items) and (
                item_code == rule.get("free_item")
                or (cint(rule.get("same_item")) and item_code in paid_items)
            )
        if gives_item:
            seen.add((item_code, rule_name))
            authorized.add(index)
    return authorized


def collect_below_buying_price_errors(
    items, is_return=False, pos_profile=None, invoice_doc=None, policy=None
):
    if is_return:
        return []

    item_rows = list(items or [])
    policy = policy or resolve_sale_floor_policy(pos_profile)
    if not policy.get("enabled"):
        return []
    buying_floors = resolve_buying_floors(
        item_rows,
        pos_profile=pos_profile,
        invoice_doc=invoice_doc,
    )
    errors = []
    has_doc = bool(invoice_doc and hasattr(invoice_doc, "get"))
    invoice_discount_percentage = (
        max(flt(invoice_doc.get("additional_discount_percentage")), 0) if has_doc else 0
    )
    invoice_discount_amount = max(flt(invoice_doc.get("discount_amount")), 0) if has_doc else 0
    apply_discount_on = (
        cstr(invoice_doc.get("apply_discount_on") or "Grand Total").strip()
        if has_doc
        else "Grand Total"
    )
    promotional_free_rows = _server_applied_free_row_indexes(item_rows, invoice_doc)

    row_tax_params = {}
    eligible_row_total = 0.0
    eligible_net_total = 0.0
    for index, item in enumerate(item_rows):
        if not hasattr(item, "get"):
            continue
        tax_fraction, per_unit_tax = _row_inclusive_tax_params(item, invoice_doc)
        row_tax_params[index] = (tax_fraction, per_unit_tax)
        if (
            not item.get("is_return")
            and not item.get("posa_is_replace")
            and index not in promotional_free_rows
            and flt(item.get("qty")) > 0
        ):
            qty = abs(flt(item.get("qty")))
            gross_rate = abs(flt(item.get("rate")))
            pre_discount_net = max((gross_rate - per_unit_tax) / (1.0 + tax_fraction), 0.0)
            eligible_row_total += gross_rate * qty
            eligible_net_total += pre_discount_net * qty

    for index, row in enumerate(item_rows):
        item_code = row.get("item_code") if hasattr(row, "get") else None
        if not item_code:
            continue
        if (
            row.get("posa_is_replace")
            or index in promotional_free_rows
            or flt(row.get("qty")) < 0
        ):
            continue

        floor_info = buying_floors.get(index) or {}
        if floor_info.get("invalid_uom"):
            item_name = row.get("item_name") or item_code
            errors.append(
                {
                    "item_code": item_code,
                    "item_name": item_name,
                    "policy": "block",
                    "reason": "invalid_uom",
                    "uom": floor_info.get("uom"),
                    "message": _(
                        "Item {0} cannot be sold in UOM {1} because the Item has no conversion factor for it."
                    ).format(item_name, floor_info.get("uom")),
                }
            )
            continue
        floor = flt(floor_info.get("rate"))
        if floor <= 0:
            if policy.get("missing_cost_action") == "Block":
                item_name = row.get("item_name") or item_code
                errors.append(
                    {
                        "item_code": item_code,
                        "item_name": item_name,
                        "policy": "block",
                        "reason": "missing_buying_price",
                        "message": _(
                            "Item {0} cannot be sold because no valid buying floor is available."
                        ).format(item_name),
                    }
                )
            continue

        invoice_to_company = flt(floor_info.get("invoice_to_company")) or 1.0
        base_floor = flt(floor_info.get("base_rate")) or (floor * invoice_to_company)
        minimum_margin = flt(policy.get("minimum_margin_percentage"))
        minimum_selling_rate = floor * (1 + minimum_margin / 100)
        base_minimum_selling_rate = base_floor * (1 + minimum_margin / 100)

        tax_fraction, per_unit_tax = row_tax_params.get(index, (0.0, 0.0))
        gross_rate = abs(flt(row.get("rate")))
        if apply_discount_on == "Net Total":
            pre_discount_net = max((gross_rate - per_unit_tax) / (1.0 + tax_fraction), 0.0)
            if invoice_discount_percentage > 0:
                computed_net_rate = pre_discount_net * max(
                    1.0 - invoice_discount_percentage / 100.0, 0.0
                )
            elif invoice_discount_amount > 0 and eligible_net_total > 0:
                computed_net_rate = pre_discount_net * max(
                    1.0 - invoice_discount_amount / eligible_net_total, 0.0
                )
            else:
                computed_net_rate = pre_discount_net
        else:
            discounted_gross = gross_rate
            if invoice_discount_percentage > 0:
                discounted_gross *= max(1.0 - invoice_discount_percentage / 100.0, 0.0)
            elif invoice_discount_amount > 0 and eligible_row_total > 0:
                discounted_gross *= max(1.0 - invoice_discount_amount / eligible_row_total, 0.0)
            computed_net_rate = max((discounted_gross - per_unit_tax) / (1.0 + tax_fraction), 0.0)

        net_rate_precision = _resolve_currency_precision(row, invoice_doc, "net_rate")
        base_precision = _resolve_currency_precision(row, invoice_doc, "base_net_rate")

        selling_rate = computed_net_rate
        if row.get("net_rate") not in (None, ""):
            row_net_rate = abs(flt(row.get("net_rate")))
            if row_net_rate > 0 or gross_rate <= 0:
                if net_rate_precision is not None:
                    if flt(row_net_rate, net_rate_precision) < flt(
                        selling_rate, net_rate_precision
                    ):
                        selling_rate = row_net_rate
                elif row_net_rate < selling_rate:
                    selling_rate = row_net_rate

        base_selling_rate = selling_rate * invoice_to_company
        if row.get("base_net_rate") not in (None, ""):
            row_base_net_rate = abs(flt(row.get("base_net_rate")))
            if row_base_net_rate > 0 or gross_rate <= 0:
                if base_precision is not None:
                    if flt(row_base_net_rate, base_precision) < flt(
                        base_selling_rate, base_precision
                    ):
                        base_selling_rate = row_base_net_rate
                        selling_rate = row_base_net_rate / invoice_to_company
                elif row_base_net_rate < base_selling_rate:
                    base_selling_rate = row_base_net_rate
                    selling_rate = row_base_net_rate / invoice_to_company

        if base_precision is not None:
            compared_base_selling = flt(base_selling_rate, base_precision)
            compared_base_minimum = flt(base_minimum_selling_rate, base_precision)
        else:
            compared_base_selling = flt(base_selling_rate, 9)
            compared_base_minimum = flt(base_minimum_selling_rate, 9)

        if compared_base_selling >= compared_base_minimum:
            continue

        reported_selling_rate = (
            flt(selling_rate, net_rate_precision)
            if net_rate_precision is not None
            else selling_rate
        )
        reported_base_selling = (
            compared_base_selling if base_precision is not None else base_selling_rate
        )
        reported_base_buying = (
            flt(base_floor, base_precision) if base_precision is not None else base_floor
        )
        reported_base_minimum = (
            compared_base_minimum if base_precision is not None else base_minimum_selling_rate
        )

        item_name = row.get("item_name") or item_code
        cost_source = floor_info.get("cost_source") or "buying_price_list"
        cost_label = floor_info.get("cost_label") or "Buying Price"
        warehouse = floor_info.get("warehouse")
        if cost_source == "warehouse_valuation" and warehouse:
            message = _(
                "Item {0} cannot be sold at {1}; below Warehouse Valuation ({2}) minimum rate {3}. "
                "Contact Store Manager to verify selling price or stock valuation."
            ).format(item_name, reported_selling_rate, warehouse, minimum_selling_rate)
        elif cost_source == "last_purchase_rate":
            message = _(
                "Item {0} cannot be sold at {1}; below Last Purchase Rate minimum rate {2}. "
                "Contact Store Manager to verify selling price or stock valuation."
            ).format(item_name, reported_selling_rate, minimum_selling_rate)
        else:
            message = _(
                "Item {0} cannot be sold at {1}; the minimum permitted rate is {2}."
            ).format(item_name, reported_selling_rate, minimum_selling_rate)
        errors.append(
            {
                "item_code": item_code,
                "item_name": item_name,
                "policy": policy.get("action", "Block").lower().replace(" ", "_"),
                "reason": "below_buying_price",
                "selling_rate": reported_selling_rate,
                "buying_rate": floor,
                "minimum_selling_rate": minimum_selling_rate,
                "base_selling_rate": reported_base_selling,
                "base_buying_rate": reported_base_buying,
                "base_minimum_selling_rate": reported_base_minimum,
                "company_currency": floor_info.get("company_currency"),
                "minimum_margin_percentage": minimum_margin,
                "buying_price_list": floor_info.get("price_list"),
                "buying_price_uom": floor_info.get("uom"),
                "buying_price_currency": floor_info.get("invoice_currency"),
                "cost_source": cost_source,
                "cost_label": cost_label,
                "warehouse": warehouse,
                "message": message,
            }
        )

    return errors


def _set_invoice_value(invoice_doc, fieldname, value):
    meta = getattr(invoice_doc, "meta", None)
    if meta is not None and hasattr(meta, "has_field") and not meta.has_field(fieldname):
        return
    if hasattr(invoice_doc, "set"):
        invoice_doc.set(fieldname, value)
    elif isinstance(invoice_doc, dict):
        invoice_doc[fieldname] = value
    else:
        setattr(invoice_doc, fieldname, value)


def _authorize_below_cost_override(pos_profile):
    """Authorize the server-resolved session user or active terminal cashier."""

    from posawesome.posawesome.api.employees import _get_user_doc, _is_pos_supervisor
    from posawesome.posawesome.api.pos_access import get_authenticated_pos_user
    from posawesome.posawesome.api.terminal_state import get_active_terminal_cashier

    session_user = get_authenticated_pos_user()
    if _is_pos_supervisor(_get_user_doc(session_user)):
        return session_user

    cashier = get_active_terminal_cashier(pos_profile)
    if _is_pos_supervisor(_get_user_doc(cashier)):
        return cashier
    frappe.throw(
        _("A POS supervisor is required to override a below-cost sale."),
        frappe.PermissionError,
    )


def _apply_below_cost_override(invoice_doc, errors):
    reason = cstr(invoice_doc.get("posa_below_cost_override_reason")).strip()
    if not reason:
        frappe.throw(_("A reason is required to override a below-cost sale."))
    actor = _authorize_below_cost_override(invoice_doc.get("pos_profile"))
    details = [
        {
            "item_code": error.get("item_code"),
            "selling_rate": error.get("selling_rate"),
            "buying_rate": error.get("buying_rate"),
            "minimum_selling_rate": error.get("minimum_selling_rate"),
            "minimum_margin_percentage": error.get("minimum_margin_percentage"),
            "uom": error.get("buying_price_uom"),
            "currency": error.get("buying_price_currency"),
            "cost_source": error.get("cost_source"),
            "warehouse": error.get("warehouse"),
        }
        for error in errors
    ]
    _set_invoice_value(invoice_doc, "posa_below_cost_override", 1)
    _set_invoice_value(invoice_doc, "posa_below_cost_override_by", actor)
    _set_invoice_value(
        invoice_doc,
        "posa_below_cost_override_details",
        json.dumps(details, separators=(",", ":"), sort_keys=True),
    )


def validate_invoice_item_sale_controls(invoice_doc):
    flags = getattr(invoice_doc, "flags", None)
    if flags is not None and getattr(flags, "posa_item_sale_controls_validated", False):
        return

    errors = collect_item_sale_control_errors(
        invoice_doc.get("items") or [],
        is_return=bool(invoice_doc.get("is_return")),
    )
    policy = resolve_sale_floor_policy(invoice_doc.get("pos_profile"))
    floor_errors = collect_below_buying_price_errors(
        invoice_doc.get("items") or [],
        is_return=bool(invoice_doc.get("is_return")),
        pos_profile=invoice_doc.get("pos_profile"),
        invoice_doc=invoice_doc,
        policy=policy,
    )
    missing_cost_errors = [
        error for error in floor_errors if error.get("reason") in UNOVERRIDABLE_FLOOR_REASONS
    ]
    below_cost_errors = [
        error for error in floor_errors if error.get("reason") not in UNOVERRIDABLE_FLOOR_REASONS
    ]
    if not below_cost_errors or policy.get("action") != "POS Supervisor Override":
        _set_invoice_value(invoice_doc, "posa_below_cost_override", 0)
        _set_invoice_value(invoice_doc, "posa_below_cost_override_by", None)
        _set_invoice_value(invoice_doc, "posa_below_cost_override_details", None)
        if not floor_errors:
            _set_invoice_value(invoice_doc, "posa_below_cost_override_reason", None)
    if below_cost_errors and policy.get("action") == "Warning Only":
        floor_errors = missing_cost_errors
    elif below_cost_errors and policy.get("action") == "POS Supervisor Override":
        if cint(invoice_doc.get("posa_below_cost_override")):
            _apply_below_cost_override(invoice_doc, below_cost_errors)
            floor_errors = missing_cost_errors
        else:
            below_cost_errors[0]["message"] = _(
                "This sale is below the permitted floor and requires a POS supervisor override."
            )
    errors.extend(floor_errors)
    if errors:
        frappe.throw(errors[0].get("message"))

    if flags is not None:
        flags.posa_item_sale_controls_validated = True


def validate_pos_invoice_item_sale_controls(invoice_doc):
    """Apply POS sale controls from document hooks without affecting non-POS invoices."""
    doctype = getattr(invoice_doc, "doctype", None) or invoice_doc.get("doctype")
    # SAP WPUUMS / POSDM rule: already-paid POS Invoices being consolidated by
    # ERPNext's POSInvoiceMergeLog carry both is_consolidated=1 and the in-process
    # flags.is_consolidated=True marker; never block end-of-day shift close.
    flags = getattr(invoice_doc, "flags", None)
    if (
        doctype == "Sales Invoice"
        and cint(invoice_doc.get("is_consolidated"))
        and bool(getattr(flags, "is_consolidated", False))
    ):
        return
    is_pos_document = (
        doctype == "POS Invoice"
        or bool(invoice_doc.get("is_pos"))
        or bool(invoice_doc.get("pos_profile"))
    )
    if not is_pos_document:
        return

    validate_invoice_item_sale_controls(invoice_doc)
