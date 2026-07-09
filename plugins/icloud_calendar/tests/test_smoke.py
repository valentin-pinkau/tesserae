"""icloud_calendar smoke: the CalDAV discovery + calendar-query chain is
mocked at ``urllib.request.urlopen`` (four sequential responses:
principal → calendar-home → calendar list → calendar-query REPORT), so
no network is touched. Covers the unconfigured error card and the happy
path across every supported size.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient

# --- canned CalDAV responses -----------------------------------------

_PRINCIPAL = b"""<?xml version="1.0" encoding="utf-8"?>
<multistatus xmlns="DAV:">
  <response><href>/</href><propstat><prop>
    <current-user-principal><href>/998877/principal/</href></current-user-principal>
  </prop><status>HTTP/1.1 200 OK</status></propstat></response>
</multistatus>"""

_HOME = b"""<?xml version="1.0" encoding="utf-8"?>
<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <response><href>/998877/principal/</href><propstat><prop>
    <c:calendar-home-set><href>https://p42-caldav.icloud.com/998877/calendars/</href></c:calendar-home-set>
  </prop><status>HTTP/1.1 200 OK</status></propstat></response>
</multistatus>"""

_LIST = b"""<?xml version="1.0" encoding="utf-8"?>
<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <response><href>/998877/calendars/</href><propstat><prop>
    <resourcetype><collection/></resourcetype>
  </prop><status>HTTP/1.1 200 OK</status></propstat></response>
  <response><href>/998877/calendars/work/</href><propstat><prop>
    <displayname>Work</displayname>
    <resourcetype><collection/><c:calendar/></resourcetype>
    <c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>
  </prop><status>HTTP/1.1 200 OK</status></propstat></response>
</multistatus>"""


def _report_xml() -> bytes:
    # A timed event at 12:00Z *today* so it lands inside the rolling
    # window and buckets onto day 0 (visible at every size). Timezone is
    # pinned to UTC in the test so "today" is unambiguous.
    day = datetime.now(UTC).strftime("%Y%m%d")
    ics = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VEVENT\r\nUID:evt-1\r\n"
        f"DTSTAMP:{day}T090000Z\r\nDTSTART:{day}T120000Z\r\nDTEND:{day}T130000Z\r\n"
        "SUMMARY:Standup Sync\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<response><href>/998877/calendars/work/evt-1.ics</href><propstat><prop>"
        f"<c:calendar-data>{ics}</c:calendar-data>"
        "</prop><status>HTTP/1.1 200 OK</status></propstat></response>"
        "</multistatus>"
    ).encode("utf-8")


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *a: object) -> bool:
        return False


def _seed(app: Flask) -> None:
    with app.app_context():
        store = app.config["SETTINGS_STORE"]
        store.update_section("app", {"timezone": "UTC"})
        store.update_section(
            "plugins",
            {"icloud_calendar": {"apple_id": "me@icloud.com", "app_password_secret": "abcd-efgh-ijkl-mnop"}},
        )


# --- tests ------------------------------------------------------------


@pytest.mark.parametrize("size", ["xs", "sm", "md", "lg"])
def test_unconfigured_shows_credentials_hint(client: FlaskClient, size: str) -> None:
    resp = client.get(f"/_test/render?plugin=icloud_calendar&size={size}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'data-plugin="icloud_calendar"' in body
    assert "app-specific password" in body


@pytest.mark.parametrize("size", ["xs", "sm", "md", "lg"])
def test_caldav_events_render(app: Flask, client: FlaskClient, size: str) -> None:
    _seed(app)
    responses = [
        _FakeResp(_PRINCIPAL),
        _FakeResp(_HOME),
        _FakeResp(_LIST),
        _FakeResp(_report_xml()),
    ]
    with patch("urllib.request.urlopen", side_effect=responses):
        resp = client.get(f"/_test/render?plugin=icloud_calendar&size={size}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'data-plugin="icloud_calendar"' in body
    assert "Standup Sync" in body
