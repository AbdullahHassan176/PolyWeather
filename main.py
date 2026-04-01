#!/usr/bin/env python3
"""
PolyWeather Bot

Scans Polymarket weather markets every SCAN_INTERVAL seconds.
For each market it:
  1. Parses the question to extract city / date / threshold / direction.
  2. Fetches an ECMWF ensemble forecast (or regular-API fallback) via Open-Meteo.
  3. Computes P(outcome) from the ensemble distribution.
  4. Replaces stale Gamma prices with live CLOB midpoints.
  5. Compares to the live price; if edge >= MIN_EDGE, sizes a bet using
     quarter-Kelly and places it (or logs it in dry-run mode).
  6. Records every signal with full metadata to paper_trades.jsonl.

Usage:
  py -3.11 main.py
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import schedule
from loguru import logger

_PAPER_MODE = "--paper" in sys.argv   # experimental paper-trading with looser filters

if _PAPER_MODE:
    os.environ["DRY_RUN"]    = "true"   # never place real orders in paper mode
    os.environ["PAPER_MODE"] = "1"      # signal analyzer to skip variability penalty

_LOCKFILE       = Path("polyweather_paper.pid" if _PAPER_MODE else "polyweather.pid")
_LOG_FILE       = "polyweather_paper.log"      if _PAPER_MODE else "polyweather.log"
_TRADES_FILE    = Path("paper_trades_experimental.jsonl" if _PAPER_MODE else "paper_trades.jsonl")


def _acquire_lock() -> bool:
    """Return True if this process acquired the singleton lock, False if another instance is running."""
    if _LOCKFILE.exists():
        try:
            existing_pid = int(_LOCKFILE.read_text().strip())
            # Check if that PID is still alive
            try:
                os.kill(existing_pid, 0)
                # Process exists — another instance is running
                print(f"[polyweather] Another instance already running (PID {existing_pid}). Exiting.", flush=True)
                return False
            except (OSError, SystemError):
                # Stale lock file — previous process died without cleanup
                # (SystemError can occur on Windows when signal 0 is unsupported)
                pass
        except (ValueError, IOError):
            pass
    _LOCKFILE.write_text(str(os.getpid()))
    return True


def _release_lock():
    try:
        _LOCKFILE.unlink(missing_ok=True)
    except OSError:
        pass

from config import cfg
from polymarket.markets import PolymarketClient
from polymarket.parser import parse_question
from polymarket.prices import enrich_with_live_prices, fetch_spread
from weather.client import WeatherClient
from strategy.analyzer import analyze
from trader.executor import build_clob_client, execute_trade, get_usdc_balance
from tracker.log import record_signal


def _configure_logging():
    logger.remove()
    logger.add(
        sys.stderr,
        level=cfg.log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        colorize=True,
    )
    logger.add(
        _LOG_FILE,
        level="DEBUG",
        rotation="10 MB",
        retention="14 days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
    )


def _load_open_condition_ids() -> set[str]:
    """
    Return conditionIds that have been signalled but not yet resolved.
    Used to avoid re-entering positions we already hold.
    """
    log_path = _TRADES_FILE
    if not log_path.exists():
        return set()
    open_ids: set[str] = set()
    with log_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                # Block re-entry based on fill_status:
                #   "filled"        — position held, don't double up
                #   "delayed"       — order queued on CLOB, WILL fill on-chain;
                #                     retrying creates duplicates (Taipei root cause)
                #   "error"         — order may have reached CLOB despite exception;
                #                     don't retry to avoid unknown duplicate positions
                # Allow retry for:
                #   "slippage_abort"  — order never sent, price may recover
                #   "fok_unmatched"   — order rejected by book, safe to retry
                #   "dry_run"         — paper trade only, no real position
                status = entry.get("fill_status")
                if entry.get("won") is None and status in ("filled", "delayed", "error", "dry_run"):
                    open_ids.add(entry.get("condition_id", ""))
            except json.JSONDecodeError:
                pass
    return open_ids


_weather_client: WeatherClient | None = None


def _get_weather_client() -> WeatherClient:
    """Return a module-level WeatherClient singleton (cache persists across scans)."""
    global _weather_client
    if _weather_client is None:
        _weather_client = WeatherClient()
    return _weather_client


_LIVE_TRADING_THRESHOLD = 30  # clean resolved trades needed before going live


def _count_clean_resolved() -> int:
    """Count resolved trades matching the current clean strategy criteria."""
    log_path = Path("paper_trades.jsonl")
    if not log_path.exists():
        return 0
    count = 0
    with log_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if (e.get("direction") == "between"
                        and e.get("our_side") == "NO"
                        and e.get("forecast_horizon_days", 0) >= 2
                        and e.get("edge", 0) >= 0.15
                        and e.get("market_price", 0) >= 0.75
                        and e.get("won") is not None):
                    count += 1
            except json.JSONDecodeError:
                pass
    return count


def run_scan():
    logger.info("=== Scan started ===")

    # ── Build shared CLOB client (cached after first call) ────────────────────
    clob = build_clob_client()

    # ── Live-readiness check ──────────────────────────────────────────────────
    if cfg.dry_run:
        clean_n = _count_clean_resolved()
        if clean_n >= _LIVE_TRADING_THRESHOLD:
            logger.warning(
                f"*** READY FOR LIVE TRADING: {clean_n} clean resolved trades "
                f"(>= {_LIVE_TRADING_THRESHOLD}). "
                f"Set DRY_RUN=false and MAX_TRADE_USDC=40 in .env to go live. ***"
            )
        else:
            logger.info(f"Paper trading: {clean_n}/{_LIVE_TRADING_THRESHOLD} clean resolved trades")

    # ── Budget guard ──────────────────────────────────────────────────────────
    if not cfg.dry_run:
        budget = get_usdc_balance()
        if budget < 5.0:
            logger.error(f"USDC balance ${budget:.2f} below minimum — aborting scan")
            return
        logger.info(f"Available budget: ${budget:.2f} USDC")
    else:
        budget = cfg.max_trade_usdc * 1_000   # unlimited in dry-run

    # ── Fetch markets ─────────────────────────────────────────────────────────
    pm = PolymarketClient()
    wc = _get_weather_client()

    try:
        markets = pm.get_weather_markets()
    except Exception as exc:
        logger.error(f"Failed to fetch markets: {exc}")
        return
    finally:
        pm.close()

    if not markets:
        logger.info("No tradeable markets found this scan")
        return

    # ── Enrich with live CLOB midpoints ───────────────────────────────────────
    markets = enrich_with_live_prices(markets, clob)
    if not markets:
        logger.warning("No markets survived CLOB price enrichment")
        return

    # ── Deduplication ─────────────────────────────────────────────────────────
    open_cids       = _load_open_condition_ids()
    traded_this_scan: set[str] = set()
    today_utc       = datetime.now(timezone.utc).date()

    signals = 0

    for market in markets:
        cid = market["conditionId"]

        if cid in open_cids:
            logger.debug(f"Already held, skipping: {market['question'][:55]}")
            continue
        if cid in traded_this_scan:
            continue

        parsed = parse_question(market["question"])
        if parsed is None:
            logger.debug(f"Unparseable: {market['question'][:80]}")
            continue

        # Skip markets whose target date is today or earlier — outcome already
        # known from observations, which our forecast model doesn't use
        if parsed.get("date") and parsed["date"] <= today_utc:
            logger.debug(f"Resolves today/past, skipping: {market['question'][:65]}")
            continue

        # Skip 1-day horizon unless in experimental paper mode testing 1d between trades.
        # Live: backtesting shows 1d trades are consistently unprofitable (-$206 P&L).
        # Paper: testing whether 1d "between" specifically is viable (ECMWF more accurate at 1d).
        _min_horizon = 1 if _PAPER_MODE else 2
        if parsed.get("date") and (parsed["date"] - today_utc).days < _min_horizon:
            logger.debug(f"Horizon too short (<{_min_horizon}d), skipping: {market['question'][:65]}")
            continue

        # Skip exact-temperature markets ("be 17°C on ...") — the ±0.5°C band
        # is too narrow for ECMWF to be reliable; all backtested losses came from here
        if parsed.get("direction") == "exact_c":
            logger.debug(f"Skipping exact_c market: {market['question'][:65]}")
            continue

        # Only trade "between" direction — the only profitable segment in backtesting.
        # above/below thresholds (-$45/-$21) and exact_c (-$4) all lose money.
        if parsed.get("direction") != "between":
            logger.debug(f"Skipping non-between direction ({parsed.get('direction')}): {market['question'][:55]}")
            continue

        # ── Ensemble probability ──────────────────────────────────────────────
        city      = parsed["city"]
        tdate     = parsed["date"]
        unit      = parsed["unit"]
        direction = parsed["direction"]

        if direction == "exact_c":
            our_prob = wc.get_probability(
                city=city, target_date=tdate,
                threshold=parsed["threshold"] - 0.5,
                threshold2=parsed["threshold"] + 0.5,
                direction="between", unit=unit,
            )
        else:
            our_prob = wc.get_probability(
                city=city, target_date=tdate,
                threshold=parsed["threshold"],
                threshold2=parsed.get("threshold2"),
                direction=direction, unit=unit,
            )

        if our_prob is None:
            logger.debug(f"No forecast for {city}: {market['question'][:60]}")
            continue

        # Forecast metadata for logging
        forecast_meta = wc.get_forecast_meta(city, tdate, unit)

        # ── Edge & sizing ─────────────────────────────────────────────────────
        signal = analyze(market, parsed, our_prob, forecast_meta=forecast_meta)
        if signal is None:
            continue

        # Only place NO bets — YES bets had 20% WR in backtesting
        if signal.side == "YES":
            logger.debug(f"Skipping YES signal: {market['question'][:60]}")
            continue

        # ── Bid/ask spread (one order-book call per signal) ───────────────────
        # Always fetch the YES token's order book — it has the real liquidity.
        # For NO bets, the YES spread mirrors the NO spread (they sum to 1).
        yes_token_id = market["yes_token_id"]
        spread_data = fetch_spread(yes_token_id, clob)
        signal.bid    = spread_data["bid"]
        signal.ask    = spread_data["ask"]
        signal.spread = spread_data["spread"]
        signal.volume_24h = float(market.get("volume_24hr", 0) or 0)

        # ── Budget check ──────────────────────────────────────────────────────
        if signal.bet_usdc > budget:
            logger.warning(
                f"Budget exhausted (${budget:.2f} left, need ${signal.bet_usdc:.2f})"
                f" — stopping scan"
            )
            break

        signals += 1
        traded_this_scan.add(cid)

        # ── Execute (or dry-run) ──────────────────────────────────────────────
        filled, fill_status, actual_spent = execute_trade(signal, clob_client=clob)

        # Record with all metadata (use experimental file in paper mode)
        record_signal(signal, fill_status=fill_status, actual_spent=actual_spent,
                      log_file=_TRADES_FILE)

        if filled and not cfg.dry_run:
            budget -= (actual_spent or signal.bet_usdc)

    logger.info(
        f"=== Scan complete: {signals} signal(s) from {len(markets)} markets "
        f"| budget remaining: ${budget:.2f} ==="
    )


def main():
    if not _acquire_lock():
        sys.exit(0)

    try:
        _run()
    finally:
        _release_lock()


def _run():
    _configure_logging()

    logger.info("PolyWeather Bot starting")
    logger.info(
        f"  dry_run={cfg.dry_run}  min_edge={cfg.min_edge}  "
        f"max_trade=${cfg.max_trade_usdc}  kelly={cfg.kelly_fraction}  "
        f"interval={cfg.scan_interval}s"
    )

    if cfg.dry_run:
        logger.warning("DRY RUN mode — no real orders will be placed")

    run_scan()

    schedule.every(cfg.scan_interval).seconds.do(run_scan)
    logger.info(f"Next scan in {cfg.scan_interval}s (Ctrl+C to stop)")

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")


if __name__ == "__main__":
    main()
