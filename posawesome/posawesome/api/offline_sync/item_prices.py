import frappe

from posawesome.posawesome.api.offline_sync.common import (
    _build_response as _build_common_response,
    _coerce_limit,
    _coerce_offset,
    _max_timestamp,
    _resolve_profile,
)
from posawesome.posawesome.api.item_sale_controls import _resolve_buying_price_list

SYNC_SCHEMA_VERSION = "2026-07-17"

ITEM_PRICE_FIELDS = [
    "name",
    "price_list",
    "item_code",
    "uom",
    "currency",
    "customer",
    "supplier",
    "buying",
    "selling",
    "price_list_rate",
    "valid_from",
    "valid_upto",
    "modified",
]


def _build_response(**kwargs):
    response = _build_common_response(**kwargs)
    response["schema_version"] = SYNC_SCHEMA_VERSION
    return response


def _profile_price_lists(profile):
    """Return every selling list plus the profile's authoritative buying floor list."""

    rows = (
        frappe.get_all(
            "Price List",
            filters={"selling": 1},
            fields=["name"],
            order_by="name asc",
        )
        or []
    )
    names = [row.get("name") for row in rows if row.get("name")]
    selected = profile.get("selling_price_list")
    if selected and selected not in names:
        names.append(selected)
    buying_price_list = _resolve_buying_price_list(profile.get("name"))
    if buying_price_list and buying_price_list not in names:
        names.append(buying_price_list)
    return sorted(set(names)), buying_price_list


def _deleted_item_prices(watermark):
    if not watermark:
        return []
    rows = (
        frappe.get_all(
            "Deleted Document",
            filters={
                "deleted_doctype": "Item Price",
                "creation": [">", watermark],
            },
            fields=["deleted_name", "creation"],
            order_by="creation asc, deleted_name asc",
        )
        or []
    )
    return [
        {
            "key": f"item_price::{row.get('deleted_name')}",
            "modified": row.get("creation"),
        }
        for row in rows
        if row.get("deleted_name")
    ]


def _price_list_not_uom_dependent_map(price_lists):
    """Map each synced Price List name to its ``price_not_uom_dependent`` flag."""

    if not price_lists:
        return {}
    rows = (
        frappe.get_all(
            "Price List",
            filters={"name": ("in", list(price_lists))},
            fields=["name", "price_not_uom_dependent"],
        )
        or []
    )
    flag_map = {
        row.get("name"): row.get("price_not_uom_dependent") in (1, True, "1")
        for row in rows
        if row.get("name")
    }
    db = getattr(frappe, "db", None)
    if db and callable(getattr(db, "get_value", None)):
        for price_list in price_lists:
            if price_list not in flag_map:
                val = db.get_value("Price List", price_list, "price_not_uom_dependent")
                flag_map[price_list] = val in (1, True, "1")
    return flag_map


@frappe.whitelist()
def sync_item_prices(
    pos_profile=None,
    watermark=None,
    offset=0,
    limit=500,
    schema_version=None,
):
    if schema_version and schema_version != SYNC_SCHEMA_VERSION:
        return _build_response(full_resync_required=True)

    profile = _resolve_profile(pos_profile)
    if not profile:
        frappe.throw("pos_profile is required")

    price_lists, buying_price_list = _profile_price_lists(profile)
    if not price_lists:
        response = _build_response(next_watermark=watermark)
        response["scope"] = {
            "price_lists": [],
            "buying_price_list": buying_price_list,
            "buying_price_not_uom_dependent": 0,
        }
        return response

    not_uom_dependent_map = _price_list_not_uom_dependent_map(price_lists)
    resolved_offset = _coerce_offset(offset)
    resolved_limit = _coerce_limit(limit, 500)
    filters = {"price_list": ("in", price_lists)}
    if watermark:
        filters["modified"] = [">", watermark]

    rows = (
        frappe.get_all(
            "Item Price",
            filters=filters,
            fields=ITEM_PRICE_FIELDS,
            order_by="modified asc, name asc",
            start=resolved_offset,
            limit_page_length=resolved_limit + 1,
        )
        or []
    )
    has_more = len(rows) > resolved_limit
    page_rows = rows[:resolved_limit]
    deleted_rows = _deleted_item_prices(watermark)

    changes = [
        {
            "key": f"item_price::{row.get('name')}",
            "modified": row.get("modified"),
            "data": {
                **dict(row),
                "price_not_uom_dependent": (
                    1 if not_uom_dependent_map.get(row.get("price_list")) else 0
                ),
            },
        }
        for row in page_rows
        if row.get("name")
    ]
    deleted = [{"key": row["key"]} for row in deleted_rows]
    next_watermark = None
    if not has_more:
        next_watermark = _max_timestamp(
            watermark,
            [row.get("modified") for row in page_rows],
            [row.get("modified") for row in deleted_rows],
        )

    response = _build_response(
        changes=changes,
        deleted=deleted,
        next_watermark=next_watermark,
        has_more=has_more,
    )
    response["next_offset"] = resolved_offset + len(page_rows) if has_more else None
    response["scope"] = {
        "price_lists": price_lists,
        "buying_price_list": buying_price_list,
        "buying_price_not_uom_dependent": (
            1 if not_uom_dependent_map.get(buying_price_list) else 0
        ),
    }
    return response
