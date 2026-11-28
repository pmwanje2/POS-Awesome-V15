export type CurrencyRateStalePolicy = {
	allowStale?: number | boolean | null;
	staleDays?: number | null;
};

export function isCurrencyFlagEnabled(value: unknown): boolean {
	return value === 1 || value === true || value === "1";
}

export function oppositeCurrencyPurpose(
	purpose?: string | null,
): "for_buying" | "for_selling" | null {
	if (purpose === "for_buying") {
		return "for_selling";
	}
	if (purpose === "for_selling") {
		return "for_buying";
	}
	return null;
}

export function subtractDaysIso(isoDate: string, days: number): string | null {
	const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate.slice(0, 10));
	if (!match) {
		return null;
	}
	const year = Number(match[1]);
	const month = Number(match[2]);
	const day = Number(match[3]);
	const dt = new Date(Date.UTC(year, month - 1, day - days));
	if (Number.isNaN(dt.getTime())) {
		return null;
	}
	return dt.toISOString().slice(0, 10);
}

/**
 * Enforce ERPNext setup/utils.py:84-87 effective-date and stale-days boundary:
 * - Rate date must be on or before `targetDate`
 * - When `allowStale` is explicitly disabled (`0`/`false`), rate date must satisfy
 *   strict `rateDate > (targetDate - staleDays)`.
 */
export function isCurrencyRateDateWithinStalePolicy(
	rateDate: string | null | undefined,
	targetDate: string | null | undefined,
	options?: CurrencyRateStalePolicy | null,
): boolean {
	const cutoff = String(targetDate || "").slice(0, 10);
	const rowDate = String(rateDate || "").slice(0, 10);
	if (cutoff && rowDate && rowDate > cutoff) {
		return false;
	}
	const enforceStaleLimit = Boolean(
		cutoff &&
			options &&
			options.allowStale !== undefined &&
			options.allowStale !== null &&
			!isCurrencyFlagEnabled(options.allowStale),
	);
	if (!enforceStaleLimit) {
		return true;
	}
	const staleDays =
		options?.staleDays !== undefined &&
		options?.staleDays !== null &&
		Number.isFinite(Number(options.staleDays)) &&
		Number(options.staleDays) >= 0
			? Math.trunc(Number(options.staleDays))
			: 1;
	const checkpointDate = subtractDaysIso(cutoff, staleDays);
	if (checkpointDate && (!rowDate || rowDate <= checkpointDate)) {
		return false;
	}
	return true;
}
