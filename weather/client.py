"""
Weather client — fetches temperature forecasts from Open-Meteo + NOAA NWS.

Source priority
───────────────
1. Multi-model ensemble (ensemble-api.open-meteo.com):
     ECMWF IFS025  — 50 members, most accurate globally
     GFS seamless  — 30 members, strong US coverage
     ICON seamless — 39 members, German DWD model, independent bias
     GEM global    — 20 members, Canadian model
   Total: up to 139 members

2. NWS augmentation (US cities only, api.weather.gov):
     Official NOAA point forecast — adds 20 synthetic members with tight σ.
     Reduces station-mismatch error for US markets (Polymarket resolves
     against NWS-adjacent stations).

3. Synthetic fallback — regular Open-Meteo point forecast + normal-distribution
   ensemble, calibrated σ by horizon:
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
_NWS_API       = "https://api.weather.gov"
_NWS_HEADERS   = {"User-Agent": "PolyWeather/1.0 (weather trading bot)"}

# Calibrated forecast RMSE by horizon (°C)
_SIGMA_BY_HORIZON = [(1, 1.5), (2, 2.0), (3, 2.5), (5, 3.0), (999, 4.0)]

# Tighter sigma for NWS-derived synthetic members — NWS is station-calibrated.
# In °F (NWS natively reports °F).  Only used up to 7d (NWS forecast range).
_NWS_SIGMA_BY_HORIZON_F = [(1, 1.0), (2, 1.5), (3, 2.0), (5, 3.0), (7, 4.0)]

# Approximate bounding box for the contiguous US + Alaska/Hawaii buffer.
# Used to decide whether to call the NWS API.
_US_LAT = (24.0, 72.0)
_US_LON = (-180.0, -65.0)

# Bias corrections removed Apr 9 — corrections were based on insufficient data
# (<30 points per city) and were adding noise rather than signal. The raw
# ECMWF ensemble is more reliable than manual station-mismatch corrections
# until we have 50+ resolved trades per city to calibrate against.
_TEMP_BIAS_F: dict[str, float] = {}


def _today_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _is_us_coords(lat: float, lon: float) -> bool:
    return _US_LAT[0] <= lat <= _US_LAT[1] and _US_LON[0] <= lon <= _US_LON[1]


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
        # NWS grid URL cache: "lat,lon" → hourly forecast URL | None
        self._nws_grids: dict[str, Optional[str]] = {}
        logger.debug(f"WeatherClient: loaded {len(self._cache)} entries from disk cache")

    def _throttle(self):
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < _REQUEST_DELAY:
            time.sleep(_REQUEST_DELAY - elapsed)
        self._last_request_time = time.monotonic()

    # ── Ensemble fetcher ──────────────────────────────────────────────────────

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

    # ── NWS forecast (US cities, reduces station mismatch) ────────────────────

    def _fetch_nws(
        self, lat: float, lon: float, target_date: date, temp_unit: str
    ) -> Optional[float]:
        """
        Fetch NOAA NWS daily max temperature for a US location.
        Returns the max temp in requested unit, or None if unavailable.
        NWS only provides forecasts ~7 days out.
        """
        grid_key = f"{lat:.3f},{lon:.3f}"

        # Resolve grid URL (cached per session)
        if grid_key not in self._nws_grids:
            try:
                resp = self._http.get(
                    f"{_NWS_API}/points/{lat},{lon}",
                    headers=_NWS_HEADERS, timeout=10,
                )
                if resp.status_code == 200:
                    self._nws_grids[grid_key] = resp.json()["properties"]["forecastHourly"]
                else:
                    self._nws_grids[grid_key] = None
            except Exception:
                self._nws_grids[grid_key] = None

        hourly_url = self._nws_grids.get(grid_key)
        if not hourly_url:
            return None

        try:
            self._throttle()
            resp = self._http.get(hourly_url, headers=_NWS_HEADERS, timeout=15)
            if resp.status_code != 200:
                return None
            periods = resp.json()["properties"]["periods"]
            target_str = target_date.isoformat()
            daily_max_f: Optional[float] = None
            for p in periods:
                if p["startTime"][:10] == target_str:
                    t = float(p["temperature"])  # NWS always returns °F
                    if daily_max_f is None or t > daily_max_f:
                        daily_max_f = t
            if daily_max_f is None:
                return None
            if temp_unit == "celsius":
                return (daily_max_f - 32.0) * 5.0 / 9.0
            return daily_max_f
        except Exception as exc:
            logger.debug(f"NWS fetch error: {exc}")
            return None

    # ── Regular forecast (fallback) ───────────────────────────────────────────

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
        Tries all four ensemble models, augments US cities with NWS data,
        and falls back to synthetic normal ensemble if all models fail.
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

        # ── Step 1: try all four ensemble models ──────────────────────────────
        all_members: list[float] = []
        for model in ("ecmwf_ifs025", "gfs_seamless", "icon_seamless", "gem_global"):
            m = self._fetch_ensemble(lat, lon, target_date, temp_unit,
                                     model=model, api_url=cfg.ensemble_api)
            if m:
                all_members.extend(m)

        if all_members:
            members = all_members
            logger.debug(f"Multi-model ensemble: {len(members)} members for {city}")
        else:
            logger.info(f"Ensemble unavailable for {city} — using regular-API fallback")
            members = self._fetch_regular(lat, lon, target_date, temp_unit) or []

        # ── Step 2: augment with NWS for US cities ────────────────────────────
        # NWS is the official NOAA forecast, closely tied to the weather stations
        # Polymarket uses for US market resolution.  Adds 20 tight synthetic members.
        today = date.today()
        horizon_days = (target_date - today).days
        if _is_us_coords(lat, lon) and 1 <= horizon_days <= 7:
            nws_temp = self._fetch_nws(lat, lon, target_date, temp_unit)
            if nws_temp is not None:
                sigma_f = next(s for h, s in _NWS_SIGMA_BY_HORIZON_F if horizon_days <= h)
                nws_sigma = sigma_f if unit == "F" else sigma_f * 5.0 / 9.0
                rng = np.random.default_rng(seed=int(target_date.strftime("%Y%m%d")) + 999)
                nws_members = rng.normal(loc=nws_temp, scale=nws_sigma, size=20).tolist()
                members = members + nws_members
                logger.debug(
                    f"NWS augmentation for {city}: {nws_temp:.1f}°{unit} "
                    f"(σ={nws_sigma:.1f}), +20 members → {len(members)} total"
                )

        if not members:
            self._cache[cache_key] = None
            return None

        # ── Step 3: apply bias corrections (currently empty) ─────────────────
        bias_f = _TEMP_BIAS_F.get(city, 0.0)
        if bias_f != 0.0:
            bias = bias_f if unit == "F" else bias_f * 5 / 9
            members = [t + bias for t in members]
            logger.debug(
                f"{city}: applied bias correction {bias_f:+.2f}°F "
                f"({'warmer' if bias_f > 0 else 'cooler'})"
            )

        logger.debug(
            f"{city} {target_date}: {len(members)} members, "
            f"mean={np.mean(members):.1f}°{unit}, std={np.std(members):.1f}°{unit}"
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
        method        : "ensemble", "multi_model", "nws_augmented", or "regular_fallback"
        n_members     : number of ensemble members used
        forecast_mean : mean of member temps
        forecast_std  : std-dev of member temps
        """
        cache_key = f"{city.lower()}|{target_date.isoformat()}|{unit}"
        temps = self._cache.get(cache_key)
        if temps is None:
            return None

        all_ensemble_blocked = all(
            f"{cfg.ensemble_api}:{m}" in self._blocked
            for m in ("ecmwf_ifs025", "gfs_seamless", "icon_seamless", "gem_global")
        )
        n = len(temps)
        if all_ensemble_blocked:
            method = "regular_fallback"
        elif n > 139:
            method = "nws_augmented"   # ensemble + NWS top-up
        elif n > 50:
            method = "multi_model"
        else:
            method = "ensemble"

        return {
            "method": method,
            "n_members": n,
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
