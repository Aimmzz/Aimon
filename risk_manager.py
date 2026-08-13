import time
from datetime import datetime
import config
import memory

# ============================================
# BATAS KERUGIAN HARIAN (berbasis % balance)
# ============================================
# can_trade() dipanggil BERKALI-KALI per cycle (sekali per kandidat scan),
# jadi balance TIDAK boleh difetch tiap panggilan — di-cache pendek saja.
# 60 detik cukup: balance tidak berubah signifikan dalam rentang itu, dan
# tetap ikut turun/naik mengikuti hasil trade dalam hitungan menit.
_balance_cache = {"value": 0.0, "fetched_at": 0.0}
_BALANCE_CACHE_TTL_SECONDS = 60

def _get_cached_balance() -> float:
    now = time.time()
    if _balance_cache["value"] <= 0 or (now - _balance_cache["fetched_at"]) > _BALANCE_CACHE_TTL_SECONDS:
        try:
            import scanner  # import lokal — hindari circular import di level modul
            info = scanner.get_account_balance()
            bal  = info.get("available_balance", 0) or info.get("balance", 0)
            if bal > 0:
                _balance_cache["value"]      = bal
                _balance_cache["fetched_at"] = now
        except Exception:
            pass  # gagal fetch — pakai nilai cache lama / fallback di bawah
    return _balance_cache["value"]

def get_max_daily_loss() -> float:
    """
    Batas kerugian harian dalam DOLAR, dihitung dari persentase balance.

    Dulu ini angka tetap di config ($9) sementara position sizing berbasis
    persentase balance — timpang jauh, sampai satu SL normal saja sudah
    melampaui batas harian. Sekarang keduanya sama-sama proporsional
    terhadap balance, jadi konsisten di skala akun manapun.
    """
    balance = _get_cached_balance()
    if balance <= 0:
        return config.MAX_DAILY_LOSS_FALLBACK
    return balance * config.MAX_DAILY_LOSS_PCT / 100

# ============================================
# STATE
# ============================================
# CATATAN: cooldown SL SEBELUMNYA disimpan di variabel Python biasa
# (last_sl_time) — itu HILANG setiap kali proses bot di-restart, sehingga
# bot "lupa" baru saja kena SL dan bisa re-entry ke symbol yang sama tanpa
# jeda. Ini penyebab konkret AIOUSDT kena SL dua kali beruntun dalam sesi
# testing. Sekarang waktu SL terakhir dibaca langsung dari trade_history
# di database (memory.get_last_sl_time()) — persisten lintas restart, dan
# bahkan sudah benar di CYCLE YANG SAMA saat SL terjadi, karena posisi
# terbuka selalu dimonitor & ditutup di STEP 1 sebelum scanning di STEP 3.

# ============================================
# PRE-TRADE CHECKS
# ============================================

def can_trade(symbol: str = None) -> tuple[bool, str]:
    """
    Cek semua kondisi sebelum boleh trade
    symbol: kalau diisi, cek juga apakah symbol ini sudah punya posisi terbuka
    Return: (boleh_trade, alasan)
    """
    # Cek 1: Max daily loss
    daily_pnl = memory.get_daily_pnl()
    max_daily_loss = get_max_daily_loss()
    if daily_pnl <= -max_daily_loss:
        return False, f"Max daily loss tercapai (${daily_pnl:.2f} / -${max_daily_loss:.2f} = {config.MAX_DAILY_LOSS_PCT}% balance)"

    # Cek 2: Max open trades
    open_trades = memory.get_open_trades()
    if len(open_trades) >= config.MAX_OPEN_TRADES:
        return False, f"Max open trades tercapai ({len(open_trades)}/{config.MAX_OPEN_TRADES})"

    # Cek 3: Jangan double-entry di symbol yang sama
    if symbol and any(t["symbol"] == symbol for t in open_trades):
        return False, f"Sudah ada posisi terbuka di {symbol}"

    # Cek 3b: Symbol ini sudah kena SL berkali-kali HARI INI — blokir
    # sisa hari untuk symbol ini saja (bukan cooldown global di Cek 4).
    # Beda dari Cek 3: ini TIDAK menghentikan scan kandidat lain, cuma
    # menghindari symbol spesifik ini — lihat penanganannya di main.py
    # (reason ini harus masuk daftar "skip symbol, lanjut scan").
    if symbol:
        sl_count = memory.get_symbol_sl_count_today(symbol)
        if sl_count >= config.MAX_SL_PER_SYMBOL_PER_DAY:
            return False, f"{symbol} sudah kena SL {sl_count}x hari ini — dihindari sisa hari"

    # Cek 4: Cooldown setelah stop loss — dibaca dari DATABASE, bukan
    # variabel memori, supaya cooldown-nya tahan restart.
    last_sl_iso = memory.get_last_sl_time()
    if last_sl_iso:
        try:
            last_sl_dt = datetime.fromisoformat(last_sl_iso)
            elapsed = (datetime.now() - last_sl_dt).total_seconds() / 60
            if elapsed < config.COOLDOWN_AFTER_SL:
                remaining = config.COOLDOWN_AFTER_SL - elapsed
                return False, f"Cooldown SL aktif — {remaining:.0f} menit lagi"
        except (ValueError, TypeError):
            pass  # timestamp tidak valid — jangan blokir trading karena ini

    return True, "OK"

def check_confidence(confidence: float) -> tuple[bool, str]:
    """Cek apakah confidence AI cukup untuk trade"""
    if confidence < config.MIN_CONFIDENCE_TO_TRADE:
        return False, f"Confidence terlalu rendah ({confidence:.0%} < {config.MIN_CONFIDENCE_TO_TRADE:.0%})"
    return True, "OK"

def set_sl_cooldown():
    """
    Dipanggil saat SL terdeteksi di check_position_status(), SEBELUM
    executor.close_position() benar-benar mencatat trade sebagai CLOSED
    di database. Fungsi ini sekarang HANYA untuk logging — cooldown yang
    sesungguhnya otomatis aktif begitu memory.save_trade_close() menyimpan
    close_reason='SL', karena can_trade() membaca waktu SL terakhir
    langsung dari database (lihat memory.get_last_sl_time()).
    """
    print(f"⏳ SL terdeteksi — cooldown {config.COOLDOWN_AFTER_SL} menit akan aktif begitu posisi tercatat closed")

# ============================================
# POSITION SIZING
# ============================================

def calculate_position_size(symbol_info: dict, price: float, balance_info: dict = None, ai_result: dict = None) -> dict:
    """
    Hitung ukuran posisi secara dinamis berdasarkan:
    1. Persentase dari available balance (BASE_RISK_PCT)
    2. allocation_pct dari AI (confidence-scaled)
    3. Win/loss streak (anti-martingale)
    4. Dibatasi oleh MIN_MARGIN_PCT / MAX_MARGIN_PCT dari balance

    Args:
        symbol_info  : info exchange (step_size, min_qty, qty_precision)
        price        : harga saat ini
        balance_info : dict dari scanner.get_account_balance(), opsional
        ai_result    : dict hasil brain.make_decision(), opsional (untuk allocation_pct)

    Return: dict berisi qty, margin, notional, dan detail breakdown untuk logging
    """
    fallback_used = False

    # ── Tentukan margin dasar ──
    if config.DYNAMIC_SIZING_ENABLED and balance_info and balance_info.get("available_balance", 0) > 0:
        balance = balance_info["available_balance"]

        # 1. Base risk dari balance
        base_margin = balance * config.BASE_RISK_PCT

        # 2. Scale berdasarkan allocation_pct dari AI (default 100% kalau tidak ada)
        allocation_pct = 100.0
        if ai_result:
            allocation_pct = ai_result.get("allocation_pct", 100.0)
            # AI kadang return 0 untuk SKIP, tapi fungsi ini hanya dipanggil
            # saat decision != SKIP, jadi guard minimal value
            if allocation_pct <= 0:
                allocation_pct = 100.0
            allocation_pct = max(10.0, min(100.0, allocation_pct))  # clamp 10-100%

        margin = base_margin * (allocation_pct / 100.0)

        # 3. Streak multiplier (anti-martingale)
        if config.STREAK_SCALING_ENABLED:
            streak = memory.get_current_streak()
            multiplier = 1.0
            if streak["type"] == "WIN":
                multiplier = 1.0 + (streak["count"] * config.STREAK_WIN_STEP)
            elif streak["type"] == "LOSS":
                multiplier = 1.0 - (streak["count"] * config.STREAK_LOSS_STEP)

            multiplier = max(config.STREAK_MULTIPLIER_MIN, min(config.STREAK_MULTIPLIER_MAX, multiplier))
            margin = margin * multiplier
        else:
            streak = {"type": "NONE", "count": 0}
            multiplier = 1.0

        # 4. Clamp ke MIN/MAX persen balance
        min_margin = balance * config.MIN_MARGIN_PCT
        max_margin = balance * config.MAX_MARGIN_PCT
        margin = max(min_margin, min(max_margin, margin))

    else:
        # Fallback — static margin dari config (balance tidak tersedia / dynamic disabled)
        fallback_used = True
        margin = config.MARGIN_PER_TRADE
        balance = None
        allocation_pct = 100.0
        streak = {"type": "NONE", "count": 0}
        multiplier = 1.0

    # ── Hitung quantity ──
    notional = margin * config.LEVERAGE
    raw_qty  = notional / price

    step_size     = symbol_info.get("step_size", 0.001)
    qty_precision = symbol_info.get("qty_precision", 3)

    if step_size > 0:
        qty = round(round(raw_qty / step_size) * step_size, qty_precision)
    else:
        qty = round(raw_qty, qty_precision)

    min_qty = symbol_info.get("min_qty", 0.001)
    if qty < min_qty:
        qty = min_qty
        # Recalculate margin actual berdasarkan qty yang dipaksa naik ke min_qty
        notional = qty * price
        margin   = notional / config.LEVERAGE

    return {
        "qty"            : qty,
        "margin"         : round(margin, 4),
        "notional"       : round(notional, 4),
        "balance"        : round(balance, 2) if balance is not None else None,
        "allocation_pct" : round(allocation_pct, 1),
        "streak_type"    : streak["type"],
        "streak_count"   : streak["count"],
        "streak_multiplier": round(multiplier, 2),
        "fallback_used"  : fallback_used
    }

def calculate_tp_sl_prices(
    side: str,
    entry_price: float,
    ai_tp: float = 0,
    ai_sl: float = 0
) -> tuple[float, float]:
    """
    Hitung harga TP dan SL
    Prioritaskan harga dari AI, fallback ke config
    """
    if side == "LONG":
        sl = ai_sl if ai_sl > 0 else round(entry_price * (1 - config.STOP_LOSS_PCT / 100), 6)
        tp = ai_tp if ai_tp > 0 else round(entry_price * (1 + config.TAKE_PROFIT_PCT / 100), 6)
    else:  # SHORT
        sl = ai_sl if ai_sl > 0 else round(entry_price * (1 + config.STOP_LOSS_PCT / 100), 6)
        tp = ai_tp if ai_tp > 0 else round(entry_price * (1 - config.TAKE_PROFIT_PCT / 100), 6)

    return tp, sl

def extend_tp_price(side: str, entry_price: float) -> float:
    """
    Hitung TP baru saat dynamic TP extend dipicu.
    LONG  -> entry + DYNAMIC_TP_EXTENDED%
    SHORT -> entry - DYNAMIC_TP_EXTENDED%
    """
    if side == "LONG":
        return round(entry_price * (1 + config.DYNAMIC_TP_EXTENDED / 100), 6)
    else:  # SHORT
        return round(entry_price * (1 - config.DYNAMIC_TP_EXTENDED / 100), 6)

# ============================================
# REVIEW TESIS POSISI AKTIF
# ============================================

def tighten_sl_price(
    side: str,
    entry_price: float,
    current_sl: float,
    current_price: float,
    severity: str
) -> float | None:
    """
    Hitung SL baru yang LEBIH KETAT, dipicu oleh review tesis AI
    (brain.make_thesis_review). AI HANYA memilih severity ("mild"/"strong")
    — angka SL selalu dihitung deterministik di sini, AI tidak pernah
    menghitung harga sendiri (pelajaran yang sama dengan entry decision:
    LLM tidak reliable untuk aritmetika harga).

    - mild   : titik tengah antara SL lama dan entry price
    - strong : breakeven (entry price)

    Guard WAJIB: hasil akhir harus benar-benar LEBIH KETAT dari current_sl
    (kalau tidak, return None — pemanggil treat sebagai HOLD, tidak ada
    yang diterapkan). Kalau severity valid tapi hasil hitungnya sudah
    "kelewatan" harga sekarang (kasus langka: harga sudah bergerak jauh
    saat review terjadi), SL di-clamp persis ke harga sekarang — bukan
    ditolak — supaya polling SL normal (tiap 10 detik) yang menutup
    posisinya sebagai "SL" di cycle berikutnya, alih-alih diam-diam
    mengabaikan sinyal bahwa posisi ini seharusnya sudah keluar.

    Return SL baru (float), atau None kalau severity tidak valid / hasil
    hitung ternyata tidak lebih ketat dari SL yang sudah ada.
    """
    if severity not in ("mild", "strong"):
        return None

    candidate = entry_price if severity == "strong" else (current_sl + entry_price) / 2
    candidate = round(candidate, 6)

    if side == "LONG":
        # SL LONG di bawah harga — lebih ketat berarti lebih TINGGI.
        if candidate <= current_sl:
            return None  # bukan perbaikan, abaikan
        return min(candidate, current_price)
    else:  # SHORT
        # SL SHORT di atas harga — lebih ketat berarti lebih RENDAH.
        if candidate >= current_sl:
            return None
        return max(candidate, current_price)

# ============================================
# POSITION MONITOR
# ============================================

def check_position_status(
    trade_id: int,
    side: str,
    entry_price: float,
    current_price: float,
    tp_price: float,
    sl_price: float,
    highest_price: float,
    lowest_price: float,
    duration_minutes: float = 0,
    leverage: int = None
) -> dict:
    """
    Cek apakah posisi perlu ditutup
    Return: dict dengan action dan alasan

    duration_minutes dipakai HANYA untuk stagnant breaker di akhir fungsi
    (lihat bawah) — default 0 supaya pemanggilan lama tanpa argumen ini
    tetap jalan (breaker otomatis tidak aktif kalau durasi tidak dikirim).

    leverage: leverage AKTUAL trade ini (dari DB) — default None jatuh ke
    config.LEVERAGE untuk kompatibilitas mundur. Sebelumnya fungsi ini
    SELALU pakai config.LEVERAGE global, tidak konsisten dengan
    executor.py/main.py yang sudah pakai leverage per-trade dari DB.
    Tidak berdampak nyata sekarang (LEVERAGE statis di config), tapi jadi
    sumber bug diam-diam kalau nanti leverage per-trade jadi dinamis.
    """
    lev = leverage if leverage is not None else config.LEVERAGE

    if side == "LONG":
        pnl_pct = (current_price - entry_price) / entry_price * 100 * lev

        # Cek Take Profit
        if current_price >= tp_price:
            # Dynamic TP — extend kalau momentum masih kuat
            if config.DYNAMIC_TP_ENABLED:
                return {
                    "action" : "CHECK_EXTEND",
                    "reason" : "TP tercapai — cek apakah perlu extend"
                }
            return {"action": "CLOSE", "reason": "TP"}

        # Cek Stop Loss
        if current_price <= sl_price:
            set_sl_cooldown()
            return {"action": "CLOSE", "reason": "SL"}

        # Trailing Stop
        if config.TRAILING_STOP_ENABLED and pnl_pct >= config.TRAILING_STOP_ACTIVATION:
            trailing_sl = highest_price * (1 - config.TRAILING_STOP_CALLBACK / 100)
            if current_price <= trailing_sl:
                return {"action": "CLOSE", "reason": "TRAILING"}

    else:  # SHORT
        pnl_pct = (entry_price - current_price) / entry_price * 100 * lev

        # Cek Take Profit
        if current_price <= tp_price:
            if config.DYNAMIC_TP_ENABLED:
                return {
                    "action" : "CHECK_EXTEND",
                    "reason" : "TP tercapai — cek apakah perlu extend"
                }
            return {"action": "CLOSE", "reason": "TP"}

        # Cek Stop Loss
        if current_price >= sl_price:
            set_sl_cooldown()
            return {"action": "CLOSE", "reason": "SL"}

        # Trailing Stop
        if config.TRAILING_STOP_ENABLED and pnl_pct >= config.TRAILING_STOP_ACTIVATION:
            trailing_sl = lowest_price * (1 + config.TRAILING_STOP_CALLBACK / 100)
            if current_price >= trailing_sl:
                return {"action": "CLOSE", "reason": "TRAILING"}

    # ── Stagnant Position Breaker ──
    # Murni deterministik, TANPA AI (beda dengan thesis review yang
    # menangkap PEMBALIKAN sinyal — ini menangkap STAGNASI: posisi yang
    # sudah lama terbuka tapi PnL-nya tidak pernah berkomitmen ke arah
    # manapun, tidak dekat TP maupun SL). Gate GANDA supaya tidak salah
    # tutup posisi yang sedang progres jelas menuju TP/SL:
    # 1. Sudah terbuka lebih lama dari MAX_HOLD_MINUTES
    # 2. PnL saat ini ada di "zona mati" (±STAGNANT_PNL_BAND_PCT) — kalau
    #    sedang untung/rugi besar, itu BUKAN stagnan, biarkan TP/SL/trailing
    #    yang urus, breaker ini tidak boleh ikut campur.
    if (
        config.STAGNANT_BREAKER_ENABLED
        and duration_minutes >= config.MAX_HOLD_MINUTES
        and abs(pnl_pct) <= config.STAGNANT_PNL_BAND_PCT
    ):
        return {
            "action": "CLOSE",
            "reason": "STAGNANT",
            "pnl_pct": pnl_pct
        }

    # Posisi masih aman
    return {"action": "HOLD", "reason": "Dalam range normal", "pnl_pct": pnl_pct}

def should_extend_tp(current_rsi: float, side: str) -> bool:
    """
    Putuskan apakah TP perlu di-extend
    Berdasarkan RSI saat ini
    """
    if side == "LONG":
        # Kalau RSI masih kuat → tahan posisi lebih lama
        return current_rsi >= config.DYNAMIC_TP_RSI_THRESH
    else:  # SHORT
        # Kalau RSI masih lemah → tahan short lebih lama
        return current_rsi <= (100 - config.DYNAMIC_TP_RSI_THRESH)

# ============================================
# SUMMARY
# ============================================

def print_risk_status():
    """Print status risk management saat ini"""
    daily_pnl    = memory.get_daily_pnl()
    daily_trades = memory.get_daily_trade_count()
    open_trades  = memory.get_open_trades()
    can, reason  = can_trade()

    # Cooldown SL untuk display — dibaca dari DB, konsisten dengan can_trade()
    cooldown_active = False
    last_sl_iso = memory.get_last_sl_time()
    if last_sl_iso:
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(last_sl_iso)).total_seconds() / 60
            cooldown_active = elapsed < config.COOLDOWN_AFTER_SL
        except (ValueError, TypeError):
            pass

    print(f"\n🛡️  RISK STATUS")
    print(f"{'='*40}")
    print(f"Daily PnL      : ${daily_pnl:.2f} / -${get_max_daily_loss():.2f} ({config.MAX_DAILY_LOSS_PCT}% balance)")
    print(f"Daily trades   : {daily_trades}")
    print(f"Open trades    : {len(open_trades)}/{config.MAX_OPEN_TRADES}")
    print(f"Cooldown aktif : {'Ya' if cooldown_active else 'Tidak'}")
    print(f"Boleh trade    : {'✅ Ya' if can else f'❌ Tidak — {reason}'}")
    print(f"{'='*40}")


if __name__ == "__main__":
    from scanner import get_symbol_info

    print("🧪 Test Risk Manager\n")

    # Init db dulu
    memory.init_db()

    # Test 1: Can trade?
    print("1️⃣  Cek apakah boleh trade...")
    can, reason = can_trade()
    print(f"   Boleh trade: {can} — {reason}")

    # Test 2: Position sizing
    print("\n2️⃣  Hitung position size...")
    dummy_info = {
        "qty_precision": 3,
        "step_size"    : 0.001,
        "min_qty"      : 0.001
    }
    dummy_balance = {"available_balance": 45.0}
    sizing = calculate_position_size(dummy_info, price=150.0, balance_info=dummy_balance, ai_result={"allocation_pct": 100})
    print(f"   Harga          : $150")
    print(f"   Balance        : ${dummy_balance['available_balance']}")
    print(f"   Margin         : ${sizing['margin']}")
    print(f"   Notional       : ${sizing['notional']}")
    print(f"   Quantity       : {sizing['qty']}")
    print(f"   Streak         : {sizing['streak_type']} x{sizing['streak_count']} (mult {sizing['streak_multiplier']})")
    print(f"   Allocation     : {sizing['allocation_pct']}%")
    print(f"   Fallback used  : {sizing['fallback_used']}")

    # Test 3: TP/SL calculation
    print("\n3️⃣  Hitung TP/SL...")
    entry = 150.0
    tp, sl = calculate_tp_sl_prices("LONG", entry)
    print(f"   Entry  : ${entry}")
    print(f"   TP     : ${tp} (+{config.TAKE_PROFIT_PCT}%)")
    print(f"   SL     : ${sl} (-{config.STOP_LOSS_PCT}%)")

    tp, sl = calculate_tp_sl_prices("SHORT", entry)
    print(f"\n   Entry  : ${entry} (SHORT)")
    print(f"   TP     : ${tp} (-{config.TAKE_PROFIT_PCT}%)")
    print(f"   SL     : ${sl} (+{config.STOP_LOSS_PCT}%)")

    # Test 3b: Extended TP
    print("\n3️⃣b Hitung Extended TP...")
    ext_tp = extend_tp_price("LONG", entry)
    print(f"   Extended TP (LONG) : ${ext_tp} (+{config.DYNAMIC_TP_EXTENDED}%)")
    ext_tp = extend_tp_price("SHORT", entry)
    print(f"   Extended TP (SHORT): ${ext_tp} (-{config.DYNAMIC_TP_EXTENDED}%)")

    # Test 4: Position status check
    print("\n4️⃣  Simulasi cek posisi LONG...")
    tp_p, sl_p = calculate_tp_sl_prices("LONG", 150.0)
    scenarios = [
        (153.0, "Profit — belum TP"),
        (156.0, "Hit Take Profit"),
        (147.0, "Hit Stop Loss"),
        (150.5, "Sideways"),
    ]
    for curr_price, label in scenarios:
        status = check_position_status(
            trade_id=1, side="LONG",
            entry_price=150.0, current_price=curr_price,
            tp_price=tp_p, sl_price=sl_p,
            highest_price=curr_price, lowest_price=147.0
        )
        print(f"   [{label}] Harga: ${curr_price} → {status['action']} ({status['reason']})")

    print()
    print_risk_status()