import os
from dotenv import load_dotenv

load_dotenv()

# ============================================
# MODE: Testnet atau Live
# ============================================
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"

# ============================================
# API KEYS
# ============================================
if USE_TESTNET:
    API_KEY    = os.getenv("BINANCE_TESTNET_API_KEY")
    API_SECRET = os.getenv("BINANCE_TESTNET_SECRET_KEY")
else:
    API_KEY    = os.getenv("BINANCE_API_KEY")
    API_SECRET = os.getenv("BINANCE_SECRET_KEY")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")  # dipakai warning message lama, lihat GEMINI_API_KEYS di bawah

# ── Gemini mendukung MULTI KEY, sama seperti Groq ──
# Isi di .env dipisah koma: GEMINI_API_KEYS=key1,key2,key3
# (GEMINI_API_KEY tunggal lama tetap didukung untuk kompatibilitas mundur)
_gemini_keys_raw = os.getenv("GEMINI_API_KEYS", "")
GEMINI_API_KEYS = [k.strip() for k in _gemini_keys_raw.split(",") if k.strip()]
if not GEMINI_API_KEYS and GEMINI_API_KEY:
    GEMINI_API_KEYS = [GEMINI_API_KEY]

# ── Groq mendukung MULTI KEY tanpa perlu edit kode ──
# Isi di .env dipisah koma: GROQ_API_KEYS=key1,key2,key3
# (GROQ_API_KEY tunggal lama tetap didukung untuk kompatibilitas mundur)
_groq_keys_raw = os.getenv("GROQ_API_KEYS", "")
GROQ_API_KEYS = [k.strip() for k in _groq_keys_raw.split(",") if k.strip()]
if not GROQ_API_KEYS and os.getenv("GROQ_API_KEY"):
    GROQ_API_KEYS = [os.getenv("GROQ_API_KEY")]

# ============================================
# AI PROVIDERS (Multi-provider dengan failover)
# ============================================

# Urutan prioritas KATEGORI — "groq" otomatis diperluas jadi groq, groq2,
# groq3, ... sesuai jumlah key di GROQ_API_KEYS (lihat ai_providers.py)
AI_PROVIDER_ORDER = ["gemini", "groq"]

# Gemini — via endpoint OpenAI-compatible
# gemini-2.5-flash TERNYATA dibatasi untuk API key/project BARU (error asli:
# "no longer available to new users") — bukan mati total, tapi tetap tidak
# bisa dipakai key kita. gemini-3.5-flash GA sejak 19 Mei 2026, masih free
# tier, dan terbuka untuk akun baru.
GEMINI_MODEL    = "gemini-3.5-flash"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# ── Daftar model yang DIKETAHUI bermasalah (mati ATAU dibatasi untuk akun baru) ──
# Startup akan WARNING KERAS kalau GEMINI_MODEL match salah satu ini,
# supaya tidak lagi bergantung pada kamu inget-inget dari percakapan lama.
# Update daftar ini kalau ada info deprecation/restriksi baru dari
# https://ai.google.dev/gemini-api/docs/deprecations
KNOWN_DEAD_GEMINI_MODELS = {
    "gemini-2.0-flash"       : "mati sejak 1 Juni 2026",
    "gemini-2.0-flash-lite"  : "mati sejak 1 Juni 2026",
    "gemini-2.0-flash-001"   : "mati sejak 1 Juni 2026",
    "gemini-2.5-flash-lite"  : "shutdown 22 Juli 2026 — jangan pakai sekarang",
    "gemini-2.5-flash"       : "dibatasi untuk API key/project BARU sejak pertengahan 2026 (error: 'no longer available to new users') — akun kita kena batasan ini",
    "gemini-2.5-pro-preview" : "shutdown 2 Des 2025",
}

# Groq — via endpoint OpenAI-compatible
GROQ_MODEL    = "llama-3.3-70b-versatile"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# Cooldown provider saat kena rate limit (menit)
PROVIDER_COOLDOWN_MINUTE_MIN = 2    # kena limit per-menit (RPM/TPM) → tunggu sebentar
PROVIDER_COOLDOWN_DAILY_MIN  = 60   # kena limit harian (RPD/TPD) → tunggu lama, recheck tiap jam

# Timeout per panggilan AI (detik) — kalau lewat, langsung failover
# ke provider berikutnya alih-alih menunggu tanpa batas
AI_TIMEOUT_SECONDS = 20

# Minimum confidence score dari AI untuk eksekusi trade
# Diturunkan sementara ke 0.40 (bukan 0.60) untuk fase "latihan" —
# supaya bot lebih sering entry dan post-mortem punya lebih banyak data.
# Konsekuensi: level 40-59% di rubrik brain.py berarti AI sendiri menandai
# ada faktor negatif (biasanya trend 15m berlawanan) — win rate akan lebih
# rendah dari biasanya. WAJIB tetap PAPER_TRADE_MODE=True selama ini aktif.
# Naikkan lagi ke 0.60 begitu sudah cukup data trade untuk post-mortem.
MIN_CONFIDENCE_TO_TRADE = 0.60

# ============================================
# REVIEW TESIS POSISI AKTIF
# ============================================
# Fitur ini meninjau ulang posisi yang SEDANG TERBUKA secara berkala,
# menilai apakah alasan awal (tesis) entry masih valid — BUKAN memberi
# AI wewenang mengelola risiko ulang. AI hanya bisa TIGHTEN_SL (dihitung
# deterministik oleh risk_manager, tidak pernah oleh AI) atau EXIT_EARLY.
# AI TIDAK BISA menjauhkan SL atau menambah margin ke posisi rugi.
THESIS_REVIEW_ENABLED = True

# Interval review per posisi (menit) — dihitung dari review TERAKHIR,
# bukan dari entry, supaya jaraknya konsisten sepanjang trade berjalan.
THESIS_REVIEW_INTERVAL_MINUTES = 10

# Jeda minimum setelah entry sebelum review PERTAMA boleh terjadi —
# beri waktu data teknikal terbentuk, supaya tidak membandingkan kondisi
# yang nyaris identik dengan saat entry.
THESIS_REVIEW_MIN_HOLD_MINUTES = 15

# Threshold confidence KHUSUS untuk bertindak (TIGHTEN_SL/EXIT_EARLY) —
# LEBIH TINGGI dari MIN_CONFIDENCE_TO_TRADE karena membatalkan tesis /
# mengunci profit adalah tindakan lebih signifikan daripada entry biasa.
# Di bawah ini, action apapun dari AI dipaksa jadi HOLD.
THESIS_REVIEW_MIN_CONFIDENCE = 0.65

# ── Gerbang bias indikator ──
# Kalau True: AI HANYA dipanggil kalau indikator sudah menghasilkan bias
# LONG/SHORT (minimal 2 sinyal align). Kandidat dengan bias NEUTRAL
# langsung di-skip tanpa panggilan AI.
# Ini memangkas 75-90% panggilan AI → free tier jadi cukup.
REQUIRE_INDICATOR_BIAS = True

# ============================================
# TRADING SETTINGS
# ============================================

# Leverage — rendah dulu, bot tidak punya intuisi seperti manusia
LEVERAGE = 3

# Margin per trade (USDT) — FALLBACK kalau dynamic sizing gagal
MARGIN_PER_TRADE = 5.0

# ============================================
# DYNAMIC POSITION SIZING
# ============================================

DYNAMIC_SIZING_ENABLED = True

# Base risk per trade — persentase dari available balance
BASE_RISK_PCT = 0.10  # 10% dari balance

# Batas margin per trade — persentase dari available balance
MIN_MARGIN_PCT = 0.03
MAX_MARGIN_PCT = 0.18

# ── Anti-martingale (streak-based scaling) ──
STREAK_SCALING_ENABLED = True
STREAK_WIN_STEP    = 0.10
STREAK_LOSS_STEP   = 0.15
STREAK_MULTIPLIER_MIN = 0.50
STREAK_MULTIPLIER_MAX = 1.50

# ── Risk/Reward — disesuaikan untuk gaya SCALPING di timeframe 5m/15m ──
# Angka lama (SL 2% / TP 4%) terbukti dari histori trade sering butuh
# 40-60+ menit untuk kena TP — itu horizon swing-trading, bukan scalping.
# Untuk 5m/15m, target realistis jauh lebih dekat: pergerakan wajar dalam
# rentang menit-ke-puluhan-menit ada di kisaran 1-3%, bukan 4%+. TP yang
# terlalu jauh memaksa posisi menunggu lama, membuka celah lebih besar
# untuk pembalikan arah sebelum target tercapai.
# R/R tetap 1:2 (rasio yang sama, cuma jaraknya diperkecil).
STOP_LOSS_PCT   = 1.5
TAKE_PROFIT_PCT = 3.0

# Dynamic TP — kalau momentum kuat (RSI masih ekstrem saat TP tersentuh),
# tahan posisi lebih lama dengan target yang diperpanjang. DYNAMIC_TP_EXTENDED
# diskalakan turun proporsional mengikuti TAKE_PROFIT_PCT baru (rasio 1.5x
# dipertahankan: 6.0/4.0 lama = 4.5/3.0 baru).
DYNAMIC_TP_ENABLED    = True
DYNAMIC_TP_EXTENDED   = 4.5
DYNAMIC_TP_RSI_THRESH = 65

# ── Trailing stop ──
# Aktivasi diturunkan (2.0% → 1.0%) supaya trailing mulai bekerja lebih
# awal relatif terhadap target TP yang sekarang lebih dekat (3.0%) — kalau
# tetap 2.0%, trailing baru aktif di 2/3 perjalanan menuju TP, ruang
# geraknya sempit. Callback diperketat (1.0% → 0.5%) supaya profit yang
# sudah didapat tidak gampang "diberikan kembali" ke market — penting
# sekarang karena trailing BENAR-BENAR jalan (highest/lowest persisten ke
# DB, bug lama sudah diperbaiki), bukan cuma tercatat lalu tidak berlaku.
TRAILING_STOP_ENABLED       = True
TRAILING_STOP_ACTIVATION    = 1.0
TRAILING_STOP_CALLBACK      = 0.5

# ── Stagnant Position Breaker ──
# Murni deterministik, TANPA AI — beda dengan THESIS_REVIEW yang menangkap
# pembalikan sinyal, ini menangkap STAGNASI: posisi sudah lama terbuka
# tapi PnL tidak pernah berkomitmen ke arah manapun (tidak dekat TP,
# tidak dekat SL). Trigger CLOSE hanya kalau KEDUA syarat terpenuhi
# bersamaan — durasi lama SAJA tidak cukup, PnL kecil SAJA juga tidak
# cukup (supaya tidak salah tutup posisi yang sedang progres jelas).
STAGNANT_BREAKER_ENABLED   = True
MAX_HOLD_MINUTES           = 60    # selaras norma scalping 5m/15m
STAGNANT_PNL_BAND_PCT      = 2.0   # PnL leveraged dalam ±2% = "zona mati"

# Max posisi terbuka bersamaan
MAX_OPEN_TRADES = 2

# Max loss per hari (USDT)
MAX_DAILY_LOSS = 9.0

# Cooldown setelah kena stop loss (menit)
COOLDOWN_AFTER_SL = 15

# ============================================
# SCANNER SETTINGS
# ============================================

# 0 = TANPA BATAS — semua pair yang lolos filter di bawah ikut discan.
# Aman karena: (1) fetch candle sudah paralel, (2) gerbang bias memfilter
# sebelum AI dipanggil, jadi kandidat banyak ≠ panggilan AI banyak.
TOP_GAINER_LIMIT = 0

# Diturunkan dari 100M → 30M supaya kandidat lebih banyak.
# JANGAN dibuang total — pair di bawah ini slippage-nya besar dan
# datanya jelek untuk dipelajari post-mortem.
MIN_VOLUME_24H = 30_000_000

MIN_PRICE_CHANGE_PCT = 1.0    # diturunkan dari 2.0 — lebih banyak kandidat
MAX_PRICE_CHANGE_PCT = 15.0

SCALPING_TIMEFRAME = "5m"
TREND_TIMEFRAME = "15m"

# 99 (bukan 100) — di Binance Futures, klines limit <100 = weight 1,
# limit 100-499 = weight 2. Satu candle lebih sedikit = separuh weight.
CANDLE_LIMIT = 99

# Jumlah thread paralel untuk fetch candle saat pre-scan
SCAN_WORKERS = 8

# ============================================
# INDICATOR THRESHOLDS
# ============================================

RSI_PERIOD      = 14
RSI_OVERSOLD    = 35
RSI_OVERBOUGHT  = 65
RSI_NEUTRAL_LOW = 45
RSI_NEUTRAL_HIGH = 55

MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9

BB_PERIOD = 20
BB_STD    = 2.0

VOLUME_SPIKE_MULTIPLIER = 2.0

# ============================================
# TIMING
# ============================================

# Seberapa sering bot scan market (detik)
# 120 detik cukup untuk timeframe 5m — scan tiap 60s hanya membakar
# kuota AI dan API Binance tanpa nilai tambah (candle 5m belum berubah)
SCAN_INTERVAL_SECONDS = 120

# Seberapa sering cek posisi terbuka (detik)
POSITION_CHECK_SECONDS = 10

# ============================================
# TESTNET URLs
# ============================================
TESTNET_BASE_URL   = "https://testnet.binancefuture.com"
TESTNET_STREAM_URL = "wss://stream.binancefuture.com"

# ============================================
# VALIDASI
# ============================================
def validate_config():
    errors   = []
    warnings = []

    if not API_KEY:
        errors.append("❌ API_KEY tidak ditemukan di .env")
    if not API_SECRET:
        errors.append("❌ API_SECRET tidak ditemukan di .env")

    # Minimal SATU AI provider harus punya key
    if not GEMINI_API_KEYS and not GROQ_API_KEYS:
        errors.append("❌ Tidak ada AI provider — isi GEMINI_API_KEYS dan/atau GROQ_API_KEYS di .env")
    if not GEMINI_API_KEYS:
        warnings.append("⚠️  GEMINI_API_KEYS/GEMINI_API_KEY kosong — hanya Groq yang aktif")
    if not GROQ_API_KEYS:
        warnings.append("⚠️  GROQ_API_KEYS/GROQ_API_KEY kosong — hanya Gemini yang aktif")

    # ── Guard model deprecated — supaya tidak lagi bergantung ke memori percakapan ──
    if GEMINI_API_KEYS and GEMINI_MODEL in KNOWN_DEAD_GEMINI_MODELS:
        errors.append(
            f"❌ GEMINI_MODEL='{GEMINI_MODEL}' {KNOWN_DEAD_GEMINI_MODELS[GEMINI_MODEL]}. "
            f"Ganti ke 'gemini-3.5-flash' di config.py sebelum lanjut."
        )

    if errors:
        for e in errors:
            print(e)
        return False

    for w in warnings:
        print(w)

    rr_ratio = TAKE_PROFIT_PCT / STOP_LOSS_PCT
    mode     = "🧪 TESTNET" if USE_TESTNET else "🔴 LIVE"

    active_providers = []
    if GEMINI_API_KEYS:
        n = len(GEMINI_API_KEYS)
        active_providers.append(f"gemini x{n} ({GEMINI_MODEL})" if n > 1 else f"gemini ({GEMINI_MODEL})")
    if GROQ_API_KEYS:
        n = len(GROQ_API_KEYS)
        active_providers.append(f"groq x{n} ({GROQ_MODEL})" if n > 1 else f"groq ({GROQ_MODEL})")

    print(f"✅ Config loaded — Mode: {mode}")
    print(f"{'='*40}")
    print(f"💰 Modal setting:")
    if DYNAMIC_SIZING_ENABLED:
        print(f"   Sizing          : 🔄 DYNAMIC ({BASE_RISK_PCT*100:.0f}% balance, range {MIN_MARGIN_PCT*100:.0f}%-{MAX_MARGIN_PCT*100:.0f}%)")
        print(f"   Streak scaling  : {'✅' if STREAK_SCALING_ENABLED else '❌'}")
    else:
        print(f"   Sizing          : 📌 STATIC (${MARGIN_PER_TRADE}/trade)")
    print(f"   Leverage        : {LEVERAGE}x")
    print(f"   Max trades      : {MAX_OPEN_TRADES}")
    print(f"   Max loss/hari   : ${MAX_DAILY_LOSS}")
    print(f"📊 Risk management:")
    print(f"   Stop loss       : {STOP_LOSS_PCT}%")
    print(f"   Take profit     : {TAKE_PROFIT_PCT}%")
    print(f"   R/R Ratio       : 1:{rr_ratio}")
    print(f"   Trailing stop   : {'✅' if TRAILING_STOP_ENABLED else '❌'}")
    print(f"   Dynamic TP      : {'✅' if DYNAMIC_TP_ENABLED else '❌'}")
    print(f"   Cooldown SL     : {COOLDOWN_AFTER_SL} menit")
    print(f"🔍 Scanner:")
    print(f"   Min volume      : ${MIN_VOLUME_24H:,.0f}")
    print(f"   Price change    : {MIN_PRICE_CHANGE_PCT}% - {MAX_PRICE_CHANGE_PCT}%")
    print(f"   Timeframe       : {SCALPING_TIMEFRAME} + {TREND_TIMEFRAME}")
    print(f"   Scan interval   : {SCAN_INTERVAL_SECONDS}s")
    print(f"🤖 AI:")
    print(f"   Providers       : {' → '.join(active_providers)}")
    print(f"   Bias gate       : {'✅ (AI hanya dipanggil kalau ada bias indikator)' if REQUIRE_INDICATOR_BIAS else '❌'}")
    print(f"   Min confidence  : {MIN_CONFIDENCE_TO_TRADE*100:.0f}%")
    print(f"{'='*40}")
    return True


if __name__ == "__main__":
    validate_config()