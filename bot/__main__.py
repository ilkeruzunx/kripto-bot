"""Komut satırı: python -m bot <komut>"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

from .config import Config, ConfigError, load_config, planned_spend
from .exchange import LiveBroker, MarketData, PaperBroker, make_client
from .notify import Notifier
from .state import StateStore
from .strategy import CoinState

ROOT = Path.cwd()


def setup_logging(verbose: bool) -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file = RotatingFileHandler(ROOT / "logs" / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(file)
    root.addHandler(console)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def state_path(cfg: Config) -> Path:
    return ROOT / "state" / f"{cfg.mode}.json"


def api_keys() -> tuple[str | None, str | None]:
    return os.getenv("BTCTURK_API_KEY") or None, os.getenv("BTCTURK_API_SECRET") or None


def paper_broker(cfg: Config, states: dict[str, CoinState]) -> PaperBroker:
    """Kayıtlı sanal pozisyonlardan sanal cüzdanı yeniden kurar."""
    realized = sum(s.realized_pnl for s in states.values())
    broker = PaperBroker(cfg.total_budget + realized, cfg.fee)
    for coin, st in states.items():
        if st.position:
            broker.cash -= st.position.cost
            broker.holdings[cfg.symbol(coin)] = st.position.qty
    return broker


def cmd_check(cfg: Config, _args) -> int:
    key, secret = api_keys()
    client = make_client(cfg, key, secret)
    market = MarketData(client)
    prices = market.prices([cfg.symbol(c) for c in cfg.coins if market.has_symbol(cfg.symbol(c))])
    ok = True
    print(f"Mod: {cfg.mode} | Toplam bütçe: {cfg.total_budget:,.0f} TL | Komisyon: %{cfg.fee_pct}")
    if cfg.scanning:
        print(f"Seçim: tarama — aşağıdaki coinler hep izlenir, kalan yerler (toplam {cfg.selection.max_coins}) "
              "taramayla dolar. Taramayı görmek için: python -m bot scan")
    print(f"En fazla açık pozisyon: {cfg.max_open_positions}\n")
    print(f"{'Coin':<7}{'Parite':<12}{'Fiyat':>14}{'Min emir TL':>13}{'En fazla TL':>13}")
    for coin in cfg.coins:
        sym = cfg.symbol(coin)
        if not market.has_symbol(sym):
            print(f"{coin:<7}{sym:<12}{'BORSADA YOK':>14}")
            ok = False
            continue
        s = cfg.strategy_for(coin)
        min_cost = market.min_cost(sym)
        warn = "  ⚠ ilk alım min tutarın altında" if s.base_order < min_cost else ""
        print(f"{coin:<7}{sym:<12}{prices.get(sym, 0):>14.6g}{min_cost:>13,.2f}{s.max_spend():>13,.0f}{warn}")
        ok &= not warn
    min_any = max((market.min_cost(cfg.symbol(c)) for c in cfg.coins if market.has_symbol(cfg.symbol(c))), default=0)
    if cfg.scanning and cfg.default_strategy.base_order < min_any:
        print("⚠ Varsayılan ilk alım tutarı bazı coinlerin en düşük emir tutarının altında.")
    print(f"\nAçık pozisyonların tüm kademeleri dolarsa kullanılacak: {planned_spend(cfg):,.0f} TL")

    if key and secret:
        try:
            bal = LiveBroker(client, cfg).quote_balance()
            print(f"API anahtarı çalışıyor. Serbest {cfg.quote} bakiyesi: {bal:,.2f}")
        except Exception as e:
            print(f"API anahtarı ile bakiye okunamadı: {e}")
            ok = False
    else:
        print("API anahtarı yok (.env). Sanal mod için gerekmez.")
    return 0 if ok else 1


def cmd_backtest(cfg: Config, args) -> int:
    from .backtest import format_results, load_ohlcv, simulate

    client = make_client(cfg)
    market = MarketData(client)
    if args.coins:
        coins = [c.strip().upper() for c in args.coins.split(",")]
    elif cfg.scanning:
        from .scanner import scan

        print("Tarama yapılıyor...", flush=True)
        coins = scan(cfg, market).selected
        print(
            f"Bugünkü izleme listesi test ediliyor: {', '.join(coins)}\n"
            "Not: bu coinler BUGÜN iyi göründüğü için seçildi; geçmişte hep böyle değildi. "
            "Sonuçlar gerçekte olacağından iyimser çıkar.\n"
        )
    else:
        coins = cfg.coins
    results = []
    for coin in coins:
        sym = cfg.symbol(coin)
        if not market.has_symbol(sym):
            print(f"{sym} borsada yok, atlanıyor")
            continue
        print(f"{sym} verisi hazırlanıyor ({args.days} gün)...", flush=True)
        candles = load_ohlcv(client, sym, cfg.strategy_for(coin).timeframe, args.days, ROOT / "data")
        if not candles:
            print(f"{sym} için veri yok")
            continue
        results.append(simulate(cfg, coin, candles))
    print()
    print(format_results(results))
    return 0


def cmd_run(cfg: Config, args) -> int:
    from .engine import Engine

    if args.canli != (cfg.mode == "live"):
        print(
            "Gerçek emir için hem config.yaml'da 'mode: live' hem de komutta --canli olmalı.\n"
            "Sanal mod için ikisi de olmamalı."
        )
        return 2
    store = StateStore(state_path(cfg))
    key, secret = api_keys()
    if cfg.mode == "live":
        if not (key and secret):
            print("Canlı mod için .env içinde BTCTURK_API_KEY ve BTCTURK_API_SECRET gerekli.")
            return 2
        client = make_client(cfg, key, secret)
        broker = LiveBroker(client, cfg)
    else:
        client = make_client(cfg)
        broker = paper_broker(cfg, store.load())
    market = MarketData(client)
    missing = [c for c in cfg.coins if not market.has_symbol(cfg.symbol(c))]
    if missing:
        print(f"Borsada olmayan coin(ler): {', '.join(missing)}. config.yaml'dan çıkar.")
        return 2
    scanner = None
    if cfg.scanning:
        from .scanner import scan

        def scanner() -> list[str]:
            return scan(cfg, market).selected

    engine = Engine(cfg, market, broker, store, Notifier(cfg.telegram), scanner=scanner)
    try:
        engine.run_forever()
    except KeyboardInterrupt:
        print("\nDurduruldu.")
    return 0


def cmd_scan(cfg: Config, args) -> int:
    from .scanner import format_scan, scan

    market = MarketData(make_client(cfg))
    print("Tüm TL pariteleri taranıyor (1-2 dakika sürebilir)...\n", flush=True)
    print(format_scan(scan(cfg, market), limit=args.limit))
    if not cfg.scanning:
        print("\nNot: config.yaml'da selection.mode 'fixed'; bot bu listeyi kullanmaz, yalnız 'coins' listesini kullanır.")
    return 0


def cmd_status(cfg: Config, _args) -> int:
    states = StateStore(state_path(cfg)).load()
    if not states:
        print(f"Kayıt yok ({state_path(cfg)}).")
        return 0
    total_pnl = invested = 0.0
    print(f"Mod: {cfg.mode}\n")
    print(f"{'Coin':<7}{'Durum':<16}{'Miktar':>16}{'Ort. maliyet':>14}{'Yatırılan TL':>14}{'Kapanan':>9}{'Gerçekleşen TL':>16}")
    for coin, st in states.items():
        pos = st.position
        state = "DURDURULDU" if st.blocked else (f"açık ({pos.buys} alım)" if pos else "bekliyor")
        print(
            f"{coin:<7}{state:<16}{(pos.qty if pos else 0):>16.8g}{(pos.avg_cost if pos else 0):>14.6g}"
            f"{(pos.cost if pos else 0):>14,.2f}{st.closed_trades:>9}{st.realized_pnl:>+16,.2f}"
        )
        total_pnl += st.realized_pnl
        invested += pos.cost if pos else 0
    print(f"\nAçık pozisyonlarda: {invested:,.2f} TL | Gerçekleşen toplam K/Z: {total_pnl:+,.2f} TL")
    for coin, st in states.items():
        if st.blocked:
            print(f"\n⚠ {coin}: {st.blocked}")
    for coin, st in states.items():
        for h in st.history[-3:]:
            t = datetime.fromtimestamp(h["ts"]).strftime("%Y-%m-%d %H:%M")
            print(f"  {t} {coin} {h['side']} {h['qty']:.8g} @ {h['price']:.6g} {h.get('pnl', 0):+,.2f}")
    return 0


def cmd_unblock(cfg: Config, args) -> int:
    store = StateStore(state_path(cfg))
    states = store.load()
    coin = args.coin.upper()
    st = states.get(coin)
    if not st or not (st.blocked or st.pending_order):
        print(f"{coin} durdurulmuş değil.")
        return 0
    print(f"{coin}: {st.blocked}")
    print("BtcTurk'te bu coin'in son emirlerini ve bakiyesini kontrol ettin mi?")
    if st.position:
        print(f"Botun kaydındaki pozisyon: {st.position.qty:.8g} {coin}, maliyet {st.position.cost:,.2f} TL")
    print("Seçenekler: [d]evam (kayıt doğru), [s]ıfırla (bu coin'in pozisyonunu kayıttan sil), [i]ptal")
    choice = input("> ").strip().lower()
    if choice == "d":
        st.blocked = None
        st.pending_order = None
    elif choice == "s":
        st.blocked = None
        st.pending_order = None
        st.position = None
    else:
        print("Değişiklik yapılmadı.")
        return 0
    store.save(states)
    print("Tamam. Botu yeniden başlat.")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(prog="python -m bot", description="BtcTurk kripto al-sat botu")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true", help="ayrıntılı günlük")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="coinleri, fiyatları, en düşük emir tutarlarını ve API anahtarını kontrol et")
    b = sub.add_parser("backtest", help="geçmiş veriyle test")
    b.add_argument("--days", type=int, default=365)
    b.add_argument("--coins", help="virgülle ayrılmış, ör. BTC,ETH (varsayılan: hepsi)")
    r = sub.add_parser("run", help="botu çalıştır (varsayılan sanal mod)")
    r.add_argument("--canli", action="store_true", help="GERÇEK emir gönder (config'te mode: live da gerekli)")
    sc = sub.add_parser("scan", help="tüm TL paritelerini tara, izleme listesini göster")
    sc.add_argument("--limit", type=int, default=25, help="kaç satır gösterilsin")
    sub.add_parser("status", help="pozisyonları ve kar/zararı göster")
    u = sub.add_parser("unblock", help="durdurulmuş bir coin'i elle kontrol sonrası aç")
    u.add_argument("coin")
    args = p.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config.yaml hatası: {e}")
        return 2
    if args.cmd == "run":
        setup_logging(args.verbose)
    else:
        logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    handler = {"check": cmd_check, "backtest": cmd_backtest, "run": cmd_run, "scan": cmd_scan, "status": cmd_status, "unblock": cmd_unblock}
    return handler[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
