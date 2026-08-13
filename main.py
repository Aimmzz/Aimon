import time
import schedule
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box
from rich.markup import escape as _rich_escape

import config
import memory
import scanner
import indicators
import brain
import executor
import risk_manager
import ai_providers

# highlight=False supaya angka/string tidak diwarnai otomatis oleh Rich
console = Console(highlight=False)

# ============================================
# STATE
# ============================================

bot_state = {
    "cycle_count"    : 0,
    "start_time"     : datetime.now(),
    # Pause scanning kalau SEMUA AI provider rate limited
    "ai_paused_until": None,
}

# ============================================
# LOGGING — print langsung, scrollable
# ============================================

LOG_STYLES = {
    "ok"   : "green",
    "err"  : "bold red",
    "warn" : "yellow",
    "ai"   : "cyan",
    "skip" : "dim",
    "info" : "white",
}

def log(message: str, level: str = "info"):
    """
    Print log berwarna dengan timestamp — output biasa, bisa di-scroll.

    message di-escape sebelum dikirim ke Rich, supaya kurung siku literal
    di dalamnya (nama provider, pesan exception seperti "[Errno 111]", dll)
    tidak salah ditafsirkan sebagai markup tag oleh Rich — kalau itu terjadi,
    teksnya akan hilang/rusak di layar tanpa error yang kelihatan.
    """
    timestamp = datetime.now().strftime("%H:%M:%S")
    style     = LOG_STYLES.get(level, "white")
    console.print(f"[dim]{timestamp}[/] [{style}]{_rich_escape(message)}[/]")

# ============================================
# STATUS SUMMARY — diprint di akhir cycle
# ============================================

def print_cycle_summary(open_trade_details: list):
    """Ringkasan compact pengganti dashboard — tabel kecil per cycle"""
    stats       = memory.get_performance_stats()
    can, reason = risk_manager.can_trade()
    providers   = ai_providers.get_provider_status()

    uptime     = datetime.now() - bot_state["start_time"]
    uptime_str = f"{int(uptime.total_seconds() // 3600)}h{int((uptime.total_seconds() % 3600) // 60):02d}m"

    # ── Baris status satu-liner ──
    daily = stats["daily_pnl"]
    total = stats["total_pnl"]
    daily_c = "green" if daily >= 0 else "red"
    total_c = "green" if total >= 0 else "red"

    prov_parts = []
    for p in providers:
        if not p["has_key"]:
            prov_parts.append(f"[dim]{p['name']}:—[/]")
        elif p["available"]:
            prov_parts.append(f"[green]{p['name']}:●[/]")
        else:
            prov_parts.append(f"[yellow]{p['name']}:◐{p['cooldown']}[/]")
    prov_text = " ".join(prov_parts)

    trade_gate = "[green]✅[/]" if can else f"[red]❌ {reason}[/]"

    console.print(
        f"   [bold]Σ[/] PnL hari ini [{daily_c}]${daily:+.2f}[/] "
        f"| total [{total_c}]${total:+.2f}[/] "
        f"| WR {stats['win_rate']:.0f}% ({stats['total_trades']}) "
        f"| trade {trade_gate} "
        f"| AI {prov_text} "
        f"| up {uptime_str}"
    )

    # ── Tabel posisi terbuka (hanya kalau ada) ──
    if open_trade_details:
        t = Table(box=box.SIMPLE_HEAD, show_header=True, padding=(0, 1))
        t.add_column("Symbol", no_wrap=True)
        t.add_column("Side", width=5)
        t.add_column("Entry → Now", justify="right")
        t.add_column("TP / SL", justify="right")
        t.add_column("PnL", justify="right")
        t.add_column("Dur", justify="right")
        t.add_column("Rev", justify="right")  # jumlah review tesis — bukti fitur ini jalan

        for d in open_trade_details:
            side_style = "green" if d["side"] == "LONG" else "red"
            pnl_color  = "green" if d["unrealized_usdt"] >= 0 else "red"
            t.add_row(
                d["symbol"],
                f"[{side_style}]{d['side']}[/]",
                f"{d['entry_price']:.6g} → [bold]{d['current_price']:.6g}[/]",
                f"[cyan]{d['tp_price']:.6g}[/] / [magenta]{d['sl_price']:.6g}[/]",
                f"[{pnl_color}]${d['unrealized_usdt']:+.2f} ({d['unrealized_pct']:+.1f}%)[/]",
                f"{d['running_min']}m",
                str(d.get("review_count", 0))
            )
        console.print(t)

# ============================================
# PRE-SCAN (Tahap 1 — paralel)
# ============================================

def prescan_candidate(candidate: dict) -> dict | None:
    """
    Tahap 1 (dijalankan PARALEL untuk semua kandidat):
    fetch candle 5m + hitung indikator + gerbang bias.
    Return kandidat (dengan df & indikator terlampir) kalau lolos, else None.

    Candle 15m TIDAK di-fetch di sini — hanya kandidat yang lolos gerbang
    yang layak dapat request tambahan (hemat weight API Binance).
    """
    try:
        symbol = candidate["symbol"]

        df = scanner.get_candles(symbol, config.SCALPING_TIMEFRAME, limit=config.CANDLE_LIMIT)
        if df.empty or len(df) < 50:
            return None

        inds = indicators.analyze(df)
        if not inds:
            return None

        # Gerbang bias — minimal 2 sinyal align sebelum layak dianalisis AI
        if config.REQUIRE_INDICATOR_BIAS and inds.get("bias") == "NEUTRAL":
            return None

        candidate["df_5m"]       = df
        candidate["prelim_bias"] = inds.get("bias")
        return candidate

    except Exception:
        return None

# ============================================
# MAIN BOT CYCLE
# ============================================

def monitor_all_positions() -> list[dict]:
    """
    Cek & tutup (kalau perlu) SEMUA posisi terbuka.

    Dipanggil dari DUA jadwal berbeda:
    1. Jadwal cepat (config.POSITION_CHECK_SECONDS) — inilah yang
       SEHARUSNYA sudah jalan sejak awal tapi ternyata tidak (bug lama:
       nilai config ini tidak pernah disambungkan ke schedule manapun).
    2. bot_cycle() (config.SCAN_INTERVAL_SECONDS) — dipanggil lagi di sini
       murni untuk mengisi tabel ringkasan cycle, bukan untuk deteksi
       pertama; deteksi cepatnya sudah ditangani jadwal #1.

    KENAPA JADWAL CEPAT INI PENTING (khusus PAPER_TRADE_MODE):
    Di mode LIVE, TP/SL adalah order STOP_MARKET/TAKE_PROFIT_MARKET asli
    di Binance — exchange yang mengeksekusi nyaris instan begitu harga
    tersentuh, bot tidak perlu polling cepat untuk itu. Tapi di PAPER
    mode, TIDAK ADA order asli sama sekali; satu-satunya cara SL/TP
    "tereksekusi" adalah bot sendiri mendeteksinya lewat polling. Kalau
    polling hanya terjadi tiap SCAN_INTERVAL_SECONDS (120 detik), posisi
    bisa overshoot jauh dari harga SL sebelum sempat terdeteksi — ini
    penyebab konkret MVLLUSDT closed di -17.6% padahal STOP_LOSS_PCT
    cuma 2% (leverage 3x mestinya menghasilkan sekitar -6%, bukan -17.6%).

    Return list detail posisi (untuk tabel ringkasan), posisi yang closed
    tidak ikut dalam list ini.
    """
    open_trades = memory.get_open_trades()
    details = []
    for trade in open_trades:
        detail = monitor_open_trade(trade)
        if detail:
            details.append(detail)
    return details


def review_all_theses():
    """
    Jadwal TERPISAH (config.THESIS_REVIEW_INTERVAL_MINUTES) — tinjau ulang
    apakah alasan awal entry masih valid untuk posisi yang sudah cukup lama
    terbuka (config.THESIS_REVIEW_MIN_HOLD_MINUTES). Ini BUKAN pengecekan
    SL/TP biasa (itu tugas monitor_all_positions) — ini soal mendeteksi
    pembalikan tesis SEBELUM harga sempat menyentuh SL asli.

    AI hanya memutuskan HOLD/TIGHTEN_SL/EXIT_EARLY. Angka SL baru selalu
    dihitung & divalidasi oleh risk_manager.tighten_sl_price() — AI tidak
    pernah menghitung harga sendiri, dan SL tidak pernah bisa menjauh
    lewat jalur ini (lihat validasi di tighten_sl_price).
    """
    if not config.THESIS_REVIEW_ENABLED:
        return

    due_trades = memory.get_trades_due_for_review(
        min_hold_minutes        = config.THESIS_REVIEW_MIN_HOLD_MINUTES,
        review_interval_minutes = config.THESIS_REVIEW_INTERVAL_MINUTES
    )

    if not due_trades:
        return  # tidak ada yang due menit ini — diam total, jangan log tiap menit

    batch_results = []  # untuk ringkasan satu baris di akhir — visibilitas fitur ini benar jalan

    for trade in due_trades:
        symbol = trade["symbol"]
        try:
            df       = scanner.get_candles(symbol, config.SCALPING_TIMEFRAME, limit=config.CANDLE_LIMIT)
            df_trend = scanner.get_candles(symbol, config.TREND_TIMEFRAME, limit=config.CANDLE_LIMIT)
            if df.empty or len(df) < 50:
                memory.mark_trade_reviewed(trade["id"])  # tetap catat, jangan retry tiap poll
                batch_results.append(f"{symbol}=SKIP(data)")
                continue

            current_inds = indicators.analyze(df, df_trend)
            if not current_inds:
                memory.mark_trade_reviewed(trade["id"])
                batch_results.append(f"{symbol}=SKIP(data)")
                continue

            review = brain.make_thesis_review(trade, current_inds)
            memory.mark_trade_reviewed(trade["id"])

            if review.get("rate_limited"):
                batch_results.append(f"{symbol}=SKIP(AI limit)")
                continue  # semua AI provider habis — HOLD, coba lagi review berikutnya

            action = review["action"]
            batch_results.append(f"{symbol}={action}")

            if action == "HOLD":
                continue  # detail HOLD tidak perlu log baris sendiri — cukup di ringkasan batch

            log(
                f"🔎 Review tesis {symbol}: {action}"
                f"{' (' + review['severity'] + ')' if review.get('severity') else ''}"
                f" — conf {review['confidence']:.0%} via {review.get('ai_provider', '?')}"
                f" — {review['reasoning']}",
                "ai"
            )

            if action == "EXIT_EARLY":
                qty = memory.get_trade_quantity(trade)
                executor.close_position(
                    trade_id     = trade["id"],
                    symbol       = symbol,
                    side         = trade["side"],
                    quantity     = qty,
                    entry_price  = trade["entry_price"],
                    entry_time   = trade["timestamp"],
                    close_reason = "THESIS_INVALID",
                    margin       = trade["margin"],
                    leverage     = trade["leverage"]
                )
                log(f"🚪 {symbol} ditutup dini — tesis entry sudah batal", "warn")

            elif action == "TIGHTEN_SL":
                current_price = executor.get_current_price(symbol)
                if current_price == 0:
                    continue

                new_sl = risk_manager.tighten_sl_price(
                    side          = trade["side"],
                    entry_price   = trade["entry_price"],
                    current_sl    = trade.get("sl_price", 0),
                    current_price = current_price,
                    severity      = review["severity"]
                )

                if new_sl is not None:
                    memory.update_trade_sl(trade["id"], new_sl)
                    log(f"🔒 {symbol} SL diperketat ke {new_sl} ({review['severity']})", "ok")
                else:
                    log(f"⚠️  {symbol} TIGHTEN_SL diminta tapi hasil hitung tidak valid — diabaikan", "warn")

        except Exception as e:
            log(f"❌ Review tesis error {symbol}: {str(e)[:60]}", "err")
            memory.mark_trade_reviewed(trade["id"])  # jangan retry terus kalau errornya persisten
            batch_results.append(f"{symbol}=ERROR")

    # Ringkasan satu baris — bukti bahwa fitur ini benar-benar jalan tiap
    # interval, tanpa perlu spam log detail untuk setiap HOLD.
    log(f"🔎 Review tesis batch: {', '.join(batch_results)}", "ai")


def bot_cycle():
    """Satu siklus penuh bot"""
    bot_state["cycle_count"] += 1
    cycle_num = bot_state["cycle_count"]

    next_at = (datetime.now() + timedelta(seconds=config.SCAN_INTERVAL_SECONDS)).strftime("%H:%M:%S")
    console.rule(f"[bold cyan]Cycle #{cycle_num}[/] [dim]— next scan ±{next_at}[/]", style="cyan")

    open_trade_details = []  # fallback kalau exception terjadi sebelum STEP 1 selesai

    try:
        # ── STEP 1: Monitor posisi terbuka — HARUS selalu jalan,
        # terlepas dari status can_trade, supaya tidak deadlock.
        # (Deteksi CEPATnya sudah ditangani jadwal terpisah di
        # monitor_all_positions() — ini cuma untuk isi tabel ringkasan.)
        open_trade_details = monitor_all_positions()

        # ── STEP 2: Risk check (general) ──
        can, reason = risk_manager.can_trade()
        if not can:
            log(f"⏸️  Skip scan — {reason}", "warn")
            print_cycle_summary(open_trade_details)
            return

        # ── STEP 2b: Semua AI provider sedang pause? ──
        paused_until = bot_state["ai_paused_until"]
        if paused_until and datetime.now() < paused_until:
            remaining = int((paused_until - datetime.now()).total_seconds())
            log(f"⏸️  AI pause — {remaining}s lagi", "warn")
            print_cycle_summary(open_trade_details)
            return
        elif paused_until:
            bot_state["ai_paused_until"] = None
            log("▶️  Pause selesai — melanjutkan scanning", "ok")

        # ── STEP 3: Scan pair (semua yang lolos filter scanner) ──
        gainers = scanner.get_top_gainers()

        if not gainers:
            log("⚠️  Tidak ada kandidat ditemukan", "warn")
            print_cycle_summary(open_trade_details)
            return

        # ── STEP 4a: Pre-scan PARALEL — fetch candle 5m + gerbang bias
        # untuk SEMUA kandidat sekaligus. Ini yang membuat scan semua
        # pair tetap cepat (paralel, bukan satu-satu).
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=config.SCAN_WORKERS) as pool:
            results = list(pool.map(prescan_candidate, gainers))
        shortlist = [c for c in results if c is not None]
        elapsed = time.time() - t0

        log(
            f"🚪 Gerbang bias: {len(shortlist)}/{len(gainers)} kandidat lolos "
            f"({elapsed:.1f}s, {config.SCAN_WORKERS} worker)"
        )

        if not shortlist:
            print_cycle_summary(open_trade_details)
            return

        # ── STEP 4b: Analisis AI — SEKUENSIAL, hanya untuk shortlist ──
        for candidate in shortlist:
            symbol       = candidate["symbol"]
            price        = candidate["price"]
            price_change = candidate["price_change"]

            can, reason = risk_manager.can_trade(symbol)
            if not can:
                # Alasan yang SPESIFIK ke symbol ini saja — skip kandidat
                # ini, tapi tetap lanjut cek kandidat lain di cycle yang
                # sama. Alasan lain (max daily loss, max open trades,
                # cooldown global) menghentikan SELURUH scan cycle.
                if "sudah ada posisi terbuka" in reason or "dihindari sisa hari" in reason:
                    log(f"⏭️  {symbol} — {reason}", "skip")
                    continue
                log(f"⏸️  Stop scan — {reason}", "warn")
                break

            # Candle 15m baru di-fetch di sini (hanya untuk yang lolos gerbang)
            df       = candidate["df_5m"]
            df_trend = scanner.get_candles(symbol, config.TREND_TIMEFRAME, limit=config.CANDLE_LIMIT)

            # Analisis ulang LENGKAP dengan konfirmasi trend 15m
            inds = indicators.analyze(df, df_trend)
            if not inds:
                continue

            # Bias bisa berubah setelah trend dimasukkan? Tidak — bias dihitung
            # dari sinyal 5m. Tapi guard tetap dipasang untuk keamanan.
            if config.REQUIRE_INDICATOR_BIAS and inds.get("bias") == "NEUTRAL":
                continue

            formatted = indicators.format_for_ai(symbol, price, price_change, inds)

            # ── AI decision (Gemini → Groq failover otomatis) ──
            ai_result  = brain.make_decision(symbol, price, price_change, inds, formatted)
            decision   = ai_result["decision"]
            confidence = ai_result["confidence"]
            provider   = ai_result.get("ai_provider") or "?"

            # rate_limited hanya True kalau SEMUA provider cooldown —
            # failover antar provider sudah terjadi di ai_providers.chat()
            if ai_result.get("rate_limited"):
                pause_minutes = 6
                bot_state["ai_paused_until"] = datetime.now() + timedelta(minutes=pause_minutes)
                log(f"⏸️  Semua AI provider rate limit — pause {pause_minutes}m", "err")
                break

            memory.save_scan_log(
                symbol       = symbol,
                price_change = price_change,
                volume_24h   = candidate["volume_24h"],
                rsi          = inds.get("rsi", 0),
                ai_decision  = decision,
                ai_confidence= confidence,
                executed     = False
            )

            conf_ok, conf_reason = risk_manager.check_confidence(confidence)
            if decision == "SKIP" or not conf_ok:
                log(f"⏭️  {symbol} — {decision} ({confidence:.0%}) via {provider}", "skip")
                continue

            # ── STEP 5: Eksekusi trade ──
            log(f"⚡ Entry {decision} {symbol} @ ${price:.4f} — conf {confidence:.0%} via {provider}", "ai")

            sym_info     = scanner.get_symbol_info(symbol)
            balance_info = scanner.get_account_balance()
            sizing       = risk_manager.calculate_position_size(
                sym_info, price,
                balance_info = balance_info,
                ai_result    = ai_result
            )
            qty    = sizing["qty"]
            margin = sizing["margin"]

            if sizing["fallback_used"]:
                log(f"⚠️  {symbol} — balance tidak tersedia, margin fallback ${margin:.2f}", "warn")
            else:
                log(
                    f"📐 Sizing: ${margin:.2f} (alloc {sizing['allocation_pct']:.0f}%, "
                    f"streak {sizing['streak_type']}x{sizing['streak_count']})"
                )

            trade_result = executor.open_position(
                symbol     = symbol,
                side       = decision,
                quantity   = qty,
                margin     = margin,
                ai_result  = ai_result,
                indicators = inds
            )

            if trade_result["status"] == "ok":
                tp = trade_result["tp_price"]
                sl = trade_result["sl_price"]
                log(f"✅ {decision} {symbol} dibuka #{trade_result['trade_id']} | TP {tp} SL {sl}", "ok")
                memory.save_scan_log(
                    symbol=symbol, price_change=price_change,
                    volume_24h=candidate["volume_24h"], rsi=inds.get("rsi", 0),
                    ai_decision=decision, ai_confidence=confidence, executed=True
                )
                break  # hanya 1 trade per cycle
            else:
                log(f"❌ Gagal buka posisi {symbol}", "err")

    except Exception as e:
        log(f"❌ Cycle error: {str(e)[:80]}", "err")

    print_cycle_summary(open_trade_details)

def monitor_open_trade(trade: dict) -> dict | None:
    """
    Monitor posisi yang sedang terbuka.
    Return dict detail posisi (untuk summary) atau None kalau error/ditutup.
    """
    try:
        trade_id    = trade["id"]
        symbol      = trade["symbol"]
        side        = trade["side"]
        entry_price = trade["entry_price"]
        entry_time  = trade["timestamp"]
        margin      = trade["margin"]
        leverage    = trade["leverage"]
        # Quantity TERSIMPAN dari saat open (bukan dihitung ulang) —
        # hitung ulang bisa beda dari yang asli akibat pembulatan
        # step_size, fatal di LIVE mode (order close ditolak/pecahan)
        qty         = memory.get_trade_quantity(trade)

        # ── Rekonsiliasi exchange ↔ DB (LIVE mode saja, no-op di PAPER) ──
        # Di live, TP/SL adalah order asli yang dieksekusi BINANCE — bisa
        # ter-fill saat bot polling/mati. Cek dulu posisi ini masih nyata
        # ada sebelum mengambil keputusan apapun atasnya; kalau ternyata
        # sudah closed di exchange, DB disinkronkan dan selesai di sini.
        if executor.reconcile_trade_with_exchange(trade):
            return None

        stored_tp = trade.get("tp_price") or 0
        stored_sl = trade.get("sl_price") or 0

        if stored_tp and stored_sl:
            tp, sl = stored_tp, stored_sl
        else:
            tp, sl = risk_manager.calculate_tp_sl_prices(side, entry_price)

        current_price = executor.get_current_price(symbol)
        if current_price == 0:
            return None

        # Durasi posisi — dipindah ke atas (sebelumnya dihitung setelah
        # check_position_status) supaya bisa dikirim sebagai parameter
        # untuk stagnant breaker.
        try:
            entry_dt = datetime.fromisoformat(entry_time)
            # total_seconds(), BUKAN .seconds — .seconds wrap setiap 24 jam
            running_min = int((datetime.now() - entry_dt).total_seconds() / 60)
        except Exception:
            running_min = 0

        # ── Tracking high/low untuk trailing — PERSISTEN ke DB ──
        # Sebelumnya dihitung ulang dari nol tiap cek (`max(current_price,
        # entry_price)`), jadi begitu harga turun lagi dari puncaknya, bot
        # "lupa" pernah ada di titik tertinggi — trailing SL efektif tidak
        # pernah bisa trigger. Sekarang baca nilai yang sudah tersimpan,
        # gabungkan dengan harga sekarang, lalu simpan lagi.
        stored_high = trade.get("highest_price") or entry_price
        stored_low  = trade.get("lowest_price") or entry_price
        high = max(stored_high, current_price)
        low  = min(stored_low, current_price)
        if high != stored_high or low != stored_low:
            memory.update_trade_extremes(trade_id, high, low)

        status = risk_manager.check_position_status(
            trade_id          = trade_id,
            side              = side,
            entry_price       = entry_price,
            current_price     = current_price,
            tp_price          = tp,
            sl_price          = sl,
            highest_price     = high,
            lowest_price      = low,
            duration_minutes  = running_min,
            leverage          = leverage
        )

        # PnL unrealized
        if side == "LONG":
            unrealized_pct  = (current_price - entry_price) / entry_price * 100 * leverage
            unrealized_usdt = (current_price - entry_price) / entry_price * margin * leverage
        else:
            unrealized_pct  = (entry_price - current_price) / entry_price * 100 * leverage
            unrealized_usdt = (entry_price - current_price) / entry_price * margin * leverage

        action = status["action"]

        if action == "CHECK_EXTEND":
            df   = scanner.get_candles(symbol, config.SCALPING_TIMEFRAME, limit=50)
            inds = indicators.analyze(df)
            if inds and risk_manager.should_extend_tp(inds["rsi"], side):
                new_tp = risk_manager.extend_tp_price(side, entry_price)
                memory.update_trade_tp(trade_id, new_tp)
                log(f"📈 {symbol} TP extend ke {new_tp} (RSI {inds['rsi']:.1f})", "ok")
                tp = new_tp  # supaya summary menampilkan TP baru
            else:
                executor.close_position(trade_id, symbol, side, qty, entry_price, entry_time, "TP", margin=margin, leverage=leverage)
                log(f"✅ {symbol} TP tercapai — posisi ditutup", "ok")
                return None

        elif action == "CLOSE":
            reason = status["reason"]
            executor.close_position(trade_id, symbol, side, qty, entry_price, entry_time, reason, margin=margin, leverage=leverage)
            level = "ok" if reason == "TP" else "err" if reason == "SL" else "warn"
            log(f"🔒 {symbol} ditutup — {reason}", level)
            return None

        return {
            "symbol"          : symbol,
            "side"            : side,
            "entry_price"     : entry_price,
            "current_price"   : current_price,
            "tp_price"        : tp,
            "sl_price"        : sl,
            "unrealized_usdt" : unrealized_usdt,
            "unrealized_pct"  : unrealized_pct,
            "running_min"     : running_min,
            "review_count"    : trade.get("review_count", 0)
        }

    except Exception as e:
        log(f"❌ Monitor error {trade.get('symbol', '?')}: {str(e)[:50]}", "err")
        return None

# ============================================
# POST MORTEM SCHEDULER
# ============================================

def run_post_mortem():
    log("🔬 Menjalankan post-mortem analysis...", "ai")
    success = brain.perform_post_mortem()
    if success:
        log("✅ Strategy policy diupdate", "ok")
    else:
        log("⏭️  Post-mortem di-skip (belum cukup data baru / gagal AI) — policy TIDAK berubah", "warn")

# ============================================
# MAIN
# ============================================

def main():
    console.print(Panel(
        "[bold cyan]🤖 BINANCE FUTURES AI BOT[/]\n"
        f"[dim]Mode:[/] {'🧪 TESTNET' if config.USE_TESTNET else '🔴 LIVE'}"
        f"  [dim]|[/]  {'📝 PAPER' if executor.PAPER_TRADE_MODE else '💸 REAL ORDER'}"
        f"  [dim]|[/]  scan tiap {config.SCAN_INTERVAL_SECONDS}s",
        border_style="cyan"
    ))

    memory.init_db()

    if not config.validate_config():
        console.print("[red]❌ Config tidak valid, bot berhenti[/]")
        return

    # Tampilkan status AI provider di startup
    for p in ai_providers.get_provider_status():
        if p["has_key"]:
            log(f"🤖 Provider ({p['name']}) siap — {p['model']}", "ok")
        else:
            log(f"⚠️  Provider ({p['name']}) tidak aktif — API key kosong", "warn")

    # ── Config epoch — deteksi otomatis kalau parameter trading berubah ──
    # Post-mortem (brain.py) pakai epoch_start ini supaya sampling
    # winner/loser tidak mencampur data dari rezim SL/TP/trailing yang
    # berbeda-beda. Tidak perlu langkah manual — cukup jalan tiap startup.
    epoch_start, is_new_epoch = memory.ensure_config_epoch(config.get_trading_params_fingerprint())
    if is_new_epoch:
        log(f"⚙️  Parameter trading berubah — epoch baru dimulai, post-mortem akan sampling dari sini", "warn")
    else:
        log(f"⚙️  Parameter trading sama seperti sebelumnya (epoch sejak {epoch_start[:16]})", "info")

    # ── Catch-up post-mortem di startup ──
    # Jadwal 00:00 di bawah cuma jalan kalau proses bot masih HIDUP persis
    # di jam itu — kalau bot sering di-restart (pola umum saat testing),
    # jadwal itu bisa tidak pernah ke-trigger sama sekali, dan
    # strategy_policy.txt tertahan default walau data trade sudah banyak.
    # Guard ini: kalau belum pernah update SAMA SEKALI, atau sudah lebih
    # dari 20 jam sejak update terakhir, jalankan post-mortem SEKARANG
    # sebelum mulai scan — supaya tidak bergantung pada uptime yang
    # kebetulan menyentuh tengah malam.
    last_update = memory.get_last_strategy_update_time()
    should_catchup = False
    if last_update is None:
        should_catchup = True
        log("🔬 Belum pernah ada post-mortem — jalankan catch-up sekarang", "ai")
    else:
        try:
            hours_since = (datetime.now() - datetime.fromisoformat(last_update)).total_seconds() / 3600
            if hours_since >= 20:
                should_catchup = True
                log(f"🔬 Post-mortem terakhir {hours_since:.0f} jam lalu — jalankan catch-up sekarang", "ai")
        except (ValueError, TypeError):
            pass

    if should_catchup:
        run_post_mortem()

    # ── Jadwal cepat: monitoring posisi terbuka ──
    # Sebelumnya config.POSITION_CHECK_SECONDS ada di config.py tapi TIDAK
    # PERNAH disambungkan ke schedule manapun — posisi cuma dicek sebagai
    # bagian dari bot_cycle() tiap 120 detik. Untuk pair volatile di PAPER
    # mode (tidak ada order SL/TP asli di exchange), ini bisa bikin
    # overshoot jauh dari harga SL yang seharusnya. Sekarang benar-benar
    # jalan tiap POSITION_CHECK_SECONDS, terpisah dari scan kandidat baru.
    schedule.every(config.POSITION_CHECK_SECONDS).seconds.do(monitor_all_positions)
    schedule.every(1).minutes.do(review_all_theses)
    schedule.every(config.SCAN_INTERVAL_SECONDS).seconds.do(bot_cycle)
    schedule.every().day.at("00:00").do(run_post_mortem)

    log(f"✅ Bot berjalan — CTRL+C untuk berhenti", "ok")
    log(f"👁️  Monitoring posisi tiap {config.POSITION_CHECK_SECONDS}s, scan kandidat tiap {config.SCAN_INTERVAL_SECONDS}s", "info")
    if config.THESIS_REVIEW_ENABLED:
        log(f"🔎 Review tesis tiap {config.THESIS_REVIEW_INTERVAL_MINUTES} menit (hold minimum {config.THESIS_REVIEW_MIN_HOLD_MINUTES} menit)", "info")

    try:
        # Cycle pertama langsung — di dalam try yang sama supaya Ctrl+C
        # kapan pun (termasuk di tengah cycle pertama) keluar rapi,
        # bukan traceback mentah kayak KeyboardInterrupt pas nunggu
        # respons HTTP dari provider AI.
        bot_cycle()

        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]⏹️  Bot dihentikan manual[/]")
        memory.print_stats()
        console.print("[green]👋 Goodbye![/]")


if __name__ == "__main__":
    main()