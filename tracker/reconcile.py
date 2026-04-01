"""
Paper-trade reconciler.

Usage
─────
    py -3.11 -m tracker.reconcile          # resolve outcomes + fetch actuals + report
    py -3.11 -m tracker.reconcile --report # report only (no API calls)
    py -3.11 -m tracker.reconcile --actuals # fetch actual temps only, then report

What it does
────────────
1. Reads paper_trades.jsonl
2. Queries the Polymarket CLOB for market resolution (won/lost)
3. Fetches the actual historical max temperature from Open-Meteo for each
   resolved trade (so we can compare our forecast vs reality)
4. Saves updates back to paper_trades.jsonl
5. Prints a rich performance report with breakdowns by:
   - Side (YES/NO)
   - Edge bucket
   - Forecast method (ensemble vs regular_fallback)
   - Forecast horizon (days)
   - Direction (above/below/between/exact_c)
   - City (top 15)
   - Spread bucket (liquidity proxy)
   - Calibration: our_prob vs actual win rate
"""
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

import sys

import httpx
from loguru import logger

# Ensure the project root is on the path so weather.cities is importable
# regardless of which directory the script is invoked from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

LOG_FILE    = Path("paper_trades.jsonl")
CLOB_BASE   = "https://clob.polymarket.com"
WEATHER_API = "https://archive-api.open-meteo.com/v1/archive"
DELAY       = 0.3   # seconds between API calls


# ── I/O ───────────────────────────────────────────────────────────────────────

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


def _save(entries: list[dict]) -> None:
    with LOG_FILE.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


# ── Outcome resolution ────────────────────────────────────────────────────────

def _check_resolution(client: httpx.Client, condition_id: str) -> Optional[tuple[str, bool]]:
    """Return (winning_outcome, True) if resolved, else None."""
    try:
        resp = client.get(f"{CLOB_BASE}/markets/{condition_id}")
        resp.raise_for_status()
    except httpx.HTTPError:
        return None

    for t in resp.json().get("tokens", []):
        if t.get("winner") is True:
            return t.get("outcome", ""), True
    return None


def update_outcomes(entries: list[dict]) -> tuple[list[dict], int]:
    """Fetch resolution for all unresolved entries."""
    pending = [
        e for e in entries
        if e.get("won") is None
        and e.get("fill_status") in ("dry_run", "filled", None)
    ]
    if not pending:
        logger.info("No unresolved entries to check.")
        return entries, 0

    logger.info(f"Checking {len(pending)} unresolved entries against CLOB...")
    n = 0
    with httpx.Client(timeout=15) as client:
        for entry in pending:
            cid = entry.get("condition_id", "")
            if not cid:
                continue
            result = _check_resolution(client, cid)
            if result is None:
                continue
            winning_outcome, _ = result
            entry["outcome"] = winning_outcome
            entry["won"]     = (winning_outcome.lower() == entry["our_side"].lower())
            n += 1
            logger.debug(
                f"{'WIN' if entry['won'] else 'LOSS'}  {entry['question'][:55]}"
            )
            time.sleep(DELAY)
    return entries, n


# ── Actual temperature fetcher ────────────────────────────────────────────────

def _fetch_actual_temp(
    client: httpx.Client,
    city: str,
    target_date: str,
    unit: str,
) -> Optional[float]:
    """
    Fetch the observed daily max temperature for city on target_date
    from the Open-Meteo historical archive API.
    Returns the temperature or None.
    """
    coords = _get_coordinates(city)
    if coords is None:
        return None
    lat, lon   = coords
    temp_unit  = "fahrenheit" if unit == "F" else "celsius"
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": target_date, "end_date": target_date,
        "daily": "temperature_2m_max",
        "temperature_unit": temp_unit,
    }
    try:
        resp = client.get(WEATHER_API, params=params, timeout=15)
        resp.raise_for_status()
        temps = resp.json().get("daily", {}).get("temperature_2m_max", [])
        return float(temps[0]) if temps else None
    except Exception:
        return None


from weather.cities import get_coordinates as _get_coordinates  # noqa: E402


def update_actual_temps(entries: list[dict]) -> tuple[list[dict], int]:
    """
    Fetch actual observed max temperatures for resolved entries that don't
    have actual_temp yet.  Requires the market to have already resolved.
    """
    targets = [
        e for e in entries
        if e.get("won") is not None
        and e.get("actual_temp") is None
        and e.get("city")
        and e.get("target_date")
        and e.get("unit")
    ]
    if not targets:
        logger.info("No entries need actual temperature lookup.")
        return entries, 0

    logger.info(f"Fetching actual temperatures for {len(targets)} resolved trades...")
    n = 0
    with httpx.Client(timeout=15) as client:
        for entry in targets:
            temp = _fetch_actual_temp(
                client,
                city=entry["city"],
                target_date=entry["target_date"],
                unit=entry.get("unit", "C"),
            )
            if temp is not None:
                entry["actual_temp"] = temp
                n += 1
            time.sleep(DELAY)
    return entries, n


# ── Report helpers ────────────────────────────────────────────────────────────

def _edge_bucket(edge: float) -> str:
    if edge < 0.10: return " 5-10%"
    if edge < 0.20: return "10-20%"
    if edge < 0.30: return "20-30%"
    return "  >30%"

def _horizon_bucket(h: int) -> str:
    if h <= 1: return "1d"
    if h <= 2: return "2d"
    if h <= 3: return "3d"
    if h <= 5: return "4-5d"
    return " 6d+"

def _spread_bucket(s: float) -> str:
    if s == 0:    return "unknown"
    if s < 0.03:  return "<3c  (tight)"
    if s < 0.06:  return "3-6c (ok)"
    if s < 0.10:  return "6-10c (wide)"
    return ">10c (very wide)"

def _calibration_buckets(entries: list[dict]) -> list[tuple[str, int, int, float]]:
    """Return (label, n, wins, our_avg_prob) rows sorted by our_prob."""
    bkts: dict[str, list] = defaultdict(list)
    for e in entries:
        prob = e.get("our_prob", 0)
        b = f"{int(prob*10)*10:3d}-{int(prob*10)*10+10:3d}%"
        bkts[b].append(e)
    rows = []
    for b in sorted(bkts):
        es   = bkts[b]
        n    = len(es)
        wins = sum(1 for e in es if e.get("won"))
        avg  = sum(e.get("our_prob", 0) for e in es) / n
        rows.append((b, n, wins, avg))
    return rows


def _breakdown(entries: list[dict], key_fn, title: str, top_n: int = 20) -> None:
    W   = 70
    sep = "-" * W
    print(f"\n  {title}")
    print(f"  {sep}")
    print(f"  {'Category':<24}  {'N':>4}  {'Wins':>5}  {'WinRate':>8}  {'AvgEdge':>8}  {'SimPnL':>8}")
    by: dict[str, list] = defaultdict(list)
    for e in entries:
        by[key_fn(e)].append(e)
    rows = sorted(by.items(), key=lambda x: -len(x[1]))[:top_n]
    for label, es in rows:
        n    = len(es)
        wins = sum(1 for e in es if e.get("won"))
        wr   = wins / n
        avg_edge = sum(e.get("edge", 0) for e in es) / n
        pnl  = sum(
            e["bet_usdc"] * (1 - e["market_price"]) / max(e["market_price"], 0.001)
            if e.get("won") else -e["bet_usdc"]
            for e in es
        )
        print(f"  {label:<24}  {n:>4}  {wins:>5}  {wr:>8.1%}  {avg_edge:>8.3f}  ${pnl:>+7.2f}")


# ── Main report ───────────────────────────────────────────────────────────────

def _print_section(title: str, resolved: list[dict], pending: list[dict], sep: str, dsep: str) -> None:
    """Print a self-contained report section for a cohort of trades."""
    wins        = sum(1 for e in resolved if e["won"])
    losses      = len(resolved) - wins
    win_rate    = wins / len(resolved) if resolved else 0
    total_staked = sum(e["bet_usdc"] for e in resolved)
    pnl = sum(
        e["bet_usdc"] * (1 - e["market_price"]) / max(e["market_price"], 0.001)
        if e["won"] else -e["bet_usdc"]
        for e in resolved
    )
    roi = pnl / total_staked if total_staked else 0

    print(f"\n{dsep}")
    print(f"  {title}")
    print(dsep)
    print(f"  Resolved : {len(resolved)}    Pending : {len(pending)}")
    if not resolved:
        print("  No resolved trades yet.")
        return
    print(f"  Wins     : {wins}    Losses  : {losses}    Win rate : {win_rate:.1%}")
    print(f"  Staked   : ${total_staked:.2f}")
    print(f"  P&L      : ${pnl:+.2f}  (ROI {roi:+.1%})")

    # Open positions
    if pending:
        print(f"\n  OPEN POSITIONS ({len(pending)})")
        for e in sorted(pending, key=lambda x: x.get("target_date", "")):
            print(f"    {e.get('target_date','?')[:10]}  ${e['bet_usdc']:.2f}  {e.get('our_side','?')}  {e.get('question','')[:60]}")

    # Breakdowns — only meaningful with enough data
    if len(resolved) >= 10:
        _breakdown(resolved, lambda e: e.get("direction", "unknown"), "BY DIRECTION")
        _breakdown(resolved, lambda e: e.get("city", "unknown"), "BY CITY (top 10)", top_n=10)
        _breakdown(resolved, lambda e: _edge_bucket(e.get("edge", 0)), "BY EDGE SIZE")

    # Worst losses
    worst = sorted([e for e in resolved if not e["won"]], key=lambda x: -x["bet_usdc"])[:5]
    if worst:
        print(f"\n  WORST LOSSES")
        for e in worst:
            print(f"    ${e['bet_usdc']:>5.2f}  {e.get('our_side','?')}  edge={e.get('edge',0):.3f}  {e.get('question','')[:60]}")


def print_report(entries: list[dict]) -> None:
    W    = 70
    dsep = "=" * W
    sep  = "-" * W

    all_tracked = [e for e in entries if e.get("fill_status") in ("dry_run", "filled", None)]

    live_all  = [e for e in all_tracked if e.get("fill_status") == "filled"]
    paper_all = [e for e in all_tracked if e.get("fill_status") != "filled"]

    live_res  = [e for e in live_all  if e.get("won") is not None]
    live_pen  = [e for e in live_all  if e.get("won") is None]
    paper_res = [e for e in paper_all if e.get("won") is not None]
    paper_pen = [e for e in paper_all if e.get("won") is None]

    # ── LIVE TRADES ───────────────────────────────────────────────────────────
    _print_section("LIVE TRADES (real money)", live_res, live_pen, sep, dsep)

    # ── PAPER TRADES (historical reference only) ──────────────────────────────
    print(f"\n{dsep}")
    print(f"  PAPER / SIMULATION TRADES  (historical reference — not real money)")
    print(dsep)
    print(f"  Resolved : {len(paper_res)}    Pending : {len(paper_pen)}")
    if paper_res:
        pw   = sum(1 for e in paper_res if e["won"])
        ppnl = sum(
            e["bet_usdc"] * (1 - e["market_price"]) / max(e["market_price"], 0.001)
            if e["won"] else -e["bet_usdc"]
            for e in paper_res
        )
        pstk = sum(e["bet_usdc"] for e in paper_res)
        print(f"  Wins     : {pw}    Losses  : {len(paper_res)-pw}    Win rate : {pw/len(paper_res):.1%}")
        print(f"  Sim P&L  : ${ppnl:+.2f}  (ROI {ppnl/pstk:+.1%})  [simulated, not real]")

    # ── Forecast calibration (uses all resolved data) ─────────────────────────
    all_res = live_res + paper_res
    actuals = [e for e in all_res if e.get("actual_temp") is not None]
    if actuals:
        print(f"\n  {sep}")
        print(f"  FORECAST CALIBRATION (n={len(actuals)} with actual temps)")
        print(f"  {'Market':<40}  {'Pred':>6}  {'Actual':>7}  {'Error':>7}")
        for e in sorted(actuals, key=lambda x: x.get("target_date", ""))[-10:]:
            pred = e.get("forecast_mean", 0)
            act  = e["actual_temp"]
            u    = e.get("unit", "")
            print(f"  {e['question'][:40]:<40}  {pred:>5.1f}{u}  {act:>6.1f}{u}  {pred-act:>+6.1f}{u}")

    print(f"\n{dsep}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    report_only  = "--report"  in sys.argv
    actuals_only = "--actuals" in sys.argv

    entries = _load()
    if not entries:
        print("paper_trades.jsonl is empty. Run the bot first.")
        return

    if not report_only:
        entries, n_outcomes = update_outcomes(entries)
        if n_outcomes:
            logger.info(f"Resolved {n_outcomes} new outcome(s)")
            _save(entries)  # save immediately — don't risk losing outcomes if actuals fetch fails
            logger.info("Saved outcome updates to paper_trades.jsonl")

        try:
            entries, n_actuals = update_actual_temps(entries)
            if n_actuals:
                logger.info(f"Fetched {n_actuals} actual temperature(s)")
                _save(entries)
                logger.info("Saved actual temperature updates to paper_trades.jsonl")
        except Exception as exc:
            logger.warning(f"Actual temps fetch failed (non-fatal): {exc}")

    print_report(entries)


if __name__ == "__main__":
    main()
