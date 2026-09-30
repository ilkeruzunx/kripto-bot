"""Kademeli alım + kar al + zarar durdur stratejisi.

Borsadan bağımsızdır: fiyat ve mum kapanışlarını alır, ne yapılacağını söyler.
Canlı bot, sanal bot ve geçmiş test aynı kodu kullanır.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum

from .config import StrategyConfig
from .indicators import ema, rsi


@dataclass
class Position:
    qty: float = 0.0            # eldeki coin miktarı
    cost: float = 0.0           # harcanan toplam TL (komisyon dahil)
    buys: int = 0               # ilk alım dahil kaç alım yapıldı
    last_buy_price: float = 0.0
    opened_at: float = 0.0
    trailing_peak: float | None = None  # kar hedefi aşıldıktan sonra görülen en yüksek fiyat

    @property
    def avg_cost(self) -> float:
        return self.cost / self.qty if self.qty else 0.0

    def add_buy(self, qty: float, cost: float, price: float, ts: float) -> None:
        if self.buys == 0:
            self.opened_at = ts
        self.qty += qty
        self.cost += cost
        self.buys += 1
        self.last_buy_price = price


@dataclass
class CoinState:
    position: Position | None = None
    cooldown_until: float = 0.0
    realized_pnl: float = 0.0
    closed_trades: int = 0
    wins: int = 0
    # Bot bir emrin sonucundan emin olamazsa coin'i durdurur; elle kontrol gerekir.
    blocked: str | None = None
    pending_order: dict | None = None
    history: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CoinState":
        d = dict(d)
        pos = d.pop("position", None)
        return cls(position=Position(**pos) if pos else None, **d)


class Action(str, Enum):
    HOLD = "HOLD"
    BUY_BASE = "BUY_BASE"
    BUY_SAFETY = "BUY_SAFETY"
    SELL_TP = "SELL_TP"
    SELL_SL = "SELL_SL"


@dataclass
class Decision:
    action: Action
    amount_try: float = 0.0  # alımlarda harcanacak TL
    reason: str = ""


class Strategy:
    def __init__(self, cfg: StrategyConfig, fee: float):
        self.cfg = cfg
        self.fee = fee

    def target_price(self, pos: Position) -> float:
        """Satış komisyonu düştükten sonra net take_profit_pct kar bırakan fiyat."""
        return pos.avg_cost * (1 + self.cfg.take_profit_pct / 100) / (1 - self.fee)

    def stop_price(self, pos: Position) -> float | None:
        if self.cfg.stop_loss_pct is None:
            return None
        return pos.avg_cost * (1 - self.cfg.stop_loss_pct / 100)

    def next_safety_price(self, pos: Position) -> float | None:
        idx = pos.buys - 1
        if idx >= self.cfg.safety_orders:
            return None
        return pos.last_buy_price * (1 - self.cfg.safety_drop_pct(idx) / 100)

    def entry_signal(self, closes: Sequence[float]) -> tuple[bool, str]:
        c = self.cfg
        if len(closes) < c.candles_needed():
            return False, f"yetersiz mum ({len(closes)}/{c.candles_needed()})"
        parts = []
        if c.trend_ema is not None:
            e = ema(closes, c.trend_ema)
            if e is None or closes[-1] <= e:
                return False, f"trend yok (fiyat {closes[-1]:.6g} <= EMA{c.trend_ema} {e or 0:.6g})"
            parts.append(f"fiyat>EMA{c.trend_ema}")
        if c.rsi_below is not None:
            r = rsi(closes, c.rsi_period)
            if r is None or r >= c.rsi_below:
                return False, f"RSI {r or 0:.1f} >= {c.rsi_below}"
            parts.append(f"RSI {r:.1f}<{c.rsi_below}")
        return True, ", ".join(parts) or "koşulsuz giriş"

    def decide(self, state: CoinState, price: float, closes: Sequence[float], now: float) -> Decision:
        """Ne yapılacağına karar verir. Takip eden stop için position.trailing_peak'i günceller."""
        if state.blocked:
            return Decision(Action.HOLD, reason=f"durduruldu: {state.blocked}")

        pos = state.position
        if pos is None or pos.qty <= 0:
            if now < state.cooldown_until:
                return Decision(Action.HOLD, reason="bekleme süresinde")
            ok, why = self.entry_signal(closes)
            if ok:
                return Decision(Action.BUY_BASE, self.cfg.base_order, why)
            return Decision(Action.HOLD, reason=why)

        stop = self.stop_price(pos)
        if stop is not None and price <= stop:
            return Decision(Action.SELL_SL, reason=f"fiyat {price:.6g} <= stop {stop:.6g}")

        target = self.target_price(pos)
        if pos.trailing_peak is not None or price >= target:
            if self.cfg.trailing_pct <= 0:
                return Decision(Action.SELL_TP, reason=f"hedef {target:.6g} aşıldı")
            pos.trailing_peak = max(pos.trailing_peak or 0.0, price)
            trigger = max(target, pos.trailing_peak * (1 - self.cfg.trailing_pct / 100))
            if price <= trigger:
                return Decision(
                    Action.SELL_TP,
                    reason=f"takip stop: zirve {pos.trailing_peak:.6g}, fiyat {price:.6g} <= {trigger:.6g}",
                )
            return Decision(Action.HOLD, reason=f"kar takibi (zirve {pos.trailing_peak:.6g})")

        nxt = self.next_safety_price(pos)
        if nxt is not None and price <= nxt:
            size = self.cfg.safety_sizes()[pos.buys - 1]
            return Decision(Action.BUY_SAFETY, size, f"ek alım #{pos.buys}: fiyat {price:.6g} <= {nxt:.6g}")

        return Decision(Action.HOLD, reason=f"pozisyonda (hedef {target:.6g})")

    def cooldown_for(self, action: Action) -> float:
        minutes = self.cfg.cooldown_after_sl_min if action == Action.SELL_SL else self.cfg.cooldown_after_tp_min
        return minutes * 60
