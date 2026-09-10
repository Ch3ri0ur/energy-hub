# T11: Retained `energy/health` keeps saying "ok" after the hub dies — add MQTT Last Will

**Priority:** P2 (monitoring correctness) | **Effort:** XS | **Area:** publisher / main

## Problem
`publish_health` publishes `energy/health` **retained** with `status: "ok"` and
`connected: true`. If the process crashes or the host loses power, the broker keeps
serving that retained payload forever, so Telegraf/Grafana and anyone subscribed see a
healthy hub. Also `energy/grid`, `energy/pv/*` etc. keep their last retained values,
which is acceptable because they carry `stale` flags — but health itself has no way to
go bad.

## Where
- `energy_hub/publisher.py:54-55` — `publish_health` (retained)
- `energy_hub/main.py:117-124` — client construction (place for `will_set`)
- `energy_hub/main.py:206-212` — shutdown path

## Fix
1. Before connecting, in `_run`:
   ```python
   offline = json.dumps({"status": "offline", "connected": False, "issues": ["hub_offline"], "timestamp": None})
   client.will_set(f"{cfg.output.prefix}/health", offline, qos=0, retain=True)
   ```
2. On clean shutdown (the `finally` block), publish the same offline payload (with the
   current timestamp) retained before `client.disconnect()`, and `wait_for_publish()` on it (1 s timeout).
3. Note in `docs/telegraf-energy.conf` that the `status` tag can be `offline`.

## Acceptance criteria
- `kill -9` the hub → `mosquitto_sub -t energy/health -C 1` returns the offline payload.
- Clean `systemctl stop` → same.
- Normal running → health published as before.

## Tests
Unit-test that `will_set` is called with the health topic (use a `FakeClient` that records
`will_set` args) — extract the will construction into a small helper if needed.
