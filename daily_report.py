import sys
import argparse
from datetime import datetime, date, timedelta
from memory import get_db, init_db

EXPERIMENT_MARK_FILE = "experiment_start.txt"

# ============================================
# LAPORAN HARIAN
# ============================================

def generate_report() -> str:
    """Generate laporan harian yang siap di-copy paste"""

    today = date.today().isoformat()

    with get_db() as conn:
        cursor = conn.cursor()

        # ── Stats hari ini ── (filter close_timestamp: trade yang CLOSE
        # hari ini, bukan yang DIBUKA hari ini — posisi yang dibuka
        # 23:50 dan close 00:30 harus terhitung ke hari closenya)
        cursor.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl_usdt < 0 THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl_usdt), 0) as total_pnl,
                COALESCE(MAX(pnl_usdt), 0) as best,
                COALESCE(MIN(pnl_usdt), 0) as worst,
                COALESCE(AVG(duration_mins), 0) as avg_dur
            FROM trade_history
            WHERE DATE(close_timestamp) = ? AND status = 'CLOSED'
        """, (today,))
        today_stats = dict(cursor.fetchone())

        # ── Stats keseluruhan ──
        cursor.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) as wins,
                COALESCE(SUM(pnl_usdt), 0) as total_pnl,
                COALESCE(AVG(duration_mins), 0) as avg_dur
            FROM trade_history WHERE status = 'CLOSED'
        """)
        overall = dict(cursor.fetchone())

        # ── Close reason breakdown ──
        cursor.execute("""
            SELECT close_reason, COUNT(*) as count
            FROM trade_history
            WHERE DATE(close_timestamp) = ? AND status = 'CLOSED'
            GROUP BY close_reason
        """, (today,))
        reasons = dict(cursor.fetchall())

        # ── Best & worst trade hari ini ──
        cursor.execute("""
            SELECT symbol, side, pnl_usdt, pnl_percent, close_reason, duration_mins
            FROM trade_history
            WHERE DATE(close_timestamp) = ? AND status = 'CLOSED'
            ORDER BY pnl_usdt DESC LIMIT 1
        """, (today,))
        best_row = cursor.fetchone()

        cursor.execute("""
            SELECT symbol, side, pnl_usdt, pnl_percent, close_reason, duration_mins
            FROM trade_history
            WHERE DATE(close_timestamp) = ? AND status = 'CLOSED'
            ORDER BY pnl_usdt ASC LIMIT 1
        """, (today,))
        worst_row = cursor.fetchone()

        # ── SEMUA pair yang ditradingkan hari ini ──
        # Tanpa LIMIT — sebelumnya ada LIMIT 5 + ORDER BY count yang
        # menyembunyikan pair lain (sebagian besar pair cuma ditradingkan
        # 1x/hari, jadi urutan by count jadi sewenang-wenang). Sekarang
        # tampilkan semua, diurutkan dari yang paling untung ke paling rugi.
        cursor.execute("""
            SELECT symbol, COUNT(*) as count, SUM(pnl_usdt) as total_pnl
            FROM trade_history
            WHERE DATE(close_timestamp) = ? AND status = 'CLOSED'
            GROUP BY symbol
            ORDER BY total_pnl DESC
        """, (today,))
        top_pairs = cursor.fetchall()

        # ── Scan stats (berdasarkan kapan SCAN terjadi, bukan trade
        # open/close — jadi TETAP pakai timestamp biasa, ini benar) ──
        cursor.execute("""
            SELECT
                COUNT(*) as total_scanned,
                SUM(executed) as total_executed,
                SUM(CASE WHEN ai_decision = 'LONG' THEN 1 ELSE 0 END) as long_signals,
                SUM(CASE WHEN ai_decision = 'SHORT' THEN 1 ELSE 0 END) as short_signals,
                SUM(CASE WHEN ai_decision = 'SKIP' THEN 1 ELSE 0 END) as skips,
                COALESCE(AVG(ai_confidence), 0) as avg_confidence
            FROM scan_log
            WHERE DATE(timestamp) = ?
        """, (today,))
        scan_stats = dict(cursor.fetchone())

        # ── Open trades saat ini ──
        cursor.execute("""
            SELECT symbol, side, entry_price, timestamp
            FROM trade_history WHERE status = 'OPEN'
        """)
        open_trades = cursor.fetchall()

        # ── Strategy policy ──
        try:
            with open("strategy_policy.txt", "r", encoding="utf-8") as f:
                policy = f.read().strip()
        except Exception:
            policy = "Belum ada policy"

    # ── Hitung metrics ──
    today_total = today_stats["total"] or 0
    today_wins  = today_stats["wins"] or 0
    today_pnl   = today_stats["total_pnl"] or 0
    today_wr    = (today_wins / today_total * 100) if today_total > 0 else 0

    overall_total = overall["total"] or 0
    overall_wins  = overall["wins"] or 0
    overall_pnl   = overall["total_pnl"] or 0
    overall_wr    = (overall_wins / overall_total * 100) if overall_total > 0 else 0

    total_scanned  = scan_stats["total_scanned"] or 0
    total_executed = scan_stats["total_executed"] or 0
    execution_rate = (total_executed / total_scanned * 100) if total_scanned > 0 else 0

    report = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 DAILY BOT REPORT — {today}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📅 HARI INI
- Total trades   : {today_total}
- Win / Loss     : {today_wins}W / {(today_total - today_wins)}L
- Win rate       : {today_wr:.1f}%
- Total PnL      : ${today_pnl:.2f}
- Best trade     : ${today_stats['best']:.2f}
- Worst trade    : ${today_stats['worst']:.2f}
- Avg durasi     : {today_stats['avg_dur']:.0f} menit

📈 CLOSE REASONS
{chr(10).join([f'• {r}: {c}x' for r, c in reasons.items()]) if reasons else '• Belum ada trade'}

🏆 BEST TRADE HARI INI
{f"• {best_row[0]} {best_row[1]} | ${best_row[2]:.2f} ({best_row[3]:.1f}%) | {best_row[4]} | {best_row[5]} menit" if best_row else "• Belum ada"}

💀 WORST TRADE HARI INI  
{f"• {worst_row[0]} {worst_row[1]} | ${worst_row[2]:.2f} ({worst_row[3]:.1f}%) | {worst_row[4]} | {worst_row[5]} menit" if worst_row else "• Belum ada"}

🎯 SEMUA PAIR HARI INI (urut dari paling untung)
{chr(10).join([f"{'✅' if p[2] >= 0 else '❌'} {p[0]}: {p[1]}x trade | PnL ${p[2]:+.2f}" for p in top_pairs]) if top_pairs else '• Belum ada'}

🔍 SCAN STATS
- Total pair discan : {total_scanned}
- Dieksekusi        : {int(total_executed or 0)}
- Execution rate    : {execution_rate:.1f}%
- LONG signals      : {int(scan_stats['long_signals'] or 0)}
- SHORT signals     : {int(scan_stats['short_signals'] or 0)}
- SKIP              : {int(scan_stats['skips'] or 0)}
- Avg confidence    : {float(scan_stats['avg_confidence'] or 0):.0%}

📊 OVERALL (Sejak awal)
- Total trades   : {overall_total}
- Win rate       : {overall_wr:.1f}%
- Total PnL      : ${overall_pnl:.2f}
- Avg durasi     : {overall['avg_dur']:.0f} menit

🔓 OPEN TRADES SAAT INI
{chr(10).join([f'• {t[0]} {t[1]} @ {t[2]} (since {t[3][11:16]})' for t in open_trades]) if open_trades else '• Tidak ada'}

🧠 STRATEGY POLICY AKTIF
{policy[:300]}{'...' if len(policy) > 300 else ''}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""".strip()

    return report

# ============================================
# MODE EVALUASI EKSPERIMEN (parameter dibekukan)
# ============================================

def mark_experiment_start():
    """Catat waktu SEKARANG sebagai awal periode eksperimen (parameter dibekukan)"""
    now_iso = datetime.now().isoformat()
    with open(EXPERIMENT_MARK_FILE, "w", encoding="utf-8") as f:
        f.write(now_iso)
    print(f"✅ Eksperimen ditandai mulai: {now_iso}")
    print(f"   (tersimpan di {EXPERIMENT_MARK_FILE} — jalankan --since-experiment nanti untuk evaluasi)")

def _read_experiment_start() -> str:
    try:
        with open(EXPERIMENT_MARK_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        print(f"❌ Belum ada eksperimen ditandai. Jalankan dulu: python daily_report.py --start-experiment")
        sys.exit(1)

def generate_range_report(start_iso: str, label: str = None) -> str:
    """
    Laporan untuk rentang waktu SEJAK start_iso sampai sekarang.
    Fokus utama: breakdown close_reason dengan avg PnL per alasan.

    Filter pakai close_timestamp (waktu CLOSE), bukan timestamp (waktu
    OPEN) — trade yang closenya masuk window ini harus terhitung, biarpun
    dibuka sedikit sebelum start_iso.
    """
    label = label or f"Sejak {start_iso[:16]}"

    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) as wins,
                COALESCE(SUM(pnl_usdt), 0) as total_pnl,
                COALESCE(AVG(pnl_usdt), 0) as avg_pnl,
                COALESCE(AVG(duration_mins), 0) as avg_dur,
                COALESCE(MAX(pnl_usdt), 0) as best,
                COALESCE(MIN(pnl_usdt), 0) as worst
            FROM trade_history
            WHERE close_timestamp >= ? AND status = 'CLOSED'
        """, (start_iso,))
        stats = dict(cursor.fetchone())

        # ── Breakdown close_reason: count + avg pnl + win rate PER alasan ──
        cursor.execute("""
            SELECT
                close_reason,
                COUNT(*) as count,
                COALESCE(SUM(pnl_usdt), 0) as total_pnl,
                COALESCE(AVG(pnl_usdt), 0) as avg_pnl,
                SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) as wins
            FROM trade_history
            WHERE close_timestamp >= ? AND status = 'CLOSED'
            GROUP BY close_reason
            ORDER BY count DESC
        """, (start_iso,))
        reason_rows = [dict(r) for r in cursor.fetchall()]

        # ── Distribusi confidence (dari scan_log — event scan, bukan
        # trade open/close, jadi tetap pakai timestamp biasa) ──
        cursor.execute("""
            SELECT
                COUNT(*) as total_scanned,
                SUM(executed) as total_executed,
                COALESCE(AVG(ai_confidence), 0) as avg_confidence,
                COALESCE(MIN(ai_confidence), 0) as min_confidence,
                COALESCE(MAX(ai_confidence), 0) as max_confidence
            FROM scan_log
            WHERE timestamp >= ? AND ai_decision != ''
        """, (start_iso,))
        scan_stats = dict(cursor.fetchone())

        # ── Worst pair ──
        cursor.execute("""
            SELECT symbol, COUNT(*) as count, SUM(pnl_usdt) as total_pnl
            FROM trade_history
            WHERE close_timestamp >= ? AND status = 'CLOSED'
            GROUP BY symbol
            ORDER BY total_pnl ASC LIMIT 5
        """, (start_iso,))
        worst_pairs = cursor.fetchall()

    total = stats["total"] or 0
    wins  = stats["wins"] or 0
    wr    = (wins / total * 100) if total > 0 else 0

    days_elapsed = max(1, (datetime.now() - datetime.fromisoformat(start_iso)).days)

    reason_lines = []
    for r in reason_rows:
        r_wr = (r["wins"] / r["count"] * 100) if r["count"] > 0 else 0
        emoji = "✅" if r["avg_pnl"] >= 0 else "❌"
        reason_lines.append(
            f"  {emoji} {r['close_reason'] or '(kosong)':<15} {r['count']:>3}x | "
            f"total ${r['total_pnl']:+.2f} | avg ${r['avg_pnl']:+.2f} | WR dalam grup {r_wr:.0f}%"
        )

    worst_pair_lines = [f"  • {p[0]}: {p[1]}x trade | PnL ${p[2]:+.2f}" for p in worst_pairs] or ["  • Belum ada"]

    total_scanned  = scan_stats["total_scanned"] or 0
    total_executed = scan_stats["total_executed"] or 0

    report = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 LAPORAN EVALUASI EKSPERIMEN
   {label}
   ({days_elapsed} hari berjalan)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📈 RINGKASAN
- Total trades   : {total}
- Win rate       : {wr:.1f}%   ⚠️  angka ini SAJA tidak cukup — lihat breakdown di bawah
- Total PnL      : ${stats['total_pnl']:+.2f}
- Avg PnL/trade  : ${stats['avg_pnl']:+.2f}
- Best/Worst     : ${stats['best']:+.2f} / ${stats['worst']:+.2f}
- Avg durasi     : {stats['avg_dur']:.0f} menit

🔍 BREAKDOWN close_reason (INI YANG PALING PENTING DIBACA)
{chr(10).join(reason_lines) if reason_lines else '  • Belum ada trade closed'}

   Cara baca cepat:
   - Banyak SL dgn avg pnl sangat negatif → kualitas sinyal/confidence
   - Banyak STAGNANT dgn avg pnl mendekati $0 → breaker bekerja sesuai
     desain (menghindari modal terkunci), TAPI kalau jumlahnya besar,
     pertimbangkan apakah MAX_HOLD_MINUTES kurang pas
   - Banyak TRAILING dgn avg pnl kecil positif → trailing mengunci
     profit kecil terlalu cepat, pertimbangkan longgarkan callback
   - THESIS_INVALID dgn avg pnl negatif kecil (bukan besar) → review
     tesis berhasil memotong kerugian lebih awal dari SL asli

🤖 KALIBRASI AI (dari scan_log)
- Total discan     : {total_scanned}
- Dieksekusi       : {int(total_executed or 0)} ({(total_executed/total_scanned*100) if total_scanned else 0:.1f}%)
- Avg confidence   : {float(scan_stats['avg_confidence'] or 0):.0%}
- Range confidence : {float(scan_stats['min_confidence'] or 0):.0%} - {float(scan_stats['max_confidence'] or 0):.0%}

📉 5 PAIR PALING MERUGIKAN
{chr(10).join(worst_pair_lines)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""".strip()

    return report

# ============================================
# HISTORY LENGKAP — semua trade, bukan agregat
# ============================================

def generate_full_history() -> str:
    """
    List SETIAP trade yang pernah tercatat (OPEN maupun CLOSED), urut dari
    yang PALING BARU.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, timestamp, symbol, side, entry_price, exit_price,
                   pnl_usdt, pnl_percent, status, close_reason, duration_mins,
                   ai_confidence
            FROM trade_history
            ORDER BY id DESC
        """)
        rows = [dict(r) for r in cursor.fetchall()]

    if not rows:
        return "Belum ada trade sama sekali."

    lines = []
    for r in rows:
        ts = r["timestamp"][:16].replace("T", " ")
        if r["status"] == "OPEN":
            lines.append(
                f"#{r['id']:<4} {ts} | {r['symbol']:<12} {r['side']:<5} | "
                f"OPEN @ {r['entry_price']:.6g} | conf {r['ai_confidence']:.0%}"
            )
        else:
            emoji = "✅" if r["pnl_usdt"] >= 0 else "❌"
            lines.append(
                f"#{r['id']:<4} {ts} | {r['symbol']:<12} {r['side']:<5} | "
                f"{emoji} ${r['pnl_usdt']:+.2f} ({r['pnl_percent']:+.1f}%) | "
                f"{r['close_reason']:<14} | {r['duration_mins']}m | conf {r['ai_confidence']:.0%}"
            )

    total = len(rows)
    closed = [r for r in rows if r["status"] == "CLOSED"]
    wins = sum(1 for r in closed if r["pnl_usdt"] > 0)
    total_pnl = sum(r["pnl_usdt"] for r in closed)
    wr = (wins / len(closed) * 100) if closed else 0

    header = (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📜 HISTORY LENGKAP — {total} trade tercatat\n"
        f"   ({len(closed)} closed, WR {wr:.1f}%, total PnL ${total_pnl:+.2f})\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    return header + "\n" + "\n".join(lines)


if __name__ == "__main__":
    init_db()

    parser = argparse.ArgumentParser(description="Laporan bot trading")
    parser.add_argument("--start-experiment", action="store_true",
                         help="Tandai SEKARANG sebagai awal periode eksperimen (parameter dibekukan)")
    parser.add_argument("--since-experiment", action="store_true",
                         help="Generate laporan evaluasi sejak eksperimen terakhir ditandai")
    parser.add_argument("--since", type=str, default=None,
                         help="Generate laporan evaluasi sejak tanggal/waktu manual, format: 'YYYY-MM-DD' atau 'YYYY-MM-DD HH:MM:SS'")
    parser.add_argument("--all-time", action="store_true",
                         help="Laporan AGREGAT sejak trade pertama (breakdown close_reason, dst) — sama seperti --since tapi tanpa batas tanggal")
    parser.add_argument("--all-trades", action="store_true",
                         help="List SETIAP trade satu per satu (bukan agregat) — untuk cek history/trade tertentu")
    args = parser.parse_args()

    if args.start_experiment:
        mark_experiment_start()
        sys.exit(0)

    if args.all_trades:
        print(generate_full_history())
        sys.exit(0)

    if args.all_time:
        report = generate_range_report("1970-01-01T00:00:00", label="Sejak trade pertama (semua waktu)")
    elif args.since_experiment:
        start_iso = _read_experiment_start()
        report = generate_range_report(start_iso, label="Eksperimen (parameter dibekukan)")
    elif args.since:
        try:
            # Terima baik 'YYYY-MM-DD' maupun 'YYYY-MM-DD HH:MM:SS'
            start_dt = datetime.fromisoformat(args.since)
        except ValueError:
            print(f"❌ Format tanggal tidak valid: {args.since} (pakai 'YYYY-MM-DD' atau 'YYYY-MM-DD HH:MM:SS')")
            sys.exit(1)
        report = generate_range_report(start_dt.isoformat(), label=f"Sejak {args.since}")
    else:
        report = generate_report()

    print(report)

    # Auto copy ke clipboard kalau ada pyperclip
    try:
        import pyperclip
        pyperclip.copy(report)
        print("\n✅ Report sudah di-copy ke clipboard!")
    except Exception:
        print("\n💡 Tip: Install pyperclip untuk auto-copy:")
        print("   pip install pyperclip")