"""Ana döngü: fiyatları okur, stratejiye sorar, emirleri uygular, durumu kaydeder."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

import ccxt

from .config import Config
from .exchange import AmbiguousOrderError, Broker, MarketData, new_client_id
from .notify import Notifier
from .state import StateStore
from .strategy import Action, CoinState, Decision, Position, Strategy

log = logging.getLogger(__name__)

CANDLE_REFRESH_SECONDS = 300
HISTORY_LIMIT = 200


class Engine:
    def __init__(
        self,
        cfg: Config,
        market: MarketData,
        broker: Broker,
        store: StateStore,
        notifier: Notifier,
        clock: Callable[[], float] = time.time,
    ):
        self.cfg = cfg
        self.market = market
        self.broker = broker
        self.store = store
        self.notifier = notifier
        self.clock = clock
        self.strategies = {c: Strategy(cfg.strategies[c], cfg.fee) for c in cfg.coins}
        self.states: dict[str, CoinState] = store.load()
        self._candles: dict[str, tuple[float, list[float]]] = {}

        for coin in cfg.coins:
            self.states.setdefault(coin, CoinState())
        for coin, st in self.states.items():
            if st.pending_order and not st.blocked:
                st.blocked = (
                    "önceki çalışmada sonucu belirsiz kalan bir emir var "
                    f"({st.pending_order.get('side')}, {st.pending_order.get('client_id')}). "
                    f"BtcTurk'te kontrol et, sonra: python -m bot unblock {coin}"
                )
                self.notifier.send(f"⚠️ {coin} durduruldu: {st.blocked}")
        self.store.save(self.states)

    # --- yardımcılar ---

    def invested(self) -> float:
        return sum(s.position.cost for s in self.states.values() if s.position)

    def _closes(self, coin: str) -> list[float]:
        now = self.clock()
        cached = self._candles.get(coin)
        if cached and now - cached[0] < CANDLE_REFRESH_SECONDS:
            return cached[1]
        s = self.cfg.strategies[coin]
        closes = self.market.closes(self.cfg.symbol(coin), s.timeframe, s.candles_needed())
        self._candles[coin] = (now, closes)
        return closes

    def _record(self, st: CoinState, **event) -> None:
        st.history.append({"ts": self.clock(), **event})
        del st.history[:-HISTORY_LIMIT]

    # --- döngü ---

    def tick(self) -> None:
        symbols = [self.cfg.symbol(c) for c in self.cfg.coins]
        prices = self.market.prices(symbols)
        for coin in self.cfg.coins:
            price = prices.get(self.cfg.symbol(coin))
            if not price:
                log.debug("%s fiyatı yok, atlanıyor", coin)
                continue
            try:
                self._tick_coin(coin, price)
            except ccxt.NetworkError as e:
                log.warning("%s: ağ hatası, sonraki turda tekrar denenecek: %s", coin, e)
            finally:
                self.store.save(self.states)

    def _tick_coin(self, coin: str, price: float) -> None:
        st = self.states[coin]
        needs_candles = st.position is None and not st.blocked and self.clock() >= st.cooldown_until
        closes = self._closes(coin) if needs_candles else []
        decision = self.strategies[coin].decide(st, price, closes, self.clock())
        log.debug("%s %.6g -> %s (%s)", coin, price, decision.action.value, decision.reason)
        if decision.action in (Action.BUY_BASE, Action.BUY_SAFETY):
            self._buy(coin, st, price, decision)
        elif decision.action in (Action.SELL_TP, Action.SELL_SL):
            self._sell(coin, st, price, decision)

    def _buy(self, coin: str, st: CoinState, price: float, d: Decision) -> None:
        symbol = self.cfg.symbol(coin)
        amount = d.amount_try
        if self.invested() + amount > self.cfg.total_budget + 1e-6:
            log.warning("%s: bütçe sınırı (%.0f TL) aşılacağı için alım yapılmadı", coin, self.cfg.total_budget)
            return
        min_cost = self.market.min_cost(symbol)
        if amount < min_cost:
            log.warning("%s: %.2f TL, borsanın en düşük emir tutarının (%.2f) altında", coin, amount, min_cost)
            return
        if self.broker.quote_balance() + 0.01 < amount:
            log.warning("%s: yetersiz %s bakiyesi, alım yapılmadı", coin, self.cfg.quote)
            return

        fill = self._send(coin, st, "buy", lambda cid: self.broker.buy(symbol, amount, price, cid))
        if fill is None or fill.empty:
            return
        if st.position is None:
            st.position = Position()
        st.position.add_buy(fill.qty, fill.quote, fill.price, self.clock())
        self._record(st, side="buy", kind=d.action.value, qty=fill.qty, quote=fill.quote, price=fill.price)
        label = "İlk alım" if d.action == Action.BUY_BASE else f"Ek alım #{st.position.buys - 1}"
        self.notifier.send(
            f"🟢 {coin} {label}: {fill.qty:.8g} @ {fill.price:.6g} = {fill.quote:,.2f} TL\n"
            f"Ortalama maliyet: {st.position.avg_cost:.6g} | Sebep: {d.reason}"
        )

    def _sell(self, coin: str, st: CoinState, price: float, d: Decision) -> None:
        symbol = self.cfg.symbol(coin)
        pos = st.position
        assert pos is not None
        fill = self._send(coin, st, "sell", lambda cid: self.broker.sell(symbol, pos.qty, price, cid))
        if fill is None or fill.empty:
            return

        sold = min(fill.qty, pos.qty)
        cost_part = pos.cost * sold / pos.qty
        pnl = fill.quote - cost_part
        pos.qty -= sold
        pos.cost -= cost_part
        st.realized_pnl += pnl
        self._record(st, side="sell", kind=d.action.value, qty=sold, quote=fill.quote, price=fill.price, pnl=pnl)

        remaining_value = pos.qty * price
        dust = remaining_value < max(self.market.min_cost(symbol), 1.0)
        if dust:
            st.closed_trades += 1
            st.wins += int(pnl > 0)
            st.position = None
            st.cooldown_until = self.clock() + self.strategies[coin].cooldown_for(d.action)
        icon = "✅" if d.action == Action.SELL_TP else "🛑"
        status = "Pozisyon kapandı." if dust else f"Kısmi satış, kalan {pos.qty:.8g}."
        self.notifier.send(
            f"{icon} {coin} satış: {sold:.8g} @ {fill.price:.6g} = {fill.quote:,.2f} TL\n"
            f"Kar/zarar: {pnl:+,.2f} TL | {status} | Sebep: {d.reason}"
        )

    def _send(self, coin: str, st: CoinState, side: str, place: Callable[[str], object]):
        """Emri 'bekleyen' olarak kaydedip gönderir; sonuç belirsizse coin'i durdurur."""
        cid = new_client_id()
        st.pending_order = {"side": side, "client_id": cid, "ts": self.clock()}
        self.store.save(self.states)
        try:
            fill = place(cid)
        except AmbiguousOrderError as e:
            st.blocked = f"{e}. BtcTurk'te kontrol et, sonra: python -m bot unblock {coin}"
            self.notifier.send(f"⚠️ {coin} durduruldu: {st.blocked}")
            return None
        except ccxt.BaseError as e:
            st.pending_order = None
            log.warning("%s %s emri reddedildi: %s", coin, side, e)
            return None
        st.pending_order = None
        return fill

    def run_forever(self) -> None:
        self.notifier.send(
            f"🤖 Bot başladı ({self.cfg.mode}) — {len(self.cfg.coins)} coin, bütçe {self.cfg.total_budget:,.0f} TL"
        )
        while True:
            try:
                self.tick()
            except ccxt.NetworkError as e:
                log.warning("Ağ hatası: %s", e)
            except Exception:
                log.exception("Beklenmeyen hata")
            time.sleep(self.cfg.poll_seconds)
