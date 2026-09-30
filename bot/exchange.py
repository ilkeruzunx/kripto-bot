"""Borsa katmanı: piyasa verisi, sanal (paper) ve gerçek (live) emir."""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import ccxt

from .config import Config

log = logging.getLogger(__name__)


class AmbiguousOrderError(RuntimeError):
    """Emir gönderildi ama sonucu bilinmiyor (ağ hatası vb.). Elle kontrol gerekir."""


@dataclass
class Fill:
    qty: float        # alınan/satılan coin miktarı
    quote: float      # alışta cüzdandan çıkan, satışta cüzdana giren TL (komisyon dahil)
    price: float      # ortalama gerçekleşme fiyatı

    @property
    def empty(self) -> bool:
        return self.qty <= 0


def make_client(cfg: Config, api_key: str | None = None, secret: str | None = None) -> ccxt.Exchange:
    klass = getattr(ccxt, cfg.exchange)
    params: dict = {"enableRateLimit": True, "timeout": 20000}
    if api_key and secret:
        params.update(apiKey=api_key, secret=secret)
    return klass(params)


class MarketData:
    def __init__(self, client: ccxt.Exchange):
        self.client = client
        self._markets: dict | None = None

    @property
    def markets(self) -> dict:
        if self._markets is None:
            self._markets = self.client.load_markets()
        return self._markets

    def has_symbol(self, symbol: str) -> bool:
        return symbol in self.markets

    def prices(self, symbols: list[str]) -> dict[str, float]:
        tickers = self.client.fetch_tickers(symbols)
        return {s: float(t["last"]) for s, t in tickers.items() if s in symbols and t.get("last")}

    def closes(self, symbol: str, timeframe: str, n: int) -> list[float]:
        # ccxt'nin BtcTurk kodu 'since' verilmezse başlangıcı hatalı hesaplıyor; açıkça veriyoruz.
        tf_ms = self.client.parse_timeframe(timeframe) * 1000
        since = self.client.milliseconds() - (n + 1) * tf_ms
        candles = self.client.fetch_ohlcv(symbol, timeframe, since=since, limit=n + 2)
        return [float(c[4]) for c in candles][-n:]

    def min_cost(self, symbol: str) -> float:
        limits = self.markets[symbol].get("limits") or {}
        return float((limits.get("cost") or {}).get("min") or 0)


class Broker:
    def buy(self, symbol: str, amount_quote: float, price: float, client_id: str) -> Fill:
        raise NotImplementedError

    def sell(self, symbol: str, qty: float, price: float, client_id: str) -> Fill:
        raise NotImplementedError

    def quote_balance(self) -> float:
        raise NotImplementedError


class PaperBroker(Broker):
    """Gerçek emir göndermez; komisyon ve kaymayı hesaba katarak doldurur."""

    def __init__(self, cash: float, fee: float, slippage: float = 0.001):
        self.cash = cash
        self.fee = fee
        self.slippage = slippage
        self.holdings: dict[str, float] = {}

    def buy(self, symbol: str, amount_quote: float, price: float, client_id: str) -> Fill:
        amount_quote = min(amount_quote, self.cash)
        if amount_quote <= 0:
            return Fill(0, 0, price)
        fill_price = price * (1 + self.slippage)
        qty = amount_quote / (fill_price * (1 + self.fee))
        self.cash -= amount_quote
        self.holdings[symbol] = self.holdings.get(symbol, 0) + qty
        return Fill(qty, amount_quote, fill_price)

    def sell(self, symbol: str, qty: float, price: float, client_id: str) -> Fill:
        qty = min(qty, self.holdings.get(symbol, 0))
        if qty <= 0:
            return Fill(0, 0, price)
        fill_price = price * (1 - self.slippage)
        proceeds = qty * fill_price * (1 - self.fee)
        self.holdings[symbol] -= qty
        self.cash += proceeds
        return Fill(qty, proceeds, fill_price)

    def quote_balance(self) -> float:
        return self.cash


class LiveBroker(Broker):
    """Gerçek emir. Hemen dolması için en iyi fiyatın biraz içinde limit emir verir,
    dolmayan kısmı zaman aşımında iptal eder ve gerçekleşen işlemleri borsadan okur."""

    def __init__(
        self,
        client: ccxt.Exchange,
        cfg: Config,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self.client = client
        self.cfg = cfg
        self.sleep = sleep
        self.clock = clock
        self.slip = cfg.orders.slippage_pct / 100

    def quote_balance(self) -> float:
        bal = self.client.fetch_balance()
        return float((bal.get("free") or {}).get(self.cfg.quote) or 0)

    def coin_balance(self, coin: str) -> float:
        bal = self.client.fetch_balance()
        return float((bal.get("free") or {}).get(coin) or 0)

    def buy(self, symbol: str, amount_quote: float, price: float, client_id: str) -> Fill:
        ask = float(self.client.fetch_ticker(symbol).get("ask") or price)
        limit = ask * (1 + self.slip)
        qty = float(self.client.amount_to_precision(symbol, amount_quote / (limit * (1 + self.cfg.fee))))
        return self._execute(symbol, "buy", qty, limit, client_id)

    def sell(self, symbol: str, qty: float, price: float, client_id: str) -> Fill:
        coin = symbol.split("/")[0]
        qty = min(qty, self.coin_balance(coin))
        qty = float(self.client.amount_to_precision(symbol, qty))
        bid = float(self.client.fetch_ticker(symbol).get("bid") or price)
        return self._execute(symbol, "sell", qty, bid * (1 - self.slip), client_id)

    def _execute(self, symbol: str, side: str, qty: float, limit: float, client_id: str) -> Fill:
        if qty <= 0:
            return Fill(0, 0, limit)
        started_ms = int(self.clock() * 1000) - 5000
        try:
            order = self.client.create_order(
                symbol, "limit", side, qty, limit, {"clientOrderId": client_id}
            )
        except ccxt.NetworkError as e:
            raise AmbiguousOrderError(f"{symbol} {side} emri gönderilirken ağ hatası: {e}") from e
        order_id = str(order.get("id") or "")
        log.info("%s %s emri verildi: id=%s miktar=%s limit=%s", symbol, side, order_id, qty, limit)

        try:
            deadline = self.clock() + self.cfg.orders.timeout_seconds
            while True:
                open_ids = {str(o.get("id")) for o in self.client.fetch_open_orders(symbol)}
                if order_id not in open_ids:
                    break
                if self.clock() >= deadline:
                    log.info("%s emri zaman aşımı, iptal ediliyor", order_id)
                    self.client.cancel_order(order_id, symbol)
                    self.sleep(2)
                    break
                self.sleep(2)
            return self._fill_from_trades(symbol, side, order_id, started_ms, limit)
        except ccxt.NetworkError as e:
            raise AmbiguousOrderError(f"{symbol} {side} emrinin ({order_id}) sonucu okunamadı: {e}") from e

    def _fill_from_trades(self, symbol: str, side: str, order_id: str, since_ms: int, limit: float) -> Fill:
        coin, quote = symbol.split("/")
        trades = [t for t in self.client.fetch_my_trades(symbol, since=since_ms) if str(t.get("order")) == order_id]
        qty = sum(float(t["amount"]) for t in trades)
        gross = sum(float(t["cost"]) for t in trades)
        fee_quote = fee_coin = 0.0
        for t in trades:
            fee = t.get("fee") or {}
            cost = float(fee.get("cost") or 0)
            if fee.get("currency") == coin:
                fee_coin += cost
            else:
                fee_quote += cost
        if qty <= 0:
            return Fill(0, 0, limit)
        if side == "buy":
            return Fill(qty - fee_coin, gross + fee_quote, gross / qty)
        return Fill(qty + fee_coin, gross - fee_quote, gross / qty)


def new_client_id() -> str:
    return uuid.uuid4().hex
