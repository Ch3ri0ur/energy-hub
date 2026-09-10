# T17: Config loader silently ignores typos; unused keys; unclear stale-policy semantics; empty backoff crashes

**Priority:** P3 (quality) | **Effort:** S | **Area:** config

## Problem
1. `_merge` drops unknown keys without a log line (`config.py:154-159`). A typo such
   as `stale_timout: 60` is silently ignored and the default applies.
2. (done 2026-09-10) unused validation keys were removed with the validator overhaul.
3. `stale_policy`: `_stale_grace_deadline` (`engine.py:121-128`) computes
   `max(stale_timeout, min(zero_after, stale_timeout + grace_period))`. With the shipped
   values (stale 120, grace 300, zero_after 300) `grace_period` has no effect — the
   comments in `config.yaml:78-80` describe two independent knobs that are not.
4. `alerts.backoff_stages: []` raises `IndexError` in `AlertManager._should_send`
   (`health.py:83`).
5. `ValidationConfig` numeric fields are not coerced; YAML `max_power: "30000"` (string)
   would raise a `TypeError` deep in `energy_hub/validation.py` instead of at load time.

## Fix
1. In `_merge`, compute `unknown = set(raw) - known` and `logger.warning("config: unknown keys in %s: %s", dc_class.__name__, sorted(unknown))` (add a module logger).
2. (done)
3. Simplify to one semantic: `zero_after` is the absolute age (seconds since last
   reading) after which power contribution is zeroed; `grace_period` is removed **or**
   redefined as "seconds *after* stale_timeout" with `zero_after` removed. Pick the
   first (keep `zero_after`), delete `grace_period`, update `_stale_grace_deadline` to
   `max(stale_timeout, zero_after)` and fix the YAML comments.
4. In `load_config`, if `alerts.backoff_stages` is empty, set it to the default list and warn.
5. Add a small `_coerce(dc_instance)` that casts fields to their annotated `int`/`float`/`bool`
   types (bool: accept `true/false/yes/no/1/0` strings), called for every section in `load_config`.

## Acceptance criteria
- Loading a YAML with `sources: {tasmota_pv: {stale_timout: 5}}` logs a warning naming `stale_timout`.
- `_stale_grace_deadline(120)` with `zero_after=300` returns 300; with `zero_after=60` returns 120.
- `backoff_stages: []` does not crash.

## Tests
`tests/test_config.py`: write YAML to `tmp_path`, load it, assert warnings via `caplog`; coerce case `max_power: "1000"` → `1000.0`.
