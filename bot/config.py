from __future__ import annotations

import math
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


TIMEFRAME_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def timeframe_seconds(tf: str) -> int:
    """'15m' -> 900, '4h' -> 14400."""
    try:
        return int(tf[:-1]) * TIMEFRAME_UNITS[tf[-1]]
    except (KeyError, ValueError, IndexError):
        raise ConfigError(f"geçersiz mum aralığı: {tf!r} (ör. 15m, 1h, 4h)") from None


@dataclass(frozen=True)
class EntryFilterConfig:
    """Çoklu zaman dilimi giriş filtresi. Yalnız yeni pozisyon açmayı (ilk alım) etkiler.

    Her kural ayrı açılıp kapatılır; hepsi kapalıyken bot eskisi gibi davranır.
    Yalnız kapanmış mumlar kullanılır.
    """

    rsi_period: int = 14
    # 1) Büyük resim: trend_tf mumlarında EMA(trend_fast_ema) > EMA(trend_slow_ema)
    trend: bool = False
    trend_tf: str = "4h"
    trend_fast_ema: int = 20
    trend_slow_ema: int = 50
    # 2) Onay: confirm_tf mumlarında fiyat > EMA(confirm_ema) ve RSI < confirm_rsi_below
    confirm: bool = False
    confirm_tf: str = "1h"
    confirm_ema: int = 50
    confirm_rsi_below: float = 50
    # 3) Tetik: trigger_tf'de RSI son trigger_lookback mum içinde trigger_rsi_below altına inmiş
    #    ve son mumun RSI'ı bir öncekinden yüksek (dipten dönüş)
    trigger: bool = False
    trigger_tf: str = "15m"
    trigger_rsi_below: float = 35
    trigger_lookback: int = 6

    @property
    def active(self) -> bool:
        return self.trend or self.confirm or self.trigger

    def candles_needed(self) -> dict[str, int]:
        """Her zaman dilimi için gereken kapanmış mum sayısı."""
        need: dict[str, int] = {}

        def add(tf: str, n: int) -> None:
            need[tf] = max(need.get(tf, 0), n)

        rsi_need = (self.rsi_period + 1) * 3
        if self.trend:
            add(self.trend_tf, self.trend_slow_ema * 3)
        if self.confirm:
            add(self.confirm_tf, max(self.confirm_ema * 3, rsi_need))
        if self.trigger:
            add(self.trigger_tf, rsi_need + self.trigger_lookback)
        return need

    def validate(self) -> None:
        for tf in (self.trend_tf, self.confirm_tf, self.trigger_tf):
            timeframe_seconds(tf)
        if self.rsi_period < 2:
            raise ConfigError("entry_filter.rsi_period >= 2 olmalı")
        if not 2 <= self.trend_fast_ema < self.trend_slow_ema:
            raise ConfigError("entry_filter: 2 <= trend_fast_ema < trend_slow_ema olmalı")
        if self.confirm_ema < 2:
            raise ConfigError("entry_filter.confirm_ema >= 2 olmalı")
        if not (0 < self.confirm_rsi_below < 100 and 0 < self.trigger_rsi_below < 100):
            raise ConfigError("entry_filter: RSI eşikleri 0-100 arası olmalı")
        if self.trigger_lookback < 2:
            raise ConfigError("entry_filter.trigger_lookback >= 2 olmalı")


@dataclass(frozen=True)
class ProtectionConfig:
    """Sert düşüş korumaları. Kar al, takip eden stop ve zarar durdur bunlardan hiç etkilenmez.

    Yüzdeler bütçeye (total_budget) oranlıdır; bütçe büyüyünce sınırlar da büyür.
    """

    # 4) BTC rejim filtresi: regime_tf mumlarında BTC < EMA(regime_ema) ya da 24 saatte
    #    regime_drop_24h_pct'ten fazla düşüş => piyasa riskli, yeni pozisyon açılmaz
    btc_regime: bool = False
    regime_coin: str = "BTC"
    regime_tf: str = "4h"
    regime_ema: int = 50
    regime_drop_24h_pct: float | None = 4
    # 5) Piyasa riskliyken ek alım (kademeli alım) da yapılmasın
    block_safety_when_risky: bool = False
    # 6) Toplam risk sınırı: açık pozisyonlara bağlı para bütçenin bu %'sini geçmesin (null = kapalı)
    max_invested_pct: float | None = None
    #    Tüm ek alımlarını yapmış (dolu) pozisyon sayısı en fazla bu kadar (null = kapalı)
    max_full_positions: int | None = None
    # 7) Devre kesici: toplam K/Z (gerçekleşen + anlık) circuit_window_hours içinde bütçenin
    #    bu %'sinden fazla düşerse circuit_pause_hours boyunca hiç alım yapılmaz (null = kapalı)
    circuit_breaker_pct: float | None = None
    circuit_window_hours: float = 24
    circuit_pause_hours: float = 24

    def validate(self) -> None:
        timeframe_seconds(self.regime_tf)
        if self.regime_ema < 2:
            raise ConfigError("protection.regime_ema >= 2 olmalı")
        for name in ("regime_drop_24h_pct", "max_invested_pct", "circuit_breaker_pct"):
            v = getattr(self, name)
            if v is not None and not 0 < v <= 100:
                raise ConfigError(f"protection.{name} 0-100 arası olmalı (ya da null)")
        if self.max_full_positions is not None and self.max_full_positions < 1:
            raise ConfigError("protection.max_full_positions >= 1 olmalı (ya da null)")
        if self.circuit_window_hours <= 0 or self.circuit_pause_hours <= 0:
            raise ConfigError("protection: circuit_window_hours ve circuit_pause_hours > 0 olmalı")


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
    entry_filter: EntryFilterConfig = field(default_factory=EntryFilterConfig)
    protection: ProtectionConfig = field(default_factory=ProtectionConfig)
    max_open_auto: bool = False  # max_open_positions: auto ile mi hesaplandı

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

    entry_filter = EntryFilterConfig(**_pick(EntryFilterConfig, raw.get("entry_filter") or {}, "entry_filter"))
    entry_filter.validate()
    protection = ProtectionConfig(**_pick(ProtectionConfig, raw.get("protection") or {}, "protection"))
    protection.validate()

    default_open = len(coins) if selection.mode == "fixed" else selection.max_coins
    raw_open = raw.get("max_open_positions")
    auto_open = isinstance(raw_open, str) and raw_open.strip().lower() == "auto"
    try:
        max_open = default_open if auto_open else int(raw_open or default_open)
    except (TypeError, ValueError):
        raise ConfigError("max_open_positions bir sayı ya da 'auto' olmalı") from None
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
        entry_filter=entry_filter,
        protection=protection,
        max_open_auto=auto_open,
    )
    if auto_open:
        max_open = auto_max_open_positions(cfg)
        if max_open < 1:
            raise ConfigError(
                f"max_open_positions: auto — bütçe ({total_budget:,.0f} TL) tek bir coinin en fazla "
                f"harcamasına ({biggest_max_spend(cfg):,.0f} TL) bile yetmiyor."
            )
        cfg = replace(cfg, max_open_positions=max_open)
    planned = planned_spend(cfg)
    if planned > total_budget + 1e-6:
        raise ConfigError(
            f"Açık pozisyonların tüm kademeleri dolarsa {planned:,.0f} TL gerekir; total_budget "
            f"({total_budget:,.0f} TL) yetmiyor. base_order / safety_order ya da max_open_positions değerini düşür."
        )
    return cfg


def biggest_max_spend(cfg: Config) -> float:
    """Coin başı en fazla harcamanın en büyüğü (tüm kademeler dolarsa)."""
    if cfg.scanning:
        return max([cfg.default_strategy.max_spend()] + [s.max_spend() for s in cfg.overrides.values()])
    return max(cfg.strategy_for(c).max_spend() for c in cfg.coins)


def auto_max_open_positions(cfg: Config) -> int:
    """max_open_positions: auto => floor(total_budget / en büyük coin başı max_spend)."""
    return math.floor(cfg.total_budget / biggest_max_spend(cfg) + 1e-9)


def planned_spend(cfg: Config) -> float:
    """Açık pozisyon sınırı dolup tüm kademeler alınırsa kullanılacak en fazla TL."""
    if cfg.scanning:
        return biggest_max_spend(cfg) * cfg.max_open_positions
    spends = sorted((cfg.strategy_for(c).max_spend() for c in cfg.coins), reverse=True)
    return sum(spends[: cfg.max_open_positions])


def load_config(path: str | Path = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as f:
        return parse_config(yaml.safe_load(f) or {})
