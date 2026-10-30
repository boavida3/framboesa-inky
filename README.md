# events-display

Shows upcoming Google Calendar events on a Pimoroni Inky Impression 7.3" (Spectra 6, 800x480) attached to a Raspberry Pi. Without display hardware it saves `events_preview.png` instead.

## Setup (on the Pi)
```bash
pip install -r requirements.txt
cp .env.example .env   # add your secret iCal URLs, calendar names and DISPLAY_TZ
python3 events_display.py --once
```

Run it from cron; it only refreshes the display when events change:
```
* * * * * /path/to/python3 /path/to/events_display.py --once >> /path/to/events_display.log 2>&1
```
A systemd unit (`events-display.service`) is included as an alternative to cron; use one, not both.

See `CLAUDE.md` for architecture details. Built on [Pimoroni's inky library](https://github.com/pimoroni/inky) (MIT).
