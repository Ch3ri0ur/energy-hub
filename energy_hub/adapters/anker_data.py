"""Anker data adapter — parses the combined Anker snapshot from local MQTT."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

from .base import AnkerGridReading, SolarbankReading

logger = logging.getLogger("energy_hub.adapters.anker")


def _float(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _parse_iso_epoch(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _received_at(data: dict[str, Any], updated_key: str) -> float:
    return (
        _parse_iso_epoch(data.get(updated_key))
        or _parse_iso_epoch(data.get("timestamp"))
        or time.time()
    )


def parse_anker_snapshot(
    payload: bytes | str,
) -> tuple[AnkerGridReading | None, SolarbankReading | None]:
    """Parse the combined Anker MQTT snapshot.

    Returns (grid_reading, solarbank_reading).  Either may be None if the
    relevant fields are missing from the snapshot.

    Expected JSON keys (published by the Anker plugin to local MQTT)::

        grid_to_home_power, pv_to_grid_power, net_grid_power,
        grid_import_energy, grid_export_energy,
        photovoltaic_power, pv_{1..4}_power, battery_soc,
        battery_power_signed, battery_energy_wh, battery_capacity_wh,
        output_power, ac_output_power_signed, grid_to_battery_power,
        home_demand, temperature
    """
    try:
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8")
        data: dict[str, Any] = json.loads(payload)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("Failed to parse Anker snapshot: %s", exc)
        return None, None

    grid: AnkerGridReading | None = None
    if "net_grid_power" in data or "grid_to_home_power" in data:
        g2h = _float(data.get("grid_to_home_power"))
        p2g = _float(data.get("pv_to_grid_power"))
        grid = AnkerGridReading(
            grid_to_home_power=g2h,
            pv_to_grid_power=p2g,
            net_grid_power=_float(data.get("net_grid_power"), g2h - p2g),
            grid_import_energy=_float(data.get("grid_import_energy")),
            grid_export_energy=_float(data.get("grid_export_energy")),
            received_at=_received_at(data, "meter_updated_at"),
        )

    sb: SolarbankReading | None = None
    if "photovoltaic_power" in data or "output_power" in data or "battery_soc" in data:
        sb = SolarbankReading(
            photovoltaic_power=_float(data.get("photovoltaic_power")),
            pv_1_power=_float(data.get("pv_1_power")),
            pv_2_power=_float(data.get("pv_2_power")),
            pv_3_power=_float(data.get("pv_3_power")),
            pv_4_power=_float(data.get("pv_4_power")),
            battery_soc=_float(data.get("battery_soc")),
            battery_power_signed=_float(data.get("battery_power_signed")),
            battery_energy_wh=_float(data.get("battery_energy_wh")),
            battery_capacity_wh=_float(data.get("battery_capacity_wh")),
            output_power=_float(data.get("output_power")),
            ac_output_power_signed=_float(data.get("ac_output_power_signed")),
            grid_to_battery_power=_float(data.get("grid_to_battery_power")),
            home_demand=_float(data.get("home_demand")),
            temperature=_float(data.get("temperature")),
            received_at=_received_at(data, "solarbank_updated_at"),
        )

    return grid, sb
