import re
from datetime import date, datetime
from typing import Optional

from dateutil import parser as dateutil_parser
from loguru import logger


# Regex patterns for weather market questions
_ABOVE_BELOW = re.compile(
    r"highest temperature in (.+?) be (\d+(?:\.\d+)?)\s*[°]?\s*(F|C)\s*or\s*(higher|above|lower|below)\s+on\s+(.+?)[\?\s]*$",
    re.IGNORECASE,
)

_BETWEEN = re.compile(
    r"highest temperature in (.+?) be between\s+(\d+(?:\.\d+)?)[–\-](\d+(?:\.\d+)?)\s*[°]?\s*(F|C)\s+on\s+(.+?)[\?\s]*$",
    re.IGNORECASE,
)

# Also catch "X°F or higher" with the degree symbol attached to the number
_ABOVE_BELOW_DEG = re.compile(
    r"highest temperature in (.+?) be (\d+(?:\.\d+)?)°(F|C)\s*or\s*(higher|above|lower|below)\s+on\s+(.+?)[\?\s]*$",
    re.IGNORECASE,
)

_BETWEEN_DEG = re.compile(
    r"highest temperature in (.+?) be between\s+(\d+(?:\.\d+)?)[–\-](\d+(?:\.\d+)?)°(F|C)\s+on\s+(.+?)[\?\s]*$",
    re.IGNORECASE,
)

# Celsius spelled out
_CELSIUS_EXACT = re.compile(
    r"highest temperature in (.+?) be (\d+(?:\.\d+)?)°C(?:\s+on|\s*,?\s+on)\s+(.+?)[\?\s]*$",
    re.IGNORECASE,
)


def _parse_date(date_str: str) -> Optional[date]:
    date_str = date_str.strip().rstrip("?").strip()
    today = datetime.now()
    try:
        dt = dateutil_parser.parse(date_str, default=datetime(today.year, today.month, today.day))
        # If parsed date is more than 30 days in the past, assume next year
        if (dt.date() - today.date()).days < -30:
            dt = dt.replace(year=dt.year + 1)
        return dt.date()
    except Exception:
        logger.debug(f"Could not parse date: {date_str!r}")
        return None


def parse_question(question: str) -> Optional[dict]:
    """
    Parse a Polymarket weather market question.

    Returns a dict with keys:
      city (str), date (date), threshold (float), threshold2 (float|None),
      direction ('above'|'below'|'between'), unit ('F'|'C')

    Returns None if the question does not match a known weather pattern.
    """
    # Try "or higher / or below" patterns (with and without °)
    for pattern in (_ABOVE_BELOW_DEG, _ABOVE_BELOW):
        m = pattern.search(question)
        if m:
            city = m.group(1).strip()
            threshold = float(m.group(2))
            unit = m.group(3).upper()
            direction_word = m.group(4).lower()
            direction = "above" if direction_word in ("higher", "above") else "below"
            parsed = _parse_date(m.group(5))
            if parsed:
                return {
                    "city": city,
                    "date": parsed,
                    "threshold": threshold,
                    "threshold2": None,
                    "direction": direction,
                    "unit": unit,
                }

    # Try "between X-Y" patterns
    for pattern in (_BETWEEN_DEG, _BETWEEN):
        m = pattern.search(question)
        if m:
            city = m.group(1).strip()
            threshold = float(m.group(2))
            threshold2 = float(m.group(3))
            unit = m.group(4).upper()
            parsed = _parse_date(m.group(5))
            if parsed:
                return {
                    "city": city,
                    "date": parsed,
                    "threshold": threshold,
                    "threshold2": threshold2,
                    "direction": "between",
                    "unit": unit,
                }

    # Try bare Celsius exact value: "be 39°C on March 25"
    m = _CELSIUS_EXACT.search(question)
    if m:
        city = m.group(1).strip()
        threshold = float(m.group(2))
        parsed = _parse_date(m.group(3))
        if parsed:
            return {
                "city": city,
                "date": parsed,
                "threshold": threshold,
                "threshold2": None,
                "direction": "exact_c",  # handled specially in analyzer
                "unit": "C",
            }

    return None
