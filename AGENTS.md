# AGENTS.md

Guidance for working with code in this repository.

## Purpose

Calendar display for a Raspberry Pi Zero 2 W with a Pimoroni Inky Impression 7.3" (Spectra 6, 800x480, E673). It fetches private iCal feeds, draws an agenda and pushes it to the panel. Without display hardware it saves `events_preview.png` instead.

## Running

On the Pi, from the repo root, using its virtual environment:

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python events_display.py
```

Each run fetches the calendars once and exits. Cron runs it every minute under `flock`, so two runs can't draw at the same time:

```
* * * * * flock -n /tmp/events_display.lock /path/to/.venv/bin/python /path/to/events_display.py >> /path/to/events_display.log 2>&1
```

I2C and SPI must be enabled, with `dtoverlay=spi0-0cs` in `/boot/firmware/config.txt`. Korean titles and emoji need the `fonts-noto-cjk` and `fonts-noto-color-emoji` packages; without them, those characters don't display properly.

## How it works

Everything is in `events_display.py`, in this order:

1. **Fetch:** `fetch_events()` reads every `GCAL_ICAL_URL_*` feed, expands repeating events, keeps the next 90 days, hides events that have ended and removes duplicates across calendars. Each event is a dict: `name`, `start` (ISO string in local time), `all_day`, `cal_index`.
2. **Draw:** `render()` draws the agenda on an 800x480 `P` mode image with a six-color palette (`PALETTE`). The Inky library maps those pure colors onto the panel's own. The picture depends only on the events, today's date and the warning, never on the time of day.
3. **Refresh:** `refresh()` hashes the image and only pushes it to the panel (`show()`) when the hash differs from the one saved in `.events_state.json`. Each redraw takes about 30 seconds and flashes the panel, so avoid needless ones.
4. **Failures:** `main()` handles them. Within `FAILURE_GRACE` (1 hour) of a failed fetch the screen stays as it is. After that, it shows the calendars that still work, or the last known events from the state file, with a red "Not updated since" warning in the header.

## Layout

- **Header:** month and year on the left; today's date, or the warning, on the right.
- **Rows:** a colored bar for the calendar (blue, red, yellow), a bold title on the left, then the date and time in right-aligned columns. Dates within two days read Today or Tomorrow.
- Titles are drawn by `draw_title()`, which places emoji inline. A title containing non-Latin letters uses `KOREAN_FONT` for the whole title.

## Configuration (`.env`)

```
DISPLAY_TZ=America/Los_Angeles    # timezone for all display times
GCAL_ICAL_URL_1=https://example.com/private-.../calendar.ics
```

Add `_2`, `_3` and so on for more calendars. iCal addresses are secrets: never commit `.env`, and keep them out of logs.
