#!/usr/bin/env python3
"""Daily Astana events guide for the existing Telegram bot.

The guide contains an all-day weather forecast and upcoming theatre, cinema,
conference/IT and other cultural events. It only shows source text and links
when a listing provides them; it does not invent descriptions or ticket status.
"""
from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Almaty")
UA = "AstanaEventsGuide/2.0 (+https://github.com/sayupov/astana-evening-events)"
THEATRE_URL = "https://teatr.kz/ru/astana"
CINEMA_URL = "https://kz.kinoafisha.info/astana/movies/"
HUB_URL = "https://astanahub.com/ru/event/calendar/"
CONFERENCE_URL = "https://expomap.ru/conference/city/astana/2026/nov/"
CITY_EVENTS_URL = "https://astana.kz/ru/events/"
TICKETON_URL = "https://ticketon.kz/astana"

WEATHER_CODES = {
    0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность",
    3: "пасмурно", 45: "туман", 48: "изморозь", 51: "морось",
    53: "морось", 55: "сильная морось", 61: "дождь", 63: "дождь",
    65: "сильный дождь", 71: "снег", 73: "снег", 75: "сильный снег",
    80: "ливень", 81: "ливень", 82: "сильный ливень", 95: "гроза",
}


def fetch(url: str, timeout: int = 25) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Language": "ru-KZ,ru;q=0.9"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read(2_000_000).decode(charset, errors="replace")


def plain(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def cut(value: str, limit: int = 160) -> str:
    value = plain(value)
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def theatre_items(page: str) -> list[dict]:
    text = plain(page)
    pattern = re.compile(
        r"(?P<time>\d{1,2}:\d{2})\s+(?P<title>.{2,130}?)\s+\d{1,2}\+\s+"
        r"(?P<place>.{2,150}?)(?=\s+·\s+|\s+Билеты|$)"
    )
    today_start, tomorrow_start = text.find("Сегодня ·"), text.find("Завтра ·")
    now_time = datetime.now(TZ).time()
    result, seen = [], set()
    for match in pattern.finditer(text):
        is_today = today_start >= 0 and match.start() >= today_start and (
            tomorrow_start < 0 or match.start() < tomorrow_start
        )
        hour, minute = map(int, match.group("time").split(":"))
        if is_today and (hour, minute) <= (now_time.hour, now_time.minute):
            continue
        title = match.group("title").strip(" -–:.")
        if not 3 <= len(title) <= 130 or title.casefold() in seen:
            continue
        seen.add(title.casefold())
        result.append({
            "title": title, "when": match.group("time"), "place": match.group("place").strip(),
            "about": "Спектакль. Подробности и состав смотрите в афише.",
            "url": THEATRE_URL,
        })
    return result


def cinema_items(page: str) -> list[dict]:
    prefix = re.escape('"url":"https:\\/\\/www.kinoafisha.info\\/movies\\/')
    pattern = re.compile(prefix + r'(\d+)\\/","name":"([^"]+)"')
    result, seen = [], set()
    for movie_id, title in pattern.findall(page):
        title = html.unescape(title).strip()
        if not title or title.casefold() in seen:
            continue
        seen.add(title.casefold())
        result.append({
            "title": title, "when": "ближайшие сеансы", "place": "кинотеатры Астаны",
            "about": "Кино. Жанр, возрастной рейтинг и расписание смотрите по ссылке.",
            "url": f"https://kz.kinoafisha.info/astana/movies/{movie_id}/",
        })
    return result


def jsonld_events(page: str, source_url: str) -> list[dict]:
    """Extract public Event schema cards without guessing unavailable fields."""
    scripts = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page, flags=re.IGNORECASE | re.DOTALL,
    )
    result, seen = [], set()

    def nodes(value):
        if isinstance(value, list):
            for entry in value:
                yield from nodes(entry)
        elif isinstance(value, dict):
            if "@graph" in value:
                yield from nodes(value["@graph"])
            else:
                yield value

    for script in scripts:
        try:
            payload = json.loads(html.unescape(script).strip())
        except json.JSONDecodeError:
            continue
        for event in nodes(payload):
            event_type = event.get("@type", "")
            if isinstance(event_type, list):
                is_event = "Event" in event_type
            else:
                is_event = "Event" in event_type
            title = plain(str(event.get("name", "")))
            if not is_event or not title or title.casefold() in seen:
                continue
            seen.add(title.casefold())
            start = plain(str(event.get("startDate", "")))
            location = event.get("location", {})
            if isinstance(location, dict):
                place = plain(str(location.get("name", "")))
            else:
                place = plain(str(location))
            result.append({
                "title": title, "when": event_time(start), "place": place or "площадка уточняется",
                "about": cut(str(event.get("description", ""))) or "Краткое описание не указано в афише.",
                "url": event.get("url") or source_url,
            })
    return result


def conference_items(page: str) -> list[dict]:
    """Read public conference cards from Expomap's Astana calendar."""
    text = plain(page)
    pattern = re.compile(
        r"(?P<date>\d{2}\.\d{2}\.\d{2}\s*[—-]\s*\d{2}\.\d{2}\.\d{2})\s+"
        r"Казахстан,\s*Астана\s+(?P<title>.{3,180}?)(?:\s+#|\s+Подробнее|$)"
    )
    result, seen = [], set()
    for match in pattern.finditer(text):
        title = match.group("title").strip(" .")
        if title.casefold() in seen:
            continue
        seen.add(title.casefold())
        tail = text[match.end():match.end() + 260]
        about = re.split(r"\s+#|\s+Подробнее", tail)[0].strip()
        result.append({
            "title": title,
            "when": match.group("date"),
            "place": "Астана",
            "about": cut(about) or "Конференция. Подробности и регистрация по ссылке.",
            "url": CONFERENCE_URL,
        })
    return result


def event_time(value: str) -> str:
    if not value:
        return "дату и время уточните"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TZ)
        today = datetime.now(TZ).date()
        day = "сегодня" if parsed.date() == today else parsed.strftime("%d.%m")
        return f"{day}, {parsed:%H:%M}"
    except ValueError:
        return value[:40]


def weather_day() -> list[str]:
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast?latitude=51.1694&longitude=71.4491"
            "&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset"
            "&hourly=temperature_2m,apparent_temperature,precipitation_probability,"
            "wind_speed_10m,weather_code&timezone=Asia%2FAlmaty&forecast_days=1"
        )
        data = json.loads(fetch(url))
        hourly, daily = data["hourly"], data["daily"]
        lines = ["ПОГОДА НА ВЕСЬ ДЕНЬ"]
        for label, hour in (("Утро", "09:00"), ("День", "13:00"), ("Вечер", "18:00"), ("Ночь", "21:00")):
            index = next((i for i, value in enumerate(hourly["time"]) if value.endswith(hour)), None)
            if index is None:
                continue
            temp = round(hourly["temperature_2m"][index])
            feels = round(hourly["apparent_temperature"][index])
            wind = round(hourly["wind_speed_10m"][index])
            rain = hourly["precipitation_probability"][index]
            condition = WEATHER_CODES.get(hourly["weather_code"][index], "условия уточняются")
            lines.append(f"{label} {hour}: {temp:+d} °C, ощущается как {feels:+d} °C, {condition}, ветер {wind} км/ч, осадки {rain}%.")
        low, high = round(daily["temperature_2m_min"][0]), round(daily["temperature_2m_max"][0])
        sunrise = daily["sunrise"][0][-5:]
        sunset = daily["sunset"][0][-5:]
        lines.append(f"За день: {low:+d}…{high:+d} °C. Восход {sunrise}, закат {sunset}.")
        return lines
    except (KeyError, TypeError, ValueError, urllib.error.URLError, TimeoutError):
        return ["ПОГОДА НА ВЕСЬ ДЕНЬ", "Прогноз сейчас недоступен. Проверьте погоду перед выходом."]


def block(title: str, items: list[dict], maximum: int = 3) -> list[str]:
    lines = [title]
    if not items:
        return lines + ["Пока нет карточек с подтвержденными данными.", ""]
    for item in items[:maximum]:
        details = " · ".join(part for part in (item["when"], item["place"]) if part)
        lines += [item["title"], details, f"О чем: {item['about']}", item["url"], ""]
    return lines


def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not chat or not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise RuntimeError("TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы")
    body = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "disable_web_page_preview": "true"
    }).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=35) as response:
        if not json.loads(response.read()).get("ok"):
            raise RuntimeError("Telegram отклонил сообщение")


def build_message() -> str:
    sections = {"ТЕАТР": [], "КИНО": [], "КОНФЕРЕНЦИИ И IT": [], "ВЫСТАВКИ И ДРУГИЕ СОБЫТИЯ": []}
    errors = []
    for name, url, parser, section in (
        ("театр", THEATRE_URL, theatre_items, "ТЕАТР"),
        ("кино", CINEMA_URL, cinema_items, "КИНО"),
        ("Astana Hub", HUB_URL, lambda page: jsonld_events(page, HUB_URL), "КОНФЕРЕНЦИИ И IT"),
        ("конференции", CONFERENCE_URL, conference_items, "КОНФЕРЕНЦИИ И IT"),
        ("Astana.kz", CITY_EVENTS_URL, lambda page: jsonld_events(page, CITY_EVENTS_URL), "ВЫСТАВКИ И ДРУГИЕ СОБЫТИЯ"),
        ("Ticketon", TICKETON_URL, lambda page: jsonld_events(page, TICKETON_URL), "ВЫСТАВКИ И ДРУГИЕ СОБЫТИЯ"),
    ):
        try:
            sections[section].extend(parser(fetch(url)))
        except (urllib.error.URLError, TimeoutError, UnicodeError, ValueError) as exc:
            errors.append(f"{name}: {type(exc).__name__}")

    today = datetime.now(TZ).strftime("%d.%m.%Y")
    lines = ["АФИША АСТАНЫ", f"Дата: {today}", ""]
    lines += weather_day() + [""]
    for title in ("ТЕАТР", "КИНО", "КОНФЕРЕНЦИИ И IT", "ВЫСТАВКИ И ДРУГИЕ СОБЫТИЯ"):
        lines += block(title, sections[title])
    lines.append("Время, площадку и наличие билетов проверьте по ссылке перед выходом.")
    if errors:
        lines.append("Временно недоступны источники: " + ", ".join(errors) + ".")
    return "\n".join(lines).strip()


if __name__ == "__main__":
    message = build_message()
    if os.getenv("DRY_RUN") == "1":
        print(message)
    else:
        send_telegram(message)
