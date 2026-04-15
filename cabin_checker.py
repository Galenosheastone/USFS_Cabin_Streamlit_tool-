#!/usr/bin/env python3
"""
Recreation.gov Cabin Availability Checker
Checks campground availability and sends email alerts when open dates are found.
"""

import argparse
import calendar as cal_module
import json
import logging
import os
import smtplib
import sys
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(config_path: str) -> dict:
    path = Path(config_path)
    if not path.exists():
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


def resolve_state_path(config: dict, config_path: str) -> str:
    config_dir = Path(config_path).parent
    return str(config_dir / config["notifications"]["state_file"])


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

def load_state(state_path: str) -> dict:
    path = Path(state_path)
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {"last_checked": None, "availability": {}}


def save_state(state_path: str, state: dict) -> None:
    state["last_checked"] = datetime.now().isoformat(timespec="seconds")
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)
    logger.debug("State saved to %s", state_path)


def reset_state(state_path: str) -> None:
    path = Path(state_path)
    if path.exists():
        path.unlink()
        logger.info("State file cleared: %s", state_path)
    else:
        logger.info("No state file to clear at %s", state_path)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

AVAILABILITY_URL = (
    "https://www.recreation.gov/api/camps/availability/campground/{cg_id}/month"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

REQUEST_DELAY = 2.0  # seconds between API calls


def months_in_range(start: date, end: date) -> list[date]:
    """Return the first day of each month that overlaps [start, end]."""
    months = []
    current = start.replace(day=1)
    while current <= end:
        months.append(current)
        # Advance to next month
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return months


RETRY_DELAYS = [10, 20, 40]  # seconds to wait after each 429, before giving up


def fetch_month_availability(cg_id: str, month_start: date) -> dict | None:
    """Fetch availability JSON for one campground/month. Returns None on error."""
    url = AVAILABILITY_URL.format(cg_id=cg_id)
    params = {"start_date": month_start.strftime("%Y-%m-01T00:00:00.000Z")}
    for attempt, retry_wait in enumerate([0] + RETRY_DELAYS):
        if retry_wait:
            logger.warning(
                "Rate limited (429) for campground %s (%s) — waiting %ds (attempt %d/%d)",
                cg_id, month_start, retry_wait, attempt, len(RETRY_DELAYS),
            )
            time.sleep(retry_wait)
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
            if resp.status_code == 429:
                continue  # trigger next retry
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            logger.error("HTTP error for campground %s (%s): %s", cg_id, month_start, e)
            return None
        except requests.exceptions.RequestException as e:
            logger.error("Request error for campground %s (%s): %s", cg_id, month_start, e)
            return None
    logger.error("Giving up on campground %s (%s) after repeated 429s", cg_id, month_start)
    return None


# ---------------------------------------------------------------------------
# Availability parsing
# ---------------------------------------------------------------------------

def check_campground(cg_id: str, start: date, end: date) -> dict[str, list[str]]:
    """
    Returns a dict mapping site_id -> sorted list of available date strings
    (YYYY-MM-DD) within [start, end].
    """
    results: dict[str, list[str]] = {}
    months = months_in_range(start, end)

    for i, month_start in enumerate(months):
        if i > 0:
            time.sleep(REQUEST_DELAY)

        logger.debug("Fetching campground %s for %s", cg_id, month_start.strftime("%Y-%m"))
        data = fetch_month_availability(cg_id, month_start)
        if data is None:
            continue

        campsites = data.get("campsites", {})
        for site_id, site_info in campsites.items():
            availabilities = site_info.get("availabilities", {})
            site_name = site_info.get("site", site_id)
            key = f"{site_name}||{site_id}"

            for date_str, status in availabilities.items():
                if status != "Available":
                    continue
                # date_str looks like "2026-07-12T00:00:00Z"
                try:
                    avail_date = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").date()
                except ValueError:
                    continue
                if start <= avail_date <= end:
                    results.setdefault(key, [])
                    date_iso = avail_date.isoformat()
                    if date_iso not in results[key]:
                        results[key].append(date_iso)

    # Sort each site's dates
    for key in results:
        results[key].sort()

    return results


# ---------------------------------------------------------------------------
# Consecutive-night filtering & date range collapsing
# ---------------------------------------------------------------------------

def filter_min_nights(dates: list[str], min_nights: int) -> list[str]:
    """Keep only dates that are part of a run of at least min_nights consecutive days."""
    if min_nights <= 1:
        return dates
    date_objs = [date.fromisoformat(d) for d in dates]
    date_set = set(date_objs)
    kept = []
    for d in date_objs:
        # Check if there's a consecutive run of min_nights starting at d
        if all((d + timedelta(days=k)) in date_set for k in range(min_nights)):
            for k in range(min_nights):
                iso = (d + timedelta(days=k)).isoformat()
                if iso not in kept:
                    kept.append(iso)
    kept.sort()
    return kept


def collapse_to_ranges(dates: list[str]) -> list[str]:
    """Collapse consecutive dates into human-readable ranges like 'Jun 14–17'."""
    if not dates:
        return []
    date_objs = [date.fromisoformat(d) for d in dates]
    ranges = []
    run_start = date_objs[0]
    run_end = date_objs[0]
    for d in date_objs[1:]:
        if d == run_end + timedelta(days=1):
            run_end = d
        else:
            ranges.append(_format_range(run_start, run_end))
            run_start = d
            run_end = d
    ranges.append(_format_range(run_start, run_end))
    return ranges


def _format_range(start: date, end: date) -> str:
    if start == end:
        return start.strftime("%b %-d")
    if start.month == end.month:
        return f"{start.strftime('%b %-d')}–{end.strftime('%-d')}"
    return f"{start.strftime('%b %-d')}–{end.strftime('%b %-d')}"


# ---------------------------------------------------------------------------
# Diff against state
# ---------------------------------------------------------------------------

def compute_new_availability(
    current: dict[str, dict[str, list[str]]],
    previous: dict[str, dict[str, list[str]]],
    mode: str,
) -> dict[str, dict[str, list[str]]]:
    """
    Returns a filtered version of `current` containing only newly-available dates.
    If mode is 'always', returns current unchanged.
    """
    if mode == "always":
        return current

    new: dict[str, dict[str, list[str]]] = {}
    for cg_id, sites in current.items():
        prev_sites = previous.get(cg_id, {})
        for site_key, dates in sites.items():
            prev_dates = set(prev_sites.get(site_key, []))
            truly_new = [d for d in dates if d not in prev_dates]
            if truly_new:
                new.setdefault(cg_id, {})[site_key] = truly_new
    return new


# ---------------------------------------------------------------------------
# Email composition
# ---------------------------------------------------------------------------

def _build_month_calendar(year: int, month: int, available_dates: set) -> str:
    """Return an HTML mini-calendar table with available dates highlighted green."""
    cal_weeks = cal_module.monthcalendar(year, month)
    month_name = date(year, month, 1).strftime("%B %Y")

    html = (
        '<table style="border-collapse:collapse;margin:4px;display:inline-table;'
        'vertical-align:top;box-shadow:0 1px 3px rgba(0,0,0,0.12);">'
    )
    html += (
        f'<tr><th colspan="7" style="text-align:center;background:#2c5f2e;color:white;'
        f'padding:5px 8px;font-size:0.82em;white-space:nowrap;">{month_name}</th></tr>'
    )
    html += "<tr>" + "".join(
        f'<th style="width:28px;text-align:center;font-size:0.7em;color:#888;padding:2px;">{d}</th>'
        for d in ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"]
    ) + "</tr>"

    for week in cal_weeks:
        html += "<tr>"
        for day in week:
            if day == 0:
                html += '<td style="width:28px;height:24px;"></td>'
            else:
                d = date(year, month, day)
                if d in available_dates:
                    style = (
                        "width:28px;height:24px;text-align:center;background:#4CAF50;"
                        "color:white;font-size:0.8em;border-radius:3px;font-weight:bold;"
                    )
                else:
                    style = "width:28px;height:24px;text-align:center;font-size:0.8em;color:#ccc;"
                html += f'<td style="{style}">{day}</td>'
        html += "</tr>"

    html += "</table>"
    return html


def compose_email(
    new_avail: dict[str, dict[str, list[str]]],
    campground_names: dict[str, str],
    min_nights: int,
) -> tuple[str, str, str]:
    """Returns (subject, html_body, text_body)."""
    total_sites = sum(len(sites) for sites in new_avail.values())
    subject = f"\U0001f3d4\ufe0f Cabin Alert: {total_sites} site(s) available \u2014 Recreation.gov"

    text_parts = ["Recreation.gov Availability Alert", "=" * 40]

    # --- Gather summary data ---
    summary_rows = []
    for cg_id, sites in new_avail.items():
        cg_name = campground_names.get(cg_id, f"Campground {cg_id}")
        all_dates = sorted(d for dates in sites.values() for d in dates)
        next_date = date.fromisoformat(all_dates[0]).strftime("%b %-d") if all_dates else "—"
        summary_rows.append((cg_name, cg_id, len(sites), next_date))

    # --- HTML ---
    html_parts = [
        "<html><body style='font-family:Arial,sans-serif;max-width:700px;margin:auto;color:#333;'>",
        "<h2 style='color:#2c5f2e;border-bottom:2px solid #2c5f2e;padding-bottom:8px;'>",
        "\U0001f3d4\ufe0f Recreation.gov Availability Alert</h2>",
    ]

    # Summary table
    html_parts.append("<h3 style='color:#444;margin-bottom:6px;'>Summary</h3>")
    html_parts.append(
        "<table style='border-collapse:collapse;width:100%;margin-bottom:24px;'>"
        "<tr style='background:#2c5f2e;color:white;'>"
        "<th style='padding:8px 12px;text-align:left;'>Cabin</th>"
        "<th style='padding:8px 12px;text-align:center;'>Sites</th>"
        "<th style='padding:8px 12px;text-align:center;'>Next Opening</th>"
        "<th style='padding:8px 12px;text-align:center;'>Book</th>"
        "</tr>"
    )
    for i, (name, cg_id, num_sites, next_date) in enumerate(summary_rows):
        bg = "#f4f9f4" if i % 2 == 0 else "white"
        cg_url = f"https://www.recreation.gov/camping/campgrounds/{cg_id}"
        html_parts.append(
            f"<tr style='background:{bg};'>"
            f"<td style='padding:7px 12px;'>{name}</td>"
            f"<td style='padding:7px 12px;text-align:center;font-weight:bold;color:#2c5f2e;'>{num_sites}</td>"
            f"<td style='padding:7px 12px;text-align:center;'>{next_date}</td>"
            f"<td style='padding:7px 12px;text-align:center;'>"
            f"<a href='{cg_url}' style='color:#2c5f2e;font-weight:bold;text-decoration:none;'>Book &#8594;</a>"
            f"</td></tr>"
        )
    html_parts.append("</table>")

    # Per-campground detail
    for cg_id, sites in new_avail.items():
        cg_name = campground_names.get(cg_id, f"Campground {cg_id}")
        cg_url = f"https://www.recreation.gov/camping/campgrounds/{cg_id}"

        html_parts.append(
            f"<h3 style='color:#2c5f2e;margin-top:28px;margin-bottom:4px;'>"
            f"<a href='{cg_url}' style='color:#2c5f2e;text-decoration:none;'>{cg_name}</a></h3>"
        )

        text_parts.append(f"\n{cg_name}")
        text_parts.append(cg_url)

        # Collect all available dates across sites for the calendar
        all_dates_set: set[date] = set()
        for dates in sites.values():
            filtered = filter_min_nights(dates, min_nights) if min_nights > 1 else dates
            all_dates_set.update(date.fromisoformat(d) for d in filtered)

        # Calendar grids for each month that has availability
        if all_dates_set:
            months_with_avail = sorted({(d.year, d.month) for d in all_dates_set})
            html_parts.append("<div style='margin:8px 0 10px;'>")
            for year, month in months_with_avail:
                html_parts.append(_build_month_calendar(year, month, all_dates_set))
            html_parts.append("</div>")

        # Site detail list
        html_parts.append("<ul style='margin:6px 0 0;padding-left:20px;'>")
        for site_key, dates in sorted(sites.items()):
            site_name = site_key.split("||")[0]
            filtered = filter_min_nights(dates, min_nights) if min_nights > 1 else dates
            if not filtered:
                continue
            ranges = collapse_to_ranges(filtered)
            ranges_str = ", ".join(ranges)
            html_parts.append(f"<li><strong>{site_name}</strong>: {ranges_str}</li>")
            text_parts.append(f"  {site_name}: {ranges_str}")
        html_parts.append("</ul>")

    html_parts += [
        "<p style='color:#aaa;font-size:0.8em;margin-top:32px;border-top:1px solid #eee;padding-top:8px;'>",
        "Sent by cabin_checker.py &mdash; book fast, these go quickly!",
        "</p>",
        "</body></html>",
    ]

    return subject, "\n".join(html_parts), "\n".join(text_parts)


def resolve_email_config(config: dict) -> dict:
    email_config = dict(config.get("email", {}))

    if os.getenv("FS_CABIN_SMTP_SERVER"):
        email_config["smtp_server"] = os.environ["FS_CABIN_SMTP_SERVER"]
    if os.getenv("FS_CABIN_SMTP_PORT"):
        email_config["smtp_port"] = int(os.environ["FS_CABIN_SMTP_PORT"])
    if os.getenv("FS_CABIN_SENDER_EMAIL"):
        email_config["sender_email"] = os.environ["FS_CABIN_SENDER_EMAIL"]
    if os.getenv("FS_CABIN_SENDER_PASSWORD"):
        email_config["sender_password"] = os.environ["FS_CABIN_SENDER_PASSWORD"]
    if os.getenv("FS_CABIN_RECIPIENTS"):
        email_config["recipients"] = [
            value.strip()
            for value in os.environ["FS_CABIN_RECIPIENTS"].split(",")
            if value.strip()
        ]

    return email_config


def send_email(config: dict, subject: str, html_body: str, text_body: str) -> bool:
    """Send notification email. Returns True on success."""
    ec = resolve_email_config(config)

    if not ec.get("sender_email") or not ec.get("sender_password"):
        logger.error("Email sender credentials are not configured.")
        return False
    if not ec.get("recipients"):
        logger.error("Email recipients are not configured.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = ec["sender_email"]
    msg["To"] = ", ".join(ec["recipients"])

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(ec["smtp_server"], ec["smtp_port"]) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(ec["sender_email"], ec["sender_password"])
            smtp.sendmail(ec["sender_email"], ec["recipients"], msg.as_string())
        logger.info("Email sent to: %s", ", ".join(ec["recipients"]))
        return True
    except smtplib.SMTPException as e:
        logger.error("Failed to send email: %s", e)
        return False


def run_availability_check(
    config: dict,
    state_path: str,
    progress_callback: Callable[[int, int, str, str], None] | None = None,
) -> dict:
    """
    Run the availability check and return the structured results needed by both the
    CLI workflow and the Streamlit app.
    """
    start = date.fromisoformat(config["search"]["start_date"])
    end = date.fromisoformat(config["search"]["end_date"])
    min_nights = config["search"].get("min_nights", 1)
    mode = config["notifications"]["mode"]
    campgrounds = config["campgrounds"]

    logger.info(
        "Checking %d campground(s) for availability %s to %s",
        len(campgrounds),
        start,
        end,
    )

    state = load_state(state_path)
    previous_avail = state.get("availability", {})
    current_avail: dict[str, dict[str, list[str]]] = {}
    campground_names: dict[str, str] = {}

    total_campgrounds = len(campgrounds)
    for index, cg in enumerate(campgrounds, start=1):
        cg_id = str(cg["id"])
        cg_name = cg.get("name", cg_id)
        campground_names[cg_id] = cg_name

        if progress_callback:
            progress_callback(index, total_campgrounds, cg_id, cg_name)

        logger.info("Checking: %s (ID %s)", cg_name, cg_id)
        sites = check_campground(cg_id, start, end)

        if min_nights > 1:
            filtered_sites = {}
            for site_key, dates in sites.items():
                filtered_dates = filter_min_nights(dates, min_nights)
                if filtered_dates:
                    filtered_sites[site_key] = filtered_dates
            sites = filtered_sites

        if sites:
            total_dates = sum(len(v) for v in sites.values())
            logger.info(
                "  Found %d available date(s) across %d site(s)", total_dates, len(sites)
            )
        else:
            logger.info("  No availability found.")

        current_avail[cg_id] = sites

        if index < total_campgrounds:
            time.sleep(REQUEST_DELAY)

    new_avail = compute_new_availability(current_avail, previous_avail, mode)
    new_avail = {cg_id: sites for cg_id, sites in new_avail.items() if sites}

    state["availability"] = current_avail

    return {
        "start": start,
        "end": end,
        "min_nights": min_nights,
        "mode": mode,
        "campground_names": campground_names,
        "current_availability": current_avail,
        "new_availability": new_avail,
        "previous_availability": previous_avail,
        "state": state,
        "state_path": state_path,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Recreation.gov cabin availability and send email alerts."
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "config.yaml"),
        help="Path to config.yaml (default: config.yaml next to this script)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check availability and print results, but do not send email",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear state.json so all current availability triggers a fresh alert",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)

    config = load_config(args.config)
    state_path = resolve_state_path(config, args.config)

    if args.reset:
        reset_state(state_path)
        logger.info("State reset. Next run will alert on all found availability.")
        return

    run_result = run_availability_check(config, state_path)
    min_nights = run_result["min_nights"]
    campground_names = run_result["campground_names"]
    new_avail = run_result["new_availability"]
    current_avail = run_result["current_availability"]
    state = run_result["state"]

    if not new_avail:
        logger.info("No new availability to report.")
    else:
        total_new = sum(len(s) for s in new_avail.values())
        logger.info("New availability: %d site(s) across %d campground(s)", total_new, len(new_avail))

        subject, html_body, text_body = compose_email(new_avail, campground_names, min_nights)

        if args.dry_run:
            print("\n--- DRY RUN: Email would be sent ---")
            print(f"Subject: {subject}\n")
            print(text_body)
            print("------------------------------------\n")
        elif config["email"].get("enabled", True):
            ok = send_email(config, subject, html_body, text_body)
            if not ok:
                logger.error("Email failed — printing results to stdout instead:")
                print(text_body)
        else:
            logger.info("Email disabled in config. Results:")
            print(text_body)

    # Save updated state (even on dry-run, so we track what we've seen)
    save_state(state_path, state)
    logger.info("Done.")


if __name__ == "__main__":
    main()
