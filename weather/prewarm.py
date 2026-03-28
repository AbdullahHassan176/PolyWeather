"""
Weather cache pre-warmer.

Run once at startup (or via cron at midnight UTC) to fetch ensemble forecasts
for all known cities before the main bot starts scanning.  This ensures every
city has real ensemble data cached, so the bot runs entirely off cache and
doesn't burn the daily API quota during individual scans.

Usage
─────
    py -3.11 -m weather.prewarm          # fetch next 7 days for all cities
    py -3.11 -m weather.prewarm --days 3 # fetch next 3 days only
"""
import sys
from datetime import date, timedelta
from pathlib import Path

# Ensure project root is importable regardless of invocation directory
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from loguru import logger
from weather.cities import CITIES
from weather.client import WeatherClient


def prewarm(days: int = 7) -> None:
    today = date.today()
    dates = [today + timedelta(days=d) for d in range(1, days + 1)]
    cities = list(CITIES.keys())

    logger.info(f"Pre-warming weather cache: {len(cities)} cities × {days} days = {len(cities)*days} lookups")

    wc = WeatherClient()
    total = 0
    cached = 0

    for city in cities:
        for tdate in dates:
            # Try F first (US cities), then C (rest)
            for unit in ("F", "C"):
                key = f"{city.lower()}|{tdate.isoformat()}|{unit}"
                if key in wc._cache and wc._cache[key] is not None:
                    cached += 1
                    continue
                result = wc.get_ensemble_temps(city, tdate, unit)
                total += 1
                if result:
                    logger.debug(f"  {city} {tdate} {unit}: {len(result)} members")

    wc.close()
    logger.info(
        f"Pre-warm complete — {total} new fetches, {cached} already cached. "
        f"Cache now has entries for {len(wc._cache)} city/date/unit combos."
    )


if __name__ == "__main__":
    days = 7
    if "--days" in sys.argv:
        idx = sys.argv.index("--days")
        if idx + 1 < len(sys.argv):
            days = int(sys.argv[idx + 1])
    prewarm(days=days)
