"""Ana döngü: fiyatları okur, stratejiye sorar, emirleri uygular, durumu kaydeder."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime

import ccxt

from .config import Config, biggest_max_spend, planned_spend, timeframe_seconds
from .exchange import AmbiguousOrderError, Broker, MarketData, new_client_id
from .indicators import ema
from .notify import Notifier
from .state import StateStore
from .strategy import Action, CoinState, Decision, Position, Strategy, mark

log = logging.getLogger(__name__)

CANDLE_REFRESH_SECONDS = 300
CANDLE_RETRY_SECONDS = 30
HISTORY_LIMIT = 200
SCAN_RETRY_SECONDS = 1800
PNL_SNAPSHOT_SECONDS = 300

# Piyasa durumu (panelde ve 'status' çıktısında gösterilir)
MARKET_NORMAL = "normal"
MARKET_RISKY = "riskli"
MARKET_BREAKER = "devre_kesici"


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
        self.meta: dict = store.load_meta()
        self._candles: dict[str, tuple[float, list[float]]] = {}
        # Zaman dilimi başına ayrı önbellek: (sembol, tf) -> (geçerlilik sonu ms, mum sayısı, kapanmış mumlar)
        self._tf_candles: dict[tuple[str, str], tuple[float, int, list[list[float]]]] = {}
        self._last_prices: dict[str, float] = {}
        for coin, st in self.states.items():
            if st.pending_order and not st.blocked:
                st.blocked = (
                    "önceki çalışmada sonucu belirsiz kalan bir emir var "
                    f"({st.pending_order.get('side')}, {st.pending_order.get('client_id')}). "
                    f"BtcTurk'te kontrol et, sonra: python -m bot unblock {coin}"
                )
                self.notifier.send(f"⚠️ {coin} durduruldu: {st.blocked}")
        self._save()

    # --- yardımcılar ---

    def _save(self) -> None:
        self.store.save(self.states, self.meta)

    def strategy(self, coin: str) -> Strategy:
        if coin not in self._strategies:
            self._strategies[coin] = Strategy(self.cfg.strategy_for(coin), self.cfg.fee, self.cfg.entry_filter)
        return self._strategies[coin]

    def invested(self) -> float:
        return sum(s.position.cost for s in self.states.values() if s.position)

    def open_positions(self) -> int:
        return sum(1 for s in self.states.values() if s.position)

    def is_full(self, coin: str, st: CoinState) -> bool:
        """Pozisyon tüm ek alımlarını yapmış mı?"""
        return st.position is not None and st.position.buys >= self.cfg.strategy_for(coin).safety_orders + 1

    def total_pnl(self) -> float:
        """Gerçekleşen + anlık (satış komisyonu düşülmüş) toplam K/Z. Fiyatı bilinmeyen pozisyon 0 sayılır."""
        pnl = sum(s.realized_pnl for s in self.states.values())
        for coin, s in self.states.items():
            price = self._last_prices.get(self.cfg.symbol(coin))
            if s.position and price:
                pnl += s.position.qty * price * (1 - self.cfg.fee) - s.position.cost
        return pnl

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

    def _closed_candles(self, symbol: str, tf: str, n: int) -> list[list[float]]:
        """Son n KAPANMIŞ mum (oluşmakta olan mum atılır; ileriyi görme yok).

        Her (sembol, zaman dilimi) ayrı önbelleklenir; yeni bir mum kapanana kadar tekrar çekilmez.
        """
        now_ms = self.clock() * 1000
        tf_ms = timeframe_seconds(tf) * 1000
        key = (symbol, tf)
        cached = self._tf_candles.get(key)
        if cached and cached[1] >= n and now_ms < cached[0]:
            return cached[2][-n:]
        raw = self.market.ohlcv(symbol, tf, n + 1)
        candles = [c for c in raw if c[0] + tf_ms <= now_ms][-n:]
        valid_until = candles[-1][0] + 2 * tf_ms if candles else now_ms + tf_ms
        if valid_until <= now_ms:  # borsa yeni mumu henüz vermedi
            valid_until = now_ms + CANDLE_RETRY_SECONDS * 1000
        self._tf_candles[key] = (valid_until, n, candles)
        return candles

    def _filter_closes(self, coin: str) -> dict[str, list[float]]:
        symbol = self.cfg.symbol(coin)
        return {
            tf: [float(c[4]) for c in self._closed_candles(symbol, tf, n)]
            for tf, n in self.cfg.entry_filter.candles_needed().items()
        }

    # --- piyasa durumu / korumalar ---

    @property
    def market_state(self) -> dict:
        return self.meta.get("market") or {"state": MARKET_NORMAL, "reason": ""}

    def update_market_state(self, prices: dict[str, float]) -> None:
        """BTC rejimini ve devre kesiciyi değerlendirir, sonucu meta['market']'e yazar."""
        now = self.clock()
        p = self.cfg.protection
        risky, fine = self._regime_checks(prices) if p.btc_regime else ([], [])
        self._update_breaker(now)
        until = (self.meta.get("breaker_until") or 0) if p.circuit_breaker_pct is not None else 0
        if now < until:
            state = MARKET_BREAKER
            reason = (f"{self.meta.get('breaker_reason', '')}; "
                      f"{datetime.fromtimestamp(until):%d.%m %H:%M}'e kadar alım yok")
            if risky:
                reason += " | ayrıca riskli: " + "; ".join(risky)
        elif risky:
            state, reason = MARKET_RISKY, "; ".join(risky)
        else:
            state = MARKET_NORMAL
            reason = "; ".join(fine) or ("korumalar kapalı" if not self._protections_on() else "")
        prev = self.market_state.get("state")
        self.meta["market"] = {"state": state, "reason": reason, "until": until if state == MARKET_BREAKER else None,
                               "ts": now}
        if prev != state:
            log.info("Piyasa durumu: %s -> %s (%s)", prev, state, reason)

    def _protections_on(self) -> bool:
        p = self.cfg.protection
        return p.btc_regime or p.max_invested_pct is not None or p.max_full_positions is not None \
            or p.circuit_breaker_pct is not None

    def _regime_checks(self, prices: dict[str, float]) -> tuple[list[str], list[str]]:
        """BTC rejimi: (riskli sebepleri, geçen kontroller)."""
        p = self.cfg.protection
        coin, sym = p.regime_coin, self.cfg.symbol(p.regime_coin)
        price = prices.get(sym)
        if not price:
            return [f"{coin} fiyatı alınamadı"], []
        risky: list[str] = []
        fine: list[str] = []
        try:
            closes = [float(c[4]) for c in self._closed_candles(sym, p.regime_tf, p.regime_ema * 3)]
            e = ema(closes, p.regime_ema)
            if e is None:
                risky.append(f"{coin} {p.regime_tf} verisi yetersiz")
            elif price < e:
                risky.append(f"{coin} {price:,.6g} < {p.regime_tf} EMA{p.regime_ema} {e:,.6g}")
            else:
                fine.append(f"{coin} {price:,.6g} >= {p.regime_tf} EMA{p.regime_ema} {e:,.6g}")
            if p.regime_drop_24h_pct is not None:
                ref = self._price_hours_ago(sym, 24)
                if ref is None:
                    risky.append(f"{coin} 24 saatlik değişim hesaplanamadı")
                else:
                    change = (price / ref - 1) * 100
                    text = f"{coin} 24 saatte %{change:+.1f} (sınır -%{p.regime_drop_24h_pct:g})"
                    (risky if change < -p.regime_drop_24h_pct else fine).append(text)
        except ccxt.BaseError as e:
            risky.append(f"{coin} mum verisi alınamadı: {e}")
        return risky, fine

    def _price_hours_ago(self, symbol: str, hours: int) -> float | None:
        """En geç (şimdi - hours) saatinde kapanmış son 1 saatlik mumun kapanışı."""
        cutoff_ms = (self.clock() - hours * 3600) * 1000
        candles = self._closed_candles(symbol, "1h", hours + 6)
        older = [c for c in candles if c[0] + 3_600_000 <= cutoff_ms]
        return float(older[-1][4]) if older else None

    def _update_breaker(self, now: float) -> None:
        p = self.cfg.protection
        if p.circuit_breaker_pct is None:
            return
        until = self.meta.get("breaker_until") or 0
        if until and now >= until:
            self.meta["breaker_until"] = until = 0
            self.notifier.send("🟢 Devre kesici süresi doldu; alımlar yeniden serbest.")
        pnl = self.total_pnl()
        hist = [h for h in self.meta.get("pnl_history", []) if h[0] >= now - p.circuit_window_hours * 3600]
        if not until:
            peak = max([h[1] for h in hist], default=pnl)
            limit = self.cfg.total_budget * p.circuit_breaker_pct / 100
            if peak - pnl > limit:
                until = now + p.circuit_pause_hours * 3600
                self.meta["breaker_until"] = until
                self.meta["breaker_reason"] = (
                    f"toplam K/Z son {p.circuit_window_hours:g} saatte {peak:+,.0f} → {pnl:+,.0f} TL "
                    f"({pnl - peak:+,.0f} TL; sınır bütçenin %{p.circuit_breaker_pct:g}'ü = {limit:,.0f} TL)"
                )
                hist = []
                self.notifier.send(
                    f"⛔ Devre kesici devrede: {self.meta['breaker_reason']}.\n"
                    f"{p.circuit_pause_hours:g} saat yeni alım yapılmayacak. Açık pozisyonlar satılmıyor; "
                    "kar al, takip eden stop ve zarar durdur çalışmaya devam ediyor."
                )
        if not hist or now - hist[-1][0] >= PNL_SNAPSHOT_SECONDS:
            hist.append([now, round(pnl, 2)])
        self.meta["pnl_history"] = hist

    def entry_block(self) -> str | None:
        """Yeni pozisyon açmayı tamamen engelleyen piyasa durumu varsa sebebi."""
        m = self.market_state
        if m["state"] == MARKET_BREAKER:
            return mark(False, f"devre kesici aktif: {m['reason']}")
        if m["state"] == MARKET_RISKY:
            return mark(False, f"piyasa riskli: {m['reason']}")
        return None

    def guard_checks(self, coin: str, st: CoinState, d: Decision) -> list[tuple[bool, str]]:
        """Bir alımın korumalara takılıp takılmadığı: [(geçti_mi, açıklama), ...] (kapalı kurallar listede yok)."""
        p = self.cfg.protection
        checks: list[tuple[bool, str]] = []
        m = self.market_state
        if m["state"] == MARKET_BREAKER:
            checks.append((False, f"devre kesici aktif: {m['reason']}"))
        if p.btc_regime and (d.action == Action.BUY_BASE or p.block_safety_when_risky):
            risky = m["state"] == MARKET_RISKY
            checks.append((not risky, f"piyasa {'riskli' if risky else 'normal'}: {m['reason']}"))
        if p.max_invested_pct is not None:
            limit = self.cfg.total_budget * p.max_invested_pct / 100
            after = self.invested() + d.amount_try
            checks.append((after <= limit + 1e-6, (
                f"risk sınırı: bağlı para {after:,.0f} {'<=' if after <= limit + 1e-6 else '>'} "
                f"{limit:,.0f} TL (bütçenin %{p.max_invested_pct:g}'i)"
            )))
        if p.max_full_positions is not None:
            buys_after = (st.position.buys if st.position else 0) + 1
            if buys_after >= self.cfg.strategy_for(coin).safety_orders + 1:
                full = sum(1 for c, s in self.states.items() if c != coin and self.is_full(c, s))
                checks.append((full < p.max_full_positions,
                               f"dolu pozisyon {full}/{p.max_full_positions}"))
        return checks

    def _record(self, st: CoinState, **event) -> None:
        st.history.append({"ts": self.clock(), **event})
        del st.history[:-HISTORY_LIMIT]

    # --- döngü ---

    def tick(self) -> None:
        self.refresh_watchlist()
        coins = self.active_coins()
        symbols = [self.cfg.symbol(c) for c in coins]
        regime_symbol = self.cfg.symbol(self.cfg.protection.regime_coin)
        if self.cfg.protection.btc_regime and regime_symbol not in symbols:
            symbols.append(regime_symbol)
        prices = self.market.prices(symbols)
        self._last_prices.update(prices)
        self.update_market_state(prices)
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
                self._save()

    def _tick_coin(self, coin: str, price: float) -> None:
        st = self.states.setdefault(coin, CoinState())
        if st.position is None and (coin not in self.watchlist or self.open_positions() >= self.cfg.max_open_positions):
            return  # yeni pozisyon açılmayacak
        needs_candles = st.position is None and not st.blocked and self.clock() >= st.cooldown_until
        if needs_candles and self.entry_block():
            log.debug("%s yeni pozisyon açılmıyor: %s", coin, self.entry_block())
            return
        closes = self._closes(coin) if needs_candles else []
        by_tf = self._filter_closes(coin) if needs_candles and self.cfg.entry_filter.active else None
        decision = self.strategy(coin).decide(st, price, closes, self.clock(), by_tf)
        if decision.action in (Action.BUY_BASE, Action.BUY_SAFETY):
            checks = self.guard_checks(coin, st, decision)
            if checks:
                decision.reason = " | ".join([decision.reason] + [mark(*c) for c in checks])
            if not all(ok for ok, _ in checks):
                log.info("%s %s yapılmadı: %s", coin, decision.action.value, decision.reason)
                return
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
        self._save()
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

    def startup_summary(self) -> str:
        c, p, ef = self.cfg, self.cfg.protection, self.cfg.entry_filter
        lines = [
            f"Pozisyon sınırı: {c.max_open_positions}{' (auto)' if c.max_open_auto else ''} | "
            f"coin başı en fazla {biggest_max_spend(c):,.0f} TL | en kötü durum harcaması "
            f"{planned_spend(c):,.0f} TL / bütçe {c.total_budget:,.0f} TL"
        ]
        rules = [name for name, on in (("4s trend", ef.trend), ("1s onay", ef.confirm), ("15dk tetik", ef.trigger)) if on]
        lines.append(f"Giriş filtresi: {', '.join(rules) if rules else 'kapalı'}")
        guards = []
        if p.btc_regime:
            guards.append(f"BTC rejimi{' + ek alım durdurma' if p.block_safety_when_risky else ''}")
        if p.max_invested_pct is not None:
            guards.append(f"risk sınırı %{p.max_invested_pct:g} = {c.total_budget * p.max_invested_pct / 100:,.0f} TL")
        if p.max_full_positions is not None:
            guards.append(f"en fazla {p.max_full_positions} dolu pozisyon")
        if p.circuit_breaker_pct is not None:
            guards.append(f"devre kesici %{p.circuit_breaker_pct:g} = {c.total_budget * p.circuit_breaker_pct / 100:,.0f} TL")
        lines.append(f"Korumalar: {', '.join(guards) if guards else 'kapalı'}")
        return "\n".join(lines)

    def run_forever(self) -> None:
        summary = self.startup_summary()
        for line in summary.splitlines():
            log.info(line)
        self.notifier.send(
            f"🤖 Bot başladı ({self.cfg.mode}, seçim: {self.cfg.selection.mode}) — en fazla "
            f"{self.cfg.max_open_positions} açık pozisyon, bütçe {self.cfg.total_budget:,.0f} TL\n{summary}"
        )
        while True:
            try:
                self.tick()
            except ccxt.NetworkError as e:
                log.warning("Ağ hatası: %s", e)
            except Exception:
                log.exception("Beklenmeyen hata")
            time.sleep(self.cfg.poll_seconds)
