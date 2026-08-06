#!/usr/bin/env python3
"""
MSB chart scraper — downloads the inline chart images from a custom-route
briefing on briefing.met.ie (default "Maritime Patrol").

Target charts: SigWx, Low Level Wind & Temp, Low Level Sig Weather.

Kept separate from msb_map2.py (the METAR/pseudo-station map) on purpose:
this one only deals with the chart images.

NOTE: Authenticated, Met Éireann-copyright content. Use your own credentials
for your own situational awareness only; do not redistribute.

Usage (Spyder): press Run. Prompts for your MSB username/password.
Charts are saved into ./charts/.
"""

import io
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from getpass import getpass
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from PIL import Image

BASE = "https://briefing.met.ie/"
ROUTE_NAME = "Maritime Patrol"
OUT_DIR = os.environ.get("MSB_CHARTS_OUT", "charts")

# --- Met Office surface-pressure charts (public, no login) ------------------
# The page's raw HTML contains the chart image URLs (no JS needed). Each URL is
#   .../v1/surface-pressure/colour/{issueDatetime}/{name}_{stepHours}.gif
# and the chart's VALID time = issue datetime + step hours. We keep the freshest
# issue per valid time, then pick the most recent valid time that isn't in the
# future (the current analysis) as the "latest" chart to show. Logic mirrors the
# user's existing briefing tool.
PRESSURE_PAGE = "https://weather.metoffice.gov.uk/maps-and-charts/surface-pressure"
PRESSURE_RE = re.compile(
    r"https://data\.consumer-digital\.api\.metoffice\.gov\.uk/"
    r"v1/surface-pressure/colour/"
    r"(\d{4}-\d{2}-\d{2}T\d{4})/"          # issue datetime
    r"[A-Za-z0-9]+_(\d{2,3})\.gif",        # chart name + step hours
    re.I,
)
PRESSURE_HEADERS = {"User-Agent": "Mozilla/5.0 (briefing tool; personal use)"}

# Charts are served rotated 90°. Rotating by +90 (counter-clockwise) usually
# rights them. If they come out upside-down or still sideways, try 270 or -90.
ROTATE_DEGREES = 90

# WMO product codes -> friendly chart-type names, keyed off the image alt text.
# pgde15 = upper-level SigWx (100-450);  pwxc99 = low-level sig weather (FL050-300);
# qgxd70 = low-level wind & temp (below FL100).
CHART_TYPES = {
    "pgde15": "SigWx",
    "pwxc99": "LowLevel_SigWeather",
    "qgxd70": "LowLevel_Wind_Temp",
}


# ---------------------------------------------------------------------------
# Login + locate the briefing page
# ---------------------------------------------------------------------------
def get_credentials():
    """MSB credentials from env vars MSB_USER / MSB_PASS if set, else prompt.
    In a headless run (MSB_HEADLESS=1 or no terminal), fail cleanly rather than
    block on a prompt. Nothing secret is stored in this script."""
    user = os.environ.get("MSB_USER")
    pwd = os.environ.get("MSB_PASS")
    headless = os.environ.get("MSB_HEADLESS") or not sys.stdin.isatty()
    if not user or not pwd:
        if headless:
            raise SystemExit("MSB_USER / MSB_PASS not set and no interactive "
                             "terminal. Set them as environment variables.")
        if not user:
            user = input("MSB username: ")
        if not pwd:
            pwd = getpass("MSB password: ")
    return user, pwd


def login(session):
    user, pwd = get_credentials()
    session.post(urljoin(BASE, "login-result.php"), data={
        "action": "login",
        "username": user,
        "password": pwd,
    })


def find_route_url(session, route_name):
    soup = BeautifulSoup(session.get(urljoin(BASE, "main.php")).content, "html.parser")
    for a in soup.find_all("a", href=True):
        if "custombriefing.php" in a["href"] and route_name.lower() in a.get_text(strip=True).lower():
            return urljoin(BASE, a["href"])
    return None


def get_soup(session, url):
    return BeautifulSoup(session.get(url).content, "html.parser")


# ---------------------------------------------------------------------------
# Chart discovery + download
# ---------------------------------------------------------------------------
def collect_charts(soup):
    """
    Return a list of chart dicts for the full-size (thumb=0) images, one per
    forecast time, tagged with type and timestamp.

    Each chart id appears twice on the page: a thumbnail (thumb=1) whose alt
    carries the forecast time, and a full-size (thumb=0) image we actually want.
    We read the time from the thumbnails, then attach it to the full-size URL.
    """
    times_by_id = {}      # id -> forecast time string (from thumbnails)
    full_by_id = {}       # id -> (code, src) for full-size images

    for img in soup.find_all("img"):
        src = img.get("src") or ""
        alt = img.get("alt") or ""
        if "view.php" not in src:
            continue
        q = parse_qs(urlparse(src).query)
        cid = q.get("id", [""])[0]
        code = next((c for c in CHART_TYPES if c in alt.lower()), None)
        if not code:
            continue
        is_full = q.get("thumb", ["1"])[0] == "0"
        if is_full:
            full_by_id[cid] = (code, src)
        else:
            # thumbnail alt looks like "pgde15 - 100 to 450 - 2026-08-03 06:00:00"
            m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", alt)
            if m:
                times_by_id[cid] = m.group(1)

    charts = []
    for cid, (code, src) in full_by_id.items():
        t = times_by_id.get(cid, "unknown-time")
        charts.append({
            "type": CHART_TYPES[code],
            "code": code,
            "id": cid,
            "time": t,
            "url": urljoin(BASE, src),
        })
    # sort by type then time for tidy output
    charts.sort(key=lambda c: (c["type"], c["time"]))
    return charts


def _safe(s):
    return re.sub(r"[^0-9A-Za-z_-]", "-", s)


# Stable "latest" filename per chart type, so a webpage can always point at a
# fixed URL regardless of the forecast timestamp. Written in addition to the
# timestamped copy.
LATEST_NAMES = {
    "SigWx": "sigwx_latest",
    "LowLevel_SigWeather": "lowlevel_sigwx_latest",
    "LowLevel_Wind_Temp": "lowlevel_windtemp_latest",
}


def download_charts(session, charts, out_dir=OUT_DIR):
    os.makedirs(out_dir, exist_ok=True)
    saved = []
    # most recent forecast time per chart type -> that's the "latest" copy
    latest_time = {}
    for c in charts:
        if c["time"] > latest_time.get(c["type"], ""):
            latest_time[c["type"]] = c["time"]
    for c in charts:
        resp = session.get(c["url"])
        # figure out extension from content-type (charts are usually PNG/GIF)
        ctype = resp.headers.get("Content-Type", "")
        ext = {"image/png": ".png", "image/gif": ".gif",
               "image/jpeg": ".jpg"}.get(ctype.split(";")[0], ".png")
        fname = os.path.join(out_dir, f"{c['type']}_{_safe(c['time'])}{ext}")

        # The site serves these with rotate=1 (turned 90° for its own layout),
        # so the raw bytes are sideways. Rotate back with Pillow.
        # If they come out the wrong way, flip ROTATE_DEGREES to -90 (or 270).
        img = Image.open(io.BytesIO(resp.content))
        img = img.rotate(ROTATE_DEGREES, expand=True)
        img.save(fname)

        c["file"] = fname
        saved.append(fname)
        print(f"  saved {c['type']:22s} {c['time']}  -> {fname}", file=sys.stderr)

        # Also write a stable "latest" copy for the newest time of each type,
        # so the app can link a fixed filename that never changes.
        if c["type"] in LATEST_NAMES and c["time"] == latest_time.get(c["type"]):
            latest = os.path.join(out_dir, LATEST_NAMES[c["type"]] + ext)
            img.save(latest)
            print(f"    (latest) -> {latest}", file=sys.stderr)
    return saved


def discover_pressure_charts():
    """Parse the Met Office surface-pressure page for colour chart URLs.
    Returns {valid_datetime: url}, keeping the freshest issue (smallest step)
    for each valid time. No login required. Mirrors the user's briefing tool."""
    r = requests.get(PRESSURE_PAGE, headers=PRESSURE_HEADERS, timeout=30)
    r.raise_for_status()
    charts = {}   # valid_dt -> (step, url)
    for m in PRESSURE_RE.finditer(r.text):
        issue = datetime.strptime(m.group(1), "%Y-%m-%dT%H%M").replace(tzinfo=timezone.utc)
        step = int(m.group(2))
        valid = issue + timedelta(hours=step)
        if valid not in charts or step < charts[valid][0]:
            charts[valid] = (step, m.group(0))
    return {valid: url for valid, (step, url) in charts.items()}


def download_pressure_charts(out_dir=OUT_DIR):
    """Download ALL available Met Office surface-pressure colour charts (analysis
    + forecast steps), save each locally, and write pressure_charts.js setting
    window.__PRESSURE_CHARTS (list of {validity,label,src}) and __PRESSURE_ISSUED
    for the app's pressure viewer. Best-effort; on failure writes nothing extra.

    Follows the user's briefing-tool logic: discover charts keyed by valid time,
    freshest issue per valid time. Here we keep the most recent issue cycle and
    present its steps (T+0 analysis onward) so the viewer pages through them."""
    try:
        r = requests.get(PRESSURE_PAGE, headers=PRESSURE_HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"  surface pressure: page fetch failed ({e}); skipped.", file=sys.stderr)
        return None

    # collect (issue, step, valid, url) for every colour chart on the page
    entries = []
    for m in PRESSURE_RE.finditer(r.text):
        issue = datetime.strptime(m.group(1), "%Y-%m-%dT%H%M").replace(tzinfo=timezone.utc)
        step = int(m.group(2))
        entries.append((issue, step, issue + timedelta(hours=step), m.group(0)))
    if not entries:
        print("  surface pressure: no charts found; skipped.", file=sys.stderr)
        return None

    # Use the most recent ISSUE cycle (the freshest run), then its steps in order.
    latest_issue = max(e[0] for e in entries)
    cycle = sorted((e for e in entries if e[0] == latest_issue), key=lambda e: e[1])

    os.makedirs(out_dir, exist_ok=True)
    charts_meta = []
    for issue, step, valid, url in cycle:
        try:
            img = requests.get(url, headers=PRESSURE_HEADERS, timeout=30)
            img.raise_for_status()
        except Exception as e:
            print(f"    step T+{step}: fetch failed ({e}); skipped.", file=sys.stderr)
            continue
        fname = f"pressure_{step:03d}.gif"
        with open(os.path.join(out_dir, fname), "wb") as fh:
            fh.write(img.content)
        validity = "T+0 (Analysis)" if step == 0 else f"T+{step}"
        label = f"{valid:%d/%m} {valid:%H}Z"
        charts_meta.append({"validity": validity, "label": label,
                            "src": f"./charts/{fname}"})
        print(f"    {validity:14s} {label}  -> {fname}", file=sys.stderr)

    if not charts_meta:
        print("  surface pressure: nothing saved; skipped.", file=sys.stderr)
        return None

    # human-readable issued string, e.g. "0000Z Wed 05 Aug 2026"
    issued_str = f"{latest_issue:%H%M}Z {latest_issue:%a %d %b %Y}"
    data_path = os.path.join(out_dir, "..", "pressure_charts.js")
    data_path = os.path.normpath(data_path)
    # write next to the web root (same folder pattern as airport_data.js);
    # allow override via env for flexibility
    data_path = os.environ.get("MSB_PRESSURE_JS", data_path)
    with open(data_path, "w", encoding="utf-8") as fh:
        fh.write("window.__PRESSURE_CHARTS = " +
                 __import__("json").dumps(charts_meta, ensure_ascii=False) + ";\n")
        fh.write("window.__PRESSURE_ISSUED = " +
                 __import__("json").dumps(issued_str) + ";\n")
    print(f"  surface pressure: {len(charts_meta)} charts, issued {issued_str} "
          f"-> {data_path}", file=sys.stderr)
    return charts_meta


def main():
    session = requests.Session()
    login(session)

    url = find_route_url(session, ROUTE_NAME)
    if not url:
        raise SystemExit(f'Could not find a briefing matching "{ROUTE_NAME}".')

    soup = get_soup(session, url)
    charts = collect_charts(soup)
    print(f"Found {len(charts)} full-size charts:", file=sys.stderr)
    for c in charts:
        print(f"  {c['type']:22s} {c['time']}", file=sys.stderr)

    print("\nDownloading MSB charts...", file=sys.stderr)
    saved = download_charts(session, charts)
    print(f"Saved {len(saved)} MSB charts to ./{OUT_DIR}/", file=sys.stderr)

    print("\nFetching Met Office surface pressure charts...", file=sys.stderr)
    download_pressure_charts()


if __name__ == "__main__":
    main()