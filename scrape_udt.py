"""
UDT eSchool parent portal scraper.

Logs into the school's UDT eSchool parent portal via a plain form POST,
fetches the activity/messages page (all messages load on one page - no
pagination or scroll-triggered fetching needed), parses each message,
filters by date, and downloads any PDF attachments by following each
attachment's viewer page to find the real file URL.

SETUP:
    pip install -r requirements.txt
    cp .env.example .env
    # then edit .env and fill in your real values

USAGE:
    python scrape_udt.py
"""

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# CONFIG - loaded from .env (see .env.example)
# ---------------------------------------------------------------------------

load_dotenv()

BASE_URL = os.getenv("UDT_BASE_URL", "https://sarvottam.udtweb.com")
LOGIN_URL = f"{BASE_URL}/Logins/index"
ACTIVITY_URL = f"{BASE_URL}/parents/activity?type=3"

USERNAME = os.getenv("UDT_USERNAME")
PASSWORD = os.getenv("UDT_PASSWORD")

LOOKBACK_HOURS = float(os.getenv("LOOKBACK_HOURS", "36"))

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
PDF_DIR = Path(__file__).parent / "data" / "pdfs"
PDF_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
}

DATE_RE = re.compile(
    r"posted .*? on (\d{1,2} \w{3} \d{4} \d{1,2}:\d{2} [ap]m)", re.IGNORECASE
)
FILE_PATH_RE = re.compile(r'var\s+file_path\s*=\s*"([^"]+)"')


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_message(li) -> dict | None:
    header_p = li.select_one("._header p")
    if not header_p:
        return None

    match = DATE_RE.search(header_p.get_text(" ", strip=True))
    if not match:
        return None
    try:
        posted_dt = dateparser.parse(match.group(1))
    except Exception:
        return None

    title_tag = li.select_one("h4.post_title")
    title = title_tag.get_text(" ", strip=True) if title_tag else ""
    title = re.sub(r"\s+", " ", title).strip()

    body_paras = [
        p.get_text(" ", strip=True)
        for p in li.select("._content.gallery-deatils > p")
    ]
    body_text = "\n".join(t for t in body_paras if t)

    attachments = []
    for a in li.select("ul.list-images li a"):
        href = a.get("href", "")
        name_tag = a.find_next_sibling("div", class_="media_name")
        name = name_tag.get_text(strip=True) if name_tag else href
        attachments.append({"name": name, "href": href})

    return {
        "id": li.get("id", ""),
        "title": title,
        "posted_at": posted_dt.isoformat(),
        "body": body_text,
        "attachments": attachments,
    }


def extract_messages(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select("ul.list-row > li")
    parsed = []
    for li in items:
        msg = parse_message(li)
        if msg:
            parsed.append(msg)
    return parsed


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def login(session) -> None:
    if not USERNAME or not PASSWORD:
        raise RuntimeError(
            "UDT_USERNAME and/or UDT_PASSWORD are not set. "
            "Copy .env.example to .env and fill in your real credentials."
        )
    resp = session.post(
        LOGIN_URL,
        data={"username": USERNAME, "password": PASSWORD},
        allow_redirects=True,
    )
    resp.raise_for_status()
    if 'id="login-form"' in resp.text:
        raise RuntimeError(
            "Login appears to have failed - check UDT_USERNAME/UDT_PASSWORD in .env, "
            "or the site may need additional hidden form fields not seen "
            "in the static HTML (check the Network tab's Form Data on submit)."
        )


def fetch_activity_messages(session) -> list[dict]:
    activity_resp = session.get(ACTIVITY_URL)
    activity_resp.raise_for_status()
    return extract_messages(activity_resp.text)


def filter_recent(
    messages: list[dict], lookback_hours: float | None = None, cutoff: datetime | None = None
) -> list[dict]:
    if cutoff is None:
        if lookback_hours is None:
            lookback_hours = LOOKBACK_HOURS
        cutoff = datetime.now() - timedelta(hours=lookback_hours)
    filtered = [
        m for m in messages
        if dateparser.parse(m["posted_at"]) >= cutoff
    ]
    filtered.sort(key=lambda m: m["posted_at"], reverse=True)
    return filtered


def download_message_attachments(session, messages: list[dict]) -> None:
    for m in messages:
        for att in m["attachments"]:
            href = att["href"]
            if not href:
                continue
            viewer_url = href if href.startswith("http") else f"{BASE_URL}{href}"
            try:
                viewer_resp = session.get(viewer_url)
                match = FILE_PATH_RE.search(viewer_resp.text)
                if not match:
                    att["saved_as"] = None
                    att["note"] = "Could not find file_path in viewer page"
                    continue

                file_url = match.group(1).replace("\\/", "/")
                path_part = file_url.split("?")[0].split("#")[0]
                ext = ""
                if "." in path_part.rsplit("/", 1)[-1]:
                    ext = "." + path_part.rsplit(".", 1)[-1]
                msg_id = m.get("id") or att.get("href", "")
                safe_name = re.sub(r"[^\w\-. ]", "_", f"{msg_id}_{att['name']}") + ext
                dest_path = PDF_DIR / safe_name

                if dest_path.exists():
                    att["saved_as"] = str(dest_path)
                    att["source_url"] = file_url
                    att["note"] = "Already downloaded"
                    continue

                file_resp = session.get(file_url)
                dest_path.write_bytes(file_resp.content)
                att["saved_as"] = str(dest_path)
                att["source_url"] = file_url
            except Exception as e:
                att["saved_as"] = None
                att["note"] = f"Download failed: {e}"


def fetch_recent_messages(
    lookback_hours: float | None = None,
    cutoff: datetime | None = None,
    download_attachments: bool = False,
) -> list[dict]:
    """Log in, fetch the activity page, and return messages posted since
    `cutoff` if given, else within the rolling lookback window (hours),
    most recent first. Raises RuntimeError if login fails."""
    session = requests.Session()
    session.headers.update(HEADERS)
    login(session)
    all_messages = fetch_activity_messages(session)
    if not all_messages:
        print("No messages parsed - the page structure may differ from what "
              "was inspected, or login didn't actually succeed. Inspect "
              "activity_resp.text manually if this happens.")
    filtered = filter_recent(all_messages, lookback_hours=lookback_hours, cutoff=cutoff)
    if download_attachments:
        download_message_attachments(session, filtered)
    return filtered


def fetch_attendance(start: datetime, end: datetime) -> list[dict]:
    records = {}
    month = start.replace(day=1)
    with requests.Session() as session:
        session.headers.update(HEADERS)
        login(session)
        while (month.year, month.month) <= (end.year, end.month):
            response = session.get(
                f"{BASE_URL}/parents/get-student-attendance-data",
                params={"year": month.year, "month": month.month},
                timeout=30,
            )
            response.raise_for_status()
            entries = response.json()
            if not isinstance(entries, list):
                raise ValueError("Invalid attendance response")
            for entry in entries:
                attendance_date = datetime.strptime(entry["date"], "%Y-%m-%d").date()
                status = entry["title"].strip().lower()
                if status not in {"present", "absent", "leave"}:
                    raise ValueError("Unknown attendance status")
                if start.date() <= attendance_date <= end.date():
                    records[attendance_date.isoformat()] = {
                        "date": attendance_date.isoformat(), "status": status,
                    }
            month = month.replace(year=month.year + month.month // 12, month=month.month % 12 + 1)
    return [records[date] for date in sorted(records)]


def run():
    filtered = fetch_recent_messages(download_attachments=True)
    if not filtered:
        print("No messages parsed in the lookback window.")
        return

    out_file = OUTPUT_DIR / f"messages_{datetime.now():%Y%m%d_%H%M%S}.json"
    out_file.write_text(json.dumps(filtered, indent=2, ensure_ascii=False))
    print(f"Saved {len(filtered)} messages to {out_file}")
    print(f"PDFs (where successfully downloaded) are in {PDF_DIR}")


if __name__ == "__main__":
    run()
