"""__NAME__ (__ID__) smoke test, parametrized over sizes.

Self-guarding: while this lives in the ``_template`` folder the loader skips
(leading ``_``), ``__ID__`` is never registered, so every test SKIPS and a full
``pytest plugins/`` stays green. After you copy the folder and replace
``__ID__`` with the real id, the plugin loads and these run for real.

The template's server.py returns demo data offline, so the happy path needs no
network mock. When you wire a real source, use the commented patterns below.
"""

from __future__ import annotations

# from unittest.mock import patch  # uncomment when you mock a network source

import pytest
from flask import Flask
from flask.testing import FlaskClient

PLUGIN_ID = "__ID__"


def _skip_if_not_loaded(app: Flask) -> None:
    if app.config["PLUGIN_REGISTRY"].get(PLUGIN_ID) is None:
        pytest.skip(f"{PLUGIN_ID!r} not loaded — copy the template and rename it first")


@pytest.mark.parametrize("size", ["xs", "sm", "md", "lg"])
def test_renders(app: Flask, client: FlaskClient, size: str) -> None:
    _skip_if_not_loaded(app)

    # If your widget needs plugin settings, seed them inside an app context:
    #   with app.app_context():
    #       store = app.config["SETTINGS_STORE"]
    #       store.update_section("plugins", {PLUGIN_ID: {"api_url": "http://x",
    #                                                     "api_key_secret": "k"}})
    # and mock the network:
    #   with patch("urllib.request.urlopen", side_effect=[_FakeResp(body), ...]):
    #       resp = client.get(...)

    resp = client.get(f"/_test/render?plugin={PLUGIN_ID}&size={size}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f'data-plugin="{PLUGIN_ID}"' in body
    # A value from the built-in demo data (server.py:_demo). Replace with a
    # value from your real payload once wired.
    assert "Online" in body


# Reference: a fake urllib response for mocked network tests.
# class _FakeResp:
#     def __init__(self, body: bytes) -> None:
#         self._body = body
#     def read(self) -> bytes:
#         return self._body
#     def __enter__(self) -> "_FakeResp":
#         return self
#     def __exit__(self, *a: object) -> bool:
#         return False
