import json
from typing import Optional

import httpx
from loguru import logger

from config import cfg


# Minimum 24-hour CLOB volume (USDC) to bother with a market
MIN_VOLUME_24H = 500.0


class PolymarketClient:
    def __init__(self):
        self._http = httpx.Client(timeout=30, headers={"User-Agent": "PolyWeather/1.0"})

    def get_weather_markets(self) -> list[dict]:
        """
        Fetch all active weather temperature markets from Polymarket via
        the Events API (tag_slug=temperature).

        Returns a list of normalised market dicts.
        """
        markets: list[dict] = []
        offset = 0

        while True:
            params = {
                "tag_slug": "temperature",
                "active": "true",
                "closed": "false",
                "limit": 100,
                "offset": offset,
            }
            try:
                resp = self._http.get(f"{cfg.gamma_api}/events", params=params)
                resp.raise_for_status()
                page = resp.json()
            except Exception as exc:
                logger.error(f"Error fetching events (offset={offset}): {exc}")
                break

            if not page:
                break

            for event in page:
                for raw_market in event.get("markets", []):
                    # Skip already-resolved markets
                    if raw_market.get("closed"):
                        continue
                    if not raw_market.get("active"):
                        continue

                    normalised = self._normalise(raw_market)
                    if normalised:
                        markets.append(normalised)

            if len(page) < 100:
                break
            offset += 100

        # Filter by minimum 24h trading volume
        liquid = [m for m in markets if m["volume_24hr"] >= MIN_VOLUME_24H]

        logger.info(
            f"Found {len(liquid)} active temperature markets "
            f"(volume_24hr >= ${MIN_VOLUME_24H}) from {len(markets)} total"
        )
        return liquid

    def _normalise(self, raw: dict) -> Optional[dict]:
        """
        Extract and normalise the fields we care about from a raw Gamma API
        event-market object.
        """
        question = raw.get("question", "")
        if not question:
            return None

        # clobTokenIds — JSON string or list
        clob_token_ids = raw.get("clobTokenIds", [])
        if isinstance(clob_token_ids, str):
            try:
                clob_token_ids = json.loads(clob_token_ids)
            except Exception:
                clob_token_ids = []

        # outcomePrices — JSON string or list
        outcome_prices = raw.get("outcomePrices", [])
        if isinstance(outcome_prices, str):
            try:
                outcome_prices = json.loads(outcome_prices)
            except Exception:
                outcome_prices = []

        # outcomes list
        outcomes = raw.get("outcomes", [])
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = []

        if len(clob_token_ids) < 2 or len(outcome_prices) < 2:
            return None

        yes_idx, no_idx = 0, 1
        if outcomes and len(outcomes) >= 2:
            for i, o in enumerate(outcomes):
                if str(o).lower() == "yes":
                    yes_idx = i
                elif str(o).lower() == "no":
                    no_idx = i

        yes_price = float(outcome_prices[yes_idx])
        no_price = float(outcome_prices[no_idx])

        # Skip markets not accepting orders (resolved or paused)
        if not raw.get("acceptingOrders", False):
            return None

        # Skip markets that have already effectively resolved (price near 0 or 1)
        if yes_price < 0.01 or yes_price > 0.99:
            return None

        volume_24hr = float(
            raw.get("volume24hr", raw.get("volume_24hr", 0)) or 0
        )

        return {
            "conditionId": raw.get("conditionId", ""),
            "question": question,
            "yes_token_id": clob_token_ids[yes_idx],
            "no_token_id": clob_token_ids[no_idx],
            "yes_price": yes_price,
            "no_price": no_price,
            "volume_24hr": volume_24hr,
            "end_date": raw.get("endDateIso", raw.get("endDate", "")),
        }

    def close(self):
        self._http.close()
