"""Health monitoring and alerting with exponential backoff."""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .config import AlertConfig

logger = logging.getLogger("energy_hub.health")

# issue -> (severity, message).  "info" issues are published on the MQTT alert
# topic but never sent to the webhook.
ISSUES: dict[str, tuple[str, str]] = {
    "pv_stale": ("warning", "PV data stale"),
    "grid_stale": ("warning", "Grid data stale (no meter delivering)"),
    "grid_tasmota_stale": ("info", "Tasmota grid reader stale, Anker meter covering"),
    "pv_quality_poor": ("warning", "PV sensor quality poor"),
    "grid_quality_poor": ("warning", "Grid sensor quality poor"),
    "anker_plugin_down": ("warning", "Anker cloud plugin not connected"),
}


def _describe(issue: str, context: dict[str, Any]) -> str:
    severity, msg = ISSUES.get(issue, ("warning", issue))
    if issue == "anker_plugin_down":
        err = (context.get("anker_plugin") or {}).get("last_error")
        if err:
            msg = f"{msg}: {err}"
    return msg


class AlertManager:
    """Throttles alerts with exponential backoff and delivers via MQTT + webhook."""

    def __init__(self, cfg: AlertConfig) -> None:
        self._cfg = cfg
        # issue -> (last_send_time, backoff_stage)
        self._state: dict[str, tuple[float, int]] = {}
        self._last_issues: set[str] = set()
        # issue -> time it last recovered (while it stays clear)
        self._cleared_at: dict[str, float] = {}
        # issues whose warning actually went to the webhook and has not been
        # followed by an "All clear" yet
        self._webhook_open: set[str] = set()

    def check(
        self,
        issues: list[str],
        context: dict[str, Any],
        publish_cb: Callable[[dict], None],
    ) -> None:
        if not self._cfg.enabled:
            return

        now = time.time()
        current = set(issues)
        recovered = self._last_issues - current

        # Keep the backoff state of recovered issues for a while: an issue that
        # recovers and comes back five minutes later (flaky WiFi) must continue its
        # backoff instead of alerting again at stage 0.  But an issue that stayed
        # clear for a whole first-stage delay is a new incident: forget its stage,
        # otherwise a once-a-day problem escalates to the one-week stage and goes
        # silent.
        stages = self._cfg.backoff_stages
        cooldown = stages[-1] if stages else 0
        first_stage = stages[0] if stages else 0
        for issue in recovered:
            self._cleared_at[issue] = now
        for issue in current:
            self._cleared_at.pop(issue, None)
        for issue in list(self._state):
            if issue in current:
                continue
            last, _ = self._state[issue]
            cleared = self._cleared_at.get(issue)
            long_clear = cleared is not None and now - cleared > first_stage
            if now - last > cooldown or long_clear:
                del self._state[issue]
                self._cleared_at.pop(issue, None)

        for issue in current:
            if not self._should_send(issue, now):
                continue
            severity = ISSUES.get(issue, ("warning", issue))[0]
            msg = _describe(issue, context)
            stage = self._advance(issue, now)
            payload = {
                "issue": issue,
                "severity": severity,
                "message": msg,
                "timestamp": now,
                "context": context,
            }
            publish_cb(payload)
            if severity != "info":
                self._webhook(msg, severity, issue, now)
                self._webhook_open.add(issue)
            logger.info("Alert sent: %s (stage %d)", issue, stage)

        for issue in sorted(recovered):
            payload = {
                "issue": issue,
                "severity": "info",
                "message": f"{ISSUES.get(issue, ('', issue))[1]} — recovered",
                "timestamp": now,
                "context": context,
            }
            publish_cb(payload)
            logger.info("Recovered: %s", issue)

        if not current and recovered:
            payload = {
                "issue": "recovered",
                "severity": "info",
                "message": "All clear",
                "timestamp": now,
                "context": context,
            }
            publish_cb(payload)
            # Push an "All clear" only when a warning was pushed before: an info
            # issue (Tasmota reader flapping while Anker covers) never reaches the
            # webhook, so neither must its recovery.
            if self._webhook_open:
                self._webhook("All clear", "info", "recovered", now)
                self._webhook_open.clear()
            logger.info("Recovery alert sent")

        self._last_issues = current

    def _should_send(self, issue: str, now: float) -> bool:
        if issue not in self._state:
            return True
        last, stage = self._state[issue]
        delay = self._cfg.backoff_stages[min(stage, len(self._cfg.backoff_stages) - 1)]
        return (now - last) >= delay

    def _advance(self, issue: str, now: float) -> int:
        if issue in self._state:
            _, stage = self._state[issue]
            next_stage = min(stage + 1, len(self._cfg.backoff_stages) - 1)
        else:
            next_stage = 0
        self._state[issue] = (now, next_stage)
        return next_stage

    def _webhook(self, message: str, severity: str, issue: str, ts: float) -> None:
        """Deliver in a background thread so a slow webhook never stalls the publish loop."""
        if not self._cfg.webhook_url:
            return
        data = json.dumps({
            "title": self._cfg.webhook_title,
            "message": message,
            "severity": severity,
            "issue": issue,
            "timestamp": ts,
        }).encode()
        threading.Thread(target=self._post, args=(data,), name="alert-webhook", daemon=True).start()

    def _post(self, data: bytes) -> None:
        try:
            req = urllib.request.Request(
                self._cfg.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5).read()  # noqa: S310
        except (urllib.error.URLError, OSError, ValueError) as exc:   # ValueError: bad URL
            logger.error("Webhook failed: %s", exc)


class HealthMonitor:
    """Collects health info and triggers alerts on issues."""

    def __init__(
        self,
        cfg: AlertConfig,
        start_time: float,
        quality_alert_threshold: float = 0.5,
    ) -> None:
        self._alerts = AlertManager(cfg)
        self._start_time = start_time
        self._min_quality = max(0.0, min(1.0, 1.0 - quality_alert_threshold))

    def build_health(
        self,
        source_statuses: dict[str, Any],
        anker_status: dict[str, Any] | bool | None,
        connected: bool,
    ) -> dict[str, Any]:
        """``anker_status`` is ``AnkerPlugin.status()``, ``None`` when the plugin is
        disabled (a bool is accepted for backwards compatibility)."""
        now = time.time()
        issues: list[str] = []
        if isinstance(anker_status, bool):
            anker_status = {"alive": anker_status}
        anker_alive = anker_status is None or bool(anker_status.get("alive"))

        pv = source_statuses.get("pv_roof", {})
        grid = source_statuses.get("grid_tasmota", {})
        anker_grid = source_statuses.get("grid_anker", {})

        if pv.get("status") in ("stale", "dead"):
            issues.append("pv_stale")
        if grid.get("status") in ("stale", "dead", "no_data"):
            if anker_grid.get("status") == "ok":
                issues.append("grid_tasmota_stale")   # failover is covering
            elif grid.get("status") != "no_data" or anker_grid.get("status") not in (None, "no_data"):
                issues.append("grid_stale")
        if pv.get("quality", 1.0) < self._min_quality:
            issues.append("pv_quality_poor")
        if grid.get("quality", 1.0) < self._min_quality:
            issues.append("grid_quality_poor")
        if not anker_alive:
            issues.append("anker_plugin_down")

        health = {
            "status": "degraded" if issues else "ok",
            "connected": connected,
            "uptime": round(now - self._start_time, 1),
            "sources": source_statuses,
            "anker_plugin_alive": anker_alive,
            "anker_plugin": anker_status,
            "issues": issues,
            "timestamp": now,
        }
        return health

    def process(
        self,
        health: dict[str, Any],
        publish_alert: Callable[[dict], None],
    ) -> None:
        self._alerts.check(health.get("issues", []), health, publish_alert)
