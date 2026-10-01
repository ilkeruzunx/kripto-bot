"""Geçmiş veriyle test: canlı botla aynı Engine ve Strategy kodunu mum mum çalıştırır."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, replace
from pathlib import Path

import ccxt

from .config import Config
from .engine import Engine
from .exchange import PaperBroker

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
    path = data_dir / f"{symbol.replace('/', '_')}_{timeframe}_{days}d.json"
    if path.exists() and time.time() - path.stat().st_mtime < 12 * 3600:
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

    def save(self, states):
        pass


class _SilentNotifier:
    def send(self, text: str) -> None:
        log.debug(text)


class _ReplayMarket:
    """Engine'e 'şu anki' mumu ve öncesini gösterir."""

    def __init__(self, symbol: str, candles: list[list[float]]):
        self.symbol = symbol
        self.candles = candles
        self.i = 0

    def prices(self, symbols):
        return {self.symbol: float(self.candles[self.i][4])}

    def closes(self, symbol, timeframe, n):
        lo = max(0, self.i + 1 - n)
        return [float(c[4]) for c in self.candles[lo : self.i + 1]]

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


def simulate(cfg: Config, coin: str, candles: list[list[float]], slippage: float = 0.001) -> Result:
    s = cfg.strategy_for(coin)
    budget = s.max_spend()
    one = replace(
        cfg, coins=[coin], default_strategy=s, overrides={}, total_budget=budget, mode="paper",
        max_open_positions=1, selection=replace(cfg.selection, mode="fixed"),
    )
    symbol = one.symbol(coin)
    market = _ReplayMarket(symbol, candles)
    broker = PaperBroker(budget, one.fee, slippage)
    now = {"t": 0.0}
    engine = Engine(one, market, broker, _MemoryStore(), _SilentNotifier(), clock=lambda: now["t"])

    peak = budget
    max_dd = 0.0
    for i, c in enumerate(candles):
        market.i = i
        now["t"] = c[0] / 1000
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
