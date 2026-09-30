import math

import pytest

from bot.config import ConfigError, StrategyConfig, load_config, parse_config
from bot.indicators import ema, rsi
from bot.strategy import Action, CoinState, Position, Strategy

FEE = 0.002


def cfg(**kw) -> StrategyConfig:
    base = dict(rsi_below=None, trend_ema=None, trailing_pct=0)
    base.update(kw)
    return StrategyConfig(**base)


def open_state(qty=1.0, cost=100.0, price=100.0, buys=1) -> CoinState:
    pos = Position()
    pos.add_buy(qty, cost, price, 0)
    pos.buys = buys
    return CoinState(position=pos)


# --- göstergeler ---

def test_ema_constant_series():
    assert ema([5.0] * 50, 10) == pytest.approx(5.0)
    assert ema([1.0] * 3, 10) is None


def test_rsi_extremes():
    assert rsi(list(range(1, 40)), 14) == 100.0
    assert rsi(list(range(40, 1, -1)), 14) == pytest.approx(0.0)
    assert rsi([1.0] * 5, 14) is None


# --- giriş ---

def test_unconditional_entry_buys_base_order():
    s = Strategy(cfg(base_order=500), FEE)
    d = s.decide(CoinState(), 100, [100.0] * 50, now=0)
    assert d.action == Action.BUY_BASE and d.amount_try == 500


def test_rsi_filter_blocks_entry_on_uptrend_and_allows_on_dip():
    s = Strategy(cfg(rsi_below=40), FEE)
    up = [100 + i for i in range(60)]
    down = [200 - i for i in range(60)]
    assert s.decide(CoinState(), up[-1], up, 0).action == Action.HOLD
    assert s.decide(CoinState(), down[-1], down, 0).action == Action.BUY_BASE


def test_trend_filter_requires_price_above_ema():
    s = Strategy(cfg(trend_ema=20), FEE)
    down = [200 - i for i in range(60)]
    up = [100 + i for i in range(60)]
    assert s.decide(CoinState(), down[-1], down, 0).action == Action.HOLD
    assert s.decide(CoinState(), up[-1], up, 0).action == Action.BUY_BASE


def test_not_enough_candles_holds():
    s = Strategy(cfg(trend_ema=200), FEE)
    assert s.decide(CoinState(), 100, [100.0] * 50, 0).action == Action.HOLD


def test_cooldown_blocks_entry():
    s = Strategy(cfg(), FEE)
    st = CoinState(cooldown_until=1000)
    assert s.decide(st, 100, [100.0] * 50, now=999).action == Action.HOLD
    assert s.decide(st, 100, [100.0] * 50, now=1000).action == Action.BUY_BASE


def test_blocked_coin_never_trades():
    s = Strategy(cfg(), FEE)
    st = open_state()
    st.blocked = "x"
    assert s.decide(st, 1, [], 0).action == Action.HOLD
    assert s.decide(st, 1000, [], 0).action == Action.HOLD


# --- kademeli alım ---

def test_safety_orders_trigger_on_drop_and_scale():
    c = cfg(safety_orders=2, safety_order=100, safety_volume_scale=2, safety_step_pct=5, stop_loss_pct=None)
    s = Strategy(c, FEE)
    st = open_state(price=100)
    assert s.decide(st, 95.1, [], 0).action == Action.HOLD
    d = s.decide(st, 95, [], 0)
    assert d.action == Action.BUY_SAFETY and d.amount_try == 100
    st.position.add_buy(1, 95, 95, 0)
    d = s.decide(st, 90.25, [], 0)
    assert d.action == Action.BUY_SAFETY and d.amount_try == 200
    st.position.add_buy(1, 90, 90, 0)
    # tüm ek alımlar kullanıldı
    assert s.decide(st, 50, [], 0).action == Action.HOLD


def test_max_spend():
    c = cfg(base_order=1000, safety_orders=3, safety_order=1000, safety_volume_scale=1.3)
    assert c.max_spend() == pytest.approx(1000 + 1000 + 1300 + 1690)


# --- çıkış ---

def test_take_profit_is_net_of_fees():
    s = Strategy(cfg(take_profit_pct=3), FEE)
    st = open_state(qty=1, cost=100)
    target = 100 * 1.03 / (1 - FEE)
    assert s.decide(st, target * 0.999, [], 0).action == Action.HOLD
    assert s.decide(st, target, [], 0).action == Action.SELL_TP
    # hedefte satınca net kar gerçekten %3
    assert target * (1 - FEE) - 100 == pytest.approx(3)


def test_trailing_take_profit_follows_peak():
    s = Strategy(cfg(take_profit_pct=3, trailing_pct=2), FEE)
    st = open_state(qty=1, cost=100)
    target = s.target_price(st.position)
    assert s.decide(st, target + 1, [], 0).action == Action.HOLD  # takip başladı
    assert s.decide(st, 120, [], 0).action == Action.HOLD          # yeni zirve
    assert s.decide(st, 118, [], 0).action == Action.HOLD          # zirveden %1.7 düşüş
    d = s.decide(st, 117.6, [], 0)                                 # %2 düşüş
    assert d.action == Action.SELL_TP


def test_trailing_sells_if_price_falls_back_below_target():
    s = Strategy(cfg(take_profit_pct=3, trailing_pct=5), FEE)
    st = open_state(qty=1, cost=100)
    target = s.target_price(st.position)
    s.decide(st, target + 0.5, [], 0)
    assert s.decide(st, target - 0.01, [], 0).action == Action.SELL_TP


def test_stop_loss():
    s = Strategy(cfg(stop_loss_pct=10, safety_orders=0), FEE)
    st = open_state(qty=1, cost=100)
    assert s.decide(st, 90.01, [], 0).action == Action.HOLD
    assert s.decide(st, 90, [], 0).action == Action.SELL_SL


def test_stop_loss_disabled():
    s = Strategy(cfg(stop_loss_pct=None, safety_orders=0), FEE)
    assert s.decide(open_state(), 1, [], 0).action == Action.HOLD


# --- config ---

def test_repo_config_is_valid_and_within_budget():
    c = load_config("config.yaml")
    assert c.mode == "paper"
    assert sum(s.max_spend() for s in c.strategies.values()) <= c.total_budget
    assert c.strategies["TRUMP"].stop_loss_pct == 18
    assert c.strategies["BTC"].stop_loss_pct == 12


def test_config_rejects_over_budget():
    with pytest.raises(ConfigError, match="yetmiyor"):
        parse_config({"coins": ["BTC", "ETH"], "total_budget": 1000, "strategy": {"base_order": 1000, "safety_orders": 0}})


def test_config_rejects_unknown_fields_and_stray_overrides():
    with pytest.raises(ConfigError, match="bilinmeyen"):
        parse_config({"coins": ["BTC"], "total_budget": 1e6, "strategy": {"take_proft_pct": 3}})
    with pytest.raises(ConfigError, match="olmayan coin"):
        parse_config({"coins": ["BTC"], "total_budget": 1e6, "overrides": {"ETH": {}}})


def test_config_rejects_live_typos():
    with pytest.raises(ConfigError):
        parse_config({"mode": "canli", "coins": ["BTC"], "total_budget": 1e6})


def test_state_roundtrip(tmp_path):
    from bot.state import StateStore

    store = StateStore(tmp_path / "s.json")
    st = open_state(qty=0.5, cost=50)
    st.realized_pnl = 12.5
    store.save({"BTC": st, "ETH": CoinState()})
    loaded = store.load()
    assert loaded["BTC"].position.avg_cost == pytest.approx(100)
    assert loaded["BTC"].realized_pnl == 12.5
    assert loaded["ETH"].position is None
    assert not math.isnan(loaded["BTC"].position.qty)
