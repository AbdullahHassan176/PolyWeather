"""
Live CLOB price enrichment.

Phase 1 — midpoints (all markets, one batch call)
──────────────────────────────────────────────────
Replaces stale Gamma outcomePrices with real-time CLOB midpoints.
Markets with YES < 5% or YES > 95% are dropped (illiquid minority side).

Phase 2 — bid/ask spread (signal tokens only, one call per token)
──────────────────────────────────────────────────────────────────
Called from main.py after edge filtering, only for markets that passed
analysis.  Adds "bid", "ask", "spread" to the market dict and the signal.

Why midpoints?
──────────────
Gamma prices can lag minutes-to-hours.  Using CLOB midpoints for edge
calculation and bid/ask for execution-quality diagnostics gives both accurate
signals and rich data for back-testing.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from py_clob_client.client import ClobClient

_WARN_LAG   = 0.10   # log warning when Gamma lags CLOB by ≥ 10 cents
_MIN_YES    = 0.05   # drop markets where YES < 5% (illiquid NO side)
_MAX_YES    = 0.95   # drop markets where YES > 95% (illiquid YES side)


# ── Phase 1: midpoint enrichment ──────────────────────────────────────────────

def enrich_with_live_prices(
    markets: list[dict],
    clob_client: "ClobClient",
    chunk_size: int = 200,
) -> list[dict]:
    """
    Fetch real-time CLOB mid-market prices for every YES token.
    Updates yes_price / no_price in-place and drops illiquid/resolved markets.
    """
    from py_clob_client.clob_types import BookParams

    token_to_market: dict[str, dict] = {
        m["yes_token_id"]: m for m in markets if m.get("yes_token_id")
    }
    all_token_ids = list(token_to_market.keys())
    if not all_token_ids:
        return markets

    midpoints: dict[str, float] = {}
    for i in range(0, len(all_token_ids), chunk_size):
        batch = all_token_ids[i : i + chunk_size]
        try:
            resp = clob_client.get_midpoints(
                params=[BookParams(token_id=t) for t in batch]
            )
            if isinstance(resp, dict):
                for tid, price_str in resp.items():
                    try:
                        midpoints[tid] = float(price_str)
                    except (TypeError, ValueError):
                        pass
        except Exception as exc:
            logger.warning(f"CLOB midpoints fetch failed (chunk {i//chunk_size}): {exc}")

    if not midpoints:
        logger.warning("No CLOB midpoints — using stale Gamma prices")
        return markets

    enriched: list[dict] = []
    stale_count = 0

    for m in markets:
        tid = m.get("yes_token_id", "")
        if tid not in midpoints:
            continue

        live_yes = midpoints[tid]

        if live_yes < _MIN_YES or live_yes > _MAX_YES:
            continue

        lag = abs(live_yes - m["yes_price"])
        if lag >= _WARN_LAG:
            stale_count += 1
            logger.debug(
                f"Stale Gamma: {m['question'][:55]}  "
                f"gamma={m['yes_price']:.3f} → clob={live_yes:.3f}  lag={lag:.3f}"
            )

        m["yes_price"] = live_yes
        m["no_price"]  = round(1.0 - live_yes, 4)
        enriched.append(m)

    dropped = len(markets) - len(enriched)
    logger.info(
        f"CLOB prices: {len(enriched)}/{len(markets)} markets enriched  "
        f"({dropped} dropped, {stale_count} had stale Gamma prices >= {_WARN_LAG:.0%})"
    )
    return enriched


# ── Phase 2: bid/ask spread for a single token ────────────────────────────────

def fetch_spread(token_id: str, clob_client: "ClobClient") -> dict:
    """
    Fetch best bid and ask for *token_id*.
    Returns {"bid": float, "ask": float, "spread": float}.
    Returns zeros on failure.
    """
    empty = {"bid": 0.0, "ask": 0.0, "spread": 0.0}
    try:
        book = clob_client.get_order_book(token_id)
        if book is None:
            return empty

        best_bid = float(book.bids[0].price) if book.bids else 0.0
        best_ask = float(book.asks[0].price) if book.asks else 0.0

        if best_bid <= 0 or best_ask <= 0:
            return empty

        return {
            "bid":    round(best_bid, 4),
            "ask":    round(best_ask, 4),
            "spread": round(best_ask - best_bid, 4),
        }
    except Exception as exc:
        logger.debug(f"Spread fetch failed for {token_id[:16]}…: {exc}")
        return empty
