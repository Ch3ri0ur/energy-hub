"""energy-hub entry point — wires everything together."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys
import time

import paho.mqtt.client as mqtt

from .adapters.anker_data import parse_anker_snapshot
from .adapters.charger import parse_charger_nrg
from .adapters.tasmota_sml import parse_tasmota_sml
from .anker_plugin import AnkerPlugin
from .config import AppConfig, load_config
from .engine import Engine
from .gas_tracker import GasTracker
from .health import HealthMonitor
from .publisher import Publisher

logger = logging.getLogger("energy_hub")


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def _setup_logging(cfg: AppConfig) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, cfg.logging.level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)

    if cfg.logging.file:
        fh = logging.handlers.RotatingFileHandler(
            cfg.logging.file,
            maxBytes=cfg.logging.max_bytes,
            backupCount=cfg.logging.backup_count,
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)


# ---------------------------------------------------------------------------
# MQTT message routing
# ---------------------------------------------------------------------------


def _build_router(cfg: AppConfig, engine: Engine, gas: GasTracker) -> dict[str, object]:
    """Return {topic: callback} mapping for local MQTT subscriptions."""

    def on_pv(_client, _userdata, msg):
        reading = parse_tasmota_sml(msg.payload, source="pv")
        if reading:
            engine.update_pv(reading)

    def on_grid(_client, _userdata, msg):
        reading = parse_tasmota_sml(msg.payload, source="grid")
        if reading:
            engine.update_grid_tasmota(reading)

    def on_anker(_client, _userdata, msg):
        grid, sb = parse_anker_snapshot(msg.payload)
        if grid:
            engine.update_grid_anker(grid)
        if sb:
            engine.update_solarbank(sb)

    def on_charger(_client, _userdata, msg):
        power = parse_charger_nrg(msg.payload)
        if power is not None:
            engine.update_charger(power)

    routes: dict[str, object] = {}
    if cfg.sources.tasmota_pv.topic:
        routes[cfg.sources.tasmota_pv.topic] = on_pv
    if cfg.sources.tasmota_grid.topic:
        routes[cfg.sources.tasmota_grid.topic] = on_grid
    if cfg.sources.anker.topic:
        routes[cfg.sources.anker.topic] = on_anker
    if cfg.sources.charger.topic:
        routes[cfg.sources.charger.topic] = on_charger

    # Gas tracker topics
    for topic in gas.topics:
        routes[topic] = gas.on_message

    return routes


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _run(cfg: AppConfig) -> None:
    start_time = time.time()
    engine = Engine(cfg)
    health_mon = HealthMonitor(
        cfg.alerts,
        start_time,
        cfg.validation.quality_alert_threshold,
    )

    # --- Local MQTT client ---
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=cfg.mqtt.client_id,
        protocol=mqtt.MQTTv311,
    )
    if cfg.mqtt.username:
        client.username_pw_set(cfg.mqtt.username, cfg.mqtt.password)

    pub = Publisher(client, cfg.output)

    # Gas tracker (independent of engine/publisher)
    gas = GasTracker(client, cfg.gas)

    # Route incoming messages
    routes = _build_router(cfg, engine, gas)

    def on_connect(_client, _userdata, _flags, reason_code, _properties):
        if reason_code == 0:
            logger.info("Connected to MQTT broker %s:%d", cfg.mqtt.broker, cfg.mqtt.port)
            for topic in routes:
                _client.subscribe(topic)
                logger.info("Subscribed to %s", topic)
        else:
            logger.error("MQTT connect failed: %s", reason_code)

    def on_message(_client, _userdata, msg):
        cb = routes.get(msg.topic)
        if cb:
            cb(_client, _userdata, msg)

    def on_disconnect(_client, _userdata, _flags, reason_code, _properties):
        if reason_code != 0:
            logger.warning("MQTT disconnected (rc=%s), reconnecting...", reason_code)

    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect

    client.connect(cfg.mqtt.broker, cfg.mqtt.port, cfg.mqtt.keepalive)
    client.loop_start()

    # --- Anker plugin ---
    anker: AnkerPlugin | None = None
    if cfg.anker.enabled:
        anker = AnkerPlugin(
            cfg=cfg.anker,
            local_mqtt_client=client,
            output_topic=cfg.sources.anker.topic,
        )
        anker.start()

    # --- Signal handling ---
    shutdown = False

    def _signal_handler(signum, _frame):
        nonlocal shutdown
        logger.info("Received signal %d, shutting down...", signum)
        shutdown = True

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # --- Periodic tick ---
    last_health = 0.0
    logger.info("energy-hub running (publish every %ds, health every %ds)",
                cfg.output.publish_interval, cfg.output.health_interval)

    try:
        while not shutdown:
            now = time.time()

            # Publish aggregated snapshot
            if pub.should_publish():
                snapshot = engine.snapshot()
                pub.publish_snapshot(snapshot)

            # Publish health + check alerts
            if (now - last_health) >= cfg.output.health_interval:
                statuses = engine.source_statuses()
                health = health_mon.build_health(
                    source_statuses=statuses,
                    anker_alive=anker.is_alive() if anker else True,
                    connected=client.is_connected(),
                )
                pub.publish_health(health)
                health_mon.process(health, pub.publish_alert)
                last_health = now

            time.sleep(1)
    finally:
        logger.info("Shutting down...")
        if anker:
            anker.stop()
        client.loop_stop()
        client.disconnect()
        logger.info("energy-hub stopped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="energy-hub: unified energy monitor")
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "-e", "--env-file",
        default=".env",
        help="Path to .env file (default: .env, use '' to disable)",
    )
    args = parser.parse_args()

    env_file = args.env_file or None
    cfg = load_config(args.config, env_file=env_file)
    _setup_logging(cfg)
    _run(cfg)


if __name__ == "__main__":
    main()
