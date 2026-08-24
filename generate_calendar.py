from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar


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


def get(url: str) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response


def normalize_event_url(url: str) -> str | None:
    """Return a clean SCHOA /events/... URL, or None."""
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
    """Extract event URLs from either HTML or sitemap XML text."""
    urls: set[str] = set()

    # Absolute URLs in sitemap XML or HTML.
    for match in re.findall(
        r"https?://suncityhoa\.org/events/[^\s\"'<>?#]+",
        text,
        flags=re.I,
    ):
        url = normalize_event_url(match)
        if url:
            urls.add(url)

    # Relative/absolute hrefs rendered on calendar pages.
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
    """Discover events from the public calendar/events pages."""
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
    """Recursively inspect likely WordPress sitemap indexes."""
    initial = [
        "https://suncityhoa.org/wp-sitemap.xml",
        "https://suncityhoa.org/sitemap_index.xml",
        "https://suncityhoa.org/post-sitemap.xml",
        "https://suncityhoa.org/page-sitemap.xml",
    ]

    queue = list(initial)
    seen_sitemaps: set[str] = set()
    urls: set[str] = set()

    # Cap recursion so a malformed sitemap index cannot run forever.
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

        # Follow child sitemap files on the same host.
        for child in re.findall(r"<loc>([^<]*sitemap[^<]*)</loc>", text, flags=re.I):
            child = child.replace("&amp;", "&").strip()
            parsed = urlparse(child)
            if parsed.netloc.lower() == "suncityhoa.org" and child not in seen_sitemaps:
                queue.append(child)

    print(
        f"Sitemap discovery: {len(urls)} event URLs "
        f"from {len(seen_sitemaps)} sitemap files"
    )
    return urls


def discover_from_existing_calendar() -> set[str]:
    """
    Recover source event URLs from the last good ICS file.

    This is only a fallback. It prevents a temporary SCHOA discovery outage
    from immediately turning a working feed into a hard failure.
    """
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
    """
    Discover SCHOA events using multiple independent methods.

    Priority is fresh public calendar pages and sitemaps. If those sources
    temporarily stop exposing links, retain known event URLs from the last
    successfully generated ICS file instead of failing immediately.
    """
    urls: set[str] = set()

    urls.update(discover_from_calendar_pages())
    urls.update(discover_from_sitemaps())

    # If fresh discovery is suspiciously small, recover known URLs too.
    if len(urls) < 3:
        print("Fresh discovery found fewer than 3 events; using fallback URLs")
        urls.update(discover_from_existing_calendar())

    result = sorted(urls)
    print(f"Discovered {len(result)} candidate event pages total")

    for url in result[:25]:
        print(f"  {url}")

    return result


def extract_ical_url(event_url: str) -> str | None:
    """Open an individual event page and locate its SCHOA iCal feed URL."""
    html = get(event_url).text

    # Accept absolute or relative versions and either literal & or HTML &amp;.
    match = re.search(
        r"(?:https?://suncityhoa\.org/)?\?rhc_action=get_icalendar_events(?:&amp;|&)ID=(\d+)",
        html,
        flags=re.I,
    )

    if not match:
        # Some pages expose ID= before the action URL in minified/script data.
        match = re.search(
            r"rhc_action=get_icalendar_events[^\"'<>]{0,120}?(?:&amp;|&)ID=(\d+)",
            html,
            flags=re.I,
        )

    if not match:
        return None

    event_id = match.group(1)
    return f"https://suncityhoa.org/?rhc_action=get_icalendar_events&ID={event_id}"


def fetch_event_calendar(ical_url: str) -> Calendar:
    """Download a SCHOA event's ICS data."""
    response = requests.get(
        ical_url,
        headers={
            **HEADERS,
            "Accept": "text/calendar,text/plain,*/*",
            "Referer": CALENDAR_URL,
        },
        timeout=45,
    )
    response.raise_for_status()
    return Calendar.from_ical(response.content)


def build_combined_calendar(event_pages: list[str]) -> tuple[Calendar, int]:
    combined = Calendar()
    combined.add("prodid", "-//Sun City Arizona Events Calendar//EN")
    combined.add("version", "2.0")
    combined.add("calscale", "GREGORIAN")
    combined.add("method", "PUBLISH")
    combined.add("x-wr-calname", "Sun City Events")
    combined.add("x-wr-timezone", "America/Phoenix")

    seen_uids = set()
    added = 0

    for index, event_url in enumerate(event_pages, 1):
        try:
            ical_url = extract_ical_url(event_url)

            if not ical_url:
                print(
                    f"[{index}/{len(event_pages)}] "
                    f"SKIP no iCal feed: {event_url}"
                )
                continue

            cal = fetch_event_calendar(ical_url)
            page_added = 0

            for component in cal.walk("VEVENT"):
                uid = str(component.get("UID", ""))
                if not uid:
                    uid = f"{event_url}-{component.get('DTSTART')}"

                if uid in seen_uids:
                    continue

                seen_uids.add(uid)

                if not component.get("URL"):
                    component.add("url", event_url)

                description = str(component.get("DESCRIPTION", "")).strip()
                source_note = f"Source: {event_url}"

                if source_note not in description:
                    if description:
                        description += "\n\n"
                    description += source_note

                    if component.get("DESCRIPTION"):
                        component["DESCRIPTION"] = description
                    else:
                        component.add("description", description)

                combined.add_component(component)
                page_added += 1
                added += 1

            print(
                f"[{index}/{len(event_pages)}] "
                f"added {page_added}: {event_url}"
            )

        except Exception as exc:
            print(
                f"[{index}/{len(event_pages)}] "
                f"ERROR {event_url}: {exc}"
            )

    return combined, added


def main():
    print("Discovering Sun City SCHOA events...")

    event_pages = discover_event_pages()
    if not event_pages:
        raise RuntimeError(
            "No SCHOA event pages discovered; existing calendar was not replaced."
        )

    calendar, count = build_combined_calendar(event_pages)
    print(f"Generated {count} unique calendar events")

    # Safety check so a broken scraper cannot overwrite a good feed.
    if count < 3:
        raise RuntimeError(
            f"Only {count} events were generated; refusing to publish a bad feed."
        )

    OUTPUT.write_bytes(calendar.to_ical())
    print(f"Wrote {OUTPUT} with {count} events")


if __name__ == "__main__":
    main()
