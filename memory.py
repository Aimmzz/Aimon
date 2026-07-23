import sqlite3
import os
from datetime import datetime, date
from contextlib import contextmanager

DB_PATH = "bot_memory.db"

# ============================================
# CONNECTION MANAGER
# ============================================

@contextmanager
def get_db():
    """Thread-safe database connection"""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row  # Akses kolom by name
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

# ============================================
# INIT DATABASE
# ============================================

def init_db():
    """Buat semua tabel kalau belum ada"""
    with get_db() as conn:
        cursor = conn.cursor()

        # Tabel 1: History semua trade bot
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trade_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT NOT NULL,
                symbol          TEXT NOT NULL,
                side            TEXT NOT NULL,  -- LONG atau SHORT
                entry_price     REAL NOT NULL,
                exit_price      REAL DEFAULT 0,
                margin          REAL NOT NULL,
                leverage        INTEGER NOT NULL,
                pnl_usdt        REAL DEFAULT 0,
                pnl_percent     REAL DEFAULT 0,
                status          TEXT NOT NULL,  -- OPEN, CLOSED, LIQUIDATED
                close_reason    TEXT DEFAULT '', -- TP, SL, TRAILING, MANUAL, THESIS_INVALID, STAGNANT
                ai_confidence   REAL DEFAULT 0,
                ai_reasoning    TEXT DEFAULT '',
                rsi_at_entry    REAL DEFAULT 0,
                macd_at_entry   TEXT DEFAULT '',
                volume_spike    INTEGER DEFAULT 0,
                duration_mins   INTEGER DEFAULT 0,
                tp_price        REAL DEFAULT 0,
                sl_price        REAL DEFAULT 0
            )
        """)

        # Tabel 2: Daily performance summary
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_summary (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                date            TEXT NOT NULL UNIQUE,
                total_trades    INTEGER DEFAULT 0,
                winning_trades  INTEGER DEFAULT 0,
                losing_trades   INTEGER DEFAULT 0,
                win_rate        REAL DEFAULT 0,
                total_pnl_usdt  REAL DEFAULT 0,
                best_trade_pnl  REAL DEFAULT 0,
                worst_trade_pnl REAL DEFAULT 0,
                total_long      INTEGER DEFAULT 0,
                total_short     INTEGER DEFAULT 0
            )
        """)

        # Tabel 3: Strategy policy (hasil learning AI)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS strategy_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                policy      TEXT NOT NULL,
                source      TEXT DEFAULT 'post_mortem'
            )
        """)

        # Tabel: Config epoch — catat kapan parameter TRADING terakhir
        # berubah (fingerprint dari config.get_trading_params_fingerprint()).
        # Post-mortem pakai ini supaya sampling winner/loser tidak
        # mencampur data dari rezim SL/TP/trailing yang berbeda-beda.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS config_epochs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                fingerprint TEXT NOT NULL
            )
        """)

        # Tabel 4: Scanner log — token yang pernah dianalisis
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scan_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT NOT NULL,
                symbol          TEXT NOT NULL,
                price_change    REAL DEFAULT 0,
                volume_24h      REAL DEFAULT 0,
                rsi             REAL DEFAULT 0,
                ai_decision     TEXT DEFAULT '',
                ai_confidence   REAL DEFAULT 0,
                executed        INTEGER DEFAULT 0  -- 0=skip, 1=traded
            )
        """)

        # ── Migrasi kolom baru untuk fitur review tesis posisi aktif ──
        # trade_history sudah ada isinya di DB lama, jadi kolom baru tidak
        # bisa lewat CREATE TABLE IF NOT EXISTS (itu cuma berlaku untuk
        # tabel yang belum ada sama sekali) — perlu ALTER TABLE eksplisit.
        # Dibungkus try/except karena SQLite akan error kalau kolom sudah
        # ada (dijalankan berkali-kali tiap init_db() aman).
        for column_def in [
            "last_reviewed_at TEXT DEFAULT NULL",
            "review_count INTEGER DEFAULT 0",
            "highest_price REAL DEFAULT NULL",
            "lowest_price REAL DEFAULT NULL",
        ]:
            try:
                cursor.execute(f"ALTER TABLE trade_history ADD COLUMN {column_def}")
            except sqlite3.OperationalError:
                pass  # kolom sudah ada dari init_db() sebelumnya

        # ── Migrasi close_timestamp ──
        # SEBELUMNYA: semua query "hari ini"/"sejak tanggal X" (get_daily_pnl,
        # get_symbol_sl_count_today, dst) filter pakai `timestamp` (waktu
        # OPEN posisi), bukan waktu CLOSE. Bug nyata: posisi yang dibuka
        # 23:50 dan close 00:30 (lewat tengah malam) PnL-nya tercatat ke
        # tanggal KEMARIN — bisa lolos dari gerbang MAX_DAILY_LOSS untuk
        # kedua hari sekaligus (tidak masuk hitungan "hari ini" karena
        # tanggalnya kemarin, tidak masuk hitungan "kemarin" karena saat
        # itu masih OPEN). Sekarang ada kolom close_timestamp eksplisit,
        # diisi PERSIS saat close terjadi (lihat save_trade_close()).
        try:
            cursor.execute("ALTER TABLE trade_history ADD COLUMN close_timestamp TEXT DEFAULT NULL")
        except sqlite3.OperationalError:
            pass  # kolom sudah ada

        # Backfill baris CLOSED lama yang belum punya close_timestamp,
        # pakai pendekatan lama (entry + duration_mins) sebagai perkiraan
        # terbaik — sekali jalan, tidak menimpa yang sudah terisi.
        # replace(...,' ','T'): SQLite datetime() pakai spasi sebagai
        # pemisah, tapi semua timestamp yang ditulis Python pakai 'T'
        # (dari .isoformat()) — format harus SAMA, karena perbandingan
        # tanggal di query lain (DATE(), >=) berbasis string lexicographic.
        cursor.execute("""
            UPDATE trade_history
            SET close_timestamp = replace(
                datetime(timestamp, '+' || duration_mins || ' minutes'), ' ', 'T'
            )
            WHERE status = 'CLOSED' AND close_timestamp IS NULL
        """)

        # ── Migrasi kolom baru untuk verifikasi post-mortem ──
        # Sebelumnya strategy_log cuma simpan teks policy — tidak ada cara
        # objektif untuk cek apakah policy versi baru BENAR lebih baik dari
        # sebelumnya. Kolom ini menyimpan snapshot performa SEBELUM update
        # terjadi, supaya bisa dibandingkan dengan performa SETELAHNYA nanti
        # (window sejak update ini sampai update berikutnya).
        for column_def in [
            "win_rate_before REAL DEFAULT NULL",
            "avg_pnl_pct_before REAL DEFAULT NULL",
            "sample_size INTEGER DEFAULT NULL",
        ]:
            try:
                cursor.execute(f"ALTER TABLE strategy_log ADD COLUMN {column_def}")
            except sqlite3.OperationalError:
                pass  # kolom sudah ada dari init_db() sebelumnya

        # ── Migration: tambah kolom tp_price/sl_price kalau DB lama belum punya ──
        cursor.execute("PRAGMA table_info(trade_history)")
        cols = [c[1] for c in cursor.fetchall()]
        if "tp_price" not in cols:
            cursor.execute("ALTER TABLE trade_history ADD COLUMN tp_price REAL DEFAULT 0")
        if "sl_price" not in cols:
            cursor.execute("ALTER TABLE trade_history ADD COLUMN sl_price REAL DEFAULT 0")

    print("✅ Database siap — bot_memory.db")

# ============================================
# TRADE OPERATIONS
# ============================================

def save_trade_open(
    symbol: str,
    side: str,
    entry_price: float,
    margin: float,
    leverage: int,
    ai_confidence: float,
    ai_reasoning: str,
    rsi: float,
    macd: str,
    volume_spike: bool,
    tp_price: float = 0,
    sl_price: float = 0
) -> int:
    """Simpan trade yang baru dibuka, return trade_id"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO trade_history (
                timestamp, symbol, side, entry_price,
                margin, leverage, status,
                ai_confidence, ai_reasoning,
                rsi_at_entry, macd_at_entry, volume_spike,
                tp_price, sl_price, highest_price, lowest_price
            ) VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(),
            symbol, side, entry_price,
            margin, leverage,
            ai_confidence, ai_reasoning,
            rsi, macd, int(volume_spike),
            tp_price, sl_price, entry_price, entry_price
        ))
        trade_id = cursor.lastrowid
        print(f"💾 Trade #{trade_id} dibuka — {symbol} {side} @ {entry_price} | TP {tp_price} | SL {sl_price}")
        return trade_id

def save_trade_close(
    trade_id: int,
    exit_price: float,
    pnl_usdt: float,
    pnl_percent: float,
    close_reason: str,
    duration_mins: int
):
    """Update trade yang sudah ditutup"""
    with get_db() as conn:
        conn.execute("""
            UPDATE trade_history SET
                exit_price      = ?,
                pnl_usdt        = ?,
                pnl_percent     = ?,
                status          = 'CLOSED',
                close_reason    = ?,
                duration_mins   = ?,
                close_timestamp = ?
            WHERE id = ?
        """, (exit_price, pnl_usdt, pnl_percent, close_reason, duration_mins, datetime.now().isoformat(), trade_id))

        emoji = "✅" if pnl_usdt > 0 else "❌"
        print(f"{emoji} Trade #{trade_id} ditutup — PnL: ${pnl_usdt:.2f} ({pnl_percent:.1f}%) | Alasan: {close_reason}")

def update_trade_tp(trade_id: int, new_tp: float):
    """Update TP price aktif untuk trade yang sedang OPEN (dynamic TP extend)"""
    with get_db() as conn:
        conn.execute("""
            UPDATE trade_history SET tp_price = ? WHERE id = ? AND status = 'OPEN'
        """, (new_tp, trade_id))
        print(f"📝 Trade #{trade_id} — TP di-extend ke {new_tp}")

def update_trade_sl(trade_id: int, new_sl: float):
    """Update SL price aktif untuk trade yang sedang OPEN (hasil review tesis)"""
    with get_db() as conn:
        conn.execute("""
            UPDATE trade_history SET sl_price = ? WHERE id = ? AND status = 'OPEN'
        """, (new_sl, trade_id))
        print(f"📝 Trade #{trade_id} — SL diperketat ke {new_sl}")

def update_trade_extremes(trade_id: int, highest_price: float, lowest_price: float):
    """
    Simpan harga tertinggi & terendah yang PERNAH dicapai selama trade ini
    berjalan — dipanggil setiap monitoring check (tiap POSITION_CHECK_SECONDS).

    Ini yang membuat trailing stop benar-benar berfungsi: sebelumnya
    highest/lowest dihitung ulang dari nol setiap kali cek (main.py lama
    pakai `max(current_price, entry_price)`), jadi begitu harga turun lagi
    dari puncaknya, bot "lupa" pernah ada di titik tertinggi — trailing SL
    jadi efektif tidak pernah bisa trigger. Sekarang persisten ke DB,
    persis seperti cooldown SL yang sudah diperbaiki sebelumnya.
    """
    with get_db() as conn:
        conn.execute("""
            UPDATE trade_history
            SET highest_price = ?, lowest_price = ?
            WHERE id = ? AND status = 'OPEN'
        """, (highest_price, lowest_price, trade_id))

def mark_trade_reviewed(trade_id: int):
    """
    Catat bahwa review tesis baru saja dilakukan untuk trade ini — dipanggil
    SETIAP kali review terjadi (termasuk saat hasilnya HOLD), supaya jadwal
    review berikutnya dihitung dari review TERAKHIR, bukan dari entry.
    """
    with get_db() as conn:
        conn.execute("""
            UPDATE trade_history
            SET last_reviewed_at = ?, review_count = review_count + 1
            WHERE id = ? AND status = 'OPEN'
        """, (datetime.now().isoformat(), trade_id))

def get_trades_due_for_review(min_hold_minutes: int, review_interval_minutes: int) -> list:
    """
    Ambil trade OPEN yang sudah waktunya direview ulang tesisnya.

    Due kalau:
    - Sudah OPEN minimal `min_hold_minutes` (belum pernah direview, posisi
      masih baru — beri waktu data teknikal terbentuk dulu), ATAU
    - Sudah `review_interval_minutes` sejak review TERAKHIR (bukan sejak
      entry — supaya intervalnya konsisten tiap review, bukan makin jarang)
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trade_history WHERE status = 'OPEN'")
        trades = [dict(row) for row in cursor.fetchall()]

    due = []
    now = datetime.now()
    for t in trades:
        try:
            if t.get("last_reviewed_at"):
                last = datetime.fromisoformat(t["last_reviewed_at"])
                elapsed_min = (now - last).total_seconds() / 60
                if elapsed_min >= review_interval_minutes:
                    due.append(t)
            else:
                entry_dt = datetime.fromisoformat(t["timestamp"])
                elapsed_min = (now - entry_dt).total_seconds() / 60
                if elapsed_min >= min_hold_minutes:
                    due.append(t)
        except (ValueError, TypeError):
            continue  # timestamp rusak — skip, jangan crash seluruh cycle

    return due

def get_open_trades() -> list:
    """Ambil semua trade yang masih terbuka"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trade_history WHERE status = 'OPEN'")
        return [dict(row) for row in cursor.fetchall()]

# ============================================
# STREAK TRACKING (untuk anti-martingale sizing)
# ============================================

def get_current_streak() -> dict:
    """
    Hitung win/loss streak berjalan dari trade CLOSED terakhir.
    Return: {"type": "WIN"|"LOSS"|"NONE", "count": int}

    Streak dihitung dari trade paling baru mundur ke belakang,
    berhenti begitu hasil (win/loss) berubah arah.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT pnl_usdt
            FROM trade_history
            WHERE status = 'CLOSED'
            ORDER BY id DESC
            LIMIT 50
        """)
        rows = [r[0] for r in cursor.fetchall()]

    if not rows:
        return {"type": "NONE", "count": 0}

    streak_type = "WIN" if rows[0] > 0 else "LOSS"
    count = 0
    for pnl in rows:
        is_win = pnl > 0
        if (streak_type == "WIN" and is_win) or (streak_type == "LOSS" and not is_win):
            count += 1
        else:
            break

    return {"type": streak_type, "count": count}


def get_last_sl_time() -> str | None:
    """
    Waktu CLOSE dari trade terakhir yang berakhir karena SL.

    Dipakai untuk cooldown SL yang PERSISTEN lintas restart — berbeda dari
    pendekatan lama yang menyimpan waktu di variabel Python biasa
    (last_sl_time di risk_manager.py). Variabel memori itu HILANG setiap
    kali proses bot di-restart, sehingga bot "lupa" baru saja kena SL dan
    bisa re-entry ke symbol yang sama tanpa jeda.

    Sekarang baca close_timestamp LANGSUNG (kolom eksplisit, diisi persis
    saat close terjadi) — sebelumnya diperkirakan dari entry_time +
    duration_mins karena kolom ini belum ada; sekarang lebih akurat.

    Return ISO timestamp, atau None kalau belum pernah ada trade SL.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT close_timestamp
            FROM trade_history
            WHERE status = 'CLOSED' AND close_reason = 'SL'
            ORDER BY id DESC LIMIT 1
        """)
        row = cursor.fetchone()

    return row["close_timestamp"] if row else None

def get_symbol_sl_count_today(symbol: str) -> int:
    """
    Berapa kali SYMBOL INI (bukan global) kena SL HARI INI.

    Dipakai untuk cooldown per-symbol yang lebih tegas daripada cooldown
    global (COOLDOWN_AFTER_SL, 15 menit semua symbol) — kalau satu symbol
    yang SAMA sudah kena SL beberapa kali di hari yang sama (pola nyata:
    PROMUSDT 2x SL dalam sehari), itu sinyal kuat symbol tsb sedang
    konsisten melawan pembacaan bot, bukan cuma butuh jeda sesaat.

    Filter pakai close_timestamp (bukan timestamp/waktu open) — SL yang
    terjadi setelah tengah malam harus terhitung ke hari itu, bukan ke
    hari saat posisinya dibuka.
    """
    today = date.today().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) as cnt
            FROM trade_history
            WHERE symbol = ? AND close_reason = 'SL'
              AND status = 'CLOSED' AND DATE(close_timestamp) = ?
        """, (symbol, today))
        row = cursor.fetchone()
    return row["cnt"] if row else 0


# ============================================
# DAILY STATS
# ============================================

def get_daily_pnl() -> float:
    """
    Total PnL hari ini — dipakai gerbang MAX_DAILY_LOSS.

    Filter pakai close_timestamp, BUKAN timestamp (waktu open). Sebelumnya
    filter timestamp berarti posisi yang dibuka 23:50 dan close 00:30
    (lewat tengah malam) PnL-nya masuk hitungan tanggal KEMARIN — bisa
    lolos dari MAX_DAILY_LOSS untuk kedua hari sekaligus.
    """
    today = date.today().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(SUM(pnl_usdt), 0)
            FROM trade_history
            WHERE DATE(close_timestamp) = ? AND status = 'CLOSED'
        """, (today,))
        return cursor.fetchone()[0]

def get_daily_trade_count() -> int:
    """Jumlah trade yang CLOSE hari ini (bukan yang dibuka hari ini)"""
    today = date.today().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*)
            FROM trade_history
            WHERE DATE(close_timestamp) = ? AND status = 'CLOSED'
        """, (today,))
        return cursor.fetchone()[0]

def update_daily_summary():
    """Update ringkasan harian"""
    today = date.today().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl_usdt < 0 THEN 1 ELSE 0 END) as losses,
                SUM(pnl_usdt) as total_pnl,
                MAX(pnl_usdt) as best,
                MIN(pnl_usdt) as worst,
                SUM(CASE WHEN side = 'LONG' THEN 1 ELSE 0 END) as longs,
                SUM(CASE WHEN side = 'SHORT' THEN 1 ELSE 0 END) as shorts
            FROM trade_history
            WHERE DATE(close_timestamp) = ? AND status = 'CLOSED'
        """, (today,))

        row = cursor.fetchone()
        if not row or row[0] == 0:
            return

        total, wins, losses, total_pnl, best, worst, longs, shorts = row
        win_rate = (wins / total * 100) if total > 0 else 0

        cursor.execute("""
            INSERT INTO daily_summary (
                date, total_trades, winning_trades, losing_trades,
                win_rate, total_pnl_usdt, best_trade_pnl, worst_trade_pnl,
                total_long, total_short
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                total_trades    = excluded.total_trades,
                winning_trades  = excluded.winning_trades,
                losing_trades   = excluded.losing_trades,
                win_rate        = excluded.win_rate,
                total_pnl_usdt  = excluded.total_pnl_usdt,
                best_trade_pnl  = excluded.best_trade_pnl,
                worst_trade_pnl = excluded.worst_trade_pnl,
                total_long      = excluded.total_long,
                total_short     = excluded.total_short
        """, (today, total, wins, losses, win_rate, total_pnl, best, worst, longs, shorts))

# ============================================
# PERFORMANCE STATS
# ============================================

def get_performance_stats() -> dict:
    """Ambil statistik lengkap untuk dashboard"""
    with get_db() as conn:
        cursor = conn.cursor()

        # Overall stats
        cursor.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) as wins,
                COALESCE(SUM(pnl_usdt), 0) as total_pnl,
                COALESCE(AVG(pnl_percent), 0) as avg_pnl_pct,
                COALESCE(MAX(pnl_usdt), 0) as best,
                COALESCE(MIN(pnl_usdt), 0) as worst,
                COALESCE(AVG(duration_mins), 0) as avg_duration
            FROM trade_history WHERE status = 'CLOSED'
        """)
        overall = dict(cursor.fetchone())

        # Close reason breakdown
        cursor.execute("""
            SELECT close_reason, COUNT(*) as count
            FROM trade_history
            WHERE status = 'CLOSED'
            GROUP BY close_reason
        """)
        close_reasons = dict(cursor.fetchall())

        # Recent 5 trades
        cursor.execute("""
            SELECT timestamp, symbol, side, pnl_usdt, pnl_percent, close_reason
            FROM trade_history
            WHERE status = 'CLOSED'
            ORDER BY id DESC LIMIT 5
        """)
        recent = [dict(row) for row in cursor.fetchall()]

        total = overall["total"]
        wins  = overall["wins"] or 0
        win_rate = (wins / total * 100) if total > 0 else 0

        return {
            "total_trades"  : total,
            "win_rate"      : round(win_rate, 1),
            "total_pnl"     : round(overall["total_pnl"], 2),
            "avg_pnl_pct"   : round(overall["avg_pnl_pct"], 2),
            "best_trade"    : round(overall["best"], 2),
            "worst_trade"   : round(overall["worst"], 2),
            "avg_duration"  : round(overall["avg_duration"], 1),
            "close_reasons" : close_reasons,
            "recent_trades" : recent,
            "daily_pnl"     : round(get_daily_pnl(), 2),
            "daily_trades"  : get_daily_trade_count()
        }

def get_performance_snapshot(since_iso: str = None, exclude_manual: bool = True) -> dict:
    """
    Snapshot performa ringkas (win rate, avg pnl_percent, jumlah sampel)
    sejak `since_iso` sampai sekarang — atau sepanjang waktu kalau None.

    Dipakai untuk VERIFIKASI post-mortem: simpan snapshot ini SEBELUM
    setiap update strategy_policy, supaya nanti bisa dibandingkan dengan
    performa SETELAH policy itu dipakai — mengecek apakah policy baru
    BENAR memperbaiki hasil, bukan cuma diasumsikan lebih baik oleh AI.

    exclude_manual: kecualikan close_reason='MANUAL' — konsisten dengan
    perform_post_mortem() yang juga mengecualikan trade manual dari
    pembelajaran, karena itu keputusan manusia, bukan keputusan bot.
    """
    where = "status = 'CLOSED'"
    params = []
    if since_iso:
        where += " AND close_timestamp >= ?"
        params.append(since_iso)
    if exclude_manual:
        where += " AND close_reason != 'MANUAL'"

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) as wins,
                COALESCE(AVG(pnl_percent), 0) as avg_pnl_pct
            FROM trade_history WHERE {where}
        """, params)
        row = dict(cursor.fetchone())

    total = row["total"] or 0
    wins  = row["wins"] or 0
    win_rate = (wins / total * 100) if total > 0 else 0

    return {
        "sample_size" : total,
        "win_rate"    : round(win_rate, 1),
        "avg_pnl_pct" : round(row["avg_pnl_pct"], 2)
    }

def get_last_strategy_update_time() -> str | None:
    """Waktu update strategy_policy TERAKHIR — dipakai untuk menentukan window snapshot 'before'"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp FROM strategy_log ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
    return row["timestamp"] if row else None

def ensure_config_epoch(fingerprint: str) -> tuple[str, bool]:
    """
    Bandingkan fingerprint parameter trading SEKARANG dengan yang terakhir
    tersimpan. Kalau beda (atau belum pernah ada sama sekali), catat epoch
    BARU dengan timestamp sekarang — post-mortem akan sampling HANYA dari
    trade setelah titik ini, supaya tidak mencampur data dari rezim
    parameter yang berbeda (SL/TP/trailing yang pernah berubah beberapa kali).

    Dipanggil SEKALI di startup (main.py). Return (epoch_start_iso, is_new).
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp, fingerprint FROM config_epochs ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()

        if row and row["fingerprint"] == fingerprint:
            return row["timestamp"], False  # tidak berubah, epoch tetap sama

        now_iso = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO config_epochs (timestamp, fingerprint) VALUES (?, ?)",
            (now_iso, fingerprint)
        )
        return now_iso, True

def get_current_config_epoch_start() -> str | None:
    """Waktu mulai epoch config AKTIF — None kalau belum pernah dicatat sama sekali"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp FROM config_epochs ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
    return row["timestamp"] if row else None

def save_scan_log(
    symbol: str,
    price_change: float,
    volume_24h: float,
    rsi: float,
    ai_decision: str,
    ai_confidence: float,
    executed: bool
):
    """Log setiap token yang discan"""
    with get_db() as conn:
        conn.execute("""
            INSERT INTO scan_log (
                timestamp, symbol, price_change, volume_24h,
                rsi, ai_decision, ai_confidence, executed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(),
            symbol, price_change, volume_24h,
            rsi, ai_decision, ai_confidence, int(executed)
        ))

def print_stats():
    """Print stats ke terminal"""
    stats = get_performance_stats()
    print("\n" + "="*45)
    print("📊 PERFORMANCE SUMMARY")
    print("="*45)
    print(f"Total trades    : {stats['total_trades']}")
    print(f"Win rate        : {stats['win_rate']}%")
    print(f"Total PnL       : ${stats['total_pnl']}")
    print(f"Best trade      : ${stats['best_trade']}")
    print(f"Worst trade     : ${stats['worst_trade']}")
    print(f"Avg duration    : {stats['avg_duration']} menit")
    print(f"\nHari ini:")
    print(f"  Trades        : {stats['daily_trades']}")
    print(f"  PnL           : ${stats['daily_pnl']}")
    if stats['close_reasons']:
        print(f"\nClose reasons:")
        for reason, count in stats['close_reasons'].items():
            print(f"  {reason:<12}: {count}x")
    print(f"\n🕐 5 Trade Terakhir:")
    for t in stats['recent_trades']:
        time_str = t['timestamp'][11:16]
        emoji    = "✅" if t['pnl_usdt'] > 0 else "❌"
        print(f"  {emoji} [{time_str}] {t['symbol']:<12} {t['side']:<5} ${t['pnl_usdt']:>6.2f} ({t['pnl_percent']:>5.1f}%) — {t['close_reason']}")
    print("="*45)


if __name__ == "__main__":
    init_db()
    print("\n📝 Test insert dummy trade...")

    tid = save_trade_open(
        symbol="BTCUSDT", side="LONG",
        entry_price=67000, margin=5.0, leverage=3,
        ai_confidence=0.82, ai_reasoning="RSI oversold + volume spike + MACD crossover bullish",
        rsi=32.5, macd="bullish_cross", volume_spike=True,
        tp_price=68000, sl_price=66500
    )

    save_trade_close(
        trade_id=tid, exit_price=67800,
        pnl_usdt=0.36, pnl_percent=3.6,
        close_reason="TP", duration_mins=12
    )

    update_daily_summary()
    print_stats()