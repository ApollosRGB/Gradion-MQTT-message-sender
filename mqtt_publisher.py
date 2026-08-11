"""
MQTT Publisher
--------------
Publishes a JSON command to an MQTT topic on a repeating timer, for terminals
and cron-style automation. The GUI version is mqtt_trigger_app.py.

Nothing about a particular broker is written into this file. Settings come from
environment variables, so this script can be committed to a public repository
and shared without leaking where it points or who it connects as.

    MQTT_HOST       broker hostname                     (required)
    MQTT_PORT       broker port                         (default 8883)
    MQTT_TOPIC      topic to publish to                 (required)
    MQTT_USERNAME   broker username                     (optional)
    MQTT_PASSWORD   broker password                     (prompted if not set)
    MQTT_INTERVAL   seconds between messages            (default 60)
    MQTT_PAYLOAD    JSON payload to publish             (default below)
    MQTT_TLS        1 or 0 - use TLS                    (default 1)
    MQTT_VERIFY     1 or 0 - validate the certificate   (default 0)

Example:

    export MQTT_HOST="broker.example.com"
    export MQTT_TOPIC="example/device/cmd"
    export MQTT_USERNAME="alice"
    export MQTT_PAYLOAD='{"command":"example","value":1}'
    python mqtt_publisher.py

On PowerShell use $env:MQTT_HOST = "broker.example.com" instead of export.

Keep the password out of your shell history: leave MQTT_PASSWORD unset and the
script prompts for it without echoing.
"""

import getpass
import json
import os
import ssl
import sys
import time

import paho.mqtt.client as mqtt

DEFAULT_PAYLOAD = '{"command": "example", "value": 1}'


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _number(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        sys.exit(f"{name} must be a number, got {raw!r}")


def load_settings() -> dict:
    host = os.environ.get("MQTT_HOST", "").strip()
    topic = os.environ.get("MQTT_TOPIC", "").strip()
    missing = [n for n, v in (("MQTT_HOST", host), ("MQTT_TOPIC", topic)) if not v]
    if missing:
        sys.exit(
            f"Missing required setting(s): {', '.join(missing)}.\n"
            f"See the notes at the top of this file, or run the GUI version "
            f"(mqtt_trigger_app.py) instead.")

    return {
        "host": host,
        "port": int(_number("MQTT_PORT", 8883)),
        "topic": topic,
        "username": os.environ.get("MQTT_USERNAME", "").strip(),
        "interval": max(0.1, _number("MQTT_INTERVAL", 60)),
        "payload": os.environ.get("MQTT_PAYLOAD") or DEFAULT_PAYLOAD,
        "use_tls": _flag("MQTT_TLS", True),
        "verify": _flag("MQTT_VERIFY", False),
    }


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        print("Connected to broker.")
    else:
        print(f"Failed to connect, reason code: {reason_code}")


def main() -> None:
    cfg = load_settings()

    # Fail early on a malformed payload rather than at the first publish.
    try:
        json.loads(cfg["payload"])
    except ValueError:
        print("Warning: MQTT_PAYLOAD is not valid JSON. Publishing it as-is.")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect

    if cfg["username"]:
        password = os.environ.get("MQTT_PASSWORD") or getpass.getpass(
            f"Password for '{cfg['username']}' on {cfg['host']}: ")
        client.username_pw_set(cfg["username"], password)

    if cfg["use_tls"]:
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED if cfg["verify"] else ssl.CERT_NONE)
        if not cfg["verify"]:
            client.tls_insecure_set(True)

    print(f"Connecting to {cfg['host']}:{cfg['port']} "
          f"(TLS {'on' if cfg['use_tls'] else 'off'}) ...")
    client.connect(cfg["host"], cfg["port"], keepalive=60)
    client.loop_start()

    try:
        while True:
            result = client.publish(cfg["topic"], cfg["payload"])
            result.wait_for_publish()
            print(f"Published to '{cfg['topic']}': {cfg['payload']}")
            time.sleep(cfg["interval"])
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        client.loop_stop()
        client.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    main()
