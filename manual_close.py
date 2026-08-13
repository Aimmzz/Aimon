"""
manual_close.py — Tutup posisi terbuka secara manual, di luar TP/SL/trailing.

KENAPA SCRIPT TERPISAH, BUKAN FITUR DI DALAM main.py:
main.py pakai `schedule` yang jalan sinkron di loop utama — kalau close
manual ditaruh di situ (misal lewat input()), itu akan MEMBLOKIR seluruh
scan cycle sampai kamu menjawab prompt. Jadi ini script berdiri sendiri,
dijalankan di terminal LAIN sementara bot utama tetap berjalan normal.

Database (bot_memory.db) adalah single source of truth untuk posisi
terbuka — begitu script ini menutup sebuah trade, cycle bot berikutnya
otomatis tidak akan lagi memonitornya. Tidak ada konflik.

CATATAN PAPER MODE:
Kalau PAPER_TRADE_MODE=True, posisi ini TIDAK PERNAH ada di Binance sama
sekali — murni simulasi di database lokal. "Close manual" di sini berarti
update database + ambil harga terkini untuk hitung PnL, BUKAN kirim order
ke Binance. Di mode LIVE, executor.close_position() yang dipanggil sama
persis akan tetap mengirim order close asli ke exchange seperti biasa.

Jalankan:
    python manual_close.py
"""

import memory
import executor
import config


def _calc_pnl(trade: dict, current_price: float) -> tuple[float, float]:
    entry    = trade["entry_price"]
    leverage = trade["leverage"]
    margin   = trade["margin"]

    if trade["side"] == "LONG":
        pnl_pct  = (current_price - entry) / entry * 100 * leverage
        pnl_usdt = (current_price - entry) / entry * margin * leverage
    else:
        pnl_pct  = (entry - current_price) / entry * 100 * leverage
        pnl_usdt = (entry - current_price) / entry * margin * leverage

    return pnl_usdt, pnl_pct


def main():
    memory.init_db()
    open_trades = memory.get_open_trades()

    if not open_trades:
        print("📭 Tidak ada posisi terbuka saat ini.")
        return

    mode_label = "🧪 PAPER (simulasi lokal, tidak ada di Binance)" if executor.PAPER_TRADE_MODE else "🔴 LIVE (akan kirim order close ke Binance)"
    print(f"\nMode: {mode_label}")
    print("\n📍 POSISI TERBUKA")
    print("=" * 78)

    rows = []
    for t in open_trades:
        current_price = executor.get_current_price(t["symbol"])
        pnl_usdt, pnl_pct = _calc_pnl(t, current_price)
        rows.append((t, current_price, pnl_usdt, pnl_pct))

    for i, (t, current_price, pnl_usdt, pnl_pct) in enumerate(rows, 1):
        emoji = "📈" if pnl_usdt >= 0 else "📉"
        print(
            f"  {i}. #{t['id']} {t['symbol']:<12} {t['side']:<5} "
            f"entry {t['entry_price']:.6g} → now {current_price:.6g} | "
            f"{emoji} PnL ${pnl_usdt:+.2f} ({pnl_pct:+.1f}%)"
        )
    print("=" * 78)

    choice = input("\nMasukkan nomor posisi yang mau ditutup (kosongkan untuk batal): ").strip()
    if not choice or choice.lower() in ("batal", "cancel", "0"):
        print("Dibatalkan.")
        return

    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(rows):
            raise IndexError
        trade, current_price, pnl_usdt, pnl_pct = rows[idx]
    except (ValueError, IndexError):
        print("❌ Nomor tidak valid.")
        return

    confirm = input(
        f"Yakin tutup #{trade['id']} {trade['symbol']} {trade['side']} "
        f"(estimasi PnL ${pnl_usdt:+.2f})? Ketik 'y' untuk konfirmasi: "
    ).strip().lower()
    if confirm != "y":
        print("Dibatalkan.")
        return

    # qty TERSIMPAN dari saat open (pembulatan step_size sudah termasuk);
    # fallback hitung ulang hanya untuk trade lama sebelum kolom quantity ada
    qty = memory.get_trade_quantity(trade)

    result = executor.close_position(
        trade_id     = trade["id"],
        symbol       = trade["symbol"],
        side         = trade["side"],
        quantity     = qty,
        entry_price  = trade["entry_price"],
        entry_time   = trade["timestamp"],
        close_reason = "MANUAL",
        margin       = trade["margin"],
        leverage     = trade["leverage"]
    )

    if result["status"] == "ok":
        print(f"\n✅ Posisi #{trade['id']} ditutup — PnL: ${result['pnl_usdt']:+.2f} ({result['pnl_pct']:+.1f}%)")
        print("   Bot utama akan otomatis berhenti memonitor posisi ini di cycle berikutnya.")
    else:
        print(f"\n❌ Gagal menutup: {result.get('reason')}")


if __name__ == "__main__":
    main()