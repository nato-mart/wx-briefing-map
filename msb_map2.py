#!/usr/bin/env python3
"""
Met Self Briefing System (MSB) -> interactive map of a custom-route briefing.

Logs into briefing.met.ie, opens a named custom-route briefing (default
"Maritime Patrol"), and plots TWO layers you can toggle:
  * Airport METARs   (real ICAO airfields, "METAR ...")
  * Pseudo-stations  (synop-derived, "PsMETAR ...")
Clicking any marker shows the raw report plus a plain-language decode.

NOTE: These are *pseudo* / model-derived reports from an authenticated,
Met Éireann-copyright system. Use your own credentials, for your own
situational awareness only -- not as a primary flight-briefing source.

Usage (Spyder): press Run. It prompts for your MSB username/password.
"""

import json
import os
import re
import sys
from getpass import getpass
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import folium

BASE = "https://briefing.met.ie/"
ROUTE_NAME = "Maritime Patrol"

# ---------------------------------------------------------------------------
# Airport ICAO -> (name, lat, lon)
# ---------------------------------------------------------------------------
ICAO_COORDS = {
    "EIME": (53.3022, -6.4514), "EIAC": (53.4239, -7.9403),
    "EIWF": (52.1872, -7.0870), "EISG": (54.2802, -8.5992),
    "EINN": (52.7019, -8.9248), "EIKY": (52.1809, -9.5238),
    "EIDL": (55.0442, -8.3410), "EIKN": (53.9103, -8.8185),
    "EIDW": (53.4213, -6.2701), "EICK": (51.8413, -8.4911),
    "EGPF": (55.8719, -4.4331), "EGGP": (53.3336, -2.8497),
    "EGGD": (51.3827, -2.7191), "LFRB": (48.4479, -4.4185),
    "EGAA": (54.6575, -6.2158), "EGAE": (55.0428, -7.1611),
    "EGAC": (54.6181, -5.8725),
}

# ---------------------------------------------------------------------------
# Pseudo-station coordinates. Matched FIRST by 4-letter MSB code, then by the
# parenthesised name (case-insensitive, punctuation-insensitive). The climate
# stations come from the original met.ie DMS parse; codes added as identified.
# Any entry that can't be matched is printed to the console so you can add it.
# ---------------------------------------------------------------------------
PS_BY_CODE = {
    "ANRY": (53.2892, -8.7856),   # Athenry
    "BALD": (53.3022, -6.4514),   # Casement Aerodrome (Baldonnel)
    "CORK": (51.8413, -8.4911),   # Cork Airport (= EICK)
    "DUBL": (53.4213, -6.2701),   # Dublin Airport (= EIDW)
    "KNOC": (53.9103, -8.8185),   # Connaught / Knock (= EIKN)
    "SHAN": (52.7019, -8.9248),   # Shannon Airport (= EINN)
    "VALE": (51.9397, -10.2444),  # Valentia Observatory
}
PS_BY_NAME = {
    "athenry": (53.2892, -8.7856),
    "ballyhaise": (54.0514, -7.3097),
    "belmullet": (54.2275, -10.0069),
    "carlow oakpark": (52.8611, -6.9153),
    "oak park": (52.8611, -6.9153),
    "claremorris": (53.7108, -8.9925),
    "dunsany": (53.5158, -6.6600),
    "fermoy moorepark": (52.1639, -8.2639),
    "moore park": (52.1639, -8.2639),
    "finner": (54.4939, -8.2431),
    "gurteen": (53.0531, -8.0086),
    "johnstown castle": (52.2978, -6.4967),
    "mace head": (53.3258, -9.9008),
    "malin head": (55.3722, -7.3389),
    "markree": (54.1750, -8.4556),
    "mount dillon": (53.7269, -7.9808),
    "mullingar": (53.5372, -7.3622),
    "newport": (53.9222, -9.5722),
    "phoenix park": (53.3639, -6.3333),
    "roches point": (51.7931, -8.2444),
    "sherkin island": (51.4764, -9.4278),
    "valentia": (51.9397, -10.2444),
    "casement aerodrome": (53.3022, -6.4514),
}

METAR_RE = re.compile(
    r"\(([^\n]*?)\)\s*\n\s*METAR\s+([A-Z]{4})\s+([^\n]*?=)", re.MULTILINE
)
PSMETAR_RE = re.compile(
    r"\(([^\n)]*?)\)\s*\n\s*PsMETAR\s+([A-Z]{2,5})\s+([^\n]*?=)", re.MULTILINE
)


def _norm(name):
    return re.sub(r"[^a-z ]", "", name.lower()).strip()


def ps_coords(code, name):
    if code in PS_BY_CODE:
        return PS_BY_CODE[code]
    return PS_BY_NAME.get(_norm(name))


# ---------------------------------------------------------------------------
# Login + fetch
# ---------------------------------------------------------------------------
def get_credentials():
    """
    Get MSB credentials from environment variables MSB_USER / MSB_PASS if set,
    otherwise prompt (interactive use only).

    Set them once so you don't retype each run:
      Windows (one-off, in Command Prompt):
        setx MSB_USER "yourname"
        setx MSB_PASS "yourpassword"
      (open a NEW terminal / restart Spyder afterwards so they're picked up)
      macOS/Linux (add to ~/.bashrc or ~/.zshrc):
        export MSB_USER="yourname"
        export MSB_PASS="yourpassword"

    In an automated / headless run (e.g. GitHub Actions, cron, any CI), set
    the env var MSB_HEADLESS=1. Then, if the credentials are missing, this
    raises immediately instead of blocking forever on an input() prompt that
    no one can answer. Credentials themselves are never stored in this script.
    """
    user = os.environ.get("MSB_USER")
    pwd = os.environ.get("MSB_PASS")
    headless = os.environ.get("MSB_HEADLESS") or not sys.stdin.isatty()
    if not user or not pwd:
        if headless:
            raise SystemExit(
                "MSB_USER / MSB_PASS not set and no interactive terminal "
                "available. Set them as environment variables / CI secrets.")
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


def scrape_page_text(session, url):
    return BeautifulSoup(session.get(url).content, "html.parser").get_text("\n")


def parse_airports(text):
    return [{"name": n.strip(), "code": c, "report": f"METAR {c} {b.strip()}"}
            for n, c, b in METAR_RE.findall(text)]


def parse_pseudos(text):
    return [{"name": n.strip(), "code": c, "report": f"PsMETAR {c} {b.strip()}"}
            for n, c, b in PSMETAR_RE.findall(text)]


# ---------------------------------------------------------------------------
# METAR / PsMETAR decoder (handles Q#### and bare MSL pressure)
# ---------------------------------------------------------------------------
def decode(report):
    parts = []
    # issued time: DDHHMMZ
    m = re.search(r"\b(\d{2})(\d{2})(\d{2})Z\b", report)
    if m:
        parts.append(f"Issued: {m.group(2)}:{m.group(3)}Z")
    if re.search(r"\bAUTO\b", report):
        parts.append("Automated station")
    # wind
    m = re.search(r"\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT", report)
    if m:
        d, spd, gust = m.groups()
        wind = "variable" if d == "VRB" else f"from {int(d)}°"
        parts.append(f"Wind {wind} at {int(spd)} kt" + (f", gusting {int(gust)}" if gust else ""))
    # variable wind direction band
    m = re.search(r"\b(\d{3})V(\d{3})\b", report)
    if m:
        parts.append(f"Wind varying {int(m.group(1))}°–{int(m.group(2))}°")
    # visibility / CAVOK
    if "CAVOK" in report:
        parts.append("CAVOK (vis ≥10 km, no cloud below 5000 ft / sig wx)")
    else:
        mw = re.search(r"(\d{3}|VRB)\d{2,3}(?:G\d{2,3})?KT", report)
        after = report[mw.end():] if mw else report
        mv = re.search(r"(?<!\d)(\d{4})(?:NDV)?(?!\d)", after)
        if mv:
            v = int(mv.group(1))
            parts.append("Visibility ≥10 km" if v >= 9999 else f"Visibility {v:,} m")
    # cloud layers with base height (ft AGL)
    cloud_words = {"FEW": "few", "SCT": "scattered", "BKN": "broken", "OVC": "overcast"}
    clouds = []
    for cm in re.finditer(r"\b(FEW|SCT|BKN|OVC)(\d{3})(?:///)?\b", report):
        clouds.append(f"{cloud_words[cm.group(1)]} at {int(cm.group(2))*100:,} ft")
    if "NSC" in report:
        clouds.append("no significant cloud")
    if clouds:
        parts.append("Cloud: " + "; ".join(clouds))
    # temp / dewpoint
    m = re.search(r"\b(M?\d{2})/(M?\d{2})\b", report)
    if m:
        t = lambda x: -int(x[1:]) if x.startswith("M") else int(x)
        parts.append(f"Temp {t(m.group(1))}°C, Dewpoint {t(m.group(2))}°C")
    # pressure: Q#### (hPa) or bare #### before MSL
    m = re.search(r"\bQ(\d{4})\b", report) or re.search(r"\b(\d{4})\s+MSL", report)
    if m:
        parts.append(f"QNH {int(m.group(1))} hPa")
    # weather phenomena (match longest codes first, no overlap)
    wx_map = [("+SHRA","heavy rain showers"),("-SHRA","light rain showers"),("SHRA","rain showers"),
              ("+RA","heavy rain"),("-RA","light rain"),("RA","rain"),
              ("+SN","heavy snow"),("-SN","light snow"),("SN","snow"),
              ("DZ","drizzle"),("TS","thunderstorm"),("SH","showers"),
              ("BR","mist"),("FG","fog"),("HZ","haze"),("FU","smoke")]
    wx, used = [], []
    for code, label in wx_map:
        for mm in re.finditer(rf"(?<![A-Z+-]){re.escape(code)}(?![A-Z])", report):
            if any(s <= mm.start() < e for s, e in used):
                continue
            wx.append(label); used.append(mm.span()); break
    if wx:
        parts.append("Weather: " + ", ".join(dict.fromkeys(wx)))
    if "NOSIG" in report:
        parts.append("No significant change expected (2h)")
    return parts


# ---------------------------------------------------------------------------
# Structured decode -> the shape the Design app's AIRPORT_DB expects.
# Fills what the METAR provides; TAF/NOTAM/winds are best-effort and fall
# back to honest placeholders when not available from the scrape.
# ---------------------------------------------------------------------------
def _flight_category(report):
    """VFR / MVFR / IFR / LIFR from ceiling (ft) and visibility (m).
    Uses standard ICAO-ish thresholds adapted to metres."""
    # visibility in metres
    vis_m = 10000
    if "CAVOK" in report:
        vis_m = 10000
    else:
        mw = re.search(r"(\d{3}|VRB)\d{2,3}(?:G\d{2,3})?KT", report)
        after = report[mw.end():] if mw else report
        mv = re.search(r"(?<!\d)(\d{4})(?:NDV)?(?!\d)", after)
        if mv:
            vis_m = int(mv.group(1))
            if vis_m >= 9999:
                vis_m = 10000
    # ceiling = lowest BKN/OVC base in ft
    ceiling = 99999
    for cm in re.finditer(r"\b(BKN|OVC)(\d{3})(?:///)?\b", report):
        ceiling = min(ceiling, int(cm.group(2)) * 100)
    # thresholds (feet / metres)
    if ceiling < 500 or vis_m < 1600:
        return "LIFR"
    if ceiling < 1000 or vis_m < 5000:
        return "IFR"
    if ceiling < 3000 or vis_m < 8000:
        return "MVFR"
    return "VFR"


def _structured_metar(report):
    """Return (metarDecoded list of {label,value}, issued 'HH:MMZ' or '')."""
    out = []
    issued = ""
    m = re.search(r"\b(\d{2})(\d{2})(\d{2})Z\b", report)
    if m:
        issued = f"{m.group(2)}:{m.group(3)}Z"
    # wind
    m = re.search(r"\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT", report)
    if m:
        d, spd, gust = m.groups()
        wd = "VRB" if d == "VRB" else f"{int(d)}°"
        val = f"{wd} @ {int(spd)}kt" + (f" gust {int(gust)}" if gust else "")
        # variable wind direction band, e.g. 240V310 -> "(var 240°–310°)"
        mv = re.search(r"\b(\d{3})V(\d{3})\b", report)
        if mv:
            val += f" (var {int(mv.group(1))}°\u2013{int(mv.group(2))}°)"
        out.append({"label": "Wind", "value": val})
    # visibility
    if "CAVOK" in report:
        out.append({"label": "Visibility", "value": "CAVOK"})
    else:
        # A standalone 4-digit group (optionally NDV) is the visibility in
        # metres. Search after the wind so we don't catch the time; the group
        # may be separated from KT by a variable-wind band (e.g. 240V310).
        after = report[m.end():] if m else report
        mvis = re.search(r"(?<!\d)(\d{4})(NDV)?(?!\d)", after)
        if mvis:
            v = int(mvis.group(1))
            out.append({"label": "Visibility",
                        "value": "10 km+" if v >= 9999 else f"{v:,} m"})
    # sky
    cloud_words = {"FEW": "Few", "SCT": "Scattered", "BKN": "Broken", "OVC": "Overcast"}
    sky = []
    for cm in re.finditer(r"\b(FEW|SCT|BKN|OVC)(\d{3})(?:///)?\b", report):
        sky.append(f"{cloud_words[cm.group(1)]} {int(cm.group(2))*100:,} ft")
    if "NSC" in report or "CAVOK" in report and not sky:
        sky.append("No significant cloud")
    if sky:
        out.append({"label": "Sky", "value": " / ".join(sky)})
    # temp/dewpoint
    m = re.search(r"\b(M?\d{2})/(M?\d{2})\b", report)
    if m:
        t = lambda x: -int(x[1:]) if x.startswith("M") else int(x)
        out.append({"label": "Temp / Dewpoint",
                    "value": f"{t(m.group(1))}° / {t(m.group(2))}°C"})
    # QNH
    m = re.search(r"\bQ(\d{4})\b", report) or re.search(r"\b(\d{4})\s+MSL", report)
    if m:
        out.append({"label": "QNH", "value": f"{int(m.group(1))} hPa"})

    # Remarks: anything meaningful AFTER the QNH group — wind shear (WS RWYnn),
    # trend groups (NOSIG / TEMPO / BECMG ...), RMK sections, recent weather
    # (RExx), etc. We take the tail of the report from just after the QNH and
    # tidy up common codes into readable text, leaving the rest as-is so nothing
    # is silently dropped.
    remarks = ""
    if m:
        tail = report[m.end():].strip().strip("=").strip()
        if tail:
            # human-friendly expansions for the most common codes
            expansions = [
                (r"\bWS\s+ALL\s+RWY\b", "wind shear all runways"),
                (r"\bWS\s+RWY\s*(\d{2}[LCR]?)\b", r"wind shear runway \1"),
                (r"\bNOSIG\b", "no significant change expected"),
                (r"\bTEMPO\b", "temporary:"),
                (r"\bBECMG\b", "becoming:"),
                (r"\bRMK\b", "remark:"),
            ]
            # Insert a separator before each distinct trend/shear group so
            # multiple remarks read clearly, then expand codes to words.
            tail = re.sub(r"\s+(WS|NOSIG|TEMPO|BECMG|RMK)\b", " \u00b7 \\1", tail)
            tail = tail.lstrip("\u00b7 ").strip()
            pretty = tail
            for pat, rep in expansions:
                pretty = re.sub(pat, rep, pretty)
            pretty = re.sub(r"\s+", " ", pretty).strip()
            remarks = pretty
    if remarks:
        out.append({"label": "Remarks", "value": remarks})

    # The Design app REQUIRES these four labels to exist (it does
    # metarDecoded.find(label===X).value). Guarantee each is present; fill any
    # the METAR didn't provide with a dash so the app never crashes.
    have = {d["label"] for d in out}
    for required in ("Wind", "Visibility", "Sky", "QNH"):
        if required not in have:
            out.append({"label": required, "value": "—"})
    # keep a stable order: required fields first in canonical order, then extras
    order = {"Wind": 0, "Visibility": 1, "Sky": 2, "Temp / Dewpoint": 3, "QNH": 4,
             "Remarks": 5}
    out.sort(key=lambda d: order.get(d["label"], 99))
    return out, issued


# module-level containers the parsers can populate if the page has the data.
# Keyed by ICAO/station code. Left empty -> app shows honest placeholders.
TAF_BY_CODE = {}
NOTAMS_BY_CODE = {}
WINDS_BY_CODE = {}


def build_airport_db(airports, pseudos):
    """Produce a dict in the shape the Design app's AIRPORT_DB expects."""
    db = {}
    for item in airports + pseudos:
        code = item["code"]
        report = item["report"]
        # strip the "METAR "/"PsMETAR " prefix for the raw display
        raw = re.sub(r"^(METAR|PsMETAR)\s+", "", report).rstrip("=").strip()
        decoded, issued = _structured_metar(report)
        entry = {
            "code": code,
            "name": item["name"],
            "flightCat": _flight_category(report),
            "metarRaw": raw,
            "metarTime": (f"Observed {issued}" if issued else "Observed —"),
            "metarDecoded": decoded,
            "tafRaw": TAF_BY_CODE.get(code, "TAF not available in this briefing"),
            "notams": NOTAMS_BY_CODE.get(code, []),
            "windsAloft": WINDS_BY_CODE.get(code, []),
        }
        db[code] = entry
    return db


def write_airport_data_js(db, path):
    """Write window.__AIRPORT_DB = {...} for the page to consume."""
    payload = json.dumps(db, ensure_ascii=False, indent=2)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("window.__AIRPORT_DB = " + payload + ";\n")
    print(f"Wrote airport data ({len(db)} stations) to {path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Map with two toggleable layers
# ---------------------------------------------------------------------------
def popup_html(name, code, report):
    decoded = "".join(f"<li>{d}</li>" for d in decode(report))
    return (f"<b>{name}</b> ({code})<hr style='margin:4px 0'>"
            f"<code style='font-size:11px'>{report}</code>"
            f"<ul style='margin:6px 0 0;padding-left:18px;font-size:12px'>{decoded}</ul>")


def add_weather_layers(m):
    """
    Add animated weather overlays that share one player:
      * RainViewer radar (preloaded tiles, ~10-min steps, past 2h + nowcast)
      * EUMETSAT IR cloud (WMS, 15-min steps, past 2h)

    Two top-right toggles: "Radar (animated)" and "EUMETSAT cloud (IR)".
    The bottom play/pause + slider + timestamp panel appears whenever EITHER
    is on. Whichever is active drives the timeline; if both are on, radar
    drives and the cloud layer follows (nearest 15-min slot).

    Free public APIs, no keys. Attribution shown for both. Degrades gracefully
    if RainViewer is unreachable (cloud-only still works).
    """
    import datetime

    # ---- RainViewer radar frames (may be empty if offline) -------------------
    rv_host, rv_frames = "", []
    try:
        data = requests.get(
            "https://api.rainviewer.com/public/weather-maps.json", timeout=20
        ).json()
        rv_host = data.get("host", "")
        radar = data.get("radar", {})
        past = radar.get("past", []) or []
        nowcast = radar.get("nowcast", []) or []
        rv_frames = ([{"time": f["time"], "path": f["path"], "kind": "past"} for f in past] +
                     [{"time": f["time"], "path": f["path"], "kind": "forecast"} for f in nowcast])
    except Exception as e:
        print(f"RainViewer radar unavailable ({e}); cloud-only will still work.",
              file=sys.stderr)

    # ---- EUMETSAT cloud timeline: 15-min steps over the last 2h (UTC) --------
    now = datetime.datetime.now(datetime.timezone.utc)
    slot0 = now - datetime.timedelta(
        minutes=now.minute % 15, seconds=now.second, microseconds=now.microsecond)
    slot0 = slot0 - datetime.timedelta(minutes=15)   # ensure published
    cloud_times = []
    for k in range(8, -1, -1):   # 9 steps back = 2h at 15-min cadence
        t = slot0 - datetime.timedelta(minutes=15 * k)
        cloud_times.append(t.strftime("%Y-%m-%dT%H:%M:%S.000Z"))

    map_var = m.get_name()
    rv_frames_json = json.dumps(rv_frames)
    rv_host_json = json.dumps(rv_host)
    cloud_times_json = json.dumps(cloud_times)

    # ---- unified control box (top-right): weather animation toggles sit at
    #      the top; Folium's LayerControl is restyled just below to look like
    #      part of the same panel (see build_map for the CSS that joins them).
    toggles = """
    <div id="wx-controls" style="position:fixed;top:12px;right:10px;z-index:9999;
         font-family:sans-serif;font-size:13px;background:#1a1a1a;color:#eee;
         border:1px solid #444;border-radius:6px 6px 0 0;padding:8px 10px 6px;
         box-shadow:0 2px 6px rgba(0,0,0,0.5);min-width:170px">
      <div id="wx-hdr" style="font-weight:600;font-size:11px;letter-spacing:.5px;
           color:#aaa;margin-bottom:6px;text-transform:uppercase;cursor:pointer;
           display:flex;align-items:center;justify-content:space-between;gap:8px"
           title="Show/hide layer controls">
        <span>Map layers</span>
        <span id="wx-caret" style="transition:transform .15s;font-size:10px">&#9650;</span>
      </div>
      <div id="wx-body">
        <label style="cursor:pointer;display:flex;align-items:center;gap:6px;padding:2px 0">
          <input type="checkbox" id="wx-radar-chk"> Rainfall
        </label>
        <label style="cursor:pointer;display:flex;align-items:center;gap:6px;padding:2px 0">
          <input type="checkbox" id="wx-cloud-chk"> EUMETSAT cloud (IR)
        </label>
      </div>
    </div>
    <script>
    window.addEventListener('load', function() {
      function wire() {
        var hdr = document.getElementById('wx-hdr');
        var body = document.getElementById('wx-body');
        var caret = document.getElementById('wx-caret');
        var ctrl = document.getElementById('wx-controls');
        if (!hdr || !body) { setTimeout(wire, 200); return; }
        var collapsed = false;
        hdr.onclick = function() {
          collapsed = !collapsed;
          body.style.display = collapsed ? 'none' : '';
          caret.style.transform = collapsed ? 'rotate(180deg)' : '';
          // also collapse the Leaflet layer-control box joined below
          var lc = document.querySelector('.leaflet-control-layers');
          if (lc) lc.style.display = collapsed ? 'none' : '';
          // round the bottom of our box when the layer control is hidden
          if (ctrl) ctrl.style.borderRadius = collapsed ? '6px' : '6px 6px 0 0';
        };
      }
      wire();
    });
    </script>
    """

    # ---- player panel (bottom, hidden until a layer is on) -------------------
    panel = """
    <div id="wx-panel" style="display:none;position:fixed;bottom:20px;left:50%;
         transform:translateX(-50%);z-index:9999;background:#1a1a1a;color:#eee;
         border:1px solid #444;border-radius:8px;padding:8px 12px;
         font-family:sans-serif;font-size:13px;align-items:center;gap:8px;
         box-shadow:0 2px 8px rgba(0,0,0,0.5)">
      <button id="wx-prev" style="cursor:pointer;background:#333;color:#eee;
              border:1px solid #555;border-radius:4px;padding:2px 8px">⏮</button>
      <button id="wx-play" style="cursor:pointer;background:#333;color:#eee;
              border:1px solid #555;border-radius:4px;padding:2px 10px">▶</button>
      <button id="wx-next" style="cursor:pointer;background:#333;color:#eee;
              border:1px solid #555;border-radius:4px;padding:2px 8px">⏭</button>
      <input id="wx-slider" type="range" min="0" max="1" value="0"
             style="width:180px;cursor:pointer">
      <span id="wx-time" style="min-width:120px;text-align:center;
            font-variant-numeric:tabular-nums">--:--</span>
      <select id="wx-speed" style="background:#333;color:#eee;border:1px solid #555;
              border-radius:4px;padding:2px">
        <option value="1200">Slow</option>
        <option value="700" selected>Normal</option>
        <option value="350">Fast</option>
      </select>
    </div>
    """

    js = f"""
    <script>
    window.addEventListener('load', function() {{
        var rvHost = {rv_host_json};
        var rvFrames = {rv_frames_json};       // [{{time,path,kind}}]
        var cloudTimes = {cloud_times_json};   // ["ISO", ...] oldest->newest
        var map = null;
        var rvLayers = [];       // radar tile layers by index
        var cloudLayers = [];    // preloaded WMS layers, one per cloud time
        var radarOn = false, cloudOn = false;
        var current = 0;         // index into the ACTIVE timeline
        var playing = false, timer = null, speedMs = 700;

        function getMap() {{
            try {{ if (typeof {map_var} !== 'undefined' && {map_var}) return {map_var}; }} catch(e) {{}}
            var el = document.querySelector('.folium-map');
            if (el && window.L && L.Map) {{
                for (var k in window) {{
                    try {{ var v = window[k];
                        if (v instanceof L.Map && v.getContainer && v.getContainer()===el) return v;
                    }} catch(e) {{}}
                }}
            }}
            return null;
        }}
        function p2(n) {{ return String(n).padStart(2,'0'); }}
        function fmtUnix(ts) {{
            var d = new Date(ts*1000);
            return p2(d.getHours()) + ":" + p2(d.getMinutes());
        }}
        function fmtISO(iso) {{
            var d = new Date(iso);
            return p2(d.getUTCHours()) + ":" + p2(d.getUTCMinutes()) + "Z";
        }}

        // --- radar tiles ---
        function rvLayerFor(i) {{
            if (!map || !rvFrames.length) return null;
            if (!rvLayers[i]) {{
                rvLayers[i] = L.tileLayer(
                    rvHost + rvFrames[i].path + "/256/{{z}}/{{x}}/{{y}}/4/1_1.png",
                    {{ opacity:0.0, zIndex:200, maxNativeZoom:7, maxZoom:19,
                      
                       attribution:'Weather data by <a href="https://www.rainviewer.com/">RainViewer</a>' }});
                rvLayers[i].addTo(map);
            }}
            return rvLayers[i];
        }}
        function hideAllRadar() {{
            for (var k=0;k<rvLayers.length;k++) if (rvLayers[k]) rvLayers[k].setOpacity(0.0);
        }}

        // --- cloud WMS: PRELOAD one layer per time; swap by showing the new
        //     frame fully BEFORE hiding the old one, so no black gap appears ---
        var cloudCurrent = -1;
        function cloudLayerFor(i) {{
            if (!map) return null;
            if (!cloudLayers[i]) {{
                cloudLayers[i] = L.tileLayer.wms(
                    "https://view.eumetsat.int/geoserver/wms", {{
                        layers:"msg_fes:ir108", format:"image/png", transparent:true,
                        version:"1.1.1", opacity:0.0, zIndex:150,
                        time: cloudTimes[i], attribution:"© EUMETSAT" }});
                cloudLayers[i].addTo(map);
            }}
            return cloudLayers[i];
        }}
        function preloadClouds() {{
            if (!map) return;
            for (var i=0;i<cloudTimes.length;i++) cloudLayerFor(i);
        }}
        function hideAllClouds() {{
            for (var k=0;k<cloudLayers.length;k++) if (cloudLayers[k]) cloudLayers[k].setOpacity(0.0);
            cloudCurrent = -1;
        }}
        function showCloud(i) {{
            if (!map) return;
            var next = cloudLayerFor(i);
            if (!next) return;
            // bring the new frame fully up FIRST (it's opaque over the old one)
            next.setOpacity(0.6);
            // then hide the previously-shown frame underneath it
            if (cloudCurrent !== -1 && cloudCurrent !== i && cloudLayers[cloudCurrent]) {{
                cloudLayers[cloudCurrent].setOpacity(0.0);
            }}
            cloudCurrent = i;
            cloudLayerFor((i+1)%cloudTimes.length);  // ensure neighbour ready
        }}
        function nearestCloudIndex(unixSec) {{
            var best = 0, bestd = Infinity;
            for (var i=0;i<cloudTimes.length;i++) {{
                var d = Math.abs(new Date(cloudTimes[i]).getTime()/1000 - unixSec);
                if (d < bestd) {{ bestd = d; best = i; }}
            }}
            return best;
        }}

        // --- the ACTIVE timeline: radar if on, else cloud ---
        function activeLen() {{
            if (radarOn && rvFrames.length) return rvFrames.length;
            if (cloudOn) return cloudTimes.length;
            return 0;
        }}
        function show(i) {{
            var n = activeLen();
            if (!n) return;
            if (i < 0) i = 0; if (i >= n) i = n-1;
            current = i;
            document.getElementById("wx-slider").value = i;

            if (radarOn && rvFrames.length) {{
                // radar drives
                var f = rvFrames[i];
                var tag = f.kind === "forecast" ? " (forecast)" : "";
                var label = fmtUnix(f.time) + tag;
                if (map) {{
                    hideAllRadar();
                    var lyr = rvLayerFor(i); if (lyr) lyr.setOpacity(0.7);
                    rvLayerFor((i+1)%rvFrames.length);
                }}
                // cloud follows, if on (preloaded -> smooth)
                if (cloudOn && map) {{
                    var ci = nearestCloudIndex(f.time);
                    showCloud(ci);
                    label += "  Cloud " + fmtISO(cloudTimes[ci]);
                }}
                document.getElementById("wx-time").innerHTML = label;
            }} else if (cloudOn) {{
                // cloud drives alone (preloaded -> smooth)
                if (map) showCloud(i);
                document.getElementById("wx-time").innerHTML = "Cloud " + fmtISO(cloudTimes[i]);
            }}
        }}
        function next() {{ show((current+1) % activeLen()); }}
        function prev() {{ var n=activeLen(); show((current-1+n)%n); }}
        function play() {{ if(!activeLen())return; playing=true;
            document.getElementById("wx-play").innerHTML="⏸"; timer=setInterval(next,speedMs); }}
        function stop() {{ playing=false;
            document.getElementById("wx-play").innerHTML="▶"; if(timer)clearInterval(timer); }}
        function toggle() {{ playing?stop():play(); }}

        function refreshPanel() {{
            var panel = document.getElementById("wx-panel");
            var slider = document.getElementById("wx-slider");
            var anyOn = (radarOn && rvFrames.length) || cloudOn;
            if (anyOn) {{
                panel.style.display = "flex";
                slider.max = activeLen() - 1;
                // start at newest frame of the active timeline
                show(activeLen() - 1);
            }} else {{
                stop();
                panel.style.display = "none";
            }}
        }}

        function init() {{
            var radarChk = document.getElementById("wx-radar-chk");
            var cloudChk = document.getElementById("wx-cloud-chk");
            if (!radarChk || !cloudChk) {{ setTimeout(init,200); return; }}
            if (!map) map = getMap();

            document.getElementById("wx-play").onclick = toggle;
            document.getElementById("wx-next").onclick = function(){{ stop(); next(); }};
            document.getElementById("wx-prev").onclick = function(){{ stop(); prev(); }};
            document.getElementById("wx-slider").oninput = function(){{ stop(); show(parseInt(this.value)); }};
            document.getElementById("wx-speed").onchange = function(){{
                speedMs = parseInt(this.value); if (playing) {{ stop(); play(); }}
            }};

            // disable radar checkbox if RainViewer gave us nothing
            if (!rvFrames.length) {{
                radarChk.disabled = true;
                radarChk.parentElement.style.opacity = 0.5;
                radarChk.parentElement.title = "Radar unavailable right now";
            }}

            radarChk.onchange = function() {{
                radarOn = this.checked;
                if (!map) map = getMap();
                if (!radarOn) hideAllRadar();
                refreshPanel();
            }};
            cloudChk.onchange = function() {{
                cloudOn = this.checked;
                if (!map) map = getMap();
                if (cloudOn) {{
                    if (map) preloadClouds();   // build all frames up front -> smooth
                }} else {{
                    hideAllClouds();
                }}
                refreshPanel();
            }};

            // keep trying to grab the map so tiles light up once it exists
            (function waitMap() {{
                if (!map) map = getMap();
                if (map) {{ if ((radarOn&&rvFrames.length)||cloudOn) show(current); return; }}
                setTimeout(waitMap, 300);
            }})();
        }}
        init();
    }});
    </script>
    """

    m.get_root().html.add_child(folium.Element(toggles))
    m.get_root().html.add_child(folium.Element(panel))
    m.get_root().html.add_child(folium.Element(js))
    print(f"Added weather layers: radar {len(rv_frames)} frames, cloud {len(cloud_times)} steps.",
          file=sys.stderr)
def build_map(airports, pseudos, route_name, out_path):
    # resolve coordinates
    ap = [(a, ICAO_COORDS[a["code"]]) for a in airports if a["code"] in ICAO_COORDS]
    ps, ps_missing = [], []
    for p in pseudos:
        c = ps_coords(p["code"], p["name"])
        (ps.append((p, c)) if c else ps_missing.append(p))

    ap_missing = [a for a in airports if a["code"] not in ICAO_COORDS]
    for label, missing in [("airport ICAO", ap_missing), ("pseudo-station", ps_missing)]:
        if missing:
            print(f"\nNo coordinates for these {label} entries (add to the table):", file=sys.stderr)
            for x in missing:
                print(f"  {x['code']}  {x['name']}", file=sys.stderr)

    # De-duplicate: several sites (Cork, Dublin, Shannon, Knock...) arrive as
    # BOTH an airport METAR and a pseudo-station at the same coordinates. Keep
    # the airport METAR and drop the co-located pseudo-station.
    def _key(c):
        return (round(c[0], 3), round(c[1], 3))   # ~100 m tolerance
    ap_locs = {_key(c) for _, c in ap}
    ps_dedup, dropped = [], []
    for item, c in ps:
        (dropped if _key(c) in ap_locs else ps_dedup).append((item, c))
    if dropped:
        names = ", ".join(f"{it['code']}" for it, _ in dropped)
        print(f"Dropped {len(dropped)} pseudo-station(s) duplicating an airport "
              f"METAR: {names}", file=sys.stderr)
    ps = ps_dedup

    allpts = [c for _, c in ap + ps]
    if not allpts:
        raise SystemExit("Nothing to plot.")
    lats = [la for la, _ in allpts]
    lons = [lo for _, lo in allpts]
    lat0 = sum(lats) / len(lats)
    lon0 = sum(lons) / len(lons)
    # bounding box of the stations, with a small margin so pins aren't on the edge
    margin = 0.6  # degrees
    sw = [min(lats) - margin, min(lons) - margin]
    ne = [max(lats) + margin, max(lons) + margin]
    m = folium.Map(location=[lat0, lon0], tiles=None,
                   min_zoom=5,
                   max_bounds=True,
                   min_lat=sw[0] - 2, max_lat=ne[0] + 2,
                   min_lon=sw[1] - 3, max_lon=ne[1] + 3)
    # dark basemap, but not shown as a toggle in the layer control
    folium.TileLayer("CartoDB dark_matter", control=False).add_to(m)
    # frame the map tightly on the stations (overrides any default zoom)
    m.fit_bounds([sw, ne])

    # animated radar + EUMETSAT cloud, sharing one player (top-right toggles)
    add_weather_layers(m)

    # bright-yellow coastlines / national outlines for UK + Ireland.
    # Loaded from uk_ireland.geojson sitting next to this script; skipped
    # (with a note) if that file isn't present, so the map still builds.
    try:
        geo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "uk_ireland.geojson")
        with open(geo_path, "r", encoding="utf-8") as fh:
            coast = json.load(fh)
        folium.GeoJson(
            coast,
            name="Coastlines (UK & Ireland)",
            style_function=lambda _f: {
                "color": "#FFEB00",   # bright yellow
                "weight": 1.6,
                "fill": False,
                "opacity": 0.9,
            },
        ).add_to(m)
    except FileNotFoundError:
        print("uk_ireland.geojson not found next to the script; "
              "coastline layer skipped.", file=sys.stderr)
    except Exception as e:
        print(f"Could not add coastline layer ({e}); continuing.", file=sys.stderr)

    fg_ap = folium.FeatureGroup(name="Airport METARs", show=True)
    fg_ps = folium.FeatureGroup(name="Pseudo-stations", show=True)

    def small_pin(lat, lon, item, fg, bg, glyph, txt_color):
        # compact circular pin with a glyph, plus a small label ABOVE it
        pin_html = (
            f'<div style="width:16px;height:16px;border-radius:50%;'
            f'background:{bg};border:1.5px solid #000;box-shadow:0 0 3px #000;'
            f'display:flex;align-items:center;justify-content:center;'
            f'color:#000;font-size:9px;line-height:1">'
            f'<i class="fa fa-{glyph}"></i></div>')
        folium.map.Marker(
            [lat, lon],
            tooltip=f"{item['name']} ({item['code']})",
            popup=folium.Popup(popup_html(item["name"], item["code"], item["report"]), max_width=340),
            icon=folium.DivIcon(
                icon_size=(16, 16), icon_anchor=(8, 8),
                html=pin_html)).add_to(fg)
        # label centered above the pin
        folium.map.Marker([lat, lon], icon=folium.DivIcon(
            icon_size=(0, 0), icon_anchor=(0, 0),
            html=(f'<div style="font-size:9px;font-weight:600;color:{txt_color};'
                  f'text-shadow:0 0 3px #000;white-space:nowrap;'
                  f'transform:translate(-50%,-20px)">{item["name"]}</div>'))).add_to(fg)

    for item, (lat, lon) in ap:
        small_pin(lat, lon, item, fg_ap, "#8ecbff", "plane", "#8ecbff")

    for item, (lat, lon) in ps:
        small_pin(lat, lon, item, fg_ps, "#7CFC9E", "cloud", "#7CFC9E")

    fg_ap.add_to(m); fg_ps.add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)

    # Merge the LayerControl visually with the weather-toggle box above it:
    # dark theme, no rounded top, flush to the right edge so they read as one.
    join_css = """
    <style>
      /* pin Leaflet's layer control directly beneath our weather box */
      .leaflet-top.leaflet-right { top: 92px; }
      .leaflet-control-layers {
          background: #1a1a1a !important;
          color: #eee !important;
          border: 1px solid #444 !important;
          border-top: none !important;
          border-radius: 0 0 6px 6px !important;
          box-shadow: 0 2px 6px rgba(0,0,0,0.5) !important;
          font-family: sans-serif !important;
          font-size: 13px !important;
          min-width: 170px !important;
          margin-right: 10px !important;
          padding: 4px 10px 8px !important;
      }
      .leaflet-control-layers-expanded { padding: 4px 10px 8px !important; }
      .leaflet-control-layers label { color: #eee !important; margin: 2px 0 !important; }
      .leaflet-control-layers-separator { border-top: 1px solid #444 !important; }
      /* hide the little layers icon; we always show it expanded */
      .leaflet-control-layers-toggle { display: none !important; }
    </style>
    """
    m.get_root().html.add_child(folium.Element(join_css))

    m.save(out_path)
    return out_path, len(ap), len(ps)


def main():
    session = requests.Session()
    login(session)
    url = find_route_url(session, ROUTE_NAME)
    if not url:
        raise SystemExit(f'Could not find a briefing matching "{ROUTE_NAME}".')

    # DEBUG: set MSB_DUMP=1 to save the raw briefing HTML and flattened text
    # so the page structure (TAF/NOTAM/winds sections) can be inspected. This
    # writes msb_debug.html and msb_debug.txt next to the script, then exits.
    if os.environ.get("MSB_DUMP"):
        raw = session.get(url).content
        with open("msb_debug.html", "wb") as fh:
            fh.write(raw)
        text_dump = BeautifulSoup(raw, "html.parser").get_text("\n")
        with open("msb_debug.txt", "w", encoding="utf-8") as fh:
            fh.write(text_dump)
        print("Wrote msb_debug.html and msb_debug.txt. Inspect these for the "
              "TAF / NOTAM / winds sections, then share the relevant parts.",
              file=sys.stderr)
        return

    text = scrape_page_text(session, url)
    airports = parse_airports(text)
    pseudos = parse_pseudos(text)
    print(f"Scraped {len(airports)} airport METARs and {len(pseudos)} pseudo-stations.", file=sys.stderr)

    out_path = os.environ.get("MSB_OUT", "maritime_patrol_map.html")
    out, n_ap, n_ps = build_map(airports, pseudos, ROUTE_NAME, out_path)
    print(f"Plotted {n_ap} airports + {n_ps} pseudo-stations.", file=sys.stderr)
    print(f"Map written to {out}", file=sys.stderr)

    # Also emit structured data for the Design app to consume.
    db = build_airport_db(airports, pseudos)
    data_path = os.environ.get("MSB_DATA_OUT", "airport_data.js")
    write_airport_data_js(db, data_path)


if __name__ == "__main__":
    main()
