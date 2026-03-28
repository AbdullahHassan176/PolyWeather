from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from loguru import logger

from config import cfg
from weather.cities import HIGH_VARIABILITY_CITIES

# Minimum edge multiplier by forecast horizon (days out).
# Longer horizons = more uncertainty = need more edge to justify the bet.
_HORIZON_EDGE_SCALE = [
    (1, 1.0),   # same-day / next-day: standard MIN_EDGE
    (2, 1.3),   # 2 days out: 30% higher bar
    (3, 1.6),   # 3 days out: 60% higher bar
    (5, 2.0),   # 4-5 days: double the bar
    (999, 2.5), # 6+ days: very conservative
]


@dataclass
class TradeSignal:
    # ── Core identity ────────────────────────────────────────────────────────
    market_question: str
    condition_id: str
    side: str           # "YES" or "NO"
    token_id: str
    price: float        # market price we're buying at (from live CLOB midpoint)
    our_prob: float     # our ensemble probability for the bet side
    edge: float         # our_prob - market_price
    bet_usdc: float     # dollar amount to wager (Kelly-sized)

    # ── Market context ───────────────────────────────────────────────────────
    city: str = ""
    target_date: Optional[date] = None
    forecast_horizon_days: int = 0      # days from today to target_date
    unit: str = ""                       # "F" or "C"
    direction: str = ""                  # above / below / between / exact_c
    threshold: float = 0.0
    threshold2: Optional[float] = None  # upper bound for "between"

    # ── Forecast metadata ─────────────────────────────────────────────────────
    forecast_method: str = ""           # "ensemble" | "regular_fallback"
    forecast_mean: float = 0.0          # mean of ensemble member temps
    forecast_std: float = 0.0           # std-dev of ensemble member temps
    n_members: int = 0                  # number of members used

    # ── Live order-book snapshot (filled in by prices.py enrichment) ─────────
    bid: float = 0.0                    # best bid for YES token at signal time
    ask: float = 0.0                    # best ask for YES token at signal time
    spread: float = 0.0                 # ask - bid
    volume_24h: float = 0.0             # 24h USDC volume from Gamma API

    # ── Execution results (filled in by executor / main.py) ──────────────────
    live_price: float = 0.0             # price computed by create_market_order
    slippage: float = 0.0               # live_price - signal.price


def analyze(
    market: dict,
    parsed: dict,
    our_prob: float,
    forecast_meta: Optional[dict] = None,
) -> Optional["TradeSignal"]:
    """
    Given a normalised market dict, a parsed question, our ensemble
    probability for the YES outcome, and optional forecast metadata,
    return a TradeSignal if there is sufficient edge; otherwise None.

    Kelly formula for binary prediction markets:
      f* = edge / (1 - price)          [fraction of max position]
    We then scale by kelly_fraction (e.g. 0.25) and cap at max_trade_usdc.
    """
    yes_price = market["yes_price"]
    no_price  = market["no_price"]

    edge_yes = our_prob - yes_price
    edge_no  = (1.0 - our_prob) - no_price

    best_edge = max(edge_yes, edge_no)

    # Scale the required edge by forecast horizon and city variability
    tdate = parsed.get("date")
    horizon_days = (tdate - date.today()).days if tdate else 0
    scale = next(s for h, s in _HORIZON_EDGE_SCALE if horizon_days <= h)
    required_edge = cfg.min_edge * scale

    # Synthetic fallback (regular_fallback) has wider σ → probabilities are less
    # extreme → edges are smaller. Lower the bar by 40% when fallback is used so
    # we still find signals. Calibration showed 92.3% WR on 5-10% edge trades.
    fm = forecast_meta or {}
    if fm.get("method") == "regular_fallback":
        required_edge *= 0.60

    city = parsed.get("city", "")
    if city in HIGH_VARIABILITY_CITIES:
        required_edge += cfg.high_variability_extra_edge

    if best_edge < required_edge:
        logger.debug(
            f"No edge ({best_edge:.3f} < {required_edge:.3f} req) on: {market['question'][:60]}"
        )
        return None

    if best_edge > cfg.max_edge:
        logger.debug(
            f"Edge too high ({best_edge:.3f} > {cfg.max_edge:.3f} cap) — likely mispriced: {market['question'][:60]}"
        )
        return None

    if edge_yes >= edge_no:
        side     = "YES"
        price    = yes_price
        token_id = market["yes_token_id"]
        edge     = edge_yes
    else:
        side     = "NO"
        price    = no_price
        token_id = market["no_token_id"]
        edge     = edge_no

    # Kelly fraction: how much of max capital to risk
    kelly_f  = edge / (1.0 - price) if price < 1.0 else 0.0
    kelly_f  = max(0.0, min(kelly_f, 1.0))
    bet_usdc = kelly_f * cfg.kelly_fraction * cfg.max_trade_usdc
    bet_usdc = min(bet_usdc, cfg.max_trade_usdc)
    bet_usdc = round(bet_usdc, 2)

    if bet_usdc < 5.0:
        logger.debug(
            f"Bet too small (${bet_usdc:.2f} < $5 CLOB min) for: {market['question'][:60]}"
        )
        return None

    # Hard floor: never buy a token priced below 5¢ — the market is near-certain
    # and our ensemble probability estimates are unreliable that close to 0/1.
    if price < 0.05:
        logger.debug(f"Price too low ({price:.3f}) — near-certain market, skipping")
        return None

    logger.info(
        f"Signal [{side}] {market['question'][:65]}\n"
        f"  market={price:.3f}  ours={our_prob:.3f}  edge={edge:+.3f}  bet=${bet_usdc:.2f}"
    )

    from datetime import date as date_type
    today = date_type.today()
    tdate = parsed.get("date")
    horizon = (tdate - today).days if tdate else 0

    fm = forecast_meta or {}

    return TradeSignal(
        # core
        market_question=market["question"],
        condition_id=market["conditionId"],
        side=side,
        token_id=token_id,
        price=price,
        our_prob=our_prob,
        edge=edge,
        bet_usdc=bet_usdc,
        # market context
        city=parsed.get("city", ""),
        target_date=tdate,
        forecast_horizon_days=horizon,
        unit=parsed.get("unit", ""),
        direction=parsed.get("direction", ""),
        threshold=parsed.get("threshold", 0.0),
        threshold2=parsed.get("threshold2"),
        # forecast metadata
        forecast_method=fm.get("method", ""),
        forecast_mean=fm.get("forecast_mean", 0.0),
        forecast_std=fm.get("forecast_std", 0.0),
        n_members=fm.get("n_members", 0),
        # market order-book (set later by main.py after fetching spread)
        bid=market.get("bid", 0.0),
        ask=market.get("ask", 0.0),
        spread=market.get("spread", 0.0),
        volume_24h=market.get("volume_24h", 0.0),
    )
