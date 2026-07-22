import sqlite3

conn = sqlite3.connect("bot_memory.db")
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# ── 1. Distribusi confidence 24 jam terakhir ──
print("\n📊 DISTRIBUSI CONFIDENCE (24 jam terakhir)")
print("=" * 45)
cur.execute("""
    SELECT ROUND(ai_confidence, 2) AS conf, ai_decision, COUNT(*) AS jumlah
    FROM scan_log
    WHERE timestamp > datetime('now', '-1 day') AND ai_decision != ''
    GROUP BY conf, ai_decision
    ORDER BY conf
""")
rows = cur.fetchall()
if rows:
    for r in rows:
        bar = "█" * min(r["jumlah"], 40)
        print(f"  {r['conf']:.2f} | {r['ai_decision']:<5} | {r['jumlah']:>3}x {bar}")
else:
    print("  (belum ada data scan 24 jam terakhir)")

# ── 2. Ringkasan keputusan AI ──
print("\n🤖 RINGKASAN KEPUTUSAN AI (24 jam)")
print("=" * 45)
cur.execute("""
    SELECT ai_decision,
           COUNT(*) AS jumlah,
           ROUND(AVG(ai_confidence), 2) AS avg_conf,
           ROUND(MIN(ai_confidence), 2) AS min_conf,
           ROUND(MAX(ai_confidence), 2) AS max_conf,
           SUM(executed) AS dieksekusi
    FROM scan_log
    WHERE timestamp > datetime('now', '-1 day') AND ai_decision != ''
    GROUP BY ai_decision
""")
for r in cur.fetchall():
    print(f"  {r['ai_decision']:<5} : {r['jumlah']:>3}x | conf avg {r['avg_conf']} "
          f"(min {r['min_conf']} max {r['max_conf']}) | eksekusi {r['dieksekusi']}x")

# ── 3. Trade terakhir ──
print("\n📋 10 TRADE TERAKHIR")
print("=" * 45)
cur.execute("""
    SELECT timestamp, symbol, side, status, pnl_usdt, close_reason, ai_confidence
    FROM trade_history
    ORDER BY id DESC LIMIT 10
""")
rows = cur.fetchall()
if rows:
    for r in rows:
        pnl = f"${r['pnl_usdt']:+.2f}" if r["status"] == "CLOSED" else "OPEN"
        print(f"  [{r['timestamp'][5:16]}] {r['symbol']:<12} {r['side']:<5} "
              f"{pnl:>8} | {r['close_reason'] or '-':<8} | conf {r['ai_confidence']:.2f}")
else:
    print("  (belum ada trade)")

conn.close()
print()