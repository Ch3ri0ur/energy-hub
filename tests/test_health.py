import time

from energy_hub.config import AlertConfig
from energy_hub.health import HealthMonitor


def statuses(tasmota="ok", anker="ok", pv="ok", quality=1.0):
    return {
        "pv_roof": {"status": pv, "quality": quality},
        "grid_tasmota": {"status": tasmota, "quality": quality},
        "grid_anker": {"status": anker},
    }


def mon(**kw):
    return HealthMonitor(AlertConfig(webhook_url="", **kw), start_time=time.time())


def test_no_issues_when_all_ok():
    assert mon().build_health(statuses(), True, True)["issues"] == []


def test_tasmota_stale_with_anker_covering_is_info_issue():
    h = mon().build_health(statuses(tasmota="stale", anker="ok"), True, True)
    assert h["issues"] == ["grid_tasmota_stale"]


def test_grid_stale_when_nothing_delivers():
    assert mon().build_health(statuses(tasmota="dead", anker="stale"), True, True)["issues"] == ["grid_stale"]
    assert mon().build_health(statuses(tasmota="stale", anker="no_data"), True, True)["issues"] == ["grid_stale"]


def test_no_grid_issue_when_grid_never_configured():
    assert mon().build_health(statuses(tasmota="no_data", anker="no_data"), True, True)["issues"] == []


def test_anker_down_issue_with_details():
    h = mon().build_health(statuses(), {"alive": False, "last_error": "AuthError: bad password"}, True)
    assert "anker_plugin_down" in h["issues"] and h["anker_plugin"]["last_error"].startswith("AuthError")
    assert mon().build_health(statuses(), None, True)["issues"] == []          # plugin disabled
    assert mon().build_health(statuses(), {"alive": True}, True)["issues"] == []


def test_anker_alert_message_includes_last_error():
    m = mon()
    out = []
    m.process({"issues": ["anker_plugin_down"], "anker_plugin": {"last_error": "AuthError"}}, out.append)
    assert "AuthError" in out[0]["message"]


def test_info_issues_skip_webhook_and_alert_payload_has_severity(monkeypatch):
    m = mon()
    calls = []
    monkeypatch.setattr(m._alerts, "_webhook", lambda *a: calls.append(a))
    out = []
    m.process({"issues": ["grid_tasmota_stale"]}, out.append)
    assert out[0]["severity"] == "info" and calls == []
    m.process({"issues": ["grid_stale"]}, out.append)
    assert out[1]["severity"] == "warning" and len(calls) == 1


def test_backoff_survives_recovery(monkeypatch):
    m = mon()
    out = []
    m.process({"issues": ["pv_stale"]}, out.append)
    m.process({"issues": []}, out.append)              # recovered
    m.process({"issues": ["pv_stale"]}, out.append)    # back again within the backoff window
    sent = [a["issue"] for a in out]
    # warning, per-issue recovery, all-clear — and NO second warning (backoff kept)
    assert sent == ["pv_stale", "pv_stale", "recovered"]
    assert out[1]["severity"] == "info" and "recovered" in out[1]["message"]


def test_per_issue_recovery_messages():
    m = mon()
    out = []
    m.process({"issues": ["pv_stale", "grid_stale"]}, out.append)
    m.process({"issues": ["grid_stale"]}, out.append)
    assert [a["issue"] for a in out][-1] == "pv_stale" and out[-1]["severity"] == "info"
