import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import Mock, patch


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


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
    frappe_utils.flt = lambda value, precision=None: float(value or 0)
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
                return [dict(rule)]
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
            [self._promo_trigger_row("PR-OTHER, PR-FREE"), self._promo_free_row()],
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
        self.assertEqual(errors[0]["reason"], "missing_buying_price")

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


if __name__ == "__main__":
    unittest.main()
