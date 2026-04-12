"""go-eCharger adapter — extracts total charging power from the nrg array."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("energy_hub.adapters.charger")


def parse_charger_nrg(payload: bytes | str) -> float | None:
    """Parse the go-eCharger ``nrg`` array and return total power in watts.

    The ``nrg`` topic publishes a JSON array of 16 values.  Index 11
    contains the total power in watts.  Returns None on parse failure.
    """
    try:
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8")
        data: list[Any] = json.loads(payload)
        if not isinstance(data, list) or len(data) < 12:
            logger.warning("Charger nrg payload too short: %d elements", len(data) if isinstance(data, list) else 0)
            return None
        return float(data[11])
    except (json.JSONDecodeError, TypeError, ValueError, IndexError) as exc:
        logger.warning("Failed to parse charger nrg payload: %s", exc)
        return None
