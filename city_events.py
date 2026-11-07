#!/usr/bin/env python3
"""Evening Astana theatre and cinema guide for the existing Telegram bot.

Uses public listing pages only. It does not scrape tickets behind a login and
does not claim that tickets are available when a listing says otherwise.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Almaty")
UA = "NewsDigestAstanaEvents/1.0 (+https://github.com/sayupov/news-digest)"
THEATRE_URL = "https://teatr.kz/ru/astana"
CINEMA_URL = "https://kz.kinoafisha.info/astana/movies/"


def fetch(url: str, timeout: int = 25) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru-KZ,ru;q=0.9"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read(2_000_000).decode(charset, errors="replace")


def plain(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def theatre_items(page: str) -> list[dict]:
    """Extract the visible Astana schedule from teatr.kz."""
    text = plain(page)
    pattern = re.compile(r"(?P<time>\d{1,2}:\d{2})\s+(?P<title>.{2,130}?)\s+\d{1,2}\+\s+(?P<place>.{2,150}?)(?=\s+·\s+|\s+Билеты|$)")
    today_start = text.find("Сегодня ·")
    tomorrow_start = text.find("Завтра ·")
    now_time = datetime.now(TZ).time()
    result: list[dict] = []
    seen: set[str] = set()
    for match in pattern.finditer(text):
        is_today = today_start >= 0 and match.start() >= today_start and (tomorrow_start < 0 or match.start() < tomorrow_start)
        hour, minute = map(int, match.group("time").split(":"))
        if is_today and (hour, minute) <= (now_time.hour, now_time.minute):
            continue
        title = match.group("title").strip(" -–:.")
        if not 3 <= len(title) <= 130 or title.casefold() in seen:
            continue
        seen.add(title.casefold())
        result.append({"category": "Театр", "title": title, "when": match.group("time"),
                       "place": match.group("place").strip(), "price": "цену уточните", "url": THEATRE_URL})
    return result


def cinema_items(page: str) -> list[dict]:
    """Use Kinoafisha's server-rendered movie list and direct schedule links."""
    prefix = re.escape('"url":"https:\\/\\/www.kinoafisha.info\\/movies\\/')
    pattern = re.compile(prefix + r'(\d+)\\/","name":"([^"]+)"')
    result: list[dict] = []
    seen: set[str] = set()
    for movie_id, title in pattern.findall(page):
        title = html.unescape(title).strip()
        if not title or title.casefold() in seen:
            continue
        seen.add(title.casefold())
        result.append({"category": "Кино", "title": title, "when": "ближайшие сеансы",
                       "place": "кинотеатры Астаны", "price": "цену уточните",
                       "url": f"https://kz.kinoafisha.info/astana/movies/{movie_id}/"})
    return result


def weather() -> str:
    try:
        url = "https://api.open-meteo.com/v1/forecast?latitude=51.1694&longitude=71.4491&daily=weather_code,temperature_2m_max,temperature_2m_min&timezone=Asia%2FAlmaty&forecast_days=2"
        data = json.loads(fetch(url))
        daily = data["daily"]
        high, low = round(daily["temperature_2m_max"][0]), round(daily["temperature_2m_min"][0])
        code = daily["weather_code"][0]
        label = "осадки возможны" if code >= 51 else "без существенных осадков"
        return f"Погода в Астане: {low:+d}…{high:+d} °C, {label}."
    except (KeyError, TypeError, ValueError, urllib.error.URLError, TimeoutError):
        return "Прогноз погоды сейчас недоступен, проверьте его перед выходом."


def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not chat or not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise RuntimeError("TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы")
    body = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=35) as response:
        if not json.loads(response.read()).get("ok"):
            raise RuntimeError("Telegram отклонил сообщение")


def build_message() -> str:
    items: list[dict] = []
    errors: list[str] = []
    try:
        items.extend(theatre_items(fetch(THEATRE_URL)))
    except (urllib.error.URLError, TimeoutError, UnicodeError) as exc:
        errors.append(f"театр: {type(exc).__name__}")
    try:
        items.extend(cinema_items(fetch(CINEMA_URL)))
    except (urllib.error.URLError, TimeoutError, UnicodeError) as exc:
        errors.append(f"кино: {type(exc).__name__}")
    # Prefer a balanced set and then fill remaining places in listing order.
    chosen: list[dict] = []
    for category in ("Театр", "Кино"):
        chosen.extend([item for item in items if item["category"] == category][:2])
    for item in items:
        if len(chosen) >= 5:
            break
        if item not in chosen:
            chosen.append(item)
    now = datetime.now(TZ).strftime("%d.%m.%Y")
    lines = ["АФИША АСТАНЫ", f"Дата: {now}", weather(), ""]
    if not chosen:
        lines.append("Не удалось получить актуальные карточки афиши. Откройте театральную и киноафишу по ссылкам ниже.")
    for number, item in enumerate(chosen, 1):
        why = "вариант для культурного вечера" if item["category"] == "Театр" else "доступный вариант в помещении"
        lines += [f"{number}. {item['title']}", f"   {item['category']} · {item['when']} · {item['place']} · {item['price']}", f"   Почему: {why}.", f"   {item['url']}", ""]
    lines.append("Проверьте наличие мест и точное время по ссылке перед оплатой.")
    if errors:
        lines.append("Не открылись источники: " + ", ".join(errors) + ".")
    return "\n".join(lines).strip()


if __name__ == "__main__":
    message = build_message()
    if os.getenv("DRY_RUN") == "1":
        print(message)
    else:
        send_telegram(message)
