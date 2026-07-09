# Widget template

A minimal, working `.list-body` widget you copy to start a new one. The
leading `_` makes the loader skip this folder, so it never mounts as a live
plugin — it's scaffold only.

See `.claude/skills/create-tesserae-widget/SKILL.md` for the full recipe,
archetype choices, token discipline, and the verification workflow.

## Use it

1. Copy the folder and give it a real id (`<family>_<role>`, lowercase
   `[a-z0-9_]`):
   ```sh
   cp -r plugins/_template plugins/my_widget
   ```
2. Find-and-replace the two placeholders across every file:
   - `__NAME__`  → the human name (e.g. `Departures`)
   - `__ID__`    → the folder id you chose (e.g. `my_widget`)
   ```sh
   grep -rl '__NAME__\|__ID__' plugins/my_widget | xargs sed -i '' 's/__NAME__/Departures/g; s/__ID__/my_widget/g'
   ```
   (`data-widget` on the root reads `ctx.cell.plugin_id` at runtime, so it
   already tracks the folder name — no need to touch it.)
3. Pick your archetype and adapt `client.js` (this one uses `.list-body`; swap
   for `.stat-body` / `.chart-body` / `.cal-body` / `.tt-*` / `.wx-body` /
   `.img-body` / `.status-body` as needed — copy the closest shipped widget).
4. Edit `server.py` only if you need data the browser can't reach (APIs, files,
   cross-origin). Delete it entirely if the widget renders from
   `ctx.cell.options` alone.
5. Verify:
   ```sh
   ./.venv/bin/python -m pytest plugins/my_widget/ -q
   ```
   Then render it — see the SKILL's §6 for the dev-server + Playwright
   screenshot recipe. Walk all four sizes (xs/sm/md/lg).

## What's here

```
plugin.json          manifest: sizes, one example secret setting + cell options
client.js            .list-body shell, error + empty states, size branching
server.py            OPTIONAL demo fetch: disk cache + friendly errors + settings
tests/test_smoke.py  parametrized over sizes, mocked network, seeded settings
```

The template renders offline out of the box: `server.py` returns demo data when
no `api_url` setting is configured, so you can screenshot it immediately and see
the pattern before wiring a real source.
