#!/usr/bin/env python3
"""
Copenhell 2026 schedule checker.
Fetches the Clashfinder page for copenhell2026 and compares it against
the last-known state saved in scripts/schedule-state.json.

Outputs:
  changed=true  via GITHUB_OUTPUT if the schedule has been updated.
"""

import json
import os
import hashlib
import sys
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies — run: pip install requests beautifulsoup4", file=sys.stderr)
    sys.exit(1)

STATE_FILE = Path(__file__).parent / "schedule-state.json"
CLASHFINDER_URL = "https://clashfinder.com/s/copenhell2026/"
COPENHELL_URL   = "https://copenhell.dk/lineup/"

# ── helpers ──────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def set_output(key: str, value: str) -> None:
    """Write a GitHub Actions step output."""
    gho = os.environ.get("GITHUB_OUTPUT")
    if gho:
        with open(gho, "a") as f:
            f.write(f"{key}={value}\n")
    else:
        # Local run — just print
        print(f"OUTPUT: {key}={value}")


def fetch_page(url: str, timeout: int = 20) -> str | None:
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "copenhell-schedule-bot/1.0"})
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        print(f"WARNING: could not fetch {url}: {exc}", file=sys.stderr)
        return None


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    state = load_state()
    changed = False

    # ── 1. Clashfinder snapshot ──────────────────────────────────────────────
    cf_html = fetch_page(CLASHFINDER_URL)
    if cf_html:
        cf_hash = content_hash(cf_html)
        prev_cf  = state.get("clashfinder_hash", "")
        if cf_hash != prev_cf:
            print(f"Clashfinder page changed (was {prev_cf[:12] or 'none'}, now {cf_hash[:12]})")
            state["clashfinder_hash"] = cf_hash
            changed = True
        else:
            print("Clashfinder page unchanged.")
    else:
        print("Clashfinder fetch failed — skipping hash check.")

    # ── 2. Copenhell lineup page ─────────────────────────────────────────────
    cp_html = fetch_page(COPENHELL_URL)
    if cp_html:
        soup = BeautifulSoup(cp_html, "html.parser")

        # Collect artist names visible on the lineup page
        # The site typically lists artists in <h2>, <h3>, or element with class "artist"
        artist_els = (
            soup.select(".artist-name")
            or soup.select("h2.name")
            or soup.select("[class*='artist'] h2")
            or soup.select("h3")
        )
        artists = sorted({el.get_text(strip=True) for el in artist_els if el.get_text(strip=True)})

        cp_hash = content_hash("|".join(artists))
        prev_cp = state.get("lineup_hash", "")
        if cp_hash != prev_cp:
            prev_artists = set(state.get("lineup_artists", []))
            curr_artists  = set(artists)
            added   = curr_artists - prev_artists
            removed = prev_artists - curr_artists
            if added:
                print(f"New artists on lineup page: {', '.join(sorted(added))}")
            if removed:
                print(f"Artists removed from lineup page: {', '.join(sorted(removed))}")
            state["lineup_hash"]    = cp_hash
            state["lineup_artists"] = artists
            changed = True
        else:
            print("Copenhell lineup page unchanged.")
    else:
        print("Copenhell fetch failed — skipping lineup check.")

    # ── Persist state & emit output ─────────────────────────────────────────
    save_state(state)
    set_output("changed", "true" if changed else "false")
    print("Done —", "changes detected." if changed else "no changes.")


if __name__ == "__main__":
    main()
