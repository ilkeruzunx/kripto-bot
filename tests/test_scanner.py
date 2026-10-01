import pytest

from bot.config import parse_config
from bot.scanner import format_scan, scan

DAY = 86_400_000


def cfg(**sel):
    s = {"mode": "scan", "max_coins": 4, "min_volume_try": 1_000_000, "max_spread_pct": 0.5,
         "min_age_days": 30, "min_volatility_pct": 1, "max_volatility_pct": 8}
    s.update(sel)
    return parse_config({"coins": ["BTC"], "total_budget": 1e6, "selection": s, "max_open_positions": 4})


def series(n, start, daily_growth, wiggle):
    out, p = [], start
    for i in range(n):
        p *= 1 + daily_growth
        out.append([i * DAY, 0, 0, 0, p * (1 + (wiggle if i % 2 else -wiggle)), 0])
    return out


class Market:
    def __init__(self):
        self.tickers = {}
        self.candles = {}

    def add(self, coin, volume_try, spread_pct, candles, last=100.0):
        half = last * spread_pct / 200
        self.tickers[f"{coin}/TRY"] = {"last": last, "bid": last - half, "ask": last + half,
                                       "baseVolume": volume_try / last, "quoteVolume": None}
        self.candles[f"{coin}/TRY"] = candles

    def all_tickers(self):
        return self.tickers

    def active_symbols(self):
        return set(self.tickers) | {"OLD/TRY"}

    def ohlcv(self, symbol, tf, n):
        return self.candles[symbol][-n:]


@pytest.fixture
def market():
    m = Market()
    m.add("BTC", 5e9, 0.05, series(90, 100, 0.001, 0.02))
    m.add("GOOD", 5e7, 0.1, series(90, 100, 0.006, 0.02))      # BTC'den güçlü, trendde
    m.add("OK", 5e7, 0.1, series(90, 100, 0.002, 0.02))        # biraz güçlü
    m.add("DOWN", 5e7, 0.1, series(90, 100, -0.005, 0.02))     # düşüş trendi
    m.add("THIN", 1e5, 0.1, series(90, 100, 0.01, 0.02))       # düşük hacim
    m.add("WIDE", 5e7, 2.0, series(90, 100, 0.01, 0.02))       # geniş fark
    m.add("NEW", 5e7, 0.1, series(10, 100, 0.03, 0.02))        # yeni listelenmiş
    m.add("WILD", 5e7, 0.1, series(90, 100, 0.004, 0.15))      # aşırı oynak
    m.add("FLAT", 5e7, 0.1, series(90, 100, 0.001, 0.001))     # durgun
    m.add("USDT", 9e9, 0.01, series(90, 40, 0, 0.001))         # stabil
    m.tickers["ETH/USDT"] = m.tickers["GOOD/TRY"]                # başka kotasyon: yok sayılır
    return m


def test_scan_filters_and_ranks(market):
    r = scan(cfg(), market)
    reasons = {row.coin: row.rejected for row in r.rows}
    assert reasons["THIN"] == "düşük hacim"
    assert reasons["WIDE"] == "geniş alış-satış farkı"
    assert reasons["NEW"].startswith("yeni listelenmiş")
    assert reasons["DOWN"] == "trend yok"
    assert reasons["WILD"] == "çok oynak"
    assert reasons["FLAT"] == "çok durgun"
    assert reasons["USDT"] == "kara liste / stabil"
    assert r.selected == ["BTC", "GOOD", "OK"]  # sabit coin önce, sonra puana göre
    good = next(x for x in r.rows if x.coin == "GOOD")
    ok = next(x for x in r.rows if x.coin == "OK")
    assert good.score > ok.score > 0


def test_scan_respects_max_coins_and_blacklist(market):
    assert scan(cfg(max_coins=2), market).selected == ["BTC", "GOOD"]
    assert scan(cfg(blacklist=["GOOD"]), market).selected == ["BTC", "OK"]


def test_trend_requirement_can_be_disabled(market):
    assert "DOWN" in scan(cfg(require_trend=False, max_coins=10), market).selected


def test_format_scan_runs(market):
    text = format_scan(scan(cfg(), market))
    assert "SEÇİLDİ" in text and "İzleme listesi (3): BTC, GOOD, OK" in text
