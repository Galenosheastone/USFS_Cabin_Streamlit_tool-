# USFS_Cabin_Streamlit_tool
A tool to look for cabin availability in Montana 

# FS Cabin Checker

A lightweight Python tool that checks Recreation.gov for cabin and campground
availability and sends you an email the moment open dates appear. You can run it as
either a Streamlit app or a cron-friendly CLI script.

Currently configured to monitor ~25 Forest Service cabins and fire lookouts across
three Montana ranger districts: **Bozeman**, **Hebgen Lake**, and **Yellowstone/Livingston/Shields**.

Email alerts include a summary table, color-coded calendar grids showing available dates,
and direct booking links — no external dependencies beyond `requests` and `pyyaml`.

---

## Quick Start

**1. Install dependencies**

```bash
cd recgov_cabin_checker
pip install -r requirements.txt
```

**2. Edit `config.yaml`**

- Add your campground IDs (see [Finding Campground IDs](#finding-campground-ids) below).
- Set your date range under `search`.
- Fill in your Gmail address and App Password under `email`
  (see [setup_gmail.md](setup_gmail.md) for instructions).

**3. Launch the Streamlit app**

```bash
streamlit run streamlit_app.py
```

The app lets you edit settings, run checks, preview the alert email, and reset saved
state from the browser.

The Settings tab also includes Montana Forest Service region presets. You can load
all cabin/lookout IDs for one or more Montana national forests directly into the
monitored campground list before saving.

**4. Or use the CLI**

Do a dry run to verify everything works:

```bash
python cabin_checker.py --dry-run
```

This checks availability and prints what would be emailed — no email is actually sent.

Run for real:

```bash
python cabin_checker.py
```

---

## Cron Setup

To check every 15 minutes and log output:

```cron
*/15 * * * * cd /path/to/recgov_cabin_checker && /usr/bin/python3 cabin_checker.py >> checker.log 2>&1
```

To edit your crontab:

```bash
crontab -e
```

Tips:
- Use an absolute path to your Python interpreter (`which python3` to find it).
- The log file will grow over time — consider adding `logrotate` or a weekly `> checker.log` cron entry.
- On macOS, you may need to grant Terminal/cron Full Disk Access in System Preferences > Privacy & Security.

---

## Finding Campground IDs

The campground ID is the number at the end of the Recreation.gov URL.

Example:
```
https://www.recreation.gov/camping/campgrounds/234607
                                               ^^^^^^
                                               This is the ID
```

Search for your cabin or campground on Recreation.gov, open the campground page, and
copy the ID from the URL. Paste it into `config.yaml` under `campgrounds`.

---

## Adding or Removing Campgrounds

Edit the `campgrounds` list in `config.yaml`:

```yaml
campgrounds:
  - id: "234607"
    name: "Beaver Creek Cabin (Gallatin NF)"
  - id: "99999"
    name: "My New Cabin"
```

The `name` field is just a human-friendly label for the email — it doesn't affect
which campground is checked.

---

## CLI Reference

```
python cabin_checker.py              # Normal run: check all, email if new dates found
python cabin_checker.py --dry-run    # Print results, skip sending email
python cabin_checker.py --reset      # Clear state so all found dates trigger a fresh alert
python cabin_checker.py --verbose    # Enable DEBUG logging
python cabin_checker.py --config /path/to/config.yaml  # Use alternate config file
```

---

## How It Works

1. For each campground, the script queries the Recreation.gov availability API for each
   month in your configured date range.
2. It collects all dates with status `"Available"` within your window.
3. It compares against `state.json` (auto-created) to find **newly** available dates
   (dates not seen on the previous run).
4. If new dates are found, it sends an HTML email with a summary table, color-coded
   calendar grids per cabin, and direct booking links.
5. It updates `state.json` with the current snapshot.

If the API rate-limits the script (429 errors), it automatically retries with backoff
(10s → 20s → 40s) before giving up on that month.

---

## Troubleshooting

**429 Too Many Requests errors**

The script automatically retries with backoff (10s, 20s, 40s). If you see persistent
429s across many cabins, the API is heavily throttled — wait a few minutes and re-run.
After any run with 429 errors, use `--reset` and re-run to ensure complete data.

**403 errors from the API**

Recreation.gov requires a browser-like User-Agent. The script sends one automatically.
If you still get 403s, the site may be temporarily blocking requests — wait and retry.

**`SMTPAuthenticationError` when sending email**

- Make sure you're using a **Gmail App Password**, not your real Gmail password.
- Confirm 2-Step Verification is enabled on your Google account.
- See [setup_gmail.md](setup_gmail.md) for step-by-step instructions.

**"Email disabled in config" message**

Set `email.enabled: true` in `config.yaml`.

**Getting alerted for the same dates repeatedly**

The script only alerts on **new** availability by default (`mode: new_only`). If you
want to be alerted every run regardless, set `mode: always` in `config.yaml`. You can
also run `--reset` to clear the state and get a one-time re-alert on everything currently
available.

**"No new availability to report" when you know dates are open**

Your `state.json` from a previous run already recorded those dates. Run:
```bash
python cabin_checker.py --reset
python cabin_checker.py
```

**No results found for a cabin I know is available**

- Verify the campground ID from the Recreation.gov URL.
- Check that your `start_date` and `end_date` cover the dates you're interested in.
- Run with `--verbose` to see detailed API request/response logging.
- If a previous run had 429 errors, some months may be missing — do a `--reset` and re-run.

---

## Cabins Monitored

**Bozeman Ranger District**
Battle Ridge Cabin, Fox Creek Cabin, Garnet Mountain Fire Lookout, Little Bear Cabin,
Maxey Cabin, Mystic Lake Cabin, Spanish Creek Cabin, Window Rock Cabin, Windy Pass Cabin,
Yellow Mule Cabin

**Hebgen Lake Ranger District**
Basin Station Cabin, Beaver Creek Cabin, Cabin Creek Cabin, Wapiti Cabin

**Yellowstone / Livingston / Shields Valley**
Big Creek Cabin, Deer Creek Cabin, Fourmile Cabin, Ibex Cabin, Mill Creek Cabin,
Porcupine Cabin, Trail Creek Cabin, West Bridger Cabin, West Boulder Cabin
