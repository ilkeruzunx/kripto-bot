import math

import ccxt
import pytest

from bot.backtest import simulate
from bot.config import parse_config
from bot.engine import Engine
from bot.exchange import AmbiguousOrderError, LiveBroker, PaperBroker
from bot.state import StateStore
from bot.strategy import CoinState, Position


def make_cfg(**strategy):
    s = dict(rsi_below=None, trend_ema=None, trailing_pct=0, base_order=100, safety_orders=1,
             safety_order=100, safety_step_pct=5, take_profit_pct=3, stop_loss_pct=10,
             cooldown_after_tp_min=0, cooldown_after_sl_min=60)
    s.update(strategy)
    return parse_config({"coins": ["BTC", "ETH"], "total_budget": 400, "fee_pct": 0.2, "strategy": s})


class FakeMarket:
    def __init__(self, prices):
        self.p = prices

    def prices(self, symbols):
        return {s: self.p[s] for s in symbols if s in self.p}

    def closes(self, symbol, timeframe, n):
        return [self.p[symbol]] * n

    def min_cost(self, symbol):
        return 10.0


class Notes:
    def __init__(self):
        self.msgs = []

    def send(self, t):
        self.msgs.append(t)


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


def make_engine(tmp_path, cfg, market, broker=None, store=None):
    broker = broker or PaperBroker(cfg.total_budget, cfg.fee, slippage=0)
    store = store or StateStore(tmp_path / "state.json")
    clock = Clock()
    return Engine(cfg, market, broker, store, Notes(), clock=clock), broker, clock


def test_full_cycle_buy_safety_take_profit(tmp_path):
    cfg = make_cfg()
    m = FakeMarket({"BTC/TRY": 100.0})
    e, broker, _ = make_engine(tmp_path, cfg, m)

    e.tick()
    pos = e.states["BTC"].position
    assert pos.buys == 1 and pos.cost == pytest.approx(100)
    assert e.states.get("ETH") is None or e.states["ETH"].position is None  # fiyatı yok, atlandı

    m.p["BTC/TRY"] = 95.0
    e.tick()
    assert e.states["BTC"].position.buys == 2
    avg = e.states["BTC"].position.avg_cost

    m.p["BTC/TRY"] = avg * 1.03 / (1 - cfg.fee) + 0.01
    e.tick()
    st = e.states["BTC"]
    assert st.position is None
    assert st.closed_trades == 1 and st.wins == 1
    assert st.realized_pnl == pytest.approx(200 * 0.03, rel=0.01)
    assert broker.cash == pytest.approx(400 + st.realized_pnl)
    # durum diske yazıldı
    assert StateStore(tmp_path / "state.json").load()["BTC"].closed_trades == 1


def test_stop_loss_sets_cooldown(tmp_path):
    cfg = make_cfg(safety_orders=0, safety_order=0)
    m = FakeMarket({"BTC/TRY": 100.0})
    e, _, clock = make_engine(tmp_path, cfg, m)
    e.tick()
    m.p["BTC/TRY"] = 80.0
    e.tick()
    st = e.states["BTC"]
    assert st.position is None and st.realized_pnl < 0 and st.wins == 0
    assert st.cooldown_until == pytest.approx(clock.t + 3600)
    e.tick()
    assert e.states["BTC"].position is None  # beklemede tekrar almaz


def test_budget_cap_is_enforced(tmp_path):
    cfg = make_cfg()
    cfg = cfg.__class__(**{**cfg.__dict__, "total_budget": 150})
    m = FakeMarket({"BTC/TRY": 100.0, "ETH/TRY": 10.0})
    e, _, _ = make_engine(tmp_path, cfg, m)
    e.tick()
    assert e.states["BTC"].position is not None
    assert e.states["ETH"].position is None  # 100 + 100 > 150
    assert e.invested() <= 150


def test_below_exchange_minimum_is_skipped(tmp_path):
    cfg = make_cfg(base_order=5, safety_orders=0, safety_order=0)
    e, _, _ = make_engine(tmp_path, cfg, FakeMarket({"BTC/TRY": 100.0}))
    e.tick()
    assert e.states["BTC"].position is None


class AmbiguousBroker(PaperBroker):
    def buy(self, *a, **k):
        raise AmbiguousOrderError("ağ koptu")


def test_ambiguous_order_blocks_coin_and_survives_restart(tmp_path):
    cfg = make_cfg()
    store = StateStore(tmp_path / "state.json")
    e, _, _ = make_engine(tmp_path, cfg, FakeMarket({"BTC/TRY": 100.0}),
                          broker=AmbiguousBroker(400, cfg.fee), store=store)
    e.tick()
    st = e.states["BTC"]
    assert st.blocked and st.pending_order
    assert any("durduruldu" in m for m in e.notifier.msgs)
    e2, _, _ = make_engine(tmp_path, cfg, FakeMarket({"BTC/TRY": 100.0}), store=store)
    e2.tick()
    assert e2.states["BTC"].position is None and e2.states["BTC"].blocked


def test_pending_order_from_crash_blocks_on_startup(tmp_path):
    cfg = make_cfg()
    store = StateStore(tmp_path / "state.json")
    store.save({"BTC": CoinState(pending_order={"side": "buy", "client_id": "x", "ts": 0})})
    e, _, _ = make_engine(tmp_path, cfg, FakeMarket({"BTC/TRY": 100.0}), store=store)
    assert e.states["BTC"].blocked


class RejectingBroker(PaperBroker):
    def buy(self, *a, **k):
        raise ccxt.InsufficientFunds("yok")


def test_rejected_order_does_not_block(tmp_path):
    cfg = make_cfg()
    e, _, _ = make_engine(tmp_path, cfg, FakeMarket({"BTC/TRY": 100.0}), broker=RejectingBroker(400, cfg.fee))
    e.tick()
    st = e.states["BTC"]
    assert st.position is None and not st.blocked and st.pending_order is None


def test_paper_broker_restored_from_state():
    from bot.__main__ import paper_broker

    cfg = make_cfg()
    pos = Position()
    pos.add_buy(1.5, 150, 100, 0)
    b = paper_broker(cfg, {"BTC": CoinState(position=pos, realized_pnl=20), "ETH": CoinState()})
    assert b.cash == pytest.approx(400 + 20 - 150)
    assert b.holdings["BTC/TRY"] == 1.5


def test_backtest_on_synthetic_waves():
    cfg = parse_config({
        "coins": ["BTC"], "total_budget": 10000, "fee_pct": 0.2,
        "strategy": {"rsi_below": 40, "trend_ema": None, "trailing_pct": 1},
    })
    candles = []
    for i in range(3000):
        p = 1000 * (1 + 0.1 * math.sin(i / 40)) * (1 + i / 30000)
        candles.append([i * 3_600_000, p, p, p, p, 1])
    r = simulate(cfg, "BTC", candles)
    assert r.trades > 5
    assert r.pnl > 0
    assert r.budget == cfg.strategy_for("BTC").max_spend()


def test_backtest_crash_hits_stop_loss():
    cfg = parse_config({"coins": ["BTC"], "total_budget": 10000,
                        "strategy": {"rsi_below": None, "trend_ema": None}})
    candles = [[i * 3_600_000, 0, 0, 0, 1000 * (0.99**i), 1] for i in range(300)]
    r = simulate(cfg, "BTC", candles)
    assert r.pnl < 0 and r.trades >= 1 and r.wins == 0


# --- gerçek emir katmanı (sahte ccxt istemcisiyle) ---

class FakeClient:
    def __init__(self, fill_ratio=1.0, fee_currency="TRY"):
        self.fill_ratio = fill_ratio
        self.fee_currency = fee_currency
        self.cancelled = []
        self.orders = []

    def fetch_ticker(self, s):
        return {"ask": 101.0, "bid": 99.0}

    def amount_to_precision(self, s, a):
        return str(math.floor(a * 1e4) / 1e4)

    def fetch_balance(self):
        return {"free": {"TRY": 1000.0, "BTC": 0.5}}

    def create_order(self, symbol, type_, side, qty, price, params):
        self.orders.append((side, qty, price, params))
        return {"id": "42"}

    def fetch_open_orders(self, s):
        return [{"id": "42"}] if self.fill_ratio < 1 and not self.cancelled else []

    def cancel_order(self, oid, s):
        self.cancelled.append(oid)

    def fetch_my_trades(self, s, since=None):
        side, qty, price, _ = self.orders[-1]
        q = qty * self.fill_ratio
        if q == 0:
            return []
        return [{"order": "42", "amount": q, "cost": q * 100, "fee": {"cost": q * 100 * 0.002, "currency": self.fee_currency}},
                {"order": "other", "amount": 99, "cost": 1, "fee": None}]


def live(client):
    cfg = make_cfg()
    return LiveBroker(client, cfg, sleep=lambda s: None, clock=iter(range(0, 10_000, 10)).__next__)


def test_live_buy_uses_limit_above_ask_and_reads_fill():
    c = FakeClient()
    f = live(c).buy("BTC/TRY", 100, 100, "cid")
    side, qty, price, params = c.orders[0]
    assert side == "buy" and price == pytest.approx(101 * 1.003) and params["clientOrderId"] == "cid"
    assert qty * price * (1 + 0.002) <= 100
    assert f.qty == pytest.approx(qty) and f.quote == pytest.approx(qty * 100 * 1.002)


def test_live_sell_capped_by_balance_and_partial_fill_cancelled():
    c = FakeClient(fill_ratio=0.5)
    f = live(c).sell("BTC/TRY", 2.0, 100, "cid")
    side, qty, price, _ = c.orders[0]
    assert side == "sell" and qty == 0.5 and price == pytest.approx(99 * 0.997)
    assert c.cancelled == ["42"]
    assert f.qty == pytest.approx(0.25) and f.quote == pytest.approx(25 * 0.998)


def test_live_network_error_on_submit_is_ambiguous():
    c = FakeClient()

    def boom(*a):
        raise ccxt.NetworkError("timeout")

    c.create_order = boom
    with pytest.raises(AmbiguousOrderError):
        live(c).buy("BTC/TRY", 100, 100, "cid")


def test_all_safety_orders_fill_when_budget_is_exact():
    # 1000 * 1.3**2 kayan noktada 1690.0000000000002 olur; bütçe tam yetmeli.
    cfg = parse_config({"coins": ["BTC"], "total_budget": 4990, "fee_pct": 0.2,
                        "strategy": {"rsi_below": None, "trend_ema": None, "stop_loss_pct": None}})
    candles = [[i * 3_600_000, 0, 0, 0, 1000 * (0.97**i), 1] for i in range(10)]
    from bot.backtest import simulate as sim
    r = sim(cfg, "BTC", candles)
    assert r.open_value > 0
    import bot.backtest as B
    m = B._ReplayMarket("BTC/TRY", candles)
    b = PaperBroker(4990, cfg.fee, 0)
    clock = Clock()
    e = Engine(cfg, m, b, B._MemoryStore(), Notes(), clock=clock)
    for i in range(len(candles)):
        m.i = i
        e.tick()
    assert e.states["BTC"].position.buys == 4
    assert b.cash == pytest.approx(0, abs=0.01)


# --- izleme listesi / tarama ---

def scan_cfg(**extra):
    raw = {"coins": ["BTC"], "total_budget": 400, "fee_pct": 0.2, "max_open_positions": 2,
           "selection": {"mode": "scan", "max_coins": 3},
           "strategy": dict(rsi_below=None, trend_ema=None, trailing_pct=0, base_order=100,
                            safety_orders=1, safety_order=100, cooldown_after_tp_min=0)}
    raw.update(extra)
    return parse_config(raw)


def test_engine_uses_scanned_watchlist_and_respects_max_open(tmp_path):
    cfg = scan_cfg()
    m = FakeMarket({"BTC/TRY": 100.0, "AAA/TRY": 10.0, "BBB/TRY": 5.0})
    e, _, _ = make_engine(tmp_path, cfg, m)
    e.scanner = lambda: ["BTC", "AAA", "BBB"]
    e.tick()
    opened = [c for c, s in e.states.items() if s.position]
    assert e.watchlist == ["BTC", "AAA", "BBB"]
    assert len(opened) == 2  # max_open_positions
    assert any("İzleme listesi" in m for m in e.notifier.msgs)


def test_coin_dropped_from_watchlist_keeps_managing_position(tmp_path):
    cfg = scan_cfg()
    m = FakeMarket({"BTC/TRY": 100.0, "AAA/TRY": 10.0})
    e, _, clock = make_engine(tmp_path, cfg, m)
    e.scanner = lambda: ["BTC", "AAA"]
    e.tick()
    assert e.states["AAA"].position
    # yeni taramada AAA listeden düştü
    e.scanner = lambda: ["BTC"]
    clock.t += 25 * 3600
    m.p["AAA/TRY"] = 9.5  # ek alım seviyesi: listeden düşse de mevcut pozisyon yönetilir
    e.tick()
    assert e.watchlist == ["BTC"]
    assert e.states["AAA"].position.buys == 2
    m.p["AAA/TRY"] = 20.0  # kar al
    e.tick()
    assert e.states["AAA"].position is None and e.states["AAA"].wins == 1
    e.tick()  # listede olmadığı için yeniden açılmaz
    assert e.states["AAA"].position is None


def test_scanner_failure_keeps_old_watchlist(tmp_path):
    cfg = scan_cfg()
    e, _, clock = make_engine(tmp_path, cfg, FakeMarket({"BTC/TRY": 100.0}))

    def boom():
        raise ccxt.NetworkError("x")

    e.scanner = boom
    e.tick()
    assert e.watchlist == ["BTC"] and e.states["BTC"].position
    calls = []
    e.scanner = lambda: calls.append(1) or ["BTC"]
    e.tick()
    assert calls == []  # 30 dk dolmadan tekrar denemez
    clock.t += 1801
    e.tick()
    assert calls == [1]
