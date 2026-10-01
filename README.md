# kripto-bot

BtcTurk üzerinde çalışan kademeli alım (DCA) + kar al + zarar durdur botu.

> ⚠️ Bu bot kâr garantisi vermez. Kripto piyasası çok oynaktır; kaybetmeyi göze
> alamayacağın parayı koyma. Önce **geçmiş test**, sonra **sanal mod**, en son
> **küçük tutarla canlı mod**.

## Nasıl çalışır?

### 1. Hangi coinler? (tarama)

`config.yaml` → `selection.mode: scan` (varsayılan) iken bot her 24 saatte bir
BtcTurk'teki **tüm TL paritelerini** tarar:

- **Eler:** 24 saatlik hacmi 20 milyon TL altı, alış-satış farkı %0,3 üstü,
  30 günden yeni, stabil coin (USDT, USDC…) ve kara listedekiler.
- **Şart koyar:** fiyat 50 günlük ortalamanın üstünde (yükselen trend), günlük
  ortalama hareket %1,5–8 arası (ne durgun ne aşırı oynak).
- **Puanlar:** son 30 ve 7 günde BTC'den ne kadar iyi gittiği (göreli güç).
- **Seçer:** `coins` listesi (BTC, ETH, SOL, XRP) hep izlenir; kalan yerler
  (toplam 12) en yüksek puanlılarla dolar.

Aynı anda en fazla **10 pozisyon** açılır (10 × 4.990 = 49.900 TL). Listeden düşen
bir coinde açık pozisyon varsa zorla satılmaz; kendi kar/stop kuralıyla kapanır.
Yalnız kendi listenle çalışmak için `selection.mode: fixed` yap.

```bash
python -m bot scan    # bugün hangi coinler seçilir, hangileri neden elendi
```

### 2. Al-sat kuralları

Her coin için ayrı ayrı:

1. **İlk alım** — RSI düşükse (varsayılan < 40) ve fiyat 200 saatlik ortalamanın
   üstündeyse (yükselen trendde geri çekilme) `base_order` kadar TL'lik alır.
2. **Ek alımlar** — fiyat son alımdan `safety_step_pct` (%4) düşerse
   `safety_order` kadar daha alır. Her ek alım bir öncekinin 1.3 katı; en fazla 3 kez.
   Böylece ortalama maliyet düşer.
3. **Kar al** — fiyat, komisyonlar düştükten sonra `take_profit_pct` (%3) net kar
   bırakacak seviyeyi geçince takibe başlar. Zirveden `trailing_pct` (%1) düşünce satar.
4. **Zarar durdur** — fiyat ortalama maliyetin `stop_loss_pct` (%12) altına inerse
   hepsini satar ve 24 saat o coin'e girmez.

Varsayılan ayarlarla her coin'e en fazla 1000 + 1000 + 1300 + 1690 = **4.990 TL**,
en fazla 10 açık pozisyonla toplam **49.900 TL** gider. Bot `total_budget` (50.000 TL)
sınırını asla aşmaz. Oynak coinler (TRUMP, LAYER, SPK, ZRO, ENA) taramayla seçilirse
onlar için daha geniş adım ve stop kullanılır. Hepsi `config.yaml` içinde.

## Mac'te kurulum

Terminal'i aç:

```bash
# 1) Python 3.11+ (yoksa)
brew install python@3.12

# 2) Projeyi indir
git clone https://github.com/ilkeruzunx/kripto-bot.git
cd kripto-bot

# 3) Sanal ortam ve paketler
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 4) Ayar dosyası (sanal mod için anahtar gerekmez)
cp .env.example .env
```

## Kullanım

Her yeni terminalde önce `cd kripto-bot && source .venv/bin/activate`.

```bash
python -m bot check        # coinler BtcTurk'te var mı, fiyatlar, en düşük emir tutarı
python -m bot scan         # tarama: bugünkü izleme listesi ve elenme sebepleri
python -m bot backtest     # son 1 yıl geçmiş test (tüm coinler)
python -m bot backtest --days 180 --coins BTC,ETH
python -m bot run          # SANAL modda çalıştır (gerçek emir yok) — Ctrl+C ile durdur
python -m bot status       # pozisyonlar ve kar/zarar
```

Günlükler `logs/bot.log`, durum `state/paper.json` (canlıda `state/live.json`).

### Önerilen sıra

1. `check` → listede **BORSADA YOK** yazan coin'i `config.yaml`'dan çıkar.
2. `scan` → seçilen coinlere bak; çok az coin seçiliyorsa filtreleri gevşet
   (ör. `min_volume_try`), istemediğin coini `blacklist`'e yaz.
3. `backtest` → sonuçları "Al-tut%" sütunuyla karşılaştır. Ayarları değiştirip tekrar dene.
   Aynı veride çok ayar deneyip en iyisini seçmek geleceği garanti etmez (aşırı uyum).
   Tarama modunda test, **bugün** seçilen coinlerle yapılır; bu coinler son dönemde
   iyi gittiği için seçildiğinden sonuç olduğundan iyimser çıkar.
4. `run` → en az **2–4 hafta sanal modda** çalıştır, `status` ile izle.
5. Canlıya geçiş (aşağıda) — önce `total_budget`'ı küçük tut (ör. 5.000 TL, 2–3 coin).

## Canlı mod (gerçek para)

1. BtcTurk → Hesabım → **API Erişimi** → yeni anahtar:
   - Yetki: **yalnız işlem (trade)**. **Çekim (withdraw) yetkisi VERME.**
   - IP kısıtlaması: botun çalıştığı makinenin IP'si.
2. Anahtarı `.env` içine yaz (`BTCTURK_API_KEY`, `BTCTURK_API_SECRET`).
   `.env` asla git'e girmez; kimseyle paylaşma.
3. `python -m bot check` → "API anahtarı çalışıyor" görmelisin.
4. BtcTurk'teki komisyon oranını `config.yaml` → `fee_pct` alanına yaz.
5. `config.yaml` → `mode: live`, sonra:

```bash
python -m bot run --canli
```

Güvenlik kilidi: gerçek emir için **hem** `mode: live` **hem** `--canli` gerekir.

### Emirler nasıl verilir?

Bot piyasa emri yerine en iyi fiyatın %0,3 içinde **limit emir** verir (hemen dolar,
ani fiyat sıçramasında fazla ödemeyi önler). 30 saniyede dolmayan kısım iptal edilir.
Gerçekleşen miktar ve komisyon BtcTurk'ün işlem geçmişinden okunur.

### "Durduruldu" uyarısı

Emir gönderilirken bağlantı koparsa bot, emrin gerçekleşip gerçekleşmediğini
bilemez. Çift alım riskine girmemek için **o coin'i durdurur** ve bildirir.
BtcTurk'te o coin'in son emirlerini kontrol et, sonra:

```bash
python -m bot unblock BTC
```

## Telegram bildirimi (isteğe bağlı)

1. Telegram'da **@BotFather** → `/newbot` → token'ı al.
2. Botuna bir mesaj at, sonra tarayıcıda
   `https://api.telegram.org/bot<TOKEN>/getUpdates` aç → `chat.id` değerini al.
3. `.env` içine `TELEGRAM_BOT_TOKEN` ve `TELEGRAM_CHAT_ID` yaz.

Her alım, satış, durdurma ve bot başlangıcında mesaj gelir.

## Sunucuya taşıma (sonra)

Ucuz bir Linux VPS (Ubuntu) yeterli. Kurulum Mac'tekiyle aynı; sürekli çalışması için
`deploy/kripto-bot.service` dosyasındaki talimatları izle. BtcTurk API anahtarındaki
IP kısıtlamasını sunucunun IP'si ile güncelle.

Mac'ten sunucuya geçerken `state/` klasörünü de kopyala (açık pozisyonlar orada).
**Aynı anda iki yerde canlı bot çalıştırma.**

## Geliştirme

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

| Dosya | Görevi |
|---|---|
| `bot/strategy.py` | Al/sat kararları (borsadan bağımsız) |
| `bot/engine.py` | Ana döngü, izleme listesi, bütçe kontrolü, durum kaydı |
| `bot/scanner.py` | TL paritelerini tarama, eleme ve puanlama |
| `bot/exchange.py` | BtcTurk bağlantısı, sanal ve gerçek emir |
| `bot/backtest.py` | Geçmiş test (canlıyla aynı kodu kullanır) |
| `bot/config.py` | `config.yaml` okuma ve doğrulama |
