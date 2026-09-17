#!/usr/bin/env python3
"""
Fetch official Konan / Shiga public-comment indexes and refresh
AUTO sections in public-comment.html plus data/public-comment.json.

Does not invent cases. On total scrape failure, keeps existing JSON and exits 0.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import logging
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

ROOT = Path(__file__).resolve().parents[1]
PC_HTML = ROOT / "public-comment.html"
PC_JSON = ROOT / "data" / "public-comment.json"

USER_AGENT = (
    "Mozilla/5.0 (compatible; KonanGenzeikaiBot/1.0; "
    "+https://github.com/konancitycommunity/konan-genzeikai)"
)

KONAN_INDEX = (
    "https://www.city.shiga-konan.lg.jp/shisei/kocho/paburikku_komento/index.html"
)
KONAN_R7 = (
    "https://www.city.shiga-konan.lg.jp/shisei/kocho/paburikku_komento/r7/index.html"
)
SHIGA_INDEX = "https://www.pref.shiga.lg.jp/bj00/10105.html"
SHIGA_INDEX_ALT = "https://www.pref.shiga.lg.jp/kensei/kenseisanka/22148.html"

MAX_ORG = 8

JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("update_public_comment")

SOURCES = [
    {
        "name": "湖南市 パブリックコメント（公式一覧）",
        "url": KONAN_INDEX,
    },
    {
        "name": "滋賀県 意見募集（県民政策コメント）公式一覧",
        "url": SHIGA_INDEX,
    },
]


def fetch(url: str) -> tuple[str | None, str | None]:
    """Return (html, final_url) or (None, None)."""
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=45,
            allow_redirects=True,
        )
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text, r.url
    except Exception as e:
        log.warning("fetch failed %s: %s", url, e)
        return None, None


def head_ok(url: str) -> bool:
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=20,
            allow_redirects=True,
        )
        return r.status_code == 200
    except Exception:
        return False


def esc(s: str) -> str:
    return html_lib.escape(s, quote=True)


def normalize_title(title: str) -> str:
    t = (title or "").strip()
    # Light cleanup only — avoid NFKC so ～「」 stay readable.
    t = t.replace("(案)", "（案）").replace("(素案)", "（素案）")
    t = t.replace("~", "～")
    return re.sub(r"\s+", " ", t).strip()


def slugify(title: str, org: str) -> str:
    base = normalize_title(title)
    # Prefer readable ascii-ish id from existing style when possible
    ascii_map = {
        "湖南市都市計画マスタープラン 第3版（案）": "konan-city-planning-master-plan-3",
        "湖南市建築物耐震改修促進計画（案）": "konan-seismic-retrofit-plan",
        "湖南市空家等対策計画（案）": "konan-vacant-house-plan",
        "第五次湖南市行政改革大綱（案）": "konan-5th-administrative-reform",
        "第12次滋賀県交通安全計画（素案）": "shiga-12th-traffic-safety-plan",
        "県税賦課徴収事務の特定個人情報保護評価書（案）": "shiga-tax-specific-personal-information-assessment",
        "滋賀県障害者差別のない共生社会づくり条例の一部を改正する条例案": "shiga-disability-equality-ordinance-amendment",
        "滋賀県道路脱炭素化推進計画（素案）": "shiga-road-decarbonization-plan",
    }
    if base in ascii_map:
        return ascii_map[base]
    prefix = "konan" if org == "湖南市" else "shiga"
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{digest}"


_WAREKI = {
    "令和": 2018,
    "平成": 1988,
    "昭和": 1925,
}


def _wareki_to_year(era: str, n: int) -> int:
    return _WAREKI[era] + n


def parse_jp_date(text: str) -> date | None:
    """Parse a single Japanese or ISO date from text."""
    if not text:
        return None
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r"[（(][^）)]*[）)]", "", t)  # drop weekday
    t = t.replace(" ", "")

    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", t)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    m = re.search(r"(令和|平成|昭和)(\d{1,2}|元)年(\d{1,2})月(\d{1,2})日", t)
    if m:
        era, yn, mo, dy = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
        year_n = 1 if yn == "元" else int(yn)
        return date(_wareki_to_year(era, year_n), mo, dy)

    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    return None


def parse_period(text: str) -> tuple[str, date | None, date | None]:
    """
    Return (period_display, start, end).
    Display uses ISO YYYY-MM-DD～YYYY-MM-DD when both ends parse.
    """
    if not text:
        return "", None, None
    # Normalize range markers before NFKC (NFKC turns fullwidth ～ into ~).
    raw = text
    for mark in ("～", "〜", "∼", "至", "から"):
        raw = raw.replace(mark, "~")
    raw = unicodedata.normalize("NFKC", raw)
    raw = re.sub(r"\s+", "", raw)
    raw = raw.replace("まで", "")
    parts = re.split(r"[~\-–—]", raw)
    dates: list[date] = []
    for part in parts:
        d = parse_jp_date(part)
        if d:
            dates.append(d)
    if len(dates) >= 2:
        start, end = dates[0], dates[-1]
        return f"{start.isoformat()}～{end.isoformat()}", start, end
    if len(dates) == 1:
        d = dates[0]
        return d.isoformat(), d, d
    cleaned = re.sub(r"[（(][^）)]*[）)]", "", unicodedata.normalize("NFKC", text))
    cleaned = re.sub(r"\s+", "", cleaned).replace("から", "～").replace("まで", "")
    return cleaned.strip(" ：:"), None, None


def status_from_end(end: date | None, force_closed: bool = False) -> str:
    if force_closed:
        return "closed"
    if end is None:
        return "closed"
    return "open" if end >= TODAY else "closed"


def load_existing() -> dict[str, Any]:
    if not PC_JSON.exists():
        return {}
    try:
        return json.loads(PC_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("could not read existing json: %s", e)
        return {}


def make_item(
    *,
    title: str,
    org: str,
    status: str,
    period: str,
    url: str,
    note: str | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": slugify(title, org),
        "title": normalize_title(title),
        "org": org,
        "status": status,
        "period": period,
        "url": url,
    }
    if note:
        item["note"] = note.strip()
    if end:
        item["_end"] = end.isoformat()
    return item


# ---- Konan ----


def scrape_konan_index(html: str, base_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, Any]] = []
    # Year sections: h3 令和N年度 followed by dl entries with dt.title
    for h3 in soup.find_all("h3"):
        heading = h3.get_text(" ", strip=True)
        if not re.search(r"令和\d+年度|平成", heading):
            continue
        node = h3.next_sibling
        while node is not None:
            if isinstance(node, Tag) and node.name == "h3":
                break
            if isinstance(node, Tag):
                for dt in node.find_all("dt", class_=lambda c: c and "title" in c):
                    a = dt.find("a", href=True)
                    if not a:
                        continue
                    title = a.get_text(" ", strip=True)
                    href = urljoin(base_url, a["href"])
                    dd = dt.find_next_sibling("dd")
                    period_raw = ""
                    note = None
                    if dd:
                        blob = dd.get_text(" ", strip=True)
                        m = re.search(r"【意見募集期間】([^【]+)", blob)
                        if m:
                            period_raw = m.group(1).strip()
                        m2 = re.search(r"【意見提出者数（件数）】([^【]+)", blob)
                        if m2:
                            note = m2.group(1).strip()
                    period, _start, end = parse_period(period_raw)
                    status = status_from_end(end)
                    items.append(
                        make_item(
                            title=title,
                            org="湖南市",
                            status=status,
                            period=period or period_raw,
                            url=href,
                            note=note,
                            end=end,
                        )
                    )
            node = node.next_sibling
    return items


def scrape_konan_year_list(html: str, base_url: str) -> list[tuple[str, str]]:
    """Return (title, url) from a year index that only lists links."""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("#tmp_contents, #contents, article") or soup
    out: list[tuple[str, str]] = []
    for a in main.find_all("a", href=True):
        href = a["href"]
        if "/paburikku_komento/" not in href:
            continue
        if href.rstrip("/").endswith("index.html") or href.endswith("/"):
            continue
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 4:
            continue
        out.append((title, urljoin(base_url, href)))
    # Dedup preserve order
    seen: set[str] = set()
    uniq: list[tuple[str, str]] = []
    for t, u in out:
        if u in seen:
            continue
        seen.add(u)
        uniq.append((t, u))
    return uniq


def enrich_konan_detail(title: str, url: str) -> dict[str, Any] | None:
    html, final = fetch(url)
    if not html:
        # Keep list-level with index fallback
        return make_item(
            title=title,
            org="湖南市",
            status="closed",
            period="",
            url=KONAN_INDEX,
            note=None,
        )
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    period_raw = ""
    m = re.search(r"意見募集期間\s*\n?\s*([^\n]+)", text)
    if m:
        period_raw = m.group(1).strip()
    note = None
    m2 = re.search(r"意見提出者数（意見数）\s*\n?\s*([^\n]+)", text)
    if m2:
        note = m2.group(1).strip()
    period, _s, end = parse_period(period_raw)
    status = status_from_end(end)
    use_url = final or url
    if not head_ok(use_url):
        use_url = KONAN_INDEX
    return make_item(
        title=title,
        org="湖南市",
        status=status,
        period=period or period_raw,
        url=use_url,
        note=note,
        end=end,
    )


def collect_konan() -> list[dict[str, Any]]:
    html, final = fetch(KONAN_INDEX)
    if not html:
        return []
    base = final or KONAN_INDEX
    items = scrape_konan_index(html, base)
    log.info("konan index entries: %d", len(items))

    # Supplement with R7 year list if we have room
    if len(items) < MAX_ORG:
        yhtml, yfinal = fetch(KONAN_R7)
        if yhtml:
            listed = scrape_konan_year_list(yhtml, yfinal or KONAN_R7)
            existing_titles = {normalize_title(i["title"]) for i in items}
            for title, url in listed:
                if len(items) >= MAX_ORG:
                    break
                if normalize_title(title) in existing_titles:
                    continue
                detail = enrich_konan_detail(title, url)
                if detail:
                    items.append(detail)
                    existing_titles.add(normalize_title(detail["title"]))
            log.info("konan after R7 enrich: %d", len(items))
    return items


# ---- Shiga ----


def _section_heading_match(tag: Tag, keywords: tuple[str, ...]) -> bool:
    if tag.name not in ("h1", "h2", "h3", "h4"):
        return False
    t = tag.get_text(" ", strip=True)
    return any(k in t for k in keywords)


def _iter_section_tables(start: Tag) -> list[Tag]:
    tables: list[Tag] = []
    for sib in start.next_siblings:
        if isinstance(sib, Tag) and sib.name in ("h1", "h2", "h3") and sib is not start:
            # stop at next numbered major heading
            text = sib.get_text(strip=True)
            if re.match(r"^\d+\.", text) or sib.name == "h2":
                break
        if isinstance(sib, Tag):
            if sib.name == "table":
                tables.append(sib)
            else:
                tables.extend(sib.find_all("table"))
    return tables



def scrape_shiga(html: str, base_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, Any]] = []

    # Build title->url map from all page links for prep items without hrefs
    link_map: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        t = normalize_title(a.get_text(" ", strip=True))
        if len(t) < 6:
            continue
        link_map.append((t, urljoin(base_url, a["href"])))

    def resolve_url(title: str) -> str:
        nt = normalize_title(title)
        # Prefer longest matching link text containing the title core
        best: str | None = None
        best_score = 0
        core = re.sub(r"[（(].*?[）)]", "", nt)
        for lt, href in link_map:
            if nt in lt or core in lt or lt in nt:
                score = min(len(lt), len(nt))
                # Prefer non-index detail pages
                if "/bj00/10105" in href or href.rstrip("/").endswith("10105.html"):
                    score -= 50
                if score > best_score:
                    best_score = score
                    best = href
        if best and best_score > 0:
            if head_ok(best):
                return best
            log.info("detail 404/unusable, fallback index: %s", best)
        return SHIGA_INDEX

    # Open: 実施中
    for h in soup.find_all(["h2", "h3"]):
        if not _section_heading_match(h, ("実施中",)):
            continue
        for table in _iter_section_tables(h):
            for tr in table.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                if not cells or cells[0] in ("案件名",):
                    continue
                if "該当する案件はありません" in " ".join(cells):
                    continue
                title = cells[0]
                # Skip category-only rows without a period column looking like dates
                period_raw = cells[1] if len(cells) > 1 else ""
                if not parse_jp_date(period_raw) and "令和" not in period_raw and "年" not in period_raw:
                    # might be a category header row
                    if len(cells) < 2 or not period_raw:
                        continue
                a = tr.find("a", href=True)
                if a:
                    url = urljoin(base_url, a["href"])
                    if not head_ok(url):
                        url = resolve_url(title)
                else:
                    url = resolve_url(title)
                period, _s, end = parse_period(period_raw)
                items.append(
                    make_item(
                        title=title,
                        org="滋賀県",
                        status="open",
                        period=period or period_raw,
                        url=url,
                        end=end or TODAY,
                    )
                )
        break

    # Closed prep: 結果公表準備中
    for h in soup.find_all(["h2", "h3"]):
        if not _section_heading_match(h, ("結果公表準備",)):
            continue
        for table in _iter_section_tables(h):
            for tr in table.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                if not cells or cells[0] in ("案件名",):
                    continue
                if "該当する案件はありません" in " ".join(cells):
                    continue
                title = cells[0]
                period_raw = cells[1] if len(cells) > 1 else ""
                if not title or len(title) < 4:
                    continue
                a = tr.find("a", href=True)
                if a:
                    url = urljoin(base_url, a["href"])
                    if not head_ok(url):
                        url = resolve_url(title)
                else:
                    url = resolve_url(title)
                period, _s, end = parse_period(period_raw)
                items.append(
                    make_item(
                        title=title,
                        org="滋賀県",
                        status="closed",
                        period=period or period_raw,
                        url=url,
                        end=end,
                    )
                )
        break

    return items


def collect_shiga() -> list[dict[str, Any]]:
    html, final = fetch(SHIGA_INDEX)
    used = SHIGA_INDEX
    if not html:
        html, final = fetch(SHIGA_INDEX_ALT)
        used = SHIGA_INDEX_ALT
    if not html:
        return []
    items = scrape_shiga(html, final or used)
    log.info("shiga entries: %d", len(items))
    return items


# ---- Sort / select ----


def period_end_key(item: dict[str, Any]) -> date:
    if item.get("_end"):
        d = parse_jp_date(item["_end"])
        if d:
            return d
    _p, _s, end = parse_period(item.get("period") or "")
    return end or date.min


def sort_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda it: (
            0 if it.get("status") == "open" else 1,
            -period_end_key(it).toordinal(),
        ),
    )


def dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for it in items:
        key = (it.get("org"), normalize_title(it.get("title", "")))
        k = f"{key[0]}|{key[1]}"
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def sanitize(item: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": item["id"],
        "title": item["title"],
        "org": item["org"],
        "status": item["status"],
        "period": item.get("period") or "",
        "url": item["url"],
    }
    if item.get("note"):
        out["note"] = item["note"]
    return out


# ---- HTML render ----


def status_label(status: str) -> str:
    return "受付中" if status == "open" else "募集終了"


def render_cards(items: list[dict[str, Any]]) -> str:
    if not items:
        return (
            '        <div class="empty-hint">\n'
            '          <p style="margin:0;">現在、一覧上で受付中の案件はありません。</p>\n'
            "        </div>\n"
        )
    parts = ['        <ul class="card-list">\n']
    for it in items:
        parts.append("          <li>\n")
        parts.append('            <article class="card card--compact">\n')
        parts.append('              <div class="card-meta">')
        parts.append(f'<span class="tag">{esc(it["org"])}</span>')
        parts.append(f'<span class="tag">{esc(status_label(it["status"]))}</span>')
        parts.append("</div>\n")
        parts.append(
            f'              <h3><a href="{esc(it["url"])}" target="_blank" '
            f'rel="noopener">{esc(it["title"])}</a></h3>\n'
        )
        if it.get("period"):
            parts.append(f'              <p>募集期間：{esc(it["period"])}</p>\n')
        if it.get("note"):
            parts.append(f'              <p>{esc(it["note"])}</p>\n')
        parts.append("            </article>\n")
        parts.append("          </li>\n")
    parts.append("        </ul>\n")
    return "".join(parts)


def render_open(items: list[dict[str, Any]]) -> str:
    open_items = [i for i in items if i.get("status") == "open"]
    open_items = sort_items(open_items)
    if not open_items:
        return (
            '        <div class="empty-hint">\n'
            '          <p style="margin:0;">現在、一覧上で受付中の案件はありません。</p>\n'
            "        </div>\n"
        )
    return render_cards(open_items)


def format_fetched_ja(d: date) -> str:
    return f"{d.year}年{d.month}月{d.day}日"


def render_meta(fetched: date) -> str:
    return (
        f'        <p class="muted">受付状況は変わるため、最新の内容や提出方法は必ず公式ページで'
        f"確認してください。掲載内容は{format_fetched_ja(fetched)}時点で確認したものです。</p>\n"
    )


def render_summary(fetched: date, open_count: int) -> str:
    if open_count == 0:
        body = (
            f"{format_fetched_ja(fetched)}時点で確認したところ、一覧上で受付中の案件はありません。"
            "直近で募集が終了した案件を掲載しています。"
        )
    else:
        body = (
            f"{format_fetched_ja(fetched)}時点で確認したところ、一覧上の受付中は"
            f"{open_count}件です。詳細・提出方法は各公式ページで確認してください。"
        )
    return f"        <p>{esc(body)}</p>\n"


def replace_auto_section(html: str, name: str, inner: str) -> str:
    start = f"<!-- AUTO:{name} -->"
    end = f"<!-- /AUTO:{name} -->"
    i0 = html.find(start)
    i1 = html.find(end)
    if i0 < 0 or i1 < 0 or i1 < i0:
        raise RuntimeError(f"AUTO markers for {name} not found in public-comment.html")
    return html[: i0 + len(start)] + "\n" + inner + html[i1:]


def write_json(items: list[dict[str, Any]], fetched: date) -> None:
    payload = {
        "fetched_at": fetched.isoformat(),
        "sources": SOURCES,
        "items": [sanitize(i) for i in items],
    }
    PC_JSON.parent.mkdir(parents=True, exist_ok=True)
    PC_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log.info("wrote %s (%d items)", PC_JSON.relative_to(ROOT), len(items))


def update_html(items: list[dict[str, Any]], fetched: date) -> None:
    html = PC_HTML.read_text(encoding="utf-8")
    open_count = sum(1 for i in items if i.get("status") == "open")
    konan = sort_items([i for i in items if i.get("org") == "湖南市"])[:MAX_ORG]
    shiga = sort_items([i for i in items if i.get("org") == "滋賀県"])[:MAX_ORG]

    html = replace_auto_section(html, "meta", render_meta(fetched))
    html = replace_auto_section(html, "summary", render_summary(fetched, open_count))
    html = replace_auto_section(html, "open", render_open(items))
    html = replace_auto_section(html, "konan", render_cards(konan))
    html = replace_auto_section(html, "shiga", render_cards(shiga))
    PC_HTML.write_text(html, encoding="utf-8")
    log.info(
        "updated AUTO sections (open=%d konan=%d shiga=%d)",
        open_count,
        len(konan),
        len(shiga),
    )


def main() -> int:
    fetched = TODAY
    konan = collect_konan()
    shiga = collect_shiga()

    if not konan and not shiga:
        log.warning("total scrape failure; keeping existing data and exiting 0")
        return 0

    items = dedupe(sort_items(konan + shiga))
    # Drop internal sort helper before write is handled in sanitize
    try:
        write_json(items, fetched)
        update_html(items, fetched)
    except Exception:
        log.exception("failed to write outputs")
        return 1

    open_n = sum(1 for i in items if i["status"] == "open")
    log.info(
        "done: total=%d open=%d konan=%d shiga=%d",
        len(items),
        open_n,
        sum(1 for i in items if i["org"] == "湖南市"),
        sum(1 for i in items if i["org"] == "滋賀県"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
