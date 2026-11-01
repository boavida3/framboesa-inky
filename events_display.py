#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Inky Display: Calendar Events

Fetches upcoming events from private iCal feeds
and displays them on an Inky e-ink display.

Run as a daemon (default): python3 events_display.py
Run once and exit:         python3 events_display.py --once

Dependencies:
    pip install requests recurring-ical-events icalendar pillow
"""

import argparse
import datetime
import hashlib
import json
import logging
import time
import zoneinfo

import recurring_ical_events
import requests
from icalendar import Calendar
from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

import os
import re
import unicodedata

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_STATE_FILE = os.path.join(_SCRIPT_DIR, ".events_state.json")


def _load_env():
    """Load KEY=VALUE pairs from a .env file next to this script into os.environ."""
    env_path = os.path.join(_SCRIPT_DIR, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

_load_env()

# Read iCal URLs from environment; GCAL_ICAL_URL_1 and GCAL_ICAL_URL_2 set in .env
GCAL_ICAL_URLS = [v for k, v in sorted(os.environ.items()) if k.startswith("GCAL_ICAL_URL_") and v]
# Optional label per calendar (GCAL_NAME_1, GCAL_NAME_2, ...); defaults to "Calendar N"
GCAL_NAMES = [os.environ.get(f"GCAL_NAME_{k.rsplit('_', 1)[1]}") or f"Calendar {k.rsplit('_', 1)[1]}"
              for k, v in sorted(os.environ.items()) if k.startswith("GCAL_ICAL_URL_") and v]

_tz_name = os.environ.get("DISPLAY_TZ", "")
try:
    LOCAL_TZ = zoneinfo.ZoneInfo(_tz_name) if _tz_name else None
except zoneinfo.ZoneInfoNotFoundError:
    log.error("Timezone %r not found. Install tzdata: pip3 install tzdata", _tz_name)
    LOCAL_TZ = None

_rotation = os.environ.get("DISPLAY_ROTATION", "0")
if _rotation not in ("0", "90", "180", "270"):
    log.error("DISPLAY_ROTATION must be 0, 90, 180 or 270, not %r. Using 0.", _rotation)
    _rotation = "0"
# Degrees clockwise; 90 and 270 give a vertical layout
DISPLAY_ROTATION = int(_rotation)


# ---------------------------------------------------------------------------
# Event fetching
# ---------------------------------------------------------------------------

def sanitize_text(text):
    """Replace problematic Unicode chars with ASCII equivalents."""
    # Replace all types of dashes with a simple hyphen
    text = re.sub(r"[\u2010-\u2015\u2212\uFE58\uFE63\uFF0D]", "-", text)
    # Replace non-breaking and other spaces with regular space
    text = re.sub(r"[\u00a0\u2002-\u200b\u202f\u205f]", " ", text)
    # Replace smart quotes
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    return text


def fetch_calendar_events():
    """Fetch upcoming events from one or more private iCal feeds.

    Returns a list of event dicts, or raises RuntimeError if any fetch failed, so a
    temporary outage on one calendar can't look like its events were deleted.
    """
    now_local = datetime.datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.datetime.now().astimezone()
    today = now_local.date()
    cutoff = today + datetime.timedelta(days=90)
    events = []
    any_failed = False

    for cal_index, url in enumerate(GCAL_ICAL_URLS):
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            cal = Calendar.from_ical(resp.content)

            # Expands repeating events into their individual occurrences
            for component in recurring_ical_events.of(cal).between(today, cutoff + datetime.timedelta(days=1)):
                start = component.get("DTSTART").dt
                if isinstance(start, datetime.datetime):
                    if start.tzinfo is not None:
                        start = start.astimezone(LOCAL_TZ) if LOCAL_TZ else start.astimezone()
                    start_date = start.date()
                    date_str = start.strftime("%a %b %-d, %-I:%M %p")
                    sort_key = start.isoformat()
                else:
                    start_date = start
                    date_str = start.strftime("%a %b %-d")
                    sort_key = start.isoformat()

                if start_date < today or start_date > cutoff:
                    continue

                status = str(component.get("STATUS", "CONFIRMED")).upper()
                if status == "CANCELLED":
                    continue

                venue = sanitize_text(str(component.get("LOCATION", "")))
                if venue.startswith(("http://", "https://")):
                    venue = ""

                events.append({
                    "name": sanitize_text(str(component.get("SUMMARY", "(No title)"))),
                    "date": date_str,
                    "venue": venue,
                    "sort_key": sort_key,
                    "cal_index": cal_index,
                    "status": status,
                })
        except Exception as e:
            # Error messages include the request URL; redact the secret iCal path
            err = re.sub(r"/calendar/ical/\S+", "/calendar/ical/<redacted>", str(e))
            log.warning("Failed to fetch calendar %d: %s", cal_index + 1, err)
            any_failed = True

    if any_failed:
        raise RuntimeError("Calendar fetch failed")

    events.sort(key=lambda e: e["sort_key"])
    return dedupe_events(events)


def dedupe_events(events):
    """Drop events that appear on more than one calendar, keeping the first calendar's copy."""
    seen = set()
    unique = []
    for ev in sorted(events, key=lambda e: e["cal_index"]):
        key = (ev["sort_key"], ev["name"], ev["venue"])
        if key not in seen:
            seen.add(key)
            unique.append(ev)
    unique.sort(key=lambda e: e["sort_key"])
    return unique


def fetch_all_events():
    """Returns event list on success, or None if any fetch failed."""
    try:
        return fetch_calendar_events()
    except Exception as e:
        log.warning("Calendar fetch failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Change detection state
# ---------------------------------------------------------------------------

# Inky Impression 7.3" resolution, used to work out which events fit on screen
VISIBLE_WIDTH = 800
VISIBLE_HEIGHT = 480


def compute_events_hash(events):
    """Return an MD5 hex digest of the sorted event list."""
    key = sorted([(e["name"], e["date"], e["venue"], e.get("status", "CONFIRMED")) for e in events])
    return hashlib.md5(str(key).encode()).hexdigest()


def _layout_size(width, height):
    """Return the (width, height) to draw at, given the panel's size and DISPLAY_ROTATION."""
    return (height, width) if DISPLAY_ROTATION in (90, 270) else (width, height)


def _today():
    """Return today's date in the display's timezone."""
    return (datetime.datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.datetime.now().astimezone()).date()


def compute_visible_hash(events):
    """Return a digest of what the screen would show today.

    Events too far down the list to fit on screen don't affect it.
    """
    colors = {"WHITE": 0, "BLACK": 1, "RED": 2, "BLUE": 3, "GREEN": 4, "YELLOW": 5}
    width, height = _layout_size(VISIBLE_WIDTH, VISIBLE_HEIGHT)
    img = render_events_image(events, width, height, colors, now=datetime.datetime.combine(_today(), datetime.time()))
    return hashlib.md5(img.tobytes()).hexdigest()


def _has_meaningful_change(prev_fingerprints, events):
    """Return True if events changed in a way that warrants a display refresh.

    Returns True if there are new/changed events OR if future events were deleted.
    Returns False only if the change is purely natural expirations (past events dropping off).
    """
    prev_keys = {(e["sort_key"], e["name"], e["date"], e.get("venue", ""), e.get("status", "CONFIRMED")) for e in prev_fingerprints}
    curr_keys = {(e["sort_key"], e["name"], e["date"], e.get("venue", ""), e.get("status", "CONFIRMED")) for e in events}
    if curr_keys - prev_keys:
        return True  # new or changed events
    removed = prev_keys - curr_keys
    if not removed:
        return False
    # Check if any removed event is in the future (i.e. actively deleted, not expired)
    today = (datetime.datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.datetime.now().astimezone()).date()
    for sort_key, *_ in removed:
        try:
            event_date = datetime.date.fromisoformat(sort_key[:10])
            if event_date >= today:
                return True  # future event was deleted
        except ValueError:
            return True  # can't parse date, refresh to be safe
    return False  # only past events expired


def _layout_key():
    """Identify settings that need a redraw when they change: rotation and the day labels."""
    return f"{DISPLAY_ROTATION}|{_today().isoformat()}"


def load_state():
    """Return (hash_str, visible_hash, prev_fingerprints, layout) from the state file, or (None, None, [], None) if unavailable."""
    try:
        with open(_STATE_FILE) as f:
            data = json.load(f)
        return data.get("hash"), data.get("visible_hash"), data.get("events", []), data.get("layout")
    except Exception:
        return None, None, [], None


def save_state(hash_str, visible_hash, events):
    """Persist the current hash and event fingerprints to the state file."""
    fingerprints = [{"sort_key": e["sort_key"], "name": e["name"], "date": e["date"], "venue": e.get("venue", ""), "status": e.get("status", "CONFIRMED"), "cal_index": e.get("cal_index", 0)} for e in events]
    with open(_STATE_FILE, "w") as f:
        json.dump({
            "hash": hash_str,
            "visible_hash": visible_hash,
            "layout": _layout_key(),
            "updated_at": datetime.datetime.now().isoformat(),
            "events": fingerprints,
        }, f)


# ---------------------------------------------------------------------------
# Display rendering
# ---------------------------------------------------------------------------

FONT_PATH = os.path.join(_SCRIPT_DIR, "fonts", "AtkinsonHyperlegible-Bold.ttf")
FALLBACK_FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"


def _needs_fallback(text):
    """Return True if text contains characters outside Latin/common scripts."""
    for ch in text:
        if ord(ch) > 0x024F and unicodedata.category(ch).startswith("L"):
            return True
    return False


class FallbackFont:
    """Wraps a primary font + fallback for characters the primary can't render."""

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback
        # Expose attributes that PIL / our helpers might inspect
        self.size = getattr(primary, "size", None)

    def _pick(self, text):
        return self.fallback if _needs_fallback(text) else self.primary

    def getbbox(self, text, *args, **kw):
        return self._pick(text).getbbox(text, *args, **kw)

    def getmask(self, text, *args, **kw):
        return self._pick(text).getmask(text, *args, **kw)

    def getmask2(self, text, *args, **kw):
        return self._pick(text).getmask2(text, *args, **kw)

    # Let PIL's draw.text() call through to the right underlying font
    def __getattr__(self, name):
        return getattr(self.primary, name)


def getsize(font, text):
    """Get text ink dimensions (width, height) using the left-top anchor.

    Using anchor='lt' means the returned height is the actual visible ink height
    (top=0 always), which keeps header centering and line-spacing math exact.
    """
    left, _, right, bottom = font.getbbox(text, anchor="lt")
    return (right - left, bottom)


def truncate(text, font, max_width):
    """Truncate text with ellipsis if it exceeds max_width."""
    if getsize(font, text)[0] <= max_width:
        return text
    while getsize(font, text + "...")[0] > max_width and len(text) > 0:
        text = text[:-1]
    return text.rstrip() + "..."


def _load_font(size):
    """Load the display font, with a fallback for scripts it doesn't cover."""
    font = ImageFont.truetype(FONT_PATH, size)
    try:
        return FallbackFont(font, ImageFont.truetype(FALLBACK_FONT, size))
    except OSError:
        return font


def _day_label(day, today):
    """Return a plain-language heading for a date."""
    if day == today:
        return day.strftime("Today, %a %b %-d")
    if day == today + datetime.timedelta(days=1):
        return "Tomorrow"
    return day.strftime("%a %b %-d")


def _short_time(date_str):
    """Turn 'Tue Mar 3, 12:00 PM' into '12:00p'; all-day events have no time."""
    parts = date_str.split(", ", 1)
    return parts[1].replace(" AM", "a").replace(" PM", "p") if len(parts) > 1 else ""


def _place_rows(events, today, rows, cols):
    """Lay events out on a grid of slots, column by column.

    Returns (placements, hidden_count). Each placement is (slot, kind, value), where
    kind is "day" or "event". A heading always stays in the same column as its first
    event, and is repeated when a day continues into the next column.
    """
    slots = rows * cols
    placements = []
    pos = 0
    current_day = None
    for i, ev in enumerate(events):
        day = datetime.date.fromisoformat(ev["sort_key"][:10])
        needs_heading = day != current_day or pos % rows == 0
        if needs_heading and pos % rows == rows - 1:
            pos += 1
        needed = 2 if needs_heading else 1
        more_slot = 1 if i < len(events) - 1 else 0
        if pos + needed + more_slot > slots:
            return placements, len(events) - i
        if needs_heading:
            placements.append((pos, "day", _day_label(day, today)))
            pos += 1
            current_day = day
        placements.append((pos, "event", ev))
        pos += 1
    return placements, 0


def render_events_image(events, width, height, colors, now=None):
    """Render events as a day-grouped agenda on a PIL Image sized for the Inky display.

    Every row sits on one shared grid, so both columns line up across the screen.

    Args:
        events: list of event dicts with 'name', 'date', 'sort_key', 'cal_index' keys
        width: display width in pixels
        height: display height in pixels
        colors: dict with keys WHITE, BLACK, RED (int palette indices)
        now: datetime used to label days; defaults to the current time

    Returns:
        PIL.Image in palette mode
    """
    WHITE = colors["WHITE"]
    BLACK = colors["BLACK"]
    # Calendar index -> sidebar color
    cal_colors = [colors.get("BLUE", BLACK), colors["RED"], colors.get("GREEN", BLACK)]

    img = Image.new("P", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    scale = max(width, height) / 800.0
    title_font = _load_font(int(31 * scale))
    day_font = _load_font(int(25 * scale))

    margin = int(12 * scale)
    gutter = int(20 * scale)
    row_height = int(38 * scale)
    baseline = int(29 * scale)
    bar_width = int(7 * scale)
    text_inset = bar_width + int(7 * scale)
    time_gap = int(10 * scale)

    cols = 2 if width >= height else 1
    rows = (height - margin * 2) // row_height
    col_width = (width - margin * 2 - gutter * (cols - 1)) // cols
    top = (height - rows * row_height) // 2
    time_width = getsize(title_font, "12:00p")[0]

    now = now or (datetime.datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.datetime.now().astimezone())

    def slot_origin(slot):
        col, row = slot // rows, slot % rows
        return margin + col * (col_width + gutter), top + row * row_height

    if not events:
        x, y = slot_origin(0)
        draw.text((x, y + baseline), "No upcoming events.", fill=BLACK, font=title_font, anchor="ls")
        return img

    placements, hidden = _place_rows(events, now.date(), rows, cols)

    for slot, kind, value in placements:
        x, y = slot_origin(slot)
        if kind == "day":
            draw.text((x, y + baseline), value, fill=BLACK, font=day_font, anchor="ls")
            draw.line((x, y + row_height - 3, x + col_width, y + row_height - 3), fill=BLACK, width=2)
            continue
        draw.rectangle((x, y + 4, x + bar_width - 1, y + row_height - 4), fill=cal_colors[value.get("cal_index", 0) % len(cal_colors)])
        time_text = _short_time(value["date"])
        title_width = col_width - text_inset
        if time_text:
            title_width -= time_width + time_gap
            draw.text((x + col_width, y + baseline), time_text, fill=BLACK, font=title_font, anchor="rs")
        name = truncate(value["name"], title_font, title_width)
        draw.text((x + text_inset, y + baseline), name, fill=BLACK, font=title_font, anchor="ls")

    if hidden:
        x, y = slot_origin(rows * cols - 1)
        draw.text((x + col_width, y + baseline), f"+ {hidden} more", fill=BLACK, font=day_font, anchor="rs")

    return img


# ---------------------------------------------------------------------------
# Display output
# ---------------------------------------------------------------------------

def _init_display():
    """Initialize Inky hardware or return mock settings. Returns (inky_display, colors, w, h)."""
    try:
        from inky.auto import auto
        inky_display = auto(ask_user=True, verbose=True)
        inky_display.set_border(inky_display.WHITE)
        colors = {
            "WHITE": inky_display.WHITE,
            "BLACK": inky_display.BLACK,
            "RED": inky_display.RED,
            "BLUE": inky_display.BLUE,
            "GREEN": inky_display.GREEN,
            "YELLOW": inky_display.YELLOW,
        }
        return inky_display, colors, inky_display.width, inky_display.height
    except Exception as e:
        log.warning("Inky hardware not detected (%s), using mock 400x300 display.", e)
        return None, {"WHITE": 0, "BLACK": 1, "RED": 2, "BLUE": 3, "GREEN": 4, "YELLOW": 5}, 400, 300


def update_display(events):
    """Render events and push to hardware (or save preview PNG if no hardware)."""
    inky_display, colors, w, h = _init_display()
    img = render_events_image(events, *_layout_size(w, h), colors)
    if DISPLAY_ROTATION:
        img = img.rotate(-DISPLAY_ROTATION, expand=True)

    if inky_display:
        inky_display.set_image(img)
        inky_display.show()
        log.info("Display updated successfully.")
    else:
        preview_path = os.path.join(_SCRIPT_DIR, "events_preview.png")
        palette_img = img.copy()
        pal = [0] * 768  # 256 * 3
        pal[0:3] = [255, 255, 255]  # 0 = WHITE
        pal[3:6] = [0, 0, 0]        # 1 = BLACK
        pal[6:9] = [220, 0, 0]      # 2 = RED
        pal[9:12] = [0, 0, 255]     # 3 = BLUE
        pal[12:15] = [0, 180, 0]    # 4 = GREEN
        pal[15:18] = [200, 200, 0]  # 5 = YELLOW
        palette_img.putpalette(pal)
        palette_img.convert("RGB").save(preview_path)
        log.info("No display hardware. Preview saved to %s", preview_path)


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------

def refresh_if_changed(events):
    """Redraw the display only if the events on screen changed, and persist the new state."""
    new_hash = compute_events_hash(events)
    old_hash, old_visible_hash, prev_fingerprints, old_layout = load_state()
    layout_changed = _layout_key() != old_layout
    if new_hash == old_hash and not layout_changed:
        log.debug("No change, skipping display update.")
        return
    new_visible_hash = compute_visible_hash(events)
    if layout_changed:
        log.info("Day or rotation changed, updating display.")
        update_display(events)
    elif new_visible_hash == old_visible_hash:
        log.info("Change isn't visible on screen, skipping display refresh.")
    elif _has_meaningful_change(prev_fingerprints, events):
        log.info("Events changed, updating display.")
        update_display(events)
    else:
        log.info("Only event expirations, skipping display refresh.")
    save_state(new_hash, new_visible_hash, events)


def run_daemon(interval=60):
    """Poll for calendar changes every `interval` seconds and refresh display when changed."""
    log.info("Starting daemon (poll interval: %ds).", interval)
    try:
        while True:
            try:
                events = fetch_all_events()
                if events is None:
                    log.warning("Calendar fetch failed, keeping last display.")
                else:
                    refresh_if_changed(events)
            except Exception as e:
                log.warning("Daemon iteration failed: %s", e)
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("Daemon stopped.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Inky e-ink calendar display")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Fetch and render once then exit (default: run as daemon)",
    )
    args = parser.parse_args()

    if args.once:
        log.info("Fetching calendar events...")
        events = fetch_all_events()
        if events is None:
            log.warning("Calendar fetch failed.")
        elif events:
            log.info("Displaying %d events:", len(events))
            for ev in events:
                log.info("  - %s | %s | %s", ev["name"], ev["date"], ev["venue"])
        else:
            log.info("No upcoming events.")
        if events is not None:
            refresh_if_changed(events)
    else:
        run_daemon(interval=60)


if __name__ == "__main__":
    main()
