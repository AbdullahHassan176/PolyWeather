"""
Signal logger — appends one JSON record per signal to paper_trades.jsonl.

Every field is recorded so the dataset can be used for:
  • Back-testing / calibration
  • Forecast-method comparison (ensemble vs regular_fallback)
  • Liquidity analysis (spread → execution quality)
  • Kelly-sizing tuning
  • City / direction / horizon performance breakdown

fill_status values
──────────────────
  "filled"    live order confirmed MATCHED
  "unfilled"  live order sent but UNMATCHED / delayed / slippage-aborted
  "dry_run"   DRY_RUN=true, no real order sent
  None        legacy records without this field
"""
import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from strategy.analyzer import TradeSignal

LOG_FILE = Path("paper_trades.jsonl")


def record_signal(
    signal: "TradeSignal",
    fill_status: Optional[str] = None,
    actual_spent: Optional[float] = None,
    log_file: Optional[Path] = None,
) -> None:
    """
    Append one fully-detailed signal record to paper_trades.jsonl.

    Parameters
    ----------
    signal       : TradeSignal from analyzer (carries all metadata)
    fill_status  : "filled" | "unfilled" | "dry_run" | None
    actual_spent : USDC actually spent (from CLOB takingAmount), if any
    """
    entry = {
        # ── Timestamp ────────────────────────────────────────────────────────
        "ts": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),

        # ── Market identity ───────────────────────────────────────────────────
        "condition_id":   signal.condition_id,
        "question":       signal.market_question,
        "city":           signal.city,
        "target_date":    signal.target_date.isoformat() if signal.target_date else "",
        "direction":      signal.direction,
        "threshold":      signal.threshold,
        "threshold2":     signal.threshold2,
        "unit":           signal.unit,

        # ── Signal core ────────────────────────────────────────────────────────
        "our_side":       signal.side,
        "our_prob":       round(signal.our_prob, 4),
        "market_price":   round(signal.price, 4),
        "edge":           round(signal.edge, 4),
        "bet_usdc":       signal.bet_usdc,
        "forecast_horizon_days": signal.forecast_horizon_days,

        # ── Forecast metadata ─────────────────────────────────────────────────
        "forecast_method": signal.forecast_method,
        "forecast_mean":   signal.forecast_mean,
        "forecast_std":    signal.forecast_std,
        "n_members":       signal.n_members,

        # ── Market liquidity snapshot ─────────────────────────────────────────
        "bid":        signal.bid,
        "ask":        signal.ask,
        "spread":     signal.spread,
        "volume_24h": signal.volume_24h,

        # ── Execution ─────────────────────────────────────────────────────────
        "fill_status":  fill_status,
        "actual_spent": actual_spent,
        "live_price":   round(signal.live_price, 4) if signal.live_price else None,
        "slippage":     round(signal.slippage, 4)   if signal.slippage   else None,

        # ── Resolution (filled in by reconcile / watch --pnl) ─────────────────
        "outcome":      None,
        "won":          None,
        "actual_temp":  None,   # filled by actuals fetcher after resolution
    }
    target = log_file if log_file is not None else LOG_FILE
    with target.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
