# 🤖 Aimon

**Aimon** (Aim + monitor/daemon) — bot scalping otomatis untuk Binance
Futures yang menggabungkan indikator teknikal deterministik dengan AI
(Gemini + Groq, dengan failover otomatis) sebagai lapisan konfirmasi
sebelum entry. Namanya mencerminkan apa yang paling banyak bot ini
lakukan: bukan sekadar menembak entry, tapi terus **memantau** posisi
yang sudah terbuka — trailing stop, review tesis berkala, dan stagnant
breaker semuanya berjalan di jadwal independen selama posisi hidup.

Dibangun untuk berjalan di **testnet/paper mode** sebagai fase belajar
sebelum live.

> ⚠️ **Disclaimer**: Ini proyek eksperimen/pembelajaran. Trading futures
> dengan leverage berisiko tinggi — kerugian bisa melebihi modal awal.
> Kode ini TIDAK menjamin profit. Jangan jalankan di mode LIVE dengan
> uang yang tidak siap hilang sepenuhnya.

---

## Arsitektur

Prinsip desain utama: **indikator menemukan kandidat, AI mengonfirmasi
arah, kode (bukan AI) yang menghitung semua angka harga.** AI tidak
pernah dipercaya untuk aritmetika — LLM tidak reliable untuk itu, dan
setiap angka SL/TP/sizing selalu dihitung deterministik oleh
`risk_manager.py`.

```
scanner.py ──► indicators.py ──► [Gerbang Bias] ──► brain.py (AI) ──► risk_manager.py ──► executor.py
     │                                                    │                                    │
     │                                                    ▼                                    ▼
     │                                            ai_providers.py                          memory.py
     │                                          (Gemini→Groq failover)                    (SQLite, single
     │                                                                                    source of truth)
     └────────────────────────── main.py (orkestrator: schedule 3 jadwal berbeda) ──────────────┘
```

### Modul

| File | Tanggung jawab |
|---|---|
| `main.py` | Orkestrator utama — 3 jadwal independen (scan kandidat, monitoring posisi, review tesis), logging berwarna, ringkasan tiap cycle |
| `scanner.py` | Ambil top gainer/loser dari Binance Futures, fetch candle OHLCV, info akun & symbol |
| `indicators.py` | RSI, MACD, Bollinger Bands, volume spike, EMA trend — plus penentuan `bias` (LONG/SHORT/NEUTRAL) dari kombinasi sinyal |
| `brain.py` | Prompt & parsing AI — keputusan entry (`make_decision`), review tesis posisi aktif (`make_thesis_review`), post-mortem belajar dari histori (`perform_post_mortem`) |
| `ai_providers.py` | Layer multi-provider dengan failover otomatis (Gemini → Groq), deteksi rate-limit vs model deprecated, cooldown per-provider |
| `risk_manager.py` | Semua perhitungan angka: position sizing dinamis, TP/SL, trailing stop, stagnant breaker, tighten-SL tervalidasi |
| `executor.py` | Eksekusi order ke Binance (atau simulasi di PAPER mode), hitung PnL aktual |
| `memory.py` | Single source of truth — SQLite (`trade_history`, `scan_log`, `strategy_log`, `daily_summary`) |
| `daily_report.py` | Laporan harian + mode evaluasi eksperimen (breakdown per `close_reason`) |
| `manual_close.py` | Tool CLI terpisah untuk menutup posisi manual tanpa memblokir loop utama |
| `config.py` | Semua parameter tuning — lihat referensi di bawah |

---

## Tech Stack

- **Python 3.10+**
- [`python-binance`](https://github.com/sammchardy/python-binance) — REST client Binance Futures (testnet & live)
- [`pandas`](https://pandas.pydata.org/) — perhitungan indikator dari candle OHLCV
- [`openai`](https://github.com/openai/openai-python) SDK — dipakai sebagai client universal untuk Gemini & Groq (keduanya expose endpoint OpenAI-compatible)
- [`rich`](https://github.com/Textualize/rich) — logging berwarna, tabel ringkasan di terminal
- [`schedule`](https://github.com/dbader/schedule) — penjadwalan 3 loop independen tanpa threading
- `sqlite3` (built-in) — penyimpanan seluruh state bot
- **AI**: Gemini (`gemini-3.5-flash`) sebagai provider utama, Groq (`openai/gpt-oss-120b`) sebagai fallback — keduanya mendukung multi-key untuk menambah kuota gabungan

---

## Fitur

### Entry
- **Gerbang bias** — AI hanya dipanggil kalau indikator sudah punya minimal 2 sinyal align (hemat 75-90% panggilan AI)
- **Prompt dengan rubrik confidence eksplisit** (0-100, 4 tingkat) — AI wajib beri arah + confidence terpisah, bukan menghakimi sendiri "SKIP" ketika ragu
- **Multi-provider dengan failover otomatis** — Gemini → Groq (masing-masing bisa multi-key), cooldown terpisah untuk rate-limit vs model deprecated. Tiap provider bisa punya konfigurasi berbeda (`supports_json_mode`, `extra_kwargs` lewat `extra_body`) — perlu karena Gemini dan Groq (GPT-OSS) sama-sama model reasoning dengan kuirk berbeda, lihat catatan di bawah.
- **Position sizing dinamis** — persentase dari balance, di-scale oleh `allocation_pct` AI dan anti-martingale streak (menang beruntun → alokasi naik, kalah beruntun → alokasi turun)

### Pembelajaran (post-mortem)
- **Config epoch otomatis** — fingerprint parameter trading (`config.get_trading_params_fingerprint()`) dibandingkan tiap startup; kalau berubah (SL/TP/trailing/dst), epoch baru otomatis tercatat. Post-mortem HANYA sampling dari epoch aktif, supaya tidak mencampur data dari rezim parameter yang berbeda.
- **Sample size akar kuadrat** (bukan linear) — tumbuh cepat saat data masih sedikit, melandai saat sudah banyak. Dicap 5-15 sampel per sisi (winner/loser).
- **Gate agresivitas rewrite** — di bawah 20 trade dalam epoch aktif, AI diinstruksikan HANYA menyesuaikan kecil/incremental terhadap policy saat ini, bukan menulis ulang total (mencegah overfit ke kebetulan saat data masih tipis).
- **Snapshot verifikasi** — performa (win rate, avg PnL%) SEBELUM tiap update policy disimpan ke `strategy_log`, untuk mengecek objektif apakah policy baru benar memperbaiki hasil.
- **Catch-up di startup** — kalau belum pernah post-mortem sama sekali, atau sudah >20 jam sejak update terakhir, jalankan sebelum mulai scan. Tidak lagi bergantung sepenuhnya pada jadwal 00:00 (yang butuh proses tetap hidup persis di jam itu).

### Manajemen posisi aktif
- **TP/SL tetap** dihitung deterministik dari `config.py`, bukan dari AI
- **Trailing stop** — highest/lowest price **persisten ke database**, tidak dihitung ulang dari nol tiap cek (bug lama yang sudah diperbaiki)
- **Dynamic TP extend** — kalau momentum (RSI) masih kuat saat TP tersentuh, target diperpanjang
- **Review tesis berkala** (`THESIS_REVIEW_*`) — AI meninjau ulang apakah alasan awal entry masih valid; hanya bisa `HOLD` / `TIGHTEN_SL` (severity mild/strong, angka dihitung & divalidasi kode — SL tidak pernah bisa menjauh) / `EXIT_EARLY`
- **Stagnant position breaker** — murni deterministik (tanpa AI): posisi yang sudah lama terbuka TAPI PnL tidak pernah berkomitmen ke arah manapun (gate ganda: durasi + band PnL) otomatis ditutup, membebaskan slot
- **Monitoring posisi di jadwal terpisah** (10 detik) dari scan kandidat (120 detik) — penting khusus di PAPER mode karena tidak ada order stop asli di exchange

### Observability & tooling
- Log berwarna per level (ok/warn/err/ai/skip), ringkasan `Σ` tiap cycle
- `manual_close.py` — tutup posisi manual dari terminal terpisah tanpa memblokir loop utama
- `daily_report.py` — laporan harian, plus mode `--start-experiment` / `--since-experiment` untuk evaluasi periode uji dengan parameter dibekukan (breakdown `close_reason`, bukan cuma win rate)

---

## Setup

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install python-binance pandas openai rich schedule python-dotenv
```

Buat `.env`:

```dotenv
USE_TESTNET=true

BINANCE_TESTNET_API_KEY=...
BINANCE_TESTNET_SECRET_KEY=...
BINANCE_API_KEY=...       # untuk live mode nanti
BINANCE_SECRET_KEY=...

GEMINI_API_KEY=...
# atau multi-key: GEMINI_API_KEYS=key1,key2,key3

GROQ_API_KEY=...
# atau multi-key: GROQ_API_KEYS=key1,key2,key3
```

Jalankan:

```bash
python main.py
```

---

## Laporan (`daily_report.py`)

```bash
# Laporan harian biasa — ringkasan hari ini + overall sejak awal
python daily_report.py

# Tandai SEKARANG sebagai awal periode eksperimen (parameter dibekukan).
# Jalankan SEKALI saja di awal — menjalankan ini lagi akan me-reset
# penanda ke waktu baru.
python daily_report.py --start-experiment

# Cek progress eksperimen kapan saja (aman dijalankan berkali-kali,
# tidak menulis ulang penanda) — fokus ke breakdown close_reason
# (SL/TP/TRAILING/STAGNANT/THESIS_INVALID), bukan cuma win rate
python daily_report.py --since-experiment

# Manual override — bandingkan periode custom tanpa perlu penanda
python daily_report.py --since "2026-07-15"
python daily_report.py --since "2026-07-15 14:00:00"

# Ringkasan agregat sejak trade PERTAMA (sama seperti --since-experiment,
# tapi tanpa batas tanggal — breakdown close_reason mencakup semua histori)
python daily_report.py --all-time

# List SETIAP trade satu per satu (bukan agregat) — untuk scroll/cek
# trade tertentu, urut dari yang paling baru
python daily_report.py --all-trades
```

Laporan hasil `--since-experiment`/`--since` menampilkan breakdown per
`close_reason` (count, total PnL, avg PnL, win rate dalam grup) —
berguna untuk mendiagnosis SUMBER masalah: banyak `SL` dengan avg PnL
sangat negatif mengarah ke kualitas sinyal/confidence, banyak
`STAGNANT` mengarah ke parameter `MAX_HOLD_MINUTES` kurang pas, dst.
Detail lengkap di kolom `Cara baca cepat` pada output laporan itu
sendiri.

Install `pyperclip` (opsional) supaya laporan otomatis ke-copy ke
clipboard setiap kali dijalankan: `pip install pyperclip`.

---

## Referensi Config Penting (`config.py`)

| Parameter | Fungsi |
|---|---|
| `USE_TESTNET` | Testnet vs live Binance |
| `LEVERAGE`, `STOP_LOSS_PCT`, `TAKE_PROFIT_PCT` | Risk dasar per trade (default disetel untuk gaya scalping 5m/15m) |
| `DYNAMIC_SIZING_ENABLED`, `BASE_RISK_PCT`, `STREAK_*` | Position sizing & anti-martingale |
| `TRAILING_STOP_*` | Aktivasi & callback trailing stop |
| `STAGNANT_BREAKER_ENABLED`, `MAX_HOLD_MINUTES`, `STAGNANT_PNL_BAND_PCT` | Circuit breaker posisi mandek |
| `THESIS_REVIEW_*` | Interval, jeda minimum, threshold confidence review tesis |
| `REQUIRE_INDICATOR_BIAS` | Gerbang bias sebelum panggil AI |
| `MIN_CONFIDENCE_TO_TRADE` | Threshold eksekusi (lihat catatan di bawah) |
| `AI_PROVIDER_ORDER`, `GEMINI_MODEL`, `GROQ_MODEL` | Urutan & model provider AI |
| `GROQ_SUPPORTS_JSON_MODE` | `False` — GPT-OSS di Groq punya masalah dikenal dengan `response_format=json_object` (lihat catatan di bawah) |
| `KNOWN_DEAD_GEMINI_MODELS` | Guard startup — cegah pakai model yang diketahui mati/dibatasi |
| `TOP_GAINER_LIMIT`, `MIN_VOLUME_24H`, `SCAN_WORKERS` | Cakupan & paralelisasi scan |

`PAPER_TRADE_MODE` (di `executor.py`, bukan `config.py`) — kalau `True`,
semua order disimulasikan lokal, TIDAK ada order asli ke Binance.

---

## Status & Backlog Perbaikan

**Fase saat ini: membangun & memperbaiki kualitas dulu, bukan menjalankan
eksperimen terkontrol.** Eksperimen "1 minggu parameter dibekukan" yang
sempat dimulai dijeda — begitu ada temuan yang jelas layak diperbaiki,
langsung dikerjakan alih-alih ditahan sampai periode uji selesai.
Eksperimen terkontrol akan dimulai lagi dari awal begitu kualitas bot
dirasa sudah cukup matang (`daily_report.py --start-experiment` reset
penanda kapan saja).

Parameter risk/trading saat ini (bisa berubah lebih sering selama fase ini):
- `MIN_CONFIDENCE_TO_TRADE = 0.70`
- `STOP_LOSS_PCT = 1.5`, `TAKE_PROFIT_PCT = 3.0`
- `TRAILING_STOP_ACTIVATION = 1.0`, `TRAILING_STOP_CALLBACK = 0.5`
- `MAX_HOLD_MINUTES = 60`, `STAGNANT_PNL_BAND_PCT = 2.0`
- `THESIS_REVIEW_INTERVAL_MINUTES = 10`, `THESIS_REVIEW_MIN_CONFIDENCE = 0.65`

Setiap kali parameter di atas berubah, **config epoch baru otomatis
tercatat** di startup (lihat subsection Pembelajaran di atas) — post-mortem
otomatis menyesuaikan, tidak perlu langkah manual (arsip DB manual masih
disarankan untuk reset bersih total, tapi tidak lagi wajib).

### Backlog dari temuan nyata (belum semua dikerjakan)

1. ✅ **Re-entry ke pair yang baru saja kena SL di hari yang sama** —
   SUDAH DIPERBAIKI. `MAX_SL_PER_SYMBOL_PER_DAY = 2` di `config.py` —
   symbol yang kena SL 2x di hari yang sama diblokir dari entry baru
   untuk sisa hari itu (`memory.get_symbol_sl_count_today()` +
   `risk_manager.can_trade()` Cek 3b). Cooldown global 15 menit
   (`COOLDOWN_AFTER_SL`) tetap berlaku terpisah untuk semua symbol.
2. **Thesis review konsisten `HOLD` pada posisi yang memburuk perlahan
   tapi pasti** — kasus ONEUSDT: PnL menurun bertahap sampai akhirnya
   SL, tapi setiap review sepanjang perjalanan itu selalu `HOLD`.
   `THESIS_REVIEW_MIN_CONFIDENCE = 0.65` kemungkinan kurang sensitif
   untuk pola "perlahan tapi pasti" dibanding "tiba-tiba terbalik".
   Pertimbangkan tambah aturan berbasis TREN PnL (bukan cuma sinyal
   teknikal) di `make_thesis_review`.
3. 🟡 **Post-mortem belum FULLY closed-loop** — SEBAGIAN dikerjakan:
   sampling sekarang per-epoch config + formula akar kuadrat + gate
   konservatif di data sedikit + snapshot performa sebelum tiap update
   (lihat subsection Pembelajaran). Yang MASIH belum ada: mekanisme
   otomatis yang menolak/revert policy baru kalau ternyata performanya
   lebih buruk dari snapshot sebelumnya — saat ini masih perlu dicek
   manual dari `strategy_log`.

---

## Hal-hal Penting untuk Diingat

- **`MIN_CONFIDENCE_TO_TRADE`** sudah beberapa kali disetel ulang selama
  fase belajar (0.40 → 0.60 → 0.70 saat ini) — lihat
  [Status & Backlog Perbaikan](#status--backlog-perbaikan) untuk nilai
  parameter lengkap dan progress terkini.
- **PAPER mode tidak punya order stop asli di exchange** — SL/TP/trailing
  murni hasil polling bot sendiri. Ini kenapa monitoring posisi harus di
  jadwal cepat (10 detik) — polling yang lambat pernah menyebabkan
  overshoot SL jauh dari yang seharusnya.
- **Model AI bisa deprecated/dibatasi sewaktu-waktu** — sudah pernah
  kejadian dua kali (`gemini-2.0-flash` mati total, `gemini-2.5-flash`
  dibatasi untuk API key baru). `KNOWN_DEAD_GEMINI_MODELS` di `config.py`
  membantu deteksi dini, tapi cek berkala tetap perlu ke
  [halaman deprecation Gemini](https://ai.google.dev/gemini-api/docs/deprecations).
- **Model reasoning (Gemini 3.5 Flash, GPT-OSS di Groq) sama-sama
  punya "thinking" tersembunyi** yang ikut memotong `max_tokens` sebelum
  sempat menulis jawaban — gejalanya respons kosong/`finish_reason: length`.
  Gemini defaultnya level "medium", GPT-OSS juga "medium". Dimitigasi
  dengan `reasoning_effort: "low"` untuk keduanya (Groq juga ditambah
  `include_reasoning: False`) di `ai_providers.py`.
- **GPT-OSS di Groq TIDAK kompatibel dengan `response_format=json_object`**
  — validator JSON sisi server Groq terlalu ketat untuk model ini, sering
  gagal dengan HTTP 400 "Failed to generate/validate JSON" (dikonfirmasi
  laporan serupa di forum komunitas Groq). Solusi: `GROQ_SUPPORTS_JSON_MODE
  = False` di `config.py` — tetap minta JSON lewat instruksi prompt +
  parsing manual (resilient, sudah ada sejak awal untuk kasus JSON Gemini
  yang kadang rusak).
- **Parameter custom provider (reasoning_effort, include_reasoning, dll)
  HARUS lewat `extra_body`, bukan kwarg langsung** — beberapa nama
  kebetulan dikenal signature SDK `openai` (`reasoning_effort`, karena
  dipakai juga model reasoning OpenAI sendiri) tapi yang lain murni
  ekstensi provider (`include_reasoning` punya Groq) dan akan ditolak
  `TypeError` kalau dikirim sebagai kwarg langsung. `extra_body` aman
  untuk keduanya — lihat `Provider.extra_kwargs` di `ai_providers.py`.
- **Multi-key Gemini/Groq** — arsitektur mendukung, tapi ToS kedua
  provider pada dasarnya melarang penggunaan untuk *menghindari* rate
  limit. Aman kalau memang key milik sendiri dari akun berbeda; tetap
  area abu-abu untuk dipertimbangkan sendiri.
- **`close_reason: MANUAL`** (dari `manual_close.py`) sengaja dikecualikan
  dari `perform_post_mortem()` — supaya keputusan manual tidak mengotori
  pembelajaran pola exit yang murni dari keputusan bot.

---

## Belum Dikerjakan — Kesiapan Live Mode

- **Rekonsiliasi state saat LIVE mode** — kalau order TP/SL ter-fill di
  exchange, bot belum otomatis mendeteksi dan menutup record di database.
  Perlu polling `get_open_positions()` dari exchange dan dibandingkan ke
  DB sebelum benar-benar dipakai live.
- **Quantity saat close** dihitung ulang dari `margin * leverage /
  entry_price`, bukan disimpan langsung dari saat open — berisiko beda
  akibat pembulatan step_size di live mode.
- **`get_symbol_info()`** memanggil endpoint berat (`futures_exchange_info`)
  tiap kali mau entry — sebaiknya di-cache sekali di startup.

---

## Lisensi / Penggunaan

Proyek pribadi untuk pembelajaran. Gunakan dengan tanggung jawab sendiri.