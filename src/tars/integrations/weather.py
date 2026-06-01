"""OpenWeatherMap forecast — async wrapper around their /forecast endpoint.

Returns a compact "today + tonight" summary the LLM can render into a
*Weather* section of the morning briefing. Designed for graceful degradation:
on any error (rate-limit, network, bad key) returns None and the briefing
just omits the section.

Endpoint: GET /data/2.5/forecast (5-day / 3-hour forecast, free tier).
We pluck only the slots in today's date window, compute min/max temp,
collect distinct conditions, and grab the first slot's full description.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("tars.integrations.weather")

FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"


async def fetch_today_forecast(cfg) -> dict[str, Any] | None:
    """Return a dict like:
        {
          "location": "Tel Aviv",
          "today_min_c": 19.4,
          "today_max_c": 27.1,
          "morning_c": 22.0,
          "afternoon_c": 26.5,
          "conditions": ["clear", "few clouds"],
          "headline": "clear sky",
          "rain_today_mm": 0.0,
        }
    or None on any failure / when no API key is configured."""
    wcfg = getattr(cfg, "weather", None)
    if wcfg is None or not wcfg.api_key:
        return None

    params = {
        "lat": wcfg.lat,
        "lon": wcfg.lon,
        "units": "metric",
        "appid": wcfg.api_key,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(FORECAST_URL, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("weather fetch failed (%s)", e)
        return None

    tz = ZoneInfo(getattr(cfg, "timezone", "UTC"))
    today: date = datetime.now(tz).date()

    slots = data.get("list") or []
    today_slots: list[dict] = []
    morning_c: float | None = None
    afternoon_c: float | None = None
    rain_today = 0.0
    conditions: list[str] = []
    headline: str = ""

    for s in slots:
        # Each slot has dt (unix utc) + dt_txt (UTC string). Convert to local.
        ts = int(s.get("dt") or 0)
        if not ts:
            continue
        local = datetime.fromtimestamp(ts, tz=tz)
        if local.date() != today:
            continue
        today_slots.append(s)
        main = s.get("main") or {}
        weather = (s.get("weather") or [{}])[0]
        cond = (weather.get("main") or "").strip().lower()
        if cond and cond not in conditions:
            conditions.append(cond)
        if not headline:
            headline = (weather.get("description") or "").strip()
        if 7 <= local.hour <= 10 and morning_c is None:
            morning_c = main.get("temp")
        if 13 <= local.hour <= 16 and afternoon_c is None:
            afternoon_c = main.get("temp")
        rain_today += float((s.get("rain") or {}).get("3h") or 0.0)

    if not today_slots:
        # API returned but with no today coverage — possible at end of day.
        return None

    temps = [
        s.get("main", {}).get("temp")
        for s in today_slots
        if s.get("main", {}).get("temp") is not None
    ]
    if not temps:
        return None

    return {
        "location": wcfg.location_name,
        "today_min_c": round(min(temps), 1),
        "today_max_c": round(max(temps), 1),
        "morning_c": round(morning_c, 1) if morning_c is not None else None,
        "afternoon_c": round(afternoon_c, 1) if afternoon_c is not None else None,
        "conditions": conditions[:3],   # cap to 3 distinct
        "headline": headline or (conditions[0] if conditions else "unknown"),
        "rain_today_mm": round(rain_today, 1),
    }
