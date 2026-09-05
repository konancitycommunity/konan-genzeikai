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
                "今後の予定：9月7日（月）一般質問・質疑等・委員会付託／9月25日（金）委員長報告・採決",
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
    html = replace_auto_section(html, "city", render_city_html(sections["city"]))
    EVENTS_HTML.write_text(html, encoding="utf-8")
    log.info("updated AUTO sections in events.html")


def main() -> int:
    existing = load_existing()
    curated = curated_sections()

    scraped: list[dict[str, Any]] = []
    html = fetch(EVENT_SEARCH_URL)
    if html:
        scraped = scrape_event_search(html)
        log.info("scraped %d upcoming events from event_search", len(scraped))
    else:
        log.warning("event_search fetch failed")

    # Optional calendar fetch (cross-check / logging only)
    cal = fetch(CALENDAR_URL)
    if cal:
        titles = scrape_calendar_titles(cal)
        log.info("calendar page had %d linked titles (info only)", len(titles))

    city = select_city_events(scraped) if scraped else []

    if not scraped:
        # Keep previous city section; still refresh curated if valid
        prev_city = existing.get("sections", {}).get("city") or []
        log.warning(
            "zero scraped events; keeping previous city (%d items), refreshing curated only",
            len(prev_city),
        )
        sections = {
            "council": curated.get("council", []),
            "related": curated.get("related", []),
            "city": prev_city,
        }
        # If we have nothing at all and no previous, do not wipe HTML editorial — exit 0
        if not any(sections.values()):
            log.warning("nothing to write; leaving files unchanged")
            return 0
        try:
            write_json(sections)
            update_html(sections)
        except Exception:
            log.exception("failed while writing kept/curated data")
            return 1
        return 0

    sections = {
        "council": curated.get("council", []),
        "related": curated.get("related", []),
        "city": city,
    }

    try:
        write_json(sections)
        update_html(sections)
    except Exception:
        log.exception("failed to write outputs")
        return 1

    log.info(
        "done: council=%d related=%d city=%d",
        len(sections["council"]),
        len(sections["related"]),
        len(sections["city"]),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
