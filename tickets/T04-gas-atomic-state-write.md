# T04: Gas state file is written non-atomically (crash → counter resets to 0)

**Priority:** P1 (bug, silent data loss) | **Effort:** XS | **Area:** gas tracker, housekeeping

## Problem
`_save_state` calls `Path.write_text` directly on `gas_state.json`. If the process is
killed or power fails mid-write, the file is truncated/partial. On the next start
`_load_state` fails to parse it, logs a warning and **starts at 0** — the whole gas
meter reading is lost until someone manually re-syncs.

Also `gas_state.json` is not in `.gitignore`, so it can be committed by accident.

## Where
- `energy_hub/gas_tracker.py:149-155` — `_save_state`
- `energy_hub/gas_tracker.py:138-147` — `_load_state` falls back to 0
- `.gitignore` — no entry for `gas_state.json`

## Fix
1. Write atomically:
   ```python
   tmp = self._state_path.with_suffix(".json.tmp")
   tmp.write_text(json.dumps({"impulse_count": self._impulse_count, "updated": int(time.time())}, indent=2))
   os.replace(tmp, self._state_path)
   ```
   (`import os` at the top.)
2. In `_load_state`, if the file exists but is unparsable, log at **ERROR** (not warning)
   and include the hint "publish {\"volume_m3\": <reading>} to energy/gas/set to re-sync".
3. Add `gas_state.json` and `gas_state.json.tmp` to `.gitignore`.

## Acceptance criteria
- After `_save_state`, no `.tmp` file remains and the JSON parses.
- A pre-existing corrupt state file logs an ERROR containing "re-sync".
- `git status` ignores `gas_state.json`.

## Tests
`tests/test_gas_tracker.py`: save then load round-trips the count via `tmp_path`;
a corrupt file yields count 0 and an ERROR log record (use `caplog`).
