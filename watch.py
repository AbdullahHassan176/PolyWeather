"""
Live dashboard for PolyWeather Bot.

Usage:
    py -3.11 watch.py           # show snapshot and exit
    py -3.11 watch.py --live    # refresh every 30s until Ctrl+C
    py -3.11 watch.py --pnl     # full reconcile report (checks CLOB for outcomes)

What it shows
─────────────
  • USDC balance (live from CLOB)
  • Open positions (filled live trades still pending resolution)
  • Recent fills and unfilled attempts from this session
  • Running P&L for resolved trades
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx


LOG_FILE  = Path("paper_trades.jsonl")
CLOB_BASE = "https://clob.polymarket.com"


def _load() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    out = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _usdc_balance() -> str:
    """Fetch live USDC balance from CLOB API."""
    try:
        from config import cfg
        from trader.executor import build_clob_client, get_usdc_balance
        bal = get_usdc_balance()
        return f"${bal:.2f}"
    except Exception as exc:
        return f"(error: {exc})"


def _resolve_outcomes(entries: list[dict]) -> list[dict]:
    """Resolve any unresolved entries from CLOB (used by --pnl)."""
    pending = [e for e in entries if e.get("won") is None and e.get("fill_status") == "filled"]
    if not pending:
        return entries

    print(f"  Checking {len(pending)} open live positions against CLOB...")
    with httpx.Client(timeout=15) as client:
        for entry in pending:
            cid = entry.get("condition_id", "")
            if not cid:
                continue
            try:
                resp = client.get(f"{CLOB_BASE}/markets/{cid}")
                resp.raise_for_status()
                tokens = resp.json().get("tokens", [])
                for t in tokens:
                    if t.get("winner") is True:
                        entry["outcome"] = t.get("outcome", "")
                        entry["won"] = (entry["outcome"].lower() == entry["our_side"].lower())
                        break
            except Exception:
                pass
            time.sleep(0.3)

    # Persist updates
    with LOG_FILE.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    return entries


def _pnl_for(entries: list[dict]) -> tuple[float, float]:
    """Returns (realised_pnl, deployed_usdc) for filled+resolved entries."""
    deployed = 0.0
    pnl      = 0.0
    for e in entries:
        if e.get("fill_status") != "filled":
            continue
        spent = e.get("actual_spent") or e.get("bet_usdc", 0)
        deployed += spent
        if e.get("won") is True:
            price  = e.get("market_price", 0.5)
            shares = spent / max(price, 0.001)
            pnl   += shares - spent      # profit = shares × $1 − cost
        elif e.get("won") is False:
            pnl -= spent
    return pnl, deployed


def snapshot(entries: list[dict], show_balance: bool = True) -> None:
    W   = 72
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'=' * W}")
    print(f"  PolyWeather — {now}")
    print(f"{'=' * W}")

    if show_balance:
        print(f"  USDC balance  : {_usdc_balance()}")

    live_filled   = [e for e in entries if e.get("fill_status") == "filled"]
    live_unfilled = [e for e in entries if e.get("fill_status") == "unfilled"]
    open_pos      = [e for e in live_filled  if e.get("won") is None]
    resolved      = [e for e in live_filled  if e.get("won") is not None]

    pnl, deployed = _pnl_for(entries)

    print(f"\n  Filled orders   : {len(live_filled)}")
    print(f"  Open positions  : {len(open_pos)}")
    print(f"  Resolved        : {len(resolved)}")
    print(f"  Unfilled/aborted: {len(live_unfilled)}")
    print(f"  Capital deployed: ${deployed:.2f}")
    if resolved:
        wins = sum(1 for e in resolved if e["won"])
        print(f"  Win rate        : {wins}/{len(resolved)} = {wins/len(resolved):.1%}")
    print(f"  Realised P&L    : ${pnl:+.2f}")

    SEP = "-" * (W - 2)

    # ── Open positions ─────────────────────────────────────────────────────────
    if open_pos:
        print(f"\n  {SEP}")
        print(f"  OPEN POSITIONS ({len(open_pos)})")
        print(f"  {SEP}")
        print(f"  {'Date':>10}  {'Side':>4}  {'Bet':>6}  {'Price':>6}  {'Edge':>6}  Market")
        for e in sorted(open_pos, key=lambda x: x.get("target_date", "")):
            date  = e.get("target_date", "?")[-5:]
            side  = e.get("our_side", "?")
            bet   = e.get("actual_spent") or e.get("bet_usdc", 0)
            price = e.get("market_price", 0)
            edge  = e.get("edge", 0)
            q     = e.get("question", "")[:42]
            print(f"  {date:>10}  {side:>4}  ${bet:>5.2f}  {price:>6.3f}  {edge:>+6.3f}  {q}")

    # ── Resolved P&L breakdown ─────────────────────────────────────────────────
    if resolved:
        print(f"\n  {SEP}")
        print(f"  RESOLVED TRADES ({len(resolved)})")
        print(f"  {SEP}")
        print(f"  {'Date':>10}  {'Side':>4}  {'Bet':>6}  {'Result':>6}  {'P&L':>7}  Market")
        for e in sorted(resolved, key=lambda x: x.get("target_date", "")):
            date   = e.get("target_date", "?")[-5:]
            side   = e.get("our_side", "?")
            spent  = e.get("actual_spent") or e.get("bet_usdc", 0)
            won    = e.get("won", False)
            result = "WIN " if won else "LOSS"
            if won:
                price  = e.get("market_price", 0.5)
                shares = spent / max(price, 0.001)
                trade_pnl = shares - spent
            else:
                trade_pnl = -spent
            q = e.get("question", "")[:42]
            print(f"  {date:>10}  {side:>4}  ${spent:>5.2f}  {result:>6}  ${trade_pnl:>+6.2f}  {q}")

    # ── Recent unfilled (last 5) ───────────────────────────────────────────────
    if live_unfilled:
        print(f"\n  {SEP}")
        recent_unfilled = sorted(live_unfilled, key=lambda x: x.get("ts", ""))[-5:]
        print(f"  RECENT UNFILLED (last {len(recent_unfilled)})")
        for e in recent_unfilled:
            q = e.get("question", "")[:55]
            print(f"    {e.get('ts','')[:16]}  {e.get('our_side','?'):3s}  ${e.get('bet_usdc',0):.2f}  {q}")

    print(f"\n{'=' * W}\n")


def main():
    args     = set(sys.argv[1:])
    live     = "--live" in args
    pnl_mode = "--pnl" in args

    if pnl_mode:
        entries = _resolve_outcomes(_load())
    else:
        entries = _load()

    snapshot(entries, show_balance=True)

    if live:
        print("  Refreshing every 30s  (Ctrl+C to stop)")
        try:
            while True:
                time.sleep(30)
                entries = _load()
                snapshot(entries, show_balance=True)
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
