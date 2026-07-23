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
PAPER_TRADE_MODE = True  # Ganti False kalau siap live

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
    tp_price    : float,
    sl_price    : float,
    ai_result   : dict,
    indicators  : dict
) -> dict:
    """
    Buka posisi baru
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
        print(f"   TP       : {tp_price}")
        print(f"   SL       : {sl_price}")
        print(f"   Mode     : {'🧪 PAPER' if PAPER_TRADE_MODE else '🔴 LIVE'}")

        # Set leverage dulu
        set_leverage(symbol)

        if PAPER_TRADE_MODE:
            # Simulasi — ambil harga terkini
            ticker      = client.futures_symbol_ticker(symbol=symbol)
            entry_price = float(ticker["price"])

            print(f"   Entry    : ${entry_price}")
            print(f"✅ [PAPER] Posisi dibuka secara simulasi")

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

            print(f"   Entry    : ${entry_price}")
            print(f"   Order ID : {order['orderId']}")

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
            sl_price      = sl_price
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
        # Hitung TP/SL
        tp, sl = risk_manager.calculate_tp_sl_prices(
            side        = decision,
            entry_price = price,
            ai_tp       = ai_result.get("take_profit_price", 0),
            ai_sl       = ai_result.get("stop_loss_price", 0)
        )

        # Buka posisi
        trade = open_position(
            symbol     = symbol,
            side       = decision,
            quantity   = qty,
            margin     = margin,
            tp_price   = tp,
            sl_price   = sl,
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