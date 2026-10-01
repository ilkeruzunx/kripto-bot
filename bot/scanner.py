"""Coin tarayıcı: tüm TL paritelerini filtreler, puanlar ve izleme listesini seçer.

1. Eleme: düşük hacim, geniş alış-satış farkı, yeni listelenmiş, stabil coin, kara liste.
2. Puan: son 30 ve 7 günde BTC'ye göre performans (göreli güç).
3. Şart: fiyat 50 günlük ortalamanın üstünde (trend) ve günlük oynaklık makul aralıkta.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Config
from .indicators import ema

log = logging.getLogger(__name__)

DAILY_CANDLES = 90
TREND_EMA_DAYS = 50
# Mum verisi indirilecek en fazla aday (en yüksek hacimliler); borsayı yormamak için.
MAX_CANDIDATES = 40


@dataclass
class CoinScore:
    coin: str
    volume_try: float
    spread_pct: float
    age_days: int = 0
    ret7: float = 0.0
    ret30: float = 0.0
    volatility_pct: float = 0.0
    trend: bool = False
    score: float = 0.0
    rejected: str = ""   # boşsa uygun


@dataclass
class ScanResult:
    selected: list[str]
    rows: list[CoinScore]


def _pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b else 0.0


def ticker_rows(cfg: Config, tickers: dict[str, dict], active_symbols: set[str]) -> list[CoinScore]:
    """Fiyat verisinden ilk eleme (mum indirmeden)."""
    rows: list[CoinScore] = []
    suffix = f"/{cfg.quote}"
    sel = cfg.selection
    for symbol, t in tickers.items():
        if not symbol.endswith(suffix) or symbol not in active_symbols:
            continue
        coin = symbol[: -len(suffix)]
        last = float(t.get("last") or 0)
        bid, ask = float(t.get("bid") or 0), float(t.get("ask") or 0)
        volume = float(t.get("quoteVolume") or 0) or float(t.get("baseVolume") or 0) * last
        spread = (ask - bid) / ((ask + bid) / 2) * 100 if bid > 0 and ask > 0 else 999.0
        row = CoinScore(coin, volume, spread)
        if cfg.excluded(coin):
            row.rejected = "kara liste / stabil"
        elif last <= 0:
            row.rejected = "fiyat yok"
        elif volume < sel.min_volume_try:
            row.rejected = "düşük hacim"
        elif spread > sel.max_spread_pct:
            row.rejected = "geniş alış-satış farkı"
        rows.append(row)
    return rows


def score_row(cfg: Config, row: CoinScore, closes: list[float], btc_ret7: float, btc_ret30: float) -> None:
    """Günlük kapanışlardan puan ve son şartlar."""
    sel = cfg.selection
    row.age_days = len(closes)
    if len(closes) < max(sel.min_age_days, 31):
        row.rejected = "yeni listelenmiş / yetersiz veri"
        return
    row.ret7 = _pct(closes[-1], closes[-8])
    row.ret30 = _pct(closes[-1], closes[-31])
    daily = [abs(_pct(closes[i], closes[i - 1])) for i in range(len(closes) - 30, len(closes))]
    row.volatility_pct = sum(daily) / len(daily)
    e = ema(closes, min(TREND_EMA_DAYS, len(closes)))
    row.trend = e is not None and closes[-1] > e
    row.score = (row.ret30 - btc_ret30) + 0.5 * (row.ret7 - btc_ret7)
    if sel.require_trend and not row.trend:
        row.rejected = "trend yok"
    elif row.volatility_pct < sel.min_volatility_pct:
        row.rejected = "çok durgun"
    elif row.volatility_pct > sel.max_volatility_pct:
        row.rejected = "çok oynak"


def pick(cfg: Config, rows: list[CoinScore]) -> list[str]:
    """Sabit coinler + kalan yerlere en yüksek puanlılar."""
    fixed = [c for c in cfg.coins if not cfg.excluded(c)]
    room = cfg.selection.max_coins - len(fixed)
    ranked = sorted((r for r in rows if not r.rejected and r.coin not in fixed), key=lambda r: r.score, reverse=True)
    return fixed + [r.coin for r in ranked[: max(room, 0)]]


def scan(cfg: Config, market) -> ScanResult:
    """`market`: MarketData (all_tickers, active_symbols, ohlcv)."""
    rows = ticker_rows(cfg, market.all_tickers(), market.active_symbols())
    candidates = sorted((r for r in rows if not r.rejected), key=lambda r: r.volume_try, reverse=True)
    for r in candidates[MAX_CANDIDATES:]:
        r.rejected = "hacim sıralamasında geride"

    btc = [float(c[4]) for c in market.ohlcv(cfg.symbol("BTC"), "1d", DAILY_CANDLES)]
    btc_ret7 = _pct(btc[-1], btc[-8]) if len(btc) >= 8 else 0.0
    btc_ret30 = _pct(btc[-1], btc[-31]) if len(btc) >= 31 else 0.0

    for r in candidates[:MAX_CANDIDATES]:
        try:
            closes = [float(c[4]) for c in market.ohlcv(cfg.symbol(r.coin), "1d", DAILY_CANDLES)]
        except Exception as e:  # tek coin'in verisi gelmezse taramayı bozmasın
            log.warning("%s günlük verisi alınamadı: %s", r.coin, e)
            r.rejected = "veri alınamadı"
            continue
        score_row(cfg, r, closes, btc_ret7, btc_ret30)
    return ScanResult(pick(cfg, rows), rows)


def format_scan(result: ScanResult, limit: int = 25) -> str:
    chosen = set(result.selected)
    rows = sorted(
        (r for r in result.rows if r.age_days or r.coin in chosen),
        key=lambda r: (r.coin not in chosen, -r.score),
    )
    lines = [
        f"{'':2}{'Coin':<8}{'Hacim (mn TL)':>14}{'Fark%':>7}{'7g%':>8}{'30g%':>8}{'Oynak%':>8}{'Trend':>7}{'Puan':>8}  Durum",
        "-" * 92,
    ]
    for r in rows[:limit]:
        mark = "✓ " if r.coin in chosen else "  "
        status = "SEÇİLDİ" if r.coin in chosen else r.rejected or "yer kalmadı"
        lines.append(
            f"{mark}{r.coin:<8}{r.volume_try / 1e6:>14,.1f}{r.spread_pct:>7.2f}{r.ret7:>+8.1f}{r.ret30:>+8.1f}"
            f"{r.volatility_pct:>8.1f}{('evet' if r.trend else 'hayır'):>7}{r.score:>+8.1f}  {status}"
        )
    counts: dict[str, int] = {}
    for r in result.rows:
        if r.rejected and not r.age_days:
            counts[r.rejected] = counts.get(r.rejected, 0) + 1
    if counts:
        lines.append("\nÖn elemede çıkanlar: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    lines.append(f"\nİzleme listesi ({len(result.selected)}): {', '.join(result.selected)}")
    lines.append("Puan = BTC'ye göre 30 günlük fark + 7 günlük farkın yarısı.")
    return "\n".join(lines)
