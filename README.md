# events-display

Shows upcoming Google Calendar events on a Pimoroni Inky Impression 7.3" (Spectra 6, 800x480) attached to a Raspberry Pi. Without display hardware it saves `events_preview.png` instead.

## Setup (on the Pi)
```bash
pip install -r requirements.txt
cp .env.example .env   # add your secret iCal URLs, calendar names and DISPLAY_TZ
python3 events_display.py --once
```

### Add your calendars
For each calendar, open Google Calendar on a computer and go to **Settings → [your calendar] → Integrate calendar**. Copy **Secret address in iCal format**. Don't use the public address or the embed link.

Add it to `.env`, numbering each calendar and adding a label to show on the display:
```
DISPLAY_TZ=America/Los_Angeles
GCAL_ICAL_URL_1=https://calendar.google.com/calendar/ical/.../private-.../basic.ics
GCAL_NAME_1=Me
GCAL_ICAL_URL_2=https://calendar.google.com/calendar/ical/.../private-.../basic.ics
GCAL_NAME_2=Partner
```
Calendars 1, 2 and 3 are shown in blue, red and green; further calendars repeat those colors. Treat each secret address like a password: anyone who has it can read that calendar. Never commit `.env`.

Run it from cron; it only refreshes the display when events change:
```
* * * * * /path/to/python3 /path/to/events_display.py --once >> /path/to/events_display.log 2>&1
```
A systemd unit (`events-display.service`) is included as an alternative to cron; use one, not both.

See `CLAUDE.md` for architecture details. Built on [Pimoroni's inky library](https://github.com/pimoroni/inky) (MIT).
