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


STABLECOINS = ("USDT", "USDC", "DAI", "FDUSD", "TUSD", "BUSD", "USDP", "PYUSD", "EUR", "GBP", "TRY")


@dataclass(frozen=True)
class SelectionConfig:
    """Hangi coinlerde işlem yapılacağı.

    fixed: yalnız `coins` listesi.
    scan:  `coins` her zaman izlenir; bot ayrıca tüm TL paritelerini tarar ve
           en iyi puanlıları ekler (toplam `max_coins` olana kadar).
    """

    mode: str = "fixed"
    max_coins: int = 12
    rescan_hours: float = 24
    min_volume_try: float = 20_000_000
    max_spread_pct: float = 0.3
    min_age_days: int = 30
    min_volatility_pct: float = 1.5
    max_volatility_pct: float = 12
    require_trend: bool = True
    blacklist: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.mode not in ("fixed", "scan"):
            raise ConfigError("selection.mode 'fixed' ya da 'scan' olmalı")
        if self.max_coins < 1:
            raise ConfigError("selection.max_coins >= 1 olmalı")
        if not 0 <= self.min_volatility_pct < self.max_volatility_pct:
            raise ConfigError("selection: min_volatility_pct < max_volatility_pct olmalı")
        if self.min_age_days < 1 or self.rescan_hours <= 0:
            raise ConfigError("selection: min_age_days >= 1 ve rescan_hours > 0 olmalı")


@dataclass(frozen=True)
class Config:
    mode: str
    exchange: str
    quote: str
    total_budget: float
    fee_pct: float
    poll_seconds: int
    coins: list[str]                      # sabit liste (scan modunda: hep izlenenler)
    default_strategy: StrategyConfig
    overrides: dict[str, StrategyConfig]  # coin bazında strateji
    max_open_positions: int
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    orders: OrderConfig = field(default_factory=OrderConfig)
    telegram: bool = False

    @property
    def fee(self) -> float:
        return self.fee_pct / 100

    @property
    def scanning(self) -> bool:
        return self.selection.mode == "scan"

    def symbol(self, coin: str) -> str:
        return f"{coin}/{self.quote}"

    def strategy_for(self, coin: str) -> StrategyConfig:
        return self.overrides.get(coin, self.default_strategy)

    def excluded(self, coin: str) -> bool:
        return coin in self.selection.blacklist or coin in STABLECOINS


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

    sel_raw = dict(_pick(SelectionConfig, raw.get("selection") or {}, "selection"))
    sel_raw["blacklist"] = tuple(str(c).upper() for c in sel_raw.get("blacklist") or ())
    selection = SelectionConfig(**sel_raw)
    selection.validate()

    coins = [str(c).upper() for c in raw.get("coins") or []]
    if not coins and selection.mode == "fixed":
        raise ConfigError("coins listesi boş")
    if len(set(coins)) != len(coins):
        raise ConfigError("coins listesinde tekrar eden coin var")
    if selection.mode == "scan" and len(coins) > selection.max_coins:
        raise ConfigError("coins listesi selection.max_coins'ten uzun olamaz")

    base = StrategyConfig(**_pick(StrategyConfig, raw.get("strategy") or {}, "strategy"))
    base.validate("strategy")
    overrides: dict[str, StrategyConfig] = {}
    for k, v in (raw.get("overrides") or {}).items():
        coin = str(k).upper()
        s = replace(base, **_pick(StrategyConfig, v or {}, f"overrides.{coin}"))
        s.validate(coin)
        overrides[coin] = s
    if selection.mode == "fixed":
        stray = set(overrides) - set(coins)
        if stray:
            raise ConfigError(f"overrides içinde coins listesinde olmayan coin var: {', '.join(sorted(stray))}")

    total_budget = float(raw.get("total_budget", 0))
    if total_budget <= 0:
        raise ConfigError("total_budget > 0 olmalı")

    default_open = len(coins) if selection.mode == "fixed" else selection.max_coins
    max_open = int(raw.get("max_open_positions") or default_open)
    if max_open < 1:
        raise ConfigError("max_open_positions >= 1 olmalı")

    fee_pct = float(raw.get("fee_pct", 0.2))
    if not 0 <= fee_pct < 5:
        raise ConfigError("fee_pct 0-5 arası olmalı")

    cfg = Config(
        mode=mode,
        exchange=str(raw.get("exchange", "btcturk")),
        quote=str(raw.get("quote", "TRY")).upper(),
        total_budget=total_budget,
        fee_pct=fee_pct,
        poll_seconds=int(raw.get("poll_seconds", 60)),
        coins=coins,
        default_strategy=base,
        overrides=overrides,
        max_open_positions=max_open,
        selection=selection,
        orders=OrderConfig(**_pick(OrderConfig, raw.get("orders") or {}, "orders")),
        telegram=bool(raw.get("telegram", False)),
    )
    planned = planned_spend(cfg)
    if planned > total_budget + 1e-6:
        raise ConfigError(
            f"Açık pozisyonların tüm kademeleri dolarsa {planned:,.0f} TL gerekir; total_budget "
            f"({total_budget:,.0f} TL) yetmiyor. base_order / safety_order ya da max_open_positions değerini düşür."
        )
    return cfg


def planned_spend(cfg: Config) -> float:
    """Açık pozisyon sınırı dolup tüm kademeler alınırsa kullanılacak en fazla TL."""
    if cfg.scanning:
        biggest = max([cfg.default_strategy.max_spend()] + [s.max_spend() for s in cfg.overrides.values()])
        return biggest * cfg.max_open_positions
    spends = sorted((cfg.strategy_for(c).max_spend() for c in cfg.coins), reverse=True)
    return sum(spends[: cfg.max_open_positions])


def load_config(path: str | Path = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as f:
        return parse_config(yaml.safe_load(f) or {})
