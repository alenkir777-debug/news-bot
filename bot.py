"""
Telegram-бот: раз в день публикует в канал одну свежую новость о гемблинге.

Источники: RSS-ленты отраслевых изданий (список в FEEDS).
Если задан ANTHROPIC_API_KEY, новость пересказывается на русском языке через Claude.
Без ключа публикуется оригинальный заголовок, краткое описание и ссылка.

Переменные окружения:
  TELEGRAM_BOT_TOKEN  — токен бота от @BotFather (обязательно)
  TELEGRAM_CHANNEL_ID — @username канала или числовой id, например -1001234567890 (обязательно)
  ANTHROPIC_API_KEY   — ключ Claude API для пересказа на русском (необязательно)
"""

import html
import json
import os
import re
import sys
import time
import calendar
from pathlib import Path

import feedparser
import requests

FEEDS = [
    "https://igamingbusiness.com/feed/",
    "https://sbcnews.co.uk/feed/",
    "https://www.casinobeats.com/feed/",
    "https://www.legalsportsreport.com/feed/",
    "https://focusgn.com/feed",
]

STATE_FILE = Path(__file__).with_name("posted.json")
MAX_AGE_HOURS = 72          # не брать новости старше трёх суток
MAX_REMEMBERED = 500        # сколько опубликованных ссылок хранить
CLAUDE_MODEL = "claude-haiku-5-5"


def load_posted() -> list[str]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_posted(posted: list[str]) -> None:
    STATE_FILE.write_text(
        json.dumps(posted[-MAX_REMEMBERED:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clean_text(raw: str, limit: int = 600) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def collect_entries() -> list[dict]:
    now = time.time()
    entries = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url, agent="Mozilla/5.0 (news-bot)")
        except Exception as e:  # лента недоступна — просто пропускаем
            print(f"[warn] {url}: {e}", file=sys.stderr)
            continue
        source = feed.feed.get("title", url)
        for e in feed.entries:
            ts = e.get("published_parsed") or e.get("updated_parsed")
            published = calendar.timegm(ts) if ts else now
            if now - published > MAX_AGE_HOURS * 3600:
                continue
            link = e.get("link")
            if not link:
                continue
            entries.append({
                "title": clean_text(e.get("title", ""), 200),
                "summary": clean_text(e.get("summary", "")),
                "link": link,
                "source": source,
                "published": published,
            })
    entries.sort(key=lambda x: x["published"], reverse=True)
    return entries


def summarize_ru(entry: dict) -> tuple[str, str] | None:
    """Возвращает (заголовок, текст) на русском или None, если ключа нет/ошибка."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    prompt = (
        "Перескажи новость для Telegram-канала об индустрии гемблинга. "
        "Пиши по-русски, нейтрально и своими словами, без рекламы казино и призывов играть. "
        "Ответь строго в формате JSON: {\"title\": \"короткий заголовок\", "
        "\"text\": \"2–3 предложения сути\"}.\n\n"
        f"Заголовок: {entry['title']}\nОписание: {entry['summary']}\nИсточник: {entry['source']}"
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        r.raise_for_status()
        raw = r.json()["content"][0]["text"]
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        return data["title"].strip(), data["text"].strip()
    except Exception as e:
        print(f"[warn] пересказ не удался, публикую оригинал: {e}", file=sys.stderr)
        return None


def format_post(entry: dict) -> str:
    ru = summarize_ru(entry)
    title, text = ru if ru else (entry["title"], entry["summary"])
    esc = html.escape
    parts = [f"<b>{esc(title)}</b>"]
    if text:
        parts.append(esc(text))
    parts.append(f'<a href="{esc(entry["link"], quote=True)}">Источник: {esc(entry["source"])}</a>')
    return "\n\n".join(parts)


def send_to_telegram(message: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHANNEL_ID"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram API {r.status_code}: {r.text}")


def main() -> int:
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID"):
        if not os.environ.get(var):
            print(f"Не задана переменная {var}", file=sys.stderr)
            return 1

    posted = load_posted()
    fresh = [e for e in collect_entries() if e["link"] not in posted]
    if not fresh:
        print("Новых новостей нет — сегодня ничего не публикую.")
        return 0

    entry = fresh[0]
    send_to_telegram(format_post(entry))
    posted.append(entry["link"])
    save_posted(posted)
    print(f"Опубликовано: {entry['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
