# Odyssey — Movie Ticket Availability Monitor

A **configurable, read-only movie-showtime watcher** built with Python and Playwright. Originally adapted from a single-film AMC IMAX 70mm monitoring script, this portfolio project separates site-specific settings from the monitoring logic, supports configurable dates and venues, and can send Telegram alerts for **potential** seat availability.

> **Important:** This project does not reserve, hold, or buy tickets. Seat markers are a heuristic, **not** proof that a specified number of adjacent or purchasable seats exists. Always open the listing and verify manually.

## Features

- Movie, theater, format, seat quantity, and listing URL configured in YAML
- Rolling date windows or explicit start/end dates
- Multiple configurable targets and CSS selectors
- Minimum-delay safeguards and graceful handling of HTTP 403/429 responses
- Console reporting and optional Telegram notifications
- Duplicate alert prevention using local, ignored state
- One-off scans, config validation, and dry-run option
- No browser stealth, account login, or automated checkout

## Project structure

```text
├── monitor.py            # Watcher and alerting logic
├── config.example.yaml   # Reusable settings template
├── requirements.txt      # Python dependencies
├── .gitignore             # Excludes secrets and runtime files
├── tests/
│   └── test_monitor.py   # Config/date validation tests
└── README.md
```

## Get started

Requires Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
cp config.example.yaml config.yaml
python monitor.py --check-config
python monitor.py --once --dry-run
```

After reviewing your local `config.yaml`, run one normal scan:

```bash
python monitor.py --once
```

Run continuously (at the configured interval) with:

```bash
python monitor.py
```

To send Telegram alerts, set `notifications.telegram.enabled: true` in `config.yaml` and provide **environment variables** rather than putting credentials in GitHub:

```bash
export TELEGRAM_BOT_TOKEN='your-token'
export TELEGRAM_CHAT_ID='your-chat-id'
python monitor.py
```

## How to customize

Edit `config.yaml` to update:

| Setting | Purpose |
| --- | --- |
| `dates.days_ahead` | Number of days in a rolling window starting today |
| `dates.start_date` / `end_date` | Optional fixed window; supply both |
| `targets[].movie` / `theater` / `format` | Labels for alerts |
| `targets[].listing_url_template` | HTTPS listing URL with a `{date}` placeholder |
| `targets[].matching_keywords` | Words that must appear in a listing block |
| `targets[].seats_requested` | Minimum number of apparent available seat markers |
| `targets[].selectors` | Site-specific CSS selectors for listings, links, and seats |
| `monitor.poll_interval_seconds` | Waiting period between scan cycles; minimum 300 seconds |

The example target is **The Odyssey — IMAX 70mm at AMC Lincoln Square 13**. Its URL and CSS selectors were inherited from an earlier script and **have not been verified against the current site**. Website structures change; adjust selectors based on the site's current HTML if necessary. No availability is guaranteed.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Technical approach and limits

The watcher loads a listing page, looks for matching format keywords in configurable content blocks, extracts same-domain HTTPS showtime links, then inspects the resulting page for apparent available-seat elements. It never clicks purchase controls. A count meeting the configured threshold produces a **possible availability** notification and a link for manual verification.

**Known limitations:** DOM selectors are fragile, markup may be rendered dynamically, showtime buttons without links may not be discoverable, seat-map markers may not correspond to distinct bookable seats, and seat adjacency is not checked. HTTP rate-limit or access-denied responses cause that target to pause until a future scan. Use only where permitted by the ticket provider's terms, and do not attempt to circumvent its protections.

## Portfolio context

This project demonstrates Python scripting, YAML-based configuration, Playwright browser testing, failure handling, notification integration, and iterative refactoring of a one-off workflow into a reusable template. It is an **availability-notification prototype**, not a production-verified ticket booking system.
