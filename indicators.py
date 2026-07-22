import pandas as pd
import numpy as np
import config

# ============================================
# RSI
# ============================================

def calculate_rsi(df: pd.DataFrame, period: int = None) -> pd.Series:
    """Relative Strength Index"""
    period = period or config.RSI_PERIOD
    delta  = df["close"].diff()
    gain   = delta.where(delta > 0, 0)
    loss   = -delta.where(delta < 0, 0)

    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()

    rs  = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

# ============================================
# MACD
# ============================================

def calculate_macd(df: pd.DataFrame) -> dict:
    """
    MACD — Moving Average Convergence Divergence
    Return: macd line, signal line, histogram, dan label crossover
    """
    ema_fast   = df["close"].ewm(span=config.MACD_FAST, adjust=False).mean()
    ema_slow   = df["close"].ewm(span=config.MACD_SLOW, adjust=False).mean()
    macd_line  = ema_fast - ema_slow
    signal     = macd_line.ewm(span=config.MACD_SIGNAL, adjust=False).mean()
    histogram  = macd_line - signal

    # Deteksi crossover
    prev_macd   = macd_line.iloc[-2]
    curr_macd   = macd_line.iloc[-1]
    prev_signal = signal.iloc[-2]
    curr_signal = signal.iloc[-1]

    crossover = "neutral"
    if prev_macd < prev_signal and curr_macd > curr_signal:
        crossover = "bullish_cross"   # MACD cross ke atas signal → potensi long
    elif prev_macd > prev_signal and curr_macd < curr_signal:
        crossover = "bearish_cross"   # MACD cross ke bawah signal → potensi short

    return {
        "macd"      : round(curr_macd, 6),
        "signal"    : round(curr_signal, 6),
        "histogram" : round(histogram.iloc[-1], 6),
        "crossover" : crossover,
        "trend"     : "bullish" if curr_macd > curr_signal else "bearish"
    }

# ============================================
# BOLLINGER BANDS
# ============================================

def calculate_bollinger_bands(df: pd.DataFrame) -> dict:
    """
    Bollinger Bands
    Harga dekat lower band → potential long
    Harga dekat upper band → potential short
    """
    period = config.BB_PERIOD
    std    = config.BB_STD

    sma        = df["close"].rolling(window=period).mean()
    rolling_std = df["close"].rolling(window=period).std()

    upper = sma + (rolling_std * std)
    lower = sma - (rolling_std * std)

    current_price = df["close"].iloc[-1]
    upper_val     = upper.iloc[-1]
    lower_val     = lower.iloc[-1]
    mid_val       = sma.iloc[-1]
    band_width    = upper_val - lower_val

    # Posisi harga dalam band (0 = lower, 1 = upper)
    position = (current_price - lower_val) / band_width if band_width > 0 else 0.5

    # Label posisi
    if position < 0.2:
        label = "near_lower"    # Oversold area → potential long
    elif position > 0.8:
        label = "near_upper"    # Overbought area → potential short
    else:
        label = "middle"        # Sideways

    return {
        "upper"    : round(upper_val, 6),
        "middle"   : round(mid_val, 6),
        "lower"    : round(lower_val, 6),
        "position" : round(position, 3),
        "label"    : label,
        "squeeze"  : band_width < (sma.iloc[-1] * 0.02)  # Band sangat sempit = volatilitas rendah
    }

# ============================================
# VOLUME ANALYSIS
# ============================================

def calculate_volume(df: pd.DataFrame) -> dict:
    """
    Analisis volume
    Volume spike = konfirmasi sinyal lebih kuat
    """
    current_vol = df["volume"].iloc[-1]
    avg_vol     = df["volume"].rolling(window=20).mean().iloc[-1]
    vol_ratio   = current_vol / avg_vol if avg_vol > 0 else 1

    # Arah volume — apakah candle naik atau turun
    last_candle  = df.iloc[-1]
    vol_direction = "bullish" if last_candle["close"] > last_candle["open"] else "bearish"

    return {
        "current"   : round(current_vol, 2),
        "average"   : round(avg_vol, 2),
        "ratio"     : round(vol_ratio, 2),
        "is_spike"  : vol_ratio >= config.VOLUME_SPIKE_MULTIPLIER,
        "direction" : vol_direction
    }

# ============================================
# EMA TREND
# ============================================

def calculate_ema_trend(df: pd.DataFrame) -> dict:
    """
    EMA 20 dan EMA 50 untuk deteksi trend
    Harga > EMA20 > EMA50 → uptrend kuat
    Harga < EMA20 < EMA50 → downtrend kuat
    """
    ema20 = df["close"].ewm(span=20, adjust=False).mean()
    ema50 = df["close"].ewm(span=50, adjust=False).mean()

    price  = df["close"].iloc[-1]
    ema20v = ema20.iloc[-1]
    ema50v = ema50.iloc[-1]

    if price > ema20v > ema50v:
        trend = "strong_uptrend"
    elif price < ema20v and ema20v > ema50v:
        trend = "weak_uptrend"
    elif price < ema20v < ema50v:
        trend = "strong_downtrend"
    elif price > ema20v and ema20v < ema50v:
        trend = "weak_downtrend"
    else:
        trend = "sideways"

    return {
        "ema20"    : round(ema20v, 6),
        "ema50"    : round(ema50v, 6),
        "trend"    : trend,
        "price_vs_ema20": round((price - ema20v) / ema20v * 100, 2)
    }

# ============================================
# MASTER ANALYZER
# ============================================

def analyze(df: pd.DataFrame, df_trend: pd.DataFrame = None) -> dict:
    """
    Jalankan semua indikator sekaligus
    df       = candle timeframe scalping (5m)
    df_trend = candle timeframe trend (15m) — opsional
    """
    if df.empty or len(df) < 50:
        return {}

    rsi  = calculate_rsi(df)
    macd = calculate_macd(df)
    bb   = calculate_bollinger_bands(df)
    vol  = calculate_volume(df)
    ema  = calculate_ema_trend(df)

    current_rsi = round(rsi.iloc[-1], 2)

    # Trend konfirmasi dari timeframe lebih besar
    trend_confirmation = "neutral"
    if df_trend is not None and not df_trend.empty and len(df_trend) >= 50:
        trend_ema = calculate_ema_trend(df_trend)
        trend_confirmation = trend_ema["trend"]

    # Generate sinyal berdasarkan kombinasi indikator
    signals = []

    # RSI signal
    if current_rsi < config.RSI_OVERSOLD:
        signals.append(("LONG", "RSI oversold"))
    elif current_rsi > config.RSI_OVERBOUGHT:
        signals.append(("SHORT", "RSI overbought"))

    # MACD signal
    if macd["crossover"] == "bullish_cross":
        signals.append(("LONG", "MACD bullish crossover"))
    elif macd["crossover"] == "bearish_cross":
        signals.append(("SHORT", "MACD bearish crossover"))

    # Bollinger Band signal
    if bb["label"] == "near_lower":
        signals.append(("LONG", "Harga dekat lower BB"))
    elif bb["label"] == "near_upper":
        signals.append(("SHORT", "Harga dekat upper BB"))

    # Volume confirmation
    vol_confirmed = vol["is_spike"]

    # Hitung bias
    long_signals  = [s for s in signals if s[0] == "LONG"]
    short_signals = [s for s in signals if s[0] == "SHORT"]

    if len(long_signals) >= 2:
        bias = "LONG"
    elif len(short_signals) >= 2:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    return {
        "rsi"                 : current_rsi,
        "macd"                : macd,
        "bollinger"           : bb,
        "volume"              : vol,
        "ema"                 : ema,
        "signals"             : signals,
        "bias"                : bias,
        "vol_confirmed"       : vol_confirmed,
        "trend_confirmation"  : trend_confirmation
    }

def format_for_ai(symbol: str, price: float, price_change: float, indicators: dict) -> str:
    """Format data indikator menjadi teks ringkas untuk dikirim ke AI"""
    if not indicators:
        return f"Symbol: {symbol} — Data tidak cukup"

    macd = indicators["macd"]
    bb   = indicators["bollinger"]
    vol  = indicators["volume"]
    ema  = indicators["ema"]

    signals_text = ", ".join([f"{s[1]}" for s in indicators["signals"]]) or "Tidak ada sinyal jelas"

    return f"""
SYMBOL: {symbol}
Harga: {price} | Perubahan 24h: {price_change:+.2f}%

INDIKATOR TEKNIKAL:
- RSI ({config.RSI_PERIOD}): {indicators['rsi']} {'⬇️ OVERSOLD' if indicators['rsi'] < config.RSI_OVERSOLD else '⬆️ OVERBOUGHT' if indicators['rsi'] > config.RSI_OVERBOUGHT else '➡️ NEUTRAL'}
- MACD: {macd['crossover']} | Trend: {macd['trend']} | Histogram: {macd['histogram']}
- Bollinger Band: {bb['label']} | Position: {bb['position']} | Squeeze: {bb['squeeze']}
- Volume: {vol['ratio']}x rata-rata | Spike: {vol['is_spike']} | Arah: {vol['direction']}
- EMA Trend: {ema['trend']} | Jarak ke EMA20: {ema['price_vs_ema20']}%
- Trend {config.TREND_TIMEFRAME}: {indicators['trend_confirmation']}

SINYAL AKTIF: {signals_text}
BIAS KESELURUHAN: {indicators['bias']}
VOLUME KONFIRMASI: {indicators['vol_confirmed']}
""".strip()


if __name__ == "__main__":
    from scanner import get_candles, get_top_gainers
    import config

    print("🧪 Test Indicators\n")

    gainers = get_top_gainers()
    if not gainers:
        print("❌ Tidak ada gainer ditemukan")
        exit()

    symbol = gainers[0]["symbol"]
    print(f"📊 Analisis {symbol}...\n")

    df       = get_candles(symbol, config.SCALPING_TIMEFRAME, limit=100)
    df_trend = get_candles(symbol, config.TREND_TIMEFRAME, limit=100)

    if df.empty:
        print("❌ Tidak ada data candle")
        exit()

    indicators = analyze(df, df_trend)

    print(f"RSI              : {indicators['rsi']}")
    print(f"MACD crossover   : {indicators['macd']['crossover']}")
    print(f"MACD trend       : {indicators['macd']['trend']}")
    print(f"BB label         : {indicators['bollinger']['label']}")
    print(f"Volume ratio     : {indicators['volume']['ratio']}x")
    print(f"Volume spike     : {indicators['volume']['is_spike']}")
    print(f"EMA trend        : {indicators['ema']['trend']}")
    print(f"Bias             : {indicators['bias']}")
    print(f"Signals          : {indicators['signals']}")

    print(f"\n📝 Format untuk AI:")
    print("-" * 50)
    text = format_for_ai(
        symbol, gainers[0]["price"],
        gainers[0]["price_change"],
        indicators
    )
    print(text)