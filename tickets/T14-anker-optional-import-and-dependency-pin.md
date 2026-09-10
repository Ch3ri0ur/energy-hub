# T14: Hub cannot start without the Anker library even when Anker is disabled; dependency is a sibling-path checkout

**Priority:** P3 (remaining: git pin only) | **Effort:** XS | **Area:** packaging

**Status 2026-09-10:** steps 0–2 done (lazy `_load_api()` supporting both layouts, lazy
import in `main.py`, `aiofiles`/`cryptography` added explicitly). Only step 3 (pin a git
rev instead of the sibling path) and step 4 remain, to be done when production is
upgraded to 3.8.x.

## Problem
0. **Confirmed breakage (2026-09-10):** the sibling checkout `../anker-solix-api` is now
   at `v3.8.1-3-g77a0f88` (3.8.2), where the package is `anker_solix_api`
   (`src/anker_solix_api/…`), not `api`. `energy_hub/anker_plugin.py:26-27` does
   `from api.api import AnkerSolixApi` / `from api.errors import AnkerSolixError`, so
   `.venv/bin/python -c "import energy_hub.main"` fails with
   `ModuleNotFoundError: No module named 'api'`. The hub cannot start at all with this
   checkout. Additionally the regenerated `uv.lock` dropped the library's dependency
   list (the lib moved to a poetry build), so `aiofiles` is missing from `.venv`
   (`from anker_solix_api.api import AnkerSolixApi` → `No module named 'aiofiles'`).
   The rest of the plugin's API usage was verified against 3.8.2 and still matches:
   `startMqttSession`, `stopMqttSession`, `get_topic_prefix(deviceDict=)`,
   `realtime_trigger(deviceDict=, timeout=)` (sync), `status_request(deviceDict=)` (sync),
   `message_poller(topics=, trigger_devices=, msg_callback=, timeout=)`, and the
   callback arity `(session, topic, message, data, model, device_sn, values)`
   (`mqtt.py:221-223`).
1. `main.py:17` imports `anker_plugin` unconditionally, and `anker_plugin.py:26-27`
   imports `api.api` / `api.errors` at module load. The docstring says the plugin is
   "fully optional", but a missing library crashes the whole hub at import time, even
   with no Anker credentials.
2. `pyproject.toml:22` pins `anker-solix-api` to `{ path = "../anker-solix-api", editable = true }`.
   The repo is not installable on any machine without that exact sibling directory
   (this dev machine does not have it; `uv sync` fails). `uv.lock` records version 3.5.4.

## Where
- `energy_hub/main.py:17`
- `energy_hub/anker_plugin.py:26-27`
- `pyproject.toml:21-22`, `uv.lock:122-124`

## Fix
0. Change the imports to `from anker_solix_api.api import AnkerSolixApi` and
   `from anker_solix_api.errors import AnkerSolixError`. Then `uv lock && uv sync` and
   confirm `aiofiles` lands in `.venv`; if uv still cannot read the lib's poetry
   dependencies, add `aiofiles` and `cryptography` explicitly to this project's
   `dependencies`.
1. In `anker_plugin.py` move the two `api.*` imports inside `_run_bridge` (and the
   `except (... AnkerSolixError ...)` tuple in `_run_thread` must then reference a
   name resolved lazily — simplest: import inside `_run_thread` too, before the loop,
   wrapped in `try/except ImportError` that sets `last_error = "anker-solix-api not installed"`
   and returns).
2. In `main.py`, import `AnkerPlugin` inside the `if cfg.anker.enabled:` block.
3. Replace the path source with a git source so a fresh clone works, e.g.
   ```toml
   [tool.uv.sources]
   anker-solix-api = { git = "https://github.com/thomluther/anker-solix-api", rev = "77a0f887a3b8c25680c91015a65c5a4525e62857" }
   ```
   (That is the commit currently checked out locally: "Smart Meter Gen 2 support",
   2026-09-10, remote `git@github.com:thomluther/anker-solix-api.git`.) Keep a
   commented-out path source for local development.
4. Run `uv lock` and commit the updated `uv.lock`.

## Acceptance criteria
- `.venv/bin/python -c "import energy_hub.main"` succeeds against the 3.8.2 checkout.
- With `anker.user` empty, `uv run energy-hub` starts on a machine without `anker-solix-api`.
- `uv sync` succeeds from a fresh clone.
- The `sys.modules` stub in `tests/conftest.py` (T01) can be removed.

## Owner answer (2026-09-10)
The deployed box still runs 3.5.x (package `api`); the owner may upgrade it. Therefore
the import fix in step 0 must work with **both** layouts until the upgrade is done:
```python
try:
    from anker_solix_api.api import AnkerSolixApi          # >= 3.8
    from anker_solix_api.errors import AnkerSolixError
except ImportError:
    from api.api import AnkerSolixApi                      # 3.5.x
    from api.errors import AnkerSolixError
```
(inside the lazy import of step 1). Log which layout was loaded at INFO. Once the
deployment is upgraded and step 3 pins the git rev, drop the fallback branch.
Upgrade procedure on the box: `cd /home/chriu/anker-solix-api && git pull`, then
`cd /home/chriu/energy-hub && uv lock && uv sync --frozen`, then restart the service
and check the log for "Anker cloud auth" and "Using meter".
