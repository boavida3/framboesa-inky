#!/usr/bin/env python3
"""Framboesa: upcoming calendar events on an Inky e-ink display.

Reads the private iCal feeds listed in .env, draws the next events and refreshes
the panel only when the picture changes. Without display hardware it saves
events_preview.png instead. Run it from cron once a minute.
"""

import datetime
import functools
import hashlib
import json
import logging
import os
import re
import unicodedata
import zoneinfo

import recurring_ical_events
import requests
from icalendar import Calendar
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, ".events_state.json")
FAILURE_FILE = os.path.join(HERE, ".fetch_failure.json")
PREVIEW_FILE = os.path.join(HERE, "events_preview.png")

# How far ahead to look for events
WINDOW = datetime.timedelta(days=90)
# A short outage on one calendar mustn't look like its events were deleted, so the
# screen stays as it is for this long before showing a warning and whatever still works.
FAILURE_GRACE = datetime.timedelta(hours=1)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_env():
    """Load KEY=VALUE lines from the .env file next to this script into os.environ."""
    try:
        with open(os.path.join(HERE, ".env")) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


load_env()

# Private iCal addresses, from GCAL_ICAL_URL_1, GCAL_ICAL_URL_2, ...
CALENDAR_URLS = [v for k, v in sorted(os.environ.items()) if k.startswith("GCAL_ICAL_URL_") and v]

try:
    TZ = zoneinfo.ZoneInfo(os.environ["DISPLAY_TZ"]) if os.environ.get("DISPLAY_TZ") else None
except zoneinfo.ZoneInfoNotFoundError:
    log.error("Timezone %r not found. Install tzdata: pip3 install tzdata", os.environ["DISPLAY_TZ"])
    TZ = None


def to_local(dt):
    """Convert an aware datetime to the display's timezone."""
    return dt.astimezone(TZ) if TZ else dt.astimezone()


def now():
    """The current time in the display's timezone."""
    return to_local(datetime.datetime.now(datetime.timezone.utc))


# ---------------------------------------------------------------------------
# Fetching events
# ---------------------------------------------------------------------------

def clean(text):
    """Replace fancy dashes, spaces and quotes with plain ones."""
    text = re.sub(r"[‐-―−﹘﹣－]", "-", text)
    text = re.sub(r"[  -​  ]", " ", text)
    text = text.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    return text.strip()


def has_finished(component, start_date, current):
    """Whether an event's end has passed, so events running past midnight stay listed."""
    end = component.get("DTEND")
    if end is None:
        return start_date < current.date()
    if not isinstance(end.dt, datetime.datetime):
        # An all-day event's end date is the day after it finishes
        return end.dt <= current.date()
    end_time = end.dt if end.dt.tzinfo else end.dt.replace(tzinfo=current.tzinfo)
    return end_time <= current


def fetch_events():
    """Fetch upcoming events from every calendar.

    Returns (events, failed_count). events is None if every calendar failed.
    """
    current = now()
    today = current.date()
    last_day = today + WINDOW
    events = []
    failed = 0

    for cal_index, url in enumerate(CALENDAR_URLS):
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            calendar = Calendar.from_ical(resp.content)
            # Expands repeating events into their individual occurrences
            for component in recurring_ical_events.of(calendar).between(today, last_day + datetime.timedelta(days=1)):
                if str(component.get("STATUS", "")).upper() == "CANCELLED":
                    continue
                start = component.get("DTSTART").dt
                all_day = not isinstance(start, datetime.datetime)
                if not all_day and start.tzinfo:
                    start = to_local(start)
                start_date = start if all_day else start.date()
                if start_date > last_day or has_finished(component, start_date, current):
                    continue
                events.append({
                    "name": clean(str(component.get("SUMMARY", ""))) or "(No title)",
                    "start": start.isoformat(),
                    "all_day": all_day,
                    "cal_index": cal_index,
                })
        except Exception as e:
            # Error messages include the request URL; redact the secret path
            log.warning("Failed to fetch calendar %d: %s", cal_index + 1, re.sub(r"/calendar/ical/\S+", "/calendar/ical/<redacted>", str(e)))
            failed += 1

    if CALENDAR_URLS and failed == len(CALENDAR_URLS):
        return None, failed
    return dedupe(events), failed


def dedupe(events):
    """Sort events by start, dropping ones that appear on more than one calendar (the first calendar's copy stays).

    The order is fully determined, so a feed listing same-time events differently
    between fetches doesn't change the picture.
    """
    seen = set()
    unique = []
    for ev in sorted(events, key=lambda e: (e["cal_index"], e["start"], e["name"])):
        key = (ev["start"], ev["name"])
        if key not in seen:
            seen.add(key)
            unique.append(ev)
    return sorted(unique, key=lambda e: (e["start"], e["name"]))


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

WIDTH, HEIGHT = 800, 480
# The image's palette. The Inky library maps these pure colors onto the panel's own.
WHITE, BLACK, RED, BLUE, GREEN, YELLOW = range(6)
PALETTE = [(255, 255, 255), (0, 0, 0), (255, 0, 0), (0, 0, 255), (0, 255, 0), (255, 255, 0)]
# Bar color for calendars 1, 2 and 3; further calendars repeat them
CALENDAR_COLORS = [BLUE, RED, YELLOW]

KOREAN_FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
KOREAN_FONT_INDEX = 1  # face order in the collection is JP, KR, SC, TC, HK
EMOJI_FONT = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"
EMOJI_FONT_SIZE = 109  # the only size this bitmap font contains
# One emoji, including its skin tone, variation selector and any joined emoji
_EMOJI_CHAR = "[☀-➿\U0001f000-\U0001faff]"
EMOJI_RE = re.compile(f"({_EMOJI_CHAR}(?:️|[\U0001f3fb-\U0001f3ff]|‍{_EMOJI_CHAR}|[\U0001f1e6-\U0001f1ff])*)")


def font(size, bold=True):
    """Hanken Grotesk at the given size, or Pillow's default font if it isn't installed."""
    try:
        from font_hanken_grotesk import HankenGroteskBold, HankenGroteskMedium
        return ImageFont.truetype(HankenGroteskBold if bold else HankenGroteskMedium, size)
    except (ImportError, OSError):
        return ImageFont.load_default()


def has_non_latin(text):
    """Whether text has letters outside the Latin scripts, such as Korean."""
    return any(ord(ch) > 0x024F and unicodedata.category(ch).startswith("L") for ch in text)


def text_size(text_font, text):
    """Width and ink height of text."""
    left, _, right, bottom = text_font.getbbox(text, anchor="lt")
    return right - left, bottom


def shorten(text, text_font, max_width):
    """Cut text to fit max_width, ending in '...'."""
    if text_size(text_font, text)[0] <= max_width:
        return text
    while text and text_size(text_font, text + "...")[0] > max_width:
        text = text[:-1]
    return text.rstrip() + "..."


@functools.lru_cache(maxsize=64)
def emoji_image(emoji, height):
    """An emoji as an RGBA image of the given height, or None if it can't be drawn."""
    try:
        emoji_font = ImageFont.truetype(EMOJI_FONT, EMOJI_FONT_SIZE)
    except OSError:
        return None
    canvas = Image.new("RGBA", (EMOJI_FONT_SIZE * 4, EMOJI_FONT_SIZE * 2), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text((EMOJI_FONT_SIZE // 2, EMOJI_FONT_SIZE // 2), emoji, font=emoji_font, embedded_color=True)
    bbox = canvas.getbbox()
    if not bbox:
        return None
    canvas = canvas.crop(bbox)
    return canvas.resize((max(1, canvas.width * height // canvas.height), height), Image.Resampling.LANCZOS)


def paste_emoji(img, emoji, x, y):
    """Paste an RGBA emoji onto the palette image using only the panel's colors.

    Colors are boosted because pale ones wash out on the panel, and a black outline
    keeps mostly white emoji visible against the background.
    """
    outline = 2
    emoji = ImageOps.expand(emoji, outline, (0, 0, 0, 0))
    x, y = x - outline, y - outline
    flat = Image.new("RGB", emoji.size, PALETTE[WHITE])
    flat.paste(emoji, mask=emoji.getchannel("A"))
    flat = ImageEnhance.Color(flat).enhance(1.8)
    flat = ImageEnhance.Contrast(flat).enhance(1.3)
    palette = Image.new("P", (1, 1))
    palette.putpalette(sum(PALETTE, ()))
    reduced = flat.quantize(palette=palette, dither=Image.Dither.NONE)
    mask = emoji.getchannel("A").point(lambda alpha: 255 if alpha > 128 else 0)
    img.paste(BLACK, (x, y, x + mask.width, y + mask.height), mask.filter(ImageFilter.MaxFilter(3)))
    img.paste(reduced, (x, y), mask)


def draw_title(img, draw, x, baseline, name, max_width, title_font, korean_font):
    """Draw a title with inline emoji, ending in '...' if it doesn't fit in max_width."""
    if korean_font and has_non_latin(name):
        title_font = korean_font
    emoji_height = int(title_font.size * 1.16)
    gap = int(title_font.size * 0.08)
    cursor = x
    limit = x + max_width
    # Splitting on the emoji pattern puts the emoji at the odd positions
    for index, part in enumerate(EMOJI_RE.split(name)):
        if not part:
            continue
        if index % 2:
            emoji = emoji_image(part, emoji_height)
            if emoji is None:
                continue
            if cursor + emoji.width > limit:
                draw.text((cursor, baseline), "...", fill=BLACK, font=title_font, anchor="ls")
                return
            paste_emoji(img, emoji, cursor, baseline - emoji_height + int(title_font.size * 0.2))
            cursor += emoji.width + gap
        else:
            remaining = limit - cursor
            if title_font.getlength(part) > remaining:
                if remaining > title_font.getlength("..."):
                    draw.text((cursor, baseline), shorten(part, title_font, remaining), fill=BLACK, font=title_font, anchor="ls")
                return
            draw.text((cursor, baseline), part, fill=BLACK, font=title_font, anchor="ls")
            cursor += int(title_font.getlength(part))


def render(events, warning=None):
    """Draw the agenda. The warning, if any, replaces the date in the header, in red."""
    current = now()
    header_font = font(26)
    corner_font = font(24, bold=False)
    title_font = font(38)
    detail_font = font(28, bold=False)
    try:
        korean_font = ImageFont.truetype(KOREAN_FONT, 38, index=KOREAN_FONT_INDEX)
    except OSError:
        korean_font = None

    img = Image.new("P", (WIDTH, HEIGHT), WHITE)
    img.putpalette(sum(PALETTE, ()))
    draw = ImageDraw.Draw(img)
    pad = 14
    right = WIDTH - pad

    # Header: month and year on the left, today's date (or the warning) on the right
    month = current.strftime("%B %Y")
    month_height = text_size(header_font, month)[1]
    draw.text((pad, pad), month, fill=BLACK, font=header_font, anchor="lt")
    corner = warning or current.strftime("%b %-d")
    corner_width, corner_height = text_size(corner_font, corner)
    draw.text((right - corner_width, pad + (month_height - corner_height) // 2), corner, fill=RED if warning else BLACK, font=corner_font, anchor="lt")
    header_height = month_height + pad * 2
    draw.line((pad, header_height, right, header_height), fill=BLACK)

    # Rows: a colored bar for the calendar, the title, then date and time columns on the right
    top = header_height + 4
    row_height = 61
    baseline = 44
    bar_width = 12
    text_x = pad + bar_width + 6
    gap = 22
    time_right = right
    date_right = time_right - text_size(detail_font, "10:00 AM")[0] - gap
    title_width = date_right - text_size(detail_font, "Wed Nov 30")[0] - gap - text_x
    rows = (HEIGHT - top) // row_height

    if not events:
        draw.text((text_x, top + baseline), "No upcoming events.", fill=BLACK, font=title_font, anchor="ls")
        return img

    for i, ev in enumerate(events[:rows]):
        y = top + i * row_height
        start = datetime.datetime.fromisoformat(ev["start"])
        days_away = (start.date() - current.date()).days
        date_label = {0: "Today", 1: "Tomorrow"}.get(days_away, start.strftime("%a %b %-d"))
        time_label = "All Day" if ev["all_day"] else start.strftime("%-I:%M %p")
        draw.rectangle((pad, y + 7, pad + bar_width - 1, y + row_height - 8), fill=CALENDAR_COLORS[ev["cal_index"] % len(CALENDAR_COLORS)])
        draw_title(img, draw, text_x, y + baseline, ev["name"], title_width, title_font, korean_font)
        draw.text((date_right, y + baseline), date_label, fill=BLACK, font=detail_font, anchor="rs")
        draw.text((time_right, y + baseline), time_label, fill=BLACK, font=detail_font, anchor="rs")
        if i < min(rows, len(events)) - 1:
            draw.line((pad, y + row_height - 1, right, y + row_height - 1), fill=BLACK)
    return img


# ---------------------------------------------------------------------------
# Updating the display
# ---------------------------------------------------------------------------

def show(img):
    """Push the image to the Inky panel, or save a preview if there's no hardware."""
    try:
        from inky.auto import auto
        inky = auto(ask_user=True, verbose=True)
    except Exception as e:
        img.convert("RGB").save(PREVIEW_FILE)
        log.warning("No Inky display (%s). Preview saved to %s", e, PREVIEW_FILE)
        return
    inky.set_border(inky.WHITE)
    inky.set_image(img)
    inky.show()
    log.info("Display updated.")


def load_state():
    """The last saved hash and events, or {} on the first run."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def refresh(events, warning=None):
    """Redraw the panel only if the picture changed, and remember what was shown.

    Each redraw takes about 30 seconds and flashes the panel, so needless ones are avoided.
    """
    img = render(events, warning)
    digest = hashlib.md5(img.tobytes()).hexdigest()
    if digest != load_state().get("hash"):
        log.info("Screen changed, updating display.")
        show(img)
    with open(STATE_FILE, "w") as f:
        json.dump({"hash": digest, "updated_at": now().isoformat(), "events": events}, f)


def failing_since():
    """When calendar fetches started failing, recording now if this is the first failure."""
    try:
        with open(FAILURE_FILE) as f:
            return datetime.datetime.fromisoformat(json.load(f)["since"])
    except (OSError, ValueError, KeyError):
        since = datetime.datetime.now(datetime.timezone.utc)
        with open(FAILURE_FILE, "w") as f:
            json.dump({"since": since.isoformat()}, f)
        return since


def main():
    """Fetch the calendars and update the display, tolerating failures.

    Within FAILURE_GRACE of a failure, the screen stays as it is. After that, it shows
    a warning with the calendars that still work, or the last known events if none do.
    """
    events, failed = fetch_events()
    if not failed:
        if os.path.exists(FAILURE_FILE):
            os.remove(FAILURE_FILE)
        log.info("Displaying %d events.", len(events))
        refresh(events)
        return

    since = failing_since()
    if datetime.datetime.now(datetime.timezone.utc) - since < FAILURE_GRACE:
        log.warning("%d calendar(s) failed, keeping last display.", failed)
        return
    if events is None:
        events = load_state().get("events", [])
    log.warning("%d calendar(s) failing since %s, showing a warning.", failed, since.isoformat())
    refresh(events, to_local(since).strftime("Not updated since %b %-d %-I:%M %p"))


if __name__ == "__main__":
    main()
