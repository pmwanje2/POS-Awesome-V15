from __future__ import annotations

import math
import re
from typing import Any, Iterable, Sequence

import frappe
from erpnext.setup.utils import get_exchange_rate
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, nowdate
from frappe.utils.caching import redis_cache

from posawesome.posawesome.api.item_fetchers import _fetch_batches
from posawesome.posawesome.api.item_sale_controls import _resolve_buying_price_list
from posawesome.posawesome.api.pos_access import (
    get_authorized_pos_item,
    get_authorized_pos_profile,
    user_can_manage_pos,
)
from posawesome.posawesome.api.terminal_state import get_active_terminal_cashier
from posawesome.posawesome.api.utils import expand_item_groups, get_item_groups


ALTERNATE_CANDIDATE_CACHE_TTL_SECONDS = 60
ALTERNATE_CANDIDATE_QUERY_LIMIT = 5000
ALTERNATE_RESULT_LIMIT = 20
ALTERNATE_RESULT_LIMIT_MAX = 50

GENERIC_CODE_FIELD = "retailmind_old_pos_generic_code"
GENERIC_NAME_FIELD = "retailmind_old_pos_generic_name"
PACK_FIELD = "retailmind_old_pos_pack"
COMPANY_CODE_FIELD = "retailmind_old_pos_company_code"
RACK_FIELD = "retailmind_old_pos_rack"
UNITS_PER_PACK_FIELD = "retailmind_units_per_pack"
LOCKED_FOR_SALE_FIELD = "retailmind_locked_for_sale"

_GENERIC_PLACEHOLDERS = frozenset({"0", ".", "-", "--", "n/a", "na", "none", "null", "unknown"})
_UNSAFE_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _permission_denied(message: str):
    frappe.throw(message, frappe.PermissionError)


def _normalize_generic(value: Any) -> str:
    normalized = re.sub(r"\s+", " ", cstr(value).strip()).casefold()
    return "" if normalized in _GENERIC_PLACEHOLDERS else normalized


def _safe_text(value: Any) -> str | None:
    cleaned = _UNSAFE_CONTROL_CHARACTERS.sub("", cstr(value)).strip()
    return cleaned or None


def _installed_item_fields() -> tuple[str, ...]:
    optional_fields = (
        GENERIC_CODE_FIELD,
        GENERIC_NAME_FIELD,
        PACK_FIELD,
        COMPANY_CODE_FIELD,
        RACK_FIELD,
        UNITS_PER_PACK_FIELD,
        LOCKED_FOR_SALE_FIELD,
        "end_of_life",
        "valuation_rate",
        "last_purchase_rate",
    )
    return tuple(field for field in optional_fields if frappe.db.has_column("Item", field))


def _generic_identity(item_doc: Any, installed_fields: Sequence[str]) -> tuple[str, str, str]:
    installed = set(installed_fields)
    for fieldname in (GENERIC_CODE_FIELD, GENERIC_NAME_FIELD):
        if fieldname not in installed:
            continue
        raw_value = cstr(item_doc.get(fieldname)).strip()
        normalized = _normalize_generic(raw_value)
        if normalized:
            return fieldname, normalized, raw_value
    return "", "", ""


def _resolve_item_groups(profile_name: str) -> tuple[str, ...]:
    return tuple(sorted(set(expand_item_groups(get_item_groups(profile_name) or []) or [])))


def _resolve_customer_groups(profile_doc: Any) -> tuple[str, ...]:
    groups: set[str] = set()
    for row in profile_doc.get("customer_groups") or []:
        root = cstr(row.get("customer_group") if row else "").strip()
        if not root:
            continue
        bounds = frappe.db.get_value("Customer Group", root, ["lft", "rgt"])
        if not bounds:
            continue
        lft, rgt = bounds
        groups.update(
            frappe.get_all(
                "Customer Group",
                filters={"lft": [">=", lft], "rgt": ["<=", rgt]},
                pluck="name",
                limit_page_length=100000,
            )
            or []
        )
    return tuple(sorted(groups))


def _resolve_warehouse(profile_doc: Any, requested_warehouse=None) -> tuple[str, tuple[str, ...]]:
    warehouse = cstr(profile_doc.get("warehouse")).strip()
    if not warehouse:
        frappe.throw(_("The active POS Profile does not have a warehouse."))

    requested = cstr(requested_warehouse).strip()
    if requested and requested != warehouse:
        _permission_denied(_("Warehouse {0} is not available in this POS Profile.").format(requested))

    warehouse_row = frappe.db.get_value(
        "Warehouse",
        warehouse,
        ["company", "is_group", "disabled"],
        as_dict=True,
    )
    if not warehouse_row or cint(warehouse_row.get("disabled")):
        _permission_denied(_("Warehouse {0} is not available.").format(warehouse))
    if cstr(warehouse_row.get("company")).strip() != cstr(profile_doc.get("company")).strip():
        _permission_denied(_("The POS Profile warehouse does not belong to its company."))

    if not cint(warehouse_row.get("is_group")):
        return warehouse, (warehouse,)

    descendants = frappe.db.get_descendants("Warehouse", warehouse) or []
    concrete = tuple(
        sorted(
            frappe.get_all(
                "Warehouse",
                filters={
                    "name": ["in", descendants],
                    "company": profile_doc.get("company"),
                    "disabled": 0,
                    "is_group": 0,
                },
                pluck="name",
                limit_page_length=max(len(descendants), 1),
            )
            or []
        )
    )
    if not concrete:
        frappe.throw(_("Warehouse group {0} has no selling warehouses.").format(warehouse))
    return warehouse, concrete


def _authorize_customer(profile_doc: Any, customer=None):
    customer_name = cstr(customer).strip()
    if not customer_name:
        return None

    customer_doc = frappe.get_doc("Customer", customer_name)
    customer_doc.check_permission("read")
    if cint(customer_doc.get("disabled")):
        _permission_denied(_("Customer {0} is disabled.").format(customer_name))

    allowed_groups = _resolve_customer_groups(profile_doc)
    if allowed_groups and customer_doc.get("customer_group") not in allowed_groups:
        _permission_denied(_("Customer {0} is not available in this POS Profile.").format(customer_name))
    return customer_doc


def _resolve_price_list(profile_doc: Any, requested_price_list=None, customer=None) -> tuple[str, str]:
    customer_doc = _authorize_customer(profile_doc, customer)
    price_list = cstr(customer_doc.get("default_price_list") if customer_doc else "").strip()
    if not price_list and customer_doc and customer_doc.get("customer_group"):
        price_list = cstr(
            frappe.db.get_value(
                "Customer Group",
                customer_doc.get("customer_group"),
                "default_price_list",
            )
        ).strip()
    if not price_list:
        price_list = cstr(profile_doc.get("selling_price_list")).strip()
    if not price_list:
        frappe.throw(_("The active POS Profile does not have a selling price list."))

    requested = cstr(requested_price_list).strip()
    if requested and requested != price_list:
        _permission_denied(_("Price List {0} is not active for this sale.").format(requested))

    price_list_row = frappe.db.get_value(
        "Price List",
        price_list,
        ["enabled", "selling", "currency"],
        as_dict=True,
    )
    if not price_list_row or not cint(price_list_row.get("enabled")) or not cint(
        price_list_row.get("selling")
    ):
        _permission_denied(_("Price List {0} is not available for selling.").format(price_list))
    return price_list, cstr(price_list_row.get("currency")).strip()


def _candidate_select_fields(installed_fields: Sequence[str]) -> list[str]:
    fields = [
        "item_code",
        "item_name",
        "item_group",
        "brand",
        "stock_uom",
        "is_stock_item",
        "has_batch_no",
        "has_serial_no",
        "has_variants",
        "variant_of",
        *installed_fields,
    ]
    return list(dict.fromkeys(fields))


def _query_candidate_item_rows(
    generic_field: str,
    generic_value: str,
    item_groups: tuple[str, ...],
    show_template_items: bool,
    hide_variant_items: bool,
    installed_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    if generic_field not in {GENERIC_CODE_FIELD, GENERIC_NAME_FIELD}:
        return []

    conditions = [
        "item.disabled = 0",
        "item.is_sales_item = 1",
        "item.is_fixed_asset = 0",
        "item.is_stock_item = 1",
        f"lower(trim(item.`{generic_field}`)) = %(generic_value)s",
    ]
    params: dict[str, Any] = {"generic_value": generic_value}
    if item_groups:
        conditions.append("item.item_group in %(item_groups)s")
        params["item_groups"] = item_groups
    if not show_template_items:
        conditions.append("item.has_variants = 0")
    if hide_variant_items:
        conditions.append("(item.variant_of is null or item.variant_of = '')")
    if LOCKED_FOR_SALE_FIELD in installed_fields:
        conditions.append(f"ifnull(item.`{LOCKED_FOR_SALE_FIELD}`, 0) = 0")

    select_fields = ", ".join(f"item.`{field}`" for field in _candidate_select_fields(installed_fields))
    return frappe.db.sql(
        f"""
        select {select_fields}
        from `tabItem` item
        where {" and ".join(conditions)}
        order by item.item_code asc
        limit {ALTERNATE_CANDIDATE_QUERY_LIMIT}
        """,
        params,
        as_dict=True,
    )


@redis_cache(ttl=ALTERNATE_CANDIDATE_CACHE_TTL_SECONDS)
def _get_candidate_item_rows(
    generic_field: str,
    generic_value: str,
    item_groups: tuple[str, ...],
    show_template_items: bool,
    hide_variant_items: bool,
    installed_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    return _query_candidate_item_rows(
        generic_field,
        generic_value,
        item_groups,
        show_template_items,
        hide_variant_items,
        installed_fields,
    )


def clear_alternate_item_caches(doc=None, method=None):
    clearer = getattr(_get_candidate_item_rows, "clear_cache", None)
    if callable(clearer):
        clearer()


def _live_stock_map(
    warehouses: Sequence[str], item_codes: Sequence[str]
) -> dict[str, dict[str, Any]]:
    if not warehouses or not item_codes:
        return {}
    rows = frappe.db.sql(
        """
        select item_code, sum(actual_qty) as actual_qty,
               case when sum(actual_qty) > 0
                    then sum(actual_qty * ifnull(valuation_rate, 0)) / sum(actual_qty)
                    else null end as valuation_rate
        from `tabBin`
        where warehouse in %(warehouses)s and item_code in %(item_codes)s
        group by item_code
        """,
        {"warehouses": tuple(warehouses), "item_codes": tuple(item_codes)},
        as_dict=True,
    )
    return {
        row.get("item_code"): {
            "actual_qty": flt(row.get("actual_qty")),
            "valuation_rate": row.get("valuation_rate"),
        }
        for row in rows
        if row.get("item_code")
    }


def _live_batch_qty(warehouses: Sequence[str], item_codes: Sequence[str]) -> dict[str, float]:
    rows = _fetch_batches(tuple(warehouses), tuple(item_codes)) if item_codes else []
    today = getdate(nowdate())
    quantities: dict[str, float] = {}
    for row in rows or []:
        expiry = row.get("expiry_date")
        if expiry and getdate(expiry) <= today:
            continue
        qty = max(flt(row.get("batch_qty")), 0)
        if qty:
            quantities[row.get("item_code")] = quantities.get(row.get("item_code"), 0) + qty
    return quantities


def _live_serial_qty(warehouses: Sequence[str], item_codes: Sequence[str]) -> dict[str, float]:
    if not warehouses or not item_codes:
        return {}
    rows = frappe.get_all(
        "Serial No",
        filters={
            "warehouse": ["in", list(warehouses)],
            "item_code": ["in", list(item_codes)],
            "status": "Active",
        },
        fields=["item_code"],
        limit_page_length=100000,
    )
    quantities: dict[str, float] = {}
    for row in rows:
        code = row.get("item_code")
        quantities[code] = quantities.get(code, 0) + 1
    return quantities


def _select_price_rows(
    rows: Iterable[Any],
    candidates: dict[str, dict[str, Any]],
    customer: str,
) -> dict[str, float]:
    selected: dict[str, tuple[tuple[int, int, str], float]] = {}
    for row in rows or []:
        code = row.get("item_code")
        candidate = candidates.get(code)
        if not candidate:
            continue
        row_customer = cstr(row.get("customer")).strip()
        if row_customer and row_customer != customer:
            continue
        row_uom = cstr(row.get("uom")).strip()
        stock_uom = cstr(candidate.get("stock_uom")).strip()
        if row_uom and row_uom != stock_uom:
            continue
        rate = flt(row.get("price_list_rate"))
        if rate <= 0:
            continue
        score = (
            1 if row_customer and row_customer == customer else 0,
            1 if row_uom == stock_uom and row_uom else 0,
            cstr(row.get("valid_from")),
        )
        if code not in selected or score > selected[code][0]:
            selected[code] = (score, rate)
    return {code: rate for code, (_score, rate) in selected.items()}


def _live_price_map(
    price_list: str,
    item_codes: Sequence[str],
    candidates: dict[str, dict[str, Any]],
    customer: str = "",
    *,
    buying: bool = False,
) -> dict[str, float]:
    if not price_list or not item_codes:
        return {}
    rows = frappe.db.sql(
        """
        select item_code, price_list_rate, uom, customer, valid_from
        from `tabItem Price`
        where price_list = %(price_list)s
          and item_code in %(item_codes)s
          and ifnull(buying, 0) = %(buying)s
          and ifnull(selling, 0) = %(selling)s
          and (valid_from is null or valid_from <= %(today)s)
          and (valid_upto is null or valid_upto = '' or valid_upto >= %(today)s)
          and ifnull(customer, '') in ('', %(customer)s)
        """,
        {
            "price_list": price_list,
            "item_codes": tuple(item_codes),
            "buying": 1 if buying else 0,
            "selling": 0 if buying else 1,
            "today": nowdate(),
            "customer": customer,
        },
        as_dict=True,
    )
    return _select_price_rows(rows, candidates, customer)


def _cost_map(
    profile_doc: Any,
    candidates: dict[str, dict[str, Any]],
    stock_map: dict[str, dict[str, Any]],
    target_currency: str,
) -> dict[str, dict[str, Any]]:
    item_codes = tuple(candidates)
    buying_price_list = _resolve_buying_price_list(cstr(profile_doc.get("name")).strip())
    buying_currency = cstr(
        frappe.db.get_value("Price List", buying_price_list, "currency")
        if buying_price_list
        else ""
    ).strip()
    company_currency = cstr(
        frappe.db.get_value("Company", profile_doc.get("company"), "default_currency")
    ).strip()
    buying_prices = _live_price_map(
        buying_price_list,
        item_codes,
        candidates,
        buying=True,
    ) if buying_price_list else {}

    conversion_rates: dict[str, float | None] = {}

    def converted(rate: Any, source_currency: str) -> float | None:
        value = flt(rate)
        if value <= 0:
            return None
        source = cstr(source_currency).strip()
        target = cstr(target_currency).strip()
        if not source or not target:
            return None
        if source == target:
            return value
        if source not in conversion_rates:
            try:
                exchange_rate = flt(get_exchange_rate(source, target, nowdate(), "for_buying"))
                conversion_rates[source] = exchange_rate if exchange_rate > 0 else None
            except Exception:
                conversion_rates[source] = None
                frappe.log_error(
                    f"Missing exchange rate from {source} to {target} for POS alternate ranking",
                    "POS alternate item cost conversion",
                )
        exchange_rate = conversion_rates[source]
        return flt(value * exchange_rate) if exchange_rate else None

    result: dict[str, dict[str, Any]] = {}
    for code, candidate in candidates.items():
        buying_rate = converted(buying_prices.get(code), buying_currency)
        if buying_rate:
            result[code] = {"rate": buying_rate, "source": "buying_price"}
            continue
        warehouse_valuation = (stock_map.get(code) or {}).get("valuation_rate")
        converted_valuation = converted(warehouse_valuation, company_currency)
        if converted_valuation:
            result[code] = {"rate": converted_valuation, "source": "warehouse_valuation"}
            continue
        for fieldname, source in (
            ("valuation_rate", "item_valuation"),
            ("last_purchase_rate", "last_purchase"),
        ):
            converted_value = converted(candidate.get(fieldname), company_currency)
            if converted_value:
                result[code] = {"rate": converted_value, "source": source}
                break
    return result


def _available_qty(
    candidate: dict[str, Any],
    stock_map: dict[str, dict[str, Any]],
    batch_map: dict[str, float],
    serial_map: dict[str, float],
) -> float:
    code = candidate.get("item_code")
    bin_qty = max(flt((stock_map.get(code) or {}).get("actual_qty")), 0)
    if cint(candidate.get("has_serial_no")):
        return min(bin_qty, max(flt(serial_map.get(code)), 0))
    if cint(candidate.get("has_batch_no")):
        return min(bin_qty, max(flt(batch_map.get(code)), 0))
    return bin_qty


def _project_candidate(
    candidate: dict[str, Any],
    *,
    rate: float,
    available_qty: float,
    requested_qty: float,
    currency: str,
    cost: dict[str, Any] | None,
    profit_visible: bool,
) -> dict[str, Any]:
    units_per_pack = max(flt(candidate.get(UNITS_PER_PACK_FIELD)), 1)
    whole_packs = math.floor((available_qty + 0.0000001) / units_per_pack)
    loose_stock = max(available_qty - (whole_packs * units_per_pack), 0)
    row = {
        "item_code": _safe_text(candidate.get("item_code")),
        "item_name": _safe_text(candidate.get("item_name")),
        "item_group": _safe_text(candidate.get("item_group")),
        "generic_code": _safe_text(candidate.get(GENERIC_CODE_FIELD)),
        "generic_name": _safe_text(candidate.get(GENERIC_NAME_FIELD)),
        "pack": _safe_text(candidate.get(PACK_FIELD)),
        "company": _safe_text(candidate.get("brand"))
        or _safe_text(candidate.get(COMPANY_CODE_FIELD)),
        "company_code": _safe_text(candidate.get(COMPANY_CODE_FIELD)),
        "rack": _safe_text(candidate.get(RACK_FIELD)),
        "stock_uom": _safe_text(candidate.get("stock_uom")),
        "uom": _safe_text(candidate.get("stock_uom")),
        "units_per_pack": units_per_pack,
        "available_qty": flt(available_qty),
        "pack_stock": whole_packs,
        "loose_stock": flt(loose_stock),
        "rate": flt(rate),
        "retail_price": flt(rate),
        "currency": currency,
        "has_batch_no": cint(candidate.get("has_batch_no")),
        "has_serial_no": cint(candidate.get("has_serial_no")),
        "can_fulfill_requested_qty": available_qty + 0.0000001 >= requested_qty,
        "replacement_reason": "same_generic_in_stock",
        "profit_display_allowed": bool(profit_visible),
    }
    if profit_visible and cost:
        profit_per_unit = flt(rate - flt(cost.get("rate")))
        row.update(
            {
                "cost_rate": flt(cost.get("rate")),
                "cost_source": cost.get("source"),
                "profit_per_unit": profit_per_unit,
                "profit_amount": flt(profit_per_unit * requested_qty),
                "margin_percent": flt((profit_per_unit / rate) * 100) if rate else None,
            }
        )
    return row


def _rank_candidates(
    rows: list[dict[str, Any]],
    hidden_profit_scores: dict[str, float | None],
) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            hidden_profit_scores.get(row.get("item_code")) is None,
            -(hidden_profit_scores.get(row.get("item_code")) or 0),
            -flt(row.get("available_qty")),
            cstr(row.get("item_name")).casefold(),
            cstr(row.get("item_code")),
        ),
    )


@frappe.whitelist()
def get_alternate_items(
    item_code,
    pos_profile=None,
    warehouse=None,
    price_list=None,
    company=None,
    customer=None,
    qty=1,
    limit=ALTERNATE_RESULT_LIMIT,
):
    profile_doc = get_authorized_pos_profile(pos_profile, company=company)
    requested_item = get_authorized_pos_item(item_code, profile_doc)
    active_cashier = get_active_terminal_cashier(profile_doc.get("name"))

    profile_name = cstr(profile_doc.get("name")).strip()
    company_name = cstr(profile_doc.get("company")).strip()
    warehouse_name, warehouses = _resolve_warehouse(profile_doc, warehouse)
    price_list_name, currency = _resolve_price_list(profile_doc, price_list, customer)
    requested_qty = max(flt(qty), 0.000001)
    result_limit = max(1, min(cint(limit) or ALTERNATE_RESULT_LIMIT, ALTERNATE_RESULT_LIMIT_MAX))

    installed_fields = _installed_item_fields()
    generic_field, generic_key, generic_label = _generic_identity(requested_item, installed_fields)
    response = {
        "requested_item": {
            "item_code": requested_item.get("item_code") or requested_item.get("name"),
            "item_name": requested_item.get("item_name"),
            "generic_field": generic_field or None,
            "generic": generic_label or None,
        },
        "context": {
            "pos_profile": profile_name,
            "company": company_name,
            "warehouse": warehouse_name,
            "price_list": price_list_name,
            "currency": currency,
            "requested_qty": requested_qty,
        },
        "profit_display_allowed": bool(user_can_manage_pos(active_cashier)),
        "items": [],
        "reason": None,
        "candidate_metadata_ttl_seconds": ALTERNATE_CANDIDATE_CACHE_TTL_SECONDS,
        "candidate_pool_truncated": False,
        "stock_cached": False,
    }
    if not generic_key:
        response["reason"] = "missing_generic"
        return response

    item_groups = _resolve_item_groups(profile_name)
    candidate_rows = _get_candidate_item_rows(
        generic_field,
        generic_key,
        item_groups,
        bool(cint(profile_doc.get("posa_show_template_items"))),
        bool(cint(profile_doc.get("posa_hide_variants_items"))),
        installed_fields,
    ) or []
    response["candidate_pool_truncated"] = (
        len(candidate_rows) >= ALTERNATE_CANDIDATE_QUERY_LIMIT
    )
    requested_code = cstr(requested_item.get("item_code") or requested_item.get("name")).strip()
    candidates = {
        cstr(row.get("item_code")).strip(): dict(row)
        for row in candidate_rows
        if cstr(row.get("item_code")).strip() != requested_code
        and _normalize_generic(row.get(generic_field)) == generic_key
        and not (
            row.get("end_of_life")
            and getdate(row.get("end_of_life")) < getdate(nowdate())
        )
    }
    if not candidates:
        response["reason"] = "no_same_generic_items"
        return response

    item_codes = tuple(candidates)
    stock_map = _live_stock_map(warehouses, item_codes)
    candidates = {
        code: row
        for code, row in candidates.items()
        if flt((stock_map.get(code) or {}).get("actual_qty")) > 0
    }
    if not candidates:
        response["reason"] = "no_in_stock_priced_alternates"
        return response

    item_codes = tuple(candidates)
    stock_map = {code: stock_map[code] for code in item_codes}
    batch_codes = [code for code, row in candidates.items() if cint(row.get("has_batch_no"))]
    serial_codes = [code for code, row in candidates.items() if cint(row.get("has_serial_no"))]
    batch_map = _live_batch_qty(warehouses, batch_codes)
    serial_map = _live_serial_qty(warehouses, serial_codes)
    selling_prices = _live_price_map(
        price_list_name,
        item_codes,
        candidates,
        cstr(customer).strip(),
    )
    costs = _cost_map(profile_doc, candidates, stock_map, currency)

    projected: list[dict[str, Any]] = []
    hidden_profit_scores: dict[str, float | None] = {}
    for code, candidate in candidates.items():
        available_qty = _available_qty(candidate, stock_map, batch_map, serial_map)
        rate = flt(selling_prices.get(code))
        if available_qty <= 0 or rate <= 0:
            continue
        cost = costs.get(code)
        hidden_profit_scores[code] = (
            flt((rate - flt(cost.get("rate"))) * requested_qty) if cost else None
        )
        projected.append(
            _project_candidate(
                candidate,
                rate=rate,
                available_qty=available_qty,
                requested_qty=requested_qty,
                currency=currency,
                cost=cost,
                profit_visible=response["profit_display_allowed"],
            )
        )

    response["items"] = _rank_candidates(projected, hidden_profit_scores)[:result_limit]
    if not response["items"]:
        response["reason"] = "no_in_stock_priced_alternates"
    return response


__all__ = [
    "ALTERNATE_CANDIDATE_CACHE_TTL_SECONDS",
    "clear_alternate_item_caches",
    "get_alternate_items",
]
