#!/usr/bin/env python3
"""
Draft-only events updater: city (Konan) + prefecture (Shiga).

Writes ONLY to drafts/events.html and drafts/events.json.
Does not touch live events.html, data/events.json, or Monday Actions.
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
DRAFTS = Path(__file__).resolve().parent
EVENTS_HTML = DRAFTS / "events.html"
EVENTS_JSON = DRAFTS / "events.json"
LIVE_EVENTS_JSON = ROOT / "data" / "events.json"

# Reuse city scrape helpers from the live updater without writing live files.
sys.path.insert(0, str(ROOT / "scripts"))
import update_events as live  # noqa: E402

USER_AGENT = live.USER_AGENT
PREF_EVENT_URL = "https://www.pref.shiga.lg.jp/kensei/koho/e-shinbun/event/index.html"
PREF_BASE = "https://www.pref.shiga.lg.jp"

JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("update_events_pref_draft")

MAX_PREF_EVENTS = 12

# Prefer civic / public-facing announcements.
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

# Soft-deprioritize pure meeting / courtesy / alumni notices.
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


def fetch(url: str) -> str | None:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=45)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception as e:
        log.warning("fetch failed %s: %s", url, e)
        return None


def parse_list_date(text: str) -> date | None:
    """Parse list cell like 2026年9月11日."""
    if not text:
        return None
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_event_date_from_title(title: str, list_year: int | None = None) -> date | None:
    """Parse event day from titles like 10月24日 / 9月19日（土曜日）."""
    return live.parse_jp_month_day(title, year=list_year or TODAY.year)


def score_pref_event(item: dict[str, Any]) -> int:
    title = item.get("title") or ""
    score = 0
    for kw in PREF_PRIORITY_KEYWORDS:
        if kw in title:
            score += 3
    for kw in PREF_DEPRIORITIZE:
        if kw in title:
            score -= 4
    if item.get("_has_event_day"):
        score += 5
    if item.get("datetime"):
        score += 1
    d = live.parse_iso_date(item.get("datetime"))
    if d and d >= TODAY:
        score += 2
    # Fresher press-list dates win when otherwise tied
    ld = live.parse_iso_date(item.get("_listDate"))
    if ld:
        age = (TODAY - ld).days
        if age <= 14:
            score += 3
        elif age <= 45:
            score += 1
        elif age > 90:
            score -= 3
    return score


def scrape_prefecture(html: str) -> list[dict[str, Any]]:
    """Parse table.release_tbl (date + title + link). Prefer upcoming civic events."""
    soup = BeautifulSoup(html, "html.parser")
    tbl = soup.select_one("table.release_tbl")
    if not tbl:
        log.warning("no table.release_tbl found on pref event page")
        return []

    # Archive page includes old years; drop stale press without a clear future event day.
    stale_list_cutoff = TODAY - timedelta(days=35)

    items: list[dict[str, Any]] = []
    for tr in tbl.select("tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        list_date_text = tds[0].get_text(" ", strip=True)
        list_date = parse_list_date(list_date_text)
        a = tds[1].find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        href = a["href"]
        url = urljoin(PREF_BASE, href)
        if not url.startswith("http"):
            continue

        list_year = list_date.year if list_date else TODAY.year
        # Prefer year anchored to the press-list date (avoid inventing far-future years).
        event_date = None
        m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", title)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
            for y in (list_year, list_year + 1, TODAY.year, TODAY.year + 1):
                try:
                    cand = date(y, month, day)
                except ValueError:
                    continue
                # Event should not be long before the press date
                if list_date and cand < list_date - timedelta(days=14):
                    continue
                event_date = cand
                break
            # If still none, fall back to shared helper
            if event_date is None:
                event_date = parse_event_date_from_title(title, list_year=list_year)

        # Skip clearly past *event* dates when parseable from the title.
        if event_date and event_date < TODAY:
            continue

        # No event day in title: keep only relatively fresh press releases.
        if event_date is None and list_date and list_date < stale_list_cutoff:
            continue

        # dateText: prefer event day in title; else press-list date
        if event_date:
            date_text = f"{event_date.month}月{event_date.day}日"
            wd = re.search(
                r"\d{1,2}\s*月\s*\d{1,2}\s*日\s*[（(]([^）)]+)[）)]",
                title,
            )
            if wd:
                date_text = f"{date_text}（{wd.group(1)}）"
        else:
            date_text = list_date_text or ""

        sort_date = event_date or list_date
        has_event_day = event_date is not None

        item: dict[str, Any] = {
            "title": title,
            "dateText": date_text,
            "url": url,
            "tag": "滋賀県",
            "_listDate": list_date.isoformat() if list_date else "",
            "_score_sort": sort_date.isoformat() if sort_date else "9999-99-99",
            "_has_event_day": has_event_day,
        }
        if event_date:
            item["datetime"] = event_date.isoformat()
        elif list_date:
            item["datetime"] = list_date.isoformat()
            item["note"] = "掲載日（催し日は案内をご確認ください）"

        items.append(item)

    return items


def select_pref_events(scraped: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
            -score_pref_event(x),
            x.get("_score_sort") or "9999-99-99",
            x.get("title") or "",
        )
    )
    chosen = unique[:MAX_PREF_EVENTS]
    # Drop internal keys
    out: list[dict[str, Any]] = []
    for it in chosen:
        clean = {k: v for k, v in it.items() if not k.startswith("_")}
        out.append(clean)
    return out


def esc(s: str) -> str:
    return html_lib.escape(s, quote=True)


def render_prefecture_html(items: list[dict[str, Any]]) -> str:
    if not items:
        return (
            '        <div class="empty-hint">'
            '<p style="margin:0;">現在、自動取得できた滋賀県の催しはありません。</p></div>\n'
        )
    parts = ['        <ul class="card-list">\n']
    for it in items:
        parts.append("          <li>\n")
        parts.append('            <article class="card card--compact">\n')
        parts.append('              <div class="card-meta">\n')
        parts.append('                <span class="tag">滋賀県</span>\n')
        if it.get("dateText"):
            parts.append(f'                <span>{esc(it["dateText"])}</span>\n')
        parts.append("              </div>\n")
        parts.append(f'              <h3>{esc(it["title"])}</h3>\n')
        bits: list[str] = []
        if it.get("note"):
            bits.append(esc(it["note"]))
        if it.get("url"):
            bits.append(
                f'出典：<a class="event-source" href="{esc(it["url"])}" '
                f'target="_blank" rel="noopener">滋賀県</a>'
            )
        if bits:
            parts.append(f'              <p>{"／".join(bits)}</p>\n')
        parts.append("            </article>\n")
        parts.append("          </li>\n")
    parts.append("        </ul>\n")
    return "".join(parts)


def sanitize_for_json(item: dict[str, Any]) -> dict[str, Any]:
    keys = ("title", "dateText", "datetime", "place", "url", "note", "tag")
    return {k: item[k] for k in keys if item.get(k) not in (None, "")}


def replace_auto_section(html: str, name: str, inner: str) -> str:
    start = f"<!-- AUTO:{name} -->"
    end = f"<!-- /AUTO:{name} -->"
    i0 = html.find(start)
    i1 = html.find(end)
    if i0 < 0 or i1 < 0 or i1 < i0:
        raise RuntimeError(f"AUTO markers for {name} not found in {EVENTS_HTML.name}")
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
    EVENTS_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log.info("wrote %s", EVENTS_JSON.relative_to(ROOT))


def update_html(sections: dict[str, list[dict[str, Any]]]) -> None:
    if not EVENTS_HTML.exists():
        raise FileNotFoundError(f"missing {EVENTS_HTML}")
    html = EVENTS_HTML.read_text(encoding="utf-8")
    html = replace_auto_section(html, "council", live.render_council_html(sections["council"]))
    html = replace_auto_section(html, "related", live.render_related_html(sections["related"]))
    html = replace_auto_section(html, "prefecture", render_prefecture_html(sections["prefecture"]))
    html = replace_auto_section(html, "city", live.render_city_html(sections["city"]))
    EVENTS_HTML.write_text(html, encoding="utf-8")
    log.info("updated AUTO sections in drafts/events.html (live files untouched)")


def load_fallback_city() -> list[dict[str, Any]]:
    for path in (EVENTS_JSON, LIVE_EVENTS_JSON):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            city = data.get("sections", {}).get("city") or []
            if city:
                log.info("fallback city from %s (%d)", path.relative_to(ROOT), len(city))
                return city
        except Exception as e:
            log.warning("could not read %s: %s", path, e)
    return []


def main() -> int:
    curated = live.curated_sections()

    # --- city (same source as live) ---
    scraped_city: list[dict[str, Any]] = []
    city_html = fetch(live.EVENT_SEARCH_URL)
    if city_html:
        scraped_city = live.scrape_event_search(city_html)
        log.info("scraped %d upcoming city events", len(scraped_city))
    else:
        log.warning("city event_search fetch failed")

    city = live.select_city_events(scraped_city) if scraped_city else load_fallback_city()

    # --- prefecture ---
    scraped_pref: list[dict[str, Any]] = []
    pref_html = fetch(PREF_EVENT_URL)
    if pref_html:
        scraped_pref = scrape_prefecture(pref_html)
        log.info("scraped %d candidate pref items (after past-date filter)", len(scraped_pref))
    else:
        log.warning("pref event page fetch failed")

    prefecture = select_pref_events(scraped_pref) if scraped_pref else []
    if not prefecture and EVENTS_JSON.exists():
        try:
            prev = json.loads(EVENTS_JSON.read_text(encoding="utf-8"))
            prefecture = prev.get("sections", {}).get("prefecture") or []
            if prefecture:
                log.warning("keeping previous draft prefecture (%d)", len(prefecture))
        except Exception:
            pass

    sections = {
        "council": curated.get("council", []),
        "related": curated.get("related", []),
        "prefecture": prefecture,
        "city": city,
    }

    if not any(sections.values()):
        log.warning("nothing to write; leaving draft files unchanged")
        return 0

    try:
        write_json(sections)
        update_html(sections)
    except Exception:
        log.exception("failed to write draft outputs")
        return 1

    log.info(
        "done (draft only): council=%d related=%d prefecture=%d city=%d",
        len(sections["council"]),
        len(sections["related"]),
        len(sections["prefecture"]),
        len(sections["city"]),
    )
    for it in sections["prefecture"][:5]:
        log.info("  pref sample: %s", it.get("title"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
