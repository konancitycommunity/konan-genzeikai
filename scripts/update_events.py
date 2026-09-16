#!/usr/bin/env python3
"""
Fetch public Konan city event pages and refresh AUTO sections in events.html
plus data/events.json. Does not invent events. On scrape failure with no usable
items, keeps existing data and exits 0.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
EVENTS_HTML = ROOT / "events.html"
EVENTS_JSON = ROOT / "data" / "events.json"

USER_AGENT = (
    "Mozilla/5.0 (compatible; KonanGenzeikaiBot/1.0; "
    "+https://github.com/konancitycommunity/konan-genzeikai)"
)
EVENT_SEARCH_URL = "https://www.city.shiga-konan.lg.jp/event_search.html"
CALENDAR_URL = "https://www.city.shiga-konan.lg.jp/calendar.html"

PREF_EVENT_URL = "https://www.pref.shiga.lg.jp/kensei/koho/e-shinbun/event/index.html"
PREF_BASE = "https://www.pref.shiga.lg.jp"

MAX_PREF_EVENTS = 12
MAX_RELATED_AUTO = 8
RELATED_TAG = "関連（交通・税）"

# Route transport-tax and public-discussion announcements into 関連（交通・税）.
RELATED_KEYWORDS = (
    "交通税",
    "地域交通",
    "みらいトーク",
    "タウンミーティング",
    "県民対話",
    "住民説明",
    "パブリックコメント説明会",
    "パブリックコメント",
    "討論会",
    "討論",
    "税",
)

PREF_PRIORITY_KEYWORDS = (
    "セミナー",
    "開催します",
    "参加者募集",
    "フェア",
    "トーク",
    "講座",
    "シンポジウム",
    "フェスタ",
    "イベント",
    "募集",
    "体験",
    "展示",
    "オープン",
    "ワークショップ",
    "相談会",
    "説明会",
)

PREF_DEPRIORITIZE = (
    "審議会",
    "協議会",
    "表敬",
    "卒業されました",
    "訪問！",
    "訪問しました",
    "取材いただけます",
    "記者発表",
)

JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("update_events")

# Editorial / high-value items. Kept only while still valid (end date >= today).
# Do not invent new ones here without a public source URL.
CURATED: dict[str, list[dict[str, Any]]] = {
    "council": [
        {
            "title": "令和8年9月湖南市議会定例会",
            "dateText": "会期 2026-08-27〜09-25",
            "datetime": "2026-09-25",
            "place": "湖南市役所東庁舎4階議場",
            "url": (
                "https://www.city.shiga-konan.lg.jp/soshiki/gikai_jimukyoku/"
                "giji/4/reiwa6nenteireikai_6/9gatur2_1/38943.html"
            ),
            "description": "会期は2026年8月27日〜9月25日です。傍聴可能です。",
            "note": "ネット中継あり",
            "tag": "重点",
            "priority": True,
            "details": [
                "今後の予定：9月14日（月）決算常任委員会／9月18日（金）広報広聴常任委員会／9月25日（金）本会議・委員長報告・採決",
                "開議：9:30〜",
                "場所：湖南市役所東庁舎4階議場",
            ],
            "extraHtml": (
                '<p class="event-note">ネット中継：'
                '<a class="event-source" href="https://konan-city.stream.jfit.co.jp/" '
                'target="_blank" rel="noopener">konan-city.stream.jfit.co.jp</a></p>'
            ),
        },
    ],
    "related": [
        {
            "title": "SHIGAみらいトーク in 湖南市",
            "dateText": "2026-10-18（日）13:00〜15:00",
            "datetime": "2026-10-18",
            "place": "ここぴあ",
            "url": "https://www.city.shiga-konan.lg.jp/topics/41482.html",
            "note": "中高生・大学生対象の枠組みに注意（市案内を確認）。",
            "tag": "関連（交通・税）",
            "details": [
                "日時：2026年10月18日（日）13:00〜15:00",
                "場所：ここぴあ",
            ],
            "sources": [
                {
                    "label": "滋賀県",
                    "url": "https://www.pref.shiga.lg.jp/ippan/kendoseibi/koutsu/351131.html",
                },
                {
                    "label": "湖南市",
                    "url": "https://www.city.shiga-konan.lg.jp/topics/41482.html",
                },
            ],
            "description": "滋賀県・滋賀地域交通活性化協議会による、交通税・地域交通に関する県民対話です。",
        },
    ],
}

# Prefer these keywords when selecting city events for the page (civic / culture / sports).
CITY_PRIORITY_KEYWORDS = (
    "スポーツ",
    "うたごえ",
    "フェスティバル",
    "ホール",
    "コンサート",
    "文化",
    "議会",
    "トーク",
    "税",
    "交通",
    "人権",
    "防災",
    "市民",
    "祭り",
    "フェス",
    "講座",
    "セミナー",
    "募集",
)

# Soft-deprioritize endless childcare-only listings when we have enough others.
CITY_DEPRIORITIZE = (
    "ひろば",
    "ベビー",
    "健診",
    "もぐもぐ",
    "ぴよぴよ",
    "子育てサロン",
    "乳幼児",
)

MAX_CITY_EVENTS = 12


def fetch(url: str) -> str | None:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=45)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception as e:
        log.warning("fetch failed %s: %s", url, e)
        return None


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", value.strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_jp_month_day(text: str, year: int | None = None) -> date | None:
    """Parse fragments like 09月12日 / 9月7日（月曜日） into a date (assume current/nearby year)."""
    if not text:
        return None
    y = year or TODAY.year
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    try:
        d = date(y, month, day)
    except ValueError:
        return None
    # If parsed date is more than ~5 months in the past, try next year
    if d < TODAY - timedelta(days=150):
        try:
            d = date(y + 1, month, day)
        except ValueError:
            pass
    return d


def still_valid(item: dict[str, Any]) -> bool:
    d = parse_iso_date(item.get("datetime"))
    if d is None:
        # keep if no end date known
        return True
    return d >= TODAY


def load_existing() -> dict[str, Any]:
    if not EVENTS_JSON.exists():
        return {"updatedAt": None, "sections": {"council": [], "related": [], "city": []}}
    try:
        return json.loads(EVENTS_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("could not read existing JSON: %s", e)
        return {"updatedAt": None, "sections": {"council": [], "related": [], "city": []}}


def scrape_event_search(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    area = soup.select_one(".event-result-area")
    if not area:
        log.warning("no .event-result-area found")
        return []

    items: list[dict[str, Any]] = []
    for block in area.select(":scope > .in"):
        a = block.select_one("h3.title a")
        if not a:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        href = a.get("href") or ""
        url = urljoin(EVENT_SEARCH_URL, href)

        summary = block.select_one("li.summary")
        text = summary.get_text("\n", strip=True) if summary else ""

        place = None
        pm = re.search(r"【開催場所・会場】\s*([^\n【]+)", text)
        if pm:
            place = re.sub(r"\s+", " ", pm.group(1)).strip() or None

        date_lines: list[str] = []
        if "【開催日・期間】" in text:
            after = text.split("【開催日・期間】", 1)[1]
            for line in after.split("\n"):
                line = re.sub(r"\s+", " ", line).strip()
                if line and not line.startswith("【"):
                    date_lines.append(line)

        event_date = None
        for line in date_lines:
            event_date = parse_jp_month_day(line)
            if event_date:
                break

        # Skip clearly past events
        if event_date and event_date < TODAY:
            continue

        # Prefer a line that contains 月/日 as dateText; skip boilerplate
        date_text = ""
        for line in date_lines:
            if parse_jp_month_day(line) or re.search(r"\d{1,2}\s*月", line):
                date_text = line
                break
        if not date_text and date_lines:
            # fallback only if it looks date-ish
            if any(ch.isdigit() for ch in date_lines[0]):
                date_text = date_lines[0]

        item: dict[str, Any] = {
            "title": title,
            "dateText": date_text or (event_date.isoformat() if event_date else ""),
            "place": place,
            "url": url,
            "tag": "市主催",
        }
        if event_date:
            item["datetime"] = event_date.isoformat()
        # Extra detail line only when distinct from dateText
        for line in date_lines:
            if line != item["dateText"] and line and "詳細は" not in line:
                if parse_jp_month_day(line) or "時" in line:
                    item["note"] = line
                    break
        items.append(item)

    return items


def scrape_calendar_titles(html: str) -> set[str]:
    """Best-effort: collect linked event titles from calendar (for cross-check only)."""
    soup = BeautifulSoup(html, "html.parser")
    titles: set[str] = set()
    for a in soup.find_all("a", href=True):
        t = a.get_text(strip=True)
        href = a["href"]
        if t and any(x in href for x in ("topics", "soshiki", "kosodate", "boshu", "kurashi", "iryo", "life_scene")):
            if len(t) >= 4:
                titles.add(t)
    return titles


def parse_list_date(text: str) -> date | None:
    """Parse a prefectural press-list date such as 2026年9月11日."""
    if not text:
        return None
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def scrape_prefecture(html: str) -> list[dict[str, Any]]:
    """Parse Shiga's event press list and retain current/upcoming public events."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.release_tbl")
    if not table:
        log.warning("no table.release_tbl found on prefecture event page")
        return []

    items: list[dict[str, Any]] = []
    for tr in table.select("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        list_date_text = cells[0].get_text(" ", strip=True)
        list_date = parse_list_date(list_date_text)
        link = cells[1].find("a", href=True)
        if not link:
            continue
        title = link.get_text(strip=True)
        if not title:
            continue
        url = urljoin(PREF_BASE, link["href"])
        if not url.startswith("http"):
            continue

        event_date = None
        match = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", title)
        if match:
            month, day = int(match.group(1)), int(match.group(2))
            list_year = list_date.year if list_date else TODAY.year
            for year in (list_year, list_year + 1, TODAY.year, TODAY.year + 1):
                try:
                    candidate = date(year, month, day)
                except ValueError:
                    continue
                if list_date and candidate < list_date - timedelta(days=14):
                    continue
                event_date = candidate
                break

        # An event day in the title must still be today or later. If the title
        # has no event day, use the press-list date as the freshness cutoff.
        if event_date and event_date < TODAY:
            continue
        if event_date is None and (not list_date or list_date < TODAY):
            continue

        if event_date:
            date_text = f"{event_date.month}月{event_date.day}日"
            weekday = re.search(
                r"\d{1,2}\s*月\s*\d{1,2}\s*日\s*[（(]([^）)]+)[）)]", title
            )
            if weekday:
                date_text += f"（{weekday.group(1)}）"
        else:
            date_text = list_date_text or ""

        item: dict[str, Any] = {
            "title": title,
            "dateText": date_text,
            "url": url,
            "tag": "滋賀県",
            "_listDate": list_date.isoformat() if list_date else "",
            "_sortDate": (event_date or list_date).isoformat() if (event_date or list_date) else "9999-99-99",
            "_hasEventDay": event_date is not None,
        }
        if event_date:
            item["datetime"] = event_date.isoformat()
        elif list_date:
            item["datetime"] = list_date.isoformat()
            item["note"] = "掲載日（催し日は案内をご確認ください）"
        items.append(item)
    return items


def score_pref_event(item: dict[str, Any]) -> int:
    title = item.get("title") or ""
    score = 0
    for keyword in PREF_PRIORITY_KEYWORDS:
        if keyword in title:
            score += 3
    for keyword in PREF_DEPRIORITIZE:
        if keyword in title:
            score -= 4
    if item.get("_hasEventDay"):
        score += 5
    if item.get("datetime"):
        score += 1
    event_date = parse_iso_date(item.get("datetime"))
    if event_date and event_date >= TODAY:
        score += 2
    list_date = parse_iso_date(item.get("_listDate"))
    if list_date:
        age = (TODAY - list_date).days
        if age <= 14:
            score += 3
        elif age <= 45:
            score += 1
        elif age > 90:
            score -= 3
    return score


def select_pref_events(scraped: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in scraped:
        if item["title"] in seen:
            continue
        seen.add(item["title"])
        unique.append(item)
    unique.sort(key=lambda item: (-score_pref_event(item), item.get("_sortDate") or "9999-99-99", item.get("title") or ""))
    return [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in unique[:MAX_PREF_EVENTS]
    ]


def related_match_text(item: dict[str, Any]) -> str:
    return " ".join(
        item.get(key) or "" for key in ("title", "note", "description", "place")
    )


def is_related_candidate(item: dict[str, Any]) -> bool:
    return any(keyword in related_match_text(item) for keyword in RELATED_KEYWORDS)


def split_related(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    related: list[dict[str, Any]] = []
    remainder: list[dict[str, Any]] = []
    for item in items:
        (related if is_related_candidate(item) else remainder).append(item)
    return related, remainder


def norm_url(url: str | None) -> str:
    return (url or "").strip().rstrip("/").lower()


def norm_title(title: str | None) -> str:
    return " ".join((title or "").strip().split())


def titles_overlap(first: str, second: str) -> bool:
    if not first or not second:
        return False
    shorter, longer = (first, second) if len(first) <= len(second) else (second, first)
    return shorter == longer or (len(shorter) >= 8 and shorter in longer)


def as_related_item(item: dict[str, Any]) -> dict[str, Any]:
    clean = {key: value for key, value in item.items() if not str(key).startswith("_")}
    original_tag = clean.get("tag") or ""
    clean["tag"] = RELATED_TAG
    if not clean.get("details"):
        details = []
        if clean.get("dateText"):
            details.append(f"日時：{clean['dateText']}")
        if clean.get("place"):
            details.append(f"場所：{clean['place']}")
        if details:
            clean["details"] = details
    if not clean.get("sources") and clean.get("url"):
        label = "滋賀県" if original_tag == "滋賀県" or "pref.shiga.lg.jp" in clean["url"] else "湖南市"
        clean["sources"] = [{"label": label, "url": clean["url"]}]
    return clean


def merge_related(curated: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_titles: list[str] = []

    def duplicate(item: dict[str, Any]) -> bool:
        url = norm_url(item.get("url"))
        title = norm_title(item.get("title"))
        return (bool(url) and url in seen_urls) or (bool(title) and any(titles_overlap(title, old) for old in seen_titles))

    def remember(item: dict[str, Any]) -> None:
        url = norm_url(item.get("url"))
        title = norm_title(item.get("title"))
        if url:
            seen_urls.add(url)
        if title:
            seen_titles.append(title)

    for item in curated:
        merged.append(item)
        remember(item)

    def sort_key(item: dict[str, Any]) -> tuple:
        event_date = parse_iso_date(item.get("datetime"))
        missing_day = 0 if item.get("_hasEventDay") or (event_date and not item.get("note")) else 1
        return (missing_day, event_date.isoformat() if event_date else "9999-99-99", norm_title(item.get("title")))

    added = 0
    for item in sorted(candidates, key=sort_key):
        if duplicate(item):
            continue
        converted = as_related_item(item)
        merged.append(converted)
        remember(converted)
        added += 1
        if added >= MAX_RELATED_AUTO:
            break
    return merged


def render_prefecture_html(items: list[dict[str, Any]]) -> str:
    if not items:
        return '        <div class="empty-hint"><p style="margin:0;">現在、自動取得できた滋賀県の催しはありません。</p></div>\n'
    parts = ['        <ul class="card-list">\n']
    for item in items:
        parts.extend([
            "          <li>\n",
            '            <article class="card card--compact">\n',
            '              <div class="card-meta">\n',
            '                <span class="tag">滋賀県</span>\n',
        ])
        if item.get("dateText"):
            parts.append(f'                <span>{esc(item["dateText"])}</span>\n')
        parts.extend([
            "              </div>\n",
            f'              <h3>{esc(item["title"])}</h3>\n',
        ])
        bits = []
        if item.get("note"):
            bits.append(esc(item["note"]))
        if item.get("url"):
            bits.append(f'出典：<a class="event-source" href="{esc(item["url"])}" target="_blank" rel="noopener">滋賀県</a>')
        if bits:
            parts.append(f'              <p>{"／".join(bits)}</p>\n')
        parts.extend(["            </article>\n", "          </li>\n"])
    parts.append("        </ul>\n")
    return "".join(parts)


def score_city_event(item: dict[str, Any]) -> int:
    title = item.get("title") or ""
    score = 0
    for kw in CITY_PRIORITY_KEYWORDS:
        if kw in title:
            score += 3
    for kw in CITY_DEPRIORITIZE:
        if kw in title:
            score -= 2
    # Prefer items with place + date
    if item.get("place"):
        score += 1
    if item.get("datetime"):
        score += 1
    return score


def select_city_events(scraped: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Dedupe by title
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for it in scraped:
        key = it["title"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)

    unique.sort(
        key=lambda x: (
            -score_city_event(x),
            x.get("datetime") or "9999-99-99",
            x.get("title") or "",
        )
    )
    return unique[:MAX_CITY_EVENTS]


def curated_sections() -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for key, items in CURATED.items():
        kept = []
        for it in items:
            if still_valid(it):
                # strip internal-only keys for JSON later
                kept.append(it)
            else:
                log.info("dropping expired curated %s: %s", key, it.get("title"))
        out[key] = kept
    return out


def sanitize_for_json(item: dict[str, Any]) -> dict[str, Any]:
    """Public JSON fields only."""
    keys = ("title", "dateText", "datetime", "place", "url", "note", "tag")
    return {k: item[k] for k in keys if item.get(k) not in (None, "")}


def esc(s: str) -> str:
    return html_lib.escape(s, quote=True)


def render_council_html(items: list[dict[str, Any]]) -> str:
    if not items:
        return '        <div class="empty-hint"><p style="margin:0;">現在、掲載できる議会関連の予定はありません。</p></div>\n'
    parts = ['        <ul class="card-list">\n']
    for it in items:
        card_class = "card card--priority" if it.get("priority") or it.get("tag") == "重点" else "card"
        parts.append("          <li>\n")
        parts.append(f'            <article class="{card_class}">\n')
        parts.append('              <div class="card-meta">\n')
        if it.get("tag") == "重点" or it.get("priority"):
            parts.append('                <span class="tag tag--priority">重点</span>\n')
        parts.append('                <span class="tag">議員・議会</span>\n')
        if it.get("dateText"):
            parts.append(f'                <span>{esc(it["dateText"])}</span>\n')
        parts.append("              </div>\n")
        parts.append(f'              <h3>{esc(it["title"])}</h3>\n')
        if it.get("description"):
            parts.append(f'              <p>{esc(it["description"])}</p>\n')
        elif it.get("note") and not it.get("details"):
            parts.append(f'              <p>{esc(it["note"])}</p>\n')
        if it.get("details"):
            parts.append('              <ul class="event-details">\n')
            for d in it["details"]:
                # details may contain "label：value"
                if "：" in d:
                    label, rest = d.split("：", 1)
                    parts.append(
                        f"                <li><strong>{esc(label)}：</strong>{esc(rest)}</li>\n"
                    )
                else:
                    parts.append(f"                <li>{esc(d)}</li>\n")
            parts.append("              </ul>\n")
        if it.get("extraHtml"):
            parts.append(f'              {it["extraHtml"]}\n')
        if it.get("url"):
            parts.append(
                '              <p class="event-source-line">出典：'
                f'<a class="event-source" href="{esc(it["url"])}" target="_blank" rel="noopener">'
                "湖南市議会事務局（定例会案内）</a></p>\n"
            )
        parts.append("            </article>\n")
        parts.append("          </li>\n")
    parts.append("        </ul>\n")
    return "".join(parts)


def render_related_html(items: list[dict[str, Any]]) -> str:
    if not items:
        return '        <div class="empty-hint"><p style="margin:0;">現在、掲載できる関連イベントはありません。</p></div>\n'
    parts = ['        <ul class="card-list">\n']
    for it in items:
        parts.append("          <li>\n")
        parts.append('            <article class="card">\n')
        parts.append('              <div class="card-meta">\n')
        parts.append(
            f'                <span class="tag">{esc(it.get("tag") or "関連（交通・税）")}</span>\n'
        )
        if it.get("dateText"):
            parts.append(f'                <span>{esc(it["dateText"])}</span>\n')
        parts.append("              </div>\n")
        parts.append(f'              <h3>{esc(it["title"])}</h3>\n')
        if it.get("description"):
            parts.append(f'              <p>{esc(it["description"])}</p>\n')
        if it.get("details"):
            parts.append('              <ul class="event-details">\n')
            for d in it["details"]:
                if "：" in d:
                    label, rest = d.split("：", 1)
                    parts.append(
                        f"                <li><strong>{esc(label)}：</strong>{esc(rest)}</li>\n"
                    )
                else:
                    parts.append(f"                <li>{esc(d)}</li>\n")
            parts.append("              </ul>\n")
        if it.get("note"):
            parts.append(f'              <p class="event-note">{esc(it["note"])}</p>\n')
        sources = it.get("sources") or []
        if sources:
            links = " ／ ".join(
                f'<a class="event-source" href="{esc(s["url"])}" target="_blank" rel="noopener">'
                f'{esc(s["label"])}</a>'
                for s in sources
            )
            parts.append(f'              <p class="event-source-line">出典：\n                {links}\n              </p>\n')
        elif it.get("url"):
            parts.append(
                '              <p class="event-source-line">出典：'
                f'<a class="event-source" href="{esc(it["url"])}" target="_blank" rel="noopener">出典</a></p>\n'
            )
        parts.append("            </article>\n")
        parts.append("          </li>\n")
    parts.append("        </ul>\n")
    return "".join(parts)


def render_city_html(items: list[dict[str, Any]]) -> str:
    if not items:
        return '        <div class="empty-hint"><p style="margin:0;">現在、自動取得できた市主催イベントはありません。</p></div>\n'
    parts = ['        <ul class="card-list">\n']
    for it in items:
        parts.append("          <li>\n")
        parts.append('            <article class="card card--compact">\n')
        parts.append('              <div class="card-meta">\n')
        parts.append('                <span class="tag">市主催</span>\n')
        if it.get("dateText"):
            parts.append(f'                <span>{esc(it["dateText"])}</span>\n')
        parts.append("              </div>\n")
        parts.append(f'              <h3>{esc(it["title"])}</h3>\n')
        bits = []
        if it.get("place"):
            bits.append(esc(it["place"]))
        if it.get("url"):
            bits.append(
                f'出典：<a class="event-source" href="{esc(it["url"])}" '
                f'target="_blank" rel="noopener">湖南市</a>'
            )
        if bits:
            parts.append(f'              <p>{"／".join(bits)}</p>\n')
        parts.append("            </article>\n")
        parts.append("          </li>\n")
    parts.append("        </ul>\n")
    return "".join(parts)


def replace_auto_section(html: str, name: str, inner: str) -> str:
    start = f"<!-- AUTO:{name} -->"
    end = f"<!-- /AUTO:{name} -->"
    i0 = html.find(start)
    i1 = html.find(end)
    if i0 < 0 or i1 < 0 or i1 < i0:
        raise RuntimeError(f"AUTO markers for {name} not found in events.html")
    # keep markers; replace content between
    return html[: i0 + len(start)] + "\n" + inner + html[i1:]


def write_json(sections: dict[str, list[dict[str, Any]]]) -> None:
    payload = {
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sections": {
            "council": [sanitize_for_json(x) for x in sections.get("council", [])],
            "related": [sanitize_for_json(x) for x in sections.get("related", [])],
            "prefecture": [sanitize_for_json(x) for x in sections.get("prefecture", [])],
            "city": [sanitize_for_json(x) for x in sections.get("city", [])],
        },
    }
    EVENTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    EVENTS_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log.info("wrote %s", EVENTS_JSON.relative_to(ROOT))


def update_html(sections: dict[str, list[dict[str, Any]]]) -> None:
    html = EVENTS_HTML.read_text(encoding="utf-8")
    html = replace_auto_section(html, "council", render_council_html(sections["council"]))
    html = replace_auto_section(html, "related", render_related_html(sections["related"]))
    html = replace_auto_section(html, "prefecture", render_prefecture_html(sections["prefecture"]))
    html = replace_auto_section(html, "city", render_city_html(sections["city"]))
    EVENTS_HTML.write_text(html, encoding="utf-8")
    log.info("updated AUTO sections in events.html")


def main() -> int:
    existing = load_existing()
    curated = curated_sections()

    # --- city ---
    scraped_city: list[dict[str, Any]] = []
    city_html = fetch(EVENT_SEARCH_URL)
    if city_html:
        scraped_city = scrape_event_search(city_html)
        log.info("scraped %d upcoming city events", len(scraped_city))
    else:
        log.warning("city event_search fetch failed")

    if scraped_city:
        related_from_city, city_rest = split_related(scraped_city)
        city = select_city_events(city_rest)
    else:
        previous_city = existing.get("sections", {}).get("city") or []
        related_from_city, city = split_related(previous_city)
        log.warning("keeping previous city (%d items)", len(city))

    # Optional calendar fetch (cross-check / logging only).
    calendar_html = fetch(CALENDAR_URL)
    if calendar_html:
        log.info("calendar page had %d linked titles (info only)", len(scrape_calendar_titles(calendar_html)))

    # --- prefecture ---
    scraped_pref: list[dict[str, Any]] = []
    pref_html = fetch(PREF_EVENT_URL)
    if pref_html:
        scraped_pref = scrape_prefecture(pref_html)
        log.info("scraped %d candidate prefecture items (after past-date filter)", len(scraped_pref))
    else:
        log.warning("prefecture event page fetch failed")

    if scraped_pref:
        related_from_pref, pref_rest = split_related(scraped_pref)
        prefecture = select_pref_events(pref_rest)
    else:
        previous_pref = existing.get("sections", {}).get("prefecture") or []
        related_from_pref, prefecture = split_related(previous_pref)
        if previous_pref:
            log.warning("keeping previous prefecture (%d items)", len(prefecture))

    related = merge_related(curated.get("related", []), related_from_city + related_from_pref)

    def drop_past(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept = []
        for item in items:
            event_date = parse_iso_date(item.get("datetime"))
            if event_date is not None and event_date < TODAY:
                log.info("drop past: %s (%s)", item.get("title"), event_date)
                continue
            kept.append(item)
        return kept

    sections = {
        "council": drop_past(curated.get("council", [])),
        "related": drop_past(related),
        "prefecture": drop_past(prefecture),
        "city": drop_past(city),
    }

    if not any(sections.values()):
        log.warning("nothing to write; leaving files unchanged")
        return 0

    try:
        write_json(sections)
        update_html(sections)
    except Exception:
        log.exception("failed to write outputs")
        return 1

    log.info(
        "done: council=%d related=%d prefecture=%d city=%d",
        len(sections["council"]),
        len(sections["related"]),
        len(sections["prefecture"]),
        len(sections["city"]),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
