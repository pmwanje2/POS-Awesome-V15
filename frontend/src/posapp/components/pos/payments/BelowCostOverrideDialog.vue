<template>
	<v-dialog
		:model-value="modelValue"
		max-width="620"
		persistent
		@update:model-value="$emit('update:modelValue', $event)"
	>
		<v-card class="below-cost-override">
			<div class="below-cost-override__header">
				<div class="below-cost-override__mark" aria-hidden="true">
					<v-icon size="28">mdi-shield-alert-outline</v-icon>
				</div>
				<div>
					<p class="below-cost-override__eyebrow">{{ __("Controlled sale") }}</p>
					<h2 class="below-cost-override__title">
						{{ __("POS supervisor approval required") }}
					</h2>
					<p class="below-cost-override__subtitle">
						{{
							__(
								"These items are below the permitted selling floor. Approval will be recorded on the invoice.",
							)
						}}
					</p>
				</div>
			</div>

			<div class="below-cost-override__body">
				<div class="below-cost-override__items" role="list">
					<div
						v-for="risk in risks"
						:key="risk.itemCode"
						class="below-cost-override__item"
						role="listitem"
					>
						<div>
							<strong>{{ risk.itemName || risk.itemCode }}</strong>
							<span v-if="risk.itemCode && risk.itemName !== risk.itemCode">
								{{ risk.itemCode }}
							</span>
						</div>
						<div class="below-cost-override__rates">
							<span>{{ __("Sale") }} {{ formatRate(risk.sellingRate) }}</span>
							<v-icon size="16">mdi-arrow-right</v-icon>
							<strong>{{ risk.costLabel || __("Minimum") }} {{ formatRate(risk.costRate) }}</strong>
						</div>
					</div>
				</div>

				<div class="below-cost-override__actor">
					<span>{{ __("Approver") }}</span>
					<strong>{{ cashierName || __("Active POS supervisor") }}</strong>
				</div>

				<v-textarea
					ref="reasonInput"
					:model-value="reason"
					:label="__('Override reason')"
					:placeholder="__('Example: approved clearance of near-expiry stock')"
					:counter="240"
					maxlength="240"
					rows="3"
					auto-grow
					variant="outlined"
					color="warning"
					:rules="[(value) => Boolean(String(value || '').trim()) || __('Reason is required')]"
					@update:model-value="$emit('update:reason', $event)"
					@keydown.ctrl.enter="approve"
				/>
			</div>

			<div class="below-cost-override__actions">
				<v-btn variant="text" size="large" @click="$emit('cancel')">
					{{ __("Cancel") }}
				</v-btn>
				<v-btn
					color="warning"
					variant="flat"
					size="large"
					prepend-icon="mdi-shield-check-outline"
					:disabled="!canApprove"
					@click="approve"
				>
					{{ __("Approve below-cost sale") }}
				</v-btn>
			</div>
		</v-card>
	</v-dialog>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";

declare const __: (_text: string, _args?: any[]) => string;

const props = defineProps<{
	modelValue: boolean;
	risks: any[];
	reason: string;
	cashierName?: string;
	formatFloat?: (value: unknown) => number | string;
}>();

const emit = defineEmits<{
	(_event: "update:modelValue", _value: boolean): void;
	(_event: "update:reason", _value: string): void;
	(_event: "approve"): void;
	(_event: "cancel"): void;
}>();

const reasonInput = ref<any>(null);
const canApprove = computed(() => Boolean(String(props.reason || "").trim()));
const formatRate = (value: unknown) =>
	props.formatFloat ? props.formatFloat(value) : Number(value || 0).toFixed(2);
const approve = () => {
	if (canApprove.value) emit("approve");
};

watch(
	() => props.modelValue,
	(open) => {
		if (open) nextTick(() => reasonInput.value?.focus?.());
	},
);
</script>

<style scoped>
.below-cost-override {
	--override-ink: var(--pos-text-primary, #17212b);
	--override-muted: var(--pos-text-secondary, #5f6b76);
	--override-line: rgba(217, 119, 6, 0.24);
	overflow: hidden;
	border: 1px solid var(--override-line);
	background:
		linear-gradient(135deg, rgba(245, 158, 11, 0.08), transparent 42%),
		var(--pos-surface, #fff);
}

.below-cost-override__header {
	display: grid;
	grid-template-columns: auto 1fr;
	gap: 18px;
	padding: 26px 28px 20px;
	border-bottom: 1px solid var(--override-line);
}

.below-cost-override__mark {
	display: grid;
	place-items: center;
	width: 52px;
	height: 52px;
	border-radius: 14px;
	color: #92400e;
	background: #fef3c7;
	box-shadow: inset 0 0 0 1px #fcd34d;
}

.below-cost-override__eyebrow {
	margin: 0 0 3px;
	color: #b45309;
	font-size: 0.72rem;
	font-weight: 800;
	letter-spacing: 0.12em;
	text-transform: uppercase;
}

.below-cost-override__title {
	margin: 0;
	color: var(--override-ink);
	font-size: 1.35rem;
	line-height: 1.25;
}

.below-cost-override__subtitle {
	margin: 8px 0 0;
	color: var(--override-muted);
	line-height: 1.5;
}

.below-cost-override__body {
	display: grid;
	gap: 18px;
	padding: 22px 28px 10px;
}

.below-cost-override__items {
	display: grid;
	gap: 8px;
	max-height: 190px;
	overflow-y: auto;
}

.below-cost-override__item {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 16px;
	padding: 12px 14px;
	border-left: 3px solid #f59e0b;
	border-radius: 8px;
	background: rgba(245, 158, 11, 0.07);
}

.below-cost-override__item span {
	display: block;
	color: var(--override-muted);
	font-size: 0.76rem;
}

.below-cost-override__rates {
	display: flex;
	align-items: center;
	gap: 7px;
	white-space: nowrap;
	font-variant-numeric: tabular-nums;
}

.below-cost-override__actor {
	display: flex;
	justify-content: space-between;
	padding: 10px 0;
	border-bottom: 1px dashed var(--override-line);
	color: var(--override-muted);
}

.below-cost-override__actor strong {
	color: var(--override-ink);
}

.below-cost-override__actions {
	display: flex;
	justify-content: flex-end;
	gap: 10px;
	padding: 16px 28px 24px;
}

@media (max-width: 600px) {
	.below-cost-override__header,
	.below-cost-override__body {
		padding-left: 18px;
		padding-right: 18px;
	}

	.below-cost-override__item,
	.below-cost-override__actions {
		align-items: stretch;
		flex-direction: column;
	}

	.below-cost-override__rates {
		justify-content: space-between;
	}
}
</style>
