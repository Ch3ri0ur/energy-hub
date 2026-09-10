# T23: Alert webhook blocks the main publish loop; no per-issue recovery notice

**Priority:** P3 | **Effort:** S | **Area:** health / alerts

## Problem
1. `AlertManager._webhook` performs a synchronous `urllib.request.urlopen(..., timeout=5)`
   on the main thread (`health.py:112`). While ntfy/webhook is slow or unreachable the
   10 s snapshot publish loop stalls for up to 5 s per alert (several alerts → several
   stalls in one tick).
2. Recovery is only announced when **all** issues are gone (`health.py:65`). If
   `pv_stale` and `grid_stale` are both active and only PV recovers, no message is sent.
3. `messages` dict (`health.py:38-44`) is rebuilt on every call — trivial, move to module level.

## Where
- `energy_hub/health.py:95-114`, `:46-77`

## Fix
1. Run the webhook in a short-lived `threading.Thread(daemon=True)` (or a single
   worker thread with a `queue.Queue`); log failures from that thread. Keep the 5 s timeout.
2. For each issue in `recovered`, publish `{"issue": issue, "severity": "info", "message": f"{messages[issue]} — recovered"}`
   and send the webhook. Keep the "All clear" message when `current` becomes empty.
3. Move `messages` to a module-level constant `ISSUE_MESSAGES`.

## Acceptance criteria
- A webhook URL pointing to a black-holed address does not delay `publish_snapshot` (measure loop iteration time < 100 ms).
- Issues `{a, b}` → `{b}` produces one recovery alert for `a` and no "All clear".

## Tests
`tests/test_health.py` with a recording `publish_cb` and `webhook_url=""`.
