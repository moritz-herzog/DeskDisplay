import json
from types import SimpleNamespace

from backend.mqtt_publisher import MqttPublisher


class FakeClient:
    def __init__(self):
        self.published = []
        self.will = None
        self.auth = None
        self.started = False
        self.connect_args = None
        self.on_connect = None
        self.on_disconnect = None

    def username_pw_set(self, u, p):
        self.auth = (u, p)

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def reconnect_delay_set(self, **kw):
        pass

    def connect_async(self, host, port, keepalive=60):
        self.connect_args = (host, port)

    def loop_start(self):
        self.started = True

    def loop_stop(self):
        self.started = False

    def disconnect(self):
        pass

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))


CFG = SimpleNamespace(host="broker", port=1883, username="dashboard", password="pw", topic="dashboard/availability",
                      online_topic="dashboard/online", heartbeat_seconds=300)
BUSY = {"status": "busy", "mode": "auto", "until": "2026-11-15T15:30:00+01:00", "label": "Meeting"}
FREE = {"status": "free", "mode": "auto", "until": None, "label": "Frei"}


def make():
    client = FakeClient()
    pub = MqttPublisher(CFG, client_factory=lambda: client)
    pub.start()
    return pub, client


def connect(client):
    client.on_connect(client, None, {}, SimpleNamespace(is_failure=False), None)


def topic_msgs(client, topic):
    return [m for m in client.published if m[0] == topic]


def test_start_configures_will_auth_and_async_connect():
    pub, client = make()
    assert client.will == ("dashboard/online", "false", 1, True)
    assert client.auth == ("dashboard", "pw")
    assert client.connect_args == ("broker", 1883) and client.started
    assert not pub.connected


def test_online_true_on_connect_and_payload_format():
    pub, client = make()
    pub.publish_status(BUSY)  # noch nicht verbunden: nichts gesendet
    assert client.published == []
    connect(client)
    assert pub.connected
    assert ("dashboard/online", "true", 1, True) in client.published
    topic, payload, qos, retain = topic_msgs(client, "dashboard/availability")[0]
    assert (qos, retain) == (1, True)
    assert json.loads(payload) == BUSY


def test_publish_only_on_change_and_heartbeat():
    pub, client = make()
    connect(client)
    assert pub.publish_status(FREE) is True
    assert pub.publish_status(FREE) is False
    assert pub.publish_status(dict(FREE)) is False
    assert len(topic_msgs(client, "dashboard/availability")) == 1
    assert pub.publish_status(BUSY) is True
    assert len(topic_msgs(client, "dashboard/availability")) == 2
    assert pub.heartbeat() is True  # Heartbeat sendet auch ohne Änderung
    msgs = topic_msgs(client, "dashboard/availability")
    assert len(msgs) == 3 and json.loads(msgs[-1][1]) == BUSY and msgs[-1][3] is True


def test_republish_after_reconnect():
    pub, client = make()
    connect(client)
    pub.publish_status(BUSY)
    client.on_disconnect(client, None, None, SimpleNamespace(), None)
    assert not pub.connected
    assert pub.publish_status(FREE) is False  # offline
    client.published.clear()
    connect(client)
    assert json.loads(topic_msgs(client, "dashboard/availability")[0][1]) == FREE


def test_failures_never_raise():
    class Broken(FakeClient):
        def publish(self, *a, **k):
            raise RuntimeError("boom")

    client = Broken()
    pub = MqttPublisher(CFG, client_factory=lambda: client)
    pub.start()
    connect(client)
    assert pub.publish_status(BUSY) is False
    assert pub.heartbeat() is False

    def bad_factory():
        raise RuntimeError("kein paho")

    MqttPublisher(CFG, client_factory=bad_factory).start()  # darf nicht werfen
