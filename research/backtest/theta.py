"""Minimal client for the local Theta Terminal (v3), paced to the free tier's 30 requests a minute."""

import io
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

BASE = "http://127.0.0.1:25503/v3"
MIN_INTERVAL = 2.05  # seconds between requests
NO_DATA = 472  # Theta's status for a valid request with nothing to return

_last_request = 0.0


def get_csv(path, timeout=180, tries=4, **params):
    """A CSV endpoint as a DataFrame, or None when Theta has no data. Other failures are retried, then raised."""
    global _last_request
    url = f"{BASE}/{path}?" + urllib.parse.urlencode({**params, "format": "csv"}, safe="*")
    failure = None
    for attempt in range(tries):
        wait = _last_request + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                body = response.read()
            return pd.read_csv(io.BytesIO(body)) if body.strip() else None
        except urllib.error.HTTPError as e:
            if e.code == NO_DATA:
                return None
            failure = f"HTTP {e.code}: {e.read()[:160]!r}"
            if e.code == 403:  # beyond what the subscription allows: retrying cannot help
                break
        except Exception as e:  # noqa: BLE001 - network hiccups are retried, then reported
            failure = f"{type(e).__name__}: {e}"
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"{failure} :: {url}")


def option_eod(symbol, start, end, expiration="*", strike_range=None, strike="*"):
    """End-of-day rows (one per contract and day) with a parsed `date`; None when there is no data."""
    params = {"symbol": symbol, "expiration": expiration, "right": "both", "start_date": start, "end_date": end}
    if strike_range is not None:
        params["strike_range"] = strike_range
    else:
        params["strike"] = strike
    frame = get_csv("option/history/eod", **params)
    if frame is None or frame.empty:
        return None
    frame["date"] = pd.to_datetime(frame["created"]).dt.normalize()
    return frame
