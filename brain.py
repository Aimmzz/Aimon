import json
import os
from datetime import datetime
import config
import memory
import ai_providers

STRATEGY_FILE = "strategy_policy.txt"

# ============================================
# STRATEGY POLICY
# ============================================

def load_strategy_policy() -> str:
    """Baca strategy policy yang dipelajari dari post-mortem"""
    if not os.path.exists(STRATEGY_FILE):
        default = """
STRATEGY POLICY (Default — belum ada data trade):
1. Prioritaskan pair dengan volume tinggi dan likuiditas baik
2. Hindari entry saat volume sangat rendah (ratio < 0.5x)
3. Konfirmasi minimal 2 sinyal sebelum entry
4. Hindari entry saat trend 15m berlawanan dengan sinyal 5m
5. Selalu pertimbangkan risk/reward sebelum entry
""".strip()
        with open(STRATEGY_FILE, "w") as f:
            f.write(default)
        return default

    with open(STRATEGY_FILE, "r") as f:
        return f.read()

def update_strategy_policy(new_policy: str):
    """Update strategy policy dari hasil post-mortem + simpan history ke DB"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    content   = f"[Updated: {timestamp}]\n{new_policy}"
    with open(STRATEGY_FILE, "w") as f:
        f.write(content)

    # Simpan juga ke tabel strategy_log supaya history policy tidak hilang
    # (tabel ini sudah ada di memory.init_db tapi sebelumnya tidak pernah dipakai)
    try:
        with memory.get_db() as conn:
            conn.execute(
                "INSERT INTO strategy_log (timestamp, policy, source) VALUES (?, ?, 'post_mortem')",
                (datetime.now().isoformat(), new_policy)
            )
    except Exception as e:
        print(f"⚠️  Gagal simpan policy history: {e}")

    print(f"📝 Strategy policy diupdate")

# ============================================
# DECISION ENGINE
# ============================================

def make_decision(
    symbol: str,
    price: float,
    price_change: float,
    indicators: dict,
    formatted_indicators: str
) -> dict:
    """
    Kirim data ke AI (Gemini primary, Groq fallback) dan dapat keputusan trading.
    Return dict dengan decision, confidence, ai_provider, dll.
    rate_limited hanya True kalau SEMUA provider sedang cooldown.
    """

    strategy_policy = load_strategy_policy()

    stats        = memory.get_performance_stats()
    daily_pnl    = stats["daily_pnl"]
    daily_trades = stats["daily_trades"]
    win_rate     = stats["win_rate"]

    prompt = f"""
Kamu adalah AI Trading Analyst untuk Binance Futures scalping bot.
Tugasmu: Analisis data teknikal dan putuskan apakah harus BUY (LONG), SELL (SHORT), atau SKIP.

=== STRATEGY POLICY (WAJIB DIIKUTI) ===
{strategy_policy}

=== DATA TEKNIKAL ===
{formatted_indicators}

=== KONTEKS BOT HARI INI ===
- Total trade hari ini : {daily_trades}
- PnL hari ini        : ${daily_pnl:.2f}
- Win rate keseluruhan: {win_rate}%
- Max loss/hari       : ${config.MAX_DAILY_LOSS}
- Sisa ruang loss     : ${config.MAX_DAILY_LOSS - abs(min(daily_pnl, 0)):.2f}

=== RISK MANAGEMENT ===
- Leverage  : {config.LEVERAGE}x
- Stop loss : {config.STOP_LOSS_PCT}%
- Take profit: {config.TAKE_PROFIT_PCT}%

=== INSTRUKSI ===
Berikan keputusan dalam format JSON yang STRICT. Jangan tambahkan teks apapun di luar JSON.

PENTING — cara kerja decision vs confidence:
Data di atas SUDAH lolos gerbang minimal 2 sinyal indikator align (bias {indicators.get('bias', '?')}).
Tugasmu BUKAN memutuskan "yakin atau tidak lalu SKIP" — tugasmu adalah memberi ARAH
yang sesuai bias tadi ({indicators.get('bias', '?')}) dan mengukur seberapa kuat
setup-nya lewat angka confidence. Sistem trading yang akan memutuskan apakah
confidence-nya cukup untuk eksekusi, BUKAN kamu.

Maka: decision HARUS "LONG" atau "SHORT" mengikuti arah bias di atas, KECUALI kalau
kamu menemukan kondisi yang benar-benar bertentangan (misal data teknikal kosong/rusak,
atau bias sebenarnya campur aduk meski lolos gerbang). SKIP hanya untuk kasus itu —
bukan untuk "sinyal lemah" (sinyal lemah = confidence rendah, bukan SKIP).

=== PANDUAN CONFIDENCE (WAJIB DIIKUTI, skala 0-100) ===
Nilai confidence HARUS dihitung dari data teknikal di atas, bukan angka default:
- 80-100 : 3+ sinyal align searah + trend 15m mendukung + volume spike terkonfirmasi
- 60-79  : tepat 2 sinyal align searah + trend 15m TIDAK berlawanan
- 40-59  : sinyal campuran, atau trend 15m berlawanan dengan sinyal 5m
- 0-39   : sinyal sangat lemah meski masih ada bias minimal
Hitung dulu berapa sinyal yang align dan cek trend 15m, baru tentukan angkanya.
Confidence rendah TETAP HARUS disertai decision LONG/SHORT (sesuai bias) — jangan
diganti jadi SKIP. Angka confidence itu sendiri sudah menyampaikan keraguanmu.

Format response HARUS persis seperti ini:
{{
    "decision": "LONG" atau "SHORT" (SKIP hanya untuk kasus data benar-benar rusak/kontradiktif),
    "confidence": integer 0 sampai 100,
    "allocation_pct": integer 0 sampai 100,
    "reasoning": "penjelasan singkat dalam bahasa Indonesia, sebutkan sinyal apa saja yang align"
}}
"""

    ai = ai_providers.chat(
        system      = "Kamu adalah trading analyst profesional. Selalu jawab HANYA dengan JSON valid. Tidak ada teks lain.",
        user        = prompt,
        temperature = 0.1,
        max_tokens  = 400,
        json_mode   = True
    )

    # ── Semua provider rate limited / gagal ──
    if not ai["ok"]:
        return {
            "decision"     : "SKIP",
            "confidence"   : 0,
            "reasoning"    : "ALL_PROVIDERS_RATE_LIMITED" if ai["all_rate_limited"] else ai.get("error", "AI error"),
            "rate_limited" : ai["all_rate_limited"],
            "ai_provider"  : None,
            "symbol"       : symbol,
            "price"        : price
        }

    # ── Parse JSON ──
    try:
        result = json.loads(ai["content"])
    except json.JSONDecodeError as e:
        print(f"❌ JSON parse error dari [{ai['provider']}]: {e}")
        return {
            "decision"     : "SKIP",
            "confidence"   : 0,
            "reasoning"    : "JSON parse error",
            "rate_limited" : False,
            "ai_provider"  : ai["provider"],
            "symbol"       : symbol,
            "price"        : price
        }

    # ── Validasi & normalisasi ──
    decision = str(result.get("decision", "SKIP")).upper()
    if decision not in ["LONG", "SHORT", "SKIP"]:
        decision = "SKIP"

    try:
        conf_raw = float(result.get("confidence", 0))
    except (TypeError, ValueError):
        conf_raw = 0.0
    # Normalisasi: prompt minta 0-100, tapi kalau model tetap jawab 0-1
    # (kebiasaan lama), keduanya diterima
    confidence = conf_raw / 100.0 if conf_raw > 1.0 else conf_raw
    confidence = max(0.0, min(1.0, confidence))

    # SL/TP TIDAK lagi diminta dari AI — LLM tidak reliable untuk aritmetika harga.
    # Selalu dihitung deterministic dari config di risk_manager.
    if decision == "LONG":
        sl = round(price * (1 - config.STOP_LOSS_PCT / 100), 6)
        tp = round(price * (1 + config.TAKE_PROFIT_PCT / 100), 6)
    elif decision == "SHORT":
        sl = round(price * (1 + config.STOP_LOSS_PCT / 100), 6)
        tp = round(price * (1 - config.TAKE_PROFIT_PCT / 100), 6)
    else:
        sl, tp = 0, 0

    result.update({
        "decision"          : decision,
        "confidence"        : confidence,
        "stop_loss_price"   : sl,
        "take_profit_price" : tp,
        "symbol"            : symbol,
        "price"             : price,
        "rate_limited"      : False,
        "ai_provider"       : ai["provider"]
    })

    emoji = "🟢" if decision == "LONG" else "🔴" if decision == "SHORT" else "⏭️"
    print(f"\n🤖 AI Decision untuk {symbol} (via {ai['provider']}):")
    print(f"{'='*45}")
    print(f"{emoji} Keputusan  : {decision}")
    print(f"📊 Confidence : {confidence:.0%}")
    print(f"💭 Reasoning  : {result.get('reasoning', '-')}")
    if decision != "SKIP":
        print(f"🎯 TP         : {tp}")
        print(f"🛡️  SL         : {sl}")
    print(f"{'='*45}")

    return result

# ============================================
# REVIEW TESIS POSISI AKTIF
# ============================================

def make_thesis_review(trade: dict, current_indicators: dict) -> dict:
    """
    Tinjau ulang apakah alasan awal (tesis) untuk berada di posisi ini
    masih valid, dengan membandingkan kondisi SAAT ENTRY (tersimpan di DB)
    vs kondisi SEKARANG (current_indicators, hasil analyze() candle terbaru).

    BEDA PARADIGMA dengan make_decision(): ini bukan "AI mengelola risiko
    ulang" (AI tidak pernah menghitung harga SL/TP baru sendiri — itu tetap
    keputusan risk_manager.tighten_sl_price() yang deterministik). Tugas
    AI di sini murni menilai apakah kondisi teknikal yang jadi alasan
    entry sudah berbalik, dan seberapa yakin penilaian itu.

    Return dict:
        {"action": "HOLD"|"TIGHTEN_SL"|"EXIT_EARLY",
         "severity": "mild"|"strong"|None,
         "confidence": float 0-1,
         "reasoning": str,
         "rate_limited": bool}
    """
    entry_rsi   = trade.get("rsi_at_entry", 0)
    entry_macd  = trade.get("macd_at_entry", "")
    entry_side  = trade["side"]

    current_rsi  = current_indicators.get("rsi", 0)
    current_macd = current_indicators.get("macd", {}).get("crossover", "")
    current_trend = current_indicators.get("trend_confirmation", "neutral")
    current_vol   = current_indicators.get("volume", {})

    prompt = f"""
Kamu adalah AI risk reviewer untuk posisi futures yang SEDANG TERBUKA.
Tugasmu BUKAN memutuskan entry baru — posisi ini sudah berjalan, kamu
hanya menilai apakah ALASAN AWAL entry masih berlaku sekarang.

=== POSISI AKTIF ===
Symbol       : {trade['symbol']}
Arah         : {entry_side}
Entry price  : {trade['entry_price']}
SL saat ini  : {trade.get('sl_price', 0)}
TP saat ini  : {trade.get('tp_price', 0)}
Sudah berjalan: {trade.get('duration_mins', '?')} menit (perkiraan)

=== KONDISI SAAT ENTRY (alasan awal masuk posisi) ===
RSI saat entry   : {entry_rsi}
MACD saat entry  : {entry_macd}

=== KONDISI SEKARANG ===
RSI sekarang     : {current_rsi}
MACD sekarang    : {current_macd} | trend: {current_indicators.get('macd', {}).get('trend', '?')}
Trend 15m        : {current_trend}
Volume ratio     : {current_vol.get('ratio', '?')}x | spike: {current_vol.get('is_spike', '?')}

=== TUGASMU ===
Bandingkan kondisi entry vs sekarang. Untuk posisi {entry_side}, alasan
masuk posisi masih valid kalau arah RSI/MACD/trend masih konsisten
mendukung {entry_side}. Tesis dianggap BATAL kalau ada pembalikan jelas
(misal MACD berbalik arah, atau trend 15m sekarang justru kuat melawan
arah posisi).

Pilih SATU tindakan:
- HOLD        : tesis masih valid, tidak ada perubahan berarti
- TIGHTEN_SL  : ada tanda pelemahan tapi belum jelas terbalik total —
                sertakan severity "mild" (pelemahan ringan) atau
                "strong" (pelemahan jelas, hampir terbalik)
- EXIT_EARLY  : tesis sudah JELAS terbalik — lebih baik keluar sebelum
                SL asli kena

JANGAN sebut angka SL/TP baru — itu bukan tugasmu, sistem yang menghitung.
Kalau tidak yakin, pilih HOLD dengan confidence rendah — jangan
memaksakan TIGHTEN_SL/EXIT_EARLY tanpa bukti kuat di data di atas.

Format response HARUS persis seperti ini (JSON saja, tanpa teks lain):
{{
    "action": "HOLD" atau "TIGHTEN_SL" atau "EXIT_EARLY",
    "severity": "mild" atau "strong" atau null,
    "confidence": integer 0 sampai 100,
    "reasoning": "penjelasan singkat bahasa Indonesia, sebutkan indikator spesifik yang berubah"
}}
"""

    ai = ai_providers.chat(
        system      = "Kamu adalah risk reviewer yang konservatif — hanya bertindak kalau ada bukti kuat, HOLD kalau ragu. Jawab HANYA JSON valid.",
        user        = prompt,
        temperature = 0.1,
        max_tokens  = 300,
        json_mode   = True
    )

    if not ai["ok"]:
        # Semua provider gagal/rate limited — HOLD, jangan mengubah apapun
        return {
            "action": "HOLD", "severity": None, "confidence": 0,
            "reasoning": "AI tidak tersedia — HOLD sebagai default aman",
            "rate_limited": ai["all_rate_limited"]
        }

    try:
        result = json.loads(ai["content"])
    except json.JSONDecodeError:
        return {
            "action": "HOLD", "severity": None, "confidence": 0,
            "reasoning": "JSON parse error — HOLD sebagai default aman",
            "rate_limited": False
        }

    action = str(result.get("action", "HOLD")).upper()
    if action not in ("HOLD", "TIGHTEN_SL", "EXIT_EARLY"):
        action = "HOLD"

    severity = result.get("severity")
    if severity not in ("mild", "strong"):
        severity = None

    try:
        conf_raw = float(result.get("confidence", 0))
    except (TypeError, ValueError):
        conf_raw = 0.0
    confidence = conf_raw / 100.0 if conf_raw > 1.0 else conf_raw
    confidence = max(0.0, min(1.0, confidence))

    # Threshold KHUSUS review — lebih tinggi dari threshold entry, karena
    # membatalkan tesis / mengunci profit adalah tindakan lebih signifikan
    # daripada entry biasa. Di bawah ini, paksa HOLD apapun kata AI.
    if confidence < config.THESIS_REVIEW_MIN_CONFIDENCE and action != "HOLD":
        action = "HOLD"
        severity = None

    return {
        "action"      : action,
        "severity"    : severity,
        "confidence"  : confidence,
        "reasoning"   : result.get("reasoning", ""),
        "rate_limited": False,
        "ai_provider" : ai["provider"]
    }

# ============================================
# POST MORTEM LEARNING
# ============================================

def perform_post_mortem():
    """
    Analisis trade history dan update strategy policy.
    Dijalankan otomatis setiap malam.
    """
    print("\n🔬 Menjalankan Post-Mortem Analysis...")

    with memory.get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT symbol, side, pnl_usdt, pnl_percent,
                   close_reason, rsi_at_entry, macd_at_entry,
                   volume_spike, duration_mins, ai_reasoning
            FROM trade_history
            WHERE status = 'CLOSED' AND pnl_usdt > 0 AND close_reason != 'MANUAL'
            ORDER BY pnl_usdt DESC LIMIT 5
        """)
        winners = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT symbol, side, pnl_usdt, pnl_percent,
                   close_reason, rsi_at_entry, macd_at_entry,
                   volume_spike, duration_mins, ai_reasoning
            FROM trade_history
            WHERE status = 'CLOSED' AND pnl_usdt < 0 AND close_reason != 'MANUAL'
            ORDER BY pnl_usdt ASC LIMIT 5
        """)
        losers = [dict(row) for row in cursor.fetchall()]

    if not winners and not losers:
        print("⚠️  Belum cukup data trade untuk post-mortem")
        return

    current_policy = load_strategy_policy()

    prompt = f"""
Kamu adalah quant trading strategist. Analisis trade history bot ini dan update strategy policy.

=== POLICY SAAT INI ===
{current_policy}

=== 5 TRADE TERBAIK ===
{json.dumps(winners, indent=2)}

=== 5 TRADE TERBURUK ===
{json.dumps(losers, indent=2)}

=== TUGASMU ===
Berdasarkan pola dari trade terbaik dan terburuk:
1. Identifikasi pola yang menghasilkan profit (RSI berapa, MACD bagaimana, volume seperti apa)
2. Identifikasi pola yang menyebabkan loss (kondisi apa yang harus dihindari)
3. Tulis strategy policy baru yang lebih baik dari sebelumnya
4. Maksimal 8 poin, bahasa Indonesia, singkat dan actionable

Jawab HANYA dengan policy baru saja, tanpa penjelasan tambahan.
"""

    ai = ai_providers.chat(
        system      = "Kamu adalah quant trading strategist. Jawab langsung dengan policy baru.",
        user        = prompt,
        temperature = 0.3,
        max_tokens  = 600,
        json_mode   = False   # Post-mortem output-nya teks bebas, bukan JSON
    )

    if not ai["ok"]:
        print(f"❌ Post-mortem gagal — {ai.get('error', 'semua provider tidak tersedia')}")
        return

    new_policy = ai["content"]
    update_strategy_policy(new_policy)
    print(f"✅ Strategy policy diupdate (via {ai['provider']})")
    print(f"\n📋 Policy Baru:\n{new_policy}")


if __name__ == "__main__":
    from scanner import get_candles, get_top_gainers
    from indicators import analyze, format_for_ai

    print("🧪 Test Brain (Multi-Provider)\n")

    for s in ai_providers.get_provider_status():
        key_status = "✅" if s["has_key"] else "❌ key kosong"
        print(f"  [{s['name']}] {s['model']} — {key_status}")

    gainers = get_top_gainers()
    if not gainers:
        print("❌ Tidak ada gainer")
        exit()

    symbol       = gainers[0]["symbol"]
    price        = gainers[0]["price"]
    price_change = gainers[0]["price_change"]

    print(f"\n🎯 Testing dengan {symbol}...\n")

    df         = get_candles(symbol, config.SCALPING_TIMEFRAME, limit=100)
    df_trend   = get_candles(symbol, config.TREND_TIMEFRAME, limit=100)
    indicators = analyze(df, df_trend)
    formatted  = format_for_ai(symbol, price, price_change, indicators)

    result = make_decision(symbol, price, price_change, indicators, formatted)

    print(f"\n✅ Raw result:")
    print(json.dumps(result, indent=2, ensure_ascii=False))