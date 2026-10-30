# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

E-ink display dashboard for a Raspberry Pi + Pimoroni Inky display. Fetches upcoming events from Google Calendar private iCal feeds, renders them onto a PIL image, and pushes to the hardware display. Falls back to saving a PNG preview when no hardware is detected.

## Development Environment

- `~/pi-remote/` is an **SSHFS mount of the Pi's home directory** (`/home/pi/`) — edits here go directly to the Pi
- Pi SSH: `ssh <pi-ip>` (user `pi`)
- The Pi runs **UTC system timezone**; `datetime.now()` returns UTC on the Pi
- Scripts run on the Pi, not the Mac; `inky.auto` is not importable on macOS (no I2C hardware)
- Python on Pi: `/path/to/venv/bin/python3` (Python 3.13)
- Display hardware: **Pimoroni Spectra 6 7.3" (800×480, E673)**

## Running

```bash
# Install dependencies (run on the Pi — Linux, not macOS)
pip3 install requests icalendar pillow font-hanken-grotesk tzdata

# Run manually on the Pi (fetches, renders, updates display, then exits)
/path/to/venv/bin/python3 /home/pi/events_display.py --once

# Run as daemon (polls every 60s, only refreshes display on actual changes)
python3 events_display.py
```

### Scheduling

Currently deployed via **cron** (not systemd):
```
* * * * * /path/to/venv/bin/python3 /home/pi/events_display.py --once >> /home/pi/events_display.log 2>&1
```
Runs every minute. Uses `--once` to avoid stacking daemon processes.

The `--once` path includes state-checking: it compares the fetched events against `.events_state.json` and only pushes to the e-ink display when new/changed events are detected (prevents flicker).

A `events-display.service` systemd unit file exists but is **inactive** — use cron or enable it, not both.

## Architecture

Single-file script (`events_display.py`) with this data flow:

1. **Fetch** — `fetch_calendar_events()` reads one or more Google Calendar private iCal URLs (set via `GCAL_ICAL_URL_*` in `.env`), filters to events within the next 30 days, converts datetimes to local timezone, and returns up to 8 events sorted by start time.
2. **Render** — `render_events_image(events, width, height, colors)` draws a clean calendar list-view layout (black header with date, red meta rows, black event titles, thin separators) onto a PIL `"P"` mode (palette) image using a fixed 3-color palette: WHITE=0, BLACK=1, RED=2.
3. **Display** — `update_display()` tries `from inky.auto import auto` to detect hardware. On success, pushes the image to the display. On failure, saves `events_preview.png`.
4. **Daemon** — `run_daemon()` polls every 60 seconds, computes an MD5 hash of the event list, and only re-renders when events change. State is persisted to `.events_state.json` (stores both hash and event fingerprints). Pure event expirations (no new/changed events) skip the display refresh to avoid flicker.

### Key functions

| Function | Role |
|---|---|
| `fetch_calendar_events()` | Fetches iCal feeds, converts UTC→`LOCAL_TZ`, filters to next 30 days |
| `fetch_all_events()` | Wrapper with error handling around `fetch_calendar_events()` |
| `compute_events_hash()` | MD5 of sorted event list for change detection |
| `save_state(hash, events)` | Persists hash + fingerprints `{sort_key, name, date}` to `.events_state.json` |
| `load_state()` | Returns `(hash_str, prev_fingerprints)` tuple from `.events_state.json` |
| `sanitize_text()` | Normalizes Unicode (dashes, smart quotes, spaces) to ASCII |
| `render_events_image()` | Full layout: header, events loop, footer; all sizes scale from `width / 800.0` |
| `getsize(font, text)` | Wrapper around `font.getbbox(anchor="lt")` — returns `(ink_width, ink_height)`; `anchor="lt"` ensures `top=0` so heights are exact visible ink heights, preventing overlap in layout math |
| `truncate(text, font, max_width)` | Ellipsis truncation to fit display width |

### Configuration (`.env`)

```
GCAL_ICAL_URL_1=https://calendar.google.com/calendar/ical/.../basic.ics
GCAL_ICAL_URL_2=...               # optional second calendar
GCAL_NAME_1=Me                    # optional label shown per calendar (GCAL_NAME_2, ...)
DISPLAY_TZ=America/Los_Angeles    # timezone for all display times
```

- `DISPLAY_TZ` is read into `LOCAL_TZ = zoneinfo.ZoneInfo(DISPLAY_TZ)` (Python 3.9+ stdlib). All event times and the footer "Updated:" timestamp use `LOCAL_TZ`. Required because the Pi runs UTC — without this, `.astimezone()` with no arg returns UTC.
- Get iCal URLs from: Google Calendar → Settings → [Calendar] → Integrate calendar → "Secret address in iCal format".

### Inky library (`inky/`)

Vendored Pimoroni Inky v2.3.0. `inky.auto.auto()` reads the Pi's I2C EEPROM to identify the connected display and returns the correct driver. Supports pHAT (250×122), wHAT (400×300), Impressions 7.3" (800×480), Spectra variants, etc.

## Layout (in `render_events_image`)

Apple/Google Calendar list-view style:
- **Header**: "My Calendar" in BLACK (title font, left-aligned), current date in BLACK (detail font, right-aligned), thin separator below. No background fill.
- **Events**: meta row in RED (`Tue Mar 3  ·  12:00 PM  ·  Venue`, detail font) above event title in BLACK (event font). Gap between events.
- **Footer**: thin separator, right-aligned "Updated: Feb 25, 9:10 PM" in BLACK (detail font).

Font sizes scale proportionally: `scale = width / 800.0`
- Title font: 42pt × scale; event font: 32pt × scale; detail font: 26pt × scale
- Preferred font: HankenGroteskBold/Medium → DejaVuSans → PIL default
