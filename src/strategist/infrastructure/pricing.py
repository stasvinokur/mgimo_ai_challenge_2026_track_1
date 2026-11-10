"""Справочные цены из config/prices.yaml (допущения, а не счёт провайдера)."""

from __future__ import annotations

from typing import Any


class Pricing:
    def __init__(self, prices: dict[str, Any]) -> None:
        self.prices = prices or {}
        self.models: dict[str, dict[str, Any]] = self.prices.get("models", {}) or {}
        self.default: dict[str, Any] = self.prices.get(
            "default", {"input": 0.0, "output": 0.0, "estimated": True}
        )

    @property
    def rate(self) -> float:
        return float(self.prices.get("currency_rate_rub_per_usd", 80.0))

    @property
    def human_rate_per_hour(self) -> float:
        return float(self.prices.get("human_rate_rub_per_hour", 1500.0))

    def usd(self, model: str, input_tokens: int, output_tokens: int) -> tuple[float, bool]:
        entry = self.models.get(model) or self.default
        usd = (
            input_tokens * float(entry.get("input", 0)) + output_tokens * float(entry.get("output", 0))
        ) / 1e6
        return round(usd, 8), bool(entry.get("estimated", True))

    def rub(self, usd: float) -> float:
        return round(usd * self.rate, 6)

    def human_rub(self, minutes: float) -> float:
        return round(minutes / 60.0 * self.human_rate_per_hour, 2)

    def subscription_cost_usd(self, list_price_usd: float) -> float:
        """Фактические деньги на подписке: кредиты плана списываются по прайсу, абонентская плата меньше их."""
        sub = self.prices.get("subscription") or {}
        credits = float(sub.get("included_credits_usd") or 0)
        if not credits:
            return self.subscription_share_usd()
        return round(list_price_usd * float(sub.get("usd_per_month", 0.0)) / credits, 6)

    def subscription_share_usd(self) -> float:
        sub = self.prices.get("subscription") or {}
        runs = float(sub.get("expected_runs_per_month") or 0)
        if not runs:
            return 0.0
        return round(float(sub.get("usd_per_month", 0.0)) / runs, 4)
