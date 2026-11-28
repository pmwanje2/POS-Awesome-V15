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
                    for k, v in filters.items():
                        if isinstance(v, list) and len(v) == 2 and v[0] == "<=":
                            if not (str(row.get(k)) <= str(v[1])):
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
                uom="Box",
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


if __name__ == "__main__":
    unittest.main()
