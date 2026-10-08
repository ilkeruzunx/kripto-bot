"""Ayar senaryolarını aynı geçmiş veri üzerinde portföy olarak karşılaştırır.

python -m bot scenarios  — config.yaml'ı temel alır (A = bugünkü ayarlar) ve her senaryoda
bir değişiklik ekler. Tüm coinler tek bütçeyle, canlı botla aynı Engine üzerinden oynatılır.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .backtest import PortfolioResult, Series, load_ohlcv, simulate_portfolio
from .config import Config, parse_config

DEFAULT_COINS = ["BTC", "ETH", "SOL", "XRP", "AVAX", "PUMP", "SAND", "GLMR", "WLD", "APE", "PEPE",
                 "ZRO", "AAVE", "API3", "NMR"]
NEW_SIZING = {"base_order": 2000, "safety_order": 2000, "safety_orders": 3, "safety_volume_scale": 1.3}
WARMUP_DAYS = 35  # göstergeler (4s EMA50 = 150 mum = 25 gün) test başlamadan ısınsın
WEEK_HOURS = 7 * 24


@dataclass
class Scenario:
    key: str
    title: str
    raw: dict


def _merge(raw: dict, section: str, values: dict) -> dict:
    out = copy.deepcopy(raw)
    out[section] = {**(out.get(section) or {}), **values}
    return out


def build_scenarios(raw: dict, coins: list[str]) -> list[Scenario]:
    """config.yaml içeriğinden A..I senaryolarını üretir (ham sözlük olarak; parse_config ile doğrulanır)."""
    a = copy.deepcopy(raw)
    a["coins"] = list(coins)
    a["selection"] = {"mode": "fixed"}
    a["overrides"] = {k: v for k, v in (a.get("overrides") or {}).items() if str(k).upper() in coins}
    a.pop("entry_filter", None)
    a.pop("protection", None)

    b = _merge(a, "strategy", {"safety_step_pct": 4})
    c = copy.deepcopy(b)
    c["coins"] = [x for x in coins if x not in ("APE", "PEPE")]
    c["overrides"] = {k: v for k, v in c["overrides"].items() if str(k).upper() in c["coins"]}
    d = _merge(c, "entry_filter", {"trend": True, "confirm": True, "trigger": True})
    e = _merge(d, "protection", {"btc_regime": True, "block_safety_when_risky": True})
    f = _merge(e, "protection", {"max_invested_pct": 50, "max_full_positions": 4, "circuit_breaker_pct": 3})
    g = _merge(f, "strategy", NEW_SIZING)
    g["max_open_positions"] = "auto"
    h = _merge(a, "strategy", NEW_SIZING)
    h["max_open_positions"] = "auto"
    i = _merge(g, "protection", {"max_invested_pct": None})
    return [
        Scenario("A", "Bugünkü ayarlar", a),
        Scenario("B", "A + ek alım adımı %4", b),
        Scenario("C", "B − APE, PEPE", c),
        Scenario("D", "C + çoklu zaman dilimi filtresi", d),
        Scenario("E", "D + BTC rejimi + riskliyken ek alım yok", e),
        Scenario("F", "E + risk sınırı %50 / 4 dolu + devre kesici %3", f),
        Scenario("G", "F + 2.000 TL alımlar, auto pozisyon sınırı", g),
        Scenario("H", "A + yalnız yeni pozisyon boyutu", h),
        Scenario("I", "G, risk sınırı %100 (kapalı)", i),
    ]


def needed_timeframes(cfgs: list[Config]) -> dict[str, set[str]]:
    """Coin -> gereken zaman dilimleri (strateji + giriş filtresi + BTC rejimi)."""
    need: dict[str, set[str]] = {}
    for cfg in cfgs:
        for coin in cfg.coins:
            tfs = need.setdefault(coin, set())
            tfs.add(cfg.strategy_for(coin).timeframe)
            tfs.update(cfg.entry_filter.candles_needed())
        p = cfg.protection
        if p.btc_regime:
            need.setdefault(p.regime_coin, set()).update({p.regime_tf, "1h"})
    return need


def load_series(client, cfg: Config, need: dict[str, set[str]], days: int, data_dir: Path, log=print) -> Series:
    series: Series = {}
    for coin, tfs in need.items():
        for tf in sorted(tfs):
            sym = cfg.symbol(coin)
            log(f"{sym} {tf} verisi hazırlanıyor ({days} gün)...")
            series[(sym, tf)] = load_ohlcv(client, sym, tf, days, data_dir)
    return series


def market_crash_week(series: Series, coins: list[str], quote: str, start_ms: float, tf: str = "1h"):
    """Coinlerin eşit ağırlıklı ortalama 7 günlük değişiminin en kötü olduğu hafta.

    Döner: (başlangıç ms, bitiş ms, sepet %, BTC %).
    """
    closes: dict[str, dict[int, float]] = {
        c: {int(k[0]): float(k[4]) for k in series.get((f"{c}/{quote}", tf), [])} for c in coins
    }
    times = sorted(t for t in set().union(*[set(v) for v in closes.values()]) if t >= start_ms)
    week = WEEK_HOURS * 3_600_000
    best = (0.0, 0.0, 0.0, 0.0)
    for t in times[:: 6]:  # 6 saatte bir başlangıç yeterince ince
        changes = [v[t + week] / v[t] - 1 for v in closes.values() if t in v and t + week in v]
        if len(changes) < max(3, len(coins) // 2):
            continue
        avg = 100 * sum(changes) / len(changes)
        if avg < best[2]:
            btc = closes.get("BTC", {})
            btc_pct = 100 * (btc[t + week] / btc[t] - 1) if t in btc and t + week in btc else float("nan")
            best = (t, t + week, avg, btc_pct)
    return best


def _date(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def format_table(rows: list[tuple[Scenario, Config, PortfolioResult]], crash) -> str:
    c_start, c_end, basket, btc = crash
    head = (f"{'':<3}{'Senaryo':<48}{'Poz.':>5}{'İşlem':>7}{'Kazanma':>8}{'Net K/Z TL':>12}{'K/Z %':>7}"
            f"{'MaxDD TL':>10}{'MaxDD%':>7}{'SL':>5}{'Ort.SL TL':>11}{'Maks bağlı':>12}{'Kriz haftası':>14}"
            f"{'En kötü 7g':>12}{'DK':>4}")
    lines = [head, "-" * len(head)]
    for sc, cfg, r in rows:
        _, worst = r.worst_window(WEEK_HOURS)
        lines.append(
            f"{sc.key:<3}{sc.title[:47]:<48}{cfg.max_open_positions:>5}{r.trades:>7}{r.win_rate:>7.0f}%"
            f"{r.pnl:>+12,.0f}{100 * r.pnl / r.budget:>+7.1f}{-r.max_drawdown:>10,.0f}{r.max_drawdown_pct:>7.1f}"
            f"{r.stop_losses:>5}{r.avg_stop_loss:>11,.0f}{r.max_invested:>12,.0f}"
            f"{r.equity_change(c_start, c_end):>+14,.0f}{worst:>+12,.0f}{r.breaker_trips:>4}"
        )
    lines.append(
        f"\nKriz haftası: {_date(c_start)} → {_date(c_end)} (coin sepeti ort. %{basket:+.1f}, BTC %{btc:+.1f}). "
        "O haftadaki özsermaye değişimi (TL)."
    )
    lines.append(
        "Poz.: aynı anda en fazla açık pozisyon. SL: zarar durdura takılan işlem. Ort.SL: bir zarar durdurun "
        "ortalama K/Z'si. Maks bağlı: aynı anda pozisyonlarda duran en yüksek tutar. En kötü 7g: senaryonun "
        "kendi en kötü 7 günü. DK: devre kesicinin kaç kez devreye girdiği.\n"
        "Net K/Z: açık pozisyonlar son fiyattan (satış komisyonu düşülerek) satılmış sayılır. Kararlar 1 saatlik "
        "mum kapanışlarında verilir; mum içi hareketler gerçekte sonucu değiştirebilir."
    )
    return "\n".join(lines)


def run_scenarios(config_path: str, client, coins: list[str], days: int, data_dir: Path,
                  only: list[str] | None = None, log=print) -> str:
    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    scenarios = [s for s in build_scenarios(raw, coins) if not only or s.key in only]
    cfgs = [parse_config(s.raw) for s in scenarios]
    series = load_series(client, cfgs[0], needed_timeframes(cfgs), days + WARMUP_DAYS, data_dir, log)

    tf = cfgs[0].default_strategy.timeframe
    last = max(c[0] for (sym, t), v in series.items() if t == tf for c in v[-1:])
    start_ms = last - days * 86_400_000
    crash = market_crash_week(series, coins, cfgs[0].quote, start_ms, tf)
    rows = []
    for sc, cfg in zip(scenarios, cfgs):
        log(f"Senaryo {sc.key}: {sc.title}...")
        rows.append((sc, cfg, simulate_portfolio(cfg, series, start_ms=start_ms)))
    header = (f"Dönem: {_date(start_ms)} → {_date(last)} ({days} gün, önceki {WARMUP_DAYS} gün yalnız gösterge "
              f"ısınması için). Bütçe {cfgs[0].total_budget:,.0f} TL. Coinler: {', '.join(coins)}\n")
    return header + "\n" + format_table(rows, crash)
