"""
Weather client — fetches temperature forecasts from Open-Meteo.

Source priority
───────────────
1. ECMWF ensemble API  (ensemble-api.open-meteo.com) — 50 members, most accurate
2. GFS ensemble        (same endpoint, gfs_seamless)  — 30 members
3. Synthetic fallback  — regular point forecast + normal-distribution ensemble,
                       calibrated σ by horizon:
                       ≤1d: σ=1.5°C, ≤2d: σ=2.0°C, ≤3d: σ=2.5°C, ≤5d: σ=3.0°C, else σ=4.0°C

Cache strategy
──────────────
Results are cached on disk (weather_cache.json).  Entries are keyed by
"city|target_date|unit" and kept until the target_date passes (>= today).
This means each city+date combo is fetched at most once across all restarts.
"""
import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import httpx
import numpy as np
from loguru import logger

from config import cfg
from weather.cities import get_coordinates

_REQUEST_DELAY = 3.0           # minimum seconds between API calls
_CACHE_FILE    = Path("weather_cache.json")
_REGULAR_API   = "https://api.open-meteo.com/v1/forecast"

# Calibrated forecast RMSE by horizon (°C)
_SIGMA_BY_HORIZON = [(1, 1.5), (2, 2.0), (3, 2.5), (5, 3.0), (999, 4.0)]


def _today_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _load_disk_cache() -> dict:
    """Load the on-disk cache, discarding entries from previous days."""
    if not _CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        today = _today_str()
        # Keep entries for today or future dates — discard past dates only
        valid = {k: v for k, v in data.items() if k.split("|")[1] >= today}
        return valid
    except Exception:
        return {}


def _save_disk_cache(cache: dict) -> None:
    try:
        _CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")
    except Exception as exc:
        logger.debug(f"Could not save weather cache: {exc}")


class WeatherClient:
    def __init__(self):
        self._http = httpx.Client(timeout=30)
        # In-memory cache: "city|date|unit" → list[float] | None
        self._cache: dict[str, Optional[list[float]]] = _load_disk_cache()
        self._last_request_time: float = 0.0
        # Track which (endpoint, model) combos have hit their rate limit this session
        self._blocked: set[str] = set()   # entries like "ensemble_api:ecmwf_ifs025"
        logger.debug(f"WeatherClient: loaded {len(self._cache)} entries from disk cache")

    def _throttle(self):
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < _REQUEST_DELAY:
            time.sleep(_REQUEST_DELAY - elapsed)
        self._last_request_time = time.monotonic()

    # ── Ensemble fetcher (used for both API endpoints) ────────────────────────

    def _fetch_ensemble(
        self, lat: float, lon: float, target_date: date, temp_unit: str,
        model: str, api_url: str
    ) -> Optional[list[float]]:
        """
        Fetch one ensemble model from the given API URL.
        Returns member temps or None on 429/error.
        Blocks the (api_url, model) pair on 429 so we don't retry it this session.
        """
        block_key = f"{api_url}:{model}"
        if block_key in self._blocked:
            return None

        params = {
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max",
            "models": model,
            "temperature_unit": temp_unit,
            "forecast_days": 16,
        }
        if cfg.open_meteo_api_key:
            params["apikey"] = cfg.open_meteo_api_key
        self._throttle()
        try:
            resp = self._http.get(api_url, params=params)
        except httpx.HTTPError as exc:
            logger.debug(f"Ensemble network error ({model} @ {api_url}): {exc}")
            return None

        if resp.status_code == 429:
            logger.warning(f"{model} rate-limited (429) on {api_url} — blocked for this session")
            self._blocked.add(block_key)
            return None

        if resp.status_code != 200:
            return None

        daily  = resp.json().get("daily", {})
        dates  = daily.get("time", [])
        target = target_date.isoformat()
        if target not in dates:
            return None
        idx = dates.index(target)

        members = [
            float(v[idx])
            for k, v in daily.items()
            if k.startswith("temperature_2m_max_member") and v[idx] is not None
        ]
        return members if members else None

    # ── Regular forecast (fallback) ────────────────────────────────────────────

    def _fetch_regular(
        self, lat: float, lon: float, target_date: date, temp_unit: str
    ) -> Optional[list[float]]:
        """
        Fetch point forecast from regular API, then generate a synthetic
        50-member normal ensemble calibrated to the forecast horizon.
        """
        today  = date.today()
        horizon_days = (target_date - today).days
        sigma_c = next(s for h, s in _SIGMA_BY_HORIZON if horizon_days <= h)
        sigma   = sigma_c * (9 / 5) if temp_unit == "fahrenheit" else sigma_c

        params = {
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max",
            "temperature_unit": temp_unit,
            "forecast_days": min(16, max(1, horizon_days + 1)),
        }
        if cfg.open_meteo_api_key:
            params["apikey"] = cfg.open_meteo_api_key
        self._throttle()
        try:
            resp = self._http.get(_REGULAR_API, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error(f"Regular API error: {exc}")
            return None

        daily  = resp.json().get("daily", {})
        dates  = daily.get("time", [])
        target = target_date.isoformat()
        if target not in dates:
            return None
        idx   = dates.index(target)
        point = daily.get("temperature_2m_max", [None] * (idx + 1))[idx]
        if point is None:
            return None

        rng     = np.random.default_rng(seed=int(target_date.strftime("%Y%m%d")))
        members = (rng.normal(loc=point, scale=sigma, size=50)).tolist()
        logger.debug(
            f"Regular fallback: point={point:.1f}, σ={sigma:.1f}, horizon={horizon_days}d"
        )
        return members

    # ── Public interface ───────────────────────────────────────────────────────

    def get_ensemble_temps(
        self,
        city: str,
        target_date: date,
        unit: str = "F",
    ) -> Optional[list[float]]:
        """
        Return ensemble/synthetic temperature members for a city and date.
        Tries ECMWF ensemble API first; falls back to regular API on 429.
        Results are disk-cached.
        """
        coords = get_coordinates(city)
        if coords is None:
            logger.warning(f"Unknown city: {city!r}")
            return None

        cache_key  = f"{city.lower()}|{target_date.isoformat()}|{unit}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        lat, lon   = coords
        temp_unit  = "fahrenheit" if unit == "F" else "celsius"

        # Try ECMWF (50 members) + GFS (30 members) from ensemble-api.open-meteo.com.
        # Falls back to synthetic normal-distribution if rate-limited/unavailable.
        all_members: list[float] = []
        for model in ("ecmwf_ifs025", "gfs_seamless"):
            m = self._fetch_ensemble(lat, lon, target_date, temp_unit,
                                     model=model, api_url=cfg.ensemble_api)
            if m:
                all_members.extend(m)

        if all_members:
            members = all_members
            logger.debug(f"Multi-model ensemble: {len(members)} members for {city}")
        else:
            logger.info(f"Ensemble unavailable for {city} — using regular-API fallback")
            members = self._fetch_regular(lat, lon, target_date, temp_unit)

        if members:
            logger.debug(
                f"{city} {target_date}: {len(members)} members, "
                f"mean={np.mean(members):.1f}{unit}, std={np.std(members):.1f}{unit}"
            )
        self._cache[cache_key] = members
        _save_disk_cache(self._cache)
        return members

    def get_forecast_meta(
        self,
        city: str,
        target_date: date,
        unit: str = "F",
    ) -> Optional[dict]:
        """
        Return a metadata dict for this city/date forecast, or None if unavailable.

        Keys
        ----
        method        : "ensemble" or "regular_fallback"
        n_members     : number of ensemble members used
        forecast_mean : mean of member temps
        forecast_std  : std-dev of member temps
        """
        cache_key = f"{city.lower()}|{target_date.isoformat()}|{unit}"
        temps = self._cache.get(cache_key)
        if temps is None:
            return None

        coords = get_coordinates(city)
        temp_unit = "fahrenheit" if unit == "F" else "celsius"

        all_ensemble_blocked = all(
            f"{cfg.ensemble_api}:{m}" in self._blocked
            for m in ("ecmwf_ifs025", "gfs_seamless")
        )
        if all_ensemble_blocked or len(temps) <= 50:
            method = "regular_fallback" if all_ensemble_blocked else "ensemble"
        else:
            method = "multi_model"

        return {
            "method": method,
            "n_members": len(temps),
            "forecast_mean": round(float(np.mean(temps)), 2),
            "forecast_std": round(float(np.std(temps)), 2),
        }

    def get_probability(
        self,
        city: str,
        target_date: date,
        threshold: float,
        direction: str,
        unit: str = "F",
        threshold2: Optional[float] = None,
    ) -> Optional[float]:
        """
        Returns P(0–1) that the max temperature satisfies the condition.

        direction: 'above' | 'below' | 'between'
        """
        temps = self.get_ensemble_temps(city, target_date, unit)
        if temps is None:
            return None

        n = len(temps)
        if direction == "above":
            hits = sum(1 for t in temps if t >= threshold)
        elif direction == "below":
            hits = sum(1 for t in temps if t <= threshold)
        elif direction == "between":
            if threshold2 is None:
                return None
            hits = sum(1 for t in temps if threshold <= t <= threshold2)
        else:
            return None

        return (hits + 1) / (n + 2)   # Laplace smoothing

    def close(self):
        self._http.close()
