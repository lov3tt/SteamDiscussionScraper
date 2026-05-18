#!/usr/bin/env python3
"""Probe Steam discussion pages for layout differences."""
import re
import httpx
from bs4 import BeautifulSoup

GAMES = [
    ("Battlefield 2042", 1517290),
    ("PUBG", 578080),
    ("CoD MW III", 2519060),
    ("CoD MW II", 1938090),
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def probe(name: str, app_id: int) -> None:
    # Forum index (lists subforums) vs forum 0 thread list
    for label, url in [
        ("index", f"https://steamcommunity.com/app/{app_id}/discussions/"),
        ("forum0", f"https://steamcommunity.com/app/{app_id}/discussions/0/?fp=0"),
    ]:
        _probe_url(name, app_id, label, url)


def _probe_url(name: str, app_id: int, label: str, url: str) -> None:
    r = httpx.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=25)
    soup = BeautifulSoup(r.text, "html.parser")
    topics = soup.select("a.forum_topic_name")
    forums = soup.select("a.forum_link")
    title = (soup.title.string or "") if soup.title else ""
    pat = re.compile(rf"steamcommunity\.com/app/{app_id}/discussions/\d+/\d+")
    links = [a for a in soup.find_all("a", href=True) if pat.search(a["href"])]

    print(f"=== {name} ({app_id}) [{label}] status={r.status_code}")
    print(f"  final_url: {r.url}")
    print(f"  title: {title[:70]}")
    print(f"  forum_topic_name: {len(topics)}")
    print(f"  forum_link (subforums): {len(forums)}")
    for a in forums[:6]:
        href = a.get("href", "")
        print(f"    - {a.get_text(strip=True)[:50]!r} -> {href[:70]}")
    print(f"  thread links (regex): {len(links)}")
    if topics[:2]:
        for a in topics[:2]:
            print(f"    topic: {a.get_text(strip=True)[:50]!r}")
    if not topics and not links:
        for hint in ("discussions are disabled", "No forums", "Sign in", "age"):
            if hint.lower() in r.text.lower():
                print(f"  hint found in HTML: {hint!r}")
    print()


if __name__ == "__main__":
    for name, app_id in GAMES:
        probe(name, app_id)
