import logging

import pytest
import yaml

from energy_hub.config import AppConfig, load_config


def write(tmp_path, data):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def test_unknown_keys_warn(tmp_path, caplog):
    p = write(tmp_path, {"sources": {"tasmota_pv": {"stale_timout": 5}}, "bogus": {}, "mqtt": {"prot": 1}})
    with caplog.at_level(logging.WARNING):
        cfg = load_config(p, env_file=None)
    assert "stale_timout" in caplog.text and "bogus" in caplog.text and "prot" in caplog.text
    assert cfg.sources.tasmota_pv.stale_timeout == 120


def test_coercion(tmp_path):
    p = write(tmp_path, {"validation": {"max_power": "1000"}, "output": {"retain": "false", "publish_interval": "5"}})
    cfg = load_config(p, env_file=None)
    assert cfg.validation.max_power == 1000.0 and cfg.output.retain is False and cfg.output.publish_interval == 5


def test_bad_values_fail_loudly(tmp_path):
    with pytest.raises(ValueError, match="output.retain"):
        load_config(write(tmp_path, {"output": {"retain": "maybe"}}), env_file=None)
    with pytest.raises(ValueError, match="validation.max_power"):
        load_config(write(tmp_path, {"validation": {"max_power": "lots"}}), env_file=None)


def test_empty_backoff_uses_defaults(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        cfg = load_config(write(tmp_path, {"alerts": {"backoff_stages": []}}), env_file=None)
    assert cfg.alerts.backoff_stages == AppConfig().alerts.backoff_stages


def test_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("EH_MQTT_PASSWORD", "secret")
    monkeypatch.setenv("EH_MQTT_PORT", "1884")
    monkeypatch.setenv("EH_ANKER_USER", "u@x")
    monkeypatch.setenv("EH_ANKER_PASSWORD", "p")
    cfg = load_config(write(tmp_path, {"mqtt": {"password": "yaml"}}), env_file=None)
    assert cfg.mqtt.password == "secret" and cfg.mqtt.port == 1884 and cfg.anker.enabled


def test_missing_file_gives_defaults(tmp_path):
    cfg = load_config(tmp_path / "nope.yaml", env_file=None)
    assert cfg.output.prefix == "energy"


def test_repo_config_loads_clean(caplog):
    with caplog.at_level(logging.WARNING):
        cfg = load_config("config.yaml", env_file=None)
    assert "unknown" not in caplog.text
    assert cfg.sources.tasmota_grid.failback_after == 60
