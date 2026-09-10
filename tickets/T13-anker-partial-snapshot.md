# T13: Anker bridge publishes nothing unless BOTH meter and solarbank have data

**Priority:** P2 (availability of grid failover) | **Effort:** XS | **Area:** anker plugin

## Problem
`_SnapshotBuilder._build` returns `None` when either the meter or the solarbank value
dict is empty. If the Solarbank is off (winter storage, powered down, firmware update)
or simply has not sent a message yet, the A17X7 smart-meter data is withheld too — so
the grid failover source is unavailable exactly when it might be needed. The consumer
side (`parse_anker_snapshot`) already handles a snapshot with only meter or only
solarbank keys.

## Where
- `energy_hub/anker_plugin.py:190-194` — `if not meter or not sb: return None`
- `energy_hub/adapters/anker_data.py:73,86` — consumer handles either half

## Fix
Change the guard to `if not meter and not sb: return None`. `_prune_empty` already
removes keys whose value is `None`, so a meter-only snapshot contains no solarbank
keys and `parse_anker_snapshot` returns `(grid, None)`.
Add a one-line comment explaining that partial snapshots are intentional.

## Acceptance criteria
- `builder.update(meter_sn, msg, {"grid_to_home_power": 100, "pv_to_grid_power": 0})` with no solarbank data returns a snapshot containing `net_grid_power: 100` and no `battery_soc` key.
- Solarbank-only updates likewise produce a snapshot.

## Tests
`tests/test_anker_plugin.py`: construct `_SnapshotBuilder({"device_sn": "M1"}, {"device_sn": "S1"})` and assert the above. (Requires the import stub from T01 or the lazy import from T14.)
