#!/usr/bin/env python3
"""Streamlit interface for the FS Cabin Checker."""

import json
from datetime import date
from email.utils import parseaddr
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
import yaml

from cabin_checker import (
    collapse_to_ranges,
    compose_email,
    load_state,
    reset_state,
    resolve_state_path,
    run_availability_check,
    save_state,
    send_email,
    setup_logging,
)

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")
REGION_PRESET_PATH = Path(__file__).with_name("montana_fs_cabins.json")
EMAIL_SECRET_KEYS = ("smtp_server", "smtp_port", "sender_email", "sender_password")


def read_config_file(config_path: str) -> dict:
    path = Path(config_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path) as handle:
        return yaml.safe_load(handle) or {}


def write_config_file(config_path: str, config: dict) -> None:
    path = Path(config_path).expanduser()
    with open(path, "w") as handle:
        yaml.safe_dump(config, handle, sort_keys=False)


def load_region_presets() -> dict:
    if not REGION_PRESET_PATH.exists():
        return {}
    with open(REGION_PRESET_PATH) as handle:
        return json.load(handle)


def load_private_email_settings() -> dict:
    try:
        secrets = st.secrets
    except Exception:
        return {}

    try:
        secret_source = secrets["email"] if "email" in secrets else secrets
    except Exception:
        return {}

    private_settings = {}
    for key in EMAIL_SECRET_KEYS:
        try:
            value = secret_source[key] if key in secret_source else None
        except Exception:
            value = None
        if value not in (None, ""):
            private_settings[key] = value
    return private_settings


def apply_private_email_settings(base_config: dict) -> dict:
    send_config = dict(base_config)
    send_email_config = dict(base_config.get("email", {}))
    send_email_config.setdefault("smtp_server", "smtp.gmail.com")
    send_email_config.setdefault("smtp_port", 587)
    send_email_config.update(load_private_email_settings())
    send_config["email"] = send_email_config
    return send_config


def private_email_is_configured(base_config: dict) -> bool:
    email_config = apply_private_email_settings(base_config).get("email", {})
    return bool(email_config.get("sender_email") and email_config.get("sender_password"))


def campgrounds_to_text(campgrounds: list[dict]) -> str:
    return "\n".join(f'{item["id"]} | {item.get("name", "")}' for item in campgrounds)


def parse_campgrounds_text(raw_text: str) -> list[dict[str, str]]:
    campgrounds = []
    for line_number, raw_line in enumerate(raw_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if "|" in line:
            cg_id, name = line.split("|", 1)
        elif "," in line:
            cg_id, name = line.split(",", 1)
        else:
            raise ValueError(
                f"Campgrounds line {line_number} must look like '234336 | Battle Ridge Cabin'."
            )

        cg_id = cg_id.strip()
        name = name.strip()
        if not cg_id or not name:
            raise ValueError(f"Campgrounds line {line_number} is missing an ID or name.")

        campgrounds.append({"id": cg_id, "name": name})

    if not campgrounds:
        raise ValueError("Add at least one campground before saving.")

    return campgrounds


def parse_runtime_recipient(raw_value: str) -> str | None:
    value = raw_value.strip()
    if not value:
        return None
    _, parsed = parseaddr(value)
    if "@" not in parsed:
        raise ValueError("Enter a valid email address for the runtime recipient.")
    return parsed


def build_send_config(base_config: dict, runtime_recipient: str | None) -> dict:
    if not runtime_recipient:
        return base_config

    send_config = dict(base_config)
    send_email_config = dict(base_config.get("email", {}))
    send_email_config["recipients"] = [runtime_recipient]
    send_config["email"] = send_email_config
    return send_config


def get_runtime_recipient_state() -> tuple[str | None, str | None]:
    try:
        return parse_runtime_recipient(st.session_state.get("runtime_recipient", "")), None
    except ValueError as exc:
        return None, str(exc)


def send_preview_email(base_config: dict, preview: dict, runtime_recipient: str) -> bool:
    send_config = build_send_config(
        apply_private_email_settings(base_config),
        runtime_recipient,
    )
    return send_email(
        send_config,
        preview["subject"],
        preview["html_body"],
        preview["text_body"],
    )


def render_email_delivery_status() -> None:
    delivery = st.session_state.get("last_email_delivery")
    if not delivery:
        return

    status = delivery.get("status")
    message = delivery.get("message", "")
    if status == "sent":
        st.success(message)
    elif status == "error":
        st.error(message)
    elif status == "warning":
        st.warning(message)
    else:
        st.info(message)


def dedupe_campgrounds(campgrounds: list[dict]) -> list[dict]:
    deduped = []
    seen_ids = set()
    for campground in campgrounds:
        cg_id = str(campground["id"])
        if cg_id in seen_ids:
            continue
        seen_ids.add(cg_id)
        deduped.append({"id": cg_id, "name": campground.get("name", cg_id)})
    return deduped


def preset_region_names(preset_data: dict) -> list[str]:
    region_names = [region["name"] for region in preset_data.get("regions", [])]
    return ["All Montana Forest Service cabins"] + region_names


def campgrounds_for_regions(preset_data: dict, selected_regions: list[str]) -> list[dict]:
    if not selected_regions:
        return []
    if "All Montana Forest Service cabins" in selected_regions:
        return dedupe_campgrounds(preset_data.get("all_montana_campgrounds", []))

    selected = []
    region_map = {region["name"]: region["campgrounds"] for region in preset_data.get("regions", [])}
    for region_name in selected_regions:
        selected.extend(region_map.get(region_name, []))
    return dedupe_campgrounds(selected)


def render_quick_start(current_campground_count: int) -> None:
    with st.expander("How to use this app", expanded=True):
        st.markdown(
            "1. Go to **Settings** and choose one or more **Montana Forest Service region presets**.\n"
            "2. Click **Load selected region presets**.\n"
            "3. Click **Save settings** to store those cabins/lookouts in the app config.\n"
            "4. Enter your email in the sidebar if you want the alert sent to yourself.\n"
            "5. Go to **Run Checker** and click **Run availability check**.\n"
            "6. If new availability is found, the app automatically sends the alert when the search finishes.\n"
            "7. Open **Email Preview** to review or resend the alert."
        )
        st.caption(
            f"Current saved config is monitoring {current_campground_count} campground(s). "
            "If you want the full Montana list, load the preset and save it first."
        )
        st.caption(
            "The checker saves state after each run, so `new_only` notifications will only show dates that were not seen on the previous run."
        )
        st.caption(
            "The sender account stays private and should be configured through Streamlit secrets, not in the public app settings."
        )


def availability_counts(availability: dict[str, dict[str, list[str]]]) -> tuple[int, int, int]:
    non_empty = {cg_id: sites for cg_id, sites in availability.items() if sites}
    total_campgrounds = len(non_empty)
    total_sites = sum(len(sites) for sites in non_empty.values())
    total_dates = sum(len(dates) for sites in non_empty.values() for dates in sites.values())
    return total_campgrounds, total_sites, total_dates


def render_availability(
    availability: dict[str, dict[str, list[str]]],
    campground_names: dict[str, str],
    empty_message: str,
) -> None:
    non_empty = {cg_id: sites for cg_id, sites in availability.items() if sites}
    if not non_empty:
        st.info(empty_message)
        return

    total_campgrounds, total_sites, total_dates = availability_counts(non_empty)
    metric_1, metric_2, metric_3 = st.columns(3)
    metric_1.metric("Campgrounds", total_campgrounds)
    metric_2.metric("Sites", total_sites)
    metric_3.metric("Open nights", total_dates)

    sorted_rows = sorted(
        non_empty.items(),
        key=lambda item: campground_names.get(item[0], item[0]).lower(),
    )

    for cg_id, sites in sorted_rows:
        cg_name = campground_names.get(cg_id, cg_id)
        all_dates = sorted(day for site_dates in sites.values() for day in site_dates)
        next_opening = all_dates[0] if all_dates else "n/a"
        label = f"{cg_name} · {len(sites)} site(s) · next opening {next_opening}"

        with st.expander(label):
            st.markdown(
                f"[Open on Recreation.gov](https://www.recreation.gov/camping/campgrounds/{cg_id})"
            )
            for site_key, dates in sorted(sites.items()):
                site_name = site_key.split("||")[0]
                st.markdown(f"**{site_name}**")
                st.write(", ".join(collapse_to_ranges(dates)))


def load_config_into_session(config_path: str) -> dict:
    normalized_path = str(Path(config_path).expanduser())
    if st.session_state.get("loaded_config_path") != normalized_path:
        st.session_state["config_data"] = read_config_file(normalized_path)
        st.session_state["loaded_config_path"] = normalized_path
        st.session_state["run_result"] = None
        st.session_state["email_preview"] = None
        st.session_state["settings_campgrounds_text"] = campgrounds_to_text(
            st.session_state["config_data"]["campgrounds"]
        )
    return st.session_state["config_data"]


setup_logging()
st.set_page_config(page_title="FS Cabin Checker", page_icon="🏕️", layout="wide")
st.title("FS Cabin Checker")
st.caption(
    "Check Recreation.gov availability, review what changed, and send email alerts from a browser."
)

default_path = st.session_state.get("config_path", str(DEFAULT_CONFIG_PATH))
config_path = st.sidebar.text_input("Config file", value=default_path)
st.session_state["config_path"] = config_path
region_presets = load_region_presets()

try:
    config = load_config_into_session(config_path)
except Exception as exc:
    st.error(str(exc))
    st.stop()

normalized_config_path = str(Path(config_path).expanduser())
state_path = resolve_state_path(config, normalized_config_path)
saved_state = load_state(state_path)
last_checked = saved_state.get("last_checked") or "Never"
private_email_configured = private_email_is_configured(config)

st.sidebar.caption(f"State file: {state_path}")
st.sidebar.caption(f"Last checked: {last_checked}")

default_runtime_recipient = st.session_state.get("runtime_recipient", "")
runtime_recipient_input = st.sidebar.text_input(
    "Alert recipient for this session",
    value=default_runtime_recipient,
    placeholder="name@example.com",
    help="Enter the email address that should receive the alert from this app session.",
)
st.session_state["runtime_recipient"] = runtime_recipient_input

render_quick_start(len(config["campgrounds"]))

overview_1, overview_2, overview_3 = st.columns(3)
overview_1.metric("Campgrounds monitored", len(config["campgrounds"]))
overview_2.metric("Search start", config["search"]["start_date"])
overview_3.metric("Search end", config["search"]["end_date"])

run_tab, settings_tab, email_tab = st.tabs(["Run Checker", "Settings", "Email Preview"])

with run_tab:
    st.write(
        "Each live run updates the saved state file, so future alerts only include newly opened dates when notification mode is set to `new_only`."
    )

    action_1, action_2 = st.columns([2, 1])
    run_clicked = action_1.button("Run availability check", type="primary", use_container_width=True)
    reset_clicked = action_2.button("Reset saved state", use_container_width=True)

    if reset_clicked:
        reset_state(state_path)
        st.session_state["run_result"] = None
        st.session_state["email_preview"] = None
        st.session_state["last_email_delivery"] = None
        st.success("Saved state cleared. The next run will treat all current openings as new.")

    if run_clicked:
        st.session_state["last_email_delivery"] = None
        progress = st.progress(0, text="Preparing to check campgrounds...")

        def update_progress(index: int, total: int, _cg_id: str, cg_name: str) -> None:
            completed = (index - 1) / total if total else 1.0
            progress.progress(completed, text=f"Checking {cg_name} ({index}/{total})")

        try:
            with st.spinner("Querying Recreation.gov..."):
                run_result = run_availability_check(
                    config,
                    state_path,
                    progress_callback=update_progress,
                )
                save_state(state_path, run_result["state"])
            progress.progress(1.0, text="Availability check complete.")
            st.session_state["run_result"] = run_result

            if run_result["new_availability"]:
                subject, html_body, text_body = compose_email(
                    run_result["new_availability"],
                    run_result["campground_names"],
                    run_result["min_nights"],
                )
                st.session_state["email_preview"] = {
                    "subject": subject,
                    "html_body": html_body,
                    "text_body": text_body,
                }

                runtime_recipient, runtime_recipient_error = get_runtime_recipient_state()
                if not config["email"].get("enabled", True):
                    st.session_state["last_email_delivery"] = {
                        "status": "warning",
                        "message": "Availability check finished, but email sending is disabled in the saved config.",
                    }
                elif not private_email_is_configured(config):
                    st.session_state["last_email_delivery"] = {
                        "status": "warning",
                        "message": "Availability check finished, but no email was sent because private sender settings are not configured in Streamlit secrets.",
                    }
                elif runtime_recipient_error:
                    st.session_state["last_email_delivery"] = {
                        "status": "warning",
                        "message": f"Availability check finished, but no email was sent because the recipient address is invalid: {runtime_recipient_error}",
                    }
                elif not runtime_recipient:
                    st.session_state["last_email_delivery"] = {
                        "status": "warning",
                        "message": "Availability check finished, but no email was sent because no recipient address was entered in the sidebar.",
                    }
                elif send_preview_email(config, st.session_state["email_preview"], runtime_recipient):
                    st.session_state["last_email_delivery"] = {
                        "status": "sent",
                        "message": f"Availability check finished, state was saved, and the alert email was sent to {runtime_recipient}.",
                    }
                else:
                    st.session_state["last_email_delivery"] = {
                        "status": "error",
                        "message": "Availability check finished, but the alert email failed to send. Check the app logs for details.",
                    }
            else:
                st.session_state["email_preview"] = None
                st.session_state["last_email_delivery"] = {
                    "status": "info",
                    "message": "Availability check finished and state was saved. No new availability was found, so no alert email was sent.",
                }

            render_email_delivery_status()
        except Exception as exc:
            st.error(f"Checker failed: {exc}")

    run_result = st.session_state.get("run_result")
    if run_result:
        checked_at = run_result["state"].get("last_checked") or "Just now"
        st.caption(f"Most recent app run saved at {checked_at}.")
        if not run_clicked:
            render_email_delivery_status()
        new_tab, current_tab = st.tabs(["New Availability", "All Current Availability"])
        with new_tab:
            render_availability(
                run_result["new_availability"],
                run_result["campground_names"],
                "No new availability showed up on the latest run.",
            )
        with current_tab:
            render_availability(
                run_result["current_availability"],
                run_result["campground_names"],
                "No current availability was found in the configured date range.",
            )
    else:
        st.info("Run the checker to see live availability results here.")

with settings_tab:
    st.write("Edit the saved config file used by both the Streamlit app and the CLI script.")

    if region_presets:
        preset_options = preset_region_names(region_presets)
        preset_selection = st.multiselect(
            "Montana Forest Service region presets",
            options=preset_options,
            help="Choose one or more Montana national forests, then load their cabin and lookout IDs into the monitored campground list below.",
        )
        selected_preset_campgrounds = campgrounds_for_regions(region_presets, preset_selection)
        preset_actions = st.columns([2, 3])
        if preset_actions[0].button("Load selected region presets", use_container_width=True):
            if not selected_preset_campgrounds:
                st.warning("Choose at least one preset region before loading.")
            else:
                st.session_state["settings_campgrounds_text"] = campgrounds_to_text(
                    selected_preset_campgrounds
                )
                st.success(
                    f"Loaded {len(selected_preset_campgrounds)} cabins/lookouts from the selected Montana Forest Service regions. Click Save settings to persist them."
                )
        if selected_preset_campgrounds:
            preset_actions[1].caption(
                f"{len(selected_preset_campgrounds)} cabins/lookouts selected across {len(preset_selection)} region(s)."
            )

    with st.form("settings_form"):
        search_1, search_2, search_3 = st.columns(3)
        start_date = search_1.date_input(
            "Start date",
            value=date.fromisoformat(config["search"]["start_date"]),
        )
        end_date = search_2.date_input(
            "End date",
            value=date.fromisoformat(config["search"]["end_date"]),
        )
        min_nights = search_3.number_input(
            "Minimum nights",
            min_value=1,
            step=1,
            value=int(config["search"].get("min_nights", 1)),
        )

        notification_mode = st.selectbox(
            "Notification mode",
            options=["new_only", "always"],
            index=0 if config["notifications"].get("mode", "new_only") == "new_only" else 1,
        )

        email_enabled = st.checkbox(
            "Enable email sending",
            value=bool(config["email"].get("enabled", True)),
        )
        if private_email_configured:
            st.caption("Private sender email settings are configured for this deployment.")
        else:
            st.warning(
                "Private sender email settings are not configured yet. Add them in Streamlit secrets before email sending will work."
            )
        campgrounds_text = st.text_area(
            "Campgrounds (`ID | Name`, one per line)",
            key="settings_campgrounds_text",
            height=320,
        )

        save_clicked = st.form_submit_button("Save settings")

    if save_clicked:
        try:
            updated_config = dict(config)
            updated_config["campgrounds"] = parse_campgrounds_text(campgrounds_text)

            updated_search = dict(config.get("search", {}))
            updated_search["start_date"] = start_date.isoformat()
            updated_search["end_date"] = end_date.isoformat()
            updated_search["min_nights"] = int(min_nights)
            updated_config["search"] = updated_search

            updated_email = dict(config.get("email", {}))
            updated_email["enabled"] = email_enabled
            updated_email["smtp_server"] = "smtp.gmail.com"
            updated_email["smtp_port"] = 587
            updated_email["sender_email"] = ""
            updated_email["sender_password"] = ""
            updated_email["recipients"] = []
            updated_config["email"] = updated_email

            updated_notifications = dict(config.get("notifications", {}))
            updated_notifications["mode"] = notification_mode
            updated_config["notifications"] = updated_notifications

            if start_date > end_date:
                raise ValueError("Start date must be on or before the end date.")

            write_config_file(normalized_config_path, updated_config)
            st.session_state["config_data"] = updated_config
            st.session_state["run_result"] = None
            st.session_state["email_preview"] = None
            st.session_state["last_email_delivery"] = None
            st.session_state["settings_campgrounds_text"] = campgrounds_to_text(
                updated_config["campgrounds"]
            )
            st.success("Config saved.")
        except Exception as exc:
            st.error(str(exc))

with email_tab:
    preview = st.session_state.get("email_preview")
    if not preview:
        st.info("Run a check with new availability to preview the alert email.")
    else:
        render_email_delivery_status()
        st.markdown(f"**Subject:** {preview['subject']}")
        runtime_recipient, runtime_recipient_error = get_runtime_recipient_state()
        if runtime_recipient_error:
            st.error(runtime_recipient_error)

        effective_recipients = [runtime_recipient] if runtime_recipient else []
        if effective_recipients:
            st.caption("Email will be sent to: " + ", ".join(effective_recipients))
        else:
            st.caption("Enter your email in the sidebar to send the alert to yourself.")

        if not st.session_state["config_data"]["email"].get("enabled", True):
            st.warning("Email sending is disabled in the saved config.")
        elif not private_email_is_configured(st.session_state["config_data"]):
            st.warning(
                "Email sending is not configured for this deployment yet. Add private sender settings in Streamlit secrets."
            )
        elif not runtime_recipient:
            st.warning("Enter a recipient email in the sidebar before sending.")
        else:
            send_now = st.button("Send alert email now", type="primary")
            if send_now and runtime_recipient_error:
                st.error("Fix the runtime recipient email before sending.")
                send_now = False
            if send_now:
                if send_preview_email(st.session_state["config_data"], preview, runtime_recipient):
                    st.session_state["last_email_delivery"] = {
                        "status": "sent",
                        "message": f"Alert email sent to {runtime_recipient}.",
                    }
                    st.success("Alert email sent.")
                else:
                    st.session_state["last_email_delivery"] = {
                        "status": "error",
                        "message": "Alert email failed to send. Check the app logs for details.",
                    }
                    st.error("Email send failed. Check the app logs for details.")

        preview_html, preview_text = st.tabs(["HTML Preview", "Plain Text"])
        with preview_html:
            components.html(preview["html_body"], height=900, scrolling=True)
        with preview_text:
            st.text_area(
                "Plain-text email body",
                value=preview["text_body"],
                height=420,
            )
