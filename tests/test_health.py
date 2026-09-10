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
    kw.setdefault("webhook_url", "")
    return HealthMonitor(AlertConfig(**kw), start_time=time.time())


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


def test_webhook_does_not_block(monkeypatch):
    import threading
    import urllib.request

    started = threading.Event()
    release = threading.Event()

    class Slow:
        def read(self):
            started.set()
            release.wait(5)
            return b""

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Slow())
    m = mon(webhook_url="http://example.invalid/hook")
    t0 = time.time()
    m.process({"issues": ["grid_stale"]}, lambda p: None)
    assert time.time() - t0 < 1.0
    assert started.wait(2)
    release.set()


# --- webhook "All clear" and stage reset --------------------------------------

def webhook_calls(monkeypatch):
    calls = []
    import energy_hub.health as health

    monkeypatch.setattr(health.AlertManager, "_webhook", lambda self, msg, sev, issue, ts: calls.append(issue))
    return calls


def test_all_clear_not_pushed_after_info_only_issue(monkeypatch):
    calls = webhook_calls(monkeypatch)
    m = mon(webhook_url="http://example.invalid/hook")
    m.process({"issues": ["grid_tasmota_stale"]}, lambda p: None)   # info: no push
    m.process({"issues": []}, lambda p: None)
    assert calls == []
    m.process({"issues": ["grid_stale"]}, lambda p: None)           # warning: pushed
    m.process({"issues": []}, lambda p: None)
    assert calls == ["grid_stale", "recovered"]
    m.process({"issues": ["grid_tasmota_stale"]}, lambda p: None)
    m.process({"issues": []}, lambda p: None)
    assert calls == ["grid_stale", "recovered"]                      # no second "All clear"


def test_stage_resets_after_a_long_clear_period(monkeypatch):
    import energy_hub.health as health

    now = [1_800_000_000.0]
    monkeypatch.setattr(health.time, "time", lambda: now[0])
    m = mon(backoff_stages=[3600, 43200])
    out = []
    m.process({"issues": ["pv_stale"]}, out.append)
    now[0] += 600
    m.process({"issues": []}, out.append)                # clear for 2 h (> first stage)
    now[0] += 7200
    m.process({"issues": []}, out.append)
    m.process({"issues": ["pv_stale"]}, out.append)      # new incident: alerted at stage 0 again
    warnings = [a for a in out if a["issue"] == "pv_stale" and a["severity"] == "warning"]
    assert len(warnings) == 2


def test_stage_kept_when_issue_returns_quickly(monkeypatch):
    import energy_hub.health as health

    now = [1_800_000_000.0]
    monkeypatch.setattr(health.time, "time", lambda: now[0])
    m = mon(backoff_stages=[3600, 43200])
    out = []
    m.process({"issues": ["pv_stale"]}, out.append)
    now[0] += 600
    m.process({"issues": []}, out.append)
    now[0] += 600
    m.process({"issues": ["pv_stale"]}, out.append)      # back after 10 min: still in backoff
    warnings = [a for a in out if a["issue"] == "pv_stale" and a["severity"] == "warning"]
    assert len(warnings) == 1
