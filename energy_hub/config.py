"""Configuration loader — YAML file with environment variable overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


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
    max_total_energy: float = 150000.0
    max_power: float = 30000.0
    min_power: float = -30000.0
    grid_min_power: float = -5600.0
    grid_min_total_in: float = 42000.0
    grid_min_total_out: float = 28484.0
    pv_min_total_in: float = 60000.0
    pv_max_power: float = 5600.0
    max_timestamp_diff: int = 60
    stale_data_threshold: int = 120
    meter_noise_tolerance: float = 0.1
    quality_alert_threshold: float = 0.5
    quality_check_window: int = 20
    max_meter_rate_kwh_per_10s: float = 0.5
    max_power_jump: float = 15000.0
    recovery_consensus_count: int = 3
    value_history_size: int = 10
    max_consecutive_errors: int = 10


@dataclass
class StalePolicyConfig:
    grace_period: int = 300
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


def _merge(dc_class: type, raw: dict[str, Any] | None):
    """Create a dataclass instance from a raw dict, ignoring unknown keys."""
    if not raw:
        return dc_class()
    known = {f.name for f in dc_class.__dataclass_fields__.values()}
    return dc_class(**{k: v for k, v in raw.items() if k in known})


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

    mqtt = _merge(MqttConfig, raw.get("mqtt"))
    mqtt.password = str(_env_or("MQTT_PASSWORD", mqtt.password))
    mqtt.broker = str(_env_or("MQTT_BROKER", mqtt.broker))
    if port := _env("MQTT_PORT"):
        mqtt.port = int(port)

    anker = _merge(AnkerConfig, raw.get("anker"))
    anker.user = str(_env_or("ANKER_USER", anker.user))
    anker.password = str(_env_or("ANKER_PASSWORD", anker.password))
    anker.country = str(_env_or("ANKER_COUNTRY", anker.country))

    sources_raw = raw.get("sources") or {}
    sources = SourcesConfig(
        tasmota_pv=_merge(SourceConfig, sources_raw.get("tasmota_pv")),
        tasmota_grid=_merge(SourceConfig, sources_raw.get("tasmota_grid")),
        anker=_merge(SourceConfig, sources_raw.get("anker")),
        charger=_merge(SourceConfig, sources_raw.get("charger")),
    )

    output = _merge(OutputConfig, raw.get("output"))
    validation = _merge(ValidationConfig, raw.get("validation"))
    stale_policy = _merge(StalePolicyConfig, raw.get("stale_policy"))

    alerts = _merge(AlertConfig, raw.get("alerts"))
    if url := _env("ALERT_WEBHOOK"):
        alerts.webhook_url = url

    logging_cfg = _merge(LoggingConfig, raw.get("logging"))
    gas = _merge(GasConfig, raw.get("gas"))

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
