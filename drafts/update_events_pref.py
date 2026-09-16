#!/usr/bin/env python3
"""
Draft-only events updater: city (Konan) + prefecture (Shiga).

Writes ONLY to drafts/events.html and drafts/events.json.
Does not touch live events.html, data/events.json, or Monday Actions.

Tax / transport-tax / public-discussion items matching RELATED_KEYWORDS
are routed into the 関連（交通・税） section (merged with curated related).
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
MAX_RELATED_AUTO = 8
RELATED_TAG = "関連（交通・税）"

# Route tax / transport-tax / public-discussion items into 関連（交通・税）.
# Keep narrow: do not treat every 講座・セミナー as related.
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

    # Archive page includes old years; past items are dropped below.

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

        # Skip past event days when parseable from the title.
        if event_date and event_date < TODAY:
            continue

        # No event day in title: only keep press dated today or later
        # (過去の掲載は載せない。催し日不明の古いプレスは捨てる).
        if event_date is None:
            if not list_date or list_date < TODAY:
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
            # list date is today+ only (filtered above); still mark as 掲載日
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



def related_match_text(item: dict[str, Any]) -> str:
    parts = [
        item.get("title") or "",
        item.get("note") or "",
        item.get("description") or "",
        item.get("place") or "",
    ]
    return " ".join(parts)


def is_related_candidate(item: dict[str, Any]) -> bool:
    text = related_match_text(item)
    return any(kw in text for kw in RELATED_KEYWORDS)


def split_related(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split scraped items into related candidates vs remainder (preserve order)."""
    related: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for it in items:
        if is_related_candidate(it):
            related.append(it)
        else:
            rest.append(it)
    return related, rest


def norm_url(url: str | None) -> str:
    if not url:
        return ""
    return url.strip().rstrip("/").lower()


def norm_title(title: str | None) -> str:
    if not title:
        return ""
    return " ".join(title.strip().split())


def titles_overlap(a: str, b: str) -> bool:
    """True if one normalized title contains the other (avoid みらいトーク dupes)."""
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    # Require a meaningful overlap length to avoid tiny false positives
    return len(shorter) >= 8 and shorter in longer


def as_related_item(item: dict[str, Any]) -> dict[str, Any]:
    """Retag a city/pref scrape item for the related section (HTML-friendly)."""
    clean = {k: v for k, v in item.items() if not str(k).startswith("_")}
    orig_tag = clean.get("tag") or ""
    clean["tag"] = RELATED_TAG
    if not clean.get("details"):
        details: list[str] = []
        if clean.get("dateText"):
            details.append(f"日時：{clean['dateText']}")
        if clean.get("place"):
            details.append(f"場所：{clean['place']}")
        if details:
            clean["details"] = details
    if not clean.get("sources") and clean.get("url"):
        url = clean["url"]
        if orig_tag == "滋賀県" or "pref.shiga.lg.jp" in url:
            label = "滋賀県"
        else:
            label = "湖南市"
        clean["sources"] = [{"label": label, "url": url}]
    return clean


def merge_related(
    curated: list[dict[str, Any]],
    auto_candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Curated first, then up to MAX_RELATED_AUTO scraped items.
    Deduplicate by URL and overlapping title. Returns (merged, newly_added).
    """
    merged: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_titles: list[str] = []

    def remember(it: dict[str, Any]) -> None:
        u = norm_url(it.get("url"))
        if u:
            seen_urls.add(u)
        t = norm_title(it.get("title"))
        if t:
            seen_titles.append(t)

    def is_dup(it: dict[str, Any]) -> bool:
        u = norm_url(it.get("url"))
        if u and u in seen_urls:
            return True
        t = norm_title(it.get("title"))
        if t and any(titles_overlap(t, prev) for prev in seen_titles):
            return True
        return False

    for it in curated:
        merged.append(it)
        remember(it)

    # Prefer items with a concrete event day, then earlier datetime
    def auto_sort_key(it: dict[str, Any]) -> tuple:
        d = live.parse_iso_date(it.get("datetime"))
        has_day = 0 if it.get("_has_event_day") or (d and not it.get("note")) else 1
        return (has_day, d.isoformat() if d else "9999-99-99", norm_title(it.get("title")))

    newly: list[dict[str, Any]] = []
    for it in sorted(auto_candidates, key=auto_sort_key):
        if is_dup(it):
            continue
        converted = as_related_item(it)
        newly.append(converted)
        merged.append(converted)
        remember(converted)
        if len(newly) >= MAX_RELATED_AUTO:
            break
    return merged, newly


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

    related_from_city, city_rest = split_related(scraped_city) if scraped_city else ([], [])
    if related_from_city:
        log.info("city → related candidates: %d", len(related_from_city))
    city = live.select_city_events(city_rest) if city_rest else (
        [] if scraped_city else load_fallback_city()
    )
    # If we used fallback (no scrape), still peel related keywords out of city list.
    if not scraped_city and city:
        related_from_city, city = split_related(city)

    # --- prefecture ---
    scraped_pref: list[dict[str, Any]] = []
    pref_html = fetch(PREF_EVENT_URL)
    if pref_html:
        scraped_pref = scrape_prefecture(pref_html)
        log.info("scraped %d candidate pref items (after past-date filter)", len(scraped_pref))
    else:
        log.warning("pref event page fetch failed")

    related_from_pref, pref_rest = split_related(scraped_pref) if scraped_pref else ([], [])
    if related_from_pref:
        log.info("pref → related candidates: %d", len(related_from_pref))
    prefecture = select_pref_events(pref_rest) if pref_rest else []
    if not prefecture and not scraped_pref and EVENTS_JSON.exists():
        try:
            prev = json.loads(EVENTS_JSON.read_text(encoding="utf-8"))
            prefecture = prev.get("sections", {}).get("prefecture") or []
            if prefecture:
                related_from_pref, prefecture = split_related(prefecture)
                log.warning("keeping previous draft prefecture (%d)", len(prefecture))
        except Exception:
            pass

    auto_related = related_from_city + related_from_pref
    related, newly_related = merge_related(curated.get("related", []), auto_related)
    if newly_related:
        for it in newly_related:
            log.info("  related auto: %s", it.get("title"))
    else:
        log.info("related auto: none new (curated=%d, candidates=%d)",
                 len(curated.get("related", [])), len(auto_related))

    def drop_past(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept = []
        for it in items:
            d = live.parse_iso_date(it.get("datetime"))
            if d is not None and d < TODAY:
                log.info("drop past: %s (%s)", it.get("title"), d)
                continue
            kept.append(it)
        return kept

    sections = {
        "council": drop_past(curated.get("council", [])),
        "related": drop_past(related),
        "prefecture": drop_past(prefecture),
        "city": drop_past(city),
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
        "done (draft only): council=%d related=%d prefecture=%d city=%d (related auto moved=%d)",
        len(sections["council"]),
        len(sections["related"]),
        len(sections["prefecture"]),
        len(sections["city"]),
        len(newly_related),
    )
    for it in sections["prefecture"][:5]:
        log.info("  pref sample: %s", it.get("title"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
