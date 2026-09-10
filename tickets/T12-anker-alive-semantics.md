# T12: `anker_plugin_alive` is true while the plugin is still authenticating/reconnecting

**Priority:** P2 (health accuracy) | **Effort:** S | **Area:** anker plugin, health

## Problem
`_run_thread` sets `self._alive = True` **before** `_run_bridge` starts (i.e. before
cloud auth, device lookup or the MQTT session succeed). It is only set to false during
the backoff sleep. So during a broken-credentials loop the health flips
alive→down→alive every retry, and the `anker_plugin_down` alert only fires if the
health tick happens to land in a backoff window. `last_error` and `retry_count`
exist but are never exposed.

## Where
- `energy_hub/anker_plugin.py:425` — `self._alive = True`
- `energy_hub/anker_plugin.py:294-296` — MQTT session connected (the real "alive" point)
- `energy_hub/anker_plugin.py:384-393` — `is_alive`, `last_error`, `retry_count`
- `energy_hub/main.py:196-200` — passes only `anker_alive` bool
- `energy_hub/health.py:130-162` — `build_health`

## Fix
1. Pass an `on_state(alive: bool)` callback into `_run_bridge` (or give `_run_bridge`
   the plugin instance). Call `on_state(True)` right after the
   `mqtt_session.is_connected()` check passes, and `on_state(False)` in the `finally`.
   Remove the `self._alive = True` at line 425.
2. Add `AnkerPlugin.status() -> dict` returning
   `{"alive": ..., "last_error": ..., "retries": ..., "last_snapshot_at": <epoch|None>}`
   (set `last_snapshot_at` in `_publish_snapshot`).
3. `HealthMonitor.build_health(source_statuses, anker_status: dict | None, connected)`:
   `anker_alive = anker_status is None or anker_status["alive"]`; include the dict as
   `health["anker_plugin"]` and keep `anker_plugin_alive` for Telegraf compatibility.
4. Alert message for `anker_plugin_down` should include `last_error`.

## Acceptance criteria
- With wrong credentials, health reports `anker_plugin_alive: false` continuously and one `anker_plugin_down` alert (with backoff) is sent.
- When the Anker MQTT session is connected, `alive` is true.

## Tests
`tests/test_health.py`: `build_health` with `anker_status={"alive": False, "last_error": "x"}` → issues contains `anker_plugin_down`; with `None` → no issue.
