---
name: create-tesserae-widget
description: >
  Build a new Tesserae widget plugin (a drop-a-folder plugins/<id>/ with
  plugin.json + client.js + optional server.py + smoke test). Use when the
  user wants to create, scaffold, or design a new Tesserae widget/plugin, add
  a data source to the e-ink dashboard, or asks about the Spectra design
  system, archetypes, cell sizes, secrets, capabilities, or CalDAV/API fetch
  in this repo. Distilled recipe + the non-obvious gotchas that cost time.
---

# Creating a Tesserae widget

A widget is a folder the loader picks up at boot; the composer mounts it into a
Shadow DOM as `render(shadow, ctx)`, screenshots it with Playwright, quantises
to the panel palette, and streams over MQTT. It renders **once, statically** —
no motion, no client fetch.

Read `docs/widgets.md` + `docs/widget-design-system.md` for the full contract.
This skill is the fast path + the traps.

## 0. Before writing: pick the shape

- **Archetype** (the `.w-body` class — the archetype carries the font cascade,
  gaps, zoom sizing; don't roll your own body layout):
  `.stat-body` (one hero number) · `.list-body` (zebra rows) · `.chart-body`
  (Chart.js) · `.status-body` (pill + hero + grid) · `.cal-body` /
  `.tt-*` timetable / `.mc-*` month grid (calendars) · `.wx-body` (weather) ·
  `.img-body` (hero image).
- **Copy the closest shipped widget wholesale**, then adapt. The best refs:
  `weather_now` (stat/wx + disk-cache server.py), `weather_hourly` (chart via
  `spectra-chart.js` `tokens()` probe), `calendar_day`/`calendar_week`
  (`.tt-*` timetable + positioned event blocks), `ha_core`/`picture_apod`
  (secret settings), `f1_standings_drivers` (data-identity hex colours).
- **Sizes**: design for the four test-render fixtures — xs 180×180, sm 380×240,
  md 640×400, lg 1200×800. `ctx.cell.size` gives the token. Use size for
  *structural* changes (drop sections, change day-count/columns), `clamp(min,
  N·cqmin, max)` for fluid type/icons.

## 1. File layout

```
plugins/<id>/            # <id> = folder name = URL slug; lowercase [a-z0-9_]
  plugin.json            # manifest (required)
  client.js              # ES module, default export render(shadow, ctx)
  server.py              # optional, server-side data fetch -> ctx.data
  tests/test_smoke.py    # parametrized over sizes
```
No `client.css` — inline a `<style>` block inside `shadow.innerHTML`. Name it
`<family>_<role>` (e.g. `weather_now`, `icloud_calendar`).

## 2. Manifest (`plugin.json`)

```jsonc
{
  "tesserae_compat": "1.x",
  "name": "Human Name",
  "version": "0.1.0",
  "kind": "widget",                      // or "data" for a headless _core sibling
  "description": "One clear sentence.",
  "icon": "ph-calendar-dots",            // Phosphor name, editor picker
  "supports": { "sizes": ["xs","sm","md","lg"] },
  "settings": [                          // plugin-wide (one set across all cells)
    { "name": "api_key", "type": "string", "label": "API key", "secret": true, "default": "" }
  ],
  "cell_options": [                      // per-cell knobs -> ctx.cell.options
    { "name": "units", "type": "select", "label": "Units", "default": "metric",
      "choices": [ {"value":"metric","label":"Metric"}, {"value":"imperial","label":"Imperial"} ] }
  ],
  "render": { "dither": "none", "needs_network": true },
  "requires": [ "network:api.example.com", "settings:plugin" ]
}
```

- `cell_options` types: `string`, `textarea`, `number`, `select`/`multiselect`
  (need `choices` or `choices_from`), `boolean`, `color`, `location_search`.
  **No `variant` option** — visual direction comes from the page `data-style`
  axis. A genuine layout fork uses `layout` with shape-describing values.
- `settings` with `"secret": true` → stored encrypted (`enc:v1:…`) under a
  `<name>_secret` disk key; handed to `fetch()` as **real plaintext**. Model:
  `ha_core`.
- `requires:` — see the capability gotcha in §7. Omitting it = no enforcement.
- `"design": { "palette": "extended" }` only if you need arbitrary CSS
  colours/gradients (dithered on panel). Default `strict` = accent tokens only.

## 3. `client.js`

```js
export default function render(shadow, ctx) {
  const data = ctx?.data ?? {};
  const size = ctx?.cell?.size || "md";
  const opts = ctx?.cell?.options || {};
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;

  if (data.error) {                       // server.py returned {error}
    shadow.innerHTML = `${css}<div class="w" data-widget="<id>">
      <div class="w-title"><i class="ph-bold ph-warning-circle" style="color:var(--accent-1)"></i><h3>Name</h3></div>
      <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div></div>`;
    return;
  }
  // ... build ONE shadow.innerHTML string (idempotent — no appending).
}
```

- Link `/static/style/spectra-widgets.css` **first** (it `@import`s the Phosphor
  bold/regular fonts into shadow scope — no extra font links needed).
- Shell: `.w` → optional `.w-title` (or a `.cal-head`) → `.w-body <archetype>`.
- **Idempotent**: overwrite `shadow.innerHTML` in one assignment. `async` is
  awaited before screenshot, but nothing may resolve after it returns.
- **No** `fetch()`, `setInterval`, `transition`, `animation`, `:hover`,
  `requestAnimationFrame`. It's a still frame; the renderer disables animation.
- Always `escapeHtml()` user/event text.
- Charts: `import { tokens, barChart, lineChart, sparkline, hbar } from
  "../../static/spectra-chart.js"`; `const t = tokens(shadow.host)` resolves the
  live palette; helpers set `animation:false`.

## 4. Colour + type discipline (paint from semantic tokens only)

- Accents are **fixed roles by position, not hue**: 1 alert/now · 2
  warning/winner · 3 positive/up · 4 primary/today/live · 5 secondary series ·
  6 third. Reach by role; the theme picks the hue. Each has a `--accent-N-soft`
  companion for tints, and `--on-accent` for text on an accent fill.
- Surfaces/text: `--bg`, `--surface`, `--surface-sunken`, `--text-primary/-secondary/-muted`.
- **Never**: hard-coded hex (except data-identity — team/brand/flag colours,
  fall back to `var(--surface-sunken)`); `#000`/`#fff` (ghosts on E6);
  `text-transform: uppercase` (use `var(--label-transform, uppercase)`);
  hard-coded stroke widths (use `--stroke-1..3` / `--edge-weight`);
  `color-mix()` for a soft tint when `--accent-N-soft` exists; drawn internal
  borders (single outer `--edge` only — hierarchy via spacing/weight/sunken).
- **Font cascade**: leave `--font-family` alone (the `.w` shell inherits the
  style font). If you must override inline, use a **non-recursive** fallback
  (`'Archivo Black', system-ui, sans-serif`) — writing `var(--font-family, …)`
  as the fallback is a self-reference that silently breaks every chart's font.
- `tabular-nums` on figures. Prominent icons: `ph-bold` big; never `ph-fill`.

## 5. `server.py` (only if the browser can't get the data)

```python
def fetch(options: dict, settings: dict, *, ctx: dict) -> dict:
    # ctx = {"panel_w", "panel_h", "preview", "data_dir"}  — NOTE: no cell size!
    # Never raise — return {"error": "friendly message"} on any failure.
    ...
```

- **Friendly errors**: the string lands directly in the cell. Translate
  `HTTPError 401` → "Check your token", invalid input → name the field, else a
  tame "Couldn't load X right now."
- **Disk-cache** in `Path(ctx["data_dir"])` — copy the 10-min `_cached()`/mtime
  pattern from `plugins/weather_now/server.py`. Called fresh every render; the
  composer has a ~6 s per-widget budget, so keep timeouts tight (5–8 s,
  `retries=0`) and cache aggressively. Slow multi-request chains (CalDAV, OAuth)
  should cache a long-TTL discovery step separately from short-TTL data.
- **Reusable host helpers** (import from `app.*`, like the shipped widgets do):
  - `from app.plugin_http import fetch_json, fetch_text` — GET-only JSON/text
    with timeout/retries/User-Agent. For custom verbs (PROPFIND/REPORT/POST
    body) use `urllib.request` directly with `method=` + your own headers.
  - `from app.calendar_time import local_midnight_utc, event_local_date_key,
    all_day_event_overlaps_date` and `from app.tz_resolve import app_timezone`
    — timezone bucketing for calendar widgets.
  - `icalendar` + `recurring_ical_events` are deps — parse/expand `.ics`
    (copy `calendar_core/server.py:_expand_events`). No CalDAV/HTTP lib exists;
    stdlib `urllib` only.
  - Read your own secrets from the `settings` arg (`settings.get("token")`) —
    already decrypted. `_core` siblings read via
    `current_app.config["SETTINGS_STORE"].get_section("plugins")["<id>"]`.
- User-Agent convention: `"tesserae/0.1 (+<id>)"`.
- **`_core` companion pattern** (`kind:"data"`): when a widget *family* shares
  admin state/credentials — a `<family>_core` with `blueprint()` admin page +
  `choices()` + a public API the display widgets call via
  `current_app.config["PLUGIN_REGISTRY"].get("<core>").server_module`. Examples:
  `calendar_core`, `ha_core`, `weather_core`. A single self-contained widget
  does NOT need this.

## 6. Verification recipe (this is the time-saver)

`/_test/render` only exists in debug/testing mode and requires no cell/page.

**Fast path — smoke test (no browser, mocks network):**
```python
# tests/test_smoke.py — the `client`/`app` fixtures come from root conftest.py
@pytest.mark.parametrize("size", ["xs","sm","md","lg"])
def test_renders(client, size):
    with patch("urllib.request.urlopen", side_effect=[_FakeResp(xml1), _FakeResp(xml2), ...]):
        r = client.get(f"/_test/render?plugin=<id>&size={size}")
    assert r.status_code == 200 and 'data-plugin="<id>"' in r.get_data(as_text=True)
```
`_FakeResp` needs `read()` + `__enter__`/`__exit__`. `side_effect=[…]` returns
responses in call order (for multi-request flows). **Seed plugin settings**
inside `app.app_context()`:
```python
store = app.config["SETTINGS_STORE"]
store.update_section("app", {"timezone": "UTC"})   # pin tz for deterministic bucketing
store.update_section("plugins", {"<id>": {"apple_id": "x", "app_password_secret": "y"}})
# secret fields use the _secret disk suffix; plaintext passes through unwrap.
```
Run: `./.venv/bin/python -m pytest plugins/<id>/ -q`

**Visual path — real render + screenshots at exact cell dims:**
```bash
# 1. Disable auth on a scratch data root so /_test/render is reachable from loopback
SCRATCH=/tmp/icdata
./.venv/bin/python - <<'PY'
from pathlib import Path
from app.main import REPO_ROOT, create_app
from app.auth import set_password_disabled
from app.onboarding import mark_onboarded
app = create_app(data_root=Path("/tmp/icdata"), plugins_dir=REPO_ROOT/"plugins")
with app.app_context():
    s = app.config["SETTINGS_STORE"]; set_password_disabled(s, True); mark_onboarded(s)
    # optionally: s.update_section("plugins", {...}) and write a disk cache file
    #             under /tmp/icdata/plugins/<id>/ to render populated state WITHOUT network
PY
# 2. Serve with --dev (enables /_test/render; reloads client.js on edit)
TESSERAE_DATA_ROOT=/tmp/icdata ./.venv/bin/python -m app.main --dev --port 8790 --host 127.0.0.1 &
# 3. Screenshot each size (Playwright is a dep)
./.venv/bin/python - <<'PY'
from playwright.sync_api import sync_playwright
sizes={"xs":(180,180),"sm":(380,240),"md":(640,400),"lg":(1200,800)}
with sync_playwright() as p:
    b=p.chromium.launch()
    for s,(w,h) in sizes.items():
        pg=b.new_page(viewport={"width":w,"height":h}, device_scale_factor=2)
        pg.goto(f"http://127.0.0.1:8790/_test/render?plugin=<id>&size={s}", wait_until="networkidle")
        pg.wait_for_timeout(400); pg.screenshot(path=f"/tmp/shot_{s}.png")
    b.close()
PY
# then Read the PNGs. Kill server: lsof -ti tcp:8790 | xargs kill
```
To render a **populated** state without hitting a real API, write a cache JSON
into the plugin's `data_dir` (`/tmp/icdata/plugins/<id>/…`) matching your
server's cache schema; restart the server so it re-reads settings from disk.
There's also `python scripts/widget_contact_sheet.py <id>` (needs the dev
server + login) for a theme×style×size sheet.

**Walk all four sizes** — lg is forgiving, xs/sm/md are where things clip.

## 7. Gotchas that cost time

- **Capability host matching is exact-string, no wildcards** (`app/capabilities.py`).
  If an API redirects to per-account/unknowable hosts (e.g. iCloud CalDAV →
  `pNN-caldav.icloud.com`), no finite `network:<host>` list works — you must use
  `network:*` (draws catalog-review scrutiny but is the honest declaration) or
  omit `requires:` (loads unenforced). The socket hook checks the hostname on
  every connect, before DNS, so hard-coding an IP doesn't dodge it.
- **server.py has no cell size** in `ctx` (only panel dims). Return the full
  dataset and slice/branch by `ctx.cell.size` in `client.js`.
- **Header/title text overflows fast.** `--w-font-base` clamps to **28 px** at
  md *and* lg (`clamp(14px, 7cqmin, 28px)`), and `--fs-jumbo` is ~2.5× that
  (~70 px). A `.cal-head-title` / `.w-title h3` string longer than a couple of
  short words ellipsises to "•••" at md. Keep titles terse ("3 DAYS", "THIS
  WEEK"); drop the lead icon / right-meta at xs.
- **Timetable hour-gutter crowds** in short lanes (xs/sm/md): thin numeric ticks
  to every 4 h when the visible span is wide; keep every-2 h only for tall lg
  lanes.
- **Event/cell text clips** if you inherit the archetype's default
  `--fs-body` + `white-space:nowrap`. For narrow columns, shrink to
  `calc(var(--fs-caption) * 0.9)` and allow wrapping via `-webkit-line-clamp`.
- **Time zones**: normalize timed events to **UTC ISO** in server.py; parse in
  client with `new Date(iso)` so the hour lands in the *viewer's* local tz.
  (String-slicing the ISO mis-lands events by the UTC offset.) Note that headless
  Chromium screenshots render in the *machine's* tz, not the app setting — a
  test artifact, not a bug.
- **Secret masking**: the admin GET path shows `********`; only `get_for_runtime`
  (the fetch path) returns real values. Re-submitting the mask keeps the stored
  secret.

## 8. Checklist

1. `plugin.json` (copy from same-archetype widget).
2. `client.js` — shell + one archetype, error state, size branching, tokens only.
3. `server.py` — friendly errors, disk cache, tight timeout (if data-fetched).
4. `tests/test_smoke.py` — parametrized sizes, mock network, seed settings.
5. `pytest plugins/<id>/ -q` green.
6. Screenshot all four sizes; fix clipping at xs/sm/md.
