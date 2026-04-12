"""Health monitoring and alerting with exponential backoff."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .config import AlertConfig

logger = logging.getLogger("energy_hub.health")


class AlertManager:
    """Throttles alerts with exponential backoff and delivers via MQTT + webhook."""

    def __init__(self, cfg: AlertConfig) -> None:
        self._cfg = cfg
        # issue -> (last_send_time, backoff_stage)
        self._state: dict[str, tuple[float, int]] = {}
        self._last_issues: set[str] = set()

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
        messages = {
            "pv_stale": "PV data stale",
            "grid_stale": "Grid data stale",
            "pv_quality_poor": "PV sensor quality poor",
            "grid_quality_poor": "Grid sensor quality poor",
            "anker_plugin_down": "Anker cloud plugin not connected",
        }

        for issue in recovered:
            self._state.pop(issue, None)

        for issue in current:
            if not self._should_send(issue, now):
                continue
            msg = messages.get(issue, issue)
            stage = self._advance(issue, now)
            payload = {
                "issue": issue,
                "severity": "warning",
                "message": msg,
                "timestamp": now,
                "context": context,
            }
            publish_cb(payload)
            self._webhook(msg, "warning", issue, now)
            logger.info("Alert sent: %s (stage %d)", issue, stage)

        if not current and recovered:
            payload = {
                "issue": "recovered",
                "severity": "info",
                "message": "All clear",
                "timestamp": now,
                "context": context,
            }
            publish_cb(payload)
            self._webhook("All clear", "info", "recovered", now)
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
        if not self._cfg.webhook_url:
            return
        try:
            data = json.dumps({
                "title": self._cfg.webhook_title,
                "message": message,
                "severity": severity,
                "issue": issue,
                "timestamp": ts,
            }).encode()
            req = urllib.request.Request(
                self._cfg.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5).read()  # noqa: S310
        except (urllib.error.URLError, OSError) as exc:
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
        anker_alive: bool,
        connected: bool,
    ) -> dict[str, Any]:
        now = time.time()
        issues: list[str] = []

        pv = source_statuses.get("pv_roof", {})
        grid = source_statuses.get("grid_tasmota", {})

        if pv.get("status") in ("stale", "dead"):
            issues.append("pv_stale")
        if grid.get("status") in ("stale", "dead"):
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
