import requests
import pandas as pd
from datetime import datetime
from binance.client import Client
import config

# ============================================
# INIT CLIENT
# ============================================

# def get_client() -> Client:
#     """Buat Binance client sesuai mode"""
#     client = Client(config.API_KEY, config.API_SECRET)
#     if config.USE_TESTNET:
#         client.FUTURES_URL = config.TESTNET_BASE_URL
#     return client
def get_client() -> Client:
    """Buat Binance client sesuai mode menggunakan bawaan library"""
    # Menggunakan parameter 'testnet=True/False' bawaan python-binance
    # Library akan otomatis mengatur URL yang benar tanpa menumpuk versi path API
    client = Client(config.API_KEY, config.API_SECRET, testnet=config.USE_TESTNET)
    return client

# ============================================
# TOP GAINER SCANNER
# ============================================

def get_top_gainers() -> list:
    """
    Scan semua futures pair, filter berdasarkan:
    - Price change dalam range yang ditentukan
    - Volume minimum
    - Hanya pair USDT
    """
    try:
        client = get_client()
        tickers = client.futures_ticker()

        candidates = []

        for ticker in tickers:
            symbol       = ticker["symbol"]
            price_change = float(ticker["priceChangePercent"])
            volume_24h   = float(ticker["quoteVolume"])
            last_price   = float(ticker["lastPrice"])

            # Filter 1: Hanya pair USDT
            if not symbol.endswith("USDT"):
                continue

            # Filter 2: Exclude stablecoin pairs
            stables = ["BUSD", "USDC", "TUSD", "FDUSD", "DAI"]
            if any(s in symbol for s in stables):
                continue

            # Filter 3: Price change dalam range
            if not (config.MIN_PRICE_CHANGE_PCT <= abs(price_change) <= config.MAX_PRICE_CHANGE_PCT):
                continue

            # Filter 4: Volume minimum
            if volume_24h < config.MIN_VOLUME_24H:
                continue

            candidates.append({
                "symbol"       : symbol,
                "price"        : last_price,
                "price_change" : price_change,
                "volume_24h"   : volume_24h,
                "direction"    : "UP" if price_change > 0 else "DOWN"
            })

        # Sort by absolute price change (terbesar dulu)
        candidates.sort(key=lambda x: abs(x["price_change"]), reverse=True)

        # Ambil top N — 0 berarti TANPA batas (semua kandidat ikut discan).
        # Slicing candidates[:0] akan selalu kosong, jadi 0 HARUS ditangani
        # secara eksplisit di sini, bukan diserahkan ke slicing biasa.
        if config.TOP_GAINER_LIMIT and config.TOP_GAINER_LIMIT > 0:
            result = candidates[:config.TOP_GAINER_LIMIT]
        else:
            result = candidates

        print(f"\n🔍 Scanner menemukan {len(result)} kandidat dari {len(tickers)} pair")
        # Print maksimal 10 teratas saja — kalau tanpa batas, ratusan baris
        # akan membanjiri log tanpa nilai tambah
        for i, c in enumerate(result[:10], 1):
            arrow = "📈" if c["direction"] == "UP" else "📉"
            print(f"   {i:2}. {arrow} {c['symbol']:<12} {c['price_change']:+.2f}% | Vol: ${c['volume_24h']:,.0f}")
        if len(result) > 10:
            print(f"   ... dan {len(result) - 10} kandidat lainnya")

        return result

    except Exception as e:
        print(f"❌ Scanner error: {e}")
        return []

# ============================================
# KLINE / CANDLE DATA
# ============================================

def get_candles(symbol: str, interval: str, limit: int = 100) -> pd.DataFrame:
    """
    Ambil data candle untuk analisis teknikal
    Return DataFrame dengan kolom OHLCV
    """
    try:
        client = get_client()

        if config.USE_TESTNET:
            # Testnet kadang tidak punya semua pair
            # Fallback ke mainnet untuk data historis
            temp_client = Client("", "")
            klines = temp_client.futures_klines(
                symbol=symbol,
                interval=interval,
                limit=limit
            )
        else:
            klines = client.futures_klines(
                symbol=symbol,
                interval=interval,
                limit=limit
            )

        df = pd.DataFrame(klines, columns=[
            "timestamp", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"
        ])

        # Convert ke numeric
        for col in ["open", "high", "low", "close", "volume", "quote_volume"]:
            df[col] = pd.to_numeric(df[col])

        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df[["timestamp", "open", "high", "low", "close", "volume", "quote_volume"]]

        return df

    except Exception as e:
        print(f"❌ Get candles error {symbol}: {e}")
        return pd.DataFrame()

# ============================================
# ACCOUNT INFO
# ============================================

def get_account_balance() -> dict:
    """Ambil balance USDT dari futures account"""
    try:
        client = get_client()
        balances = client.futures_account_balance()

        for b in balances:
            if b["asset"] == "USDT":
                return {
                    "balance"            : float(b["balance"]),
                    "available_balance"  : float(b["availableBalance"]),
                    "unrealized_pnl"     : float(b.get("crossUnPnl", 0))
                }
        return {"balance": 0, "available_balance": 0, "unrealized_pnl": 0}

    except Exception as e:
        print(f"❌ Balance error: {e}")
        return {"balance": 0, "available_balance": 0, "unrealized_pnl": 0}

def get_open_positions() -> list:
    """Ambil semua posisi yang sedang terbuka"""
    try:
        client = get_client()
        positions = client.futures_position_information()

        open_pos = []
        for p in positions:
            amt = float(p["positionAmt"])
            if amt != 0:
                open_pos.append({
                    "symbol"        : p["symbol"],
                    "side"          : "LONG" if amt > 0 else "SHORT",
                    "size"          : abs(amt),
                    "entry_price"   : float(p["entryPrice"]),
                    "unrealized_pnl": float(p["unRealizedProfit"]),
                    "leverage"      : int(p["leverage"])
                })

        return open_pos

    except Exception as e:
        print(f"❌ Positions error: {e}")
        return []

# ============================================
# SYMBOL INFO
# ============================================

# Cache exchange info — payload-nya BESAR (semua symbol sekaligus) dan
# sebelumnya di-fetch ulang SETIAP kali mau entry. Isinya (precision,
# step_size, tick_size) nyaris tidak pernah berubah — cache 6 jam aman.
_exchange_info_cache = {"symbols": None, "fetched_at": 0}
_EXCHANGE_INFO_TTL_SECONDS = 6 * 3600

def get_symbol_info(symbol: str) -> dict:
    """Ambil info pair — precision, min qty, dll (cached, TTL 6 jam)"""
    try:
        import time as _time
        now = _time.time()
        if (_exchange_info_cache["symbols"] is None
                or now - _exchange_info_cache["fetched_at"] > _EXCHANGE_INFO_TTL_SECONDS):
            client = get_client()
            info   = client.futures_exchange_info()
            _exchange_info_cache["symbols"]    = info["symbols"]
            _exchange_info_cache["fetched_at"] = now

        symbols = _exchange_info_cache["symbols"]

        for s in symbols:
            if s["symbol"] == symbol:
                # Ambil filter quantity dan price
                qty_filter   = next((f for f in s["filters"] if f["filterType"] == "LOT_SIZE"), {})
                price_filter = next((f for f in s["filters"] if f["filterType"] == "PRICE_FILTER"), {})

                return {
                    "symbol"         : symbol,
                    "qty_precision"  : s.get("quantityPrecision", 3),
                    "price_precision": s.get("pricePrecision", 2),
                    "min_qty"        : float(qty_filter.get("minQty", 0.001)),
                    "step_size"      : float(qty_filter.get("stepSize", 0.001)),
                    "tick_size"      : float(price_filter.get("tickSize", 0.01))
                }

        return {}

    except Exception as e:
        print(f"❌ Symbol info error: {e}")
        return {}


if __name__ == "__main__":
    print("🧪 Test Scanner\n")

    # Test 1: Balance
    print("💰 Cek Balance...")
    balance = get_account_balance()
    print(f"   Balance          : ${balance['balance']:,.2f}")
    print(f"   Available        : ${balance['available_balance']:,.2f}")
    print(f"   Unrealized PnL   : ${balance['unrealized_pnl']:,.2f}")

    # Test 2: Top gainers
    print("\n📊 Scan Top Gainers...")
    gainers = get_top_gainers()

    # Test 3: Candle data untuk pair pertama
    if gainers:
        symbol = gainers[0]["symbol"]
        print(f"\n📈 Ambil candle {symbol}...")
        df = get_candles(symbol, config.SCALPING_TIMEFRAME, limit=10)
        if not df.empty:
            print(f"   Candle terbaru:")
            print(f"   Open  : {df['close'].iloc[-1]:.4f}")
            print(f"   Volume: {df['volume'].iloc[-1]:,.2f}")
            print(f"   ✅ {len(df)} candle berhasil diambil")

    # Test 4: Open positions
    print("\n📋 Open Positions...")
    positions = get_open_positions()
    if positions:
        for p in positions:
            print(f"   {p['symbol']} {p['side']} | PnL: ${p['unrealized_pnl']:.2f}")
    else:
        print("   Tidak ada posisi terbuka")