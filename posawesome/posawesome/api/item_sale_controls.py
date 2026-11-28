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
            "item_group",
            "brand",
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

    if not company_currency:
        company_currency = (
            frappe.db.get_value("Company", company, "default_currency") if company else None
        )
    if not company_currency:
        if source_currency == invoice_currency:
            return rate, rate, 1.0
        frappe.throw(
            _("Cannot validate the buying floor because the company currency is missing.")
        )

    if source_currency == company_currency and invoice_currency == company_currency:
        return rate, rate, 1.0

    from erpnext.setup.utils import get_exchange_rate

    # Never shortcut source_currency == invoice_currency with the selling invoice rate
    # when the shared currency is foreign: buying costs convert via for_buying while
    # selling revenue converts via invoice_conversion_rate / for_selling.
    source_to_company = (
        1.0
        if source_currency == company_currency
        else flt(get_exchange_rate(source_currency, company_currency, posting_date, "for_buying"))
    )
    invoice_to_company = (
        1.0
        if invoice_currency == company_currency
        else flt(invoice_conversion_rate)
        or flt(get_exchange_rate(invoice_currency, company_currency, posting_date, "for_selling"))
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


def _resolve_buying_floors_with_uom_context(items, pos_profile=None, invoice_doc=None):
    """Resolve authoritative per-line cost floors, item stock UOMs, conversion factors, and item metadata."""

    item_rows = list(items or [])
    if not item_rows:
        return {}, {}, {}, {}

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
    raw_not_uom_dependent = (
        frappe.db.get_value("Price List", price_list, "price_not_uom_dependent")
        if price_list
        else None
    )
    if raw_not_uom_dependent is None and price_list:
        get_cached_value = getattr(frappe, "get_cached_value", None)
        if callable(get_cached_value):
            raw_not_uom_dependent = get_cached_value(
                "Price List", price_list, "price_not_uom_dependent"
            )
    price_not_uom_dependent = raw_not_uom_dependent in (1, True, "1")
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
            # ERPNext get_item_details.py:1579-1592 skips conversion_factor scaling on
            # stock-UOM fallback when Price List.price_not_uom_dependent is enabled.
            floor *= (
                factor / master_factor
                if (exact or price_not_uom_dependent)
                else factor
            )
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
    return floors, stock_uoms, conversion_factors, item_info


def _resolve_buying_floors_with_context(items, pos_profile=None, invoice_doc=None):
    """Resolve authoritative per-line cost floors and item stock UOM context."""

    floors, stock_uoms, _conversion_factors, _item_info = _resolve_buying_floors_with_uom_context(
        items,
        pos_profile=pos_profile,
        invoice_doc=invoice_doc,
    )
    return floors, stock_uoms


def resolve_buying_floors(items, pos_profile=None, invoice_doc=None):
    """Resolve authoritative per-line cost floors across buying list, warehouse valuation, and LPR.

    Client-provided ``trade_price``/``buying_rate``/``conversion_factor`` values are
    intentionally ignored.
    Evaluates (1) active Buying Item Price, (2) warehouse Bin.valuation_rate (stock items,
    matching SAP VPRS / ERPNext SellingController), and (3) Item.last_purchase_rate,
    selecting the highest normalized floor in company currency.
    """

    floors, _stock_uoms = _resolve_buying_floors_with_context(
        items,
        pos_profile=pos_profile,
        invoice_doc=invoice_doc,
    )
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


def _row_tax_params(row, invoice_doc):
    """Return ``(incl_a, incl_b, all_a, all_b)`` linear per-unit tax coefficients.

    Matches ERPNext ``taxes_and_totals.py`` for ``On Net Total``,
    ``On Item Quantity``, ``On Previous Row Amount``, and
    ``On Previous Row Total``. For a per-unit net rate ``x``:
    - Inclusive taxes equal ``incl_a * x + incl_b``
    - All non-Actual taxes (inclusive + exclusive) equal ``all_a * x + all_b``
    """

    if not invoice_doc or not hasattr(invoice_doc, "get"):
        return 0.0, 0.0, 0.0, 0.0
    taxes = invoice_doc.get("taxes") or []
    if not taxes:
        return 0.0, 0.0, 0.0, 0.0

    item_tax_map = _parse_item_tax_rate(row)
    incl_terms = []
    all_terms = []
    incl_a = incl_b = 0.0
    all_a = all_b = 0.0

    for tax in taxes:
        if not hasattr(tax, "get"):
            incl_terms.append((0.0, 0.0))
            all_terms.append((0.0, 0.0))
            continue

        charge_type = cstr(tax.get("charge_type")).strip()
        account_head = tax.get("account_head")
        if account_head and account_head in item_tax_map:
            raw_override = item_tax_map[account_head]
            # ERPNext taxes_and_totals.py:476-480, 510-517 treats "N/A" (NOT_APPLICABLE_TAX) as 0%.
            if raw_override is None or (
                isinstance(raw_override, str) and raw_override.strip().upper() == "N/A"
            ):
                tax_rate = 0.0
            else:
                tax_rate = flt(raw_override)
        else:
            tax_rate = flt(tax.get("rate"))
        is_inclusive = bool(cint(tax.get("included_in_print_rate")))

        def _compute_term(terms_list):
            if charge_type == "On Net Total":
                return tax_rate / 100.0, 0.0
            if charge_type == "On Item Quantity":
                return 0.0, tax_rate
            if charge_type == "On Previous Row Amount":
                ref_idx = cint(tax.get("row_id")) - 1
                if 0 <= ref_idx < len(terms_list):
                    prev_a, prev_b = terms_list[ref_idx]
                    return (tax_rate / 100.0) * prev_a, (tax_rate / 100.0) * prev_b
                return 0.0, 0.0
            if charge_type == "On Previous Row Total":
                ref_idx = cint(tax.get("row_id")) - 1
                if 0 <= ref_idx < len(terms_list):
                    prev_sum_a = sum(a for a, _ in terms_list[: ref_idx + 1])
                    prev_sum_b = sum(b for _, b in terms_list[: ref_idx + 1])
                    return (tax_rate / 100.0) * (1.0 + prev_sum_a), (tax_rate / 100.0) * prev_sum_b
                return tax_rate / 100.0, 0.0
            return 0.0, 0.0

        cur_all_a, cur_all_b = _compute_term(all_terms)
        all_terms.append((cur_all_a, cur_all_b))
        all_a += cur_all_a
        all_b += cur_all_b

        if is_inclusive:
            cur_incl_a, cur_incl_b = _compute_term(incl_terms)
        else:
            cur_incl_a, cur_incl_b = 0.0, 0.0
        incl_terms.append((cur_incl_a, cur_incl_b))
        incl_a += cur_incl_a
        incl_b += cur_incl_b

    return max(incl_a, 0.0), max(incl_b, 0.0), max(all_a, 0.0), max(all_b, 0.0)


def _row_inclusive_tax_params(row, invoice_doc):
    """Return ``(tax_fraction, per_unit_tax)`` for tax-inclusive rows on ``invoice_doc``."""

    incl_a, incl_b, _all_a, _all_b = _row_tax_params(row, invoice_doc)
    return incl_a, incl_b


def _is_descendant_tree_node(doctype, child_name, ancestor_name):
    """Return whether ``child_name`` falls within ``ancestor_name``'s nested-set subtree."""

    if not child_name or not ancestor_name:
        return False
    if child_name == ancestor_name:
        return True
    rows = (
        frappe.get_all(
            doctype,
            filters={"name": ["in", [child_name, ancestor_name]]},
            fields=["name", "lft", "rgt"],
            limit_page_length=2,
        )
        or []
    )
    by_name = {cstr(row.get("name")).strip(): row for row in rows if hasattr(row, "get")}
    child = by_name.get(child_name)
    ancestor = by_name.get(ancestor_name)
    if not child or not ancestor:
        return False
    ancestor_lft = cint(ancestor.get("lft"))
    ancestor_rgt = cint(ancestor.get("rgt"))
    child_lft = cint(child.get("lft"))
    child_rgt = cint(child.get("rgt"))
    return (
        ancestor_lft > 0
        and ancestor_rgt >= ancestor_lft
        and child_lft >= ancestor_lft
        and child_rgt <= ancestor_rgt
    )


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
    if not has_doc:
        return False
    if applicable_for == "Campaign":
        actual = cstr(invoice_doc.get("utm_campaign") or invoice_doc.get("campaign")).strip()
        return actual == expected
    actual = cstr(invoice_doc.get(invoice_field)).strip()
    if applicable_for in ("Customer Group", "Territory"):
        return _is_descendant_tree_node(applicable_for, actual, expected)
    return actual == expected


def _server_applied_free_row_indexes(
    item_rows, invoice_doc=None, stock_uoms=None, conversion_factors=None, item_info=None
):
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
                "mixed_conditions",
                "is_cumulative",
                "same_item",
                "free_item",
                "free_qty",
                "is_recursive",
                "recurse_for",
                "apply_recursion_over",
                "round_free_qty",
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
    stock_uoms_map = stock_uoms or {}
    conversion_factors_map = conversion_factors or {}
    item_info_map = item_info or {}

    # ERPNext v16.60.0 stores `uom` on the Pricing Rule child tables
    # (`Pricing Rule Item Code`, `Pricing Rule Item Group`, `Pricing Rule Brand`),
    # not on `tabPricing Rule` (`pricing_rule/utils.py:175`).
    rule_item_uoms = {}
    rule_item_group_rows = {}
    rule_brand_uoms = {}
    item_code_rule_names = sorted(
        {
            name
            for name, rule in rules.items()
            if cstr(rule.get("apply_on")).strip() == "Item Code"
        }
    )
    if item_code_rule_names:
        for child in (
            frappe.get_all(
                "Pricing Rule Item Code",
                filters={"parent": ["in", item_code_rule_names]},
                fields=["parent", "item_code", "uom"],
                limit_page_length=0,
            )
            or []
        ):
            if hasattr(child, "get") and child.get("parent") and child.get("item_code"):
                rule_item_uoms.setdefault(
                    (child.get("parent"), cstr(child.get("item_code")).strip()), []
                ).append(cstr(child.get("uom")).strip())

    item_group_rule_names = sorted(
        {
            name
            for name, rule in rules.items()
            if cstr(rule.get("apply_on")).strip() == "Item Group"
        }
    )
    if item_group_rule_names:
        for child in (
            frappe.get_all(
                "Pricing Rule Item Group",
                filters={"parent": ["in", item_group_rule_names]},
                fields=["parent", "item_group", "uom"],
                limit_page_length=0,
            )
            or []
        ):
            if hasattr(child, "get") and child.get("parent") and child.get("item_group"):
                rule_item_group_rows.setdefault(child.get("parent"), []).append(
                    (cstr(child.get("item_group")).strip(), cstr(child.get("uom")).strip())
                )

    brand_rule_names = sorted(
        {
            name
            for name, rule in rules.items()
            if cstr(rule.get("apply_on")).strip() == "Brand"
        }
    )
    if brand_rule_names:
        for child in (
            frappe.get_all(
                "Pricing Rule Brand",
                filters={"parent": ["in", brand_rule_names]},
                fields=["parent", "brand", "uom"],
                limit_page_length=0,
            )
            or []
        ):
            if hasattr(child, "get") and child.get("parent") and child.get("brand"):
                rule_brand_uoms.setdefault(
                    (child.get("parent"), cstr(child.get("brand")).strip()), []
                ).append(cstr(child.get("uom")).strip())

    def _pick_child_uom(candidates, preferred_uom):
        if not candidates:
            return ""
        if preferred_uom and preferred_uom in candidates:
            return preferred_uom
        for candidate in candidates:
            if candidate:
                return candidate
        return ""

    def _matched_rule_uom(rule, code, preferred_uom, paid_row):
        apply_on = cstr(rule.get("apply_on")).strip()
        rule_name = rule.get("name")
        meta = item_info_map.get(code) or {}
        if apply_on == "Item Code":
            candidates = rule_item_uoms.get((rule_name, code)) or []
            if not candidates and meta.get("variant_of"):
                candidates = rule_item_uoms.get(
                    (rule_name, cstr(meta.get("variant_of")).strip())
                ) or []
            return _pick_child_uom(candidates, preferred_uom)
        if apply_on == "Item Group":
            item_group = cstr(meta.get("item_group") or paid_row.get("item_group")).strip()
            if not item_group:
                return ""
            exact = [
                uom
                for group, uom in rule_item_group_rows.get(rule_name, [])
                if group == item_group
            ]
            if exact:
                return _pick_child_uom(exact, preferred_uom)
            descendants = [
                uom
                for group, uom in rule_item_group_rows.get(rule_name, [])
                if _is_descendant_tree_node("Item Group", item_group, group)
            ]
            return _pick_child_uom(descendants, preferred_uom)
        if apply_on == "Brand":
            brand = cstr(meta.get("brand") or paid_row.get("brand")).strip()
            if not brand:
                return ""
            return _pick_child_uom(rule_brand_uoms.get((rule_name, brand)) or [], preferred_uom)
        return ""

    def _authoritative_uom_factor(code, uom, stock_uom):
        if not uom or not stock_uom or uom == stock_uom:
            return 1.0
        factor = conversion_factors_map.get((code, uom))
        if factor is not None and flt(factor) > 0:
            return flt(factor)
        try:
            from erpnext.stock.doctype.item.item import get_uom_conv_factor

            global_factor = flt(get_uom_conv_factor(uom, stock_uom))
            if global_factor > 0:
                return global_factor
        except Exception:
            pass
        return 0.0

    paid_items_by_rule = {}
    paid_rows_by_rule = {}
    paid_rows_by_rule_item = {}
    paid_qty = paid_amount = 0.0
    for row in item_rows:
        if (
            hasattr(row, "get")
            and row.get("item_code")
            and not cint(row.get("is_free_item"))
            and flt(row.get("qty")) > 0
        ):
            row_code = row.get("item_code")
            row_qty = flt(row.get("qty"))
            row_stock_uom = cstr(stock_uoms_map.get(row_code) or row.get("stock_uom")).strip()
            row_uom = cstr(row.get("uom") or row_stock_uom).strip()
            row_cf = _authoritative_uom_factor(row_code, row_uom, row_stock_uom)
            row_stock_qty = row_qty * row_cf if row_cf > 0 else 0.0
            paid_qty += row_qty
            # ERPNext's doc.total: rounded line amounts, then a rounded sum.
            paid_amount += flt(
                flt(row.get("rate")) * row_qty, _field_precision(row, "amount")
            )
            for rule_name in _applied_pricing_rule_names(row.get("pricing_rules")):
                paid_items_by_rule.setdefault(rule_name, set()).add(row_code)
                key = (rule_name, row_code)
                entry = (row_code, row_uom, row_stock_uom, row_qty, row_stock_qty, row)
                paid_rows_by_rule.setdefault(rule_name, []).append(entry)
                paid_rows_by_rule_item.setdefault(key, []).append(entry)
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
        item_stock_uom = cstr(stock_uoms_map.get(item_code) or free_row.get("stock_uom")).strip()
        expected_uom = cstr(rule.get("free_item_uom")).strip() or item_stock_uom
        row_uom = cstr(free_row.get("uom") or item_stock_uom).strip()
        if expected_uom and row_uom != expected_uom:
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
            qualifying_rows = []
        else:
            paid_items = paid_items_by_rule.get(rule_name) or set()
            is_same_item_award = bool(cint(rule.get("same_item")) and item_code in paid_items)
            gives_item = bool(paid_items) and (
                item_code == rule.get("free_item") or is_same_item_award
            )
            qualifying_rows = (
                paid_rows_by_rule_item.get((rule_name, item_code), [])
                if is_same_item_award
                else paid_rows_by_rule.get(rule_name, [])
            )
        if not gives_item:
            continue
        raw_free_qty = flt(rule.get("free_qty"))
        base_free_qty = raw_free_qty if raw_free_qty > 0 else 1.0
        if cint(rule.get("is_recursive")) and rule.get("apply_on") != "Transaction":
            recurse_for = flt(rule.get("recurse_for"))
            if recurse_for <= 0:
                continue
            # ERPNext pricing_rule/utils.py:312-332, 423-453, 546-615 + get_item_details.py:1784-1789:
            # - Only `mixed_conditions=1` aggregates current-invoice paid lines; when
            #   `mixed_conditions=0`, each paid line is checked independently against
            #   `min_qty`/`max_qty` and `min_amt`/`max_amt`.
            # - When `is_cumulative=1` (and `mixed_conditions=0`), ERPNext adds historical
            #   submitted `(cumulative_qty, cumulative_amount)` — which may be negative
            #   after returns — to each line's `stock_qty` and `amount` before checking
            #   thresholds.
            # - Only `apply_on == "Item Code"` child rows carry `item_code`, so Brand and
            #   Item Group rules convert with factor 1.0 in ERPNext.
            min_qty = flt(rule.get("min_qty"))
            max_qty = flt(rule.get("max_qty"))
            min_amt = flt(rule.get("min_amt"))
            max_amt = flt(rule.get("max_amt"))
            is_mixed = bool(cint(rule.get("mixed_conditions")))
            is_cumulative = bool(cint(rule.get("is_cumulative")))
            apply_on = cstr(rule.get("apply_on")).strip()

            class _RuleAttrDict(dict):
                def __getattr__(self, key):
                    return self.get(key)

            def _row_threshold_factors(entry):
                q_code, q_uom, q_stock_uom, _q_qty, _q_stock_qty, q_row = entry
                if apply_on != "Item Code":
                    return 1.0, 1.0
                child_uom = _matched_rule_uom(rule, q_code, q_uom, q_row)
                if not child_uom or child_uom == q_stock_uom:
                    return 1.0, 1.0
                child_cf = _authoritative_uom_factor(q_code, child_uom, q_stock_uom)
                if child_cf <= 0:
                    return None
                amt_cf = 1.0 if child_uom == q_uom else child_cf
                return child_cf, amt_cf

            def _cumulative_totals_for_row(entry):
                q_code, q_uom, _q_stock_uom, q_qty, q_stock_qty, q_row = entry
                try:
                    from erpnext.accounts.doctype.pricing_rule.utils import (
                        get_cumulative_qty_and_amount_for_row,
                    )
                except ImportError:
                    return None

                pr_doc = None
                get_cached_doc = getattr(frappe, "get_cached_doc", None)
                if callable(get_cached_doc) and rule_name:
                    does_not_exist = getattr(frappe, "DoesNotExistError", None)
                    catch_missing = (
                        (does_not_exist, KeyError)
                        if isinstance(does_not_exist, type) and issubclass(does_not_exist, Exception)
                        else (KeyError,)
                    )
                    try:
                        pr_doc = get_cached_doc("Pricing Rule", rule_name)
                    except catch_missing:
                        pr_doc = None
                if pr_doc is None:
                    pr_doc = _RuleAttrDict(rule)

                meta = item_info_map.get(q_code) or {}
                doc_doctype = (
                    cstr(
                        invoice_doc.get("doctype")
                        or getattr(invoice_doc, "doctype", None)
                    ).strip()
                    if has_doc
                    else ""
                )
                doctype = (
                    doc_doctype
                    or cstr(q_row.get("parenttype") or "Sales Invoice").strip()
                )
                row_args = _RuleAttrDict(
                    {
                        "item_code": q_code,
                        "item_group": cstr(
                            meta.get("item_group") or q_row.get("item_group")
                        ).strip(),
                        "brand": cstr(
                            meta.get("brand") or q_row.get("brand")
                        ).strip(),
                        "parenttype": doctype,
                        "doctype": doctype,
                        "company": company,
                        "name": (
                            invoice_doc.get("name")
                            or getattr(invoice_doc, "name", None)
                            if has_doc
                            else None
                        ),
                        "modified": (
                            invoice_doc.get("modified")
                            or getattr(invoice_doc, "modified", None)
                            if has_doc
                            else None
                        ),
                        "uom": q_uom,
                        "qty": q_qty,
                        "stock_qty": q_stock_qty,
                        "price_list_rate": flt(
                            q_row.get("price_list_rate") or q_row.get("rate")
                        ),
                    }
                )
                # Mirror ERPNext pricing_rule/utils.py:324-327: do not swallow
                # cumulative history lookup errors or silently fall back.
                result = get_cumulative_qty_and_amount_for_row(
                    pr_doc,
                    row_args,
                    invoice_doc if has_doc else None,
                )
                if not isinstance(result, (tuple, list)) or len(result) < 2:
                    raise TypeError(
                        "get_cumulative_qty_and_amount_for_row returned an invalid cumulative tuple"
                    )
                return flt(result[0]), flt(result[1])

            if is_mixed:
                total_threshold_qty = 0.0
                total_threshold_amt = 0.0
                valid_count = 0
                for entry in qualifying_rows:
                    factors = _row_threshold_factors(entry)
                    if factors is None:
                        continue
                    qty_cf, amt_cf = factors
                    _q_code, _q_uom, _q_stock_uom, q_qty, q_stock_qty, q_row = entry
                    total_threshold_qty += q_stock_qty / qty_cf
                    row_amt = flt(q_row.get("price_list_rate") or q_row.get("rate")) * q_qty
                    total_threshold_amt += row_amt / amt_cf
                    valid_count += 1
                if not valid_count:
                    continue
                if total_threshold_qty < min_qty - 1e-9:
                    continue
                if max_qty > 0 and total_threshold_qty > max_qty + 1e-9:
                    continue
                if total_threshold_amt < min_amt - 1e-9:
                    continue
                if max_amt > 0 and total_threshold_amt > max_amt + 1e-9:
                    continue
                eligible_rows = qualifying_rows
            else:
                eligible_rows = []
                for entry in qualifying_rows:
                    factors = _row_threshold_factors(entry)
                    if factors is None:
                        continue
                    qty_cf, amt_cf = factors
                    _q_code, _q_uom, _q_stock_uom, q_qty, q_stock_qty, q_row = entry
                    row_amt = flt(q_row.get("price_list_rate") or q_row.get("rate")) * q_qty
                    if is_cumulative:
                        # Fail closed when cumulative history cannot be loaded (including
                        # ImportError): never disable min_qty/min_amt checks.
                        cumulative_totals = _cumulative_totals_for_row(entry)
                        if cumulative_totals is None:
                            continue
                        cum_qty, cum_amt = cumulative_totals
                        row_threshold_qty = (q_stock_qty + cum_qty) / qty_cf
                        row_threshold_amt = row_amt + cum_amt
                    else:
                        row_threshold_qty = q_stock_qty / qty_cf
                        row_threshold_amt = row_amt
                    # ERPNext pricing_rule/utils.py:433-445 always enforces min_qty/min_amt
                    # even when 0 (rejecting negative cumulative totals after returns);
                    # max_qty/max_amt treat 0 as unbounded.
                    if row_threshold_qty < min_qty - 1e-9:
                        continue
                    if max_qty > 0 and row_threshold_qty > max_qty + 1e-9:
                        continue
                    if row_threshold_amt < (min_amt * amt_cf) - 1e-9:
                        continue
                    if max_amt > 0 and row_threshold_amt > (max_amt * amt_cf) + 1e-9:
                        continue
                    eligible_rows.append(entry)
                if not eligible_rows:
                    continue

            qualifying_qty = sum(entry[3] for entry in eligible_rows)
            recursion_over = flt(rule.get("apply_recursion_over"))
            transaction_qty = qualifying_qty - recursion_over
            if transaction_qty > 0:
                if cint(rule.get("round_free_qty")):
                    max_awarded_qty = (flt(transaction_qty) // recurse_for) * base_free_qty
                else:
                    max_awarded_qty = flt(transaction_qty) * base_free_qty / recurse_for
                if recursion_over and recursion_over == min_qty:
                    max_awarded_qty += base_free_qty
            else:
                max_awarded_qty = base_free_qty
        else:
            max_awarded_qty = base_free_qty
        free_row_qty = flt(free_row.get("qty"))
        if free_row_qty <= 0 or free_row_qty > max_awarded_qty + 1e-9:
            continue
        seen.add((item_code, rule_name))
        authorized.add(index)
    return authorized


def _invoice_actual_tax_total(invoice_doc):
    """Return total invoice-level ``Actual`` charges (and chained taxes on them).

    Mirrors ERPNext v16.60.0 ``taxes_and_totals.py:1135-1170`` so fallback
    ``Grand Total`` percentage discounts include fixed ``Actual`` charges in the
    grand-total discount base while distributing over scalable net + percentage
    taxes.
    """

    if not invoice_doc or not hasattr(invoice_doc, "get"):
        return 0.0
    taxes = invoice_doc.get("taxes") or []
    if not taxes:
        return 0.0

    total_actual_tax = 0.0
    actual_taxes_dict = {}

    def _record(row_idx, tax, amount):
        nonlocal total_actual_tax
        if cstr(tax.get("add_deduct_tax")).strip() == "Deduct":
            amount *= -1.0
        if cstr(tax.get("category")).strip() != "Valuation":
            total_actual_tax += amount
        actual_taxes_dict[row_idx] = (amount, total_actual_tax)

    for pos, tax in enumerate(taxes):
        if not hasattr(tax, "get"):
            continue
        row_idx = cint(tax.get("idx")) or (pos + 1)
        charge_type = cstr(tax.get("charge_type")).strip()
        if charge_type == "Actual":
            _record(row_idx, tax, flt(tax.get("tax_amount")))
            continue
        if charge_type not in ("On Previous Row Amount", "On Previous Row Total"):
            continue
        ref_idx = cint(tax.get("row_id"))
        if ref_idx not in actual_taxes_dict:
            continue
        base_tax_amount = (
            actual_taxes_dict[ref_idx][0]
            if charge_type == "On Previous Row Amount"
            else actual_taxes_dict[ref_idx][1]
        )
        _record(row_idx, tax, base_tax_amount * flt(tax.get("rate")) / 100.0)

    return total_actual_tax


def _is_round_row_wise_tax_enabled(invoice_doc=None):
    """Return whether ERPNext's ``Accounts Settings.round_row_wise_tax`` is active."""

    if invoice_doc and hasattr(invoice_doc, "get"):
        explicit = invoice_doc.get("round_row_wise_tax")
        if explicit is not None:
            return explicit in (1, True, "1")
    flags = getattr(frappe, "flags", None)
    flag_val = getattr(flags, "round_row_wise_tax", None) if flags is not None else None
    if flag_val is not None:
        return flag_val in (1, True, "1")
    get_single_value = getattr(frappe, "get_single_value", None)
    if callable(get_single_value):
        try:
            val = get_single_value("Accounts Settings", "round_row_wise_tax")
            if val is not None:
                return val in (1, True, "1")
        except Exception:
            pass
    db = getattr(frappe, "db", None)
    if db and callable(getattr(db, "get_single_value", None)):
        try:
            val = db.get_single_value("Accounts Settings", "round_row_wise_tax")
            if val is not None:
                return val in (1, True, "1")
        except Exception:
            pass
    return False


def _has_populated_net_field(row, fieldname, gross_rate, allow_zero=True):
    """Return whether ``fieldname`` on ``row`` carries a meaningful net rate (including 0.0)."""

    if row.get(fieldname) in (None, ""):
        return False
    if abs(flt(row.get(fieldname))) > 0 or gross_rate <= 0:
        return True
    if not allow_zero:
        return False
    if isinstance(row, dict):
        return True
    return (
        abs(flt(row.get("amount"))) > 0
        or abs(flt(row.get("base_rate"))) > 0
        or abs(flt(row.get("base_amount"))) > 0
    )


def _row_native_net_amount(row, gross_rate, qty):
    """Return native row net amount, preferring ``net_amount`` over ``net_rate * qty``."""

    if _has_populated_net_field(row, "net_amount", gross_rate):
        return abs(flt(row.get("net_amount")))
    if _has_populated_net_field(row, "net_rate", gross_rate):
        return abs(flt(row.get("net_rate"))) * qty
    return None


def collect_below_buying_price_errors(
    items, is_return=False, pos_profile=None, invoice_doc=None, policy=None
):
    if is_return:
        return []

    item_rows = list(items or [])
    policy = policy or resolve_sale_floor_policy(pos_profile)
    if not policy.get("enabled"):
        return []
    buying_floors, stock_uoms, conversion_factors, item_info = _resolve_buying_floors_with_uom_context(
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
    is_cash_or_non_trade_discount = (
        bool(cint(invoice_doc.get("is_cash_or_non_trade_discount"))) if has_doc else False
    )
    promotional_free_rows = _server_applied_free_row_indexes(
        item_rows,
        invoice_doc,
        stock_uoms=stock_uoms,
        conversion_factors=conversion_factors,
        item_info=item_info,
    )

    row_tax_params = {}
    pre_discount_nets = {}
    eligible_indexes = []
    eligible_net_total = 0.0
    eligible_scalable_grand_total = 0.0
    eligible_fixed_qty_tax_total = 0.0
    for index, item in enumerate(item_rows):
        if not hasattr(item, "get"):
            continue
        incl_a, incl_b, all_a, all_b = _row_tax_params(item, invoice_doc)
        row_tax_params[index] = (incl_a, incl_b, all_a, all_b)
        gross_rate = abs(flt(item.get("rate")))
        pre_discount_net = max((gross_rate - incl_b) / (1.0 + incl_a), 0.0)
        pre_discount_nets[index] = pre_discount_net
        # Include verified promotional rows in the invoice discount allocation base
        # (ERPNext taxes_and_totals.py:1130 includes rows with nonzero free_item_rate
        # in net_total), while keeping promotional_free_rows floor-exempt below.
        if (
            not item.get("is_return")
            and not item.get("posa_is_replace")
            and flt(item.get("qty")) > 0
        ):
            eligible_indexes.append(index)
            qty = abs(flt(item.get("qty")))
            eligible_net_total += pre_discount_net * qty
            eligible_scalable_grand_total += pre_discount_net * (1.0 + all_a) * qty
            eligible_fixed_qty_tax_total += all_b * qty

    invoice_actual_tax_total = _invoice_actual_tax_total(invoice_doc)
    eligible_grand_total = (
        eligible_scalable_grand_total + eligible_fixed_qty_tax_total + invoice_actual_tax_total
    )

    computed_net_rates = {}
    for index, row in enumerate(item_rows):
        if not hasattr(row, "get"):
            continue
        pre_discount_net = pre_discount_nets.get(index, 0.0)
        if apply_discount_on == "Net Total":
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
            if is_cash_or_non_trade_discount:
                computed_net_rate = pre_discount_net
            elif invoice_discount_percentage > 0 and eligible_scalable_grand_total > 0:
                effective_discount = (invoice_discount_percentage / 100.0) * eligible_grand_total
                discount_fraction = min(effective_discount / eligible_scalable_grand_total, 1.0)
                computed_net_rate = pre_discount_net * max(1.0 - discount_fraction, 0.0)
            elif invoice_discount_amount > 0 and eligible_scalable_grand_total > 0:
                discount_fraction = min(
                    invoice_discount_amount / eligible_scalable_grand_total, 1.0
                )
                computed_net_rate = pre_discount_net * max(1.0 - discount_fraction, 0.0)
            else:
                computed_net_rate = pre_discount_net
        computed_net_rates[index] = computed_net_rate

    # ERPNext distributes rounding differences (+1 ULP) onto individual lines so
    # sum(item.net_amount) matches the discounted net total (taxes_and_totals.py:1105-1116).
    # Reconcile via native ``net_amount`` (falling back to ``net_rate * qty``) so
    # multi-qty lines whose rounded ``net_rate * qty`` differs from ``net_amount``
    # still preserve ERPNext's exact allocation.
    reconciled_native_nets = False
    round_row_wise_tax = _is_round_row_wise_tax_enabled(invoice_doc)
    if eligible_indexes:
        native_sum = 0.0
        expected_sum = 0.0
        pre_discount_sum = 0.0
        invoice_ulp = 1e-9
        tax_group_totals = {}
        all_rows_reconciled = True
        all_rows_match_row_wise = True
        for idx in eligible_indexes:
            el_row = item_rows[idx]
            el_gross = abs(flt(el_row.get("rate")))
            el_qty = abs(flt(el_row.get("qty")))
            el_native_amount = _row_native_net_amount(el_row, el_gross, el_qty)
            if el_native_amount is None:
                if idx in promotional_free_rows and el_gross <= 0:
                    el_native_amount = 0.0
                else:
                    all_rows_reconciled = False
                    break
            el_pre_amount = pre_discount_nets.get(idx, 0.0) * el_qty
            el_computed_amount = computed_net_rates.get(idx, 0.0) * el_qty
            el_incl_a, el_incl_b, _el_all_a, _el_all_b = row_tax_params.get(
                idx, (0.0, 0.0, 0.0, 0.0)
            )
            has_inclusive_tax = el_incl_a > 0 or el_incl_b > 0
            el_prec = _resolve_currency_precision(el_row, invoice_doc, "net_amount")
            if el_prec is None:
                el_prec = _resolve_currency_precision(el_row, invoice_doc, "net_rate")
            row_ulp = (10.0 ** (-el_prec)) if el_prec is not None else 1e-9
            invoice_ulp = max(invoice_ulp, row_ulp)
            # ERPNext taxes_and_totals.py:438-462 carries inclusive-tax rounding across rows
            # within the same tax group, permitting up to +1 ULP on an individual inclusive row
            # (strictly capped at gross line amount) while keeping the tax-group total within 0.5 ULP.
            # When round_row_wise_tax is enabled, each row rounds independently to flt(pre_amount, prec).
            el_row_wise_expected = (
                min(el_gross * el_qty, flt(el_pre_amount, el_prec))
                if el_prec is not None
                else el_pre_amount
            )
            if abs(el_native_amount - el_row_wise_expected) > 1e-9:
                all_rows_match_row_wise = False
            el_max_pre_amount = (
                min(el_gross * el_qty, el_pre_amount + row_ulp)
                if has_inclusive_tax and el_prec is not None
                else el_pre_amount
            )
            if (
                el_native_amount > el_max_pre_amount + 1e-9
                or el_native_amount > el_computed_amount + row_ulp + 1e-9
            ):
                all_rows_reconciled = False
                break
            native_sum += el_native_amount
            expected_sum += el_computed_amount
            pre_discount_sum += el_pre_amount
            item_tax_key = tuple(
                sorted((str(k), str(v)) for k, v in _parse_item_tax_rate(el_row).items())
            )
            group_key = (round(el_incl_a, 9), round(el_incl_b, 9), item_tax_key)
            group_entry = tax_group_totals.setdefault(
                group_key, {"native": 0.0, "pre": 0.0, "ulp": 1e-9}
            )
            group_entry["native"] += el_native_amount
            group_entry["pre"] += el_pre_amount
            group_entry["ulp"] = max(group_entry["ulp"], row_ulp)
        has_unapplied_discount = (
            expected_sum < pre_discount_sum - 0.5 * invoice_ulp - 1e-9
            and native_sum >= pre_discount_sum - 1e-9
        )
        # When undiscounted, ERPNext either rounds each row independently (round_row_wise_tax=1,
        # taxes_and_totals.py:442) or carries rounding strictly within each tax group
        # (taxes_and_totals.py:446-455). When discounted, taxes_and_totals.py:1105-1116
        # carries rounding across the whole invoice so sum(item.net_amount) stays within 0.5 ULP.
        if all_rows_reconciled and not has_unapplied_discount:
            if abs(expected_sum - pre_discount_sum) <= 1e-9:
                if round_row_wise_tax:
                    reconciled_native_nets = all_rows_match_row_wise
                else:
                    reconciled_native_nets = all(
                        abs(group["native"] - group["pre"]) <= 0.5 * group["ulp"] + 1e-9
                        for group in tax_group_totals.values()
                    )
            else:
                reconciled_native_nets = (
                    abs(native_sum - expected_sum) <= 0.5 * invoice_ulp + 1e-9
                )

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

        gross_rate = abs(flt(row.get("rate")))
        row_qty = abs(flt(row.get("qty")))
        computed_net_rate = computed_net_rates.get(index, 0.0)
        net_rate_precision = _resolve_currency_precision(row, invoice_doc, "net_rate")
        base_precision = _resolve_currency_precision(row, invoice_doc, "base_net_rate")

        honored_native_net_rate = False
        if reconciled_native_nets:
            has_net_amount = _has_populated_net_field(row, "net_amount", gross_rate) and row_qty > 0
            has_net_rate = _has_populated_net_field(row, "net_rate", gross_rate)
            row_net_from_amount = (
                abs(flt(row.get("net_amount"))) / row_qty if has_net_amount else None
            )
            row_net_rate = abs(flt(row.get("net_rate"))) if has_net_rate else None
            half_ulp = (
                0.5 * (10.0 ** (-net_rate_precision))
                if net_rate_precision is not None
                else 1e-9
            )
            # ERPNext taxes_and_totals.py:1118-1123 sets net_rate = flt(net_amount / qty, prec).
            # Honor native net_rate when it is consistent with net_amount / qty within 0.5 ULP.
            if row_net_rate is not None and (
                row_net_from_amount is None
                or abs(row_net_rate - row_net_from_amount) <= half_ulp + 1e-9
            ):
                selling_rate = row_net_rate
                honored_native_net_rate = True
            elif row_net_from_amount is not None:
                selling_rate = row_net_from_amount
            else:
                selling_rate = computed_net_rate
        else:
            selling_rate = computed_net_rate
            if _has_populated_net_field(row, "net_amount", gross_rate) and row_qty > 0:
                row_net_from_amount = abs(flt(row.get("net_amount"))) / row_qty
                if net_rate_precision is not None:
                    if flt(row_net_from_amount, net_rate_precision) < flt(
                        selling_rate, net_rate_precision
                    ):
                        selling_rate = row_net_from_amount
                elif row_net_from_amount < selling_rate:
                    selling_rate = row_net_from_amount
            if _has_populated_net_field(row, "net_rate", gross_rate):
                row_net_rate = abs(flt(row.get("net_rate")))
                if net_rate_precision is not None:
                    if flt(row_net_rate, net_rate_precision) < flt(
                        selling_rate, net_rate_precision
                    ):
                        selling_rate = row_net_rate
                elif row_net_rate < selling_rate:
                    selling_rate = row_net_rate

        base_selling_rate = selling_rate * invoice_to_company
        # Never let zero base_net_* derived from an unreconciled all-zero draft override
        # a positive selling_rate (while still honoring reconciled/explicit zero selling_rate).
        allow_zero_base_net = selling_rate <= 0
        if (
            not honored_native_net_rate
            and _has_populated_net_field(
                row, "base_net_amount", gross_rate, allow_zero=allow_zero_base_net
            )
            and row_qty > 0
        ):
            row_base_from_amount = abs(flt(row.get("base_net_amount"))) / row_qty
            if base_precision is not None:
                if flt(row_base_from_amount, base_precision) < flt(
                    base_selling_rate, base_precision
                ):
                    base_selling_rate = row_base_from_amount
                    selling_rate = row_base_from_amount / invoice_to_company
            elif row_base_from_amount < base_selling_rate:
                base_selling_rate = row_base_from_amount
                selling_rate = row_base_from_amount / invoice_to_company
        if _has_populated_net_field(
            row, "base_net_rate", gross_rate, allow_zero=allow_zero_base_net
        ):
            row_base_net_rate = abs(flt(row.get("base_net_rate")))
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


def _resolve_authoritative_exchange_rate(from_currency, to_currency, posting_date):
    """Resolve the authoritative posting-date selling exchange rate from ``from_currency`` to ``to_currency``."""

    if not from_currency or not to_currency or from_currency == to_currency:
        return 1.0

    rate = 0.0
    try:
        from posawesome.posawesome.api.invoice_processing.utils import get_latest_rate

        latest_rate, _rate_date = get_latest_rate(
            from_currency,
            to_currency,
            transaction_date=posting_date,
            purpose="for_selling",
        )
        rate = flt(latest_rate)
    except Exception:
        rate = 0.0

    if rate <= 0:
        from erpnext.setup.utils import get_exchange_rate

        rate = flt(get_exchange_rate(from_currency, to_currency, posting_date, "for_selling"))

    if rate <= 0:
        frappe.throw(
            _(
                "Cannot validate the buying floor because the exchange rate between {0} and {1} is missing."
            ).format(from_currency, to_currency)
        )
    return rate


def _refresh_authoritative_conversion_rate(invoice_doc):
    """Refresh stale/forged foreign-currency conversion rates at the POS validation boundary."""

    if not invoice_doc or not hasattr(invoice_doc, "get"):
        return
    company = invoice_doc.get("company")
    invoice_currency = cstr(invoice_doc.get("currency")).strip()
    if not company or not invoice_currency:
        return
    company_currency = cstr(frappe.db.get_value("Company", company, "default_currency")).strip()
    if not company_currency:
        return

    posting_date = invoice_doc.get("posting_date") or nowdate()
    resolved_rate = (
        1.0
        if invoice_currency == company_currency
        else _resolve_authoritative_exchange_rate(
            invoice_currency,
            company_currency,
            posting_date,
        )
    )
    current_rate = flt(invoice_doc.get("conversion_rate"))
    if abs(current_rate - resolved_rate) <= 1e-9:
        return

    _set_invoice_value(invoice_doc, "conversion_rate", resolved_rate)
    calculate = getattr(invoice_doc, "calculate_taxes_and_totals", None)
    if callable(calculate):
        calculate()
        return

    for row in invoice_doc.get("items") or []:
        if not hasattr(row, "get"):
            continue
        if row.get("net_rate") not in (None, ""):
            base_precision = _resolve_currency_precision(row, invoice_doc, "base_net_rate")
            updated_base_net = flt(flt(row.get("net_rate")) * resolved_rate, base_precision)
            _set_invoice_value(row, "base_net_rate", updated_base_net)
        elif row.get("base_net_rate") not in (None, ""):
            _set_invoice_value(row, "base_net_rate", None)


def validate_invoice_item_sale_controls(invoice_doc):
    flags = getattr(invoice_doc, "flags", None)
    if flags is not None and getattr(flags, "posa_item_sale_controls_validated", False):
        return

    is_return = bool(invoice_doc.get("is_return"))
    errors = collect_item_sale_control_errors(
        invoice_doc.get("items") or [],
        is_return=is_return,
    )
    policy = resolve_sale_floor_policy(invoice_doc.get("pos_profile"))
    if not is_return and policy.get("enabled"):
        _refresh_authoritative_conversion_rate(invoice_doc)
    floor_errors = collect_below_buying_price_errors(
        invoice_doc.get("items") or [],
        is_return=is_return,
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
