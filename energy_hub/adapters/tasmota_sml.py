"""Tasmota SML sensor adapter — parses IR reader data from PV and grid meters."""

from __future__ import annotations

import json
import logging
from typing import Any

from .base import SensorReading

logger = logging.getLogger("energy_hub.adapters.tasmota")


def parse_tasmota_sml(payload: bytes | str, source: str = "") -> SensorReading | None:
    """Parse a Tasmota SENSOR payload with SML sub-object.

    Expected JSON structure::

        {"Time": "2026-04-12T14:30:00", "SML": {"Total_in": 65432.1, "Total_out": 0.0, "Power_curr": 1234.5}}

    Returns None if the payload cannot be parsed.
    """
    try:
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8")
        data: dict[str, Any] = json.loads(payload)
        sml = data["SML"]
        return SensorReading(
            power=float(sml["Power_curr"]),
            energy_import=float(sml["Total_in"]),
            energy_export=float(sml["Total_out"]),
            timestamp=data.get("Time", ""),
            source=source,
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("Failed to parse Tasmota SML payload (%s): %s", source, exc)
        return None
