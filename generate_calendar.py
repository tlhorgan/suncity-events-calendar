from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar


BASE_URL = "https://suncityhoa.org"
EVENTS_URL = "https://suncityhoa.org/events/"
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
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()
    return response


def discover_event_pages() -> list[str]:
    """
    Discover SCHOA event pages from the site's sitemap(s)
    instead of relying on the JavaScript-driven /events/ page.
    """

    sitemap_candidates = [
        "https://suncityhoa.org/wp-sitemap.xml",
        "https://suncityhoa.org/sitemap_index.xml",
        "https://suncityhoa.org/post-sitemap.xml",
        "https://suncityhoa.org/page-sitemap.xml",
    ]

    urls = set()

    for sitemap_url in sitemap_candidates:
        try:
            response = get(sitemap_url)

            text = response.text

            # Collect individual event URLs found in sitemap XML.
            for match in re.findall(
                r"<loc>(https://suncityhoa\.org/events/[^<]+)</loc>",
                text,
                flags=re.I,
            ):
                url = (
                    match.replace("&amp;", "&")
                    .split("?")[0]
                    .split("#")[0]
                    .rstrip("/")
                )

                if url.rstrip("/") != EVENTS_URL.rstrip("/"):
                    urls.add(url)

            # Some sitemap files are indexes pointing to more sitemap files.
            child_sitemaps = re.findall(
                r"<loc>(https://suncityhoa\.org/[^<]*sitemap[^<]*)</loc>",
                text,
                flags=re.I,
            )

            for child_url in child_sitemaps:
                try:
                    child = get(
                        child_url.replace("&amp;", "&")
                    ).text

                    for match in re.findall(
                        r"<loc>(https://suncityhoa\.org/events/[^<]+)</loc>",
                        child,
                        flags=re.I,
                    ):
                        url = (
                            match.replace("&amp;", "&")
                            .split("?")[0]
                            .split("#")[0]
                            .rstrip("/")
                        )

                        if url.rstrip("/") != EVENTS_URL.rstrip("/"):
                            urls.add(url)

                except Exception as exc:
                    print(
                        f"Could not inspect child sitemap "
                        f"{child_url}: {exc}"
                    )

        except Exception as exc:
            print(
                f"Could not inspect sitemap "
                f"{sitemap_url}: {exc}"
            )

    urls = sorted(urls)

    print(
        f"Discovered {len(urls)} candidate event pages"
    )

    for url in urls[:20]:
        print(f"  {url}")

    return urls

def extract_ical_url(event_url: str) -> str | None:
    """
    Open an individual event page and locate its SCHOA iCal feed URL.
    """

    html = get(event_url).text

    match = re.search(
        r'https://suncityhoa\.org/\?rhc_action=get_icalendar_events(?:&amp;|&)ID=(\d+)',
        html,
        flags=re.I,
    )

    if not match:
        return None

    event_id = match.group(1)

    return (
        "https://suncityhoa.org/"
        f"?rhc_action=get_icalendar_events&ID={event_id}"
    )


def fetch_event_calendar(ical_url: str) -> Calendar:
    """
    Download a SCHOA event's ICS data.
    """

    response = requests.get(
        ical_url,
        headers={
            **HEADERS,
            "Accept": "text/calendar,text/plain,*/*",
            "Referer": EVENTS_URL,
        },
        timeout=45,
    )

    response.raise_for_status()

    return Calendar.from_ical(response.content)


def build_combined_calendar(event_pages: list[str]) -> tuple[Calendar, int]:
    combined = Calendar()

    combined.add(
        "prodid",
        "-//Sun City Arizona Events Calendar//EN",
    )
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

                # Preserve the source event page in the calendar entry.
                if not component.get("URL"):
                    component.add("url", event_url)

                description = str(
                    component.get("DESCRIPTION", "")
                ).strip()

                source_note = f"Source: {event_url}"

                if source_note not in description:
                    if description:
                        description += "\n\n"

                    description += source_note

                    if component.get("DESCRIPTION"):
                        component["DESCRIPTION"] = description
                    else:
                        component.add(
                            "description",
                            description,
                        )

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
            "No SCHOA event pages discovered; "
            "existing calendar was not replaced."
        )

    calendar, count = build_combined_calendar(
        event_pages
    )

    print(f"Generated {count} unique calendar events")

    # Safety check so a broken scraper cannot overwrite a good feed.
    if count < 3:
        raise RuntimeError(
            f"Only {count} events were generated; "
            "refusing to publish a bad feed."
        )

    OUTPUT.write_bytes(
        calendar.to_ical()
    )

    print(
        f"Wrote {OUTPUT} with {count} events"
    )


if __name__ == "__main__":
    main()
