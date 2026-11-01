#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Inky Display: Calendar Events

Fetches upcoming events from private iCal feeds
and displays them on an Inky e-ink display.

Run as a daemon (default): python3 events_display.py
Run once and exit:         python3 events_display.py --once

Dependencies:
    pip install requests recurring-ical-events icalendar pillow font-hanken-grotesk
"""

import argparse
import datetime
import functools
import hashlib
import json
import logging
import time
import zoneinfo

import recurring_ical_events
import requests
from icalendar import Calendar
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

import os
import re
import unicodedata

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_STATE_FILE = os.path.join(_SCRIPT_DIR, ".events_state.json")
_FAILURE_FILE = os.path.join(_SCRIPT_DIR, ".fetch_failure.json")
# A short outage on one calendar mustn't look like its events were deleted, so the
# screen stays as it is for this long before showing a warning and whatever still works.
FAILURE_GRACE = datetime.timedelta(hours=1)


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
    return text.strip()


def _has_finished(component, start_date, now_local):
    """Return True if an event's end has passed, so events running past midnight stay listed."""
    end = component.get("DTEND")
    if end is None:
        return start_date < now_local.date()
    if not isinstance(end.dt, datetime.datetime):
        # An all-day event's end date is the day after it finishes
        return end.dt <= now_local.date()
    end_time = end.dt
    if end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=now_local.tzinfo)
    return end_time <= now_local


def fetch_calendar_events():
    """Fetch upcoming events from one or more private iCal feeds.

    Returns (events, failed_count). events is None if every calendar failed.
    """
    now_local = datetime.datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.datetime.now().astimezone()
    today = now_local.date()
    cutoff = today + datetime.timedelta(days=90)
    events = []
    failed_count = 0

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

                if start_date > cutoff or _has_finished(component, start_date, now_local):
                    continue

                status = str(component.get("STATUS", "CONFIRMED")).upper()
                if status == "CANCELLED":
                    continue

                venue = sanitize_text(str(component.get("LOCATION", "")))
                if venue.startswith(("http://", "https://")):
                    venue = ""

                events.append({
                    "name": sanitize_text(str(component.get("SUMMARY", ""))) or "(No title)",
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
            failed_count += 1

    if GCAL_ICAL_URLS and failed_count == len(GCAL_ICAL_URLS):
        return None, failed_count

    events.sort(key=lambda e: e["sort_key"])
    return dedupe_events(events), failed_count


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
    """Return a digest of what the screen would show, ignoring the current date and time.

    Events too far down the list to fit on screen don't affect it.
    """
    colors = {"WHITE": 0, "BLACK": 1, "RED": 2, "BLUE": 3, "GREEN": 4, "YELLOW": 5}
    width, height = _layout_size(VISIBLE_WIDTH, VISIBLE_HEIGHT)
    img = render_events_image(events, width, height, colors, now=datetime.datetime.combine(_today(), datetime.time()))
    return hashlib.md5(img.tobytes()).hexdigest()


def _layout_key(warning=None):
    """Identify what needs a redraw when it changes: rotation, the date on screen and any warning."""
    return f"{DISPLAY_ROTATION}|{_today().isoformat()}|{warning or ''}"


def _failing_since():
    """Return when calendar fetches started failing, recording now if this is the first failure."""
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        with open(_FAILURE_FILE) as f:
            return datetime.datetime.fromisoformat(json.load(f)["since"])
    except Exception:
        with open(_FAILURE_FILE, "w") as f:
            json.dump({"since": now.isoformat()}, f)
        return now


def _clear_failure():
    """Forget any recorded failure after all calendars fetch successfully."""
    if os.path.exists(_FAILURE_FILE):
        os.remove(_FAILURE_FILE)


def load_state():
    """Return (hash_str, visible_hash, prev_fingerprints, layout) from the state file, or (None, None, [], None) if unavailable."""
    try:
        with open(_STATE_FILE) as f:
            data = json.load(f)
        return data.get("hash"), data.get("visible_hash"), data.get("events", []), data.get("layout")
    except Exception:
        return None, None, [], None


def save_state(hash_str, visible_hash, events, layout):
    """Persist the current hash and event fingerprints to the state file."""
    fingerprints = [{"sort_key": e["sort_key"], "name": e["name"], "date": e["date"], "venue": e.get("venue", ""), "status": e.get("status", "CONFIRMED"), "cal_index": e.get("cal_index", 0)} for e in events]
    with open(_STATE_FILE, "w") as f:
        json.dump({
            "hash": hash_str,
            "visible_hash": visible_hash,
            "layout": layout,
            "updated_at": datetime.datetime.now().isoformat(),
            "events": fingerprints,
        }, f)


# ---------------------------------------------------------------------------
# Display rendering
# ---------------------------------------------------------------------------

FALLBACK_FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
# Face order in the collection is JP, KR, SC, TC, HK
FALLBACK_FONT_KOREAN_INDEX = 1
EMOJI_FONT = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"
# The only size this bitmap font contains
EMOJI_FONT_SIZE = 109
# One emoji, including its skin tone, variation selector and any joined emoji
_EMOJI_CHAR = "[\u2600-\u27bf\U0001f000-\U0001faff]"
_EMOJI_RE = re.compile(f"({_EMOJI_CHAR}(?:\ufe0f|[\U0001f3fb-\U0001f3ff]|\u200d{_EMOJI_CHAR}|[\U0001f1e6-\U0001f1ff])*)")
# The panel's colors, used to convert emoji images
PANEL_RGB = {"WHITE": (255, 255, 255), "BLACK": (0, 0, 0), "RED": (220, 0, 0), "BLUE": (0, 0, 255), "GREEN": (0, 180, 0), "YELLOW": (240, 210, 0)}


def _needs_fallback(text):
    """Return True if text contains characters outside Latin/common scripts."""
    for ch in text:
        if ord(ch) > 0x024F and unicodedata.category(ch).startswith("L"):
            return True
    return False


@functools.lru_cache(maxsize=64)
def _emoji_image(emoji, height):
    """Return an emoji as an RGBA image of the given height, or None if it can't be drawn."""
    try:
        font = ImageFont.truetype(EMOJI_FONT, EMOJI_FONT_SIZE)
    except OSError:
        return None
    canvas = Image.new("RGBA", (EMOJI_FONT_SIZE * 4, EMOJI_FONT_SIZE * 2), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text((EMOJI_FONT_SIZE // 2, EMOJI_FONT_SIZE // 2), emoji, font=font, embedded_color=True)
    bbox = canvas.getbbox()
    if not bbox:
        return None
    canvas = canvas.crop(bbox)
    return canvas.resize((max(1, canvas.width * height // canvas.height), height), Image.Resampling.LANCZOS)


def _paste_emoji(img, emoji_image, x, y, colors):
    """Paste an RGBA emoji onto the palette image using only the panel's colors.

    Colors are boosted because pale ones wash out on the panel, and a black outline
    keeps mostly white emoji visible against the background.
    """
    outline_room = 2
    emoji_image = ImageOps.expand(emoji_image, outline_room, (0, 0, 0, 0))
    x -= outline_room
    y -= outline_room
    names = [name for name in PANEL_RGB if name in colors]
    palette = Image.new("P", (1, 1))
    palette.putpalette([channel for name in names for channel in PANEL_RGB[name]])
    flat = Image.new("RGB", emoji_image.size, PANEL_RGB["WHITE"])
    flat.paste(emoji_image, mask=emoji_image.getchannel("A"))
    flat = ImageEnhance.Color(flat).enhance(1.8)
    flat = ImageEnhance.Contrast(flat).enhance(1.3)
    reduced = flat.quantize(palette=palette, dither=Image.Dither.NONE)
    lookup = [colors[names[i]] if i < len(names) else 0 for i in range(256)]
    mask = emoji_image.getchannel("A").point(lambda alpha: 255 if alpha > 128 else 0)
    img.paste(colors["BLACK"], (x, y, x + mask.width, y + mask.height), mask.filter(ImageFilter.MaxFilter(3)))
    img.paste(reduced.point(lookup), (x, y), mask)


def _draw_title(img, draw, x, baseline, name, max_width, font, fallback_font, colors):
    """Draw a title with inline emoji, ending in '...' if it doesn't fit in max_width."""
    if fallback_font and _needs_fallback(name):
        font = fallback_font
    emoji_height = int(font.size * 1.16)
    gap = int(font.size * 0.08)
    cursor = x
    limit = x + max_width
    ellipsis_width = int(font.getlength("..."))
    for index, part in enumerate(_EMOJI_RE.split(name)):
        if not part:
            continue
        if index % 2:
            emoji_image = _emoji_image(part, emoji_height)
            if emoji_image is None:
                continue
            if cursor + emoji_image.width > limit:
                draw.text((cursor, baseline), "...", fill=colors["BLACK"], font=font, anchor="ls")
                return
            _paste_emoji(img, emoji_image, cursor, baseline - emoji_height + int(font.size * 0.2), colors)
            cursor += emoji_image.width + gap
        else:
            remaining = limit - cursor
            if font.getlength(part) > remaining:
                if remaining > ellipsis_width:
                    draw.text((cursor, baseline), truncate(part, font, remaining), fill=colors["BLACK"], font=font, anchor="ls")
                return
            draw.text((cursor, baseline), part, fill=colors["BLACK"], font=font, anchor="ls")
            cursor += int(font.getlength(part))


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


def render_events_image(events, width, height, colors, now=None, warning=None):
    """Render event list onto a PIL Image sized for the Inky display.

    Args:
        events: list of event dicts with 'name', 'date', 'venue' keys
        width: display width in pixels
        height: display height in pixels
        colors: dict with keys WHITE, BLACK, RED (int palette indices)
        now: datetime for the header and day labels; defaults to the current time
        warning: text shown in red in place of the header date, if updates are failing

    Returns:
        PIL.Image in palette mode
    """
    WHITE = colors["WHITE"]
    BLACK = colors["BLACK"]
    RED = colors["RED"]
    BLUE = colors.get("BLUE", BLACK)
    YELLOW = colors.get("YELLOW", BLACK)

    # Calendar index → sidebar color (names come from GCAL_NAME_* in .env)
    CAL_COLORS = [BLUE, RED, YELLOW]

    img = Image.new("P", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    # Scale fonts and spacing based on display width
    scale = max(width, height) / 800.0
    title_size = max(14, int(26 * scale))
    event_size = max(10, int(38 * scale))
    detail_size = max(9, int(24 * scale))

    # Try loading nice fonts, fall back to default.
    def load_font(primary_path, dejavu_name, size):
        try:
            return ImageFont.truetype(primary_path, size)
        except (TypeError, OSError):
            try:
                return ImageFont.truetype(f"/usr/share/fonts/truetype/dejavu/{dejavu_name}", size)
            except OSError:
                return ImageFont.load_default()

    try:
        from font_hanken_grotesk import HankenGroteskBold, HankenGroteskMedium
    except ImportError:
        HankenGroteskBold = HankenGroteskMedium = None

    title_font = load_font(HankenGroteskBold, "DejaVuSans-Bold.ttf", title_size)
    event_font = load_font(HankenGroteskBold, "DejaVuSans-Bold.ttf", event_size)
    detail_font = load_font(HankenGroteskMedium, "DejaVuSans.ttf", detail_size)
    meta_font = load_font(HankenGroteskMedium, "DejaVuSans.ttf", int(28 * scale))
    # Titles in other scripts, such as Korean, use this font when it's installed
    try:
        event_fallback_font = ImageFont.truetype(FALLBACK_FONT, event_size, index=FALLBACK_FONT_KOREAN_INDEX)
    except OSError:
        event_fallback_font = None

    padding = max(6, int(14 * scale))
    bar_width = max(3, int(12 * scale))
    bar_indent = bar_width + max(4, int(6 * scale))
    content_width = width - (padding * 2) - bar_indent

    # --- Header (no background rectangle) ---
    now = now or (datetime.datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.datetime.now().astimezone())
    header_text = now.strftime("%B %Y")
    date_header = now.strftime("%b %-d")

    title_h = getsize(title_font, header_text)[1]
    header_height = title_h + padding * 2

    # Left: calendar name in black
    draw.text((padding, padding), header_text, fill=BLACK, font=title_font, anchor="lt")

    # Right: current date in detail font, vertically centered with the title
    date_w, date_h = getsize(detail_font, warning or date_header)
    date_y = padding + (title_h - date_h) // 2
    draw.text((width - padding - date_w, date_y), warning or date_header, fill=RED if warning else BLACK, font=detail_font, anchor="lt")

    # Thin separator below header
    draw.line((padding, header_height, width - padding, header_height), fill=BLACK, width=1)

    # --- Event list ---
    # Title on the left, with date and time in right-aligned columns. On a vertical
    # display one line is too narrow, so the date and time move to a second line.
    list_top = header_height + int(4 * scale)
    stacked = width < height
    row_height = int((100 if stacked else 61) * scale)
    title_baseline = int(44 * scale)
    meta_baseline = int(84 * scale) if stacked else title_baseline
    right = width - padding
    text_x = padding + bar_indent
    column_gap = int(22 * scale)

    time_right = right
    date_right = time_right - getsize(meta_font, "10:00 AM")[0] - column_gap
    date_left = date_right - getsize(meta_font, "Wed Nov 30")[0]
    title_width = (right if stacked else date_left - column_gap) - text_x

    rows = (height - list_top) // row_height

    if not events:
        draw.text((text_x, list_top + title_baseline), "No upcoming events.", fill=BLACK, font=event_font, anchor="ls")
        return img

    for i, ev in enumerate(events[:rows]):
        y = list_top + i * row_height
        cal_color = CAL_COLORS[ev.get("cal_index", 0) % len(CAL_COLORS)]

        draw.rectangle((padding, y + 7, padding + bar_width - 1, y + row_height - 8), fill=cal_color)
        _draw_title(img, draw, text_x, y + title_baseline, ev["name"], title_width, event_font, event_fallback_font, colors)

        parts = ev["date"].split(", ", 1)
        days_away = (datetime.date.fromisoformat(ev["sort_key"][:10]) - now.date()).days
        date_label = {0: "Today", 1: "Tomorrow"}.get(days_away, parts[0])
        draw.text((date_right, y + meta_baseline), date_label, fill=BLACK, font=meta_font, anchor="rs")
        draw.text((time_right, y + meta_baseline), parts[1] if len(parts) > 1 else "All Day", fill=BLACK, font=meta_font, anchor="rs")

        if i < rows - 1 and i < len(events) - 1:
            line_y = y + row_height - 1
            draw.line((padding, line_y, right, line_y), fill=BLACK, width=1)

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


def update_display(events, warning=None):
    """Render events and push to hardware (or save preview PNG if no hardware)."""
    inky_display, colors, w, h = _init_display()
    img = render_events_image(events, *_layout_size(w, h), colors, warning=warning)
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

def refresh_if_changed(events, warning=None):
    """Redraw the display only if what's on screen changed, and persist the new state."""
    new_hash = compute_events_hash(events)
    old_hash, old_visible_hash, _, old_layout = load_state()
    layout = _layout_key(warning)
    layout_changed = layout != old_layout
    if new_hash == old_hash and not layout_changed:
        log.debug("No change, skipping display update.")
        return
    new_visible_hash = compute_visible_hash(events)
    if layout_changed:
        log.info("Day, rotation or warning changed, updating display.")
        update_display(events, warning)
    elif new_visible_hash == old_visible_hash:
        log.info("Change isn't visible on screen, skipping display refresh.")
    else:
        log.info("Events changed, updating display.")
        update_display(events, warning)
    save_state(new_hash, new_visible_hash, events, layout)


def poll():
    """Fetch calendars once and update the display, tolerating failures.

    Within FAILURE_GRACE of a failure, the screen stays as it is. After that, it shows
    a warning with the calendars that still work, or the last known events if none do.
    """
    events, failed_count = fetch_calendar_events()
    if not failed_count:
        _clear_failure()
        log.info("Displaying %d events.", len(events))
        refresh_if_changed(events)
        return

    since = _failing_since()
    if datetime.datetime.now(datetime.timezone.utc) - since < FAILURE_GRACE:
        log.warning("%d calendar(s) failed, keeping last display.", failed_count)
        return

    if events is None:
        events = load_state()[2]
    log.warning("%d calendar(s) failing since %s, showing a warning.", failed_count, since.isoformat())
    last_success = since.astimezone(LOCAL_TZ) if LOCAL_TZ else since.astimezone()
    refresh_if_changed(events, last_success.strftime("Not updated since %b %-d %-I:%M %p"))


def run_daemon(interval=60):
    """Poll for calendar changes every `interval` seconds and refresh display when changed."""
    log.info("Starting daemon (poll interval: %ds).", interval)
    try:
        while True:
            try:
                poll()
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
        poll()
    else:
        run_daemon(interval=60)


if __name__ == "__main__":
    main()
