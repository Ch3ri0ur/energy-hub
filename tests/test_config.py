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
    with pytest.raises(ValueError, match=r"output\.retain"):
        load_config(write(tmp_path, {"output": {"retain": "maybe"}}), env_file=None)
    with pytest.raises(ValueError, match=r"validation\.max_power"):
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


def test_more_env_overrides_and_port_coercion(tmp_path, monkeypatch):
    monkeypatch.setenv("EH_MQTT_USERNAME", "alice")
    monkeypatch.setenv("EH_LOG_LEVEL", "debug")
    monkeypatch.setenv("EH_MQTT_PORT", "1884")
    cfg = load_config(tmp_path / "none.yaml", env_file=None)
    assert cfg.mqtt.username == "alice" and cfg.logging.level == "debug" and cfg.mqtt.port == 1884
    monkeypatch.setenv("EH_MQTT_PORT", "abc")
    with pytest.raises(ValueError, match="EH_MQTT_PORT"):
        load_config(tmp_path / "none.yaml", env_file=None)


def test_gas_topics_follow_output_prefix(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("output:\n  prefix: home/\n")
    cfg = load_config(p, env_file=None)
    assert cfg.gas.publish_topic == "home/gas" and cfg.gas.sync_topic == "home/gas/set"
    p.write_text("output:\n  prefix: home\ngas:\n  publish_topic: other/gas\n")
    cfg = load_config(p, env_file=None)
    assert cfg.gas.publish_topic == "other/gas" and cfg.gas.sync_topic == "home/gas/set"


def test_invalid_yaml_is_a_config_error(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("mqtt: [unclosed\n")
    with pytest.raises(ValueError, match="not valid YAML"):
        load_config(p, env_file=None)
    p.write_text("- just\n- a list\n")
    with pytest.raises(ValueError, match="mapping"):
        load_config(p, env_file=None)


def test_main_exits_78_on_config_error(tmp_path, monkeypatch, capsys):
    from energy_hub.main import main

    p = tmp_path / "c.yaml"
    p.write_text("mqtt:\n  port: notaport\n")
    monkeypatch.setattr("sys.argv", ["energy-hub", "-c", str(p), "-e", ""])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 78
    assert "mqtt.port" in capsys.readouterr().err
