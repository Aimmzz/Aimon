import time
from datetime import datetime
from binance.client import Client
from binance.enums import *
import config
import memory
import risk_manager

# ============================================
# PAPER TRADE MODE
# ============================================
PAPER_TRADE_MODE = False  # Ganti False kalau siap live

# ============================================
# CLIENT
# ============================================

def get_client() -> Client:
    return Client(config.API_KEY, config.API_SECRET, testnet=config.USE_TESTNET)

# ============================================
# LEVERAGE SETUP
# ============================================

def set_leverage(symbol: str) -> bool:
    """Set leverage untuk symbol tertentu"""
    try:
        if PAPER_TRADE_MODE:
            print(f"   [PAPER] Set leverage {config.LEVERAGE}x untuk {symbol}")
            return True

        client = get_client()
        client.futures_change_leverage(
            symbol=symbol,
            leverage=config.LEVERAGE
        )
        print(f"   ✅ Leverage {config.LEVERAGE}x diset untuk {symbol}")
        return True
    except Exception as e:
        print(f"   ❌ Set leverage error: {e}")
        return False

# ============================================
# OPEN POSITION
# ============================================

def open_position(
    symbol      : str,
    side        : str,      # LONG atau SHORT
    quantity    : float,
    margin      : float,
    ai_result   : dict,
    indicators  : dict
) -> dict:
    """
    Buka posisi baru.

    TP/SL dihitung DI SINI, dari entry_price yang BENAR-BENAR didapat
    saat order dieksekusi — BUKAN diterima sebagai parameter dari
    caller. Sebelumnya caller (main.py) menghitung tp/sl dari harga
    candidate saat SCAN, lalu baru memanggil fungsi ini yang fetch
    entry_price BARU secara terpisah. Untuk pair volatile (semua
    kandidat scanner memang selalu yang paling bergejolak) dikombinasi
    proses AI yang makan 5-20 detik per kandidat, harga bisa bergeser
    cukup jauh di antara keduanya — SL yang dihitung dari harga basi
    itu jadi salah posisi relatif ke harga entry asli. Bukti nyata:
    trade closed SL di durasi 0 menit dengan overshoot jauh dari
    -7.5% yang seharusnya (SL 1.5% x leverage 5x), atau sebaliknya SL
    yang nyaris menempel entry (-0.1%) — dua-duanya gejala SL dihitung
    dari harga yang sudah tidak relevan lagi saat order benar-benar jalan.

    margin: margin USDT aktual yang dipakai untuk trade ini (dari dynamic sizing
            atau fallback config.MARGIN_PER_TRADE). Disimpan ke DB dan dipakai
            lagi saat close_position untuk menghitung PnL yang akurat.
    Return: dict dengan trade_id dan status
    """
    try:
        client      = get_client()
        binance_side = SIDE_BUY if side == "LONG" else SIDE_SELL

        print(f"\n{'='*50}")
        print(f"⚡ {'[PAPER] ' if PAPER_TRADE_MODE else ''}OPEN {side} {symbol}")
        print(f"   Quantity : {quantity}")
        print(f"   Margin   : ${margin:.2f}")
        print(f"   Mode     : {'🧪 PAPER' if PAPER_TRADE_MODE else '🔴 LIVE'}")

        # Set leverage dulu
        set_leverage(symbol)

        if PAPER_TRADE_MODE:
            # Simulasi — ambil harga terkini
            ticker      = client.futures_symbol_ticker(symbol=symbol)
            entry_price = float(ticker["price"])

        else:
            # Live — kirim market order
            order = client.futures_create_order(
                symbol    = symbol,
                side      = binance_side,
                type      = ORDER_TYPE_MARKET,
                quantity  = quantity
            )

            # Ambil entry price dari order fill
            entry_price = float(order.get("avgPrice", 0))
            if entry_price == 0:
                ticker      = client.futures_symbol_ticker(symbol=symbol)
                entry_price = float(ticker["price"])

            print(f"   Order ID : {order['orderId']}")

        # ── TP/SL dihitung DARI SINI — entry_price sudah final/aktual ──
        tp_price, sl_price = risk_manager.calculate_tp_sl_prices(
            side        = side,
            entry_price = entry_price,
            ai_tp       = ai_result.get("take_profit_price", 0),
            ai_sl       = ai_result.get("stop_loss_price", 0)
        )

        print(f"   Entry    : ${entry_price}")
        print(f"   TP       : {tp_price}")
        print(f"   SL       : {sl_price}")

        if PAPER_TRADE_MODE:
            print(f"✅ [PAPER] Posisi dibuka secara simulasi")
        else:
            # Pasang TP order
            tp_side = SIDE_SELL if side == "LONG" else SIDE_BUY
            client.futures_create_order(
                symbol        = symbol,
                side          = tp_side,
                type          = FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
                stopPrice     = tp_price,
                closePosition = True,
                timeInForce   = TIME_IN_FORCE_GTC
            )

            # Pasang SL order
            client.futures_create_order(
                symbol        = symbol,
                side          = tp_side,
                type          = FUTURE_ORDER_TYPE_STOP_MARKET,
                stopPrice     = sl_price,
                closePosition = True,
                timeInForce   = TIME_IN_FORCE_GTC
            )

            print(f"✅ Order live berhasil!")

        # Simpan ke database
        rsi  = indicators.get("rsi", 0) if indicators else 0
        macd = indicators.get("macd", {}).get("crossover", "") if indicators else ""

        trade_id = memory.save_trade_open(
            symbol        = symbol,
            side          = side,
            entry_price   = entry_price,
            margin        = margin,
            leverage      = config.LEVERAGE,
            ai_confidence = ai_result.get("confidence", 0),
            ai_reasoning  = ai_result.get("reasoning", ""),
            rsi           = rsi,
            macd          = macd,
            volume_spike  = indicators.get("vol_confirmed", False) if indicators else False,
            tp_price      = tp_price,
            sl_price      = sl_price,
            quantity      = quantity
        )

        return {
            "status"      : "ok",
            "trade_id"    : trade_id,
            "entry_price" : entry_price,
            "tp_price"    : tp_price,
            "sl_price"    : sl_price,
            "quantity"    : quantity,
            "margin"      : margin,
            "side"        : side,
            "symbol"      : symbol
        }

    except Exception as e:
        print(f"❌ Open position error: {e}")
        return {"status": "error", "reason": str(e)}

# ============================================
# CLOSE POSITION
# ============================================

def _get_live_net_pnl(client, symbol: str, entry_time: str) -> dict | None:
    """
    Ambil PnL BERSIH aktual dari histori fill Binance untuk posisi yang
    baru ditutup — realizedPnl (PnL harga versi exchange) DIKURANGI
    commission (fee trading) dari SEMUA fill sejak posisi dibuka, dua
    sisi (open + close).

    Kenapa perlu: bot menghitung PnL cuma dari pergerakan harga, TIDAK
    termasuk fee — kejadian nyata trade live pertama (EULUSDT): bot catat
    +$0.07, Binance aktual +$0.03 karena fee memakan ~separuh gross di
    trade kecil/cepat. Dengan ini, angka di DB = angka di app Binance.

    Return {"net_pnl": float, "fees": float} atau None kalau gagal
    (pemanggil fallback ke perhitungan harga). Catatan batasan:
    - commission non-USDT (misal bayar fee pakai BNB) di-skip dari
      pengurangan (jarang, hanya kalau user aktifkan BNB fee discount)
    - funding fee tidak termasuk (bukan bagian account_trades; untuk
      posisi menit-jam dampaknya kecil)
    """
    try:
        start_ms = int(datetime.fromisoformat(entry_time).timestamp() * 1000) - 2000
        fills = client.futures_account_trades(symbol=symbol, startTime=start_ms)
        if not fills:
            return None

        gross = sum(float(t.get("realizedPnl", 0)) for t in fills)
        fees  = sum(
            float(t.get("commission", 0))
            for t in fills
            if t.get("commissionAsset") in ("USDT", "USDC", "BUSD")
        )
        return {"net_pnl": gross - fees, "fees": fees}
    except Exception as e:
        print(f"   ⚠️  Gagal ambil PnL bersih dari exchange: {str(e)[:60]} — pakai perhitungan harga")
        return None


def close_position(
    trade_id    : int,
    symbol      : str,
    side        : str,
    quantity    : float,
    entry_price : float,
    entry_time  : str,
    close_reason: str,
    margin      : float = None,
    leverage    : int = None
) -> dict:
    """
    Tutup posisi yang sedang terbuka

    margin/leverage: kalau tidak diberikan, akan diambil dari config
    (fallback untuk kompatibilitas). Sebaiknya selalu diteruskan dari
    trade record di DB (trade["margin"], trade["leverage"]) supaya
    PnL dihitung dengan ukuran posisi yang sebenarnya dipakai.
    """
    try:
        client       = get_client()
        close_side   = SIDE_SELL if side == "LONG" else SIDE_BUY

        actual_margin   = margin if margin is not None else config.MARGIN_PER_TRADE
        actual_leverage = leverage if leverage is not None else config.LEVERAGE

        print(f"\n{'='*50}")
        print(f"🔒 {'[PAPER] ' if PAPER_TRADE_MODE else ''}CLOSE {side} {symbol}")
        print(f"   Alasan   : {close_reason}")
        print(f"   Mode     : {'🧪 PAPER' if PAPER_TRADE_MODE else '🔴 LIVE'}")

        # Ambil harga terkini
        ticker      = client.futures_symbol_ticker(symbol=symbol)
        exit_price  = float(ticker["price"])

        if not PAPER_TRADE_MODE:
            # Cancel semua order terbuka dulu (TP/SL)
            try:
                client.futures_cancel_all_open_orders(symbol=symbol)
            except:
                pass

            # Market order close
            client.futures_create_order(
                symbol          = symbol,
                side            = close_side,
                type            = ORDER_TYPE_MARKET,
                quantity        = quantity,
                reduceOnly      = True
            )

        # Hitung PnL — pakai margin & leverage AKTUAL dari trade ini,
        # bukan config statis, supaya konsisten dengan dynamic sizing.
        if side == "LONG":
            pnl_pct  = (exit_price - entry_price) / entry_price * 100 * actual_leverage
            pnl_usdt = (exit_price - entry_price) / entry_price * actual_margin * actual_leverage
        else:
            pnl_pct  = (entry_price - exit_price) / entry_price * 100 * actual_leverage
            pnl_usdt = (entry_price - exit_price) / entry_price * actual_margin * actual_leverage

        # ── LIVE: timpa dengan PnL BERSIH aktual dari exchange ──
        # Perhitungan harga di atas tetap dihitung dulu sebagai fallback.
        # Di live, angka final diambil dari fill history Binance (sudah
        # termasuk fee dua sisi) supaya DB = kenyataan, bukan gross.
        if not PAPER_TRADE_MODE:
            live_pnl = _get_live_net_pnl(client, symbol, entry_time)
            if live_pnl is not None:
                gross_calc = pnl_usdt
                pnl_usdt   = live_pnl["net_pnl"]
                if actual_margin:
                    pnl_pct = pnl_usdt / actual_margin * 100
                print(f"   Fee      : ${live_pnl['fees']:.4f} (gross ${gross_calc:.2f} → net ${pnl_usdt:.2f})")

        # Hitung durasi — total_seconds(), BUKAN .seconds (yang wrap tiap
        # 24 jam, bug yang sama sudah diperbaiki di main.py tapi kelewat
        # di sini karena executor.py punya perhitungan durasi terpisah)
        try:
            entry_dt     = datetime.fromisoformat(entry_time)
            duration_min = int((datetime.now() - entry_dt).total_seconds() / 60)
        except Exception:
            duration_min = 0

        print(f"   Exit     : ${exit_price}")
        print(f"   Margin   : ${actual_margin:.2f} (x{actual_leverage})")
        print(f"   PnL      : ${pnl_usdt:.2f} ({pnl_pct:.1f}%)")

        # Simpan ke database
        memory.save_trade_close(
            trade_id     = trade_id,
            exit_price   = exit_price,
            pnl_usdt     = pnl_usdt,
            pnl_percent  = pnl_pct,
            close_reason = close_reason,
            duration_mins= duration_min
        )

        memory.update_daily_summary()

        return {
            "status"     : "ok",
            "exit_price" : exit_price,
            "pnl_usdt"   : pnl_usdt,
            "pnl_pct"    : pnl_pct
        }

    except Exception as e:
        print(f"❌ Close position error: {e}")
        return {"status": "error", "reason": str(e)}

# ============================================
# GET CURRENT PRICE
# ============================================

def get_current_price(symbol: str) -> float:
    """Ambil harga terkini"""
    try:
        client = get_client()
        ticker = client.futures_symbol_ticker(symbol=symbol)
        return float(ticker["price"])
    except Exception as e:
        print(f"❌ Get price error {symbol}: {e}")
        return 0.0

# ============================================
# REKONSILIASI STATE EXCHANGE ↔ DATABASE
# ============================================
# KHUSUS LIVE MODE. Di live, order TP/SL adalah order STOP_MARKET /
# TAKE_PROFIT_MARKET ASLI yang dieksekusi BINANCE — bukan bot. Kalau
# order itu ter-fill saat bot sedang polling (atau saat bot mati),
# bot tidak tahu: DB masih mencatat OPEN, bot terus memonitor "posisi
# hantu" dan bisa mengambil keputusan (thesis review, stagnant close,
# bahkan order close baru) atas posisi yang sudah tidak ada. Fungsi ini
# mendeteksi kondisi itu dan menyinkronkan DB dengan kenyataan exchange.

def reconcile_trade_with_exchange(trade: dict) -> bool:
    """
    Cek apakah posisi trade ini masih benar-benar ada di exchange.

    Return True kalau posisi TERNYATA SUDAH TIDAK ADA di exchange
    (di-close oleh Binance — TP/SL ter-fill, atau liquidation) dan DB
    sudah disinkronkan (record ditutup dengan data fill aktual).
    Return False kalau posisi masih terbuka normal (tidak ada tindakan).

    PAPER mode selalu return False — tidak ada posisi asli di exchange
    untuk direkonsiliasi, DB adalah satu-satunya kenyataan.
    """
    if PAPER_TRADE_MODE:
        return False

    symbol = trade["symbol"]
    try:
        client = get_client()

        # Cek posisi aktual di exchange
        positions = client.futures_position_information(symbol=symbol)
        pos_amt = 0.0
        for p in positions:
            pos_amt += abs(float(p.get("positionAmt", 0)))

        if pos_amt > 0:
            return False  # posisi masih hidup di exchange — normal

        # ── Posisi SUDAH TIDAK ADA di exchange, tapi DB bilang OPEN ──
        print(f"\n🔄 Rekonsiliasi: {symbol} sudah closed di exchange, sinkronkan DB...")

        # Cari harga fill aktual dari histori trade akun (paling akurat).
        # Fill penutup = trade dengan side berlawanan dari arah posisi.
        exit_price = 0.0
        close_side = "SELL" if trade["side"] == "LONG" else "BUY"
        try:
            recent = client.futures_account_trades(symbol=symbol, limit=20)
            closing_fills = [t for t in recent if t.get("side") == close_side]
            if closing_fills:
                # Weighted average kalau fill terpecah (partial fills)
                total_qty  = sum(float(t["qty"]) for t in closing_fills[-5:])
                total_quote = sum(float(t["qty"]) * float(t["price"]) for t in closing_fills[-5:])
                if total_qty > 0:
                    exit_price = total_quote / total_qty
        except Exception as e:
            print(f"   ⚠️  Gagal ambil fill history: {str(e)[:60]}")

        if exit_price == 0:
            exit_price = get_current_price(symbol)  # fallback terakhir
            if exit_price == 0:
                exit_price = trade["entry_price"]  # jangan sampai 0 — PnL dihitung nol daripada salah besar

        # Tentukan alasan close: bandingkan exit dengan TP/SL (toleransi 0.3%
        # untuk slippage STOP_MARKET). Kalau tidak dekat keduanya, EXTERNAL
        # (misal liquidation, atau manual close via app Binance).
        tp, sl = trade.get("tp_price", 0), trade.get("sl_price", 0)
        tol = 0.003
        if tp and abs(exit_price - tp) / tp <= tol:
            reason = "TP"
        elif sl and abs(exit_price - sl) / sl <= tol:
            reason = "SL"
        else:
            reason = "EXTERNAL"

        # Bersihkan order sisa (satu dari pasangan TP/SL masih resting
        # setelah pasangannya ter-fill — kalau dibiarkan, bisa ter-trigger
        # nanti dan MEMBUKA posisi baru yang tidak diinginkan)
        try:
            client.futures_cancel_all_open_orders(symbol=symbol)
        except Exception:
            pass

        # Hitung PnL dengan formula yang sama seperti close_position
        entry_price     = trade["entry_price"]
        actual_margin   = trade["margin"]
        actual_leverage = trade["leverage"]
        if trade["side"] == "LONG":
            pnl_pct  = (exit_price - entry_price) / entry_price * 100 * actual_leverage
            pnl_usdt = (exit_price - entry_price) / entry_price * actual_margin * actual_leverage
        else:
            pnl_pct  = (entry_price - exit_price) / entry_price * 100 * actual_leverage
            pnl_usdt = (entry_price - exit_price) / entry_price * actual_margin * actual_leverage

        # PnL BERSIH aktual dari exchange (termasuk fee) — jalur ini
        # selalu live (reconcile no-op di PAPER), jadi selalu dicoba
        live_pnl = _get_live_net_pnl(client, symbol, trade["timestamp"])
        if live_pnl is not None:
            pnl_usdt = live_pnl["net_pnl"]
            if actual_margin:
                pnl_pct = pnl_usdt / actual_margin * 100

        try:
            entry_dt     = datetime.fromisoformat(trade["timestamp"])
            duration_min = int((datetime.now() - entry_dt).total_seconds() / 60)
        except Exception:
            duration_min = 0

        memory.save_trade_close(
            trade_id     = trade["id"],
            exit_price   = exit_price,
            pnl_usdt     = pnl_usdt,
            pnl_percent  = pnl_pct,
            close_reason = reason,
            duration_mins= duration_min
        )
        memory.update_daily_summary()

        print(f"   ✅ #{trade['id']} {symbol} disinkronkan — {reason} @ {exit_price:.6g} | PnL ${pnl_usdt:+.2f} ({pnl_pct:+.1f}%)")
        return True

    except Exception as e:
        print(f"❌ Rekonsiliasi error {symbol}: {str(e)[:80]}")
        return False  # kalau gagal cek, JANGAN tutup DB — biarkan monitoring normal jalan


if __name__ == "__main__":
    import memory
    from scanner import get_symbol_info, get_top_gainers, get_account_balance
    from indicators import analyze, format_for_ai
    from brain import make_decision
    import risk_manager
    import config

    print("🧪 Test Executor (PAPER MODE)\n")

    memory.init_db()

    # Ambil pair untuk test
    gainers = get_top_gainers()
    if not gainers:
        print("❌ Tidak ada gainer")
        exit()

    symbol       = gainers[0]["symbol"]
    price        = gainers[0]["price"]
    price_change = gainers[0]["price_change"]

    print(f"🎯 Test dengan {symbol} @ ${price}\n")

    # Cek boleh trade
    can, reason = risk_manager.can_trade(symbol)
    print(f"🛡️  Boleh trade: {can} — {reason}\n")

    # Balance
    balance_info = get_account_balance()
    print(f"💰 Balance: ${balance_info['available_balance']:.2f}\n")

    # Symbol info
    sym_info = get_symbol_info(symbol)

    # AI decision
    df         = __import__("scanner").get_candles(symbol, config.SCALPING_TIMEFRAME)
    df_trend   = __import__("scanner").get_candles(symbol, config.TREND_TIMEFRAME)
    indicators = analyze(df, df_trend)
    formatted  = format_for_ai(symbol, price, price_change, indicators)
    ai_result  = make_decision(symbol, price, price_change, indicators, formatted)

    decision   = ai_result["decision"]
    confidence = ai_result["confidence"]

    # Hitung quantity (dynamic)
    sizing = risk_manager.calculate_position_size(sym_info, price, balance_info=balance_info, ai_result=ai_result)
    qty    = sizing["qty"]
    margin = sizing["margin"]
    print(f"📐 Sizing: margin=${margin:.2f} qty={qty} (streak={sizing['streak_type']}x{sizing['streak_count']}, alloc={sizing['allocation_pct']}%)\n")

    # Cek confidence
    conf_ok, conf_reason = risk_manager.check_confidence(confidence)

    if not can:
        print(f"⏭️  Skip — {reason}")
    elif not conf_ok:
        print(f"⏭️  Skip — {conf_reason}")
    elif decision == "SKIP":
        print(f"⏭️  AI memutuskan SKIP")
    else:
        # Buka posisi — TP/SL dihitung DI DALAM open_position() dari
        # entry_price aktual, tidak lagi dihitung di sini dari harga scan
        trade = open_position(
            symbol     = symbol,
            side       = decision,
            quantity   = qty,
            margin     = margin,
            ai_result  = ai_result,
            indicators = indicators
        )

        if trade["status"] == "ok":
            print(f"\n⏳ Simulasi tunggu 3 detik...")
            time.sleep(3)

            # Simulasi close
            close_position(
                trade_id     = trade["trade_id"],
                symbol       = symbol,
                side         = decision,
                quantity     = qty,
                entry_price  = trade["entry_price"],
                entry_time   = datetime.now().isoformat(),
                close_reason = "TEST_CLOSE",
                margin       = trade["margin"],
                leverage     = config.LEVERAGE
            )

    print("\n")
    memory.print_stats()