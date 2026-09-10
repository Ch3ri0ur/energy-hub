# T15: Anker plugin — shutdown traceback, millisecond timestamps, and unguarded message callback

**Priority:** P3 (robustness, low risk) | **Effort:** S | **Area:** anker plugin

## Problem
1. **Shutdown traceback.** `_watch_stop` (a daemon thread) calls
   `loop.call_soon_threadsafe(stop_async.set)` when `_stop` is set. If the worker was
   in the backoff `self._stop.wait(timeout=backoff)` it breaks out and calls
   `loop.close()` first; the watcher then raises `RuntimeError: Event loop is closed`,
   printing a traceback on every clean shutdown during a backoff period.
2. **`_iso_ts` overflow (defensive only).** `head.timestamp` is passed straight to
   `datetime.fromtimestamp`. Verified in anker-solix-api 3.8.2 (`mqtt.py:132-134`) that
   the library itself treats it as seconds, so today this is fine; a non-numeric or
   out-of-range value would still raise inside the callback (see item 3).
3. **Unguarded callback.** `_on_msg` calls `builder.update(...)` without try/except.
   Any exception (like item 2, or a malformed message) propagates into the library's
   `message_poller` task, ends it, and the whole bridge restarts with backoff —
   a single bad message costs 30–300 s of Anker data.

## Where
- `energy_hub/anker_plugin.py:415-419` — watcher thread
- `energy_hub/anker_plugin.py:459` — `loop.close()`
- `energy_hub/anker_plugin.py:69-70` — `_iso_ts`
- `energy_hub/anker_plugin.py:289-292` — `_on_msg`

## Fix
1. In `_watch_stop`: `if not loop.is_closed(): try: loop.call_soon_threadsafe(...) except RuntimeError: pass`.
2. `_iso_ts`: if `value > 1e11` divide by 1000; wrap in `try/except (ValueError, OverflowError, OSError)` returning `str(value)`.
3. `_on_msg`: wrap body in `try/except Exception: logger.exception("Anker message handling failed")`.

## Acceptance criteria
- `_iso_ts(1_700_000_000_000)` returns an ISO string for 2023-11-14, not an exception.
- A `builder.update` raising inside `_on_msg` is logged and does not propagate.
- Clean shutdown during backoff prints no traceback.

## Tests
`tests/test_anker_plugin.py`: the `_iso_ts` cases (seconds, milliseconds, garbage).
