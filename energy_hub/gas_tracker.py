"""Gas impulse counter — subscribes to a Zigbee2MQTT door/contact sensor
that is physically attached to a BK4 gas meter (0.01 m³/impulse).

Responsibilities:
  - Count rising-edge contact events (False → True) as gas impulses.
  - Persist accumulated impulse count to a JSON file (survives restarts).
  - Accept a sync message on a separate MQTT topic to reset the counter to a
    known meter reading (e.g. after an outage or first setup).
  - Publish current volume + battery info to a single MQTT topic.

Topics consumed:
  impulse_topic  (default: zigbee2mqtt/GasImpulsCounter)
      Zigbee2MQTT contact sensor payload, e.g.
      {"contact": false, "battery": 100, "battery_low": false, ...}

  sync_topic     (default: energy/gas/set)
      Plain JSON with the current meter reading, e.g.
      {"volume_m3": 12345.67}
      Publishing here overwrites the accumulated counter.

Topic published:
  publish_topic  (default: energy/gas)
      {"volume_m3": 12345.67, "impulse_count": 1234567,
       "battery": 100, "battery_low": false}
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

from .config import GasConfig

logger = logging.getLogger("energy_hub.gas_tracker")

_STATE_KEYS = ("impulse_count",)


class GasTracker:
    """Thread-safe (single MQTT-thread) gas impulse accumulator."""

    def __init__(self, client: mqtt.Client, cfg: GasConfig) -> None:
        self._client = client
        self._cfg = cfg
        self._state_path = Path(cfg.state_file)

        # Load persisted counter or start fresh.
        self._impulse_count: int = self._load_state()
        # Track previous contact value to detect rising edges only.
        self._contact_prev: bool | None = None
        # Last known battery info (updated on every impulse message).
        self._battery: int | None = None
        self._battery_low: bool | None = None

        logger.info(
            "GasTracker ready: %.3f m³ (%d impulses) | impulse=%.4f m³",
            self._impulse_count * cfg.impulse_m3,
            self._impulse_count,
            cfg.impulse_m3,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def topics(self) -> list[str]:
        return [self._cfg.impulse_topic, self._cfg.sync_topic]

    def on_message(self, _client: Any, _userdata: Any, msg: mqtt.MQTTMessage) -> None:
        if msg.topic == self._cfg.impulse_topic:
            self._handle_impulse(msg.payload)
        elif msg.topic == self._cfg.sync_topic:
            self._handle_sync(msg.payload)

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    def _handle_impulse(self, raw: bytes) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("GasTracker: bad JSON on impulse topic")
            return

        contact: bool | None = data.get("contact")
        if contact is None:
            return

        # Update battery info whenever we get a message.
        if "battery" in data:
            self._battery = data["battery"]
        if "battery_low" in data:
            self._battery_low = data["battery_low"]

        # Only count on rising edge (False → True).
        if contact is True and self._contact_prev is not True:
            self._impulse_count += 1
            self._save_state()
            logger.info(
                "Gas impulse #%d → %.3f m³",
                self._impulse_count,
                self._impulse_count * self._cfg.impulse_m3,
            )
            self._publish()

        self._contact_prev = contact

    def _handle_sync(self, raw: bytes) -> None:
        try:
            data = json.loads(raw)
            volume_m3 = float(data["m3"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            logger.warning("GasTracker: sync message must be {\"m3\": <float>}")
            return

        new_count = round(volume_m3 / self._cfg.impulse_m3)
        logger.info(
            "GasTracker sync: %.3f m³ → impulse_count set to %d (was %d)",
            volume_m3,
            new_count,
            self._impulse_count,
        )
        self._impulse_count = new_count
        self._save_state()
        self._publish()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> int:
        if self._state_path.exists():
            try:
                state = json.loads(self._state_path.read_text())
                count = int(state["impulse_count"])
                logger.debug("GasTracker: loaded impulse_count=%d from %s", count, self._state_path)
                return count
            except Exception as exc:
                logger.warning("GasTracker: could not read state file (%s), starting at 0", exc)
        return 0

    def _save_state(self) -> None:
        try:
            self._state_path.write_text(
                json.dumps({"impulse_count": self._impulse_count}, indent=2)
            )
        except Exception as exc:
            logger.error("GasTracker: failed to save state: %s", exc)

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def _publish(self) -> None:
        payload: dict[str, Any] = {
            "volume_m3": round(self._impulse_count * self._cfg.impulse_m3, 3),
            "impulse_count": self._impulse_count,
        }
        if self._battery is not None:
            payload["battery"] = self._battery
        if self._battery_low is not None:
            payload["battery_low"] = self._battery_low
        payload["ts"] = int(time.time())

        try:
            self._client.publish(
                self._cfg.publish_topic,
                json.dumps(payload, separators=(",", ":")),
                qos=0,
                retain=self._cfg.retain,
            )
            logger.debug("GasTracker published: %s", payload)
        except Exception:
            logger.exception("GasTracker: publish failed")
