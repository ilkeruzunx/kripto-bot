"""Geçmiş veriyle test: canlı botla aynı Engine ve Strategy kodunu mum mum çalıştırır."""
from __future__ import annotations

import json
import logging
import time
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from pathlib import Path

import ccxt

from .config import Config, timeframe_seconds
from .engine import Engine
from .exchange import PaperBroker
from .strategy import Action

log = logging.getLogger(__name__)


# --- veri ---

def download_ohlcv(client: ccxt.Exchange, symbol: str, timeframe: str, days: int) -> list[list[float]]:
    tf_ms = client.parse_timeframe(timeframe) * 1000
    now = client.milliseconds()
    since = now - days * 86_400_000
    out: dict[int, list[float]] = {}
    while since < now:
        batch = client.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        for c in batch:
            out[int(c[0])] = c
        since += 1000 * tf_ms
        time.sleep(client.rateLimit / 1000)
    return [out[k] for k in sorted(out)]


def load_ohlcv(client: ccxt.Exchange | None, symbol: str, timeframe: str, days: int, data_dir: Path) -> list[list[float]]:
    """Önbellekteki veri 12 saatten yeniyse onu kullanır; client verilmezse (çevrimdışı) eski veriyi de kullanır."""
    path = data_dir / f"{symbol.replace('/', '_')}_{timeframe}_{days}d.json"
    if path.exists() and (client is None or time.time() - path.stat().st_mtime < 12 * 3600):
        return json.loads(path.read_text())
    if client is None:
        raise FileNotFoundError(path)
    candles = download_ohlcv(client, symbol, timeframe, days)
    data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candles))
    return candles


# --- simülasyon ---

class _MemoryStore:
    def load(self):
        return {}

    def load_meta(self):
        return {}

    def save(self, states, meta=None):
        pass


class _SilentNotifier:
    def send(self, text: str) -> None:
        log.debug(text)


Series = dict  # {(sembol, zaman dilimi): mumlar}


class _Closed:
    """Bir mum serisinden belirli bir anda KAPANMIŞ olanları verir (ileriyi görmeyi engeller)."""

    def __init__(self, candles: list[list[float]], tf: str):
        self.candles = candles
        self.tf_ms = timeframe_seconds(tf) * 1000
        self.opens = [c[0] for c in candles]

    def upto(self, now_ms: float, n: int) -> list[list[float]]:
        hi = bisect_right(self.opens, now_ms - self.tf_ms)  # açılış + süre <= şimdi
        return self.candles[max(0, hi - n) : hi]


class _ReplayMarket:
    """Engine'e 'şu anki' mumu ve öncesini gösterir.

    extra: başka coin/zaman dilimi serileri {(sembol, tf): mumlar}. Bunlardan yalnız şu anki
    mumun kapanış anına kadar kapanmış olanlar verilir.
    """

    def __init__(self, symbol: str, candles: list[list[float]], extra: Series | None = None, timeframe: str = "1h"):
        self.symbol = symbol
        self.candles = candles
        self.timeframe = timeframe
        self.tf_ms = timeframe_seconds(timeframe) * 1000
        self.i = 0
        self.series = {k: _Closed(v, k[1]) for k, v in (extra or {}).items()}
        self.series[(symbol, timeframe)] = _Closed(candles, timeframe)

    @property
    def now_ms(self) -> float:
        """Şu anki mumun kapanış anı."""
        return self.candles[self.i][0] + self.tf_ms

    def prices(self, symbols):
        out = {self.symbol: float(self.candles[self.i][4])}
        for sym in symbols:
            ser = self.series.get((sym, self.timeframe))
            if sym != self.symbol and ser:
                last = ser.upto(self.now_ms, 1)
                if last:
                    out[sym] = float(last[-1][4])
        return out

    def closes(self, symbol, timeframe, n):
        lo = max(0, self.i + 1 - n)
        return [float(c[4]) for c in self.candles[lo : self.i + 1]]

    def ohlcv(self, symbol, timeframe, n):
        ser = self.series.get((symbol, timeframe))
        return ser.upto(self.now_ms, n) if ser else []

    def min_cost(self, symbol):
        return 0.0


@dataclass
class Result:
    coin: str
    candles: int
    budget: float
    pnl: float
    trades: int
    wins: int
    open_value: float
    max_drawdown_pct: float
    buy_hold_pct: float

    @property
    def pnl_pct(self) -> float:
        return 100 * self.pnl / self.budget if self.budget else 0.0


def simulate(
    cfg: Config, coin: str, candles: list[list[float]], slippage: float = 0.001, extra: Series | None = None
) -> Result:
    """Tek coin, kendi bütçesiyle (coin başı max_spend). Portföy düzeyindeki korumalar (risk sınırı,
    dolu pozisyon sınırı, devre kesici) burada anlamsız olduğu için kapatılır; onlar için simulate_portfolio.

    extra: giriş filtresi / BTC rejimi için gereken diğer seriler {(sembol, tf): mumlar}.
    """
    s = cfg.strategy_for(coin)
    budget = s.max_spend()
    one = replace(
        cfg, coins=[coin], default_strategy=s, overrides={}, total_budget=budget, mode="paper",
        max_open_positions=1, selection=replace(cfg.selection, mode="fixed"),
        protection=replace(cfg.protection, max_invested_pct=None, max_full_positions=None, circuit_breaker_pct=None),
    )
    symbol = one.symbol(coin)
    market = _ReplayMarket(symbol, candles, extra, s.timeframe)
    broker = PaperBroker(budget, one.fee, slippage)
    now = {"t": 0.0}
    engine = Engine(one, market, broker, _MemoryStore(), _SilentNotifier(), clock=lambda: now["t"])

    peak = budget
    max_dd = 0.0
    for i, c in enumerate(candles):
        market.i = i
        now["t"] = market.now_ms / 1000
        engine.tick()
        equity = broker.cash + broker.holdings.get(symbol, 0) * float(c[4]) * (1 - one.fee)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)

    st = engine.states[coin]
    last = float(candles[-1][4]) if candles else 0.0
    open_value = broker.holdings.get(symbol, 0) * last * (1 - one.fee)
    first = float(candles[0][4]) if candles else 0.0
    return Result(
        coin=coin,
        candles=len(candles),
        budget=budget,
        pnl=broker.cash + open_value - budget,
        trades=st.closed_trades,
        wins=st.wins,
        open_value=open_value,
        max_drawdown_pct=max_dd,
        buy_hold_pct=(last / first - 1) * 100 if first else 0.0,
    )


def format_results(results: list[Result]) -> str:
    lines = [
        f"{'Coin':<7}{'Mum':>7}{'Bütçe':>10}{'K/Z TL':>11}{'K/Z %':>8}{'İşlem':>7}{'Kazanç':>8}"
        f"{'Açık TL':>10}{'MaxDD%':>8}{'Al-tut%':>9}",
        "-" * 85,
    ]
    for r in results:
        win = f"{100 * r.wins / r.trades:.0f}%" if r.trades else "-"
        lines.append(
            f"{r.coin:<7}{r.candles:>7}{r.budget:>10,.0f}{r.pnl:>+11,.0f}{r.pnl_pct:>+8.1f}{r.trades:>7}{win:>8}"
            f"{r.open_value:>10,.0f}{r.max_drawdown_pct:>8.1f}{r.buy_hold_pct:>+9.1f}"
        )
    tb = sum(r.budget for r in results)
    tp = sum(r.pnl for r in results)
    lines.append("-" * 85)
    lines.append(f"{'TOPLAM':<7}{'':>7}{tb:>10,.0f}{tp:>+11,.0f}{(100 * tp / tb if tb else 0):>+8.1f}")
    lines.append(
        "\nK/Z: açık pozisyonlar son fiyattan satılmış sayılır. Al-tut: dönem başında alıp tutsaydın.\n"
        "Kararlar mum kapanışlarıyla verilir; gerçekte mum içi hareketler sonucu değiştirebilir."
    )
    return "\n".join(lines)


# --- portföy simülasyonu: tüm coinler aynı bütçe, aynı Engine ---

class _PortfolioMarket:
    """Birden çok coinin geçmişini aynı saatte oynatır. Yalnız now_ms anına kadar kapanmış mumlar görünür."""

    def __init__(self, series: Series, base_tf: str = "1h"):
        self.series = {k: _Closed(v, k[1]) for k, v in series.items()}
        self.base_tf = base_tf
        self.now_ms = 0.0

    def prices(self, symbols):
        out = {}
        for sym in symbols:
            ser = self.series.get((sym, self.base_tf))
            last = ser.upto(self.now_ms, 1) if ser else []
            if last:
                out[sym] = float(last[-1][4])
        return out

    def ohlcv(self, symbol, timeframe, n):
        ser = self.series.get((symbol, timeframe))
        return ser.upto(self.now_ms, n) if ser else []

    def closes(self, symbol, timeframe, n):
        return [float(c[4]) for c in self.ohlcv(symbol, timeframe, n)]

    def min_cost(self, symbol):
        return 0.0


class _RecordingEngine(Engine):
    """Her alım/satımı (geçmiş sınırı olmadan) saklar."""

    def __init__(self, *a, **k):
        self.events: list[dict] = []
        super().__init__(*a, **k)

    def _record(self, st, **event):
        super()._record(st, **event)
        self.events.append({"ts": self.clock(), **event})


class _CountingNotifier:
    def __init__(self):
        self.msgs: list[str] = []

    def send(self, text: str) -> None:
        self.msgs.append(text)


@dataclass
class PortfolioResult:
    budget: float
    pnl: float
    trades: int
    wins: int
    stop_losses: int
    stop_loss_total: float          # zarar durdurların toplam K/Z'si (negatif)
    max_drawdown: float             # TL
    max_drawdown_pct: float
    max_invested: float             # aynı anda bağlanan en yüksek tutar
    open_value: float
    breaker_trips: int
    equity: list[tuple[float, float]] = field(default_factory=list)  # (ms, özsermaye)

    @property
    def win_rate(self) -> float:
        return 100 * self.wins / self.trades if self.trades else 0.0

    @property
    def avg_stop_loss(self) -> float:
        return self.stop_loss_total / self.stop_losses if self.stop_losses else 0.0

    def equity_change(self, start_ms: float, end_ms: float) -> float:
        """İki an arasındaki özsermaye değişimi (TL)."""
        times = [t for t, _ in self.equity]
        a = max(0, bisect_right(times, start_ms) - 1)
        b = max(0, bisect_right(times, end_ms) - 1)
        return self.equity[b][1] - self.equity[a][1]

    def worst_window(self, hours: int) -> tuple[float, float]:
        """Özsermayenin en çok düştüğü 'hours' saatlik pencere: (başlangıç ms, değişim TL)."""
        worst, at = 0.0, self.equity[0][0] if self.equity else 0.0
        times = [t for t, _ in self.equity]
        for i, (t, e) in enumerate(self.equity):
            j = bisect_right(times, t + hours * 3_600_000) - 1
            change = self.equity[j][1] - e
            if change < worst:
                worst, at = change, t
        return at, worst


def simulate_portfolio(
    cfg: Config, series: Series, start_ms: float = 0, end_ms: float | None = None, slippage: float = 0.001
) -> PortfolioResult:
    """cfg.coins'in hepsini tek bütçeyle, canlı botla aynı Engine üzerinden saat saat oynatır.

    series: {(sembol, tf): mumlar}. start_ms öncesi mumlar yalnız göstergelerin ısınması için kullanılır.
    Saat t'deki tur, t'de açılan 1 saatlik mumun kapanışında (t + 1 saat) çalışır ve o ana kadar
    KAPANMIŞ mumları görür.
    """
    base_tf = cfg.default_strategy.timeframe
    tf_ms = timeframe_seconds(base_tf) * 1000
    symbols = [cfg.symbol(c) for c in cfg.coins]
    opens = sorted({c[0] for sym in symbols for c in series.get((sym, base_tf), [])
                    if c[0] >= start_ms and (end_ms is None or c[0] < end_ms)})
    market = _PortfolioMarket(series, base_tf)
    broker = PaperBroker(cfg.total_budget, cfg.fee, slippage)
    notes = _CountingNotifier()
    now = {"t": 0.0}
    engine = _RecordingEngine(cfg, market, broker, _MemoryStore(), notes, clock=lambda: now["t"])

    peak = cfg.total_budget
    max_dd = max_dd_pct = max_invested = 0.0
    equity: list[tuple[float, float]] = []
    for t in opens:
        market.now_ms = t + tf_ms
        now["t"] = market.now_ms / 1000
        engine.tick()
        prices = market.prices(symbols)
        eq = broker.cash + sum(q * prices.get(sym, 0) * (1 - cfg.fee) for sym, q in broker.holdings.items())
        equity.append((market.now_ms, eq))
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100)
        max_invested = max(max_invested, engine.invested())

    final = equity[-1][1] if equity else cfg.total_budget
    sl = [e for e in engine.events if e.get("kind") == Action.SELL_SL.value]
    return PortfolioResult(
        budget=cfg.total_budget,
        pnl=final - cfg.total_budget,
        trades=sum(s.closed_trades for s in engine.states.values()),
        wins=sum(s.wins for s in engine.states.values()),
        stop_losses=len(sl),
        stop_loss_total=sum(e.get("pnl", 0) for e in sl),
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct,
        max_invested=max_invested,
        open_value=final - broker.cash,
        breaker_trips=sum(1 for m in notes.msgs if m.startswith("⛔")),
        equity=equity,
    )
