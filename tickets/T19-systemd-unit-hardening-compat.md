# T19: systemd unit — `uv run` under `ProtectSystem=strict` may fail; duplicate log output; no lockfile pin

**Priority:** P3 (ops) | **Effort:** XS | **Area:** deployment

## Problem
1. `ExecStart` runs `uv run --project ... energy-hub`. With `ProtectSystem=strict` and
   only `/home/chriu/energy-hub` writable, uv's cache (`~/.cache/uv`) is read-only.
   If the lockfile or venv is out of date at boot, uv tries to resolve/sync, needs the
   network and cache, and fails — and `Restart=on-failure` loops. Without
   `--frozen`, uv may also re-resolve when `pyproject.toml` changed.
2. Both a stderr `StreamHandler` and a `RotatingFileHandler` are installed
   (`main.py:41-52`); under systemd every line goes to the journal **and** to
   `energy_hub.log`.
3. `Restart=on-failure` does not restart after a clean exit caused by an unexpected
   condition (e.g. an uncaught signal path); `Restart=always` is the usual choice for a
   daemon that should never be down.
4. The sibling checkout `/home/chriu/anker-solix-api` (editable dependency) must exist
   on the host; nothing documents that (see T14).

## Where
- `energy-hub.service:10` — ExecStart
- `energy-hub.service:11-12, 18-21`
- `energy_hub/main.py:32-52`

## Fix
1. `ExecStart=/home/chriu/.local/bin/uv run --frozen --no-sync --project ... energy-hub -c ...`
   and document that deploy runs `uv sync --frozen` once. Optionally
   `Environment=UV_CACHE_DIR=/home/chriu/energy-hub/.uv-cache` (inside `ReadWritePaths`)
   and add `.uv-cache/` to `.gitignore`.
2. In `_setup_logging`, only add the console handler when `sys.stderr.isatty()` **or**
   no file handler is configured; add `logging.console: true|false` to `LoggingConfig`
   (default true) so the deployed `config.yaml` can turn it off.
3. `Restart=always`, keep `RestartSec=10`.
4. Add a `# Requires: /home/chriu/anker-solix-api checkout (see pyproject)` comment, or
   remove once T14 pins a git source.

## Acceptance criteria
- `systemd-analyze verify energy-hub.service` clean.
- Running under systemd produces journal lines only when `logging.console: false`.
