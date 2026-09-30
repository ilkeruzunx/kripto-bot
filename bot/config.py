from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class StrategyConfig:
    timeframe: str = "1h"
    rsi_period: int = 14
    rsi_below: float | None = 40
    trend_ema: int | None = 200
    base_order: float = 1000
    safety_orders: int = 3
    safety_order: float = 1000
    safety_volume_scale: float = 1.3
    safety_step_pct: float = 4
    safety_step_scale: float = 1.0
    take_profit_pct: float = 3
    trailing_pct: float = 1.0
    stop_loss_pct: float | None = 12
    cooldown_after_tp_min: int = 60
    cooldown_after_sl_min: int = 1440

    def safety_sizes(self) -> list[float]:
        return [round(self.safety_order * self.safety_volume_scale**i, 2) for i in range(self.safety_orders)]

    def max_spend(self) -> float:
        """Bir coinde tüm kademeler dolduğunda harcanacak en fazla TL."""
        return self.base_order + sum(self.safety_sizes())

    def safety_drop_pct(self, index: int) -> float:
        """index. ek alım için son alımdan gereken düşüş (%)."""
        return self.safety_step_pct * self.safety_step_scale**index

    def candles_needed(self) -> int:
        rsi_need = (self.rsi_period + 1) * 3 if self.rsi_below is not None else 0
        return max(self.trend_ema or 0, rsi_need, 1)

    def validate(self, coin: str) -> None:
        def req(cond: bool, msg: str) -> None:
            if not cond:
                raise ConfigError(f"{coin}: {msg}")

        req(self.base_order > 0, "base_order > 0 olmalı")
        req(self.safety_orders >= 0, "safety_orders >= 0 olmalı")
        req(self.safety_orders == 0 or self.safety_order > 0, "safety_order > 0 olmalı")
        req(self.safety_step_pct > 0, "safety_step_pct > 0 olmalı")
        req(self.take_profit_pct > 0, "take_profit_pct > 0 olmalı")
        req(0 <= self.trailing_pct < self.take_profit_pct + 50, "trailing_pct geçersiz")
        req(self.stop_loss_pct is None or 0 < self.stop_loss_pct < 100, "stop_loss_pct 0-100 arası olmalı")
        req(self.rsi_below is None or 0 < self.rsi_below < 100, "rsi_below 0-100 arası olmalı")
        req(self.rsi_period >= 2, "rsi_period >= 2 olmalı")
        req(self.trend_ema is None or self.trend_ema >= 2, "trend_ema >= 2 olmalı")


@dataclass(frozen=True)
class OrderConfig:
    slippage_pct: float = 0.3
    timeout_seconds: int = 30


@dataclass(frozen=True)
class Config:
    mode: str
    exchange: str
    quote: str
    total_budget: float
    fee_pct: float
    poll_seconds: int
    coins: list[str]
    strategies: dict[str, StrategyConfig]
    orders: OrderConfig = field(default_factory=OrderConfig)
    telegram: bool = False

    @property
    def fee(self) -> float:
        return self.fee_pct / 100

    def symbol(self, coin: str) -> str:
        return f"{coin}/{self.quote}"


def _pick(cls: type, raw: dict[str, Any], where: str) -> dict[str, Any]:
    allowed = {f.name for f in fields(cls)}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{where}: bilinmeyen alan(lar): {', '.join(sorted(unknown))}")
    return raw


def parse_config(raw: dict[str, Any]) -> Config:
    mode = raw.get("mode", "paper")
    if mode not in ("paper", "live"):
        raise ConfigError("mode 'paper' ya da 'live' olmalı")

    coins = [str(c).upper() for c in raw.get("coins") or []]
    if not coins:
        raise ConfigError("coins listesi boş")
    if len(set(coins)) != len(coins):
        raise ConfigError("coins listesinde tekrar eden coin var")

    base = StrategyConfig(**_pick(StrategyConfig, raw.get("strategy") or {}, "strategy"))
    overrides = {str(k).upper(): v for k, v in (raw.get("overrides") or {}).items()}
    stray = set(overrides) - set(coins)
    if stray:
        raise ConfigError(f"overrides içinde coins listesinde olmayan coin var: {', '.join(sorted(stray))}")

    strategies: dict[str, StrategyConfig] = {}
    for coin in coins:
        s = replace(base, **_pick(StrategyConfig, overrides.get(coin) or {}, f"overrides.{coin}"))
        s.validate(coin)
        strategies[coin] = s

    total_budget = float(raw.get("total_budget", 0))
    if total_budget <= 0:
        raise ConfigError("total_budget > 0 olmalı")
    planned = sum(s.max_spend() for s in strategies.values())
    if planned > total_budget + 1e-6:
        raise ConfigError(
            f"Tüm kademeler dolarsa {planned:,.0f} TL gerekir; total_budget ({total_budget:,.0f} TL) yetmiyor. "
            "base_order / safety_order değerlerini düşür ya da coin sayısını azalt."
        )

    fee_pct = float(raw.get("fee_pct", 0.2))
    if not 0 <= fee_pct < 5:
        raise ConfigError("fee_pct 0-5 arası olmalı")

    return Config(
        mode=mode,
        exchange=str(raw.get("exchange", "btcturk")),
        quote=str(raw.get("quote", "TRY")).upper(),
        total_budget=total_budget,
        fee_pct=fee_pct,
        poll_seconds=int(raw.get("poll_seconds", 60)),
        coins=coins,
        strategies=strategies,
        orders=OrderConfig(**_pick(OrderConfig, raw.get("orders") or {}, "orders")),
        telegram=bool(raw.get("telegram", False)),
    )


def load_config(path: str | Path = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as f:
        return parse_config(yaml.safe_load(f) or {})
