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
- **AI**: Gemini (`gemini-3.5-flash`) sebagai provider utama, Groq (`llama-3.3-70b-versatile`) sebagai fallback — keduanya mendukung multi-key untuk menambah kuota gabungan

---

## Fitur

### Entry
- **Gerbang bias** — AI hanya dipanggil kalau indikator sudah punya minimal 2 sinyal align (hemat 75-90% panggilan AI)
- **Prompt dengan rubrik confidence eksplisit** (0-100, 4 tingkat) — AI wajib beri arah + confidence terpisah, bukan menghakimi sendiri "SKIP" ketika ragu
- **Multi-provider dengan failover otomatis** — Gemini → Groq (masing-masing bisa multi-key), cooldown terpisah untuk rate-limit vs model deprecated
- **Position sizing dinamis** — persentase dari balance, di-scale oleh `allocation_pct` AI dan anti-martingale streak (menang beruntun → alokasi naik, kalah beruntun → alokasi turun)

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
| `KNOWN_DEAD_GEMINI_MODELS` | Guard startup — cegah pakai model yang diketahui mati/dibatasi |
| `TOP_GAINER_LIMIT`, `MIN_VOLUME_24H`, `SCAN_WORKERS` | Cakupan & paralelisasi scan |

`PAPER_TRADE_MODE` (di `executor.py`, bukan `config.py`) — kalau `True`,
semua order disimulasikan lokal, TIDAK ada order asli ke Binance.

---

## Status Eksperimen Saat Ini

Sedang berjalan: **uji 1 minggu dengan parameter dibekukan** — jangan
ubah `config.py` apapun sampai periode ini selesai, supaya hasilnya bisa
dievaluasi secara bersih (lihat `daily_report.py --since-experiment`).

Parameter yang dibekukan:
- `MIN_CONFIDENCE_TO_TRADE = 0.60`
- `STOP_LOSS_PCT = 1.5`, `TAKE_PROFIT_PCT = 3.0`
- `TRAILING_STOP_ACTIVATION = 1.0`, `TRAILING_STOP_CALLBACK = 0.5`
- `MAX_HOLD_MINUTES = 60`, `STAGNANT_PNL_BAND_PCT = 2.0`
- `THESIS_REVIEW_INTERVAL_MINUTES = 10`, `THESIS_REVIEW_MIN_CONFIDENCE = 0.65`

### Temuan yang disimpan untuk evaluasi akhir minggu (belum ditindaklanjuti)

1. **Re-entry ke pair yang baru saja kena SL di hari yang sama** —
   pernah terjadi pada PROMUSDT (2x SL, total -$12.76) dan 1000XECUSDT
   (2x, net -$4.80). Cooldown SL saat ini global (15 menit, semua
   symbol), bukan per-symbol. Pertimbangkan cooldown lebih panjang
   khusus untuk symbol yang sama kalau pola ini konsisten muncul.
2. **Thesis review konsisten `HOLD` pada posisi yang memburuk perlahan
   tapi pasti** — kasus ONEUSDT: PnL menurun bertahap dari -1.4% sampai
   akhirnya SL di -9.6%, tapi setiap review sepanjang perjalanan itu
   selalu `HOLD` (beda dengan USUSDT yang berhasil `TIGHTEN_SL` saat
   tesis terbalik jelas). Dugaan: `THESIS_REVIEW_MIN_CONFIDENCE = 0.65`
   kurang sensitif untuk pola "perlahan tapi pasti" dibanding pola
   "tiba-tiba terbalik". Kalau berulang, pertimbangkan turunkan
   threshold atau tambah aturan berbasis tren PnL (bukan cuma sinyal
   teknikal) di `make_thesis_review`.

### Breakdown hasil sejauh ini (`--all-time`, hari pertama)

```
✅ TRAILING   5x | total $+17.87 | avg $+3.57  | WR 100%
❌ SL         5x | total $-46.46 | avg $-9.29  | WR 0%
✅ STAGNANT   3x | total $+0.31  | avg $+0.10  | WR 67%
✅ TP         1x | total $+17.75 | avg $+17.75 | WR 100%
```

Tanpa grup SL, hari pertama sebenarnya profit +$35.93 — trailing stop
dan stagnant breaker sama-sama berkontribusi positif bersih. Kerugian
net murni datang dari kualitas 5 trade yang berujung SL, bukan dari
mekanisme manajemen posisi.

---

## Hal-hal Penting untuk Diingat

- **`MIN_CONFIDENCE_TO_TRADE`** sempat diturunkan ke 0.40 untuk fase
  belajar awal (lebih banyak data untuk post-mortem), lalu dinaikkan
  lagi ke 0.60 dan dibekukan untuk uji 1 minggu — lihat
  [Status Eksperimen Saat Ini](#status-eksperimen-saat-ini) untuk
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
- **Gemini 3.5 Flash punya "thinking" tersembunyi** (default level medium)
  yang ikut memotong `max_tokens` — kalau tiba-tiba banyak respons kosong
  (`finish_reason: length`), ini penyebabnya. Sudah dimitigasi dengan
  `reasoning_effort: "low"` di `ai_providers.py`.
- **Multi-key Gemini/Groq** — arsitektur mendukung, tapi ToS kedua
  provider pada dasarnya melarang penggunaan untuk *menghindari* rate
  limit. Aman kalau memang key milik sendiri dari akun berbeda; tetap
  area abu-abu untuk dipertimbangkan sendiri.
- **`close_reason: MANUAL`** (dari `manual_close.py`) sengaja dikecualikan
  dari `perform_post_mortem()` — supaya keputusan manual tidak mengotori
  pembelajaran pola exit yang murni dari keputusan bot.

---

## Belum Dikerjakan / Diketahui Perlu Perbaikan

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