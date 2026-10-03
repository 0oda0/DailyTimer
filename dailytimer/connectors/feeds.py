"""Сервисы без ключей: погода (Open-Meteo), новости (RSS/Atom), контесты Codeforces."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape
from typing import Any

import httpx

WEATHER_CODES = {
    0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно", 45: "туман", 48: "изморозь",
    51: "морось", 53: "морось", 55: "сильная морось", 61: "небольшой дождь", 63: "дождь", 65: "ливень",
    66: "ледяной дождь", 67: "ледяной дождь", 71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа",
    80: "ливни", 81: "ливни", 82: "сильные ливни", 85: "снегопад", 86: "сильный снегопад", 95: "гроза",
    96: "гроза с градом", 99: "гроза с градом",
}


def weather(city: str, tz: str) -> dict[str, Any]:
    geo = httpx.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1, "language": "ru"}, timeout=20,
    ).json()
    if not geo.get("results"):
        raise ValueError(f"Город «{city}» не найден")
    place = geo["results"][0]
    data = httpx.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": place["latitude"], "longitude": place["longitude"], "timezone": tz, "forecast_days": 2,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "hourly": "temperature_2m,precipitation_probability,weather_code",
        },
        timeout=20,
    ).json()
    daily = data["daily"]
    days = [
        {
            "date": daily["time"][i],
            "min": round(daily["temperature_2m_min"][i]),
            "max": round(daily["temperature_2m_max"][i]),
            "rain": daily["precipitation_probability_max"][i],
            "text": WEATHER_CODES.get(daily["weather_code"][i], "—"),
        }
        for i in range(len(daily["time"]))
    ]
    hourly = data["hourly"]
    hours = [
        {"time": hourly["time"][i][11:16], "temp": round(hourly["temperature_2m"][i]),
         "rain": hourly["precipitation_probability"][i]}
        for i in range(7, 23, 3)
    ]
    return {"city": place["name"], "days": days, "hours": hours}


def _strip(text: str | None) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _text(fields: dict[str, ET.Element], *names: str) -> str:
    # Element без детей ложен в булевом контексте, поэтому сравниваем с None явно.
    for name in names:
        node = fields.get(name)
        if node is not None and node.text:
            return node.text
    return ""


def parse_feed(content: bytes, limit: int = 8) -> list[dict[str, Any]]:
    root = ET.fromstring(content)
    items = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in {"item", "entry"}:
            continue
        fields = {child.tag.rsplit("}", 1)[-1]: child for child in node}
        link = fields.get("link")
        href = (link.get("href") or link.text) if link is not None else ""
        items.append(
            {
                "title": _strip(_text(fields, "title")),
                "url": (href or "").strip(),
                "date": _text(fields, "pubDate", "updated", "published"),
                "summary": _strip(_text(fields, "description", "summary"))[:200],
            }
        )
        if len(items) >= limit:
            break
    return items


def feeds(urls: list[str]) -> list[dict[str, Any]]:
    result = []
    for url in urls:
        try:
            resp = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "DailyTimer/1.0"})
            resp.raise_for_status()
            result.append({"url": url, "items": parse_feed(resp.content)})
        except Exception as exc:  # одна битая лента не должна ломать остальные
            result.append({"url": url, "items": [], "error": str(exc)})
    return result


def codeforces_contests(days: int = 7) -> list[dict[str, Any]]:
    data = httpx.get("https://codeforces.com/api/contest.list", params={"gym": "false"}, timeout=20).json()
    now = datetime.now(timezone.utc).timestamp()
    upcoming = [
        {
            "name": c["name"],
            "start": datetime.fromtimestamp(c["startTimeSeconds"], timezone.utc).isoformat(),
            "duration_h": round(c["durationSeconds"] / 3600, 1),
            "url": f"https://codeforces.com/contests/{c['id']}",
        }
        for c in data.get("result", [])
        if c.get("phase") == "BEFORE" and now < c.get("startTimeSeconds", 0) < now + days * 86400
    ]
    return sorted(upcoming, key=lambda c: c["start"])
