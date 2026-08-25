from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from icalendar import Calendar, Event

BASE_URL = "https://suncityhoa.org"
EVENTS_URL = "https://suncityhoa.org/events/"
CALENDAR_URL = "https://suncityhoa.org/calendar/"
OUTPUT = Path("suncity-events.ics")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/142.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)


def get(url: str) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_event_url(url: str) -> str | None:
    if not url:
        return None
    url = urljoin(BASE_URL, url.replace("&amp;", "&"))
    parsed = urlparse(url)
    if parsed.netloc.lower() != "suncityhoa.org":
        return None
    path = parsed.path.rstrip("/")
    if not path.startswith("/events/") or path == "/events":
        return None
    return f"{BASE_URL}{path}"


def extract_event_urls(text: str) -> set[str]:
    urls: set[str] = set()
    for match in re.findall(
        r"https?://suncityhoa\.org/events/[^\s\"'<>?#]+",
        text,
        flags=re.I,
    ):
        url = normalize_event_url(match)
        if url:
            urls.add(url)
    try:
        soup = BeautifulSoup(text, "html.parser")
        for link in soup.find_all("a", href=True):
            url = normalize_event_url(link.get("href", ""))
            if url:
                urls.add(url)
    except Exception:
        pass
    return urls


def discover_from_calendar_pages() -> set[str]:
    urls: set[str] = set()
    for page_url in (CALENDAR_URL, EVENTS_URL):
        try:
            response = get(page_url)
            found = extract_event_urls(response.text)
            urls.update(found)
            print(f"Calendar discovery {page_url}: {len(found)} event URLs")
        except Exception as exc:
            print(f"Could not inspect calendar page {page_url}: {exc}")
    return urls


def discover_from_sitemaps() -> set[str]:
    initial = [
        "https://suncityhoa.org/wp-sitemap.xml",
        "https://suncityhoa.org/sitemap_index.xml",
        "https://suncityhoa.org/post-sitemap.xml",
        "https://suncityhoa.org/page-sitemap.xml",
    ]
    queue = list(initial)
    seen_sitemaps: set[str] = set()
    urls: set[str] = set()

    while queue and len(seen_sitemaps) < 60:
        sitemap_url = queue.pop(0)
        if sitemap_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sitemap_url)
        try:
            text = get(sitemap_url).text
        except Exception as exc:
            print(f"Could not inspect sitemap {sitemap_url}: {exc}")
            continue

        urls.update(extract_event_urls(text))
        for child in re.findall(r"<loc>([^<]*sitemap[^<]*)</loc>", text, flags=re.I):
            child = child.replace("&amp;", "&").strip()
            parsed = urlparse(child)
            if parsed.netloc.lower() == "suncityhoa.org" and child not in seen_sitemaps:
                queue.append(child)

    print(f"Sitemap discovery: {len(urls)} event URLs from {len(seen_sitemaps)} sitemap files")
    return urls


def discover_from_existing_calendar() -> set[str]:
    urls: set[str] = set()
    if not OUTPUT.exists():
        return urls
    try:
        text = OUTPUT.read_text(encoding="utf-8", errors="ignore")
        for match in re.findall(r"https?://suncityhoa\.org/events/[^\s\\,;]+", text, re.I):
            url = normalize_event_url(match)
            if url:
                urls.add(url)
    except Exception as exc:
        print(f"Could not inspect existing calendar fallback: {exc}")
    if urls:
        print(f"Existing-calendar fallback: {len(urls)} event URLs")
    return urls


def discover_event_pages() -> list[str]:
    urls: set[str] = set()
    urls.update(discover_from_calendar_pages())
    urls.update(discover_from_sitemaps())

    if len(urls) < 3:
        print("Fresh discovery found fewer than 3 events; using fallback URLs")
        urls.update(discover_from_existing_calendar())

    result = sorted(urls)
    print(f"Discovered {len(result)} candidate event pages total")
    for url in result[:25]:
        print(f"  {url}")
    return result


def parse_event_datetime(text: str, label: str):
    # Examples on SCHOA pages:
    # Start date August 19, 2026 10:00 am
    # End date October 25, 2024
    pattern = re.compile(
        rf"{re.escape(label)}\s+({MONTHS})\s+(\d{{1,2}}),\s+(20\d{{2}})"
        r"(?:\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)))?",
        re.I,
    )
    m = pattern.search(text)
    if not m:
        return None
    try:
        day = dtparser.parse(f"{m.group(1)} {m.group(2)}, {m.group(3)}").date()
        if m.group(4):
            return datetime.combine(day, dtparser.parse(m.group(4)).time())
        return day
    except Exception:
        return None


def extract_location(text: str) -> str:
    # Prefer the structured venue block when present.
    venue = re.search(r"\bVenue\s+(.+?)(?=\bAddress\b|\bOrganizer\b|\bInformation\b|$)", text, re.I)
    address = re.search(r"\bAddress\s+(.+?)(?=\bCity\b|\bOrganizer\b|\bInformation\b|$)", text, re.I)
    city = re.search(r"\bCity\s+(.+?)(?=\bPostal code\b|\bState\b|\bCountry\b|$)", text, re.I)
    state = re.search(r"\bState\s+([A-Z]{2}|Arizona)\b", text, re.I)
    postal = re.search(r"\bPostal code\s+(\d{5}(?:-\d{4})?)", text, re.I)

    parts = []
    for m in (venue, address, city, state, postal):
        if m:
            value = clean(m.group(1))
            if value and value not in parts:
                parts.append(value)
    return ", ".join(parts)


def parse_event_page(event_url: str):
    html = get(event_url).text
    soup = BeautifulSoup(html, "html.parser")
    text = clean(soup.get_text(" ", strip=True))

    start = parse_event_datetime(text, "Start date")
    end = parse_event_datetime(text, "End date")
    if start is None:
        return None

    # Skip old events. Keep today's events and all future events.
    today = datetime.now().date()
    start_day = start.date() if isinstance(start, datetime) else start
    if start_day < today:
        return None

    if end is None:
        end = start + (timedelta(hours=2) if isinstance(start, datetime) else timedelta(days=1))
    elif not isinstance(start, datetime) and not isinstance(end, datetime):
        # ICS all-day DTEND is exclusive.
        end = end + timedelta(days=1)
    elif isinstance(start, datetime) and not isinstance(end, datetime):
        end = datetime.combine(end, start.time()) + timedelta(hours=2)

    h1 = soup.find("h1")
    title = clean(h1.get_text(" ", strip=True)) if h1 else ""
    if not title:
        title_tag = soup.find("title")
        title = clean(title_tag.get_text(" ", strip=True)) if title_tag else "Sun City Event"
        title = re.sub(r"\s*-\s*SCHOA.*$", "", title, flags=re.I)

    description = ""
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        description = clean(meta["content"])

    return {
        "title": title,
        "start": start,
        "end": end,
        "location": extract_location(text),
        "description": description,
        "url": event_url,
    }


def build_combined_calendar(event_pages: list[str]) -> tuple[Calendar, int]:
    combined = Calendar()
    combined.add("prodid", "-//Sun City Arizona Events Calendar//EN")
    combined.add("version", "2.0")
    combined.add("calscale", "GREGORIAN")
    combined.add("method", "PUBLISH")
    combined.add("x-wr-calname", "Sun City Events")
    combined.add("x-wr-timezone", "America/Phoenix")

    seen = set()
    added = 0

    for index, event_url in enumerate(event_pages, 1):
        try:
            item = parse_event_page(event_url)
            if not item:
                print(f"[{index}/{len(event_pages)}] SKIP old/unparseable: {event_url}")
                continue

            key = (item["title"].lower(), str(item["start"]))
            if key in seen:
                print(f"[{index}/{len(event_pages)}] SKIP duplicate: {event_url}")
                continue
            seen.add(key)

            component = Event()
            component.add("uid", f"{abs(hash(event_url))}@suncity-events")
            component.add("summary", item["title"])
            component.add("dtstamp", datetime.utcnow())
            component.add("dtstart", item["start"])
            component.add("dtend", item["end"])
            component.add("url", event_url)
            if item["location"]:
                component.add("location", item["location"])

            description = item["description"]
            if description:
                description += "\n\n"
            description += f"Source: {event_url}"
            component.add("description", description)

            combined.add_component(component)
            added += 1
            print(f"[{index}/{len(event_pages)}] added: {item['title']}")

        except Exception as exc:
            print(f"[{index}/{len(event_pages)}] ERROR {event_url}: {exc}")

    return combined, added


def main():
    print("Discovering Sun City SCHOA events...")
    event_pages = discover_event_pages()
    if not event_pages:
        raise RuntimeError("No SCHOA event pages discovered; existing calendar was not replaced.")

    calendar, count = build_combined_calendar(event_pages)
    print(f"Generated {count} unique future calendar events")

    if count < 3:
        raise RuntimeError(f"Only {count} events were generated; refusing to publish a bad feed.")

    OUTPUT.write_bytes(calendar.to_ical())
    print(f"Wrote {OUTPUT} with {count} events")


if __name__ == "__main__":
    main()
