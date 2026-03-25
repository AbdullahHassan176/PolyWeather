"""
Trade executor — wraps py-clob-client to place market orders on Polymarket.

Auth flow
─────────
• If POLYMARKET_API_KEY (or legacy CLOB_API_KEY) credentials are set in .env,
  they are used directly (Level 2 auth).
• Otherwise, credentials are derived from PRIVATE_KEY on first call.

Wallet modes
────────────
• Plain EOA  (PROXY_WALLET is blank)  : signature_type=0, funder=EOA address
• Gnosis Safe (PROXY_WALLET is set)   : signature_type=2, funder=proxy wallet address

Polygon chain id = 137.
"""
from __future__ import annotations

from loguru import logger

from config import cfg
from strategy.analyzer import TradeSignal

_POLYGON_CHAIN_ID = 137

# Maximum price the order is allowed to fill above our calculated price.
# Protects against residual Gamma-price staleness after CLOB enrichment.
_MAX_SLIPPAGE = 0.05   # 5 cents per share

# Module-level client cache — built once per process, reused across scans.
_clob_client = None


def build_clob_client():
    """
    Build (or return the cached) authenticated ClobClient.
    Exported so main.py can obtain a client to pass to price enrichment.
    """
    global _clob_client
    if _clob_client is not None:
        return _clob_client

    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    kwargs = dict(
        host=cfg.clob_api,
        chain_id=_POLYGON_CHAIN_ID,
        key=cfg.private_key,
        signature_type=cfg.signature_type,
    )
    if cfg.funder_address:
        kwargs["funder"] = cfg.funder_address

    if cfg.clob_api_key and cfg.clob_secret and cfg.clob_pass_phrase:
        kwargs["creds"] = ApiCreds(
            api_key=cfg.clob_api_key,
            api_secret=cfg.clob_secret,
            api_passphrase=cfg.clob_pass_phrase,
        )
        client = ClobClient(**kwargs)
    else:
        client = ClobClient(**kwargs)
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        logger.info(f"Derived CLOB API key: {creds.api_key}")

    mode = "Gnosis Safe" if cfg.proxy_wallet else "EOA"
    logger.info(f"ClobClient ready ({mode}, sig_type={cfg.signature_type})")
    _clob_client = client
    return client


def get_usdc_balance() -> float:
    """Return available USDC balance from the proxy wallet (or EOA)."""
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
    try:
        client = build_clob_client()
        bal = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        usdc = float(bal.get("balance", 0)) / 1e6

        # CLOB returns `allowances` dict keyed by contract address.
        # Any non-zero value means spending is approved.
        allowances: dict = bal.get("allowances", {})
        has_allowance = any(int(v) > 0 for v in allowances.values() if v)
        if not has_allowance:
            logger.error(
                "USDC allowance is zero for all contracts. "
                "Approve via polymarket.com before trading live."
            )

        return usdc
    except Exception as exc:
        logger.error(f"Could not fetch USDC balance: {exc}")
        return 0.0


def _parse_fok_response(resp) -> tuple[bool, str]:
    """
    Parse the raw dict returned by ClobClient.post_order().

    Returns (filled: bool, detail: str).
    A FOK order is considered filled only when status == 'MATCHED'
    and there is no errorMsg.
    """
    if not isinstance(resp, dict):
        return False, f"unexpected response type: {type(resp)}"

    error = resp.get("errorMsg", "")
    status = resp.get("status", "")

    if error:
        return False, f"errorMsg={error!r}  status={status!r}"

    if status.upper() == "MATCHED":
        taking = resp.get("takingAmount", "?")   # USDC spent
        making = resp.get("makingAmount", "?")   # shares received
        return True, f"MATCHED: spent={taking} USDC, shares={making}"

    if status.upper() == "UNMATCHED":
        return False, "FOK not filled — no matching liquidity at this price"

    if status.lower() == "delayed":
        return False, "order queued as 'delayed' by CLOB — treated as not filled"

    # Unknown status — treat as failure to be safe
    return False, f"unknown status={status!r}"


def execute_trade(
    signal: TradeSignal, clob_client=None
) -> tuple[bool, str, float | None]:
    """
    Place a market-buy order for *signal*.

    Returns
    -------
    (filled, fill_status, actual_spent)
      filled       : True when the order was confirmed MATCHED
      fill_status  : "filled" | "unfilled" | "dry_run"
      actual_spent : USDC spent from CLOB response (None if not filled)

    clob_client: pass an existing client to avoid per-trade re-auth overhead.
    """
    if cfg.dry_run:
        logger.info(
            f"[DRY RUN] BUY {signal.side} | {signal.market_question[:70]}\n"
            f"  token_id : {signal.token_id}\n"
            f"  price    : {signal.price:.4f}   our_prob : {signal.our_prob:.4f}\n"
            f"  edge     : {signal.edge:+.4f}   bet      : ${signal.bet_usdc:.2f}"
        )
        return True, "dry_run", None

    if not cfg.has_trading_credentials():
        logger.error("PRIVATE_KEY not set — cannot trade. Check your .env file.")
        return False, "unfilled", None

    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        client = clob_client or build_clob_client()

        order_args = MarketOrderArgs(
            token_id=signal.token_id,
            amount=signal.bet_usdc,   # USDC to spend (BUY side)
            side="BUY",
            order_type=OrderType.FOK,
        )

        # create_market_order queries the live CLOB order book to compute the
        # fill price.  Check it against our calculated price before submitting.
        order = client.create_market_order(order_args)
        live_price = float(order_args.price)

        # Record live_price and slippage back onto the signal for logging
        signal.live_price = live_price
        signal.slippage   = live_price - signal.price

        if signal.slippage > _MAX_SLIPPAGE:
            logger.warning(
                f"Slippage abort: live_price={live_price:.4f} vs "
                f"signal_price={signal.price:.4f}  "
                f"(delta={signal.slippage:.4f} > {_MAX_SLIPPAGE})  "
                f"'{signal.market_question[:55]}'"
            )
            return False, "unfilled", None

        resp = client.post_order(order, OrderType.FOK)
        filled, detail = _parse_fok_response(resp)

        # Extract actual USDC spent from the CLOB response
        actual_spent: float | None = None
        if filled and isinstance(resp, dict):
            try:
                actual_spent = float(resp.get("takingAmount", signal.bet_usdc))
            except (TypeError, ValueError):
                actual_spent = signal.bet_usdc

        if filled:
            logger.info(
                f"Filled: BUY {signal.side} ${signal.bet_usdc:.2f} "
                f"on '{signal.market_question[:55]}'  {detail}"
            )
        else:
            logger.warning(
                f"Order not filled: '{signal.market_question[:55]}'  {detail}"
            )

        return filled, ("filled" if filled else "unfilled"), actual_spent

    except Exception as exc:
        logger.error(
            f"Trade execution error for '{signal.market_question[:55]}': {exc}"
        )
        return False, "unfilled", None
