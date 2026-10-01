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
SCAN_RETRY_SECONDS = 1800


class Engine:
    def __init__(
        self,
        cfg: Config,
        market: MarketData,
        broker: Broker,
        store: StateStore,
        notifier: Notifier,
        clock: Callable[[], float] = time.time,
        scanner: Callable[[], list[str]] | None = None,
    ):
        self.cfg = cfg
        self.market = market
        self.broker = broker
        self.store = store
        self.notifier = notifier
        self.clock = clock
        self.scanner = scanner
        self.watchlist: list[str] = list(cfg.coins)
        self._next_scan = 0.0
        self._strategies: dict[str, Strategy] = {}
        self.states: dict[str, CoinState] = store.load()
        self._candles: dict[str, tuple[float, list[float]]] = {}
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

    def strategy(self, coin: str) -> Strategy:
        if coin not in self._strategies:
            self._strategies[coin] = Strategy(self.cfg.strategy_for(coin), self.cfg.fee)
        return self._strategies[coin]

    def invested(self) -> float:
        return sum(s.position.cost for s in self.states.values() if s.position)

    def open_positions(self) -> int:
        return sum(1 for s in self.states.values() if s.position)

    def active_coins(self) -> list[str]:
        """İzleme listesi + listeden düşmüş ama hâlâ pozisyonu ya da sorunu olan coinler."""
        held = [c for c, s in self.states.items() if s.position or s.blocked or s.pending_order]
        return list(dict.fromkeys(self.watchlist + held))

    def refresh_watchlist(self) -> None:
        now = self.clock()
        if self.scanner is None or now < self._next_scan:
            return
        try:
            new = self.scanner()
        except Exception as e:
            log.warning("Tarama başarısız, eski liste kullanılıyor: %s", e)
            self._next_scan = now + SCAN_RETRY_SECONDS
            return
        self._next_scan = now + self.cfg.selection.rescan_hours * 3600
        added = [c for c in new if c not in self.watchlist]
        removed = [c for c in self.watchlist if c not in new]
        self.watchlist = new
        if added or removed:
            msg = f"🔎 İzleme listesi: {', '.join(new)}"
            if added:
                msg += f"\nEklenen: {', '.join(added)}"
            if removed:
                msg += f"\nÇıkan: {', '.join(removed)} (açık pozisyon varsa kendi kuralıyla kapanır)"
            self.notifier.send(msg)

    def _closes(self, coin: str) -> list[float]:
        now = self.clock()
        cached = self._candles.get(coin)
        if cached and now - cached[0] < CANDLE_REFRESH_SECONDS:
            return cached[1]
        s = self.cfg.strategy_for(coin)
        closes = self.market.closes(self.cfg.symbol(coin), s.timeframe, s.candles_needed())
        self._candles[coin] = (now, closes)
        return closes

    def _record(self, st: CoinState, **event) -> None:
        st.history.append({"ts": self.clock(), **event})
        del st.history[:-HISTORY_LIMIT]

    # --- döngü ---

    def tick(self) -> None:
        self.refresh_watchlist()
        coins = self.active_coins()
        prices = self.market.prices([self.cfg.symbol(c) for c in coins])
        for coin in coins:
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
        st = self.states.setdefault(coin, CoinState())
        if st.position is None and (coin not in self.watchlist or self.open_positions() >= self.cfg.max_open_positions):
            return  # yeni pozisyon açılmayacak
        needs_candles = st.position is None and not st.blocked and self.clock() >= st.cooldown_until
        closes = self._closes(coin) if needs_candles else []
        decision = self.strategy(coin).decide(st, price, closes, self.clock())
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
            st.cooldown_until = self.clock() + self.strategy(coin).cooldown_for(d.action)
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
            f"🤖 Bot başladı ({self.cfg.mode}, seçim: {self.cfg.selection.mode}) — en fazla "
            f"{self.cfg.max_open_positions} açık pozisyon, bütçe {self.cfg.total_budget:,.0f} TL"
        )
        while True:
            try:
                self.tick()
            except ccxt.NetworkError as e:
                log.warning("Ağ hatası: %s", e)
            except Exception:
                log.exception("Beklenmeyen hata")
            time.sleep(self.cfg.poll_seconds)
