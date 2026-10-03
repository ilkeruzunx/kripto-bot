"""Küçük yerel web paneli: pozisyonları ve anlık kâr/zararı tarayıcıda gösterir.

python -m bot.web_status ile çalıştırılır (deploy/kripto-bot-web.service bunu yapar).
Botun kendisinden bağımsızdır; sadece state dosyasını okur ve anlık fiyatı borsadan çeker.

Güvenlik: panel varsayılan olarak sadece 127.0.0.1'de dinler (dışarıya açık değildir).
Dışarıdan erişim için SSH port yönlendirmesi kullanılması önerilir (README). Sayfa ve
veri hiçbir zaman diske yazılmaz (sadece bellekte tutulur); diskte sadece .token dosyası
vardır ve 600 izniyle (sadece sahibi okuyabilir) oluşturulur. "/" ve "/status.json"
dışındaki her yol 404 döner, böylece .token veya başka bir dosya asla servis edilmez.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .__main__ import ROOT, api_keys, setup_logging, state_path
from .config import Config, ConfigError, load_config
from .status import collect_status as _collect_status

log = logging.getLogger(__name__)

WEB_DIR = ROOT / "web"
REFRESH_SECONDS = 20
DEFAULT_PORT = 8787
DEFAULT_HOST = "127.0.0.1"
TOKEN_FILE_MODE = 0o600


def collect_status(cfg: Config) -> dict:
    data = _collect_status(cfg, state_path(cfg), api_keys())
    data["updated_at"] = time.time()
    return data


def load_or_create_token() -> str:
    """Token'ı diskten okur, yoksa üretir. Dosya sadece sahibi okuyabilsin diye 600 izinli."""
    WEB_DIR.mkdir(exist_ok=True)
    token_file = WEB_DIR / ".token"
    if token_file.exists():
        token = token_file.read_text().strip()
        if token:
            return token
    token = secrets.token_urlsafe(16)  # ~22 karakter
    token_file.write_text(token)
    token_file.chmod(TOKEN_FILE_MODE)
    return token


INDEX_HTML_TEMPLATE = """<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kripto Bot Durumu</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, system-ui, sans-serif; background: #0f1115; color: #e6e6e6; margin: 0; padding: 16px; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .sub { color: #9aa0a6; font-size: 13px; margin-bottom: 16px; }
  .summary { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
  .card { background: #1a1d24; border-radius: 10px; padding: 12px 16px; min-width: 140px; flex: 1; }
  .card .label { font-size: 12px; color: #9aa0a6; }
  .card .value { font-size: 20px; font-weight: 600; margin-top: 4px; }
  .pos { color: #3ddc84; }
  .neg { color: #ff5f56; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: right; padding: 8px 6px; border-bottom: 1px solid #262a33; white-space: nowrap; }
  th:first-child, td:first-child { text-align: left; }
  th { color: #9aa0a6; font-weight: 500; }
  tr.waiting { color: #6b7076; }
  tr.blocked { color: #ff5f56; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; background: #262a33; }
  .err { color: #ff5f56; font-size: 13px; margin-bottom: 12px; }
  tr.section td { padding-top: 18px; padding-bottom: 6px; color: #6b7076; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; border-bottom: none; }
  tr.section:first-child td { padding-top: 8px; }
  @media (max-width: 600px) {
    table, thead, tbody, th, td, tr { display: block; }
    thead { display: none; }
    tr { background: #1a1d24; border-radius: 10px; margin-bottom: 10px; padding: 10px; }
    td { border: none; display: flex; justify-content: space-between; padding: 4px 0; }
    td::before { content: attr(data-label); color: #9aa0a6; }
    tr.section { background: none; padding: 0; margin: 14px 0 4px; }
    tr.section td { display: block; padding: 0; }
    tr.section:first-child { margin-top: 4px; }
  }
</style>
</head>
<body>
  <h1>Kripto Bot &mdash; Durum</h1>
  <div class="sub" id="meta">Yükleniyor&hellip;</div>
  <div id="err"></div>
  <div class="summary" id="summary"></div>
  <table>
    <thead>
      <tr><th>Coin</th><th>Durum</th><th>Miktar</th><th>Ort. maliyet</th><th>Güncel fiyat</th><th>Yatırılan</th><th>Anlık K/Z</th><th>Gerçekleşen</th></tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>

<script>
const TOKEN = "__TOKEN__";
function fmt(n, d) {
  if (n === null || n === undefined) return "-";
  const dec = d !== undefined ? d : (Math.abs(n) >= 1000 ? 2 : Math.abs(n) >= 1 ? 4 : 6);
  return Number(n).toLocaleString("tr-TR", {minimumFractionDigits: dec, maximumFractionDigits: dec});
}
function pnlClass(n) { return n > 0 ? "pos" : n < 0 ? "neg" : ""; }

async function refresh() {
  try {
    const res = await fetch(`status.json?key=${TOKEN}&t=${Date.now()}`, {cache: "no-store"});
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    render(data);
  } catch (e) {
    document.getElementById("meta").textContent = "Bağlantı hatası, tekrar deneniyor...";
  }
}

function render(data) {
  const updated = new Date(data.updated_at * 1000).toLocaleTimeString("tr-TR");
  document.getElementById("meta").textContent = `Mod: ${data.mode} · Son güncelleme: ${updated} (20sn'de bir yenilenir)`;
  const errEl = document.getElementById("err");
  if (data.price_error) {
    errEl.textContent = "⚠ Anlık fiyat alınamadı: " + data.price_error;
    errEl.className = "err";
  } else {
    errEl.textContent = "";
    errEl.className = "";
  }

  const totalStr = fmt(data.total_pnl);
  document.getElementById("summary").innerHTML = `
    <div class="card"><div class="label">Açık pozisyonlarda</div><div class="value">${fmt(data.invested)} TL</div></div>
    <div class="card"><div class="label">Anlık K/Z</div><div class="value ${pnlClass(data.unrealized_pnl)}">${data.unrealized_pnl>=0?"+":""}${fmt(data.unrealized_pnl)} TL</div></div>
    <div class="card"><div class="label">Gerçekleşen K/Z</div><div class="value ${pnlClass(data.realized_pnl)}">${data.realized_pnl>=0?"+":""}${fmt(data.realized_pnl)} TL</div></div>
    <div class="card"><div class="label">Toplam</div><div class="value ${pnlClass(data.total_pnl)}">${data.total_pnl>=0?"+":""}${totalStr} TL</div></div>
  `;

  document.getElementById("rows").innerHTML = renderRows(data.rows);
}

function rowHtml(r) {
  const cls = r.blocked ? "blocked" : (r.status === "bekliyor" ? "waiting" : "");
  const pct = r.unrealized_pct === null || r.unrealized_pct === undefined ? "" : ` (${r.unrealized_pct>=0?"+":""}${fmt(r.unrealized_pct,1)}%)`;
  return `<tr class="${cls}">
      <td data-label="Coin"><b>${r.coin}</b></td>
      <td data-label="Durum"><span class="badge">${r.blocked ? "durduruldu" : r.status}</span></td>
      <td data-label="Miktar">${r.qty ? fmt(r.qty, 6) : "-"}</td>
      <td data-label="Ort. maliyet">${r.avg_cost ? fmt(r.avg_cost) : "-"}</td>
      <td data-label="Güncel fiyat">${r.price ? fmt(r.price) : "-"}</td>
      <td data-label="Yatırılan">${r.invested ? fmt(r.invested) + " TL" : "-"}</td>
      <td class="${pnlClass(r.unrealized)}" data-label="Anlık K/Z">${r.price ? (r.unrealized>=0?"+":"") + fmt(r.unrealized) + " TL" + pct : "-"}</td>
      <td class="${pnlClass(r.realized_pnl)}" data-label="Gerçekleşen">${r.realized_pnl ? (r.realized_pnl>=0?"+":"") + fmt(r.realized_pnl) + " TL" : "-"}</td>
    </tr>`;
}

function sectionRow(label) {
  return `<tr class="section"><td colspan="8">${label}</td></tr>`;
}

function renderRows(allRows) {
  // Fiyatı gelen (açık pozisyonu olan) coinleri kârdan zarara ayır; fiyatı olmayanlar (bekleyen) en altta.
  const withPrice = allRows.filter(r => r.price !== null && r.price !== undefined);
  const waiting = allRows.filter(r => r.price === null || r.price === undefined);
  const profit = withPrice.filter(r => r.unrealized > 0).sort((a, b) => b.unrealized - a.unrealized);
  const loss = withPrice.filter(r => r.unrealized <= 0).sort((a, b) => a.unrealized - b.unrealized);

  let html = "";
  if (profit.length) html += sectionRow("Kârda") + profit.map(rowHtml).join("");
  if (loss.length) html += sectionRow("Zararda (en çoktan en aza)") + loss.map(rowHtml).join("");
  if (waiting.length) html += sectionRow("Bekliyor") + waiting.map(rowHtml).join("");
  return html;
}

refresh();
setInterval(refresh, 20000);
</script>
</body>
</html>
"""


def run(cfg: Config, port: int = DEFAULT_PORT, host: str = DEFAULT_HOST) -> None:
    token = load_or_create_token()
    index_body = INDEX_HTML_TEMPLATE.replace("__TOKEN__", token).encode("utf-8")

    state_lock = threading.Lock()
    status_body = [json.dumps(collect_status_safe(cfg), ensure_ascii=False).encode("utf-8")]

    def loop() -> None:
        while True:
            time.sleep(REFRESH_SECONDS)
            body = json.dumps(collect_status_safe(cfg), ensure_ascii=False).encode("utf-8")
            with state_lock:
                status_body[0] = body

    threading.Thread(target=loop, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def _write(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _forbidden(self) -> None:
            self._write(403, b"Forbidden", "text/plain; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802 (http.server imzası)
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            provided = qs.get("key", [""])[0]
            authorized = secrets.compare_digest(provided, token)

            if parsed.path == "/":
                if not authorized:
                    self._forbidden()
                    return
                self._write(200, index_body, "text/html; charset=utf-8")
            elif parsed.path == "/status.json":
                if not authorized:
                    self._forbidden()
                    return
                with state_lock:
                    body = status_body[0]
                self._write(200, body, "application/json; charset=utf-8")
            else:
                # Diskte başka hiçbir dosya (örn. .token) servis edilmez.
                self._write(404, b"Not Found", "text/plain; charset=utf-8")

        def log_message(self, *args) -> None:  # günlükleri sessize al
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    log.info("Web paneli %s:%s adresinde başladı (token .token dosyasında)", host, port)
    if host not in ("127.0.0.1", "localhost"):
        log.warning("Panel 127.0.0.1 dışında bir adreste dinliyor (%s) — bilerek mi açtın?", host)
    server.serve_forever()


def collect_status_safe(cfg: Config) -> dict:
    try:
        return collect_status(cfg)
    except Exception:
        log.exception("Durum hesaplanamadı")
        return {
            "mode": cfg.mode,
            "price_error": "durum hesaplanamadı",
            "rows": [],
            "invested": 0.0,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "total_pnl": 0.0,
            "budget": cfg.total_budget,
            "updated_at": time.time(),
        }


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m bot.web_status")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="Dinlenecek adres. Varsayılan 127.0.0.1 (dışarıya kapalı). "
        "Dışarı açmak için 0.0.0.0 ver ve bunun güvenlik anlamını bil.",
    )
    args = p.parse_args()
    setup_logging(False)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config.yaml hatası: {e}")
        return 2
    run(cfg, args.port, args.host)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
