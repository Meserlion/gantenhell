#!/usr/bin/env python3
"""
check_schedule.py - Daily Copenhell 2026 schedule checker.
Polls copenhell.dk WordPress REST API for posts newer than last_post_id,
detects lineup changes, patches SCHEDULE in index.html, sets GHA output.
"""
import html as _html, json, os, re
from datetime import datetime, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup

ROOT       = Path(__file__).parent.parent
INDEX_HTML = ROOT / "index.html"
STATE_FILE = Path(__file__).parent / "schedule-state.json"

WP_API       = "https://copenhell.dk/wp-json/wp/v2/posts"
FETCH_PARAMS = {"per_page": 20, "order": "desc", "orderby": "id"}
HEADERS      = {"User-Agent": "Mozilla/5.0 (compatible; gantenhell-schedule-checker/1.0)"}
LINEUP_CATEGORIES = {285, 284}
CHANGE_KEYWORDS = re.compile(
    r"\b(replac|cancel|postpon|withdraw|pull.?out|unable|new.?addition|join|added)\b",
    re.IGNORECASE,
)
STAGE_TOKENS = {
    "helviti": "Helviti", "hades": "Hades",
    "pandaemonium": "Pandaemonium", "gehenna": "Gehenna", "boneyard": "Boneyard",
}
DAY_TOKENS = {
    "wednesday": "Wed Jun 24", "thursday": "Thu Jun 25",
    "friday": "Fri Jun 26",    "saturday": "Sat Jun 27",
    "wed": "Wed Jun 24", "thu": "Thu Jun 25",
    "fri": "Fri Jun 26", "sat": "Sat Jun 27",
}
DAYS = ["Wed Jun 24", "Thu Jun 25", "Fri Jun 26", "Sat Jun 27"]

def set_output(name, value):
    gho = os.environ.get("GITHUB_OUTPUT", "")
    if gho:
        open(gho, "a").write(name + "=" + value + "\n")
    else:
        print("::set-output name=" + name + "::" + value)

def load_state():
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except: pass
    return {"last_post_id": 0, "processed_slugs": []}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")

def fetch_new_posts(last_id):
    try:
        r = requests.get(WP_API, params=FETCH_PARAMS, headers=HEADERS, timeout=15)
        r.raise_for_status()
        posts = r.json()
    except Exception as exc:
        print("[error] WP API failed: " + str(exc))
        return []
    new = [p for p in posts if p.get("id", 0) > last_id]
    print("[info] " + str(len(posts)) + " post(s) from API; " + str(len(new)) + " new since id " + str(last_id))
    return new

def is_lineup_post(post):
    if set(post.get("categories", [])) & LINEUP_CATEGORIES:
        return True
    title = _html.unescape(post.get("title", {}).get("rendered", ""))
    slug  = post.get("slug", "")
    return bool(CHANGE_KEYWORDS.search(title) or CHANGE_KEYWORDS.search(slug))

def strip_html(raw):
    return BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)

def clean_name(name):
    name = _html.unescape(name).strip(" -–—.,!?").strip()
    name = re.sub(r"\s+(replaces?|has|have|will|is|are|at|on|the)$", "", name, flags=re.IGNORECASE)
    return name.strip()

def find_stage_and_day(text):
    lo = text.lower()
    stage = next((v for k, v in STAGE_TOKENS.items() if k in lo), None)
    day   = next((v for k, v in DAY_TOKENS.items()  if k in lo), None)
    return stage, day

def parse_post(post):
    title    = _html.unescape(post.get("title", {}).get("rendered", ""))
    content  = strip_html(post.get("content", {}).get("rendered", ""))
    slug     = post.get("slug", "")
    combined = title + " " + content
    changes  = []

    # "X REPLACES Y"
    m = re.search(r"(.+?)\s+replaces?\s+(.+)", title, re.IGNORECASE) or \
        re.search(r"(.+?)\s+replaces?\s+(.+)", slug.replace("-", " "), re.IGNORECASE)
    if m:
        new_a, old_a = clean_name(m.group(1)), clean_name(m.group(2))
        if new_a and old_a:
            changes.append({"type": "replace", "old": old_a, "new": new_a})
            return changes

    # "X HAS HAD TO CANCEL" / "UNFORTUNATELY X HAS..."
    m = re.search(
        r"([\w\s\-.'&]+?)\s+(?:has|have)\s+(?:had\s+to\s+)?cancel"
        r"|unfortunately,?\s+([\w\s\-.'&]+?)\s+(?:has|have|will)",
        combined, re.IGNORECASE,
    )
    if m:
        artist = clean_name(m.group(1) or m.group(2) or "")
        if artist:
            rm = re.search(
                r"replacement[^:]*[:,.]\s*([\w\s\-.'&]+)"
                r"|replaced by[:\s]+([\w\s\-.'&]+)"
                r"|instead,?\s+([\w\s\-.'&]+)",
                content, re.IGNORECASE,
            )
            if rm:
                rep = clean_name(rm.group(1) or rm.group(2) or rm.group(3) or "")
                if rep:
                    changes.append({"type": "replace", "old": artist, "new": rep})
                    return changes
            changes.append({"type": "cancel", "artist": artist})
            return changes

    # "SAXON ADDED TO THE LINEUP" or "NEW ADDITION: SAXON"
    m = re.search(r"^(.+?)\s+(?:added to|joins|joining)\b", title, re.IGNORECASE) or \
        re.search(r"(?:new addition|added to lineup|added to the lineup)[:\s]+([A-Z][\w\s\-.'&]+)", combined, re.IGNORECASE)
    if m:
        artist = clean_name(m.group(1))
        stage, day = find_stage_and_day(combined)
        if artist:
            changes.append({"type": "add", "artist": artist, "stage": stage, "day": day, "start": None, "end": None})

    return changes

_SCHEDULE_RE = re.compile(r"(const SCHEDULE\s*=\s*)(\{.*?\})\s*;", re.DOTALL)

def _js_to_json(raw):
    raw = re.sub(r"//[^\n]*", "", raw)
    raw = re.sub(r"([{,\s])([a-zA-Z_]\w*)\s*:", r'\1"\2":', raw)
    raw = re.sub(r"'([^']*)'", r'"\1"', raw)
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    return raw

def extract_schedule(html):
    m = _SCHEDULE_RE.search(html)
    if not m:
        raise ValueError("Could not locate const SCHEDULE in index.html")
    return json.loads(_js_to_json(m.group(2)))

def schedule_to_js(schedule):
    day_blocks = []
    for day in DAYS:
        shows = sorted(schedule.get(day, []), key=lambda s: (s["stage"], s["start"]))
        rows  = ",\n".join(
            "  {stage:'" + s["stage"] + "', artist:'" + s["artist"] +
            "', start:'" + s["start"] + "',end:'" + s["end"] + "'}"
            for s in shows
        )
        day_blocks.append("'" + day + "':[\n" + rows + "\n]")
    return "{\n" + ",\n".join(day_blocks) + "\n}"

def apply_change(schedule, change):
    ctype = change["type"]
    if ctype == "replace":
        old_lo, new_nm = change["old"].strip().lower(), change["new"].strip()
        for day, shows in schedule.items():
            for show in shows:
                if show["artist"].lower() == old_lo:
                    show["artist"] = new_nm
                    return schedule, "Replaced '" + change["old"] + "' -> '" + new_nm + "' on " + day + " @ " + show["stage"]
        # fuzzy
        for day, shows in schedule.items():
            for show in shows:
                a = show["artist"].lower()
                if old_lo in a or a in old_lo:
                    orig = show["artist"]
                    show["artist"] = new_nm
                    return schedule, "Replaced '" + orig + "' (fuzzy '" + change["old"] + "') -> '" + new_nm + "' on " + day
        return schedule, "[skipped] '" + change["old"] + "' not found in schedule."
    elif ctype == "cancel":
        lo = change["artist"].lower()
        for day, shows in schedule.items():
            for i, show in enumerate(shows):
                if show["artist"].lower() == lo:
                    rem = shows.pop(i)
                    return schedule, "Removed '" + rem["artist"] + "' from " + day + " @ " + rem["stage"]
        return schedule, "[skipped] '" + change["artist"] + "' not found to cancel."
    elif ctype == "add":
        artist, day, stage = change["artist"], change.get("day"), change.get("stage")
        start  = change.get("start") or "TBA"
        end    = change.get("end")   or "TBA"
        if not (day and stage):
            return schedule, "[skipped] Not enough info to place '" + artist + "' (day=" + str(day) + ", stage=" + str(stage) + ")."
        schedule[day].append({"stage": stage, "artist": artist, "start": start, "end": end})
        return schedule, "Added '" + artist + "' to " + day + " @ " + str(stage)
    return schedule, None

def main():
    state   = load_state()
    last_id = int(state.get("last_post_id", 0))
    slugs   = list(state.get("processed_slugs", []))
    state["last_checked"] = datetime.now(timezone.utc).isoformat()

    print("[info] Checking for posts newer than id " + str(last_id) + "...")
    new_posts = fetch_new_posts(last_id)
    if not new_posts:
        print("[info] No new posts.")
        save_state(state); set_output("changed", "false"); return

    relevant = [p for p in new_posts if is_lineup_post(p) and p.get("slug") not in slugs]
    print("[info] " + str(len(relevant)) + " lineup-relevant post(s).")
    max_id = max(p["id"] for p in new_posts)

    if not relevant:
        state["last_post_id"] = max_id
        save_state(state); set_output("changed", "false"); return

    html_text = INDEX_HTML.read_text(encoding="utf-8")
    try:
        schedule = extract_schedule(html_text)
    except Exception as exc:
        print("[error] " + str(exc)); set_output("changed", "false"); return

    applied = []
    for post in sorted(relevant, key=lambda p: p["id"]):
        pid   = post["id"]
        title = _html.unescape(post.get("title", {}).get("rendered", ""))
        slug  = post.get("slug", "")
        print("\n[post " + str(pid) + "] " + title)
        changes = parse_post(post)
        if not changes:
            print("  -> No actionable changes.")
            slugs.append(slug); continue
        for ch in changes:
            print("  -> Detected: " + str(ch))
            schedule, desc = apply_change(schedule, ch)
            if desc:
                print("  -> " + desc)
                if not desc.startswith("[skipped]"):
                    applied.append("[post " + str(pid) + "] " + desc)
        slugs.append(slug)

    state["last_post_id"]    = max_id
    state["processed_slugs"] = slugs[-50:]

    if not applied:
        print("\n[info] No schedule changes applied.")
        save_state(state); set_output("changed", "false"); return

    new_js       = schedule_to_js(schedule)
    updated_html = _SCHEDULE_RE.sub(lambda m: m.group(1) + new_js + ";", html_text)
    today        = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    updated_html = re.sub(r"updated \d{4}-\d{2}-\d{2}", "updated " + today, updated_html)
    INDEX_HTML.write_text(updated_html, encoding="utf-8")

    print("\n[info] index.html patched with " + str(len(applied)) + " change(s):")
    for line in applied: print("  " + line)
    state["last_updated"] = today
    state["last_changes"] = applied
    save_state(state)
    set_output("changed", "true")

if __name__ == "__main__":
    main()
