"""Kayıtlı pozisyonlardan ve anlık fiyattan durum özeti (anlık + gerçekleşen kâr/zarar) çıkarır.

Hem web paneli (bot.web_status) hem de ileride başka arayüzler bunu kullanır; diske hiçbir şey yazmaz.
"""
from __future__ import annotations

from pathlib import Path

from .config import Config
from .exchange import MarketData, make_client
from .state import StateStore


def collect_status(
    cfg: Config,
    path: str | Path,
    keys: tuple[str | None, str | None] = (None, None),
    market: MarketData | None = None,
) -> dict:
    states = StateStore(path).load()

    open_symbols = [cfg.symbol(c) for c, st in states.items() if st.position]
    prices: dict[str, float] = {}
    price_error = None
    if open_symbols:
        try:
            market = market or MarketData(make_client(cfg, *keys))
            prices = market.prices(open_symbols)
        except Exception as e:  # fiyat alınamasa da kayıtlı durum gösterilsin
            price_error = str(e) or type(e).__name__

    rows = []
    invested = unrealized_total = realized_total = 0.0
    for coin, st in states.items():
        pos = st.position
        price = prices.get(cfg.symbol(coin)) if pos else None
        unrealized = unrealized_pct = None
        if pos and price:
            # Satış komisyonu düşülmüş net değer: şimdi satsak eline geçecek TL.
            unrealized = pos.qty * price * (1 - cfg.fee) - pos.cost
            unrealized_pct = unrealized / pos.cost * 100 if pos.cost else None
            unrealized_total += unrealized
        invested += pos.cost if pos else 0.0
        realized_total += st.realized_pnl
        rows.append(
            {
                "coin": coin,
                "status": f"açık ({pos.buys} alım)" if pos else "bekliyor",
                "blocked": st.blocked,
                "qty": pos.qty if pos else 0.0,
                "avg_cost": pos.avg_cost if pos else 0.0,
                "price": price,
                "invested": pos.cost if pos else 0.0,
                "unrealized": unrealized,
                "unrealized_pct": unrealized_pct,
                "realized_pnl": st.realized_pnl,
                "closed_trades": st.closed_trades,
            }
        )

    return {
        "mode": cfg.mode,
        "price_error": price_error,
        "rows": rows,
        "invested": invested,
        "unrealized_pnl": unrealized_total,
        "realized_pnl": realized_total,
        "total_pnl": unrealized_total + realized_total,
        "budget": cfg.total_budget,
    }
