// @vitest-environment jsdom

import { describe, expect, it } from "vitest";
import { defineComponent, h } from "vue";
import { mount } from "@vue/test-utils";

import PayInvoicesTable from "../src/posapp/components/pos_pay/PayInvoicesTable.vue";

const BoxStub = defineComponent({
	setup(_, { slots }) {
		return () => h("div", {}, slots.default?.());
	},
});

const VDataTableStub = defineComponent({
	props: {
		items: {
			type: Array,
			default: () => [],
		},
	},
	setup(props, { slots }) {
		return () =>
			h(
				"div",
				{ class: "v-data-table-stub" },
				(props.items as any[]).map((item) =>
					h(
						"div",
						{
							class: "invoice-row",
							"data-voucher": item.voucher_no,
						},
						[
							h(
								"span",
								{ class: "outstanding-cell" },
								slots["item.outstanding_amount"]?.({ item }),
							),
						],
					),
				),
			);
	},
});

describe("PayInvoicesTable outstanding currency display", () => {
	it("labels unconvertible third-currency outstanding rows with party_account_currency instead of invoice currency", () => {
		const unconvertibleRow = {
			voucher_no: "SINV-EUR-NOQUOTE",
			currency: "USD",
			party_account_currency: "EUR",
			outstanding_amount: 10,
			outstanding_amount_in_invoice_currency: null,
			outstanding_conversion_unavailable: true,
			invoice_amount: 11,
		};
		const convertedRow = {
			voucher_no: "SINV-USD-UGX",
			currency: "USD",
			party_account_currency: "UGX",
			outstanding_amount: 37000,
			outstanding_amount_in_invoice_currency: 10,
			outstanding_conversion_unavailable: false,
			invoice_amount: 10,
		};

		const wrapper = mount(PayInvoicesTable, {
			props: {
				invoices: [unconvertibleRow, convertedRow],
				filteredInvoices: [unconvertibleRow, convertedRow],
				posProfile: { currency: "USD" },
				posProfilesList: [],
				posProfileSearch: "",
				currencyFilter: "ALL",
				currencies: ["USD"],
				outstandingByCurrency: {},
				totalOutstanding: 10,
				totalSelected: 10,
				selectedCount: 1,
				loading: false,
				autoReconcileLoading: false,
				autoReconcileSummary: "",
				partyName: "Test Customer",
				sectionTitle: "Invoices",
				isInvoiceSelected: () => false,
				itemClass: () => "",
				currencySymbol: (code: string) => `${code} `,
				formatCurrency: (value: number) => String(value),
				headers: [],
			},
			global: {
				components: {
					VRow: BoxStub,
					VCol: BoxStub,
					VSelect: BoxStub,
					VBtn: BoxStub,
					VCheckbox: BoxStub,
					VDivider: BoxStub,
					VDataTable: VDataTableStub,
				},
			},
		});

		const unconvertibleText = wrapper
			.find('[data-voucher="SINV-EUR-NOQUOTE"] .outstanding-cell')
			.text()
			.replace(/\s+/g, " ")
			.trim();
		const convertedText = wrapper
			.find('[data-voucher="SINV-USD-UGX"] .outstanding-cell')
			.text()
			.replace(/\s+/g, " ")
			.trim();

		expect(unconvertibleText).toBe("EUR 10");
		expect(convertedText).toBe("USD 10");
	});
});
