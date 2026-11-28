import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import Mock, patch


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


class PrecisionDoc(dict):
    """Frappe Document stand-in: dict access plus currency field precision()."""

    def __init__(self, *args, precision_value=2, **kwargs):
        super().__init__(*args, **kwargs)
        self._precision_value = precision_value

    def precision(self, fieldname, parentfield=None):
        return self._precision_value


class AttrDict(dict):
    """frappe._dict stand-in: any missing attribute, precision included, is None."""

    __getattr__ = dict.get


class FakeMeta:
    def has_field(self, fieldname):
        return fieldname in {
            "retailmind_locked_for_sale",
            "retailmind_non_discountable",
            "retailmind_controlled_item",
            "retailmind_short_name",
            "buying_price_list",
        }


def _install_frappe_stub():
    frappe_module = types.ModuleType("frappe")
    frappe_module._ = lambda text: text
    frappe_module.PermissionError = PermissionError
    frappe_module.throw = lambda message, exception=None: (_ for _ in ()).throw(
        (exception or Exception)(message)
    )
    frappe_module.get_meta = lambda doctype: FakeMeta()
    frappe_module.get_all = lambda *args, **kwargs: []
    frappe_module.db = types.SimpleNamespace(
        get_value=lambda *args, **kwargs: None,
        get_single_value=lambda *args, **kwargs: None,
        exists=lambda *args, **kwargs: False,
    )
    sys.modules["frappe"] = frappe_module

    frappe_utils = types.ModuleType("frappe.utils")
    frappe_utils.flt = lambda value, precision=None: (
        float(value or 0) if precision is None else round(float(value or 0), int(precision))
    )
    frappe_utils.cint = lambda value: int(value or 0)
    frappe_utils.cstr = lambda value: str(value or "")
    frappe_utils.nowdate = lambda: "2026-07-17"
    sys.modules["frappe.utils"] = frappe_utils

    return frappe_module


def _load_module():
    module_name = "posawesome.posawesome.api.item_sale_controls"
    file_path = REPO_ROOT / "posawesome" / "posawesome" / "api" / "item_sale_controls.py"
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class TestItemSaleControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frappe = _install_frappe_stub()
        cls.controls = _load_module()

    def setUp(self):
        self.frappe.get_all = lambda *args, **kwargs: []
        self.frappe.db.get_value = lambda *args, **kwargs: None
        self.frappe.db.get_single_value = lambda *args, **kwargs: None
        self.frappe.db.exists = lambda *args, **kwargs: False
        self.global_uom_factors = {}
        self.global_uom_calls = []
        item_module = types.ModuleType("erpnext.stock.doctype.item.item")

        def get_uom_conv_factor(uom, stock_uom):
            self.global_uom_calls.append((uom, stock_uom))
            return self.global_uom_factors.get((uom, stock_uom))

        item_module.get_uom_conv_factor = get_uom_conv_factor
        sys.modules["erpnext.stock.doctype.item.item"] = item_module

    def _install_buying_price(self, rate=12.75, uom="Nos", currency="PKR"):
        self.frappe.db.get_single_value = lambda *args, **kwargs: "Standard Buying"
        self.frappe.db.get_value = lambda doctype, *args, **kwargs: (
            currency if doctype == "Price List" else None
        )

        def get_all(doctype, *args, **kwargs):
            if doctype == "Item Price":
                return [
                    {
                        "name": "IP-LOW-NOS",
                        "item_code": "LOW",
                        "price_list": "Standard Buying",
                        "price_list_rate": rate,
                        "currency": currency,
                        "uom": uom,
                        "valid_from": "2026-01-01",
                    }
                ]
            if doctype == "Item":
                return [{"item_code": "LOW", "stock_uom": "Nos"}]
            if doctype == "UOM Conversion Detail":
                return []
            return []

        self.frappe.get_all = get_all

    def test_locked_item_blocks_sale(self):
        self.frappe.get_all = lambda *args, **kwargs: [
            {
                "item_code": "LOCKED",
                "item_name": "Locked Item",
                "retailmind_locked_for_sale": 1,
                "retailmind_non_discountable": 0,
            }
        ]

        errors = self.controls.collect_item_sale_control_errors(
            [{"item_code": "LOCKED", "item_name": "Locked Item", "qty": 1}]
        )

        self.assertEqual(errors[0]["reason"], "locked_for_sale")
        self.assertEqual(errors[0]["policy"], "block")

    def test_non_discountable_item_blocks_line_discount(self):
        self.frappe.get_all = lambda *args, **kwargs: [
            {
                "item_code": "NO-DISC",
                "item_name": "No Discount",
                "retailmind_locked_for_sale": 0,
                "retailmind_non_discountable": 1,
            }
        ]

        errors = self.controls.collect_item_sale_control_errors(
            [
                {
                    "item_code": "NO-DISC",
                    "item_name": "No Discount",
                    "discount_percentage": 5,
                }
            ]
        )

        self.assertEqual(errors[0]["reason"], "non_discountable")
        self.assertEqual(errors[0]["policy"], "block")

    def test_non_discountable_item_allows_zero_discount(self):
        self.frappe.get_all = lambda *args, **kwargs: [
            {
                "item_code": "NO-DISC",
                "item_name": "No Discount",
                "retailmind_locked_for_sale": 0,
                "retailmind_non_discountable": 1,
            }
        ]

        errors = self.controls.collect_item_sale_control_errors(
            [{"item_code": "NO-DISC", "item_name": "No Discount"}]
        )

        self.assertEqual(errors, [])

    def test_database_buying_price_blocks_sale(self):
        self._install_buying_price()
        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "LOW",
                    "item_name": "Below Cost",
                    "qty": 1,
                    "rate": 10,
                    "uom": "Nos",
                }
            ]
        )

        self.assertEqual(errors[0]["reason"], "below_buying_price")
        self.assertEqual(errors[0]["policy"], "block")

    def test_buying_price_sale_control_queries_price_list_when_row_has_no_trade_price(self):
        self._install_buying_price()

        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "LOW",
                    "item_name": "Below Cost",
                    "qty": 1,
                    "rate": 10,
                    "uom": "Nos",
                }
            ]
        )

        self.assertEqual(errors[0]["reason"], "below_buying_price")

    def test_client_trade_price_cannot_lower_database_floor(self):
        self._install_buying_price(rate=12.75)

        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "LOW",
                    "item_name": "Below Cost",
                    "qty": 1,
                    "rate": 10,
                    "uom": "Nos",
                    "trade_price": 1,
                    "buying_rate": 1,
                }
            ]
        )

        self.assertEqual(errors[0]["buying_rate"], 12.75)

    def test_stock_uom_price_is_converted_for_selected_uom(self):
        self._install_buying_price(rate=12.75, uom="Nos")
        original_get_all = self.frappe.get_all

        def get_all(doctype, *args, **kwargs):
            if doctype == "UOM Conversion Detail":
                return [
                    {
                        "parent": "LOW",
                        "uom": "Box",
                        "conversion_factor": 10,
                    }
                ]
            return original_get_all(doctype, *args, **kwargs)

        self.frappe.get_all = get_all

        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "LOW",
                    "item_name": "Below Cost",
                    "qty": 1,
                    "rate": 100,
                    "uom": "Box",
                }
            ]
        )

        self.assertEqual(errors[0]["buying_rate"], 127.5)

    def test_exact_selected_uom_price_wins_without_conversion(self):
        self._install_buying_price(rate=120, uom="Box")
        original_get_all = self.frappe.get_all

        def get_all(doctype, *args, **kwargs):
            if doctype == "UOM Conversion Detail":
                return [
                    {
                        "parent": "LOW",
                        "uom": "Box",
                        "conversion_factor": 10,
                    }
                ]
            return original_get_all(doctype, *args, **kwargs)

        self.frappe.get_all = get_all

        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "LOW",
                    "item_name": "Below Cost",
                    "qty": 1,
                    "rate": 115,
                    "uom": "Box",
                }
            ]
        )

        self.assertEqual(errors[0]["buying_rate"], 120)

    def test_buying_floor_is_converted_to_invoice_currency(self):
        self._install_buying_price(rate=12.75, currency="USD")
        self.frappe.db.get_value = lambda doctype, *args, **kwargs: {
            "Price List": "USD",
            "Company": "PKR",
        }.get(doctype)
        erpnext_module = types.ModuleType("erpnext")
        erpnext_setup = types.ModuleType("erpnext.setup")
        erpnext_utils = types.ModuleType("erpnext.setup.utils")
        erpnext_utils.get_exchange_rate = lambda *args, **kwargs: 280
        sys.modules["erpnext"] = erpnext_module
        sys.modules["erpnext.setup"] = erpnext_setup
        sys.modules["erpnext.setup.utils"] = erpnext_utils

        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "LOW",
                    "item_name": "Below Cost",
                    "qty": 1,
                    "rate": 3500,
                    "uom": "Nos",
                }
            ],
            invoice_doc={
                "company": "Test Company",
                "currency": "PKR",
                "conversion_rate": 1,
                "posting_date": "2026-07-17",
            },
        )

        self.assertEqual(errors[0]["buying_rate"], 3570)

    def test_sale_floor_policy_defaults_preserve_hard_block(self):
        policy = self.controls.resolve_sale_floor_policy("Main POS")

        self.assertTrue(policy["enabled"])
        self.assertEqual(policy["action"], "Block")
        self.assertEqual(policy["missing_cost_action"], "Allow")

    def test_minimum_margin_raises_permitted_floor(self):
        self._install_buying_price(rate=100)

        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "LOW",
                    "item_name": "Below Margin",
                    "qty": 1,
                    "rate": 104,
                    "uom": "Nos",
                }
            ],
            policy={
                "enabled": True,
                "action": "Block",
                "minimum_margin_percentage": 5,
                "missing_cost_action": "Allow",
            },
        )

        self.assertEqual(errors[0]["minimum_selling_rate"], 105)

    def test_invoice_level_discount_is_included_in_floor_check(self):
        self._install_buying_price(rate=100)

        errors = self.controls.collect_below_buying_price_errors(
            [{"item_code": "LOW", "qty": 1, "rate": 105, "uom": "Nos"}],
            invoice_doc={"additional_discount_percentage": 10},
        )

        self.assertAlmostEqual(errors[0]["selling_rate"], 94.5)

    def test_missing_cost_can_be_configured_to_block(self):
        errors = self.controls.collect_below_buying_price_errors(
            [{"item_code": "NO-COST", "item_name": "No Cost", "qty": 1, "rate": 10}],
            policy={
                "enabled": True,
                "action": "Block",
                "minimum_margin_percentage": 0,
                "missing_cost_action": "Block",
            },
        )

        self.assertEqual(errors[0]["reason"], "missing_buying_price")

    def test_missing_cost_block_cannot_be_downgraded_to_warning(self):
        invoice = {"items": [{"item_code": "NO-COST", "qty": 1, "rate": 10}]}
        missing_error = {
            "item_code": "NO-COST",
            "reason": "missing_buying_price",
            "message": "missing buying floor",
        }

        with (
            patch.object(self.controls, "collect_item_sale_control_errors", return_value=[]),
            patch.object(
                self.controls,
                "collect_below_buying_price_errors",
                return_value=[missing_error],
            ),
            patch.object(
                self.controls,
                "resolve_sale_floor_policy",
                return_value={"enabled": True, "action": "Warning Only"},
            ),
        ):
            with self.assertRaisesRegex(Exception, "missing buying floor"):
                self.controls.validate_invoice_item_sale_controls(invoice)

    def test_pos_supervisor_override_records_server_authorized_actor_and_details(self):
        invoice = {
            "items": [{"item_code": "LOW", "qty": 1, "rate": 90}],
            "pos_profile": "Main POS",
            "posa_below_cost_override": 1,
            "posa_below_cost_override_reason": "Approved clearance",
        }
        floor_error = {
            "item_code": "LOW",
            "selling_rate": 90,
            "buying_rate": 100,
            "minimum_selling_rate": 100,
            "minimum_margin_percentage": 0,
            "buying_price_uom": "Nos",
            "buying_price_currency": "PKR",
            "message": "below floor",
        }

        with (
            patch.object(self.controls, "collect_item_sale_control_errors", return_value=[]),
            patch.object(
                self.controls,
                "collect_below_buying_price_errors",
                return_value=[floor_error],
            ),
            patch.object(
                self.controls,
                "resolve_sale_floor_policy",
                return_value={"enabled": True, "action": "POS Supervisor Override"},
            ),
            patch.object(
                self.controls,
                "_authorize_below_cost_override",
                return_value="supervisor@example.com",
            ),
        ):
            self.controls.validate_invoice_item_sale_controls(invoice)

        self.assertEqual(invoice["posa_below_cost_override_by"], "supervisor@example.com")
        self.assertIn('"item_code":"LOW"', invoice["posa_below_cost_override_details"])

    def test_general_manager_cannot_authorize_below_cost_override(self):
        employees = types.ModuleType("posawesome.posawesome.api.employees")
        employees._get_user_doc = lambda user: types.SimpleNamespace(name=user)
        employees._is_pos_supervisor = lambda user_doc: False
        pos_access = types.ModuleType("posawesome.posawesome.api.pos_access")
        pos_access.get_authenticated_pos_user = lambda: "manager@example.com"
        terminal_state = types.ModuleType("posawesome.posawesome.api.terminal_state")
        terminal_state.get_active_terminal_cashier = lambda profile: "manager@example.com"

        with patch.dict(
            sys.modules,
            {
                "posawesome.posawesome.api.employees": employees,
                "posawesome.posawesome.api.pos_access": pos_access,
                "posawesome.posawesome.api.terminal_state": terminal_state,
            },
        ):
            with self.assertRaisesRegex(PermissionError, "POS supervisor"):
                self.controls._authorize_below_cost_override("Main POS")

    def test_pos_invoice_hook_delegates_to_sale_control_validation(self):
        validator = Mock()
        invoice = {"doctype": "Sales Invoice", "is_pos": 1, "items": []}

        with patch.object(
            self.controls,
            "validate_invoice_item_sale_controls",
            validator,
        ):
            self.controls.validate_pos_invoice_item_sale_controls(invoice)

        validator.assert_called_once_with(invoice)

    def test_successful_validation_is_not_repeated_for_the_same_document(self):
        item_validator = Mock(return_value=[])
        price_validator = Mock(return_value=[])
        invoice = types.SimpleNamespace(
            flags=types.SimpleNamespace(),
            get=lambda key: [] if key == "items" else None,
        )

        with (
            patch.object(
                self.controls,
                "collect_item_sale_control_errors",
                item_validator,
            ),
            patch.object(
                self.controls,
                "collect_below_buying_price_errors",
                price_validator,
            ),
        ):
            self.controls.validate_invoice_item_sale_controls(invoice)
            self.controls.validate_invoice_item_sale_controls(invoice)

        item_validator.assert_called_once()
        price_validator.assert_called_once()

    def test_non_pos_sales_invoice_skips_pos_sale_controls(self):
        validator = Mock()
        invoice = {"doctype": "Sales Invoice", "is_pos": 0, "items": []}

        with patch.object(
            self.controls,
            "validate_invoice_item_sale_controls",
            validator,
        ):
            self.controls.validate_pos_invoice_item_sale_controls(invoice)

        validator.assert_not_called()

    def test_warehouse_bin_valuation_blocks_pos_sale_without_buying_price(self):
        self.frappe.db.get_value = lambda doctype, *args, **kwargs: (
            "UGX"
            if doctype == "Company"
            else ("Shop Floor - JBS" if doctype == "POS Profile" else None)
        )

        def get_all(doctype, *args, **kwargs):
            if doctype == "Item":
                return [
                    {
                        "item_code": "QB-524",
                        "stock_uom": "Nos",
                        "is_stock_item": 1,
                        "last_purchase_rate": 0,
                        "valuation_rate": 0,
                    }
                ]
            if doctype == "Bin":
                return [
                    {
                        "item_code": "QB-524",
                        "warehouse": "Shop Floor - JBS",
                        "valuation_rate": 4500,
                    }
                ]
            return []

        self.frappe.get_all = get_all

        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "QB-524",
                    "item_name": "RONG PLASTIC TOOTHPICKS WOODEN MED",
                    "qty": 1,
                    "rate": 2000,
                    "uom": "Nos",
                    "warehouse": "Shop Floor - JBS",
                }
            ],
            pos_profile="REWARD ESSENTIALS POS",
            invoice_doc={
                "company": "Rewards Essential Ltd",
                "currency": "UGX",
                "conversion_rate": 1,
                "posting_date": "2026-11-25",
            },
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["reason"], "below_buying_price")
        self.assertEqual(errors[0]["buying_rate"], 4500)
        self.assertEqual(errors[0]["cost_source"], "warehouse_valuation")
        self.assertEqual(errors[0]["cost_label"], "Warehouse Valuation")
        self.assertIn("Shop Floor - JBS", errors[0]["message"])

    def test_highest_authoritative_cost_floor_wins_over_lower_buying_price(self):
        self._install_buying_price(rate=1500, uom="Nos", currency="UGX")
        self.frappe.db.get_value = lambda doctype, *args, **kwargs: (
            "UGX" if doctype in {"Price List", "Company"} else None
        )
        base_get_all = self.frappe.get_all

        def get_all(doctype, *args, **kwargs):
            if doctype == "Item":
                return [
                    {
                        "item_code": "LOW",
                        "stock_uom": "Nos",
                        "is_stock_item": 1,
                        "last_purchase_rate": 3200,
                        "valuation_rate": 0,
                    }
                ]
            if doctype == "Bin":
                return [
                    {
                        "item_code": "LOW",
                        "warehouse": "Shop Floor - JBS",
                        "valuation_rate": 2800,
                    }
                ]
            return base_get_all(doctype, *args, **kwargs)

        self.frappe.get_all = get_all

        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "LOW",
                    "item_name": "Below Cost",
                    "qty": 1,
                    "rate": 2500,
                    "uom": "Nos",
                    "warehouse": "Shop Floor - JBS",
                }
            ],
            invoice_doc={
                "company": "Rewards Essential Ltd",
                "currency": "UGX",
                "conversion_rate": 1,
            },
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["buying_rate"], 3200)
        self.assertEqual(errors[0]["cost_source"], "last_purchase_rate")
        self.assertEqual(errors[0]["cost_label"], "Last Purchase Rate")

    def test_warehouse_valuation_converts_uom_and_invoice_currency(self):
        self.frappe.db.get_value = lambda doctype, *args, **kwargs: (
            "PKR" if doctype == "Company" else None
        )
        erpnext_module = types.ModuleType("erpnext")
        erpnext_setup = types.ModuleType("erpnext.setup")
        erpnext_utils = types.ModuleType("erpnext.setup.utils")
        erpnext_utils.get_exchange_rate = lambda *args, **kwargs: 250
        sys.modules["erpnext"] = erpnext_module
        sys.modules["erpnext.setup"] = erpnext_setup
        sys.modules["erpnext.setup.utils"] = erpnext_utils

        def get_all(doctype, *args, **kwargs):
            if doctype == "Item":
                return [
                    {
                        "item_code": "BOXED",
                        "stock_uom": "Nos",
                        "is_stock_item": 1,
                        "last_purchase_rate": 0,
                        "valuation_rate": 0,
                    }
                ]
            if doctype == "UOM Conversion Detail":
                return [{"parent": "BOXED", "uom": "Box", "conversion_factor": 10}]
            if doctype == "Bin":
                return [
                    {
                        "item_code": "BOXED",
                        "warehouse": "Main Store",
                        "valuation_rate": 1000,
                    }
                ]
            return []

        self.frappe.get_all = get_all

        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "BOXED",
                    "item_name": "Boxed Item",
                    "qty": 1,
                    "rate": 35,
                    "uom": "Box",
                    "warehouse": "Main Store",
                }
            ],
            invoice_doc={
                "company": "Test Co",
                "currency": "USD",
                "conversion_rate": 250,
                "posting_date": "2026-11-26",
            },
        )

        self.assertEqual(len(errors), 1)
        self.assertAlmostEqual(errors[0]["buying_rate"], 40.0)
        self.assertEqual(errors[0]["cost_source"], "warehouse_valuation")

    def test_non_stock_item_ignores_bin_valuation_rate(self):
        def get_all(doctype, *args, **kwargs):
            if doctype == "Item":
                return [
                    {
                        "item_code": "SVC-1",
                        "stock_uom": "Nos",
                        "is_stock_item": 0,
                        "last_purchase_rate": 0,
                        "valuation_rate": 0,
                    }
                ]
            if doctype == "Bin":
                return [
                    {
                        "item_code": "SVC-1",
                        "warehouse": "Shop Floor - JBS",
                        "valuation_rate": 5000,
                    }
                ]
            return []

        self.frappe.get_all = get_all

        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "SVC-1",
                    "item_name": "Service Fee",
                    "qty": 1,
                    "rate": 1000,
                    "uom": "Nos",
                    "warehouse": "Shop Floor - JBS",
                }
            ]
        )

        self.assertEqual(errors, [])

    PROMO_INVOICE = {"company": "Test Co", "posting_date": "2026-11-26"}

    def _install_promotional_rule(self, **overrides):
        """Install an active selling Product Pricing Rule that gives LOW free."""

        rule = {
            "name": "PR-FREE",
            "disable": 0,
            "selling": 1,
            "price_or_product_discount": "Product",
            "apply_on": "Item Code",
            "same_item": 0,
            "free_item": "LOW",
            "company": "Test Co",
            "valid_from": "2026-01-01",
            "valid_upto": None,
        }
        rule.update(overrides)
        self._install_buying_price(rate=100)
        base_get_all = self.frappe.get_all

        def get_all(doctype, *args, **kwargs):
            if doctype == "Pricing Rule":
                # Like frappe.get_all, return only the fields the query asks for.
                fields = kwargs.get("fields") or list(rule)
                return [{field: rule.get(field) for field in fields}]
            return base_get_all(doctype, *args, **kwargs)

        self.frappe.get_all = get_all
        return rule

    @staticmethod
    def _promo_trigger_row(pricing_rules='["PR-FREE"]', item_code="TRIG", rate=500):
        return {
            "item_code": item_code,
            "item_name": "Promo Trigger",
            "qty": 5,
            "rate": rate,
            "uom": "Nos",
            "pricing_rules": pricing_rules,
        }

    @staticmethod
    def _promo_free_row(pricing_rules="PR-FREE", rate=0, item_code="LOW"):
        return {
            "item_code": item_code,
            "item_name": "Promo Free Item",
            "qty": 1,
            "rate": rate,
            "uom": "Nos",
            "is_free_item": 1,
            "pricing_rules": pricing_rules,
        }

    def test_free_promotional_item_is_excluded_from_below_cost_floor(self):
        self._install_promotional_rule()

        errors = self.controls.collect_below_buying_price_errors(
            [self._promo_trigger_row(), self._promo_free_row()],
            invoice_doc=dict(self.PROMO_INVOICE),
        )

        self.assertEqual(errors, [])

    def test_same_item_promotional_free_row_is_excluded_from_below_cost_floor(self):
        self._install_promotional_rule(name="PR-BOGO", same_item=1, free_item=None)

        errors = self.controls.collect_below_buying_price_errors(
            [
                self._promo_trigger_row('["PR-BOGO"]', item_code="LOW", rate=150),
                self._promo_free_row("PR-BOGO"),
            ],
            invoice_doc=dict(self.PROMO_INVOICE),
        )

        self.assertEqual(errors, [])

    def test_transaction_promotional_free_row_is_excluded_from_below_cost_floor(self):
        # ERPNext removes free rows of unmatched Transaction rules during validate
        # (remove_free_item), so no paid row carries a transaction-level rule.
        self._install_promotional_rule(apply_on="Transaction")

        errors = self.controls.collect_below_buying_price_errors(
            [self._promo_trigger_row(pricing_rules=""), self._promo_free_row()],
            invoice_doc=dict(self.PROMO_INVOICE),
        )

        self.assertEqual(errors, [])

    def test_comma_separated_paid_row_rules_authorize_free_row(self):
        # ERPNext's get_applied_pricing_rules reads JSON lists and comma lists.
        self._install_promotional_rule()

        errors = self.controls.collect_below_buying_price_errors(
            [self._promo_trigger_row("PR-OTHER,PR-FREE"), self._promo_free_row()],
            invoice_doc=dict(self.PROMO_INVOICE),
        )

        self.assertEqual(errors, [])

    def test_forged_free_item_without_pricing_rule_is_blocked_below_cost(self):
        self._install_buying_price(rate=100)

        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "LOW",
                    "item_name": "Forged Free Item",
                    "qty": 1,
                    "rate": 1,
                    "uom": "Nos",
                    "is_free_item": 1,
                }
            ],
            invoice_doc=dict(self.PROMO_INVOICE),
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["reason"], "below_buying_price")
        self.assertEqual(errors[0]["buying_rate"], 100)

    def test_forged_free_item_naming_invalid_pricing_rule_is_blocked_below_cost(self):
        scenarios = {
            "rule does not exist": ({}, "PR-GHOST", '["PR-GHOST"]'),
            "rule disabled": ({"disable": 1}, "PR-FREE", '["PR-FREE"]'),
            "price discount rule": (
                {"price_or_product_discount": "Price"},
                "PR-FREE",
                '["PR-FREE"]',
            ),
            "buying-only rule": ({"selling": 0}, "PR-FREE", '["PR-FREE"]'),
            "rule gives a different item": (
                {"free_item": "OTHER"},
                "PR-FREE",
                '["PR-FREE"]',
            ),
            "rule expired": ({"valid_upto": "2026-06-30"}, "PR-FREE", '["PR-FREE"]'),
            "rule not yet valid": (
                {"valid_from": "2026-12-01"},
                "PR-FREE",
                '["PR-FREE"]',
            ),
            "rule for another company": (
                {"company": "Other Co"},
                "PR-FREE",
                '["PR-FREE"]',
            ),
            "same-item rule for an item that did not trigger it": (
                {"same_item": 1, "free_item": None},
                "PR-FREE",
                '["PR-FREE"]',
            ),
            "rule not applied to any paid row": ({}, "PR-FREE", ""),
            "paid row pricing_rules is malformed": ({}, "PR-FREE", '["PR-FREE"'),
            "transaction rule gives a different item": (
                {"apply_on": "Transaction", "free_item": "OTHER"},
                "PR-FREE",
                "",
            ),
            "paid row comma list pads the rule name": ({}, "PR-FREE", "PR-OTHER, PR-FREE"),
            "paid row JSON list pads the rule name": ({}, "PR-FREE", '[" PR-FREE"]'),
        }
        for label, (overrides, free_rule, trigger_rules) in scenarios.items():
            with self.subTest(label):
                self._install_promotional_rule(**overrides)

                errors = self.controls.collect_below_buying_price_errors(
                    [
                        self._promo_trigger_row(trigger_rules),
                        self._promo_free_row(free_rule, rate=1),
                    ],
                    invoice_doc=dict(self.PROMO_INVOICE),
                )

                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0]["item_code"], "LOW")
                self.assertEqual(errors[0]["reason"], "below_buying_price")
                self.assertEqual(errors[0]["buying_rate"], 100)

    def test_free_item_is_not_exempt_when_invoice_ignores_pricing_rules(self):
        # ERPNext neither applies free items nor rewrites paid-row pricing_rules
        # when ignore_pricing_rule is set, so any free row is client-made.
        self._install_promotional_rule()

        errors = self.controls.collect_below_buying_price_errors(
            [self._promo_trigger_row(), self._promo_free_row(rate=1)],
            invoice_doc={**self.PROMO_INVOICE, "ignore_pricing_rule": 1},
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["reason"], "below_buying_price")

    def test_only_one_free_row_per_rule_and_item_is_exempt(self):
        # ERPNext keys free rows by (item_code, pricing_rules) and re-sizes only
        # the first; a duplicate row is not a server-applied quantity.
        self._install_promotional_rule()

        errors = self.controls.collect_below_buying_price_errors(
            [
                self._promo_trigger_row(),
                self._promo_free_row(),
                self._promo_free_row(rate=1),
            ],
            invoice_doc=dict(self.PROMO_INVOICE),
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["reason"], "below_buying_price")
        self.assertEqual(errors[0]["selling_rate"], 1)

    def test_unknown_uom_client_conversion_factor_cannot_lower_floor(self):
        self._install_buying_price(rate=100, uom="Nos")
        base_get_all = self.frappe.get_all

        def get_all(doctype, *args, **kwargs):
            if doctype == "Item":
                return [
                    {
                        "item_code": "LOW",
                        "stock_uom": "Nos",
                        "is_stock_item": 1,
                        "last_purchase_rate": 100,
                        "valuation_rate": 0,
                    }
                ]
            if doctype == "Bin":
                return [
                    {
                        "item_code": "LOW",
                        "warehouse": "Shop Floor - JBS",
                        "valuation_rate": 100,
                    }
                ]
            return base_get_all(doctype, *args, **kwargs)

        self.frappe.get_all = get_all

        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "LOW",
                    "item_name": "Crated Item",
                    "qty": 1,
                    "rate": 1,
                    "uom": "Crate",
                    "conversion_factor": 0.01,
                    "warehouse": "Shop Floor - JBS",
                }
            ],
            policy={
                "enabled": True,
                "action": "Block",
                "minimum_margin_percentage": 0,
                "missing_cost_action": "Block",
            },
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["reason"], "invalid_uom")

    def test_known_uom_ignores_client_conversion_factor(self):
        self._install_buying_price(rate=12.75, uom="Nos")
        base_get_all = self.frappe.get_all

        def get_all(doctype, *args, **kwargs):
            if doctype == "UOM Conversion Detail":
                return [{"parent": "LOW", "uom": "Box", "conversion_factor": 10}]
            return base_get_all(doctype, *args, **kwargs)

        self.frappe.get_all = get_all

        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "LOW",
                    "item_name": "Below Cost",
                    "qty": 1,
                    "rate": 100,
                    "uom": "Box",
                    "conversion_factor": 0.01,
                }
            ]
        )

        self.assertEqual(errors[0]["buying_rate"], 127.5)

    def test_variant_uses_template_uom_conversion_not_client_factor(self):
        self._install_buying_price(rate=10, uom="Nos")
        base_get_all = self.frappe.get_all

        def get_all(doctype, *args, **kwargs):
            if doctype == "Item":
                return [
                    {
                        "item_code": "LOW",
                        "stock_uom": "Nos",
                        "variant_of": "LOW-TEMPLATE",
                        "is_stock_item": 1,
                        "last_purchase_rate": 0,
                        "valuation_rate": 0,
                    }
                ]
            if doctype == "UOM Conversion Detail":
                return [
                    {
                        "parent": "LOW-TEMPLATE",
                        "uom": "Box",
                        "conversion_factor": 12,
                    }
                ]
            return base_get_all(doctype, *args, **kwargs)

        self.frappe.get_all = get_all

        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "LOW",
                    "item_name": "Variant Item",
                    "qty": 1,
                    "rate": 50,
                    "uom": "Box",
                    "conversion_factor": 0.01,
                }
            ]
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["reason"], "below_buying_price")
        self.assertEqual(errors[0]["buying_rate"], 120)

    def test_in_process_consolidated_sales_invoice_skips_pos_sale_controls(self):
        validator = Mock()
        invoice = types.SimpleNamespace(
            doctype="Sales Invoice",
            flags=types.SimpleNamespace(is_consolidated=True),
            get=lambda key, default=None: {
                "doctype": "Sales Invoice",
                "is_pos": 1,
                "is_consolidated": 1,
                "pos_profile": "REWARD ESSENTIALS POS",
                "items": [],
            }.get(key, default),
        )

        with patch.object(
            self.controls,
            "validate_invoice_item_sale_controls",
            validator,
        ):
            self.controls.validate_pos_invoice_item_sale_controls(invoice)

        validator.assert_not_called()

    def test_forged_consolidated_flag_absent_still_runs_pos_sale_controls(self):
        validator = Mock()
        invoice = types.SimpleNamespace(
            doctype="Sales Invoice",
            flags=types.SimpleNamespace(is_consolidated=False),
            get=lambda key, default=None: {
                "doctype": "Sales Invoice",
                "is_pos": 1,
                "is_consolidated": 1,
                "pos_profile": "REWARD ESSENTIALS POS",
                "items": [],
            }.get(key, default),
        )

        with patch.object(
            self.controls,
            "validate_invoice_item_sale_controls",
            validator,
        ):
            self.controls.validate_pos_invoice_item_sale_controls(invoice)

        validator.assert_called_once_with(invoice)

    def test_override_details_records_cost_source(self):
        invoice = {
            "items": [{"item_code": "QB-524", "qty": 1, "rate": 2000}],
            "pos_profile": "REWARD ESSENTIALS POS",
            "posa_below_cost_override": 1,
            "posa_below_cost_override_reason": "Approved clearance",
        }
        floor_error = {
            "item_code": "QB-524",
            "selling_rate": 2000,
            "buying_rate": 4500,
            "minimum_selling_rate": 4500,
            "minimum_margin_percentage": 0,
            "buying_price_uom": "Nos",
            "buying_price_currency": "UGX",
            "cost_source": "warehouse_valuation",
            "warehouse": "Shop Floor - JBS",
            "message": "below floor",
        }

        with (
            patch.object(self.controls, "collect_item_sale_control_errors", return_value=[]),
            patch.object(
                self.controls,
                "collect_below_buying_price_errors",
                return_value=[floor_error],
            ),
            patch.object(
                self.controls,
                "resolve_sale_floor_policy",
                return_value={"enabled": True, "action": "POS Supervisor Override"},
            ),
            patch.object(
                self.controls,
                "_authorize_below_cost_override",
                return_value="supervisor@example.com",
            ),
        ):
            self.controls.validate_invoice_item_sale_controls(invoice)

        self.assertIn(
            '"cost_source":"warehouse_valuation"',
            invoice["posa_below_cost_override_details"],
        )

    def test_whitespace_padded_free_row_rule_name_is_not_exempt(self):
        # ERPNext matches free rows on the raw pricing_rules string
        # (remove_free_item, apply_pricing_rule_for_free_items) and Frappe does not
        # strip Small Text, so a padded name escapes both and must not match here.
        paddings = (" PR-FREE", "PR-FREE ", "\tPR-FREE", "\u00a0PR-FREE")
        for apply_on, trigger_rules in (("Item Code", '["PR-FREE"]'), ("Transaction", "")):
            for padded in paddings:
                with self.subTest(apply_on=apply_on, pricing_rules=repr(padded)):
                    self._install_promotional_rule(apply_on=apply_on)

                    errors = self.controls.collect_below_buying_price_errors(
                        [
                            self._promo_trigger_row(trigger_rules),
                            self._promo_free_row(padded, rate=1),
                        ],
                        invoice_doc=dict(self.PROMO_INVOICE),
                    )

                    self.assertEqual(len(errors), 1)
                    self.assertEqual(errors[0]["item_code"], "LOW")
                    self.assertEqual(errors[0]["reason"], "below_buying_price")

    def test_forged_free_row_cannot_meet_transaction_rule_thresholds_alone(self):
        # ERPNext counts free rows in total_qty/total before it matches Transaction
        # rules, so a forged free row could qualify the rule by itself. The trigger
        # row is the only paid row: qty 5, amount 2500. The free row sells at 1.
        scenarios = {
            "paid qty below min_qty": ({"min_qty": 10}, 1),
            "paid amount below min_amt": ({"min_amt": 5000}, 1),
            "paid qty above max_qty": ({"max_qty": 3}, 1),
            "paid amount above max_amt": ({"max_amt": 1000}, 1),
            "free row qty would meet min_qty": ({"min_qty": 10}, 5),
            "free row amount would meet min_amt": ({"min_amt": 5000}, 2500),
        }
        for label, (thresholds, free_qty) in scenarios.items():
            with self.subTest(label):
                self._install_promotional_rule(apply_on="Transaction", **thresholds)
                free_row = self._promo_free_row(rate=1)
                free_row["qty"] = free_qty

                errors = self.controls.collect_below_buying_price_errors(
                    [self._promo_trigger_row(""), free_row],
                    invoice_doc=dict(self.PROMO_INVOICE),
                )

                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0]["item_code"], "LOW")
                self.assertEqual(errors[0]["reason"], "below_buying_price")

    def test_transaction_rule_thresholds_met_by_paid_rows_exempt_free_row(self):
        self._install_promotional_rule(
            apply_on="Transaction",
            min_qty=5,
            max_qty=10,
            min_amt=2500,
            max_amt=10000,
        )

        errors = self.controls.collect_below_buying_price_errors(
            [self._promo_trigger_row(""), self._promo_free_row()],
            invoice_doc=dict(self.PROMO_INVOICE),
        )

        self.assertEqual(errors, [])

    def test_unknown_uom_is_refused_even_when_missing_cost_is_allowed(self):
        # ERPNext moves qty x the row's own conversion_factor in stock, so a UOM
        # with no master factor cannot be priced against any floor.
        self._install_buying_price(rate=100, uom="Nos")

        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "LOW",
                    "item_name": "Crated Item",
                    "qty": 1,
                    "rate": 1,
                    "uom": "Crate",
                    "conversion_factor": 1000,
                }
            ]
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["reason"], "invalid_uom")
        self.assertEqual(errors[0]["policy"], "block")
        self.assertEqual(self.global_uom_calls, [("Crate", "Nos")])

    def test_invalid_uom_refusal_cannot_be_downgraded_or_overridden(self):
        invalid_uom_error = {
            "item_code": "LOW",
            "policy": "block",
            "reason": "invalid_uom",
            "message": "invalid uom",
        }
        for action in ("Warning Only", "POS Supervisor Override"):
            with self.subTest(action):
                invoice = {
                    "items": [{"item_code": "LOW", "qty": 1, "rate": 1, "uom": "Crate"}],
                    "pos_profile": "Main POS",
                    "posa_below_cost_override": 1,
                    "posa_below_cost_override_reason": "Approved clearance",
                }
                authorize = Mock(return_value="supervisor@example.com")

                with (
                    patch.object(
                        self.controls, "collect_item_sale_control_errors", return_value=[]
                    ),
                    patch.object(
                        self.controls,
                        "collect_below_buying_price_errors",
                        return_value=[dict(invalid_uom_error)],
                    ),
                    patch.object(
                        self.controls,
                        "resolve_sale_floor_policy",
                        return_value={"enabled": True, "action": action},
                    ),
                    patch.object(self.controls, "_authorize_below_cost_override", authorize),
                ):
                    with self.assertRaisesRegex(Exception, "invalid uom"):
                        self.controls.validate_invoice_item_sale_controls(invoice)

                authorize.assert_not_called()

    def test_inflated_client_conversion_factor_raises_floor(self):
        # Box is 10 Nos on the Item, but ERPNext would move 1000 Nos for this row.
        self._install_buying_price(rate=100, uom="Nos")
        base_get_all = self.frappe.get_all

        def get_all(doctype, *args, **kwargs):
            if doctype == "UOM Conversion Detail":
                return [{"parent": "LOW", "uom": "Box", "conversion_factor": 10}]
            return base_get_all(doctype, *args, **kwargs)

        self.frappe.get_all = get_all

        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "LOW",
                    "item_name": "Boxed Item",
                    "qty": 1,
                    "rate": 1000,
                    "uom": "Box",
                    "conversion_factor": 1000,
                }
            ]
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["reason"], "below_buying_price")
        self.assertEqual(errors[0]["buying_rate"], 100000)

    def test_inflated_client_conversion_factor_scales_exact_uom_price(self):
        self._install_buying_price(rate=1000, uom="Box")
        base_get_all = self.frappe.get_all

        def get_all(doctype, *args, **kwargs):
            if doctype == "UOM Conversion Detail":
                return [{"parent": "LOW", "uom": "Box", "conversion_factor": 10}]
            return base_get_all(doctype, *args, **kwargs)

        self.frappe.get_all = get_all

        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "LOW",
                    "item_name": "Boxed Item",
                    "qty": 1,
                    "rate": 1000,
                    "uom": "Box",
                    "conversion_factor": 1000,
                }
            ]
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["reason"], "below_buying_price")
        self.assertEqual(errors[0]["buying_rate"], 100000)

    def test_stock_uom_row_ignores_client_conversion_factor(self):
        # StockController.reset_conversion_factor sets a stock-UOM row back to 1.0.
        self._install_buying_price(rate=100, uom="Nos")

        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "LOW",
                    "item_name": "Loose Item",
                    "qty": 1,
                    "rate": 100,
                    "uom": "Nos",
                    "conversion_factor": 5,
                }
            ]
        )

        self.assertEqual(errors, [])

    def test_global_uom_conversion_factor_resolves_unlisted_uom(self):
        # Same fallback as ERPNext's get_conversion_factor: the Item and template
        # have no row for this UOM, so ERPNext's get_uom_conv_factor decides.
        scenarios = {
            "kilogram sold from gram stock": ("Gram", "Kg", 1000, 0.1, 50, 100),
            "gram sold from kilogram stock": ("Kg", "Gram", 0.001, 1000, 0.5, 1),
        }
        for label, (stock_uom, uom, factor, stock_cost, rate, floor) in scenarios.items():
            with self.subTest(label):
                self._install_buying_price(rate=stock_cost, uom=stock_uom)
                base_get_all = self.frappe.get_all

                def get_all(doctype, *args, _stock_uom=stock_uom, _base=base_get_all, **kwargs):
                    if doctype == "Item":
                        return [
                            {
                                "item_code": "LOW",
                                "stock_uom": _stock_uom,
                                "is_stock_item": 1,
                                "last_purchase_rate": 0,
                                "valuation_rate": 0,
                            }
                        ]
                    return _base(doctype, *args, **kwargs)

                self.frappe.get_all = get_all
                self.global_uom_factors = {(uom, stock_uom): factor}
                self.global_uom_calls = []

                errors = self.controls.collect_below_buying_price_errors(
                    [
                        {
                            "item_code": "LOW",
                            "item_name": "Weighed Item",
                            "qty": 1,
                            "rate": rate,
                            "uom": uom,
                            "conversion_factor": factor,
                        }
                    ]
                )

                self.assertEqual(self.global_uom_calls, [(uom, stock_uom)])
                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0]["reason"], "below_buying_price")
                self.assertAlmostEqual(errors[0]["buying_rate"], floor)

    def test_conditional_pricing_rule_never_exempts_a_free_row(self):
        # ERPNext evaluates a rule's condition with safe_eval over doc.as_dict(),
        # forged free rows and the totals they inflate included, and treats any
        # non-empty string as a condition.
        for apply_on, trigger_rules in (("Item Code", '["PR-FREE"]'), ("Transaction", "")):
            for condition in ("total_qty >= 10", "   "):
                with self.subTest(apply_on=apply_on, condition=repr(condition)):
                    self._install_promotional_rule(apply_on=apply_on, condition=condition)

                    errors = self.controls.collect_below_buying_price_errors(
                        [
                            self._promo_trigger_row(trigger_rules),
                            self._promo_free_row(rate=1),
                        ],
                        invoice_doc=dict(self.PROMO_INVOICE),
                    )

                    self.assertEqual(len(errors), 1)
                    self.assertEqual(errors[0]["item_code"], "LOW")
                    self.assertEqual(errors[0]["reason"], "below_buying_price")

    def test_transaction_amount_thresholds_use_erpnext_currency_rounding(self):
        # ERPNext compares doc.total, the rounded sum of rounded line amounts
        # (flt(rate * qty, precision)); raw floats drift (33.30 * 3 == 99.8999...).
        scenarios = {
            "one paid row 33.30 x 3 meets min_amt 99.90": (
                {"min_amt": 99.90},
                [(33.30, 3)],
            ),
            "three paid rows of 33.30 meet min_amt 99.90": (
                {"min_amt": 99.90},
                [(33.30, 1), (33.30, 1), (33.30, 1)],
            ),
            "0.10 + 0.20 stays within max_amt 0.30": (
                {"max_amt": 0.30},
                [(0.10, 1), (0.20, 1)],
            ),
            "each 0.335 line rounds to 0.34 before summing to min_amt 0.68": (
                {"min_amt": 0.68},
                [(0.335, 1), (0.335, 1)],
            ),
        }
        for label, (thresholds, lines) in scenarios.items():
            with self.subTest(label):
                self._install_promotional_rule(apply_on="Transaction", **thresholds)
                paid_rows = [
                    PrecisionDoc(item_code=f"PAID-{index}", qty=qty, rate=rate, uom="Nos")
                    for index, (rate, qty) in enumerate(lines)
                ]

                errors = self.controls.collect_below_buying_price_errors(
                    [*paid_rows, self._promo_free_row()],
                    invoice_doc=PrecisionDoc(self.PROMO_INVOICE),
                )

                self.assertEqual(errors, [])

    def test_transaction_amount_threshold_accepts_frappe_dict_rows(self):
        # frappe._dict answers every missing attribute with None, so a row's
        # "precision" is not callable there.
        self._install_promotional_rule(apply_on="Transaction", min_amt=2500)

        errors = self.controls.collect_below_buying_price_errors(
            [AttrDict(self._promo_trigger_row("")), AttrDict(self._promo_free_row())],
            invoice_doc=AttrDict(self.PROMO_INVOICE),
        )

        self.assertEqual(errors, [])

    def test_free_item_exceeding_non_recursive_free_qty_is_not_exempt(self):
        self._install_promotional_rule(free_qty=1, is_recursive=0)
        free_row = self._promo_free_row(rate=0)
        free_row["qty"] = 5

        errors = self.controls.collect_below_buying_price_errors(
            [self._promo_trigger_row(), free_row],
            invoice_doc=dict(self.PROMO_INVOICE),
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["item_code"], "LOW")
        self.assertEqual(errors[0]["reason"], "below_buying_price")

    def test_free_item_with_wrong_free_item_uom_is_not_exempt(self):
        self._install_promotional_rule(free_item_uom="Nos")
        base_get_all = self.frappe.get_all

        def get_all(doctype, *args, **kwargs):
            if doctype == "UOM Conversion Detail":
                return [{"parent": "LOW", "uom": "Box", "conversion_factor": 10}]
            return base_get_all(doctype, *args, **kwargs)

        self.frappe.get_all = get_all
        free_row = self._promo_free_row(rate=0)
        free_row["uom"] = "Box"

        errors = self.controls.collect_below_buying_price_errors(
            [self._promo_trigger_row(), free_row],
            invoice_doc=dict(self.PROMO_INVOICE),
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["item_code"], "LOW")
        self.assertEqual(errors[0]["reason"], "below_buying_price")
        self.assertEqual(errors[0]["buying_rate"], 1000)

    def test_free_item_with_rule_currency_or_party_mismatch_is_not_exempt(self):
        scenarios = {
            "currency mismatch": (
                {"currency": "USD"},
                {**self.PROMO_INVOICE, "currency": "PKR"},
            ),
            "customer mismatch": (
                {"applicable_for": "Customer", "customer": "VIP-CUST"},
                {**self.PROMO_INVOICE, "customer": "WALK-IN"},
            ),
            "customer group mismatch": (
                {"applicable_for": "Customer Group", "customer_group": "Wholesale"},
                {**self.PROMO_INVOICE, "customer_group": "Retail"},
            ),
            "territory mismatch": (
                {"applicable_for": "Territory", "territory": "North"},
                {**self.PROMO_INVOICE, "territory": "South"},
            ),
        }
        for label, (rule_overrides, invoice_doc) in scenarios.items():
            with self.subTest(label):
                self._install_promotional_rule(**rule_overrides)

                errors = self.controls.collect_below_buying_price_errors(
                    [self._promo_trigger_row(), self._promo_free_row(rate=0)],
                    invoice_doc=dict(invoice_doc),
                )

                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0]["item_code"], "LOW")
                self.assertEqual(errors[0]["reason"], "below_buying_price")

    def test_free_item_matching_rule_qty_uom_currency_and_party_stays_exempt(self):
        self._install_promotional_rule(
            free_qty=2,
            is_recursive=0,
            free_item_uom="Nos",
            currency="PKR",
            applicable_for="Customer",
            customer="VIP-CUST",
        )
        free_row = self._promo_free_row(rate=0)
        free_row["qty"] = 2

        errors = self.controls.collect_below_buying_price_errors(
            [self._promo_trigger_row(), free_row],
            invoice_doc={**self.PROMO_INVOICE, "currency": "PKR", "customer": "VIP-CUST"},
        )

        self.assertEqual(errors, [])

    def test_stock_qty_exceeding_uom_factor_raises_floor_to_actual_stock_moved(self):
        scenarios = {
            "non-stock UOM stock_qty exceeds conversion_factor": (
                "Box",
                10,
                10,
                20,
                1500,
                2000,
                False,
            ),
            "stock UOM stock_qty exceeds qty": (
                "Nos",
                None,
                1,
                5,
                300,
                500,
                False,
            ),
            "exact UOM buying price scaled when stock_qty exceeds master factor": (
                "Box",
                10,
                10,
                20,
                1500,
                2000,
                True,
            ),
        }
        for label, (
            uom,
            master_factor,
            row_factor,
            stock_qty,
            rate,
            expected_floor,
            use_exact_price,
        ) in scenarios.items():
            with self.subTest(label):
                if use_exact_price:
                    self._install_buying_price(rate=1000, uom="Box", currency="UGX")
                    base_get_all = self.frappe.get_all

                    def get_all(doctype, *args, _base=base_get_all, _mf=master_factor, **kwargs):
                        if doctype == "UOM Conversion Detail" and _mf:
                            return [{"parent": "LOW", "uom": "Box", "conversion_factor": _mf}]
                        return _base(doctype, *args, **kwargs)

                    self.frappe.get_all = get_all
                else:
                    self.frappe.db.get_value = lambda doctype, *args, **kwargs: (
                        "UGX" if doctype == "Company" else None
                    )

                    def get_all(doctype, *args, _mf=master_factor, **kwargs):
                        if doctype == "Item":
                            return [
                                {
                                    "item_code": "LOW",
                                    "stock_uom": "Nos",
                                    "is_stock_item": 1,
                                    "last_purchase_rate": 0,
                                    "valuation_rate": 0,
                                }
                            ]
                        if doctype == "UOM Conversion Detail" and _mf:
                            return [{"parent": "LOW", "uom": "Box", "conversion_factor": _mf}]
                        if doctype == "Bin":
                            return [
                                {
                                    "item_code": "LOW",
                                    "warehouse": "Shop Floor - JBS",
                                    "valuation_rate": 100,
                                }
                            ]
                        return []

                    self.frappe.get_all = get_all

                errors = self.controls.collect_below_buying_price_errors(
                    [
                        {
                            "item_code": "LOW",
                            "item_name": "Stock Qty Item",
                            "qty": 1,
                            "stock_qty": stock_qty,
                            "rate": rate,
                            "uom": uom,
                            "conversion_factor": row_factor,
                            "warehouse": "Shop Floor - JBS",
                        }
                    ],
                    invoice_doc={
                        "company": "Test Co",
                        "currency": "UGX",
                        "conversion_rate": 1,
                        "posting_date": "2026-11-28",
                    },
                )

                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0]["reason"], "below_buying_price")
                self.assertAlmostEqual(errors[0]["buying_rate"], expected_floor)

    def test_variant_template_and_inflated_conversion_factor_scale_warehouse_valuation(self):
        self.frappe.db.get_value = lambda doctype, *args, **kwargs: (
            "UGX" if doctype == "Company" else None
        )

        def get_all(doctype, *args, **kwargs):
            if doctype == "Item":
                return [
                    {
                        "item_code": "VAR-1",
                        "stock_uom": "Nos",
                        "variant_of": "TPL-1",
                        "is_stock_item": 1,
                        "last_purchase_rate": 0,
                        "valuation_rate": 0,
                    }
                ]
            if doctype == "UOM Conversion Detail":
                return [{"parent": "TPL-1", "uom": "Case", "conversion_factor": 12}]
            if doctype == "Bin":
                return [
                    {
                        "item_code": "VAR-1",
                        "warehouse": "Shop Floor - JBS",
                        "valuation_rate": 100,
                    }
                ]
            return []

        self.frappe.get_all = get_all

        errors = self.controls.collect_below_buying_price_errors(
            [
                {
                    "item_code": "VAR-1",
                    "item_name": "Variant Case",
                    "qty": 1,
                    "rate": 1200,
                    "uom": "Case",
                    "conversion_factor": 15,
                    "warehouse": "Shop Floor - JBS",
                }
            ],
            invoice_doc={
                "company": "Test Co",
                "currency": "UGX",
                "conversion_rate": 1,
            },
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["buying_rate"], 1500)

    def _install_bin_valuation(self, valuation_rate=100, company_currency="UGX"):
        self.frappe.db.get_value = lambda doctype, *args, **kwargs: (
            company_currency if doctype == "Company" else None
        )

        def get_all(doctype, *args, **kwargs):
            if doctype == "Item":
                return [
                    {
                        "item_code": "LOW",
                        "stock_uom": "Nos",
                        "is_stock_item": 1,
                        "last_purchase_rate": 0,
                        "valuation_rate": 0,
                    }
                ]
            if doctype == "Bin":
                return [
                    {
                        "item_code": "LOW",
                        "warehouse": "Shop Floor - JBS",
                        "valuation_rate": valuation_rate,
                    }
                ]
            return []

        self.frappe.get_all = get_all

    def test_tax_inclusive_checkout_blocks_when_net_revenue_falls_below_valuation_floor(self):
        self._install_bin_valuation(valuation_rate=100, company_currency="UGX")

        errors = self.controls.collect_below_buying_price_errors(
            [
                PrecisionDoc(
                    item_code="LOW",
                    item_name="Tax Inclusive Item",
                    qty=1,
                    rate=110,
                    uom="Nos",
                    warehouse="Shop Floor - JBS",
                    precision_value=2,
                )
            ],
            invoice_doc=PrecisionDoc(
                company="Test Co",
                currency="UGX",
                conversion_rate=1,
                taxes=[
                    {
                        "charge_type": "On Net Total",
                        "account_head": "VAT 18% - CO",
                        "rate": 18,
                        "included_in_print_rate": 1,
                    }
                ],
                precision_value=2,
            ),
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["reason"], "below_buying_price")
        self.assertAlmostEqual(errors[0]["selling_rate"], 93.22, places=2)
        self.assertAlmostEqual(errors[0]["base_selling_rate"], 93.22, places=2)

    def test_tax_inclusive_checkout_uses_erpnext_computed_net_rate_and_base_net_rate(self):
        self._install_bin_valuation(valuation_rate=100, company_currency="UGX")

        errors = self.controls.collect_below_buying_price_errors(
            [
                PrecisionDoc(
                    item_code="LOW",
                    item_name="Tax Inclusive Item",
                    qty=1,
                    rate=110,
                    net_rate=93.22,
                    base_net_rate=93.22,
                    uom="Nos",
                    warehouse="Shop Floor - JBS",
                    precision_value=2,
                )
            ],
            invoice_doc=PrecisionDoc(
                company="Test Co",
                currency="UGX",
                conversion_rate=1,
                precision_value=2,
            ),
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["reason"], "below_buying_price")
        self.assertAlmostEqual(errors[0]["selling_rate"], 93.22, places=2)

    def test_tax_inclusive_checkout_defeats_stale_or_forged_gross_net_rate(self):
        self._install_bin_valuation(valuation_rate=100, company_currency="UGX")

        errors = self.controls.collect_below_buying_price_errors(
            [
                PrecisionDoc(
                    item_code="LOW",
                    item_name="Tax Inclusive Item",
                    qty=1,
                    rate=110,
                    net_rate=110,
                    base_net_rate=110,
                    uom="Nos",
                    warehouse="Shop Floor - JBS",
                    precision_value=2,
                )
            ],
            invoice_doc=PrecisionDoc(
                company="Test Co",
                currency="UGX",
                conversion_rate=1,
                taxes=[
                    {
                        "charge_type": "On Net Total",
                        "account_head": "VAT 18% - CO",
                        "rate": 18,
                        "included_in_print_rate": 1,
                    }
                ],
                precision_value=2,
            ),
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["reason"], "below_buying_price")
        self.assertAlmostEqual(errors[0]["selling_rate"], 93.22, places=2)

    def test_tax_inclusive_multicurrency_compares_base_net_rate_against_company_valuation(self):
        self._install_bin_valuation(valuation_rate=350000, company_currency="UGX")

        errors = self.controls.collect_below_buying_price_errors(
            [
                PrecisionDoc(
                    item_code="LOW",
                    item_name="USD Inclusive Item",
                    qty=1,
                    rate=100,
                    uom="Nos",
                    warehouse="Shop Floor - JBS",
                    precision_value=2,
                )
            ],
            invoice_doc=PrecisionDoc(
                company="Test Co",
                currency="USD",
                conversion_rate=3700,
                taxes=[
                    {
                        "charge_type": "On Net Total",
                        "account_head": "VAT 18% - CO",
                        "rate": 18,
                        "included_in_print_rate": 1,
                    }
                ],
                precision_value=2,
            ),
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["reason"], "below_buying_price")
        self.assertAlmostEqual(errors[0]["base_selling_rate"], 313559.32, places=2)
        self.assertAlmostEqual(errors[0]["base_buying_rate"], 350000.0, places=2)

    def test_tax_inclusive_net_total_discount_and_item_tax_override(self):
        self._install_bin_valuation(valuation_rate=95, company_currency="UGX")
        inclusive_tax = [
            {
                "charge_type": "On Net Total",
                "account_head": "VAT 18% - CO",
                "rate": 18,
                "included_in_print_rate": 1,
            }
        ]

        with self.subTest("fixed discount on Net Total pushes net below cost"):
            errors = self.controls.collect_below_buying_price_errors(
                [
                    PrecisionDoc(
                        item_code="LOW",
                        item_name="Discounted Net",
                        qty=1,
                        rate=118,
                        uom="Nos",
                        warehouse="Shop Floor - JBS",
                        precision_value=2,
                    )
                ],
                invoice_doc=PrecisionDoc(
                    company="Test Co",
                    currency="UGX",
                    conversion_rate=1,
                    apply_discount_on="Net Total",
                    discount_amount=10,
                    taxes=inclusive_tax,
                    precision_value=2,
                ),
            )
            self.assertEqual(len(errors), 1)
            self.assertAlmostEqual(errors[0]["selling_rate"], 90.0, places=2)

        with self.subTest("zero-rated item_tax_rate override clears floor"):
            errors = self.controls.collect_below_buying_price_errors(
                [
                    PrecisionDoc(
                        item_code="LOW",
                        item_name="Zero Rated",
                        qty=1,
                        rate=95,
                        uom="Nos",
                        warehouse="Shop Floor - JBS",
                        item_tax_rate='{"VAT 18% - CO": 0}',
                        precision_value=2,
                    )
                ],
                invoice_doc=PrecisionDoc(
                    company="Test Co",
                    currency="UGX",
                    conversion_rate=1,
                    taxes=inclusive_tax,
                    precision_value=2,
                ),
            )
            self.assertEqual(errors, [])

        with self.subTest("higher item_tax_rate override blocks below-cost net rate"):
            errors = self.controls.collect_below_buying_price_errors(
                [
                    PrecisionDoc(
                        item_code="LOW",
                        item_name="High Tax",
                        qty=1,
                        rate=115,
                        uom="Nos",
                        warehouse="Shop Floor - JBS",
                        item_tax_rate='{"VAT 18% - CO": 25}',
                        precision_value=2,
                    )
                ],
                invoice_doc=PrecisionDoc(
                    company="Test Co",
                    currency="UGX",
                    conversion_rate=1,
                    taxes=inclusive_tax,
                    precision_value=2,
                ),
            )
            self.assertEqual(len(errors), 1)
            self.assertAlmostEqual(errors[0]["selling_rate"], 92.0, places=2)

    def test_tax_inclusive_checkout_allows_sale_when_net_revenue_meets_floor(self):
        self._install_bin_valuation(valuation_rate=100, company_currency="UGX")

        errors = self.controls.collect_below_buying_price_errors(
            [
                PrecisionDoc(
                    item_code="LOW",
                    item_name="Tax Inclusive OK",
                    qty=1,
                    rate=118,
                    net_rate=100,
                    base_net_rate=100,
                    uom="Nos",
                    warehouse="Shop Floor - JBS",
                    precision_value=2,
                )
            ],
            invoice_doc=PrecisionDoc(
                company="Test Co",
                currency="UGX",
                conversion_rate=1,
                taxes=[
                    {
                        "charge_type": "On Net Total",
                        "account_head": "VAT 18% - CO",
                        "rate": 18,
                        "included_in_print_rate": 1,
                    }
                ],
                precision_value=2,
            ),
        )

        self.assertEqual(errors, [])

    def test_tax_exclusive_checkout_does_not_deduct_exclusive_tax(self):
        self._install_bin_valuation(valuation_rate=100, company_currency="UGX")

        errors = self.controls.collect_below_buying_price_errors(
            [
                PrecisionDoc(
                    item_code="LOW",
                    item_name="Tax Exclusive OK",
                    qty=1,
                    rate=100,
                    net_rate=100,
                    base_net_rate=100,
                    uom="Nos",
                    warehouse="Shop Floor - JBS",
                    precision_value=2,
                )
            ],
            invoice_doc=PrecisionDoc(
                company="Test Co",
                currency="UGX",
                conversion_rate=1,
                taxes=[
                    {
                        "charge_type": "On Net Total",
                        "account_head": "VAT 18% - CO",
                        "rate": 18,
                        "included_in_print_rate": 0,
                    }
                ],
                precision_value=2,
            ),
        )

        self.assertEqual(errors, [])

    def test_return_invoice_and_in_process_consolidation_exemptions_preserved_under_tax_inclusive(
        self,
    ):
        self._install_bin_valuation(valuation_rate=100, company_currency="UGX")

        return_errors = self.controls.collect_below_buying_price_errors(
            [
                PrecisionDoc(
                    item_code="LOW",
                    qty=-1,
                    rate=50,
                    uom="Nos",
                    warehouse="Shop Floor - JBS",
                    precision_value=2,
                )
            ],
            is_return=True,
            invoice_doc=PrecisionDoc(
                company="Test Co",
                currency="UGX",
                conversion_rate=1,
                taxes=[{"charge_type": "On Net Total", "rate": 18, "included_in_print_rate": 1}],
                precision_value=2,
            ),
        )
        self.assertEqual(return_errors, [])

        validator = Mock()
        consolidated = types.SimpleNamespace(
            doctype="Sales Invoice",
            flags=types.SimpleNamespace(is_consolidated=True),
            get=lambda key, default=None: {
                "doctype": "Sales Invoice",
                "is_pos": 1,
                "is_consolidated": 1,
                "pos_profile": "REWARD ESSENTIALS POS",
                "items": [{"item_code": "LOW", "qty": 1, "rate": 110}],
                "taxes": [{"charge_type": "On Net Total", "rate": 18, "included_in_print_rate": 1}],
            }.get(key, default),
        )
        with patch.object(self.controls, "validate_invoice_item_sale_controls", validator):
            self.controls.validate_pos_invoice_item_sale_controls(consolidated)
        validator.assert_not_called()

    def test_high_currency_precision_blocks_sub_epsilon_below_cost_shortfall(self):
        self._install_bin_valuation(valuation_rate=100.0, company_currency="KWD")

        errors = self.controls.collect_below_buying_price_errors(
            [
                PrecisionDoc(
                    item_code="LOW",
                    item_name="Four Decimal Item",
                    qty=1,
                    rate=99.9999,
                    net_rate=99.9999,
                    base_net_rate=99.9999,
                    uom="Nos",
                    warehouse="Shop Floor - JBS",
                    precision_value=4,
                )
            ],
            invoice_doc=PrecisionDoc(
                company="Test Co",
                currency="KWD",
                conversion_rate=1,
                precision_value=4,
            ),
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["reason"], "below_buying_price")

    def test_two_decimal_currency_precision_allows_valuation_rounding_to_selling_rate(self):
        self._install_bin_valuation(valuation_rate=100.004, company_currency="USD")

        errors = self.controls.collect_below_buying_price_errors(
            [
                PrecisionDoc(
                    item_code="LOW",
                    item_name="Two Decimal Item",
                    qty=1,
                    rate=100.00,
                    net_rate=100.00,
                    base_net_rate=100.00,
                    uom="Nos",
                    warehouse="Shop Floor - JBS",
                    precision_value=2,
                )
            ],
            invoice_doc=PrecisionDoc(
                company="Test Co",
                currency="USD",
                conversion_rate=1,
                precision_value=2,
            ),
        )

        self.assertEqual(errors, [])

    def test_zero_decimal_currency_precision_matches_erpnext_rounding(self):
        with self.subTest("4500.4 rounds to 4500 at 0 precision and passes"):
            self._install_bin_valuation(valuation_rate=4500.4, company_currency="UGX")
            errors = self.controls.collect_below_buying_price_errors(
                [
                    PrecisionDoc(
                        item_code="LOW",
                        item_name="UGX Item",
                        qty=1,
                        rate=4500,
                        net_rate=4500,
                        base_net_rate=4500,
                        uom="Nos",
                        warehouse="Shop Floor - JBS",
                        precision_value=0,
                    )
                ],
                invoice_doc=PrecisionDoc(
                    company="Test Co",
                    currency="UGX",
                    conversion_rate=1,
                    precision_value=0,
                ),
            )
            self.assertEqual(errors, [])

        with self.subTest("4500.6 rounds to 4501 at 0 precision and blocks"):
            self._install_bin_valuation(valuation_rate=4500.6, company_currency="UGX")
            errors = self.controls.collect_below_buying_price_errors(
                [
                    PrecisionDoc(
                        item_code="LOW",
                        item_name="UGX Item",
                        qty=1,
                        rate=4500,
                        net_rate=4500,
                        base_net_rate=4500,
                        uom="Nos",
                        warehouse="Shop Floor - JBS",
                        precision_value=0,
                    )
                ],
                invoice_doc=PrecisionDoc(
                    company="Test Co",
                    currency="UGX",
                    conversion_rate=1,
                    precision_value=0,
                ),
            )
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["reason"], "below_buying_price")

    def test_parent_doc_precision_used_when_item_row_is_plain_dict(self):
        with self.subTest("parent precision 4 blocks 99.9999 vs 100.0000"):
            self._install_bin_valuation(valuation_rate=100.0, company_currency="KWD")
            errors = self.controls.collect_below_buying_price_errors(
                [
                    {
                        "item_code": "LOW",
                        "item_name": "Plain Row",
                        "qty": 1,
                        "rate": 99.9999,
                        "uom": "Nos",
                        "warehouse": "Shop Floor - JBS",
                    }
                ],
                invoice_doc=PrecisionDoc(
                    company="Test Co",
                    currency="KWD",
                    conversion_rate=1,
                    precision_value=4,
                ),
            )
            self.assertEqual(len(errors), 1)

        with self.subTest("parent precision 2 allows 100.00 vs 100.004"):
            self._install_bin_valuation(valuation_rate=100.004, company_currency="USD")
            errors = self.controls.collect_below_buying_price_errors(
                [
                    {
                        "item_code": "LOW",
                        "item_name": "Plain Row",
                        "qty": 1,
                        "rate": 100.00,
                        "uom": "Nos",
                        "warehouse": "Shop Floor - JBS",
                    }
                ],
                invoice_doc=PrecisionDoc(
                    company="Test Co",
                    currency="USD",
                    conversion_rate=1,
                    precision_value=2,
                ),
            )
            self.assertEqual(errors, [])

    def test_chained_inclusive_quantity_tax_zero_net_revenue_is_rejected(self):
        self._install_bin_valuation(valuation_rate=0.50, company_currency="USD")

        with self.subTest("On Item Quantity 10 + 10% On Previous Row Amount at gross 11 yields 0 net"):
            errors = self.controls.collect_below_buying_price_errors(
                [
                    PrecisionDoc(
                        item_code="LOW",
                        item_name="Excise Chained Tax Item",
                        qty=1,
                        rate=11.0,
                        amount=11.0,
                        uom="Nos",
                        warehouse="Shop Floor - JBS",
                        precision_value=2,
                    )
                ],
                invoice_doc=PrecisionDoc(
                    company="Test Co",
                    currency="USD",
                    conversion_rate=1,
                    taxes=[
                        {
                            "charge_type": "On Item Quantity",
                            "account_head": "Excise - CO",
                            "rate": 10.0,
                            "included_in_print_rate": 1,
                        },
                        {
                            "charge_type": "On Previous Row Amount",
                            "account_head": "VAT on Excise - CO",
                            "row_id": "1",
                            "rate": 10.0,
                            "included_in_print_rate": 1,
                        },
                    ],
                    precision_value=2,
                ),
            )
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["reason"], "below_buying_price")
            self.assertAlmostEqual(errors[0]["selling_rate"], 0.0, places=2)
            self.assertAlmostEqual(errors[0]["base_selling_rate"], 0.0, places=2)

        with self.subTest("On Item Quantity 10 + 10% On Previous Row Total at gross 11 yields 0 net"):
            errors = self.controls.collect_below_buying_price_errors(
                [
                    PrecisionDoc(
                        item_code="LOW",
                        item_name="Excise Total Tax Item",
                        qty=1,
                        rate=11.0,
                        amount=11.0,
                        uom="Nos",
                        warehouse="Shop Floor - JBS",
                        precision_value=2,
                    )
                ],
                invoice_doc=PrecisionDoc(
                    company="Test Co",
                    currency="USD",
                    conversion_rate=1,
                    taxes=[
                        {
                            "charge_type": "On Item Quantity",
                            "account_head": "Excise - CO",
                            "rate": 10.0,
                            "included_in_print_rate": 1,
                        },
                        {
                            "charge_type": "On Previous Row Total",
                            "account_head": "VAT on Total - CO",
                            "row_id": "1",
                            "rate": 10.0,
                            "included_in_print_rate": 1,
                        },
                    ],
                    precision_value=2,
                ),
            )
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["reason"], "below_buying_price")
            self.assertAlmostEqual(errors[0]["selling_rate"], 0.0, places=2)

        with self.subTest("freshly calculated zero base_net_rate is never discarded when gross > 0"):
            errors = self.controls.collect_below_buying_price_errors(
                [
                    PrecisionDoc(
                        item_code="LOW",
                        item_name="Zero Base Net Rate Item",
                        qty=1,
                        rate=11.0,
                        amount=11.0,
                        net_rate=0.0,
                        base_net_rate=0.0,
                        uom="Nos",
                        warehouse="Shop Floor - JBS",
                        precision_value=2,
                    )
                ],
                invoice_doc=PrecisionDoc(
                    company="Test Co",
                    currency="USD",
                    conversion_rate=1,
                    precision_value=2,
                ),
            )
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["reason"], "below_buying_price")
            self.assertAlmostEqual(errors[0]["selling_rate"], 0.0, places=2)
            self.assertAlmostEqual(errors[0]["base_selling_rate"], 0.0, places=2)

        with self.subTest("chained On Item Quantity + On Previous Row Total clears floor at gross 22"):
            self._install_bin_valuation(valuation_rate=10.0, company_currency="USD")
            errors = self.controls.collect_below_buying_price_errors(
                [
                    PrecisionDoc(
                        item_code="LOW",
                        item_name="Excise Total OK",
                        qty=1,
                        rate=22.0,
                        amount=22.0,
                        net_rate=10.0,
                        base_net_rate=10.0,
                        uom="Nos",
                        warehouse="Shop Floor - JBS",
                        precision_value=2,
                    )
                ],
                invoice_doc=PrecisionDoc(
                    company="Test Co",
                    currency="USD",
                    conversion_rate=1,
                    taxes=[
                        {
                            "charge_type": "On Item Quantity",
                            "account_head": "Excise - CO",
                            "rate": 10.0,
                            "included_in_print_rate": 1,
                        },
                        {
                            "charge_type": "On Previous Row Total",
                            "account_head": "VAT on Total - CO",
                            "row_id": "1",
                            "rate": 10.0,
                            "included_in_print_rate": 1,
                        },
                    ],
                    precision_value=2,
                ),
            )
            self.assertEqual(errors, [])

    def test_fixed_grand_total_discount_distributes_across_exclusive_and_inclusive_tax(self):
        exclusive_tax = [
            {
                "charge_type": "On Net Total",
                "account_head": "VAT 10% - CO",
                "rate": 10.0,
                "included_in_print_rate": 0,
            }
        ]
        inclusive_tax = [
            {
                "charge_type": "On Net Total",
                "account_head": "VAT 18% - CO",
                "rate": 18.0,
                "included_in_print_rate": 1,
            }
        ]

        with self.subTest("exclusive tax with ERPNext distributed net_rate 90 passes 89.50 floor"):
            self._install_bin_valuation(valuation_rate=89.50, company_currency="USD")
            errors = self.controls.collect_below_buying_price_errors(
                [
                    PrecisionDoc(
                        item_code="LOW",
                        item_name="Exclusive Tax Discount",
                        qty=1,
                        rate=100.0,
                        amount=100.0,
                        net_rate=90.0,
                        base_net_rate=90.0,
                        uom="Nos",
                        warehouse="Shop Floor - JBS",
                        precision_value=2,
                    )
                ],
                invoice_doc=PrecisionDoc(
                    company="Test Co",
                    currency="USD",
                    conversion_rate=1,
                    apply_discount_on="Grand Total",
                    discount_amount=11.0,
                    taxes=exclusive_tax,
                    precision_value=2,
                ),
            )
            self.assertEqual(errors, [])

        with self.subTest("exclusive tax fallback math distributes 11 over 110 grand total to 90 net"):
            self._install_bin_valuation(valuation_rate=89.50, company_currency="USD")
            errors = self.controls.collect_below_buying_price_errors(
                [
                    PrecisionDoc(
                        item_code="LOW",
                        item_name="Exclusive Tax Fallback",
                        qty=1,
                        rate=100.0,
                        amount=100.0,
                        uom="Nos",
                        warehouse="Shop Floor - JBS",
                        precision_value=2,
                    )
                ],
                invoice_doc=PrecisionDoc(
                    company="Test Co",
                    currency="USD",
                    conversion_rate=1,
                    apply_discount_on="Grand Total",
                    discount_amount=11.0,
                    taxes=exclusive_tax,
                    precision_value=2,
                ),
            )
            self.assertEqual(errors, [])

        with self.subTest("exclusive tax with 90 net fails 90.50 floor"):
            self._install_bin_valuation(valuation_rate=90.50, company_currency="USD")
            errors = self.controls.collect_below_buying_price_errors(
                [
                    PrecisionDoc(
                        item_code="LOW",
                        item_name="Exclusive Tax Below Floor",
                        qty=1,
                        rate=100.0,
                        amount=100.0,
                        net_rate=90.0,
                        base_net_rate=90.0,
                        uom="Nos",
                        warehouse="Shop Floor - JBS",
                        precision_value=2,
                    )
                ],
                invoice_doc=PrecisionDoc(
                    company="Test Co",
                    currency="USD",
                    conversion_rate=1,
                    apply_discount_on="Grand Total",
                    discount_amount=11.0,
                    taxes=exclusive_tax,
                    precision_value=2,
                ),
            )
            self.assertEqual(len(errors), 1)
            self.assertAlmostEqual(errors[0]["selling_rate"], 90.0, places=2)

        with self.subTest("inclusive tax fixed Grand Total discount distributes proportionally"):
            self._install_bin_valuation(valuation_rate=89.50, company_currency="USD")
            ok_errors = self.controls.collect_below_buying_price_errors(
                [
                    PrecisionDoc(
                        item_code="LOW",
                        item_name="Inclusive Tax Discount OK",
                        qty=1,
                        rate=118.0,
                        amount=118.0,
                        net_rate=90.0,
                        base_net_rate=90.0,
                        uom="Nos",
                        warehouse="Shop Floor - JBS",
                        precision_value=2,
                    )
                ],
                invoice_doc=PrecisionDoc(
                    company="Test Co",
                    currency="USD",
                    conversion_rate=1,
                    apply_discount_on="Grand Total",
                    discount_amount=11.80,
                    taxes=inclusive_tax,
                    precision_value=2,
                ),
            )
            self.assertEqual(ok_errors, [])

            self._install_bin_valuation(valuation_rate=90.50, company_currency="USD")
            blocked_errors = self.controls.collect_below_buying_price_errors(
                [
                    PrecisionDoc(
                        item_code="LOW",
                        item_name="Inclusive Tax Discount Low",
                        qty=1,
                        rate=118.0,
                        amount=118.0,
                        uom="Nos",
                        warehouse="Shop Floor - JBS",
                        precision_value=2,
                    )
                ],
                invoice_doc=PrecisionDoc(
                    company="Test Co",
                    currency="USD",
                    conversion_rate=1,
                    apply_discount_on="Grand Total",
                    discount_amount=11.80,
                    taxes=inclusive_tax,
                    precision_value=2,
                ),
            )
            self.assertEqual(len(blocked_errors), 1)
            self.assertAlmostEqual(blocked_errors[0]["selling_rate"], 90.0, places=2)

    def test_validate_pos_invoice_refreshes_stale_foreign_conversion_rate_and_blocks_below_cost(
        self,
    ):
        self._install_bin_valuation(valuation_rate=100000, company_currency="UGX")
        erpnext_module = types.ModuleType("erpnext")
        erpnext_setup = types.ModuleType("erpnext.setup")
        erpnext_utils = types.ModuleType("erpnext.setup.utils")
        erpnext_utils.get_exchange_rate = lambda from_curr, to_curr, *_args, **_kwargs: (
            3700.0 if (from_curr, to_curr) == ("USD", "UGX") else 1.0
        )

        recalc_calls = []

        class DeskInvoiceDoc(PrecisionDoc):
            def calculate_taxes_and_totals(self):
                recalc_calls.append(self["conversion_rate"])
                for item in self["items"]:
                    item["net_rate"] = item["rate"]
                    item["base_net_rate"] = round(item["net_rate"] * self["conversion_rate"], 2)

        item_row = PrecisionDoc(
            item_code="LOW",
            item_name="Desk USD Item",
            qty=1,
            rate=10.0,
            amount=10.0,
            net_rate=10.0,
            base_net_rate=10000000.0,
            uom="Nos",
            warehouse="Shop Floor - JBS",
            precision_value=2,
        )
        invoice = DeskInvoiceDoc(
            doctype="POS Invoice",
            is_pos=1,
            pos_profile="Main POS",
            company="Test Co",
            currency="USD",
            conversion_rate=1000000.0,
            posting_date="2026-11-28",
            items=[item_row],
            precision_value=2,
        )
        invoice.doctype = "POS Invoice"
        invoice.flags = types.SimpleNamespace()

        with patch.dict(
            sys.modules,
            {
                "erpnext": erpnext_module,
                "erpnext.setup": erpnext_setup,
                "erpnext.setup.utils": erpnext_utils,
            },
        ):
            with self.assertRaisesRegex(Exception, "below Warehouse Valuation"):
                self.controls.validate_pos_invoice_item_sale_controls(invoice)

        self.assertEqual(invoice["conversion_rate"], 3700.0)
        self.assertEqual(recalc_calls, [3700.0])
        self.assertAlmostEqual(item_row["base_net_rate"], 37000.0, places=2)

    def test_validate_pos_invoice_preserves_return_and_consolidated_exemptions_with_foreign_currency(
        self,
    ):
        self._install_bin_valuation(valuation_rate=100000, company_currency="UGX")

        return_doc = PrecisionDoc(
            doctype="POS Invoice",
            is_pos=1,
            is_return=1,
            pos_profile="Main POS",
            company="Test Co",
            currency="USD",
            conversion_rate=3500.0,
            posting_date="2026-11-28",
            items=[
                PrecisionDoc(
                    item_code="LOW",
                    qty=-1,
                    rate=10.0,
                    net_rate=10.0,
                    base_net_rate=35000.0,
                    uom="Nos",
                    warehouse="Shop Floor - JBS",
                    precision_value=2,
                )
            ],
            precision_value=2,
        )
        return_doc.doctype = "POS Invoice"
        return_doc.flags = types.SimpleNamespace()
        self.controls.validate_pos_invoice_item_sale_controls(return_doc)
        self.assertEqual(return_doc["conversion_rate"], 3500.0)

        consolidated_doc = PrecisionDoc(
            doctype="Sales Invoice",
            is_pos=1,
            is_consolidated=1,
            pos_profile="Main POS",
            company="Test Co",
            currency="USD",
            conversion_rate=3500.0,
            posting_date="2026-11-28",
            items=[
                PrecisionDoc(
                    item_code="LOW",
                    qty=1,
                    rate=10.0,
                    uom="Nos",
                    warehouse="Shop Floor - JBS",
                    precision_value=2,
                )
            ],
            precision_value=2,
        )
        consolidated_doc.doctype = "Sales Invoice"
        consolidated_doc.flags = types.SimpleNamespace(is_consolidated=True)
        self.controls.validate_pos_invoice_item_sale_controls(consolidated_doc)
        self.assertEqual(consolidated_doc["conversion_rate"], 3500.0)

    def test_free_item_matches_utm_campaign_and_hierarchical_customer_group_and_territory(self):
        tree_nodes = {
            "Customer Group": [
                {"name": "All Customer Groups", "lft": 1, "rgt": 20},
                {"name": "Retail", "lft": 2, "rgt": 11},
                {"name": "Supermarkets", "lft": 3, "rgt": 4},
                {"name": "Wholesale", "lft": 12, "rgt": 15},
            ],
            "Territory": [
                {"name": "All Territories", "lft": 1, "rgt": 20},
                {"name": "East Africa", "lft": 2, "rgt": 11},
                {"name": "Kampala", "lft": 5, "rgt": 6},
                {"name": "West Africa", "lft": 12, "rgt": 15},
            ],
        }

        def install_rule_with_trees(**overrides):
            self._install_promotional_rule(**overrides)
            base_get_all = self.frappe.get_all

            def get_all(doctype, *args, **kwargs):
                if doctype in tree_nodes:
                    return list(tree_nodes[doctype])
                return base_get_all(doctype, *args, **kwargs)

            self.frappe.get_all = get_all

        with self.subTest("matching utm_campaign qualifies"):
            install_rule_with_trees(applicable_for="Campaign", campaign="SUMMER-2026")
            errors = self.controls.collect_below_buying_price_errors(
                [self._promo_trigger_row(), self._promo_free_row(rate=0)],
                invoice_doc={**self.PROMO_INVOICE, "utm_campaign": "SUMMER-2026"},
            )
            self.assertEqual(errors, [])

        with self.subTest("non-matching utm_campaign is rejected"):
            install_rule_with_trees(applicable_for="Campaign", campaign="SUMMER-2026")
            errors = self.controls.collect_below_buying_price_errors(
                [self._promo_trigger_row(), self._promo_free_row(rate=0)],
                invoice_doc={**self.PROMO_INVOICE, "utm_campaign": "WINTER-2026"},
            )
            self.assertEqual(len(errors), 1)

        with self.subTest("child Customer Group qualifies under parent rule"):
            install_rule_with_trees(applicable_for="Customer Group", customer_group="Retail")
            errors = self.controls.collect_below_buying_price_errors(
                [self._promo_trigger_row(), self._promo_free_row(rate=0)],
                invoice_doc={**self.PROMO_INVOICE, "customer_group": "Supermarkets"},
            )
            self.assertEqual(errors, [])

        with self.subTest("sibling Customer Group does not qualify"):
            install_rule_with_trees(applicable_for="Customer Group", customer_group="Retail")
            errors = self.controls.collect_below_buying_price_errors(
                [self._promo_trigger_row(), self._promo_free_row(rate=0)],
                invoice_doc={**self.PROMO_INVOICE, "customer_group": "Wholesale"},
            )
            self.assertEqual(len(errors), 1)

        with self.subTest("child Territory qualifies under parent rule"):
            install_rule_with_trees(applicable_for="Territory", territory="East Africa")
            errors = self.controls.collect_below_buying_price_errors(
                [self._promo_trigger_row(), self._promo_free_row(rate=0)],
                invoice_doc={**self.PROMO_INVOICE, "territory": "Kampala"},
            )
            self.assertEqual(errors, [])

        with self.subTest("sibling Territory does not qualify"):
            install_rule_with_trees(applicable_for="Territory", territory="East Africa")
            errors = self.controls.collect_below_buying_price_errors(
                [self._promo_trigger_row(), self._promo_free_row(rate=0)],
                invoice_doc={**self.PROMO_INVOICE, "territory": "West Africa"},
            )
            self.assertEqual(len(errors), 1)

    def test_free_item_enforces_default_free_qty_default_stock_uom_and_recursive_cap(self):
        with self.subTest("blank free_qty caps non-recursive award at 1"):
            self._install_promotional_rule(free_qty=None, is_recursive=0)
            free_row = self._promo_free_row(rate=0)
            free_row["qty"] = 2

            errors = self.controls.collect_below_buying_price_errors(
                [self._promo_trigger_row(), free_row],
                invoice_doc=dict(self.PROMO_INVOICE),
            )
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["reason"], "below_buying_price")

        with self.subTest("blank free_item_uom requires free item stock_uom"):
            self._install_promotional_rule(free_item_uom=None, free_qty=1, is_recursive=0)
            base_get_all = self.frappe.get_all

            def get_all(doctype, *args, **kwargs):
                if doctype == "UOM Conversion Detail":
                    return [{"parent": "LOW", "uom": "Box", "conversion_factor": 10}]
                return base_get_all(doctype, *args, **kwargs)

            self.frappe.get_all = get_all
            free_row = self._promo_free_row(rate=0)
            free_row["uom"] = "Box"

            errors = self.controls.collect_below_buying_price_errors(
                [self._promo_trigger_row(), free_row],
                invoice_doc=dict(self.PROMO_INVOICE),
            )
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["buying_rate"], 1000)

        with self.subTest("recursive rule allows exact server-computed multiple of qualifying qty"):
            self._install_promotional_rule(is_recursive=1, recurse_for=2, free_qty=1)
            trigger = self._promo_trigger_row()
            trigger["qty"] = 5
            free_row = self._promo_free_row(rate=0)
            free_row["qty"] = 2

            errors = self.controls.collect_below_buying_price_errors(
                [trigger, free_row],
                invoice_doc=dict(self.PROMO_INVOICE),
            )
            self.assertEqual(errors, [])

        with self.subTest("recursive rule blocks free qty exceeding server-computed multiple"):
            self._install_promotional_rule(is_recursive=1, recurse_for=2, free_qty=1)
            trigger = self._promo_trigger_row()
            trigger["qty"] = 5
            free_row = self._promo_free_row(rate=0)
            free_row["qty"] = 3

            errors = self.controls.collect_below_buying_price_errors(
                [trigger, free_row],
                invoice_doc=dict(self.PROMO_INVOICE),
            )
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["reason"], "below_buying_price")

        with self.subTest("recursive rule respects apply_recursion_over threshold"):
            self._install_promotional_rule(
                is_recursive=1,
                recurse_for=2,
                apply_recursion_over=1,
                free_qty=1,
            )
            trigger = self._promo_trigger_row()
            trigger["qty"] = 4

            ok_free = self._promo_free_row(rate=0)
            ok_free["qty"] = 1
            self.assertEqual(
                self.controls.collect_below_buying_price_errors(
                    [trigger, ok_free],
                    invoice_doc=dict(self.PROMO_INVOICE),
                ),
                [],
            )

            over_free = self._promo_free_row(rate=0)
            over_free["qty"] = 2
            errors = self.controls.collect_below_buying_price_errors(
                [trigger, over_free],
                invoice_doc=dict(self.PROMO_INVOICE),
            )
            self.assertEqual(len(errors), 1)

        with self.subTest("recursive rule with blank free_qty defaults to 1 per multiple"):
            self._install_promotional_rule(is_recursive=1, recurse_for=2, free_qty=None)
            trigger = self._promo_trigger_row()
            trigger["qty"] = 4
            over_free = self._promo_free_row(rate=0)
            over_free["qty"] = 3

            errors = self.controls.collect_below_buying_price_errors(
                [trigger, over_free],
                invoice_doc=dict(self.PROMO_INVOICE),
            )
            self.assertEqual(len(errors), 1)

    def test_validate_pos_and_sales_invoice_fetches_selling_exchange_rate_ignoring_newer_buying_only_rate(
        self,
    ):
        self._install_bin_valuation(valuation_rate=4000, company_currency="UGX")

        class DeskInvoiceDoc(PrecisionDoc):
            def calculate_taxes_and_totals(self):
                for item in self["items"]:
                    item["net_rate"] = item["rate"]
                    item["base_net_rate"] = round(item["net_rate"] * self["conversion_rate"], 2)

        def build_invoice(doctype):
            row = PrecisionDoc(
                item_code="LOW",
                item_name="Desk USD Item",
                qty=1,
                rate=1.0,
                amount=1.0,
                net_rate=1.0,
                base_net_rate=4200.0,
                uom="Nos",
                warehouse="Shop Floor - JBS",
                precision_value=2,
            )
            doc = DeskInvoiceDoc(
                doctype=doctype,
                is_pos=1,
                pos_profile="Main POS",
                company="Test Co",
                currency="USD",
                conversion_rate=4200.0,
                posting_date="2026-11-28",
                items=[row],
                precision_value=2,
            )
            doc.doctype = doctype
            doc.flags = types.SimpleNamespace()
            return doc, row

        # Selling rate (3700) vs newer buying-only rate (4200, for_buying=1, for_selling=0).
        def fake_get_exchange_rate(from_curr, to_curr, transaction_date=None, args=None):
            if (from_curr, to_curr) == ("USD", "UGX"):
                return 3700.0 if args == "for_selling" else 4200.0
            return 1.0

        def fake_get_latest_rate(
            from_curr,
            to_curr,
            cache=None,
            transaction_date=None,
            purpose=None,
            allow_external=True,
            silent=False,
        ):
            if (from_curr, to_curr) == ("USD", "UGX"):
                if purpose == "for_selling":
                    return 3700.0, "2026-11-20"
                return 4200.0, "2026-11-28"
            return 1.0, transaction_date

        erpnext_module = types.ModuleType("erpnext")
        erpnext_setup = types.ModuleType("erpnext.setup")
        erpnext_utils = types.ModuleType("erpnext.setup.utils")
        erpnext_utils.get_exchange_rate = fake_get_exchange_rate

        posawesome_pkg = types.ModuleType("posawesome")
        posawesome_inner = types.ModuleType("posawesome.posawesome")
        posawesome_api = types.ModuleType("posawesome.posawesome.api")
        posawesome_inv_proc = types.ModuleType("posawesome.posawesome.api.invoice_processing")
        posawesome_utils = types.ModuleType("posawesome.posawesome.api.invoice_processing.utils")
        posawesome_utils.get_latest_rate = fake_get_latest_rate

        for doctype in ("POS Invoice", "Sales Invoice"):
            with self.subTest(f"{doctype} get_exchange_rate fallback requires for_selling"):
                invoice, row = build_invoice(doctype)
                with patch.dict(
                    sys.modules,
                    {
                        "erpnext": erpnext_module,
                        "erpnext.setup": erpnext_setup,
                        "erpnext.setup.utils": erpnext_utils,
                    },
                ):
                    with self.assertRaisesRegex(Exception, "below Warehouse Valuation"):
                        self.controls.validate_pos_invoice_item_sale_controls(invoice)
                self.assertEqual(invoice["conversion_rate"], 3700.0)
                self.assertAlmostEqual(row["base_net_rate"], 3700.0, places=2)

            with self.subTest(f"{doctype} get_latest_rate path requires purpose=for_selling"):
                invoice, row = build_invoice(doctype)
                with patch.dict(
                    sys.modules,
                    {
                        "erpnext": erpnext_module,
                        "erpnext.setup": erpnext_setup,
                        "erpnext.setup.utils": erpnext_utils,
                        "posawesome": posawesome_pkg,
                        "posawesome.posawesome": posawesome_inner,
                        "posawesome.posawesome.api": posawesome_api,
                        "posawesome.posawesome.api.invoice_processing": posawesome_inv_proc,
                        "posawesome.posawesome.api.invoice_processing.utils": posawesome_utils,
                    },
                ):
                    with self.assertRaisesRegex(Exception, "below Warehouse Valuation"):
                        self.controls.validate_pos_invoice_item_sale_controls(invoice)
                self.assertEqual(invoice["conversion_rate"], 3700.0)
                self.assertAlmostEqual(row["base_net_rate"], 3700.0, places=2)

    def test_respects_native_calculated_net_cash_discount_and_fallback_actual_charge(self):
        with self.subTest("3x 1.00 lines sharing 0.02 discount preserve native [0.99, 1.00, 0.99]"):
            self._install_bin_valuation(valuation_rate=1.00, company_currency="USD")
            base_get_all = self.frappe.get_all

            def get_all(doctype, *args, **kwargs):
                if doctype == "Item":
                    return [
                        {"item_code": code, "stock_uom": "Nos", "is_stock_item": 1}
                        for code in ("ITEM-A", "ITEM-B", "ITEM-C")
                    ]
                if doctype == "Bin":
                    return [
                        {
                            "item_code": code,
                            "warehouse": "Shop Floor - JBS",
                            "valuation_rate": 1.00,
                        }
                        for code in ("ITEM-A", "ITEM-B", "ITEM-C")
                    ]
                return base_get_all(doctype, *args, **kwargs)

            self.frappe.get_all = get_all
            items = [
                PrecisionDoc(
                    item_code="ITEM-A",
                    item_name="Line A",
                    qty=1,
                    rate=1.00,
                    amount=1.00,
                    net_rate=0.99,
                    base_net_rate=0.99,
                    uom="Nos",
                    warehouse="Shop Floor - JBS",
                    precision_value=2,
                ),
                PrecisionDoc(
                    item_code="ITEM-B",
                    item_name="Line B",
                    qty=1,
                    rate=1.00,
                    amount=1.00,
                    net_rate=1.00,
                    base_net_rate=1.00,
                    uom="Nos",
                    warehouse="Shop Floor - JBS",
                    precision_value=2,
                ),
                PrecisionDoc(
                    item_code="ITEM-C",
                    item_name="Line C",
                    qty=1,
                    rate=1.00,
                    amount=1.00,
                    net_rate=0.99,
                    base_net_rate=0.99,
                    uom="Nos",
                    warehouse="Shop Floor - JBS",
                    precision_value=2,
                ),
            ]
            errors = self.controls.collect_below_buying_price_errors(
                items,
                invoice_doc=PrecisionDoc(
                    company="Test Co",
                    currency="USD",
                    conversion_rate=1,
                    apply_discount_on="Grand Total",
                    discount_amount=0.02,
                    precision_value=2,
                ),
            )
            self.assertEqual([e["item_code"] for e in errors], ["ITEM-A", "ITEM-C"])

        with self.subTest("cash or non-trade discount leaves native net_rate 100 passing 95 floor"):
            self._install_bin_valuation(valuation_rate=95.0, company_currency="USD")
            errors = self.controls.collect_below_buying_price_errors(
                [
                    PrecisionDoc(
                        item_code="LOW",
                        item_name="Cash Discount Item",
                        qty=1,
                        rate=100.0,
                        amount=100.0,
                        net_rate=100.0,
                        base_net_rate=100.0,
                        uom="Nos",
                        warehouse="Shop Floor - JBS",
                        precision_value=2,
                    )
                ],
                invoice_doc=PrecisionDoc(
                    company="Test Co",
                    currency="USD",
                    conversion_rate=1,
                    apply_discount_on="Grand Total",
                    discount_amount=10.0,
                    is_cash_or_non_trade_discount=1,
                    precision_value=2,
                ),
            )
            self.assertEqual(errors, [])

        with self.subTest("fallback-only 100 item + 20 Actual + 10% Grand Total discount yields 88 net and rejects 89 floor"):
            self._install_bin_valuation(valuation_rate=89.0, company_currency="USD")
            errors = self.controls.collect_below_buying_price_errors(
                [
                    PrecisionDoc(
                        item_code="LOW",
                        item_name="Actual Charge Fallback Item",
                        qty=1,
                        rate=100.0,
                        amount=100.0,
                        uom="Nos",
                        warehouse="Shop Floor - JBS",
                        precision_value=2,
                    )
                ],
                invoice_doc=PrecisionDoc(
                    company="Test Co",
                    currency="USD",
                    conversion_rate=1,
                    apply_discount_on="Grand Total",
                    additional_discount_percentage=10.0,
                    taxes=[
                        {
                            "charge_type": "Actual",
                            "account_head": "Freight - CO",
                            "tax_amount": 20.0,
                            "included_in_print_rate": 0,
                        }
                    ],
                    precision_value=2,
                ),
            )
            self.assertEqual(len(errors), 1)
            self.assertAlmostEqual(errors[0]["selling_rate"], 88.0, places=2)

    def test_free_item_matches_erpnext_recursive_qty_with_round_free_qty_and_min_qty(self):
        with self.subTest("round_free_qty=0 allows 2.5 award for 5 paid units and rejects 2.6"):
            self._install_promotional_rule(
                is_recursive=1,
                recurse_for=2,
                free_qty=1,
                round_free_qty=0,
            )
            trigger = self._promo_trigger_row()
            trigger["qty"] = 5

            ok_free = self._promo_free_row(rate=0)
            ok_free["qty"] = 2.5
            self.assertEqual(
                self.controls.collect_below_buying_price_errors(
                    [trigger, ok_free],
                    invoice_doc=dict(self.PROMO_INVOICE),
                ),
                [],
            )

            over_free = self._promo_free_row(rate=0)
            over_free["qty"] = 2.6
            errors = self.controls.collect_below_buying_price_errors(
                [trigger, over_free],
                invoice_doc=dict(self.PROMO_INVOICE),
            )
            self.assertEqual(len(errors), 1)

        with self.subTest("apply_recursion_over == min_qty adds base free_qty (6 paid -> 2 allowed, 3 rejected)"):
            self._install_promotional_rule(
                is_recursive=1,
                min_qty=4,
                apply_recursion_over=4,
                recurse_for=2,
                free_qty=1,
                round_free_qty=1,
            )
            trigger = self._promo_trigger_row()
            trigger["qty"] = 6

            ok_free = self._promo_free_row(rate=0)
            ok_free["qty"] = 2
            self.assertEqual(
                self.controls.collect_below_buying_price_errors(
                    [trigger, ok_free],
                    invoice_doc=dict(self.PROMO_INVOICE),
                ),
                [],
            )

            over_free = self._promo_free_row(rate=0)
            over_free["qty"] = 3
            errors = self.controls.collect_below_buying_price_errors(
                [trigger, over_free],
                invoice_doc=dict(self.PROMO_INVOICE),
            )
            self.assertEqual(len(errors), 1)

    def test_validate_invoice_prefers_same_day_selling_only_fx_over_later_dual_purpose_via_real_utils(
        self,
    ):
        self._install_bin_valuation(valuation_rate=4000, company_currency="UGX")

        class _AttrRow(dict):
            def __getattr__(self, name):
                try:
                    return self[name]
                except KeyError as exc:
                    raise AttributeError(name) from exc

        class DeskInvoiceDoc(PrecisionDoc):
            def calculate_taxes_and_totals(self):
                for item in self["items"]:
                    item["net_rate"] = item["rate"]
                    item["base_net_rate"] = round(item["net_rate"] * self["conversion_rate"], 2)

        fx_rows = [
            _AttrRow(
                name="CX-SELL-ONLY",
                date="2026-11-27",
                from_currency="USD",
                to_currency="UGX",
                for_selling=1,
                for_buying=0,
                exchange_rate=3700.0,
                creation="2026-11-27 08:00:00",
            ),
            _AttrRow(
                name="CX-DUAL",
                date="2026-11-27",
                from_currency="USD",
                to_currency="UGX",
                for_selling=1,
                for_buying=1,
                exchange_rate=4200.0,
                creation="2026-11-27 12:00:00",
            ),
        ]

        base_get_all = self.frappe.get_all

        def fake_get_all(doctype, *args, **kwargs):
            if doctype == "Currency Exchange":
                filters = kwargs.get("filters") or {}
                order_by = kwargs.get("order_by") or ""
                limit = kwargs.get("limit")
                matched = []
                for row in fx_rows:
                    ok = True
                    if isinstance(filters, dict):
                        filter_entries = [(k, v) for k, v in filters.items()]
                    else:
                        filter_entries = [
                            (entry[0], [entry[1], entry[2]]) if len(entry) == 3 else (entry[0], entry[1])
                            for entry in filters
                        ]
                    for k, v in filter_entries:
                        if isinstance(v, list) and len(v) == 2:
                            op, target = v
                            if op == "<=":
                                if not (str(row.get(k)) <= str(target)):
                                    ok = False
                                    break
                            elif op == ">":
                                if not (str(row.get(k)) > str(target)):
                                    ok = False
                                    break
                            elif op == "between":
                                if not (str(target[0]) <= str(row.get(k)) <= str(target[1])):
                                    ok = False
                                    break
                            elif op == "=":
                                if row.get(k) != target:
                                    ok = False
                                    break
                            elif row.get(k) != target:
                                ok = False
                                break
                        elif row.get(k) != v:
                            ok = False
                            break
                    if ok:
                        matched.append(row)
                clauses = [part.strip() for part in str(order_by).split(",") if part.strip()]
                for clause in reversed(clauses):
                    tokens = clause.split()
                    field = tokens[0]
                    desc = len(tokens) > 1 and tokens[1].lower() == "desc"
                    matched.sort(key=lambda r: r.get(field), reverse=desc)
                return matched[: int(limit)] if limit is not None else matched
            return base_get_all(doctype, *args, **kwargs)

        self.frappe.get_all = fake_get_all
        self.frappe.get_cached_doc = lambda *args, **kwargs: {"allow_stale": 1}
        self.frappe.whitelist = lambda *args, **kwargs: (
            args[0] if args and callable(args[0]) else (lambda fn: fn)
        )
        self.frappe.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
        self.frappe.flags = types.SimpleNamespace(mute_messages=False)
        self.frappe.message_log = []
        frappe_utils = sys.modules["frappe.utils"]
        frappe_utils.add_days = lambda d, _days: str(d)
        frappe_utils.formatdate = lambda d: str(d)
        frappe_utils.getdate = lambda value=None: str(value or "2026-11-27")
        frappe_utils.strip_html_tags = lambda s: str(s or "")

        erpnext_pkg = types.ModuleType("erpnext")
        erpnext_setup = types.ModuleType("erpnext.setup")
        erpnext_utils = types.ModuleType("erpnext.setup.utils")
        erpnext_utils.get_exchange_rate = lambda *args, **kwargs: 0.0

        utils_path = REPO_ROOT / "posawesome" / "posawesome" / "api" / "invoice_processing" / "utils.py"
        with patch.dict(
            sys.modules,
            {
                "erpnext": erpnext_pkg,
                "erpnext.setup": erpnext_setup,
                "erpnext.setup.utils": erpnext_utils,
                "posawesome": types.ModuleType("posawesome"),
                "posawesome.posawesome": types.ModuleType("posawesome.posawesome"),
                "posawesome.posawesome.api": types.ModuleType("posawesome.posawesome.api"),
                "posawesome.posawesome.api.invoice_processing": types.ModuleType(
                    "posawesome.posawesome.api.invoice_processing"
                ),
            },
        ):
            spec = importlib.util.spec_from_file_location(
                "posawesome.posawesome.api.invoice_processing.utils", utils_path
            )
            real_utils = importlib.util.module_from_spec(spec)
            sys.modules["posawesome.posawesome.api.invoice_processing.utils"] = real_utils
            spec.loader.exec_module(real_utils)

            row = PrecisionDoc(
                item_code="LOW",
                item_name="Same Day FX Item",
                qty=1,
                rate=1.0,
                amount=1.0,
                net_rate=1.0,
                base_net_rate=4200.0,
                uom="Nos",
                warehouse="Shop Floor - JBS",
                precision_value=2,
            )
            invoice = DeskInvoiceDoc(
                doctype="POS Invoice",
                is_pos=1,
                pos_profile="Main POS",
                company="Test Co",
                currency="USD",
                conversion_rate=4200.0,
                posting_date="2026-11-27",
                items=[row],
                precision_value=2,
            )
            invoice.doctype = "POS Invoice"
            invoice.flags = types.SimpleNamespace()

            with self.assertRaisesRegex(Exception, "below Warehouse Valuation"):
                self.controls.validate_pos_invoice_item_sale_controls(invoice)
            self.assertEqual(invoice["conversion_rate"], 3700.0)
            self.assertAlmostEqual(row["base_net_rate"], 3700.0, places=2)

    def test_reconciles_native_net_amount_across_mixed_quantities_without_rejecting_middle_line(self):
        self._install_bin_valuation(valuation_rate=1.00, company_currency="USD")
        base_get_all = self.frappe.get_all

        def get_all(doctype, *args, **kwargs):
            if doctype == "Item":
                return [
                    {"item_code": code, "stock_uom": "Nos", "is_stock_item": 1}
                    for code in ("ITEM-1", "ITEM-2", "ITEM-3")
                ]
            if doctype == "Bin":
                return [
                    {
                        "item_code": code,
                        "warehouse": "Shop Floor - JBS",
                        "valuation_rate": 1.00,
                    }
                    for code in ("ITEM-1", "ITEM-2", "ITEM-3")
                ]
            return base_get_all(doctype, *args, **kwargs)

        self.frappe.get_all = get_all
        items = [
            PrecisionDoc(
                item_code="ITEM-1",
                item_name="Line 1",
                qty=1,
                rate=1.00,
                amount=1.00,
                net_rate=0.99,
                net_amount=0.99,
                base_net_rate=0.99,
                base_net_amount=0.99,
                uom="Nos",
                warehouse="Shop Floor - JBS",
                precision_value=2,
            ),
            PrecisionDoc(
                item_code="ITEM-2",
                item_name="Line 2",
                qty=1,
                rate=1.00,
                amount=1.00,
                net_rate=1.00,
                net_amount=1.00,
                base_net_rate=1.00,
                base_net_amount=1.00,
                uom="Nos",
                warehouse="Shop Floor - JBS",
                precision_value=2,
            ),
            PrecisionDoc(
                item_code="ITEM-3",
                item_name="Line 3",
                qty=5,
                rate=1.00,
                amount=5.00,
                net_rate=0.99,
                net_amount=4.97,
                base_net_rate=0.99,
                base_net_amount=4.97,
                uom="Nos",
                warehouse="Shop Floor - JBS",
                precision_value=2,
            ),
        ]
        invoice_doc = PrecisionDoc(
            company="Test Co",
            currency="USD",
            conversion_rate=1,
            apply_discount_on="Net Total",
            discount_amount=0.04,
            precision_value=2,
        )
        errors = self.controls.collect_below_buying_price_errors(
            items,
            invoice_doc=invoice_doc,
        )
        self.assertEqual([e["item_code"] for e in errors], ["ITEM-1", "ITEM-3"])

    def test_recursive_free_item_checks_min_qty_against_stock_qty_when_rule_uom_is_blank(self):
        self._install_promotional_rule(
            is_recursive=1,
            min_qty=4,
            uom="",
            recurse_for=1,
            free_qty=1,
            round_free_qty=1,
            free_item_uom="Nos",
        )
        base_get_all = self.frappe.get_all

        def get_all(doctype, *args, **kwargs):
            if doctype == "Item":
                return [
                    {"item_code": "TRIG", "stock_uom": "Nos", "is_stock_item": 1},
                    {"item_code": "LOW", "stock_uom": "Nos", "is_stock_item": 1},
                ]
            if doctype == "UOM Conversion Detail":
                return [
                    {"parent": "TRIG", "uom": "Case", "conversion_factor": 6.0},
                ]
            return base_get_all(doctype, *args, **kwargs)

        self.frappe.get_all = get_all

        with self.subTest("1 Case (stock_qty=6) qualifies for min_qty=4 with blank rule uom and awards 1 free unit"):
            trigger = self._promo_trigger_row()
            trigger["qty"] = 1
            trigger["uom"] = "Case"
            trigger["stock_uom"] = "Nos"
            trigger["conversion_factor"] = 6.0
            trigger["stock_qty"] = 6.0

            ok_free = self._promo_free_row(rate=0)
            ok_free["qty"] = 1
            self.assertEqual(
                self.controls.collect_below_buying_price_errors(
                    [trigger, ok_free],
                    invoice_doc=dict(self.PROMO_INVOICE),
                ),
                [],
            )

            over_free = self._promo_free_row(rate=0)
            over_free["qty"] = 2
            errors = self.controls.collect_below_buying_price_errors(
                [trigger, over_free],
                invoice_doc=dict(self.PROMO_INVOICE),
            )
            self.assertEqual(len(errors), 1)

        with self.subTest("forged stock_qty on 1 Nos cannot satisfy min_qty=4"):
            trigger = self._promo_trigger_row()
            trigger["qty"] = 1
            trigger["uom"] = "Nos"
            trigger["stock_uom"] = "Nos"
            trigger["stock_qty"] = 6.0

            free_row = self._promo_free_row(rate=0)
            free_row["qty"] = 1
            errors = self.controls.collect_below_buying_price_errors(
                [trigger, free_row],
                invoice_doc=dict(self.PROMO_INVOICE),
            )
            self.assertEqual(len(errors), 1)

        with self.subTest("explicit rule uom scales min_qty threshold in stock units"):
            self._install_promotional_rule(
                is_recursive=1,
                min_qty=2,
                recurse_for=1,
                free_qty=1,
                round_free_qty=1,
                free_item_uom="Nos",
            )
            rule_get_all = self.frappe.get_all

            def get_all_with_uom(doctype, *args, **kwargs):
                if doctype == "Item":
                    return [
                        {"item_code": "TRIG", "stock_uom": "Nos", "is_stock_item": 1},
                        {"item_code": "LOW", "stock_uom": "Nos", "is_stock_item": 1},
                    ]
                if doctype == "Pricing Rule Item Code":
                    return [
                        {"parent": "PR-FREE", "item_code": "TRIG", "uom": "Box"},
                    ]
                if doctype == "UOM Conversion Detail":
                    return [
                        {"parent": "TRIG", "uom": "Box", "conversion_factor": 3.0},
                        {"parent": "TRIG", "uom": "Case", "conversion_factor": 6.0},
                    ]
                return rule_get_all(doctype, *args, **kwargs)

            self.frappe.get_all = get_all_with_uom

            # 5 Nos < 2 Boxes (6 Nos) must fail even though transaction qty 5 >= min_qty 2.
            five_nos = self._promo_trigger_row()
            five_nos["qty"] = 5
            five_nos["uom"] = "Nos"
            five_nos["stock_uom"] = "Nos"
            five_nos["stock_qty"] = 5.0
            free_one = self._promo_free_row(rate=0)
            free_one["qty"] = 1
            self.assertEqual(
                len(
                    self.controls.collect_below_buying_price_errors(
                        [five_nos, free_one],
                        invoice_doc=dict(self.PROMO_INVOICE),
                    )
                ),
                1,
            )

            # 1 Case (6 Nos) == 2 Boxes (6 Nos) qualifies and awards 1 free unit (transaction qty 1).
            one_case = self._promo_trigger_row()
            one_case["qty"] = 1
            one_case["uom"] = "Case"
            one_case["stock_uom"] = "Nos"
            one_case["conversion_factor"] = 6.0
            one_case["stock_qty"] = 6.0
            self.assertEqual(
                self.controls.collect_below_buying_price_errors(
                    [one_case, free_one],
                    invoice_doc=dict(self.PROMO_INVOICE),
                ),
                [],
            )

    def test_item_tax_rate_na_exempts_item_from_inclusive_tax_template(self):
        self._install_bin_valuation(valuation_rate=95.0, company_currency="USD")
        invoice_doc = PrecisionDoc(
            company="Test Co",
            currency="USD",
            conversion_rate=1,
            taxes=[
                {
                    "charge_type": "On Net Total",
                    "account_head": "VAT - CO",
                    "rate": 20.0,
                    "included_in_print_rate": 1,
                }
            ],
            precision_value=2,
        )
        row = PrecisionDoc(
            item_code="LOW",
            item_name="Tax Exempt NA Item",
            qty=1,
            rate=100.0,
            amount=100.0,
            uom="Nos",
            warehouse="Shop Floor - JBS",
            item_tax_rate='{"VAT - CO": "N/A"}',
            precision_value=2,
        )
        errors = self.controls.collect_below_buying_price_errors(
            [row],
            invoice_doc=invoice_doc,
        )
        self.assertEqual(errors, [])

    def test_promotional_rule_query_rejects_nonexistent_parent_uom_and_reads_child_uom(self):
        # Pinned ERPNext v16.60.0 tabPricing Rule parent schema does NOT have a `uom` column;
        # uom lives on Pricing Rule Item Code / Item Group / Brand child tables.
        valid_parent_columns = {
            "name",
            "disable",
            "selling",
            "buying",
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
        }

        def install_strict_rule(child_doctype=None, child_rows=None, item_extra=None, **overrides):
            self._install_promotional_rule(**overrides)
            base_get_all = self.frappe.get_all

            def strict_get_all(doctype, *args, **kwargs):
                if doctype == "Pricing Rule":
                    fields = kwargs.get("fields") or []
                    unknown = [field for field in fields if field not in valid_parent_columns]
                    if unknown:
                        raise Exception(f"OperationalError: Unknown column '{unknown[0]}' in 'field list'")
                    return base_get_all(doctype, *args, **kwargs)
                if doctype == "Item":
                    trig = {"item_code": "TRIG", "stock_uom": "Nos", "is_stock_item": 1}
                    if item_extra:
                        trig.update(item_extra)
                    return [
                        trig,
                        {"item_code": "LOW", "stock_uom": "Nos", "is_stock_item": 1},
                    ]
                if doctype == "UOM Conversion Detail":
                    return [
                        {"parent": "TRIG", "uom": "Box", "conversion_factor": 3.0},
                        {"parent": "TRIG", "uom": "Case", "conversion_factor": 6.0},
                    ]
                if child_doctype and doctype == child_doctype:
                    return list(child_rows or [])
                return base_get_all(doctype, *args, **kwargs)

            self.frappe.get_all = strict_get_all

        with self.subTest("non-recursive promotion queries only real Pricing Rule parent columns"):
            install_strict_rule(is_recursive=0, free_qty=1)
            self.assertEqual(
                self.controls.collect_below_buying_price_errors(
                    [self._promo_trigger_row(), self._promo_free_row(rate=0)],
                    invoice_doc=dict(self.PROMO_INVOICE),
                ),
                [],
            )

        with self.subTest("Pricing Rule Item Code child uom scales min_qty by SKU conversion"):
            install_strict_rule(
                child_doctype="Pricing Rule Item Code",
                child_rows=[{"parent": "PR-FREE", "item_code": "TRIG", "uom": "Box"}],
                apply_on="Item Code",
                is_recursive=1,
                min_qty=2,
                recurse_for=1,
                free_qty=1,
                round_free_qty=1,
                free_item_uom="Nos",
            )
            five_nos = self._promo_trigger_row()
            five_nos["qty"] = 5
            five_nos["uom"] = "Nos"
            five_nos["stock_uom"] = "Nos"
            five_nos["stock_qty"] = 5.0
            free_one = self._promo_free_row(rate=0)
            free_one["qty"] = 1
            self.assertEqual(
                len(
                    self.controls.collect_below_buying_price_errors(
                        [five_nos, free_one],
                        invoice_doc=dict(self.PROMO_INVOICE),
                    )
                ),
                1,
            )

            one_case = self._promo_trigger_row()
            one_case["qty"] = 1
            one_case["uom"] = "Case"
            one_case["stock_uom"] = "Nos"
            one_case["conversion_factor"] = 6.0
            one_case["stock_qty"] = 6.0
            self.assertEqual(
                self.controls.collect_below_buying_price_errors(
                    [one_case, free_one],
                    invoice_doc=dict(self.PROMO_INVOICE),
                ),
                [],
            )

        # ERPNext pricing_rule/utils.py:423-435 + get_item_details.py:1784-1789:
        # Brand and Item Group child rows carry no item_code, so get_conversion_factor(None, uom)
        # returns 1.0 and 1 Box (stock_qty=3) >= min_qty=2 qualifies and awards 1 free unit.
        for label, apply_on, child_dt, child_row, item_extra in (
            (
                "Pricing Rule Item Group child uom uses factor 1.0 matching ERPNext",
                "Item Group",
                "Pricing Rule Item Group",
                {"parent": "PR-FREE", "item_group": "Beverages", "uom": "Box"},
                {"item_group": "Beverages"},
            ),
            (
                "Pricing Rule Brand child uom uses factor 1.0 matching ERPNext",
                "Brand",
                "Pricing Rule Brand",
                {"parent": "PR-FREE", "brand": "Acme", "uom": "Box"},
                {"brand": "Acme"},
            ),
        ):
            with self.subTest(label):
                install_strict_rule(
                    child_doctype=child_dt,
                    child_rows=[child_row],
                    item_extra=item_extra,
                    apply_on=apply_on,
                    is_recursive=1,
                    min_qty=2,
                    recurse_for=1,
                    free_qty=1,
                    round_free_qty=1,
                    free_item_uom="Nos",
                )
                one_box = self._promo_trigger_row()
                one_box["qty"] = 1
                one_box["uom"] = "Box"
                one_box["stock_uom"] = "Nos"
                one_box["conversion_factor"] = 3.0
                one_box["stock_qty"] = 3.0
                one_box.update(item_extra)
                free_one = self._promo_free_row(rate=0)
                free_one["qty"] = 1
                self.assertEqual(
                    self.controls.collect_below_buying_price_errors(
                        [one_box, free_one],
                        invoice_doc=dict(self.PROMO_INVOICE),
                    ),
                    [],
                )

                one_nos = self._promo_trigger_row()
                one_nos["qty"] = 1
                one_nos["uom"] = "Nos"
                one_nos["stock_uom"] = "Nos"
                one_nos["stock_qty"] = 1.0
                one_nos.update(item_extra)
                self.assertEqual(
                    len(
                        self.controls.collect_below_buying_price_errors(
                            [one_nos, free_one],
                            invoice_doc=dict(self.PROMO_INVOICE),
                        )
                    ),
                    1,
                )

    def test_reconciled_multi_unit_line_honors_native_rounded_net_rate_cross_currency(self):
        self._install_bin_valuation(valuation_rate=3700.0, company_currency="UGX")
        erpnext_pkg = types.ModuleType("erpnext")
        erpnext_setup = types.ModuleType("erpnext.setup")
        erpnext_utils = types.ModuleType("erpnext.setup.utils")
        erpnext_utils.get_exchange_rate = (
            lambda from_curr, to_curr, *_args, **_kwargs: 3700.0
            if (from_curr, to_curr) == ("USD", "UGX")
            else 1.0
        )

        # 3 units at USD 1.00 sharing 0.01 Net Total discount -> ERPNext sets
        # net_amount=2.99, net_rate=1.00, base_net_amount=11063.00, base_net_rate=3700.00.
        valid_row = PrecisionDoc(
            item_code="LOW",
            item_name="Three Unit Rounded Net Rate",
            qty=3,
            rate=1.00,
            amount=3.00,
            net_amount=2.99,
            net_rate=1.00,
            base_net_amount=11063.00,
            base_net_rate=3700.00,
            uom="Nos",
            warehouse="Shop Floor - JBS",
            precision_value=2,
        )
        invoice_doc = PrecisionDoc(
            company="Test Co",
            currency="USD",
            conversion_rate=3700.0,
            apply_discount_on="Net Total",
            discount_amount=0.01,
            precision_value=2,
        )
        with patch.dict(
            sys.modules,
            {
                "erpnext": erpnext_pkg,
                "erpnext.setup": erpnext_setup,
                "erpnext.setup.utils": erpnext_utils,
            },
        ):
            self.assertEqual(
                self.controls.collect_below_buying_price_errors(
                    [valid_row],
                    invoice_doc=invoice_doc,
                ),
                [],
            )

            # Forged inflated net_rate inconsistent with net_amount must still be rejected.
            forged_row = PrecisionDoc(
                item_code="LOW",
                item_name="Forged Net Rate Row",
                qty=3,
                rate=1.00,
                amount=3.00,
                net_amount=2.50,
                net_rate=1.00,
                base_net_amount=9250.00,
                base_net_rate=3700.00,
                uom="Nos",
                warehouse="Shop Floor - JBS",
                precision_value=2,
            )
            forged_invoice = PrecisionDoc(
                company="Test Co",
                currency="USD",
                conversion_rate=3700.0,
                apply_discount_on="Net Total",
                discount_amount=0.50,
                precision_value=2,
            )
            self.assertEqual(
                len(
                    self.controls.collect_below_buying_price_errors(
                        [forged_row],
                        invoice_doc=forged_invoice,
                    )
                ),
                1,
            )

    def test_non_mixed_recursive_pricing_rule_evaluates_min_max_qty_per_paid_line(self):
        with self.subTest("two 3-unit lines qualify independently under max_qty=4 and award 6 free units"):
            self._install_promotional_rule(
                apply_on="Item Code",
                mixed_conditions=0,
                is_cumulative=0,
                is_recursive=1,
                min_qty=1,
                max_qty=4,
                recurse_for=1,
                free_qty=1,
                round_free_qty=1,
            )
            line_a = self._promo_trigger_row()
            line_a["qty"] = 3
            line_b = self._promo_trigger_row()
            line_b["qty"] = 3

            free_six = self._promo_free_row(rate=0)
            free_six["qty"] = 6
            self.assertEqual(
                self.controls.collect_below_buying_price_errors(
                    [line_a, line_b, free_six],
                    invoice_doc=dict(self.PROMO_INVOICE),
                ),
                [],
            )

            free_seven = self._promo_free_row(rate=0)
            free_seven["qty"] = 7
            self.assertEqual(
                len(
                    self.controls.collect_below_buying_price_errors(
                        [line_a, line_b, free_seven],
                        invoice_doc=dict(self.PROMO_INVOICE),
                    )
                ),
                1,
            )

        with self.subTest("non-mixed rule excludes a second line exceeding max_qty=4"):
            self._install_promotional_rule(
                apply_on="Item Code",
                mixed_conditions=0,
                is_cumulative=0,
                is_recursive=1,
                min_qty=1,
                max_qty=4,
                recurse_for=1,
                free_qty=1,
                round_free_qty=1,
            )
            line_ok = self._promo_trigger_row()
            line_ok["qty"] = 3
            line_over = self._promo_trigger_row()
            line_over["qty"] = 5

            free_three = self._promo_free_row(rate=0)
            free_three["qty"] = 3
            self.assertEqual(
                self.controls.collect_below_buying_price_errors(
                    [line_ok, line_over, free_three],
                    invoice_doc=dict(self.PROMO_INVOICE),
                ),
                [],
            )

            free_four = self._promo_free_row(rate=0)
            free_four["qty"] = 4
            self.assertEqual(
                len(
                    self.controls.collect_below_buying_price_errors(
                        [line_ok, line_over, free_four],
                        invoice_doc=dict(self.PROMO_INVOICE),
                    )
                ),
                1,
            )

        with self.subTest("mixed_conditions=1 still aggregates lines and rejects 3+3=6 > max_qty=4"):
            self._install_promotional_rule(
                apply_on="Item Code",
                mixed_conditions=1,
                is_cumulative=0,
                is_recursive=1,
                min_qty=1,
                max_qty=4,
                recurse_for=1,
                free_qty=1,
                round_free_qty=1,
            )
            line_a = self._promo_trigger_row()
            line_a["qty"] = 3
            line_b = self._promo_trigger_row()
            line_b["qty"] = 3
            free_six = self._promo_free_row(rate=0)
            free_six["qty"] = 6
            self.assertEqual(
                len(
                    self.controls.collect_below_buying_price_errors(
                        [line_a, line_b, free_six],
                        invoice_doc=dict(self.PROMO_INVOICE),
                    )
                ),
                1,
            )

        with self.subTest("is_cumulative=1 with mixed_conditions=0 evaluates max_qty per line and awards 6 (not 7)"):
            self._install_promotional_rule(
                apply_on="Item Code",
                mixed_conditions=0,
                is_cumulative=1,
                is_recursive=1,
                min_qty=1,
                max_qty=4,
                recurse_for=1,
                free_qty=1,
                round_free_qty=1,
            )
            line_a = self._promo_trigger_row()
            line_a["qty"] = 3
            line_b = self._promo_trigger_row()
            line_b["qty"] = 3

            free_six = self._promo_free_row(rate=0)
            free_six["qty"] = 6
            erpnext_pkg = types.ModuleType("erpnext")
            erpnext_accounts = types.ModuleType("erpnext.accounts")
            erpnext_doctype = types.ModuleType("erpnext.accounts.doctype")
            erpnext_pr = types.ModuleType("erpnext.accounts.doctype.pricing_rule")
            erpnext_pr_utils = types.ModuleType("erpnext.accounts.doctype.pricing_rule.utils")
            erpnext_pr_utils.get_cumulative_qty_and_amount_for_row = (
                lambda _pr_doc, _args, _doc=None: (0.0, 0.0)
            )
            zero_history_modules = {
                "erpnext": erpnext_pkg,
                "erpnext.accounts": erpnext_accounts,
                "erpnext.accounts.doctype": erpnext_doctype,
                "erpnext.accounts.doctype.pricing_rule": erpnext_pr,
                "erpnext.accounts.doctype.pricing_rule.utils": erpnext_pr_utils,
            }
            with patch.dict(sys.modules, zero_history_modules):
                self.assertEqual(
                    self.controls.collect_below_buying_price_errors(
                        [line_a, line_b, free_six],
                        invoice_doc=dict(self.PROMO_INVOICE),
                    ),
                    [],
                )

                free_seven = self._promo_free_row(rate=0)
                free_seven["qty"] = 7
                self.assertEqual(
                    len(
                        self.controls.collect_below_buying_price_errors(
                            [line_a, line_b, free_seven],
                            invoice_doc=dict(self.PROMO_INVOICE),
                        )
                    ),
                    1,
                )

    def test_cumulative_recursive_pricing_rule_includes_historical_returns_in_thresholds(self):
        def _pricing_rule_utils_modules(historical_qty, historical_amt=0.0):
            erpnext_pkg = types.ModuleType("erpnext")
            erpnext_accounts = types.ModuleType("erpnext.accounts")
            erpnext_doctype = types.ModuleType("erpnext.accounts.doctype")
            erpnext_pr = types.ModuleType("erpnext.accounts.doctype.pricing_rule")
            erpnext_pr_utils = types.ModuleType("erpnext.accounts.doctype.pricing_rule.utils")
            erpnext_pr_utils.get_cumulative_qty_and_amount_for_row = (
                lambda _pr_doc, _args, _doc=None: (historical_qty, historical_amt)
            )
            return {
                "erpnext": erpnext_pkg,
                "erpnext.accounts": erpnext_accounts,
                "erpnext.accounts.doctype": erpnext_doctype,
                "erpnext.accounts.doctype.pricing_rule": erpnext_pr,
                "erpnext.accounts.doctype.pricing_rule.utils": erpnext_pr_utils,
            }

        with self.subTest("negative historical returns (-2) plus current qty 5 gives cumulative 3 <= max_qty 4 and awards 5 free units"):
            self._install_promotional_rule(
                apply_on="Item Code",
                mixed_conditions=0,
                is_cumulative=1,
                is_recursive=1,
                min_qty=1,
                max_qty=4,
                recurse_for=1,
                free_qty=1,
                round_free_qty=1,
            )
            line = self._promo_trigger_row()
            line["qty"] = 5
            free_five = self._promo_free_row(rate=0)
            free_five["qty"] = 5
            free_six = self._promo_free_row(rate=0)
            free_six["qty"] = 6

            with patch.dict(sys.modules, _pricing_rule_utils_modules(-2.0, -200.0)):
                self.assertEqual(
                    self.controls.collect_below_buying_price_errors(
                        [line, free_five],
                        invoice_doc={**self.PROMO_INVOICE, "doctype": "POS Invoice"},
                    ),
                    [],
                )
                self.assertEqual(
                    len(
                        self.controls.collect_below_buying_price_errors(
                            [line, free_six],
                            invoice_doc={**self.PROMO_INVOICE, "doctype": "POS Invoice"},
                        )
                    ),
                    1,
                )

        with self.subTest("positive historical purchases (+2) plus current qty 3 exceeds max_qty 4 and rejects free row"):
            self._install_promotional_rule(
                apply_on="Item Code",
                mixed_conditions=0,
                is_cumulative=1,
                is_recursive=1,
                min_qty=1,
                max_qty=4,
                recurse_for=1,
                free_qty=1,
                round_free_qty=1,
            )
            line = self._promo_trigger_row()
            line["qty"] = 3
            free_three = self._promo_free_row(rate=0)
            free_three["qty"] = 3

            with patch.dict(sys.modules, _pricing_rule_utils_modules(2.0, 200.0)):
                self.assertEqual(
                    len(
                        self.controls.collect_below_buying_price_errors(
                            [line, free_three],
                            invoice_doc={**self.PROMO_INVOICE, "doctype": "POS Invoice"},
                        )
                    ),
                    1,
                )

        with self.subTest("negative historical returns (-2) plus current qty 2 falls below min_qty 1 and rejects free row"):
            self._install_promotional_rule(
                apply_on="Item Code",
                mixed_conditions=0,
                is_cumulative=1,
                is_recursive=1,
                min_qty=1,
                max_qty=4,
                recurse_for=1,
                free_qty=1,
                round_free_qty=1,
            )
            line = self._promo_trigger_row()
            line["qty"] = 2
            free_two = self._promo_free_row(rate=0)
            free_two["qty"] = 2

            with patch.dict(sys.modules, _pricing_rule_utils_modules(-2.0, -200.0)):
                self.assertEqual(
                    len(
                        self.controls.collect_below_buying_price_errors(
                            [line, free_two],
                            invoice_doc={**self.PROMO_INVOICE, "doctype": "POS Invoice"},
                        )
                    ),
                    1,
                )

        with self.subTest("cumulative min_amt/max_amt includes negative historical return amount (-200 + 500 = 300 in [100, 400])"):
            self._install_promotional_rule(
                apply_on="Item Code",
                mixed_conditions=0,
                is_cumulative=1,
                is_recursive=1,
                min_qty=0,
                max_qty=0,
                min_amt=100,
                max_amt=400,
                recurse_for=1,
                free_qty=1,
                round_free_qty=1,
            )
            line = self._promo_trigger_row(rate=100)
            line["qty"] = 5
            free_five = self._promo_free_row(rate=0)
            free_five["qty"] = 5

            with patch.dict(sys.modules, _pricing_rule_utils_modules(-2.0, -200.0)):
                self.assertEqual(
                    self.controls.collect_below_buying_price_errors(
                        [line, free_five],
                        invoice_doc={**self.PROMO_INVOICE, "doctype": "POS Invoice"},
                    ),
                    [],
                )

            with patch.dict(sys.modules, _pricing_rule_utils_modules(0.0, 0.0)):
                self.assertEqual(
                    len(
                        self.controls.collect_below_buying_price_errors(
                            [line, free_five],
                            invoice_doc={**self.PROMO_INVOICE, "doctype": "POS Invoice"},
                        )
                    ),
                    1,
                )

    def test_buying_list_currency_conversion_uses_for_buying_exchange_rate(self):
        self._install_buying_price(rate=10.0, uom="Nos", currency="EUR")
        self.frappe.db.get_value = lambda doctype, name, fieldname=None: {
            "Company": "UGX",
            "Price List": "EUR",
        }.get(doctype)

        fx_calls = []

        def fake_get_exchange_rate(from_curr, to_curr, transaction_date=None, args=None):
            fx_calls.append((from_curr, to_curr, args))
            if (from_curr, to_curr) == ("EUR", "UGX"):
                return 4050.0 if args == "for_buying" else 3900.0
            if (from_curr, to_curr) == ("USD", "UGX"):
                return 3700.0
            return 1.0

        erpnext_pkg = types.ModuleType("erpnext")
        erpnext_setup = types.ModuleType("erpnext.setup")
        erpnext_utils = types.ModuleType("erpnext.setup.utils")
        erpnext_utils.get_exchange_rate = fake_get_exchange_rate

        invoice_doc = {
            "company": "Test Co",
            "currency": "USD",
            "conversion_rate": 3700.0,
            "posting_date": "2026-11-28",
        }
        with patch.dict(
            sys.modules,
            {
                "erpnext": erpnext_pkg,
                "erpnext.setup": erpnext_setup,
                "erpnext.setup.utils": erpnext_utils,
            },
        ):
            # Selling at USD 10.80 -> UGX 39,960 is above for_selling (39,000) but below for_buying (40,500).
            errors = self.controls.collect_below_buying_price_errors(
                [{"item_code": "LOW", "item_name": "EUR Cost Item", "qty": 1, "rate": 10.80, "uom": "Nos"}],
                invoice_doc=invoice_doc,
            )
            self.assertEqual(len(errors), 1)
            self.assertAlmostEqual(errors[0]["base_buying_rate"], 40500.0, places=2)
            self.assertIn(("EUR", "UGX", "for_buying"), fx_calls)

            # Selling at USD 11.00 -> UGX 40,700 passes the 40,500 buying floor.
            ok_errors = self.controls.collect_below_buying_price_errors(
                [{"item_code": "LOW", "item_name": "EUR Cost Item", "qty": 1, "rate": 11.00, "uom": "Nos"}],
                invoice_doc=invoice_doc,
            )
            self.assertEqual(ok_errors, [])

    def test_uncalculated_all_zero_draft_does_not_reconcile_within_one_cent_remainder(self):
        self._install_bin_valuation(valuation_rate=10.0, company_currency="UGX")
        erpnext_pkg = types.ModuleType("erpnext")
        erpnext_setup = types.ModuleType("erpnext.setup")
        erpnext_utils = types.ModuleType("erpnext.setup.utils")
        erpnext_utils.get_exchange_rate = (
            lambda from_curr, to_curr, *_args, **_kwargs: 3700.0
            if (from_curr, to_curr) == ("USD", "UGX")
            else 1.0
        )

        with patch.dict(
            sys.modules,
            {
                "erpnext": erpnext_pkg,
                "erpnext.setup": erpnext_setup,
                "erpnext.setup.utils": erpnext_utils,
            },
        ):
            draft_rows = [
                PrecisionDoc(
                    item_code="LOW",
                    item_name=f"Zero Draft Line {idx}",
                    qty=1,
                    rate=0.01,
                    amount=0.01,
                    net_amount=0.0,
                    net_rate=0.0,
                    uom="Nos",
                    warehouse="Shop Floor - JBS",
                    precision_value=2,
                )
                for idx in (1, 2, 3)
            ]
            invoice = PrecisionDoc(
                company="Test Co",
                currency="USD",
                conversion_rate=3700.0,
                apply_discount_on="Net Total",
                discount_amount=0.02,
                precision_value=2,
            )
            # Uncalculated [0, 0, 0] must NOT reconcile against expected net total 0.01;
            # fallback 0.003333 * 3700 = UGX 12.33 >= UGX 10 passes all 3 lines.
            self.assertEqual(
                self.controls.collect_below_buying_price_errors(
                    draft_rows,
                    invoice_doc=invoice,
                ),
                [],
            )

            # ERPNext's calculated [0, 0.01, 0] reconciles cleanly: middle line passes (37 >= 10), outer lines fail (0 < 10).
            erpnext_rows = [
                PrecisionDoc(
                    item_code="LOW",
                    item_name=f"Calc Line {idx}",
                    qty=1,
                    rate=0.01,
                    amount=0.01,
                    net_amount=net_val,
                    net_rate=net_val,
                    base_net_rate=net_val * 3700.0,
                    uom="Nos",
                    warehouse="Shop Floor - JBS",
                    precision_value=2,
                )
                for idx, net_val in ((1, 0.0), (2, 0.01), (3, 0.0))
            ]
            calc_errors = self.controls.collect_below_buying_price_errors(
                erpnext_rows,
                invoice_doc=invoice,
            )
            self.assertEqual([e["item_name"] for e in calc_errors], ["Calc Line 1", "Calc Line 3"])

    def test_promotional_row_with_nonzero_free_item_rate_joins_discount_allocation_base(self):
        self._install_promotional_rule(apply_on="Item Code", free_qty=1)
        base_get_all = self.frappe.get_all

        def buying_prices_for_both(doctype, *args, **kwargs):
            if doctype == "Item Price":
                return [
                    {"name": "IP-TRIG", "item_code": "TRIG", "price_list_rate": 90.0, "uom": "Nos", "currency": "PKR"},
                    {"name": "IP-LOW", "item_code": "LOW", "price_list_rate": 100.0, "uom": "Nos", "currency": "PKR"},
                ]
            return base_get_all(doctype, *args, **kwargs)

        self.frappe.get_all = buying_prices_for_both
        invoice = {
            **self.PROMO_INVOICE,
            "apply_discount_on": "Net Total",
            "discount_amount": 11.0,
        }

        with self.subTest("reconciled ERPNext native net amounts [90, 9] pass against floor 90"):
            paid_row = {
                "item_code": "TRIG",
                "item_name": "Paid Trigger",
                "qty": 1,
                "rate": 100.0,
                "net_amount": 90.0,
                "net_rate": 90.0,
                "uom": "Nos",
                "pricing_rules": '["PR-FREE"]',
            }
            promo_row = self._promo_free_row(rate=10.0)
            promo_row["net_amount"] = 9.0
            promo_row["net_rate"] = 9.0
            self.assertEqual(
                self.controls.collect_below_buying_price_errors(
                    [paid_row, promo_row],
                    invoice_doc=invoice,
                ),
                [],
            )

        with self.subTest("fallback without native net amounts distributes 11 over 110 (paid net = 90)"):
            paid_row = {
                "item_code": "TRIG",
                "item_name": "Paid Trigger",
                "qty": 1,
                "rate": 100.0,
                "uom": "Nos",
                "pricing_rules": '["PR-FREE"]',
            }
            promo_row = self._promo_free_row(rate=10.0)
            self.assertEqual(
                self.controls.collect_below_buying_price_errors(
                    [paid_row, promo_row],
                    invoice_doc=invoice,
                ),
                [],
            )

    def test_cumulative_pricing_rule_enforces_zero_min_qty_and_min_amt_after_returns(self):
        def _pricing_rule_utils_modules(historical_qty, historical_amt):
            erpnext_pkg = types.ModuleType("erpnext")
            erpnext_accounts = types.ModuleType("erpnext.accounts")
            erpnext_doctype = types.ModuleType("erpnext.accounts.doctype")
            erpnext_pr = types.ModuleType("erpnext.accounts.doctype.pricing_rule")
            erpnext_pr_utils = types.ModuleType("erpnext.accounts.doctype.pricing_rule.utils")
            erpnext_pr_utils.get_cumulative_qty_and_amount_for_row = (
                lambda _pr_doc, _args, _doc=None: (historical_qty, historical_amt)
            )
            return {
                "erpnext": erpnext_pkg,
                "erpnext.accounts": erpnext_accounts,
                "erpnext.accounts.doctype": erpnext_doctype,
                "erpnext.accounts.doctype.pricing_rule": erpnext_pr,
                "erpnext.accounts.doctype.pricing_rule.utils": erpnext_pr_utils,
            }

        with self.subTest("min_qty=0 rejects negative combined qty (-2 + 1 = -1 < 0)"):
            self._install_promotional_rule(
                apply_on="Item Code",
                mixed_conditions=0,
                is_cumulative=1,
                is_recursive=1,
                min_qty=0,
                max_qty=4,
                min_amt=0,
                max_amt=0,
                recurse_for=1,
                free_qty=1,
                round_free_qty=1,
            )
            line = self._promo_trigger_row()
            line["qty"] = 1
            free_one = self._promo_free_row(rate=0)
            free_one["qty"] = 1

            with patch.dict(sys.modules, _pricing_rule_utils_modules(-2.0, 0.0)):
                self.assertEqual(
                    len(
                        self.controls.collect_below_buying_price_errors(
                            [line, free_one],
                            invoice_doc={**self.PROMO_INVOICE, "doctype": "POS Invoice"},
                        )
                    ),
                    1,
                )

        with self.subTest("min_amt=0 rejects negative combined amount (-200 + 100 = -100 < 0)"):
            self._install_promotional_rule(
                apply_on="Item Code",
                mixed_conditions=0,
                is_cumulative=1,
                is_recursive=1,
                min_qty=1,
                max_qty=4,
                min_amt=0,
                max_amt=0,
                recurse_for=1,
                free_qty=1,
                round_free_qty=1,
            )
            line = self._promo_trigger_row(rate=20)
            line["qty"] = 5
            free_five = self._promo_free_row(rate=0)
            free_five["qty"] = 5

            with patch.dict(sys.modules, _pricing_rule_utils_modules(-2.0, -200.0)):
                self.assertEqual(
                    len(
                        self.controls.collect_below_buying_price_errors(
                            [line, free_five],
                            invoice_doc={**self.PROMO_INVOICE, "doctype": "POS Invoice"},
                        )
                    ),
                    1,
                )

    def test_cumulative_history_read_error_propagates_instead_of_silently_changing_eligibility(self):
        def _raising_pricing_rule_utils_modules():
            erpnext_pkg = types.ModuleType("erpnext")
            erpnext_accounts = types.ModuleType("erpnext.accounts")
            erpnext_doctype = types.ModuleType("erpnext.accounts.doctype")
            erpnext_pr = types.ModuleType("erpnext.accounts.doctype.pricing_rule")
            erpnext_pr_utils = types.ModuleType("erpnext.accounts.doctype.pricing_rule.utils")

            def _raise_db_error(_pr_doc, _args, _doc=None):
                raise RuntimeError("cumulative history query failed")

            erpnext_pr_utils.get_cumulative_qty_and_amount_for_row = _raise_db_error
            return {
                "erpnext": erpnext_pkg,
                "erpnext.accounts": erpnext_accounts,
                "erpnext.accounts.doctype": erpnext_doctype,
                "erpnext.accounts.doctype.pricing_rule": erpnext_pr,
                "erpnext.accounts.doctype.pricing_rule.utils": erpnext_pr_utils,
            }

        with self.subTest("R6-5 sale surfaces cumulative lookup error instead of rejecting qty=5 alone"):
            self._install_promotional_rule(
                apply_on="Item Code",
                mixed_conditions=0,
                is_cumulative=1,
                is_recursive=1,
                min_qty=1,
                max_qty=4,
                recurse_for=1,
                free_qty=1,
                round_free_qty=1,
            )
            line = self._promo_trigger_row()
            line["qty"] = 5
            free_five = self._promo_free_row(rate=0)
            free_five["qty"] = 5

            with patch.dict(sys.modules, _raising_pricing_rule_utils_modules()):
                with self.assertRaisesRegex(RuntimeError, "cumulative history query failed"):
                    self.controls.collect_below_buying_price_errors(
                        [line, free_five],
                        invoice_doc={**self.PROMO_INVOICE, "doctype": "POS Invoice"},
                    )

        with self.subTest("minimum-threshold rule surfaces cumulative lookup error instead of bypassing min_qty"):
            self._install_promotional_rule(
                apply_on="Item Code",
                mixed_conditions=0,
                is_cumulative=1,
                is_recursive=1,
                min_qty=10,
                max_qty=0,
                recurse_for=1,
                free_qty=1,
                round_free_qty=1,
            )
            line = self._promo_trigger_row()
            line["qty"] = 1
            free_one = self._promo_free_row(rate=0)
            free_one["qty"] = 1

            with patch.dict(sys.modules, _raising_pricing_rule_utils_modules()):
                with self.assertRaisesRegex(RuntimeError, "cumulative history query failed"):
                    self.controls.collect_below_buying_price_errors(
                        [line, free_one],
                        invoice_doc={**self.PROMO_INVOICE, "doctype": "POS Invoice"},
                    )

    def test_same_foreign_currency_buying_price_uses_for_buying_fx_instead_of_selling_rate(self):
        self._install_buying_price(rate=10.0, uom="Nos", currency="USD")
        self.frappe.db.get_value = lambda doctype, name, fieldname=None: (
            "UGX"
            if doctype == "Company" and fieldname == "default_currency"
            else ("USD" if doctype == "Price List" and fieldname == "currency" else None)
        )

        fx_calls = []

        def fake_get_exchange_rate(from_curr, to_curr, transaction_date=None, args=None):
            fx_calls.append((from_curr, to_curr, args))
            if (from_curr, to_curr) == ("USD", "UGX"):
                return 4050.0 if args == "for_buying" else 3700.0
            return 1.0

        erpnext_pkg = types.ModuleType("erpnext")
        erpnext_setup = types.ModuleType("erpnext.setup")
        erpnext_utils = types.ModuleType("erpnext.setup.utils")
        erpnext_utils.get_exchange_rate = fake_get_exchange_rate

        invoice_doc = {
            "company": "Test Co",
            "currency": "USD",
            "conversion_rate": 3700.0,
            "posting_date": "2026-11-28",
        }
        with patch.dict(
            sys.modules,
            {
                "erpnext": erpnext_pkg,
                "erpnext.setup": erpnext_setup,
                "erpnext.setup.utils": erpnext_utils,
            },
        ):
            # USD 10.50 @ selling 3,700 = UGX 38,850 < USD 10 buying @ 4,050 = UGX 40,500.
            errors = self.controls.collect_below_buying_price_errors(
                [{"item_code": "LOW", "item_name": "USD Same-Curr Item", "qty": 1, "rate": 10.50, "uom": "Nos"}],
                invoice_doc=invoice_doc,
            )
            self.assertEqual(len(errors), 1)
            self.assertAlmostEqual(errors[0]["base_buying_rate"], 40500.0, places=2)
            self.assertIn(("USD", "UGX", "for_buying"), fx_calls)

            # USD 11.00 @ selling 3,700 = UGX 40,700 >= UGX 40,500 passes.
            self.assertEqual(
                self.controls.collect_below_buying_price_errors(
                    [{"item_code": "LOW", "item_name": "USD Same-Curr Item", "qty": 1, "rate": 11.00, "uom": "Nos"}],
                    invoice_doc=invoice_doc,
                ),
                [],
            )

    def test_uncalculated_all_zero_draft_with_base_net_zeros_does_not_falsely_block(self):
        self._install_bin_valuation(valuation_rate=10.0, company_currency="UGX")
        erpnext_pkg = types.ModuleType("erpnext")
        erpnext_setup = types.ModuleType("erpnext.setup")
        erpnext_utils = types.ModuleType("erpnext.setup.utils")
        erpnext_utils.get_exchange_rate = (
            lambda from_curr, to_curr, *_args, **_kwargs: 3700.0
            if (from_curr, to_curr) == ("USD", "UGX")
            else 1.0
        )

        with patch.dict(
            sys.modules,
            {
                "erpnext": erpnext_pkg,
                "erpnext.setup": erpnext_setup,
                "erpnext.setup.utils": erpnext_utils,
            },
        ):
            # creation.py:1989-2010 derives base_net_amount=0 and base_net_rate=0 from zero net fields;
            # on an uncalculated [0, 0, 0] draft with expected net total 0.01, none of the 3 rows should block.
            draft_rows = [
                PrecisionDoc(
                    item_code="LOW",
                    item_name=f"Zero Base Draft {idx}",
                    qty=1,
                    rate=0.01,
                    amount=0.01,
                    net_amount=0.0,
                    net_rate=0.0,
                    base_net_amount=0.0,
                    base_net_rate=0.0,
                    uom="Nos",
                    warehouse="Shop Floor - JBS",
                    precision_value=2,
                )
                for idx in (1, 2, 3)
            ]
            invoice = PrecisionDoc(
                company="Test Co",
                currency="USD",
                conversion_rate=3700.0,
                apply_discount_on="Net Total",
                discount_amount=0.02,
                precision_value=2,
            )
            self.assertEqual(
                self.controls.collect_below_buying_price_errors(
                    draft_rows,
                    invoice_doc=invoice,
                ),
                [],
            )

    def test_buying_price_list_price_not_uom_dependent_does_not_scale_stock_uom_fallback(self):
        self._install_buying_price(rate=8.0, uom="Each", currency="USD")
        base_get_all = self.frappe.get_all

        def get_all_with_uom(doctype, *args, **kwargs):
            if doctype == "Item":
                return [{"item_code": "LOW", "stock_uom": "Each", "is_stock_item": 1}]
            if doctype == "UOM Conversion Detail":
                return [{"parent": "LOW", "uom": "Box", "conversion_factor": 10.0}]
            return base_get_all(doctype, *args, **kwargs)

        self.frappe.get_all = get_all_with_uom

        def fake_db_get_value(doctype, name, fieldname=None):
            if doctype == "Company" and fieldname == "default_currency":
                return "USD"
            if doctype == "Price List" and fieldname == "currency":
                return "USD"
            if doctype == "Price List" and fieldname == "price_not_uom_dependent":
                return 1
            return None

        self.frappe.db.get_value = fake_db_get_value
        invoice_doc = {"company": "Test Co", "currency": "USD", "conversion_rate": 1.0}

        # With price_not_uom_dependent=1, stock-UOM fallback stays 8 (not 8 * 10 = 80).
        self.assertEqual(
            self.controls.collect_below_buying_price_errors(
                [{"item_code": "LOW", "item_name": "Boxed PNUD Item", "qty": 1, "rate": 10.0, "uom": "Box"}],
                invoice_doc=invoice_doc,
            ),
            [],
        )
        low_errors = self.controls.collect_below_buying_price_errors(
            [{"item_code": "LOW", "item_name": "Boxed PNUD Item", "qty": 1, "rate": 7.0, "uom": "Box"}],
            invoice_doc=invoice_doc,
        )
        self.assertEqual(len(low_errors), 1)
        self.assertAlmostEqual(low_errors[0]["buying_rate"], 8.0, places=2)

    def test_legitimately_rounded_tax_inclusive_net_passes_server_floor(self):
        self._install_bin_valuation(valuation_rate=35.0, company_currency="UGX")
        erpnext_pkg = types.ModuleType("erpnext")
        erpnext_setup = types.ModuleType("erpnext.setup")
        erpnext_utils = types.ModuleType("erpnext.setup.utils")
        erpnext_utils.get_exchange_rate = (
            lambda from_curr, to_curr, *_args, **_kwargs: 3700.0
            if (from_curr, to_curr) == ("USD", "UGX")
            else 1.0
        )

        # 1 x USD 0.01 with 18% inclusive VAT -> ERPNext rounds net_amount to 0.01 -> base_net_amount = UGX 37 >= UGX 35.
        row = PrecisionDoc(
            item_code="LOW",
            item_name="Rounded Inclusive Item",
            qty=1,
            rate=0.01,
            amount=0.01,
            net_amount=0.01,
            net_rate=0.01,
            base_net_amount=37.0,
            base_net_rate=37.0,
            uom="Nos",
            warehouse="Shop Floor - JBS",
            precision_value=2,
        )
        invoice = PrecisionDoc(
            company="Test Co",
            currency="USD",
            conversion_rate=3700.0,
            taxes=[
                {
                    "charge_type": "On Net Total",
                    "account_head": "VAT 18% - CO",
                    "rate": 18,
                    "included_in_print_rate": 1,
                }
            ],
            precision_value=2,
        )
        with patch.dict(
            sys.modules,
            {
                "erpnext": erpnext_pkg,
                "erpnext.setup": erpnext_setup,
                "erpnext.setup.utils": erpnext_utils,
            },
        ):
            self.assertEqual(
                self.controls.collect_below_buying_price_errors([row], invoice_doc=invoice),
                [],
            )

            # Floor of UGX 38 > UGX 37 still blocks.
            self._install_bin_valuation(valuation_rate=38.0, company_currency="UGX")
            self.assertEqual(
                len(self.controls.collect_below_buying_price_errors([row], invoice_doc=invoice)),
                1,
            )

    def test_cumulative_pricing_rule_fails_closed_when_history_helper_unimportable(self):
        self._install_promotional_rule(
            apply_on="Item Code",
            mixed_conditions=0,
            is_cumulative=1,
            is_recursive=1,
            min_qty=10,
            max_qty=0,
            recurse_for=1,
            free_qty=1,
            round_free_qty=1,
        )
        line = self._promo_trigger_row()
        line["qty"] = 1
        free_one = self._promo_free_row(rate=0)
        free_one["qty"] = 1

        # Blocking erpnext.accounts.doctype.pricing_rule.utils with None forces ImportError;
        # cumulative promotion MUST fail closed rather than setting enforce_min=False.
        with patch.dict(sys.modules, {"erpnext.accounts.doctype.pricing_rule.utils": None}):
            errors = self.controls.collect_below_buying_price_errors(
                [line, free_one],
                invoice_doc={**self.PROMO_INVOICE, "doctype": "POS Invoice"},
            )
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["item_code"], "LOW")

    def test_carried_inclusive_tax_rounding_reconciles_across_same_tax_group_rows(self):
        erpnext_pkg = types.ModuleType("erpnext")
        erpnext_setup = types.ModuleType("erpnext.setup")
        erpnext_utils = types.ModuleType("erpnext.setup.utils")
        erpnext_utils.get_exchange_rate = (
            lambda from_curr, to_curr, *_args, **_kwargs: 3700.0
            if (from_curr, to_curr) == ("USD", "UGX")
            else 1.0
        )

        # Two USD 0.04 lines @ 18% inclusive VAT -> ERPNext taxes_and_totals.py:438-455
        # carries rounding within the tax group: [0.03, 0.04] (sum 0.07 == round(0.08/1.18, 2)).
        # At FX 3,700, row 1 earns UGX 111 (>= 100) and row 2 earns UGX 148 (>= 140).
        def fake_get_all(doctype, **kwargs):
            if doctype == "Item":
                return [
                    {"name": "ITEM-CARRY-1", "stock_uom": "Nos", "is_stock_item": 1, "valuation_rate": 0},
                    {"name": "ITEM-CARRY-2", "stock_uom": "Nos", "is_stock_item": 1, "valuation_rate": 0},
                    {"name": "ITEM-TAX-FREE", "stock_uom": "Nos", "is_stock_item": 1, "valuation_rate": 0},
                ]
            if doctype == "Bin":
                return [
                    {"item_code": "ITEM-CARRY-1", "warehouse": "Shop Floor - JBS", "valuation_rate": 100.0},
                    {"item_code": "ITEM-CARRY-2", "warehouse": "Shop Floor - JBS", "valuation_rate": 140.0},
                    {"item_code": "ITEM-TAX-FREE", "warehouse": "Shop Floor - JBS", "valuation_rate": 1000.0},
                ]
            return []

        self.frappe.get_all = fake_get_all
        self.frappe.db.get_value = lambda doctype, name, fieldname=None: (
            "UGX" if doctype == "Company" and fieldname == "default_currency" else None
        )

        row_1 = PrecisionDoc(
            item_code="ITEM-CARRY-1",
            item_name="Carry Row 1",
            qty=1,
            rate=0.04,
            amount=0.04,
            net_rate=0.03,
            net_amount=0.03,
            base_net_rate=111.0,
            base_net_amount=111.0,
            uom="Nos",
            warehouse="Shop Floor - JBS",
            precision_value=2,
        )
        row_2 = PrecisionDoc(
            item_code="ITEM-CARRY-2",
            item_name="Carry Row 2",
            qty=1,
            rate=0.04,
            amount=0.04,
            net_rate=0.04,
            net_amount=0.04,
            base_net_rate=148.0,
            base_net_amount=148.0,
            uom="Nos",
            warehouse="Shop Floor - JBS",
            precision_value=2,
        )
        invoice = PrecisionDoc(
            company="Test Co",
            currency="USD",
            conversion_rate=3700.0,
            taxes=[
                {
                    "charge_type": "On Net Total",
                    "account_head": "VAT 18% - CO",
                    "rate": 18,
                    "included_in_print_rate": 1,
                }
            ],
            precision_value=2,
        )

        with patch.dict(
            sys.modules,
            {
                "erpnext": erpnext_pkg,
                "erpnext.setup": erpnext_setup,
                "erpnext.setup.utils": erpnext_utils,
            },
        ):
            # Valid ERPNext carried rounding [0.03, 0.04] across same-tax-group rows passes.
            self.assertEqual(
                self.controls.collect_below_buying_price_errors([row_1, row_2], invoice_doc=invoice),
                [],
            )

            # Single forged row claiming net_amount=0.04 (unrounded 0.0339 -> UGX 125.42 < 140) still blocks.
            single_errors = self.controls.collect_below_buying_price_errors([row_2], invoice_doc=invoice)
            self.assertEqual(len(single_errors), 1)
            self.assertEqual(single_errors[0]["item_code"], "ITEM-CARRY-2")

            # Cross-tax-group tampering (0% tax row shaved 10.00 -> 9.99 to fund +0.01 on 18% VAT row) still blocks.
            tax_free_shaved = PrecisionDoc(
                item_code="ITEM-TAX-FREE",
                item_name="Tax Free Shaved",
                qty=1,
                rate=10.00,
                amount=10.00,
                net_rate=9.99,
                net_amount=9.99,
                base_net_rate=36963.0,
                base_net_amount=36963.0,
                item_tax_rate='{"VAT 18% - CO": 0}',
                uom="Nos",
                warehouse="Shop Floor - JBS",
                precision_value=2,
            )
            cross_group_errors = self.controls.collect_below_buying_price_errors(
                [tax_free_shaved, row_2],
                invoice_doc=invoice,
            )
            self.assertEqual(len(cross_group_errors), 1)
            self.assertEqual(cross_group_errors[0]["item_code"], "ITEM-CARRY-2")


if __name__ == "__main__":
    unittest.main()
