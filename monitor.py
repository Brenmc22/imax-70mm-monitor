"""Config-driven movie ticket availability watcher (no purchasing or holds)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
import yaml
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

LOG = logging.getLogger("ticket-monitor")
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / ".monitor-state.json"


@dataclass(frozen=True)
class Target:
    name: str
    movie: str
    theater: str
    format_name: str
    seats_requested: int
    listing_url_template: str
    matching_keywords: tuple[str, ...]
    listing_container_selector: str
    showtime_link_selector: str
    available_seat_selector: str
    unavailable_phrases: tuple[str, ...]


@dataclass(frozen=True)
class Settings:
    days_ahead: int
    start_date: date | None
    end_date: date | None
    poll_interval_seconds: int
    delay_between_requests_seconds: int
    timeout_ms: int
    headless: bool
    stop_after_first_match: bool
    telegram_enabled: bool
    targets: tuple[Target, ...]


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"'{label}' must be a mapping in the YAML file")
    return value


def _positive_int(value: Any, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'{label}' must be an integer") from exc
    if result < 1:
        raise ValueError(f"'{label}' must be at least 1")
    return result


def load_settings(config_path: Path) -> Settings:
    if not config_path.is_file():
        raise FileNotFoundError(f"No configuration found: {config_path}. Copy config.example.yaml to config.yaml first.")
    data = _mapping(yaml.safe_load(config_path.read_text(encoding="utf-8")), "root")
    monitor = _mapping(data.get("monitor", {}), "monitor")
    dates = _mapping(data.get("dates", {}), "dates")
    notifications = _mapping(data.get("notifications", {}), "notifications")
    telegram = _mapping(notifications.get("telegram", {}), "notifications.telegram")

    start = date.fromisoformat(str(dates["start_date"])) if dates.get("start_date") else None
    end = date.fromisoformat(str(dates["end_date"])) if dates.get("end_date") else None
    if bool(start) != bool(end):
        raise ValueError("Set both dates.start_date and dates.end_date, or neither")
    if start and end and start > end:
        raise ValueError("dates.start_date must be on or before dates.end_date")

    raw_targets = data.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("Add at least one entry under targets")

    targets = []
    for index, raw in enumerate(raw_targets, start=1):
        target = _mapping(raw, f"targets[{index}]")
        required = ("name", "movie", "theater", "listing_url_template")
        missing = [key for key in required if not target.get(key)]
        if missing:
            raise ValueError(f"targets[{index}] missing required values: {', '.join(missing)}")
        template = str(target["listing_url_template"])
        if "{date}" not in template or not template.startswith("https://"):
            raise ValueError(f"targets[{index}].listing_url_template must be HTTPS and include {{date}}")
        if not urlsplit(template.replace("{date}", "2026-01-01")).hostname:
            raise ValueError(f"targets[{index}] needs a valid listing URL")

        selectors = _mapping(target.get("selectors", {}), f"targets[{index}].selectors")
        targets.append(Target(
            name=str(target["name"]),
            movie=str(target["movie"]),
            theater=str(target["theater"]),
            format_name=str(target.get("format", "Standard")),
            seats_requested=_positive_int(target.get("seats_requested", 2), "seats_requested"),
            listing_url_template=template,
            matching_keywords=tuple(str(s).casefold() for s in target.get("matching_keywords", [])),
            listing_container_selector=str(selectors.get("listing_containers", "div[class*='Movie'], div[class*='ShowtimeCard'], div[class*='Section']")),
            showtime_link_selector=str(selectors.get("showtime_links", "a[href*='seats'], a[href*='showtimes']")),
            available_seat_selector=str(selectors.get("available_seats", "button[aria-label*='Available'], div[class*='seat-available'], svg[class*='available']")),
            unavailable_phrases=tuple(str(s).casefold() for s in target.get("unavailable_phrases", ["sold out", "no seats available"])),
        ))

    poll = _positive_int(monitor.get("poll_interval_seconds", 900), "poll_interval_seconds")
    delay = _positive_int(monitor.get("delay_between_requests_seconds", 10), "delay_between_requests_seconds")
    if poll < 300:
        raise ValueError("poll_interval_seconds must be 300 seconds or longer to avoid rapid polling")
    if delay < 5:
        raise ValueError("delay_between_requests_seconds must be 5 seconds or longer")

    return Settings(
        days_ahead=_positive_int(dates.get("days_ahead", 7), "days_ahead"),
        start_date=start,
        end_date=end,
        poll_interval_seconds=poll,
        delay_between_requests_seconds=delay,
        timeout_ms=_positive_int(monitor.get("timeout_ms", 35000), "timeout_ms"),
        headless=bool(monitor.get("headless", True)),
        stop_after_first_match=bool(monitor.get("stop_after_first_match", True)),
        telegram_enabled=bool(telegram.get("enabled", False)),
        targets=tuple(targets),
    )


def target_dates(settings: Settings, today: date | None = None) -> list[str]:
    today = today or date.today()
    start = settings.start_date or today
    end = settings.end_date or (start + timedelta(days=settings.days_ahead - 1))
    return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]


def load_state() -> set[str]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("alerted_showtimes", []))
    except (FileNotFoundError, ValueError, TypeError):
        return set()


def save_state(alerted: set[str]) -> None:
    STATE_FILE.write_text(json.dumps({"alerted_showtimes": sorted(alerted)}, indent=2), encoding="utf-8")


def send_notification(message: str, enabled: bool, dry_run: bool) -> bool:
    LOG.warning("POTENTIAL MATCH — confirm seats manually:\n%s", message)
    if dry_run:
        LOG.info("Dry run: no notification sent or persisted")
        return False
    if not enabled:
        return True  # Console-only notification

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        LOG.error("Telegram enabled, but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
            timeout=15,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        LOG.error("Telegram alert failed: %s", exc)
        return False


class RateLimited(Exception):
    """The provider declined the request; do not retry until a later cycle."""


def open_page(page: Page, url: str, timeout_ms: int) -> None:
    response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    if response and response.status in (403, 429):
        raise RateLimited(f"Provider returned HTTP {response.status}; pausing checks")
    if response and response.status >= 400:
        raise RuntimeError(f"Page returned HTTP {response.status}")
    page.wait_for_timeout(1000)  # Allow client-rendered content a moment to appear


def find_showtime_links(page: Page, listing_url: str, target: Target) -> list[str]:
    """Heuristic HTML extraction; provider-specific selectors may need maintenance."""
    allowed_host = urlsplit(listing_url).hostname
    found: set[str] = set()
    for block in page.locator(target.listing_container_selector).all():
        try:
            content = block.inner_text(timeout=2000).casefold()
            if not all(keyword in content for keyword in target.matching_keywords):
                continue
            for link in block.locator(target.showtime_link_selector).all():
                href = link.get_attribute("href", timeout=2000)
                if not href:
                    continue
                candidate = urljoin(listing_url, href)
                parts = urlsplit(candidate)
                if parts.scheme == "https" and parts.hostname == allowed_host:
                    found.add(candidate)
        except PlaywrightError:
            LOG.debug("Unable to read one showtime block", exc_info=True)
    return sorted(found)


def count_seat_markers(page: Page, target: Target) -> int | None:
    """Count apparent available markers, NOT confirmed purchasable or adjacent seats."""
    try:
        body = page.locator("body").inner_text(timeout=5000).casefold()
        if any(phrase in body for phrase in target.unavailable_phrases):
            return 0
        return page.locator(target.available_seat_selector).count()
    except PlaywrightError:
        return None


def scan_once(page: Page, settings: Settings, alerted: set[str], dry_run: bool = False) -> bool:
    """Return True when a possible match is detected (even if alert delivery fails)."""
    found_match = False
    for target in settings.targets:
        for day in target_dates(settings):
            listing_url = target.listing_url_template.format(date=day)
            LOG.info("Checking %s — %s (%s)", target.name, day, listing_url)
            try:
                open_page(page, listing_url, settings.timeout_ms)
                links = find_showtime_links(page, listing_url, target)
                LOG.info("Found %d candidate showtime link(s)", len(links))
                for seat_url in links:
                    alert_key = f"{target.name}|{day}|{seat_url}"
                    if alert_key in alerted:
                        continue
                    time.sleep(settings.delay_between_requests_seconds)
                    open_page(page, seat_url, settings.timeout_ms)
                    count = count_seat_markers(page, target)
                    if count is None:
                        LOG.info("Unable to determine availability for %s", seat_url)
                        continue
                    if count < target.seats_requested:
                        LOG.info("Only %d apparent markers (requires %d): %s", count, target.seats_requested, seat_url)
                        continue
                    message = (
                        f"Possible ticket availability — VERIFY MANUALLY\n"
                        f"Movie: {target.movie} ({target.format_name})\n"
                        f"Theater: {target.theater}\n"
                        f"Date: {day}\n"
                        f"Requested seats: {target.seats_requested}\n"
                        f"Apparent available seat markers: {count}\n"
                        f"Adjacent seats and checkout availability are NOT verified.\n"
                        f"Open: {seat_url}"
                    )
                    found_match = True
                    if send_notification(message, settings.telegram_enabled, dry_run):
                        alerted.add(alert_key)
                        save_state(alerted)
                    if settings.stop_after_first_match:
                        return found_match
            except RateLimited as exc:
                LOG.warning("%s. Will not retry this target in this cycle.", exc)
                break
            except (PlaywrightError, RuntimeError, ValueError) as exc:
                LOG.warning("Scan error for %s on %s: %s", target.name, day, exc)
            time.sleep(settings.delay_between_requests_seconds)
    return found_match


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=BASE_DIR / "config.yaml")
    parser.add_argument("--once", action="store_true", help="Scan one cycle then exit")
    parser.add_argument("--dry-run", action="store_true", help="Never send Telegram alerts or save new matches")
    parser.add_argument("--check-config", action="store_true", help="Validate config without opening a browser")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = load_settings(args.config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.error(str(exc))
    LOG.info("Configuration OK: %d target(s), %d date(s)", len(settings.targets), len(target_dates(settings)))
    if args.check_config:
        return

    alerted = load_state()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=settings.headless)
            try:
                page = browser.new_page(locale="en-US")
                while True:
                    matched = scan_once(page, settings, alerted, dry_run=args.dry_run)
                    if args.once or (matched and settings.stop_after_first_match):
                        return
                    LOG.info("Next scan in %ds", settings.poll_interval_seconds)
                    time.sleep(settings.poll_interval_seconds)
            finally:
                browser.close()
    except KeyboardInterrupt:
        LOG.info("Monitor stopped by user")


if __name__ == "__main__":
    main()
