# AGENTS.md

Guidance for working with code in this repository.

## Purpose

Calendar display for a Raspberry Pi Zero 2 W with a Pimoroni Inky Impression 7.3" (Spectra 6, 800x480, E673). It fetches private iCal feeds, renders an agenda onto a palette image and pushes it to the panel. Without display hardware it saves `events_preview.png` instead.

## Running

Run on the Pi from the repo root, using its virtual environment:

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python events_display.py --once   # check once and exit
.venv/bin/python events_display.py          # or poll every 60 seconds
```

Deployed with cron, running `--once` every minute under `flock` so two runs can't draw at the same time:

```
* * * * * flock -n /tmp/events_display.lock /path/to/.venv/bin/python /path/to/events_display.py --once >> /path/to/events_display.log 2>&1
```

I2C and SPI must be enabled, with `dtoverlay=spi0-0cs` in `/boot/firmware/config.txt`. Korean titles and emoji need the `fonts-noto-cjk` and `fonts-noto-color-emoji` packages; without them, those characters don't display properly.

## Architecture

Everything is in `events_display.py`.

1. **Fetch:** `fetch_calendar_events()` reads every `GCAL_ICAL_URL_*` feed, expands repeating events, keeps the next 90 days, hides events that have ended and removes duplicates across calendars.
2. **Poll:** `poll()` handles failures. Within `FAILURE_GRACE` (1 hour) of a failed fetch the screen stays as it is. After that, it shows the calendars that still work, or the last known events, with a red "Not updated since" warning in the header.
3. **Change detection:** `refresh_if_changed()` compares against `.events_state.json` and only redraws when the visible content, rotation, day or warning changes. Each redraw takes about 30 seconds and flashes the panel, so avoid needless ones.
4. **Render:** `render_events_image()` draws the layout below on a `P` mode image using the panel's palette indices.
5. **Display:** `update_display()` uses `inky.auto` to detect the panel, and applies `DISPLAY_ROTATION`.

## Layout

- **Header:** month and year on the left; today's date, or the warning, on the right.
- **Rows:** a colored bar for the calendar (blue, red, yellow), a bold title on the left, then the date and time in right-aligned columns. Dates within two days read Today or Tomorrow.
- **Vertical displays:** the date and time move to a second line.
- Titles are drawn by `_draw_title()`, which places emoji inline. A title containing non-Latin letters uses `FALLBACK_FONT` for the whole title.

## Configuration (`.env`)

```
DISPLAY_TZ=America/Los_Angeles    # timezone for all display times
DISPLAY_ROTATION=0                # degrees clockwise: 0/180 horizontal, 90/270 vertical
GCAL_ICAL_URL_1=https://example.com/private-.../calendar.ics
GCAL_NAME_1=Me                    # label for this calendar (not currently displayed)
```

Add `_2`, `_3` and so on for more calendars. iCal addresses are secrets: never commit `.env`, and keep them out of logs.
