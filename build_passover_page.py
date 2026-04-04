from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup


OUTPUT_FILE = Path("index.html")
CACHE_DIR = Path(".cache")
NYT_CACHE_DIR = CACHE_DIR / "nyt"
BASE_PRICE_URL = (
    "https://www.goodeggs.com/manischewitz/original-passover-matzo/"
    "661ecb091c7d5e001109d36a"
)
HEADERS = {"User-Agent": "Codex/1.0"}
START_YEAR = 2025
END_YEAR = 1976
NYT_API_KEY_ENV = "NYT_API_KEY"
NYT_ARCHIVE_API_URL = "https://api.nytimes.com/svc/archive/v1/{year}/{month}.json"
NYT_ARTICLE_SEARCH_URL = "https://api.nytimes.com/svc/search/v2/articlesearch.json"
MAX_NYT_ARTICLES = 10
MAX_SUMMARY_WORDS = 100


@dataclass
class Weather:
    high: int
    low: int
    precip: float | None
    snow: float | None
    source: str
    station: str | None = None


@dataclass
class EventArticle:
    title: str
    summary: str
    url: str
    nyt_url: str
    source_label: str = "NYT article"


@dataclass
class Event:
    articles: list[EventArticle]
    exact_match: bool = True


def get_json(session: requests.Session, url: str, params: dict | None = None) -> dict | list:
    for attempt in range(6):
        response = session.get(url, params=params, timeout=30)
        if response.status_code != 429:
            response.raise_for_status()
            return response.json()

        retry_after = response.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            wait_seconds = int(retry_after)
        else:
            wait_seconds = min(60, 10 * (attempt + 1))
        time.sleep(wait_seconds)

    response.raise_for_status()
    return response.json()


def iso_to_slug(iso_date: str) -> str:
    dt = datetime.fromisoformat(iso_date)
    return f"{dt.strftime('%B').lower()}-{dt.day}"


def format_date(iso_date: str) -> str:
    return datetime.fromisoformat(iso_date).strftime("%a, %b %d, %Y")


def parse_float(value: str) -> float | None:
    value = value.strip()
    if value.lower() == "n/a":
        return None
    return float(value)


def fetch_passover_dates(session: requests.Session, year: int) -> dict[str, str | int]:
    url = (
        "https://www.hebcal.com/hebcal"
        f"?cfg=json&year={year}&maj=on&month=x&c=off&geo=none&m=50&s=off"
    )
    items = get_json(session, url)["items"]
    lookup = {item["title"]: item for item in items}
    return {
        "year": year,
        "day1": lookup["Pesach I"]["date"],
        "day2": lookup["Pesach II"]["date"],
        "hebcal_link": lookup["Pesach I"]["link"],
    }


def fetch_weather_table(session: requests.Session, slug: str) -> dict[int, Weather]:
    url = f"https://www.extremeweatherwatch.com/cities/chicago/day/{slug}"
    soup = BeautifulSoup(session.get(url, timeout=30).text, "html.parser")
    tables = soup.find_all("table")
    daily_table = tables[2]
    weather_by_year: dict[int, Weather] = {}

    for row in daily_table.find_all("tr")[1:]:
        cells = row.find_all(["td", "th"])
        if len(cells) < 5:
            continue
        year_text = cells[0].get_text(strip=True)
        if not year_text.isdigit():
            continue
        high_text = cells[1].get_text(strip=True)
        low_text = cells[2].get_text(strip=True)
        if high_text.lower() == "n/a" or low_text.lower() == "n/a":
            continue
        weather_by_year[int(year_text)] = Weather(
            high=int(float(high_text)),
            low=int(float(low_text)),
            precip=parse_float(cells[3].get_text(strip=True)),
            snow=parse_float(cells[4].get_text(strip=True)),
            source=url,
        )

    return weather_by_year


def fetch_noaa_weather(session: requests.Session, iso_date: str) -> Weather:
    url = (
        "https://www.ncei.noaa.gov/access/services/data/v1"
        "?dataset=daily-summaries"
        "&stations=USW00094846"
        f"&startDate={iso_date}&endDate={iso_date}"
        "&dataTypes=TMAX,TMIN,PRCP,SNOW"
        "&units=standard&format=json"
        "&includeAttributes=false&includeStationName=true"
    )
    item = get_json(session, url)[0]
    return Weather(
        high=int(float(item["TMAX"])),
        low=int(float(item["TMIN"])),
        precip=float(item["PRCP"]),
        snow=float(item["SNOW"]),
        source=url,
        station=item.get("NAME"),
    )


def format_search_date(iso_date: str) -> str:
    return datetime.fromisoformat(iso_date).strftime("%Y%m%d")


def nyt_archive_url(query: str, iso_date: str) -> str:
    date_token = format_search_date(iso_date)
    return (
        "https://www.nytimes.com/search"
        f"?dropmab=true&query={quote_plus(query)}&sort=best"
        f"&startDate={date_token}&endDate={date_token}"
    )


def wikipedia_search_url(query: str) -> str:
    return f"https://en.wikipedia.org/wiki/Special:Search?search={quote_plus(query)}"


def clean_event_text(text: str) -> str:
    return (
        text.replace("\xa0", " ")
        .replace(" (pictured)", "")
        .replace("(pictured) ", "")
        .strip()
    )


def truncate_words(text: str, limit: int) -> str:
    words = text.split()
    if len(words) <= limit:
        return text
    return " ".join(words[:limit]).rstrip(".,;:") + "..."


def first_nonempty(*values: object) -> str:
    for value in values:
        if value is None:
            continue
        text = clean_event_text(str(value))
        if text:
            return text
    return ""


def wikipedia_page_url(page: dict) -> str:
    content_urls = page.get("content_urls", {})
    desktop_urls = content_urls.get("desktop", {})
    if desktop_urls.get("page"):
        return desktop_urls["page"]
    titles = page.get("titles", {})
    canonical = titles.get("canonical") or page.get("title") or ""
    return f"https://en.wikipedia.org/wiki/{canonical}"


def event_search_query(item: dict) -> str:
    pages = item.get("pages") or []
    if pages:
        titles = pages[0].get("titles", {})
        title = titles.get("normalized") or titles.get("display") or pages[0].get("normalizedtitle")
        if title:
            return title
    return clean_event_text(item["text"])


def require_nyt_api_key() -> str:
    api_key = os.getenv(NYT_API_KEY_ENV)
    if api_key:
        return api_key
    raise RuntimeError(
        f"{NYT_API_KEY_ENV} is required to fetch New York Times archive data. "
        f"Set it in your environment before running {OUTPUT_FILE.name} generation."
    )


def ensure_cache_dirs() -> None:
    NYT_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def load_cached_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_cached_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")


def fetch_event_catalog(session: requests.Session, slug: str) -> dict:
    month_name, day_text = slug.split("-")
    month_number = datetime.strptime(month_name, "%B").month
    day_number = int(day_text)
    url = (
        "https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/all/"
        f"{month_number:02d}/{day_number:02d}"
    )
    return get_json(session, url)


def fetch_nyt_archive_month(session: requests.Session, year: int, month: int, api_key: str) -> list[dict]:
    cache_path = NYT_CACHE_DIR / f"archive-{year:04d}-{month:02d}.json"
    cached = load_cached_json(cache_path)
    if cached is not None:
        payload = cached
    else:
        payload = get_json(
            session,
            NYT_ARCHIVE_API_URL.format(year=year, month=month),
            params={"api-key": api_key},
        )
        write_cached_json(cache_path, payload)
    return payload["response"]["docs"]


def fetch_nyt_articlesearch_day(session: requests.Session, iso_date: str, api_key: str) -> list[dict]:
    cache_path = NYT_CACHE_DIR / f"articlesearch-{format_search_date(iso_date)}.json"
    cached = load_cached_json(cache_path)
    if cached is not None:
        payload = cached
    else:
        date_token = format_search_date(iso_date)
        payload = get_json(
            session,
            NYT_ARTICLE_SEARCH_URL,
            params={
                "begin_date": date_token,
                "end_date": date_token,
                "fq": 'source:("The New York Times")',
                "sort": "oldest",
                "page": 0,
                "api-key": api_key,
            },
        )
        write_cached_json(cache_path, payload)
    return payload["response"]["docs"]


def docs_for_exact_date(docs: list[dict], iso_date: str) -> list[dict]:
    return [doc for doc in docs if str(doc.get("pub_date", "")).startswith(iso_date)]


def merge_docs(*doc_groups: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()
    for docs in doc_groups:
        for doc in docs:
            web_url = doc.get("web_url")
            if not web_url or web_url in seen:
                continue
            seen.add(web_url)
            merged.append(doc)
    return merged


def print_page_score(print_page: str | None) -> int:
    if not print_page:
        return 0
    digits = "".join(ch for ch in str(print_page) if ch.isdigit())
    if not digits:
        return 0
    return max(0, 20 - int(digits))


def score_nyt_doc(doc: dict) -> tuple[int, int]:
    source = doc.get("source") or ""
    section = doc.get("section_name") or ""
    desk = doc.get("news_desk") or ""
    material = doc.get("type_of_material") or ""
    document_type = doc.get("document_type") or ""
    score = 0

    if source == "The New York Times":
        score += 100
    if document_type == "article":
        score += 35
    if material in {"News", "Front Page", "Article", "News Analysis"}:
        score += 30
    if material in {"Blog", "Paid Death Notice", "Obituary", "Review", "Schedule", "Correction"}:
        score -= 35
    if section == "Front Page":
        score += 50
    if section in {"U.S.", "World", "Business", "New York", "Washington", "NYRegion"}:
        score += 10
    if desk in {"National", "Foreign", "Washington", "Business Day", "Metro", "Politics", "Science"}:
        score += 8
    if desk in {"Opinion", "Editorial", "OpEd", "Letters", "Obits", "Obituaries"}:
        score -= 20
    if section in {"Opinion", "Obituaries"}:
        score -= 20
    score += print_page_score(doc.get("print_page"))
    if first_nonempty(doc.get("abstract"), doc.get("snippet")):
        score += 4

    headline = first_nonempty(
        doc.get("headline", {}).get("main"),
        doc.get("headline", {}).get("print_headline"),
        doc.get("headline", {}).get("name"),
    )
    return score, len(headline)


def nyt_doc_headline(doc: dict) -> str:
    return first_nonempty(
        doc.get("headline", {}).get("main"),
        doc.get("headline", {}).get("print_headline"),
        doc.get("headline", {}).get("name"),
        doc.get("snippet"),
        doc.get("abstract"),
    )


def nyt_doc_summary(doc: dict) -> str:
    headline = nyt_doc_headline(doc)
    summary = first_nonempty(doc.get("abstract"), doc.get("snippet"), doc.get("lead_paragraph"))
    if not summary or summary == headline:
        return ""
    return truncate_words(summary, MAX_SUMMARY_WORDS)


def nyt_doc_to_article(doc: dict, iso_date: str) -> EventArticle:
    headline = nyt_doc_headline(doc)
    return EventArticle(
        title=headline,
        summary=nyt_doc_summary(doc),
        url=doc["web_url"],
        nyt_url=nyt_archive_url(headline, iso_date),
    )


def select_nyt_event(session: requests.Session, iso_date: str, archive_docs: list[dict], api_key: str) -> Event | None:
    exact_archive_docs = docs_for_exact_date(archive_docs, iso_date)
    candidates = merge_docs(exact_archive_docs)
    if not candidates:
        search_docs = fetch_nyt_articlesearch_day(session, iso_date, api_key)
        exact_search_docs = docs_for_exact_date(search_docs, iso_date)
        candidates = merge_docs(exact_search_docs)
        if not candidates and search_docs:
            # Article Search is date-constrained already; use it as a last exact-day fallback.
            candidates = merge_docs(search_docs)
    if not candidates:
        return None

    ranked = sorted(candidates, key=score_nyt_doc, reverse=True)
    return Event(articles=[nyt_doc_to_article(doc, iso_date) for doc in ranked[:MAX_NYT_ARTICLES]], exact_match=True)


def select_event(payload: dict, iso_date: str) -> Event:
    target_year = datetime.fromisoformat(iso_date).year

    def with_pages(items: list[dict]) -> list[dict]:
        return [item for item in items if item.get("pages")]

    exact_events = with_pages([item for item in payload.get("events", []) if int(item["year"]) == target_year])
    exact_selected = with_pages([item for item in payload.get("selected", []) if int(item["year"]) == target_year])

    if exact_events:
        chosen = max(exact_events, key=lambda item: (len(item.get("pages", [])), len(item["text"])))
        exact_match = True
    elif exact_selected:
        chosen = max(exact_selected, key=lambda item: (len(item.get("pages", [])), len(item["text"])))
        exact_match = True
    else:
        fallback_pool = with_pages(payload.get("selected", [])) or with_pages(payload.get("events", []))
        chosen = min(fallback_pool, key=lambda item: abs(int(item["year"]) - target_year))
        exact_match = False

    page = chosen["pages"][0]
    text = clean_event_text(chosen["text"])
    query = event_search_query(chosen)
    return Event(
        articles=[
            EventArticle(
                title=f"Wikipedia event from {chosen['year']}",
                summary=truncate_words(text, MAX_SUMMARY_WORDS),
                url=wikipedia_page_url(page),
                nyt_url=nyt_archive_url(query, iso_date),
                source_label="Wikipedia event",
            )
        ],
        exact_match=exact_match,
    )


def fetch_base_price(session: requests.Session) -> float:
    html = session.get(BASE_PRICE_URL, timeout=30).text
    soup = BeautifulSoup(html, "html.parser")
    return float(soup.find("meta", attrs={"itemprop": "price"})["content"])


def fetch_cpi(session: requests.Session) -> dict[tuple[int, int], float]:
    url = "https://www.officialdata.org/us-cpi"
    soup = BeautifulSoup(session.get(url, timeout=40).text, "html.parser")
    cpi: dict[tuple[int, int], float] = {}
    for row in soup.find_all("tr")[1:]:
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
        if len(cells) < 2 or not cells[0].isdigit():
            continue
        year = int(cells[0])
        cpi[(year, 1)] = float(cells[1])
    return cpi


def price_for_year(base_price: float, cpi: dict[tuple[int, int], float], iso_date: str) -> tuple[float, str]:
    dt = datetime.fromisoformat(iso_date)
    reference_key = max(key for key in cpi if key[0] == 2026)
    reference_cpi = cpi[reference_key]
    used_key = (dt.year, 1)
    estimated = round(base_price * cpi[used_key] / reference_cpi, 2)
    return estimated, str(used_key[0])


def describe_weather(weather: Weather) -> str:
    pieces = [f"{weather.high} / {weather.low} F"]
    if weather.precip is None:
        pieces.append("precip n/a")
    elif weather.precip == 0:
        pieces.append("dry")
    else:
        pieces.append(f'{weather.precip:.2f}" rain')
    if weather.snow and weather.snow > 0:
        pieces.append(f'{weather.snow:.1f}" snow')
    return "; ".join(pieces)


def weather_block(label: str, iso_date: str, weather: Weather) -> str:
    note = ""
    if weather.station:
        note = f'<div class="source-note">NOAA fallback: {escape(weather.station)}</div>'
    return (
        '<div class="day-block">'
        f'<div class="day-title">{escape(label)}</div>'
        f'<div class="day-date"><a href="{escape(weather.source)}">{escape(format_date(iso_date))}</a></div>'
        f'<div class="day-weather">{escape(describe_weather(weather))}</div>'
        f"{note}"
        "</div>"
    )


def event_block(label: str, iso_date: str, event: Event) -> str:
    fallback_note = ""
    if not event.exact_match:
        fallback_note = (
            '<div class="source-note">'
            "The NYT APIs did not return a usable exact-date article here, so this row falls back to a date-matched Wikipedia event."
            "</div>"
        )
    title_items = [
        '<div class="event-item">'
        f'<a href="{escape(article.url)}">{escape(article.title)}</a>'
        "</div>"
        for article in event.articles
    ]
    return (
        '<div class="event-block">'
        f'<div class="event-date">{escape(label)} · {escape(format_date(iso_date))}</div>'
        + f'<div class="event-list">{"".join(title_items)}</div>'
        + f"{fallback_note}"
        "</div>"
    )


def build_rows(session: requests.Session) -> list[dict]:
    nyt_api_key = require_nyt_api_key()
    ensure_cache_dirs()
    passover_rows = [fetch_passover_dates(session, year) for year in range(START_YEAR, END_YEAR - 1, -1)]
    unique_slugs = sorted(
        {iso_to_slug(row["day1"]) for row in passover_rows}
        | {iso_to_slug(row["day2"]) for row in passover_rows}
    )
    unique_months = sorted(
        {
            (datetime.fromisoformat(str(row["day1"])).year, datetime.fromisoformat(str(row["day1"])).month)
            for row in passover_rows
        }
        | {
            (datetime.fromisoformat(str(row["day2"])).year, datetime.fromisoformat(str(row["day2"])).month)
            for row in passover_rows
        }
    )
    weather_cache = {slug: fetch_weather_table(session, slug) for slug in unique_slugs}
    nyt_archive_cache = {
        month_key: fetch_nyt_archive_month(session, month_key[0], month_key[1], nyt_api_key)
        for month_key in unique_months
    }
    event_cache = {slug: fetch_event_catalog(session, slug) for slug in unique_slugs}
    cpi = fetch_cpi(session)
    base_price = fetch_base_price(session)

    rows: list[dict] = []
    for row in passover_rows:
        day1_slug = iso_to_slug(row["day1"])
        day2_slug = iso_to_slug(row["day2"])
        day1_dt = datetime.fromisoformat(str(row["day1"]))
        day2_dt = datetime.fromisoformat(str(row["day2"]))
        day1_weather = weather_cache[day1_slug].get(row["year"]) or fetch_noaa_weather(session, row["day1"])
        day2_weather = weather_cache[day2_slug].get(row["year"]) or fetch_noaa_weather(session, row["day2"])
        estimated_price, cpi_month = price_for_year(base_price, cpi, row["day1"])
        day1_news = select_nyt_event(
            session,
            str(row["day1"]),
            nyt_archive_cache[(day1_dt.year, day1_dt.month)],
            nyt_api_key,
        ) or select_event(event_cache[day1_slug], row["day1"])
        day2_news = select_nyt_event(
            session,
            str(row["day2"]),
            nyt_archive_cache[(day2_dt.year, day2_dt.month)],
            nyt_api_key,
        ) or select_event(event_cache[day2_slug], row["day2"])
        rows.append(
            {
                "year": row["year"],
                "hebcal_link": row["hebcal_link"],
                "day1": row["day1"],
                "day2": row["day2"],
                "day1_weather": day1_weather,
                "day2_weather": day2_weather,
                "day1_event": day1_news,
                "day2_event": day2_news,
                "estimated_price": estimated_price,
                "cpi_month": cpi_month,
            }
        )

    return rows


def summary_cards(rows: list[dict]) -> str:
    warmest = max(rows, key=lambda row: row["day1_weather"].high)
    snowiest = max(
        rows,
        key=lambda row: max(row["day1_weather"].snow or 0, row["day2_weather"].snow or 0),
    )
    price_low = min(row["estimated_price"] for row in rows)
    price_high = max(row["estimated_price"] for row in rows)
    cards = [
        ("Coverage", "1976 through 2025", "50 completed Chicago Passovers"),
        (
            "Warmest Day 1",
            f'{warmest["year"]}: {warmest["day1_weather"].high} F',
            format_date(warmest["day1"]),
        ),
        (
            "Snowiest Reading",
            snowiest["year"],
            max(snowiest["day1_weather"].snow or 0, snowiest["day2_weather"].snow or 0),
        ),
        ("Estimated Box Price", f"${price_low:.2f} to ${price_high:.2f}", "inflation-scaled estimate"),
    ]
    html_parts = []
    for title, value, note in cards:
        if isinstance(note, float):
            note_text = f'{note:.1f}" snow'
        else:
            note_text = str(note)
        html_parts.append(
            '<div class="stat-card">'
            f'<div class="stat-title">{escape(str(title))}</div>'
            f'<div class="stat-value">{escape(str(value))}</div>'
            f'<div class="stat-note">{escape(note_text)}</div>'
            "</div>"
        )
    return "".join(html_parts)


def render_table(rows: list[dict]) -> str:
    parts = []
    for row in rows:
        parts.append(
            '<tr class="year-row">'
            f'<td class="year-cell" data-label="Year"><a href="{escape(row["hebcal_link"])}">{row["year"]}</a></td>'
            f'<td data-label="Passover Day 1">{weather_block("Day 1", row["day1"], row["day1_weather"])}</td>'
            f'<td data-label="Passover Day 2">{weather_block("Day 2", row["day2"], row["day2_weather"])}</td>'
            '<td data-label="In The News On Those Dates">'
            f"{event_block('Day 1', row['day1'], row['day1_event'])}"
            f"{event_block('Day 2', row['day2'], row['day2_event'])}"
            "</td>"
            '<td class="price-cell" data-label="Estimated Matzah Box Price">'
            f'<div class="price">${row["estimated_price"]:.2f}</div>'
            f'<div class="price-note">CPI year used: {escape(row["cpi_month"])}</div>'
            "</td>"
            "</tr>"
        )
    return "".join(parts)


def render_html(rows: list[dict]) -> str:
    generated_on = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Chicago Passover Weather, Events, and Matzah Prices</title>
  <style>
    :root {{
      --bg: #f5efe3;
      --paper: rgba(255, 251, 244, 0.88);
      --ink: #1f1b16;
      --muted: #6e6255;
      --accent: #7b1e32;
      --accent-soft: #efe0d9;
      --line: rgba(69, 50, 37, 0.16);
      --gold: #b88a3b;
      --shadow: 0 20px 50px rgba(58, 36, 24, 0.12);
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(184, 138, 59, 0.18), transparent 28%),
        radial-gradient(circle at top right, rgba(123, 30, 50, 0.12), transparent 22%),
        linear-gradient(180deg, #f7f1e6 0%, #efe5d1 100%);
      font-family: Georgia, "Times New Roman", serif;
      line-height: 1.5;
    }}

    a {{
      color: var(--accent);
      text-decoration-thickness: 1px;
      text-underline-offset: 0.15em;
    }}

    main {{
      width: min(1400px, calc(100% - 32px));
      margin: 32px auto 56px;
    }}

    .hero {{
      background: linear-gradient(135deg, rgba(123, 30, 50, 0.94), rgba(74, 29, 41, 0.92));
      color: #fff8f1;
      border-radius: 28px;
      padding: 34px 32px 28px;
      box-shadow: var(--shadow);
      position: relative;
      overflow: hidden;
    }}

    .hero::after {{
      content: "";
      position: absolute;
      inset: auto -40px -40px auto;
      width: 220px;
      height: 220px;
      background: radial-gradient(circle, rgba(255, 236, 196, 0.26), transparent 65%);
    }}

    .eyebrow {{
      letter-spacing: 0.16em;
      text-transform: uppercase;
      font-size: 0.74rem;
      opacity: 0.82;
      margin-bottom: 14px;
    }}

    h1 {{
      margin: 0;
      font-size: clamp(2rem, 3vw, 3.5rem);
      line-height: 0.96;
      max-width: 12ch;
    }}

    .hero p {{
      margin: 16px 0 0;
      max-width: 70ch;
      color: rgba(255, 248, 241, 0.92);
      font-size: 1rem;
    }}

    .stats {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin: 18px 0 24px;
    }}

    .stat-card {{
      background: var(--paper);
      border: 1px solid rgba(255, 255, 255, 0.35);
      border-radius: 20px;
      padding: 18px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
    }}

    .stat-title {{
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--muted);
      margin-bottom: 10px;
    }}

    .stat-value {{
      font-size: 1.35rem;
      line-height: 1.1;
      margin-bottom: 6px;
    }}

    .stat-note {{
      color: var(--muted);
      font-size: 0.92rem;
    }}

    .notes {{
      display: grid;
      grid-template-columns: 1.3fr 1fr;
      gap: 16px;
      margin-bottom: 24px;
    }}

    .panel {{
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 22px;
      box-shadow: var(--shadow);
    }}

    .panel h2 {{
      margin: 0 0 10px;
      font-size: 1.2rem;
    }}

    .panel p,
    .panel li {{
      margin: 0;
      color: var(--muted);
    }}

    .panel ul {{
      margin: 0;
      padding-left: 18px;
      display: grid;
      gap: 8px;
    }}

    .table-wrap {{
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 12px;
      box-shadow: var(--shadow);
      overflow: auto;
    }}

    table {{
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      min-width: 1100px;
    }}

    thead th {{
      position: sticky;
      top: 0;
      background: rgba(246, 238, 222, 0.96);
      backdrop-filter: blur(8px);
      z-index: 1;
      text-align: left;
      font-size: 0.78rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
      padding: 16px 14px;
      border-bottom: 1px solid var(--line);
    }}

    tbody td {{
      vertical-align: top;
      padding: 18px 14px;
      border-bottom: 1px solid var(--line);
    }}

    tbody tr:last-child td {{
      border-bottom: none;
    }}

    tbody tr:nth-child(odd) {{
      background: rgba(255, 255, 255, 0.28);
    }}

    .year-cell {{
      white-space: nowrap;
      font-size: 1.15rem;
      font-weight: bold;
    }}

    .day-block + .day-block,
    .event-block + .event-block {{
      margin-top: 16px;
      padding-top: 16px;
      border-top: 1px dashed var(--line);
    }}

    .day-title,
    .event-date {{
      color: var(--muted);
      font-size: 0.78rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-bottom: 6px;
    }}

    .day-date {{
      font-size: 1rem;
      margin-bottom: 6px;
    }}

    .day-weather {{
      font-size: 0.96rem;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      line-height: 1.35;
    }}

    .weather-temp-group {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }}

    .weather-temp {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 0.86rem;
      font-weight: 700;
      letter-spacing: 0.01em;
    }}

    .weather-temp::before {{
      content: "";
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: currentColor;
      flex: 0 0 auto;
    }}

    .weather-temp-high {{
      color: #8a1c1c;
      background: rgba(193, 59, 59, 0.16);
      border: 1px solid rgba(193, 59, 59, 0.22);
    }}

    .weather-temp-low {{
      color: #184f8a;
      background: rgba(58, 126, 193, 0.14);
      border: 1px solid rgba(58, 126, 193, 0.2);
    }}

    .weather-divider,
    .weather-unit {{
      color: var(--muted);
      font-size: 0.82rem;
      font-weight: 600;
    }}

    .weather-badge {{
      display: inline-flex;
      align-items: center;
      padding: 4px 9px;
      border-radius: 999px;
      font-size: 0.8rem;
      font-weight: 600;
      border: 1px solid transparent;
      white-space: nowrap;
    }}

    .weather-rain {{
      color: #155e75;
      background: rgba(21, 94, 117, 0.12);
      border-color: rgba(21, 94, 117, 0.18);
    }}

    .weather-dry {{
      color: #556b2f;
      background: rgba(138, 158, 91, 0.16);
      border-color: rgba(138, 158, 91, 0.24);
    }}

    .weather-snow {{
      color: #43556a;
      background: rgba(210, 223, 235, 0.7);
      border-color: rgba(125, 149, 171, 0.28);
    }}

    .weather-unknown {{
      color: #6b5c4d;
      background: rgba(110, 98, 85, 0.12);
      border-color: rgba(110, 98, 85, 0.18);
    }}

    .source-note,
    .price-note {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 0.86rem;
    }}

    .event-list {{
      margin-top: 6px;
      display: grid;
      gap: 8px;
      font-size: 0.94rem;
      line-height: 1.35;
    }}

    .event-item {{
      padding-left: 14px;
      position: relative;
    }}

    .event-item::before {{
      content: "•";
      position: absolute;
      left: 0;
      color: var(--accent);
    }}

    .event-item a {{
      color: var(--ink);
      text-decoration: none;
      font-weight: 600;
    }}

    .event-item a:hover {{
      color: var(--accent);
      text-decoration: underline;
    }}

    .price-cell {{
      white-space: nowrap;
    }}

    .price {{
      font-size: 1.34rem;
      color: var(--accent);
      font-weight: bold;
    }}

    footer {{
      margin-top: 22px;
      color: var(--muted);
      font-size: 0.92rem;
      display: grid;
      gap: 10px;
    }}

    .source-list {{
      display: grid;
      gap: 6px;
    }}

    @media (max-width: 980px) {{
      main {{
        width: min(100% - 20px, 1400px);
      }}

      .hero {{
        padding: 26px 22px 22px;
      }}

      .stats,
      .notes {{
        grid-template-columns: 1fr;
      }}

      .table-wrap {{
        padding: 10px;
        overflow: visible;
      }}

      table {{
        min-width: 0;
      }}
    }}

    @media (max-width: 760px) {{
      .table-wrap {{
        background: transparent;
        border: none;
        box-shadow: none;
        padding: 0;
      }}

      table,
      tbody,
      tr,
      td {{
        display: block;
        width: 100%;
      }}

      thead {{
        display: none;
      }}

      tbody {{
        display: grid;
        gap: 16px;
      }}

      tbody tr {{
        background: var(--paper);
        border: 1px solid var(--line);
        border-radius: 24px;
        box-shadow: var(--shadow);
        overflow: hidden;
      }}

      tbody tr:nth-child(odd) {{
        background: var(--paper);
      }}

      tbody td {{
        border-bottom: 1px solid var(--line);
        padding: 14px 16px 16px;
      }}

      tbody td:last-child {{
        border-bottom: none;
      }}

      tbody td::before {{
        content: attr(data-label);
        display: block;
        margin-bottom: 10px;
        color: var(--muted);
        font-size: 0.76rem;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }}

      .year-cell {{
        font-size: 1.35rem;
      }}

      .year-cell::before {{
        margin-bottom: 6px;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <div class="eyebrow">Chicago · Pesach · Weather History</div>
      <h1>Fifty Chicago Passovers, with weather, linked NYT headlines, and a matzah price estimate.</h1>
      <p>
        This table covers the last 50 completed Passovers in Chicago, from 1976 through 2025.
        Each row includes the first two festival days in the diaspora calendar, observed Chicago weather,
        linked New York Times headlines for those dates, and an estimated nominal price for a regular 1 lb box of
        Manischewitz Original Passover Matzo.
      </p>
    </section>

    <section class="stats">
      {summary_cards(rows)}
    </section>

    <section class="notes">
      <div class="panel">
        <h2>Method</h2>
        <p>
          Passover dates come from Hebcal's diaspora holiday API. Weather is from Chicago daily-history
          pages at Extreme Weather Watch, with a NOAA daily-summaries fallback for the missing 1980
          April 1-2 rows. News coverage uses up to ten same-date New York Times articles per festival
          day, ranked from the official NYT Archive API with an Article Search fallback only when
          needed. Matzah prices are estimated by scaling a current shelf price with
          annual CPI values published from BLS data at OfficialData.
        </p>
      </div>
      <div class="panel">
        <h2>Matzah Estimate</h2>
        <ul>
          <li>Anchor price: $3.99 from Good Eggs for Manischewitz Original Passover Matzo, 1 lb.</li>
          <li>Inflation series: annual U.S. CPI values compiled from BLS data at OfficialData.</li>
          <li>Each year is scaled against the current 2026 CPI value shown in that table.</li>
        </ul>
      </div>
    </section>

    <section class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Year</th>
            <th>Passover Day 1</th>
            <th>Passover Day 2</th>
            <th>In The News On Those Dates</th>
            <th>Estimated Matzah Box Price</th>
          </tr>
        </thead>
        <tbody>
          {render_table(rows)}
        </tbody>
      </table>
    </section>

    <footer>
      <div>Generated locally on {escape(generated_on)}.</div>
      <div class="source-list">
        <div>Sources: <a href="https://www.hebcal.com/hebcal?cfg=json&year=2025&maj=on&month=x&c=off&geo=none&m=50&s=off">Hebcal</a>, <a href="https://www.extremeweatherwatch.com/cities/chicago/day/april-13">Extreme Weather Watch</a>, <a href="https://www.ncei.noaa.gov/access/services/data/v1?dataset=daily-summaries&stations=USW00094846&startDate=1980-04-01&endDate=1980-04-01&dataTypes=TMAX,TMIN,PRCP,SNOW&units=standard&format=json">NOAA daily summaries</a>, <a href="https://developer.nytimes.com/docs/archive-product/1/overview">New York Times Archive API</a>, <a href="https://developer.nytimes.com/docs/articlesearch-product/1/overview">New York Times Article Search API</a>, <a href="{escape(BASE_PRICE_URL)}">Good Eggs</a>, <a href="https://www.officialdata.org/us-cpi">OfficialData CPI table</a>, <a href="https://www.bls.gov/data/inflation_calculator_inside.htm">BLS inflation calculator note</a>.</div>
        <div>Calendar note: this uses the first and second daytime festival dates in the diaspora calendar, not the prior evening seder start.</div>
      </div>
    </footer>
  </main>
  <script>
    (() => {{
      const weatherPattern = /^(\\d+)\\s*\\/\\s*(\\d+)\\s*F(?:;\\s*(.+))?$/;

      function makeNode(tagName, className, text) {{
        const node = document.createElement(tagName);
        if (className) {{
          node.className = className;
        }}
        if (text) {{
          node.textContent = text;
        }}
        return node;
      }}

      function badgeClass(detail) {{
        if (/^dry$/i.test(detail)) {{
          return "weather-badge weather-dry";
        }}
        if (/rain/i.test(detail)) {{
          return "weather-badge weather-rain";
        }}
        if (/snow/i.test(detail)) {{
          return "weather-badge weather-snow";
        }}
        return "weather-badge weather-unknown";
      }}

      function badgeText(detail) {{
        if (/^dry$/i.test(detail)) {{
          return "No rain";
        }}
        if (/^precip n\\/a$/i.test(detail)) {{
          return "Rain n/a";
        }}
        return detail;
      }}

      document.querySelectorAll(".day-weather").forEach((node) => {{
        const raw = node.textContent.trim();
        const match = raw.match(weatherPattern);
        if (!match) {{
          return;
        }}

        const [, high, low, detailText = ""] = match;
        const details = detailText
          .split(/\\s*;\\s*/)
          .map((item) => item.trim())
          .filter(Boolean);

        node.textContent = "";
        node.setAttribute("aria-label", raw);

        const tempGroup = makeNode("span", "weather-temp-group");
        tempGroup.append(
          makeNode("span", "weather-temp weather-temp-high", `High ${{high}}`),
          makeNode("span", "weather-divider", "/"),
          makeNode("span", "weather-temp weather-temp-low", `Low ${{low}}`),
          makeNode("span", "weather-unit", "F")
        );
        node.append(tempGroup);

        details.forEach((detail) => {{
          node.append(makeNode("span", badgeClass(detail), badgeText(detail)));
        }});
      }});
    }})();
  </script>
</body>
</html>
"""


def main() -> None:
    session = requests.Session()
    session.headers.update(HEADERS)
    rows = build_rows(session)
    OUTPUT_FILE.write_text(render_html(rows), encoding="utf-8")
    print(f"Wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
