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
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import schedule
from loguru import logger

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
        "polyweather.log",
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
    log_path = Path("paper_trades.jsonl")
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
                if entry.get("won") is None:
                    open_ids.add(entry.get("condition_id", ""))
            except json.JSONDecodeError:
                pass
    return open_ids


def run_scan():
    logger.info("=== Scan started ===")

    # ── Build shared CLOB client (cached after first call) ────────────────────
    clob = build_clob_client()

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
    wc = WeatherClient()

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

        # Skip exact-temperature markets ("be 17°C on ...") — the ±0.5°C band
        # is too narrow for ECMWF to be reliable; all backtested losses came from here
        if parsed.get("direction") == "exact_c":
            logger.debug(f"Skipping exact_c market: {market['question'][:65]}")
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

        # ── Bid/ask spread (one order-book call per signal) ───────────────────
        spread_data = fetch_spread(signal.token_id, clob)
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

        # Record with all metadata
        record_signal(signal, fill_status=fill_status, actual_spent=actual_spent)

        if filled and not cfg.dry_run:
            budget -= (actual_spent or signal.bet_usdc)

    wc.close()
    logger.info(
        f"=== Scan complete: {signals} signal(s) from {len(markets)} markets "
        f"| budget remaining: ${budget:.2f} ==="
    )


def main():
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
