"""Configuration loader — YAML file with environment variable overrides."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, get_type_hints

import yaml
from dotenv import load_dotenv

logger = logging.getLogger("energy_hub.config")


def _env(key: str) -> str | None:
    """Read an EH_-prefixed environment variable."""
    return os.environ.get(f"EH_{key}")


def _env_or(key: str, fallback: Any) -> Any:
    """Read env var, falling back to YAML value.  Empty string counts as unset."""
    val = _env(key)
    if val is not None and val != "":
        return val
    return fallback


# ---------------------------------------------------------------------------
# Dataclasses — one per config section
# ---------------------------------------------------------------------------


@dataclass
class MqttConfig:
    broker: str = "192.168.1.34"
    port: int = 1883
    username: str = "mqttu"
    password: str = ""
    keepalive: int = 60
    client_id: str = "energy-hub"


@dataclass
class AnkerConfig:
    user: str = ""
    password: str = ""
    country: str = "DE"
    meter_sn: str = ""
    solarbank_sn: str = ""
    trigger_timeout: int = 60
    meter_trigger_interval: int = 60
    solarbank_status_interval: int = 5

    @property
    def enabled(self) -> bool:
        return bool(self.user and self.password)


@dataclass
class SourceConfig:
    topic: str = ""
    stale_timeout: int = 120
    failback_after: int = 0   # s of continuous fresh data before switching back to this source (0 = at once)


@dataclass
class SourcesConfig:
    tasmota_pv: SourceConfig = field(default_factory=SourceConfig)
    tasmota_grid: SourceConfig = field(default_factory=SourceConfig)
    anker: SourceConfig = field(default_factory=lambda: SourceConfig(stale_timeout=300))
    charger: SourceConfig = field(default_factory=lambda: SourceConfig(stale_timeout=300))


@dataclass
class OutputConfig:
    prefix: str = "energy"
    publish_interval: int = 10
    health_interval: int = 60
    retain: bool = True


@dataclass
class ValidationConfig:
    """Limits for Tasmota SML plausibility checks (see energy_hub/validation.py)."""

    max_total_energy: float = 150000.0   # kWh, any cumulative counter
    max_power: float = 30000.0           # W, grid import ceiling
    grid_min_power: float = -5600.0      # W, grid export floor (negative)
    pv_max_power: float = 5600.0         # W, PV generation ceiling
    grid_min_total_in: float = 42000.0   # kWh, first-reading floor
    grid_min_total_out: float = 28484.0  # kWh, first-reading floor
    pv_min_total_in: float = 60000.0     # kWh, first-reading floor
    meter_noise_tolerance: float = 0.1   # kWh, allowed backwards jitter / rate slack
    rate_safety_factor: float = 2.0      # multiplier on the physical max energy step
    power_jump_confirm: float = 3000.0   # W, larger jumps need a confirming 2nd reading (0 = off)
    max_consecutive_errors: int = 10     # progression failures before forced re-baselining
    quality_check_window: int = 20       # readings in the rolling quality window
    quality_alert_threshold: float = 0.5 # alert when rejected fraction exceeds this
    clock_offset_warn: int = 60          # s, device vs host clock offset to warn about
    max_alignment_skew: float = 30.0     # s, older reference times disable alignment


@dataclass
class StalePolicyConfig:
    # Seconds since the last reading after which a source's power contribution is
    # zeroed ("dead").  Between the source's stale_timeout and this age the last
    # value is still used, flagged stale.  Never smaller than the stale_timeout.
    zero_after: int = 300


@dataclass
class AlertConfig:
    enabled: bool = True
    webhook_url: str = ""
    webhook_title: str = "Energy Hub"
    backoff_stages: list[int] = field(
        default_factory=lambda: [3600, 43200, 172800, 604800]
    )


@dataclass
class LoggingConfig:
    file: str = "energy_hub.log"
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 3
    level: str = "INFO"


@dataclass
class GasConfig:
    impulse_topic: str = "zigbee2mqtt/GasImpulsCounter"
    sync_topic: str = "energy/gas/set"
    publish_topic: str = "energy/gas"
    state_file: str = "gas_state.json"
    impulse_m3: float = 0.01
    retain: bool = True


@dataclass
class AppConfig:
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    anker: AnkerConfig = field(default_factory=AnkerConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    stale_policy: StalePolicyConfig = field(default_factory=StalePolicyConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    gas: GasConfig = field(default_factory=GasConfig)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _coerce(value: Any, target: type, name: str) -> Any:
    """Cast a YAML/env value to the dataclass field type, raising a clear error."""
    if target is bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError(f"config: {name} must be a boolean, got {value!r}")
    if target in (int, float):
        if isinstance(value, bool):
            raise ValueError(f"config: {name} must be a number, got {value!r}")
        try:
            return target(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"config: {name} must be a {target.__name__}, got {value!r}") from exc
    if target is str:
        return "" if value is None else str(value)
    return value


def _merge(dc_class: type, raw: dict[str, Any] | None, section: str = ""):
    """Create a dataclass instance from a raw dict.

    Unknown keys are ignored but logged (a typo in config.yaml would otherwise
    silently fall back to the default); scalar values are coerced to the
    declared field type so a quoted number or "false" string fails at load time
    instead of deep inside the engine.
    """
    if not raw:
        return dc_class()
    if not isinstance(raw, dict):
        raise ValueError(f"config: section '{section or dc_class.__name__}' must be a mapping")
    hints = get_type_hints(dc_class)
    known = {f.name for f in fields(dc_class)}
    unknown = sorted(set(raw) - known)
    if unknown:
        logger.warning("config: unknown keys in '%s' ignored: %s", section or dc_class.__name__, ", ".join(unknown))
    values = {}
    for k, v in raw.items():
        if k not in known:
            continue
        target = hints.get(k)
        values[k] = _coerce(v, target, f"{section}.{k}" if section else k) if target in (bool, int, float, str) else v
    return dc_class(**values)


def load_config(path: str | Path = "config.yaml", env_file: str | Path | None = ".env") -> AppConfig:
    """Load configuration from YAML, then apply EH_ environment overrides.

    If *env_file* exists it is loaded first (without overriding vars already
    set in the real environment) so the caller no longer needs to
    ``source .env`` in the shell.
    """
    if env_file:
        env_path = Path(env_file)
        if env_path.is_file():
            load_dotenv(env_path, override=False)

    path = Path(path)
    raw: dict[str, Any] = {}
    if path.exists():
        with open(path) as f:
            raw = yaml.safe_load(f) or {}

    top_unknown = sorted(set(raw) - set(AppConfig.__dataclass_fields__))
    if top_unknown:
        logger.warning("config: unknown top-level sections ignored: %s", ", ".join(top_unknown))

    mqtt = _merge(MqttConfig, raw.get("mqtt"), "mqtt")
    mqtt.password = str(_env_or("MQTT_PASSWORD", mqtt.password))
    mqtt.broker = str(_env_or("MQTT_BROKER", mqtt.broker))
    if port := _env("MQTT_PORT"):
        mqtt.port = int(port)

    anker = _merge(AnkerConfig, raw.get("anker"), "anker")
    anker.user = str(_env_or("ANKER_USER", anker.user))
    anker.password = str(_env_or("ANKER_PASSWORD", anker.password))
    anker.country = str(_env_or("ANKER_COUNTRY", anker.country))

    sources_raw = raw.get("sources") or {}
    unknown_sources = sorted(set(sources_raw) - set(SourcesConfig.__dataclass_fields__))
    if unknown_sources:
        logger.warning("config: unknown keys in 'sources' ignored: %s", ", ".join(unknown_sources))
    sources = SourcesConfig(
        tasmota_pv=_merge(SourceConfig, sources_raw.get("tasmota_pv"), "sources.tasmota_pv"),
        tasmota_grid=_merge(SourceConfig, sources_raw.get("tasmota_grid"), "sources.tasmota_grid"),
        anker=_merge(SourceConfig, sources_raw.get("anker"), "sources.anker"),
        charger=_merge(SourceConfig, sources_raw.get("charger"), "sources.charger"),
    )

    output = _merge(OutputConfig, raw.get("output"), "output")
    validation = _merge(ValidationConfig, raw.get("validation"), "validation")
    stale_policy = _merge(StalePolicyConfig, raw.get("stale_policy"), "stale_policy")

    alerts = _merge(AlertConfig, raw.get("alerts"), "alerts")
    if url := _env("ALERT_WEBHOOK"):
        alerts.webhook_url = url
    if not alerts.backoff_stages:
        logger.warning("config: alerts.backoff_stages is empty, using defaults")
        alerts.backoff_stages = AlertConfig().backoff_stages
    alerts.backoff_stages = [int(x) for x in alerts.backoff_stages]

    logging_cfg = _merge(LoggingConfig, raw.get("logging"), "logging")
    gas = _merge(GasConfig, raw.get("gas"), "gas")

    return AppConfig(
        mqtt=mqtt,
        anker=anker,
        sources=sources,
        output=output,
        validation=validation,
        stale_policy=stale_policy,
        alerts=alerts,
        logging=logging_cfg,
        gas=gas,
    )
