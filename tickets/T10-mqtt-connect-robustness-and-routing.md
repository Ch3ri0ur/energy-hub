# T10: Hub crashes at startup if the broker is unreachable; message routing cannot use wildcards

**Priority:** P2 (robustness) | **Effort:** S | **Area:** main / MQTT

## Problem
1. `client.connect(...)` (blocking) is called before `loop_start()`. If mosquitto is
   down or DNS fails at boot, it raises `OSError`/`ConnectionRefusedError`, the
   process exits with a traceback and systemd restarts it every 10 s
   (`Restart=on-failure`). paho can handle this itself with `connect_async` +
   automatic reconnect.
2. Routing is a dict keyed by the exact topic (`routes.get(msg.topic)`), so a
   wildcard source topic such as `tele/+/SENSOR` would be subscribed but never routed.
   paho has `message_callback_add(sub, cb)` with proper wildcard matching.
3. There is no `on_connect_fail` handler, so repeated connection failures are silent.

## Where
- `energy_hub/main.py:155-156` — `client.connect(...)`; `client.loop_start()`
- `energy_hub/main.py:142-145` — `on_message` exact match
- `energy_hub/main.py:60-99` — `_build_router`

## Fix
1. Replace connect:
   ```python
   client.reconnect_delay_set(min_delay=1, max_delay=60)
   client.connect_async(cfg.mqtt.broker, cfg.mqtt.port, cfg.mqtt.keepalive)
   client.loop_start()
   ```
   Add `client.on_connect_fail = lambda c, u: logger.warning("MQTT connect failed, retrying")`.
2. Register routes with `client.message_callback_add(topic, cb)` for each entry in
   `routes`; keep `on_message` only as a DEBUG log for unmatched topics.
3. Keep subscriptions in `on_connect` (already re-subscribes after reconnect).

## Acceptance criteria
- Starting the hub with an unreachable broker logs a warning and keeps running; when the broker comes up it connects and subscribes (verify manually with mosquitto stopped/started).
- A source topic configured as `tele/+/SENSOR` routes messages from `tele/tasmota_pv/SENSOR`.
- Existing exact-topic behaviour unchanged.

## Tests
`tests/test_main_router.py`: call `_build_router` with a config and check the returned dict;
for wildcard matching use `paho.mqtt.client.topic_matches_sub`.
