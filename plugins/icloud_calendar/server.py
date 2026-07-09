"""icloud_calendar, private iCloud calendar over CalDAV.

Apple ships no calendar REST API; the supported path is CalDAV on
``caldav.icloud.com`` with HTTP Basic auth (Apple ID + an app-specific
password generated at appleid.apple.com). This module speaks the small
slice of CalDAV we need with stdlib ``urllib`` — the shared
``app/plugin_http.py`` helpers are GET-only and can't issue the
``PROPFIND`` / ``REPORT`` verbs CalDAV requires.

Flow (all Basic-authed):
  1. PROPFIND caldav.icloud.com/           → current-user-principal
  2. PROPFIND <principal>                   → calendar-home-set
     (the home href carries the per-account ``pNN-caldav.icloud.com``
      partition host — this is why the manifest declares ``network:*``,
      the host can't be known ahead of time and the capability layer
      matches hostnames by exact string.)
  3. PROPFIND <home> Depth:1                → list calendar collections
  4. REPORT <calendar> calendar-query       → VEVENTs in the time window

Steps 1-3 (discovery) are cached 24h; step 4's events are cached 10min,
mirroring weather_now's cache pattern, so only a cold render pays the
full round-trip chain and a warm render is a single disk read.

Parsing + recurrence expansion reuse the ``icalendar`` /
``recurring_ical_events`` deps (same as calendar_core); timezone
bucketing reuses ``app.calendar_time`` + ``app.tz_resolve``.

Never raises: every failure path returns ``{"error": "...", "days": []}``
with a message written for the person editing the cell.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import icalendar
import recurring_ical_events

from app.calendar_time import (
    all_day_event_overlaps_date,
    event_local_date_key,
    local_midnight_utc,
)
from app.tz_resolve import app_timezone

USER_AGENT = "tesserae/0.1 (+icloud_calendar)"
HTTP_TIMEOUT_S = 8
CACHE_TTL_S = 600  # 10 min, per weather_now
DISCOVERY_TTL_S = 86_400  # 24 h
WINDOW_DAYS = 7

CALDAV_ROOT = "https://caldav.icloud.com/"
DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"

# Accent slots cycle 1..6 so each calendar reads as a distinct categorical
# series while staying inside the theme's palette (docs: reach by role, no
# hex). The client turns the slot into var(--accent-N) / var(--accent-N-soft).
ACCENT_SLOTS = 6


class _AuthError(Exception):
    """iCloud rejected the credentials (401/403)."""


# ----- HTTP -----------------------------------------------------------


def _dav(
    url: str, method: str, body: str, apple_id: str, app_pw: str, depth: str
) -> bytes:
    """Issue one authenticated CalDAV request and return the response
    bytes. Raises ``_AuthError`` on 401/403; other HTTP/URL errors
    propagate to the caller's ``except``."""
    token = base64.b64encode(f"{apple_id}:{app_pw}".encode()).decode()
    req = urllib.request.Request(url, data=body.encode("utf-8"), method=method)
    req.add_header("Authorization", f"Basic {token}")
    req.add_header("Content-Type", 'application/xml; charset="utf-8"')
    req.add_header("Depth", depth)
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return resp.read()
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            raise _AuthError from err
        raise


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


# ----- discovery ------------------------------------------------------


_PROP_PRINCIPAL = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/>'
    "</d:prop></d:propfind>"
)
_PROP_HOME = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><c:calendar-home-set/></d:prop></d:propfind>"
)
_PROP_LIST = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><d:displayname/><d:resourcetype/>"
    "<c:supported-calendar-component-set/></d:prop></d:propfind>"
)


def _discover_calendars(apple_id: str, app_pw: str) -> list[dict[str, Any]]:
    """Walk principal → home → calendar list. Returns a list of
    ``{"href": abs_url, "name": str, "accent": 1..6}``."""
    # 1. principal
    xml = _dav(CALDAV_ROOT, "PROPFIND", _PROP_PRINCIPAL, apple_id, app_pw, "0")
    root = ET.fromstring(xml)
    href = _text(root.find(f".//{{{DAV}}}current-user-principal/{{{DAV}}}href"))
    if not href:
        return []
    principal_url = urljoin(CALDAV_ROOT, href)

    # 2. calendar-home-set (href carries the partition host)
    xml = _dav(principal_url, "PROPFIND", _PROP_HOME, apple_id, app_pw, "0")
    root = ET.fromstring(xml)
    home = _text(root.find(f".//{{{CALDAV}}}calendar-home-set/{{{DAV}}}href"))
    if not home:
        return []
    home_url = urljoin(principal_url, home)

    # 3. list collections (Depth 1)
    xml = _dav(home_url, "PROPFIND", _PROP_LIST, apple_id, app_pw, "1")
    root = ET.fromstring(xml)
    cals: list[dict[str, Any]] = []
    for resp in root.findall(f"{{{DAV}}}response"):
        rtype = resp.find(f".//{{{DAV}}}resourcetype")
        if rtype is None or rtype.find(f"{{{CALDAV}}}calendar") is None:
            continue  # not a calendar collection (home root, inbox, …)
        comps = {c.get("name") for c in resp.findall(f".//{{{CALDAV}}}comp")}
        if comps and "VEVENT" not in comps:
            continue  # reminders / task-only collection
        href_txt = _text(resp.find(f"{{{DAV}}}href"))
        if not href_txt:
            continue
        name = _text(resp.find(f".//{{{DAV}}}displayname"))
        if not name:
            name = href_txt.rstrip("/").rsplit("/", 1)[-1] or "Calendar"
        cals.append({"href": urljoin(home_url, href_txt), "name": name})
    for i, cal in enumerate(cals):
        cal["accent"] = (i % ACCENT_SLOTS) + 1
    return cals


# ----- events ---------------------------------------------------------


def _events_body(start: datetime, end: datetime) -> str:
    s = start.strftime("%Y%m%dT%H%M%SZ")
    e = end.strftime("%Y%m%dT%H%M%SZ")
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><c:calendar-data/></d:prop>"
        '<c:filter><c:comp-filter name="VCALENDAR">'
        '<c:comp-filter name="VEVENT">'
        f'<c:time-range start="{s}" end="{e}"/>'
        "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"
    )


def _expand(blob: bytes, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Parse a VCALENDAR blob and return JSON-safe events in [start, end),
    recurrences expanded. Mirrors calendar_core._expand_events: timed
    events normalise to UTC ISO, all-day keep date-only ISO."""
    try:
        cal = icalendar.Calendar.from_ical(blob)
    except Exception:
        return []
    try:
        events = recurring_ical_events.of(cal).between(start, end)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for ev in events:
        try:
            dtstart = ev.get("DTSTART")
            dtend = ev.get("DTEND") or dtstart
            sdt = dtstart.dt if dtstart else None
            edt = dtend.dt if dtend else sdt
            if sdt is None:
                continue
            all_day = not hasattr(sdt, "hour")
            if all_day:
                s_iso = sdt.isoformat()
                e_iso = edt.isoformat() if edt else s_iso
            else:
                if sdt.tzinfo is None:
                    sdt = sdt.replace(tzinfo=UTC)
                if edt and getattr(edt, "tzinfo", None) is None:
                    edt = edt.replace(tzinfo=UTC)
                s_iso = sdt.astimezone(UTC).isoformat()
                e_iso = edt.astimezone(UTC).isoformat() if edt else s_iso
            out.append(
                {
                    "summary": str(ev.get("SUMMARY") or "").strip() or "(untitled)",
                    "location": str(ev.get("LOCATION") or "").strip(),
                    "start": s_iso,
                    "end": e_iso,
                    "all_day": all_day,
                }
            )
        except Exception:
            continue
    return out


def _query_calendar(
    cal: dict[str, Any], start: datetime, end: datetime, apple_id: str, app_pw: str
) -> list[dict[str, Any]]:
    xml = _dav(cal["href"], "REPORT", _events_body(start, end), apple_id, app_pw, "1")
    root = ET.fromstring(xml)
    events: list[dict[str, Any]] = []
    for cdata in root.findall(f".//{{{CALDAV}}}calendar-data"):
        blob = (cdata.text or "").encode("utf-8")
        if not blob.strip():
            continue
        for ev in _expand(blob, start, end):
            ev["cal"] = cal["name"]
            ev["accent"] = cal["accent"]
            events.append(ev)
    return events


# ----- caching --------------------------------------------------------


def _fresh(path: Path, ttl: int) -> dict[str, Any] | None:
    if not path.exists() or time.time() - path.stat().st_mtime >= ttl:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write(path: Path, payload: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")


def _discover_cached(
    data_dir: Path, key: str, apple_id: str, app_pw: str
) -> list[dict[str, Any]]:
    path = data_dir / f"discovery_{key}.json"
    cached = _fresh(path, DISCOVERY_TTL_S)
    if cached is not None and isinstance(cached.get("calendars"), list):
        return cached["calendars"]
    cals = _discover_calendars(apple_id, app_pw)
    _write(path, {"calendars": cals})
    return cals


def _load_events(
    data_dir: Path,
    key: str,
    apple_id: str,
    app_pw: str,
    start: datetime,
    end: datetime,
    today_iso: str,
) -> list[dict[str, Any]]:
    """All events across every calendar for the window, cached 10 min.
    The cell's ``calendars`` filter is applied by the caller so the cache
    is shared across cells with different filters."""
    path = data_dir / f"events_{key}_{today_iso}.json"
    cached = _fresh(path, CACHE_TTL_S)
    if cached is not None and isinstance(cached.get("events"), list):
        return cached["events"]
    cals = _discover_cached(data_dir, key, apple_id, app_pw)
    events: list[dict[str, Any]] = []
    for cal in cals:
        events.extend(_query_calendar(cal, start, end, apple_id, app_pw))
    _write(path, {"events": events})
    return events


# ----- entry point ----------------------------------------------------


def _wanted_calendars(raw: str) -> set[str] | None:
    names = {n.strip().lower() for n in (raw or "").split(",") if n.strip()}
    return names or None


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    apple_id = str(settings.get("apple_id") or "").strip()
    app_pw = str(settings.get("app_password") or "").strip()
    if not apple_id or not app_pw:
        return {
            "error": (
                "Add your Apple ID and app-specific password in "
                "Settings → Plugins → iCloud Calendar."
            ),
            "days": [],
        }

    zone = app_timezone()
    today = datetime.now(zone).date()
    start_dt = local_midnight_utc(today, zone)
    end_dt = local_midnight_utc(today + timedelta(days=WINDOW_DAYS), zone)

    data_dir = Path(ctx["data_dir"])
    key = hashlib.sha1(apple_id.lower().encode()).hexdigest()[:10]  # noqa: S324

    try:
        events = _load_events(
            data_dir, key, apple_id, app_pw, start_dt, end_dt, today.isoformat()
        )
    except _AuthError:
        return {
            "error": "Check your Apple ID and app-specific password.",
            "days": [],
        }
    except Exception:
        return {"error": "Couldn't reach iCloud right now.", "days": []}

    wanted = _wanted_calendars(options.get("calendars") or "")
    if wanted is not None:
        events = [e for e in events if str(e.get("cal", "")).lower() in wanted]

    days: list[dict[str, Any]] = []
    cur = today
    for _ in range(WINDOW_DAYS):
        date_iso = cur.isoformat()
        day_events = [
            e
            for e in events
            if (
                all_day_event_overlaps_date(e, cur)
                if e.get("all_day")
                else event_local_date_key(e, zone) == date_iso
            )
        ]
        day_events.sort(key=lambda e: (not e["all_day"], e["start"]))
        days.append(
            {
                "date": date_iso,
                "day": cur.day,
                "is_today": cur == today,
                "weekday": cur.weekday(),  # 0 = Monday
                "events": [
                    {
                        "summary": e["summary"],
                        "location": e.get("location", ""),
                        "start": e["start"],
                        "end": e.get("end"),
                        "all_day": e.get("all_day", False),
                        "accent": e.get("accent", 4),
                    }
                    for e in day_events
                ],
            }
        )
        cur += timedelta(days=1)

    return {
        "start": today.isoformat(),
        "end": (today + timedelta(days=WINDOW_DAYS - 1)).isoformat(),
        "days": days,
    }
