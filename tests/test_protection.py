"""Çoklu zaman dilimi giriş filtresi, sert düşüş korumaları, auto pozisyon sınırı ve senaryolar."""
import math

import pytest
import yaml

from bot.backtest import _Closed, _ReplayMarket, simulate, simulate_portfolio
from bot.config import ConfigError, EntryFilterConfig, auto_max_open_positions, parse_config, planned_spend
from bot.engine import MARKET_BREAKER, MARKET_NORMAL, MARKET_RISKY, Engine
from bot.exchange import PaperBroker
from bot.state import StateStore
from bot.strategy import Action, CoinState, Strategy, entry_filter_checks

NOW = 1_000_000_000.0  # saniye; mumlar buna göre dizilir
H = 3600


def candles(closes, tf_s, now=NOW, forming=None):
    """Son mum tam 'now' anında kapanacak şekilde dizilmiş mumlar; forming verilirse oluşmakta olan mum eklenir."""
    n = len(closes)
    out = [[(now - (n - i) * tf_s) * 1000, c, c, c, c, 1] for i, c in enumerate(closes)]
    if forming is not None:
        out.append([now * 1000, forming, forming, forming, forming, 1])
    return out


class CandleMarket:
    """Fiyat + (sembol, tf) başına mum verisi. ohlcv çağrılarını sayar."""

    def __init__(self, prices, series=None):
        self.p = prices
        self.series = series or {}
        self.calls = []

    def prices(self, symbols):
        return {s: self.p[s] for s in symbols if s in self.p}

    def closes(self, symbol, timeframe, n):
        return [self.p[symbol]] * n

    def ohlcv(self, symbol, timeframe, n):
        self.calls.append((symbol, timeframe))
        return self.series.get((symbol, timeframe), [])[-n:]

    def min_cost(self, symbol):
        return 10.0


class Notes:
    def __init__(self):
        self.msgs = []

    def send(self, t):
        self.msgs.append(t)


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


BASE = dict(rsi_below=None, trend_ema=None, trailing_pct=0, base_order=100, safety_orders=1,
            safety_order=100, safety_step_pct=5, take_profit_pct=3, stop_loss_pct=10,
            cooldown_after_tp_min=0, cooldown_after_sl_min=0)


def make_cfg(coins=("BTC", "ETH"), budget=400, strategy=None, **extra):
    raw = {"coins": list(coins), "total_budget": budget, "fee_pct": 0.2, "strategy": {**BASE, **(strategy or {})}}
    raw.update(extra)
    return parse_config(raw)


def make_engine(tmp_path, cfg, market, clock=None):
    clock = clock or Clock()
    broker = PaperBroker(cfg.total_budget, cfg.fee, slippage=0)
    e = Engine(cfg, market, broker, StateStore(tmp_path / "state.json"), Notes(), clock=clock)
    return e, broker, clock


UP = [100 + i for i in range(200)]
DOWN = [300 - i for i in range(200)]


# --- BÖLÜM 3: auto pozisyon sınırı ---

NEW_SIZING = {"base_order": 2000, "safety_order": 2000, "safety_orders": 3, "safety_volume_scale": 1.3}


@pytest.mark.parametrize("budget,expected", [(50_000, 5), (100_000, 10), (9_980, 1), (19_959, 1)])
def test_auto_max_open_positions(budget, expected):
    c = parse_config({"coins": ["BTC"], "total_budget": budget, "selection": {"mode": "scan"},
                      "strategy": NEW_SIZING, "max_open_positions": "auto",
                      "overrides": {"TRUMP": {"safety_step_pct": 6, "stop_loss_pct": 18}}})
    assert c.strategy_for("TRUMP").max_spend() == pytest.approx(9980)
    assert c.max_open_positions == expected and c.max_open_auto
    assert planned_spend(c) <= budget


def test_auto_uses_biggest_coin_and_manual_value_still_budget_checked():
    raw = {"coins": ["BTC"], "total_budget": 50_000, "selection": {"mode": "scan"}, "strategy": NEW_SIZING,
           "overrides": {"BIG": {"base_order": 4000}}}  # BIG: 4000+2000+2600+3380 = 11980
    c = parse_config({**raw, "max_open_positions": "auto"})
    assert c.max_open_positions == 4 == auto_max_open_positions(c)
    assert parse_config({**raw, "max_open_positions": 3}).max_open_positions == 3
    with pytest.raises(ConfigError, match="yetmiyor"):
        parse_config({**raw, "max_open_positions": 5})
    with pytest.raises(ConfigError, match="yetmiyor"):
        parse_config({**raw, "total_budget": 5000, "max_open_positions": "auto"})
    with pytest.raises(ConfigError):
        parse_config({**raw, "max_open_positions": "çok"})


def test_repo_config_is_scenario_f():
    from bot.scenarios import DEFAULT_COINS, build_scenarios

    c = parse_config(yaml.safe_load(open("config.yaml", encoding="utf-8")))
    assert c.mode == "paper"
    assert c.excluded("APE") and c.excluded("PEPE") and "APE" not in c.coins and "PEPE" not in c.coins
    # F senaryosunun (eski bugünkü ayarlar + değişiklikler) tüm strateji/koruma ayarları birebir
    f = parse_config(next(s for s in build_scenarios(yaml.safe_load(open("config.yaml", encoding="utf-8")),
                                                    DEFAULT_COINS) if s.key == "F").raw)
    assert c.default_strategy == f.default_strategy and c.default_strategy.safety_step_pct == 4
    assert c.default_strategy.base_order == 800 and c.max_open_positions == 12
    assert c.entry_filter == f.entry_filter and c.entry_filter.trend and c.entry_filter.confirm and c.entry_filter.trigger
    p = c.protection
    assert p == f.protection
    assert p.btc_regime and p.block_safety_when_risky
    assert p.max_invested_pct == 50 and p.max_full_positions == 4 and p.circuit_breaker_pct == 3


# --- BÖLÜM 1: çoklu zaman dilimi filtresi ---

def ef(**kw):
    return EntryFilterConfig(**kw)


def test_trend_rule():
    f = ef(trend=True)
    up, down = entry_filter_checks(f, 1, {"4h": UP}), entry_filter_checks(f, 1, {"4h": DOWN})
    assert up[0][0] and "EMA20" in up[0][1] and ">" in up[0][1]
    assert not down[0][0]
    short = entry_filter_checks(f, 1, {"4h": UP[:20]})
    assert not short[0][0] and "yetersiz" in short[0][1]


def test_confirm_rule_needs_price_above_ema_and_rsi_below():
    f = ef(confirm=True)
    # yükselişten sonra küçük geri çekilme: fiyat EMA50 üstünde, RSI 50 altında
    dip = UP[:190] + [289 - 2 * i for i in range(10)]
    assert entry_filter_checks(f, dip[-1], {"1h": dip})[0][0]
    assert not entry_filter_checks(f, UP[-1], {"1h": UP})[0][0]       # RSI yüksek
    assert not entry_filter_checks(f, DOWN[-1], {"1h": DOWN})[0][0]   # fiyat EMA altında


def test_trigger_rule_needs_recent_dip_and_turn():
    f = ef(trigger=True, trigger_lookback=4)
    flat = [100 + (i % 2) for i in range(60)]
    fall = flat + [95, 90, 85, 80]
    assert not entry_filter_checks(f, 80, {"15m": fall})[0][0]          # dipte ama dönmedi
    turned = fall + [82]
    ok, why = entry_filter_checks(f, 82, {"15m": turned})[0]
    assert ok and "dönüyor" in why
    assert not entry_filter_checks(f, 101, {"15m": flat})[0][0]          # hiç 35 altına inmedi


def test_strategy_reason_lists_passed_and_failed_rules():
    s = Strategy(make_cfg().default_strategy, 0.002, ef(trend=True, trigger=True))
    d = s.decide(CoinState(), 100, [100.0] * 50, 0, {"4h": UP, "15m": [100.0] * 60})
    assert d.action == Action.HOLD
    assert "✓ koşulsuz giriş" in d.reason and "✓ 4h EMA20" in d.reason and "✗ 15m RSI" in d.reason


def test_filter_only_affects_new_positions():
    s = Strategy(make_cfg().default_strategy, 0.002, ef(trend=True))
    st = CoinState()
    assert s.decide(st, 100, [100.0] * 50, 0, {"4h": DOWN}).action == Action.HOLD
    from bot.strategy import Position
    st.position = Position()
    st.position.add_buy(1, 100, 100, 0)
    assert s.decide(st, 95, [], 0, None).action == Action.BUY_SAFETY  # ek alım filtreye bakmaz


def test_disabled_filter_keeps_old_reason_and_decision():
    cfg = make_cfg(strategy={"rsi_below": 40})
    old = Strategy(cfg.default_strategy, 0.002)
    new = Strategy(cfg.default_strategy, 0.002, ef())
    down = [200 - i for i in range(60)]
    assert old.decide(CoinState(), down[-1], down, 0).__dict__ == new.decide(CoinState(), down[-1], down, 0).__dict__


# --- kapanmış mumlar ve zaman dilimi önbelleği ---

def test_engine_ignores_forming_candle_and_caches_per_timeframe(tmp_path):
    cfg = make_cfg(entry_filter={"trend": True, "trigger": True})
    series = {("BTC/TRY", "4h"): candles(UP, 4 * H, forming=1.0),
              ("BTC/TRY", "15m"): candles([100.0] * 70, 900, forming=1.0)}
    m = CandleMarket({"BTC/TRY": 100.0}, series)
    e, _, clock = make_engine(tmp_path, cfg, m)
    got = e._closed_candles("BTC/TRY", "4h", 150)
    assert len(got) == 150 and got[-1][4] == UP[-1]       # oluşan mum (1.0) yok
    e._closed_candles("BTC/TRY", "4h", 150)
    e._closed_candles("BTC/TRY", "15m", 60)
    assert m.calls == [("BTC/TRY", "4h"), ("BTC/TRY", "15m")]  # 4h ikinci kez çekilmedi
    clock.t += 15 * 60                                    # yeni 15dk mumu kapandı, 4s mumu kapanmadı
    e._closed_candles("BTC/TRY", "4h", 150)
    e._closed_candles("BTC/TRY", "15m", 60)
    assert m.calls[-1] == ("BTC/TRY", "15m") and m.calls.count(("BTC/TRY", "4h")) == 1


def test_replay_never_shows_unclosed_candles():
    c = _Closed([[i * 900_000, 0, 0, 0, i, 0] for i in range(100)], "15m")
    # 10. mum 9_000_000'da açılır, 9_900_000'da kapanır
    assert c.upto(9_899_999, 5)[-1][4] == 9
    assert c.upto(9_900_000, 5)[-1][4] == 10


def test_backtest_filter_has_no_lookahead():
    """15dk tetik, 1 saatlik turun kapanışından SONRA gelen dibi göremez."""
    cfg = make_cfg(coins=["BTC"], budget=10_000, entry_filter={"trigger": True, "trigger_lookback": 3})
    hours = 80
    base = [[i * H * 1000, 100, 100, 100, 100.0, 1] for i in range(hours)]
    q = []
    for i in range(hours * 4):
        price = 100 + (i % 2) * 0.5
        if i in (hours * 4 - 3, hours * 4 - 2):   # son saatin içindeki dip ve dönüş (son 15dk mum hariç)
            price = 70 if i == hours * 4 - 3 else 72
        q.append([i * 900_000, price, price, price, price, 1])
    extra = {("BTC/TRY", "15m"): q}
    m = _ReplayMarket("BTC/TRY", base, extra)
    m.i = hours - 2   # sondan bir önceki saatin kapanışı: dip henüz olmadı
    assert all(c[0] + 900_000 <= m.now_ms for c in m.ohlcv("BTC/TRY", "15m", 1000))
    assert min(c[4] for c in m.ohlcv("BTC/TRY", "15m", 1000)) > 99
    r = simulate(cfg, "BTC", base[:-1], extra=extra)
    assert r.trades == 0 and r.open_value == 0


# --- BÖLÜM 2: BTC rejimi ---

def regime_cfg(**prot):
    return make_cfg(protection={"btc_regime": True, "regime_drop_24h_pct": None, **prot})


def regime_market(btc_closes, price):
    return CandleMarket({"BTC/TRY": price, "ETH/TRY": 10.0},
                        {("BTC/TRY", "4h"): candles(btc_closes, 4 * H)})


def test_btc_below_ema_blocks_new_positions(tmp_path):
    e, _, _ = make_engine(tmp_path, regime_cfg(), regime_market(DOWN, DOWN[-1]))
    e.tick()
    assert e.market_state["state"] == MARKET_RISKY and "EMA50" in e.market_state["reason"]
    assert all(s.position is None for s in e.states.values())


def test_btc_above_ema_allows_entries_with_reason(tmp_path):
    e, _, _ = make_engine(tmp_path, regime_cfg(), regime_market(UP, UP[-1]))
    e.tick()
    assert e.market_state["state"] == MARKET_NORMAL
    assert e.states["ETH"].position is not None
    assert any("✓ piyasa normal" in m for m in e.notifier.msgs)


def test_btc_24h_drop_marks_market_risky(tmp_path):
    cfg = make_cfg(protection={"btc_regime": True, "regime_drop_24h_pct": 4})
    hourly = [1000.0] * 30
    m = CandleMarket({"BTC/TRY": 950.0, "ETH/TRY": 10.0},
                     {("BTC/TRY", "4h"): candles([500.0] * 150, 4 * H), ("BTC/TRY", "1h"): candles(hourly, H)})
    e, _, _ = make_engine(tmp_path, cfg, m)
    e.tick()
    assert e.market_state["state"] == MARKET_RISKY and "24 saatte %-5.0" in e.market_state["reason"]
    m.p["BTC/TRY"] = 970.0
    e.tick()
    assert e.market_state["state"] == MARKET_NORMAL


def _open_eth(tmp_path, cfg):
    m = regime_market(UP, UP[-1])
    e, broker, clock = make_engine(tmp_path, cfg, m)
    e.tick()
    assert e.states["ETH"].position is not None
    # BTC çöktü
    m.series[("BTC/TRY", "4h")] = candles(DOWN, 4 * H, now=clock.t + 4 * H)
    clock.t += 4 * H
    m.p["BTC/TRY"] = DOWN[-1]
    return e, m, clock


def test_safety_orders_blocked_only_when_configured(tmp_path):
    e, m, _ = _open_eth(tmp_path, regime_cfg(block_safety_when_risky=True))
    m.p["ETH/TRY"] = 9.5
    e.tick()
    assert e.market_state["state"] == MARKET_RISKY and e.states["ETH"].position.buys == 1

    e2, m2, _ = _open_eth(tmp_path / "b", regime_cfg(block_safety_when_risky=False))
    m2.p["ETH/TRY"] = 9.5
    e2.tick()
    assert e2.states["ETH"].position.buys == 2


def test_exits_work_while_market_risky(tmp_path):
    e, m, _ = _open_eth(tmp_path, regime_cfg(block_safety_when_risky=True))
    m.p["ETH/TRY"] = 8.0  # zarar durdur
    e.tick()
    assert e.states["ETH"].position is None and e.states["ETH"].realized_pnl < 0
    e2, m2, _ = _open_eth(tmp_path / "b", regime_cfg(block_safety_when_risky=True))
    m2.p["ETH/TRY"] = 11.0  # kar al
    e2.tick()
    assert e2.states["ETH"].position is None and e2.states["ETH"].wins == 1


# --- BÖLÜM 2: toplam risk sınırı ---

def test_max_invested_pct_limits_all_buys(tmp_path):
    cfg = make_cfg(coins=["A", "B", "C"], budget=600, protection={"max_invested_pct": 50})  # 300 TL
    m = CandleMarket({"A/TRY": 10.0, "B/TRY": 10.0, "C/TRY": 10.0})
    e, _, _ = make_engine(tmp_path, cfg, m)
    e.tick()
    assert sum(1 for s in e.states.values() if s.position) == 3   # 3 x 100 = 300
    m.p["A/TRY"] = 9.5
    e.tick()
    assert e.states["A"].position.buys == 1                         # 300 + 100 > 300
    assert e.invested() <= 300


def test_max_full_positions(tmp_path):
    cfg = make_cfg(coins=["A", "B", "C"], budget=600, protection={"max_full_positions": 2})
    m = CandleMarket({"A/TRY": 10.0, "B/TRY": 10.0, "C/TRY": 10.0})
    e, _, _ = make_engine(tmp_path, cfg, m)
    e.tick()
    for c in "ABC":
        m.p[f"{c}/TRY"] = 9.5
    e.tick()
    full = [c for c, s in e.states.items() if e.is_full(c, s)]
    assert len(full) == 2 and e.states["C"].position.buys == 1


# --- BÖLÜM 2: devre kesici ---

def test_circuit_breaker_stops_buys_but_not_exits(tmp_path):
    cfg = make_cfg(coins=["A", "B"], budget=400, strategy={"safety_orders": 0, "stop_loss_pct": 30},
                   max_open_positions=1, protection={"circuit_breaker_pct": 3})  # 12 TL
    m = CandleMarket({"A/TRY": 10.0, "B/TRY": 10.0})
    e, _, clock = make_engine(tmp_path, cfg, m)
    e.tick()
    assert e.states["A"].position is not None
    clock.t += 600
    m.p["A/TRY"] = 8.5  # anlık ~ -15 TL
    e.tick()
    assert e.market_state["state"] == MARKET_BREAKER
    assert any(msg.startswith("⛔ Devre kesici") for msg in e.notifier.msgs)
    assert e.states["A"].position is not None                      # açık pozisyon satılmadı
    m.p["A/TRY"] = 6.9                                            # zarar durdur yine çalışır
    clock.t += 600
    e.tick()
    assert e.states["A"].position is None
    clock.t += 3600
    e.tick()
    assert all(s.position is None for s in e.states.values())     # 24 saat alım yok
    # durum diske yazıldı: yeniden başlatmada da devre kesici sürer
    e2, _, _ = make_engine(tmp_path, cfg, m, clock)
    e2.tick()
    assert e2.market_state["state"] == MARKET_BREAKER and all(s.position is None for s in e2.states.values())
    clock.t += 25 * 3600
    e2.tick()
    assert e2.market_state["state"] == MARKET_NORMAL
    assert any(s.position for s in e2.states.values())
    assert any("süresi doldu" in msg for msg in e2.notifier.msgs)


def test_breaker_threshold_scales_with_budget(tmp_path):
    cfg = make_cfg(coins=["A"], budget=10_000, strategy={"safety_orders": 0, "stop_loss_pct": 50},
                   protection={"circuit_breaker_pct": 3})  # 300 TL
    m = CandleMarket({"A/TRY": 10.0})
    e, _, clock = make_engine(tmp_path, cfg, m)
    e.tick()
    m.p["A/TRY"] = 8.0  # -20 TL: 10.000 TL bütçede devre kesici için çok küçük
    clock.t += 600
    e.tick()
    assert e.market_state["state"] == MARKET_NORMAL


# --- BÖLÜM 4: görünürlük ---

def test_market_state_visible_in_status_and_panel(tmp_path, capsys):
    from bot.__main__ import market_line
    from bot.status import collect_status

    e, _, _ = make_engine(tmp_path, regime_cfg(), regime_market(DOWN, DOWN[-1]))
    e.states["ETH"] = CoinState()
    e.tick()
    meta = StateStore(tmp_path / "state.json").load_meta()
    assert meta["market"]["state"] == MARKET_RISKY
    assert "RİSKLİ" in market_line(meta) and "EMA50" in market_line(meta)
    data = collect_status(e.cfg, tmp_path / "state.json", market=e.market)
    assert data["market"]["state"] == MARKET_RISKY


def test_unblock_style_save_keeps_meta(tmp_path):
    store = StateStore(tmp_path / "s.json")
    store.save({}, {"market": {"state": "riskli"}})
    store.save({"BTC": CoinState()})
    assert store.load_meta()["market"]["state"] == "riskli"


def test_startup_summary_logs_limit_and_worst_case(tmp_path):
    cfg = parse_config({"coins": ["BTC"], "total_budget": 50_000, "selection": {"mode": "scan"},
                        "strategy": NEW_SIZING, "max_open_positions": "auto",
                        "protection": {"max_invested_pct": 50, "circuit_breaker_pct": 3}})
    e, _, _ = make_engine(tmp_path, cfg, CandleMarket({}))
    s = e.startup_summary()
    assert "Pozisyon sınırı: 5 (auto)" in s and "49,900" in s and "25,000" in s and "1,500" in s


# --- portföy backtest ve senaryolar ---

def test_portfolio_simulation_runs_and_respects_budget():
    cfg = make_cfg(coins=["BTC", "ETH"], budget=10_000, strategy={"base_order": 1000, "safety_order": 1000,
                                                                  "safety_orders": 2},
                   protection={"max_invested_pct": 30})
    series = {}
    for k, sym in enumerate(["BTC/TRY", "ETH/TRY"]):
        series[(sym, "1h")] = [[i * H * 1000, 0, 0, 0, 100 * (1 + 0.1 * math.sin(i / 20 + k)), 1]
                               for i in range(1500)]
    r = simulate_portfolio(cfg, series)
    assert r.trades > 5 and r.max_invested <= 3000 + 1e-6
    assert len(r.equity) == 1500


def test_scenarios_are_built_as_described():
    from bot.scenarios import DEFAULT_COINS, build_scenarios

    raw = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    sc = {s.key: parse_config(s.raw) for s in build_scenarios(raw, DEFAULT_COINS)}
    assert sc["A"].default_strategy.safety_step_pct == raw["strategy"]["safety_step_pct"]
    assert sc["A"].max_open_positions == raw["max_open_positions"] and not sc["A"].entry_filter.active
    assert sc["B"].default_strategy.safety_step_pct == 4
    assert "APE" not in sc["C"].coins and "PEPE" not in sc["C"].coins and len(sc["C"].coins) == 13
    assert sc["D"].entry_filter.active and not sc["D"].protection.btc_regime
    assert sc["E"].protection.btc_regime and sc["E"].protection.block_safety_when_risky
    assert sc["F"].protection.max_invested_pct == 50 and sc["F"].protection.circuit_breaker_pct == 3
    assert sc["G"].max_open_positions == 5 and sc["G"].default_strategy.max_spend() == pytest.approx(9980)
    assert sc["G"].strategy_for("ZRO").max_spend() == pytest.approx(9980)
    assert sc["H"].max_open_positions == 5 and not sc["H"].protection.btc_regime
    assert sc["I"].protection.max_invested_pct is None and sc["I"].protection.circuit_breaker_pct == 3
