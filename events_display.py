#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Inky Display: Google Calendar Events

Fetches upcoming events from Google Calendar private iCal feeds
and displays them on an Inky e-ink display.

Run as a daemon (default): python3 events_display.py
Run once and exit:         python3 events_display.py --once

Dependencies:
    pip install requests icalendar pillow font-hanken-grotesk
"""

import argparse
import datetime
import hashlib
import json
import logging
import time
import zoneinfo

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
    """Fetch upcoming events from one or more Google Calendar private iCal feeds.

    Returns a list of event dicts, or raises RuntimeError if all fetches failed.
    """
    now_local = datetime.datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.datetime.now().astimezone()
    today = now_local.date()
    cutoff = today + datetime.timedelta(days=90)
    events = []
    any_success = False

    for cal_index, url in enumerate(GCAL_ICAL_URLS):
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            cal = Calendar.from_ical(resp.content)
            any_success = True

            for component in cal.walk():
                if component.name != "VEVENT":
                    continue

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
            log.warning("Failed to fetch calendar: %s", e)

    if not any_success and GCAL_ICAL_URLS:
        raise RuntimeError("All calendar fetches failed")

    events.sort(key=lambda e: e["sort_key"])
    return events


def fetch_all_events():
    """Returns event list on success, or None if all fetches failed."""
    try:
        return fetch_calendar_events()
    except Exception as e:
        log.warning("Calendar fetch failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Change detection state
# ---------------------------------------------------------------------------

def compute_events_hash(events):
    """Return an MD5 hex digest of the sorted event list."""
    key = sorted([(e["name"], e["date"], e["venue"], e.get("status", "CONFIRMED")) for e in events])
    return hashlib.md5(str(key).encode()).hexdigest()


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


def load_state():
    """Return (hash_str, prev_fingerprints) from the state file, or (None, []) if unavailable."""
    try:
        with open(_STATE_FILE) as f:
            data = json.load(f)
        return data.get("hash"), data.get("events", [])
    except Exception:
        return None, []


def save_state(hash_str, events):
    """Persist the current hash and event fingerprints to the state file."""
    fingerprints = [{"sort_key": e["sort_key"], "name": e["name"], "date": e["date"], "venue": e.get("venue", ""), "status": e.get("status", "CONFIRMED"), "cal_index": e.get("cal_index", 0)} for e in events]
    with open(_STATE_FILE, "w") as f:
        json.dump({
            "hash": hash_str,
            "updated_at": datetime.datetime.now().isoformat(),
            "events": fingerprints,
        }, f)


# ---------------------------------------------------------------------------
# Display rendering
# ---------------------------------------------------------------------------

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


def render_events_image(events, width, height, colors):
    """Render event list onto a PIL Image sized for the Inky display.

    Args:
        events: list of event dicts with 'name', 'date', 'venue' keys
        width: display width in pixels
        height: display height in pixels
        colors: dict with keys WHITE, BLACK, RED (int palette indices)

    Returns:
        PIL.Image in palette mode
    """
    WHITE = colors["WHITE"]
    BLACK = colors["BLACK"]
    RED = colors["RED"]
    BLUE = colors.get("BLUE", BLACK)
    GREEN = colors.get("GREEN", BLACK)

    # Calendar index → sidebar color (names come from GCAL_NAME_* in .env)
    CAL_COLORS = [BLUE, RED, GREEN]
    CAL_NAMES = GCAL_NAMES or ["Calendar"]

    img = Image.new("P", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    # Scale fonts and spacing based on display width
    scale = width / 800.0
    title_size = max(14, int(26 * scale))
    event_size = max(10, int(32 * scale))
    detail_size = max(9, int(24 * scale))

    # Try loading nice fonts, fall back to default.
    # Each font is wrapped with FallbackFont for non-Latin character support.
    try:
        from font_hanken_grotesk import HankenGroteskBold, HankenGroteskMedium
        title_font = FallbackFont(ImageFont.truetype(HankenGroteskBold, title_size),
                                  ImageFont.truetype(FALLBACK_FONT, title_size))
        event_font = FallbackFont(ImageFont.truetype(HankenGroteskMedium, event_size),
                                  ImageFont.truetype(FALLBACK_FONT, event_size))
        detail_font = FallbackFont(ImageFont.truetype(HankenGroteskMedium, detail_size),
                                   ImageFont.truetype(FALLBACK_FONT, detail_size))
    except (ImportError, OSError):
        try:
            title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", title_size)
            event_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", event_size)
            detail_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", detail_size)
            # Wrap with fallback if available
            try:
                title_font = FallbackFont(title_font, ImageFont.truetype(FALLBACK_FONT, title_size))
                event_font = FallbackFont(event_font, ImageFont.truetype(FALLBACK_FONT, event_size))
                detail_font = FallbackFont(detail_font, ImageFont.truetype(FALLBACK_FONT, detail_size))
            except OSError:
                pass
        except OSError:
            title_font = ImageFont.load_default()
            event_font = title_font
            detail_font = title_font

    padding = max(6, int(14 * scale))
    bar_width = max(3, int(8 * scale))
    bar_indent = bar_width + max(4, int(6 * scale))
    content_width = width - (padding * 2) - bar_indent

    # --- Header (no background rectangle) ---
    now = datetime.datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.datetime.now().astimezone()
    header_text = now.strftime("%B %Y")
    date_header = now.strftime("%b %-d")

    title_h = getsize(title_font, header_text)[1]
    header_height = title_h + padding * 2

    # Left: calendar name in black
    draw.text((padding, padding), header_text, fill=BLACK, font=title_font, anchor="lt")

    # Right: current date in detail font, vertically centered with the title
    date_w, date_h = getsize(detail_font, date_header)
    date_y = padding + (title_h - date_h) // 2
    draw.text((width - padding - date_w, date_y), date_header, fill=BLACK, font=detail_font, anchor="lt")

    # Thin separator below header
    draw.line((padding, header_height, width - padding, header_height), fill=BLACK, width=1)

    # --- Event list ---
    y = header_height + int(6 * scale)
    event_gap = max(6, int(10 * scale))
    meta_gap = max(1, int(2 * scale))

    # Reserve space for footer: tiny timestamp in bottom-right corner
    footer_size = max(7, int(14 * scale))
    try:
        from font_hanken_grotesk import HankenGroteskMedium
        footer_font = ImageFont.truetype(HankenGroteskMedium, footer_size)
    except (ImportError, OSError):
        try:
            footer_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", footer_size)
        except OSError:
            footer_font = ImageFont.load_default()
    footer_margin = max(4, int(6 * scale))
    footer_reserve = getsize(footer_font, "A")[1] + footer_margin

    text_x = padding + bar_indent

    if not events:
        msg = "No upcoming events."
        draw.text((text_x, y + int(20 * scale)), msg, fill=BLACK, font=event_font, anchor="lt")
        return img

    for i, ev in enumerate(events):
        # Check if there's room for at least the meta + name rows
        needed = getsize(detail_font, "A")[1] + meta_gap + getsize(event_font, "A")[1]
        if y + needed > height - footer_reserve:
            remaining = len(events) - i
            draw.text((text_x, y), f"+ {remaining} more", fill=RED, font=detail_font, anchor="lt")
            break

        y_start = y

        # Title row
        name = truncate(ev["name"], event_font, content_width)
        draw.text((text_x, y), name, fill=BLACK, font=event_font, anchor="lt")
        y += getsize(event_font, name)[1] + meta_gap

        # Detail row: "Calendar name  ·  Tue Mar 3  ·  12:00 PM [·  venue]"
        cal_index = ev.get("cal_index", 0)
        cal_color = CAL_COLORS[cal_index % len(CAL_COLORS)]
        cal_name = CAL_NAMES[cal_index % len(CAL_NAMES)]
        parts = ev["date"].split(", ", 1)
        date_part = parts[0]
        time_part = parts[1] if len(parts) > 1 else "All Day"
        meta = f"{cal_name}  \u00b7  {date_part}  \u00b7  {time_part}"
        if ev.get("venue"):
            meta += f"  \u00b7  {ev['venue']}"
        meta = truncate(meta, detail_font, content_width)
        draw.text((text_x, y), meta, fill=cal_color, font=detail_font, anchor="lt")
        y += getsize(detail_font, meta)[1]

        # Colored sidebar bar
        draw.rectangle((padding, y_start, padding + bar_width - 1, y - 1), fill=cal_color)

        y += event_gap

        # Separator line between events
        if i < len(events) - 1:
            draw.line((padding, y, width - padding, y), fill=BLACK, width=1)
            y += event_gap

    # --- Footer: tiny timestamp, bottom-right corner ---
    footer_text = now.strftime("%-m/%-d %-I:%M %p")
    footer_w, footer_h = getsize(footer_font, footer_text)
    draw.text((width - padding - footer_w, height - footer_h - footer_margin), footer_text, fill=BLACK, font=footer_font, anchor="lt")

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
    img = render_events_image(events, w, h, colors)

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
                    new_hash = compute_events_hash(events)
                    old_hash, prev_fingerprints = load_state()
                    if new_hash != old_hash:
                        if _has_meaningful_change(prev_fingerprints, events):
                            log.info("Events changed, updating display.")
                            if events:
                                log.info("Displaying %d events:", len(events))
                                for ev in events:
                                    log.info("  - %s | %s | %s", ev["name"], ev["date"], ev["venue"])
                            else:
                                log.info("No upcoming events.")
                            update_display(events)
                        else:
                            log.info("Only event expirations, skipping display refresh.")
                        save_state(new_hash, events)
                    else:
                        log.debug("No change, skipping.")
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
            new_hash = compute_events_hash(events)
            old_hash, prev_fingerprints = load_state()
            if new_hash != old_hash:
                if _has_meaningful_change(prev_fingerprints, events):
                    log.info("Events changed, updating display.")
                    update_display(events)
                else:
                    log.info("Only event expirations, skipping display refresh.")
                save_state(new_hash, events)
            else:
                log.debug("No change, skipping display update.")
    else:
        run_daemon(interval=60)


if __name__ == "__main__":
    main()
