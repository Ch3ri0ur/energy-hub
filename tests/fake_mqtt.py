"""Minimal stand-ins for paho objects used in tests."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Published:
    topic: str
    payload: object
    qos: int
    retain: bool


@dataclass
class FakeInfo:
    """Stand-in for paho's MQTTMessageInfo."""

    rc: int = 0

    def wait_for_publish(self, timeout=None):
        pass


class FakeClient:
    """Records publish() calls; mimics the paho Client surface the code uses."""

    def __init__(self) -> None:
        self.published: list[Published] = []
        self.will: tuple | None = None
        self.rc = 0   # set to e.g. 4 (MQTT_ERR_NO_CONN) to simulate a lost connection

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append(Published(topic, payload, qos, retain))
        return FakeInfo(self.rc)

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def topics(self, topic: str) -> list[Published]:
        return [p for p in self.published if p.topic == topic]


@dataclass
class FakeMessage:
    topic: str
    payload: bytes = b""
    retain: bool = False
    qos: int = 0
    _userdata: object = field(default=None, repr=False)
