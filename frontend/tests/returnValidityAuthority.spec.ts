// @vitest-environment jsdom

import { mount } from "@vue/test-utils";
import VueDatePicker from "@vuepic/vue-datepicker";
import { defineComponent, h, ref } from "vue";
import { beforeEach, describe, expect, it, vi } from "vitest";

import PaymentAdditionalInfo from "../src/posapp/components/pos/payments/PaymentAdditionalInfo.vue";
import { useInvoiceDetails } from "../src/posapp/composables/pos/invoice/useInvoiceDetails";

vi.mock("../src/offline/index", () => ({}));

const Box = defineComponent({
	setup(_, { slots }) {
		return () => h("div", slots.default?.());
	},
});

describe("server-owned return validity", () => {
	beforeEach(() => {
		(window as any).frappe = { _: (text: string) => text };
		(window as any).__ = (text: string) => text;
	});

	it("displays the saved server date in a disabled payment picker", () => {
		const wrapper = mount(PaymentAdditionalInfo, {
			props: {
				invoiceDoc: { posa_return_valid_upto: "2026-11-25" },
				posProfile: {},
				returnValidityEnabled: true,
				returnValidUptoDate: "2026-11-25",
			},
			global: {
				components: { VueDatePicker, VRow: Box, VCol: Box },
			},
		});

		const input = wrapper.get("input");
		expect.soft((input.element as HTMLInputElement).value).toBe("25-11-2026");
		expect.soft((input.element as HTMLInputElement).disabled).toBe(true);
		wrapper.unmount();
	});

	it("does not replace the server date with a client-selected date", () => {
		const invoiceDoc = ref({ posa_return_valid_upto: "2026-11-25" });
		const details = useInvoiceDetails({
			invoiceDoc,
			posProfile: ref({ posa_enable_return_validity: 1, posa_return_validity_days: 30 }),
			invoiceType: ref("Invoice"),
		});
		details.initializeReturnValidity(invoiceDoc.value);

		details.updateReturnValidUpto("2027-12-31");

		expect(invoiceDoc.value.posa_return_valid_upto).toBe("2026-11-25");
		expect(details.return_valid_upto_date.value).toBe("2026-11-25");
	});

	it("does not invent a client-side date before the server computes it", () => {
		const invoiceDoc = ref({ posting_date: "2026-10-26" });
		const details = useInvoiceDetails({
			invoiceDoc,
			posProfile: ref({ posa_enable_return_validity: 1, posa_return_validity_days: 30 }),
			invoiceType: ref("Invoice"),
		});

		details.initializeReturnValidity(invoiceDoc.value);

		expect(details.return_valid_upto_date.value).toBeNull();
		expect(invoiceDoc.value).toEqual({ posting_date: "2026-10-26" });
	});
});
