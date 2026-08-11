"""
MQTT Trigger
============
A desktop app for firing saved MQTT messages at a broker on demand.

Built for the case where something goes wrong with the system and you need to
push a known command (or a repeating stream of them) to the robot / broker
without editing a script first.

Features
--------
  * Saved presets   - name, topic, payload, interval, QoS, retain.
  * Parallel loops  - every preset has its own start/stop and its own timer,
                      so several can publish at different rates at once.
  * Live view       - Activity tab shows every publish; Debug tab shows
                      connection events and raw MQTT logging.
  * Interval control- change the seconds between messages per preset, live.
  * Light / dark    - System, Light or Dark, remembered between launches.
  * Credentials     - broker password is kept in the OS credential vault
                      (via keyring), never in a file.
  * Encrypted at    - host, username, topics and payloads live in an encrypted
    rest              vault file. The key is random per machine and is itself
                      held in the OS credential vault.

Nothing this app stores ever leaves the machine it runs on. There is no server
component, no telemetry and no sync. The app ships with no broker details in
it - you enter your own on first run, and they stay local.

Runs on Windows, macOS and Linux. Platform differences are confined to
_config_dir(), _mono_font() and the keyboard accelerators below.

  Settings vault      Windows  %APPDATA%\\MQTTTrigger\\config.vault
                      macOS    ~/Library/Application Support/MQTTTrigger/
                      Linux    ~/.config/MQTTTrigger/
  Credential vault    Windows  Credential Manager
                      macOS    Keychain
                      Linux    Secret Service / KWallet

To move a setup to another machine, use Export profile / Import profile - a
passphrase-encrypted file. Copying the vault file across will not work, because
its key never leaves the machine that made it.

Run with:    python mqtt_trigger_app.py
Build:       build_exe.bat      (Windows -> .exe)
             build_app.command  (macOS   -> .app)
             build_dmg.command  (macOS   -> .dmg)
"""

from __future__ import annotations

import json
import os
import queue
import ssl
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
import paho.mqtt.client as mqtt

# ============================================================================
# Constants
# ============================================================================

APP_NAME = "MQTT Trigger"
APP_VERSION = "1.3"
KEYRING_SERVICE = "MQTTTrigger"

# Credential-vault account name for the random key that encrypts the settings
# file. Kept apart from the broker password entries, which are keyed by
# host/port/username.
KEYRING_VAULT_ACCOUNT = "__local_vault_key__"

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")


def _config_dir() -> Path:
    """Where this platform expects an app to keep its settings."""
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "MQTTTrigger"
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA") or Path.home()) / "MQTTTrigger"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "MQTTTrigger"


def _mono_font() -> str:
    """A fixed-width font that is actually installed on this platform."""
    if IS_MAC:
        return "Menlo"
    if IS_WINDOWS:
        return "Consolas"
    return "DejaVu Sans Mono"


CONFIG_DIR = _config_dir()
CONFIG_FILE = CONFIG_DIR / "config.vault"
LEGACY_CONFIG_FILE = CONFIG_DIR / "config.json"   # pre-1.3 plaintext settings
PROFILE_SUFFIX = ".mqttprofile"
MONO_FONT = _mono_font()

# macOS uses Command for shortcuts where Windows and Linux use Control.
MODIFIER = "Command" if IS_MAC else "Control"
MODIFIER_LABEL = "Cmd" if IS_MAC else "Ctrl"

# Shipped blank on purpose. Broker details are yours, not the app's - you enter
# them once on first run and they are stored encrypted on your machine only.
# Nothing identifying a particular broker, tenant or topic belongs in this file.
DEFAULT_BROKER = {
    "host": "",
    "port": 8883,
    "username": "",
    "use_tls": True,
    "validate_cert": False,
    "keepalive": 60,
    "client_id": "",
}

DEFAULT_TOPIC = "example/device/cmd"

DEFAULT_PAYLOAD = json.dumps(
    {"command": "example", "value": 1}, indent=2
)

MIN_INTERVAL_S = 0.1
MAX_LOG_LINES = 2000
CONNECT_TIMEOUT_S = 15

# Log colours, per appearance mode. tag_config takes a single colour, so these
# get re-applied whenever the theme changes.
LOG_COLORS = {
    "Dark": {
        "ts": "#6b7280", "info": "#9aa0a6", "tx": "#4dabf7",
        "ok": "#51cf66", "warn": "#ffa94d", "err": "#ff6b6b",
    },
    "Light": {
        "ts": "#868e96", "info": "#495057", "tx": "#1971c2",
        "ok": "#2f9e44", "warn": "#e8590c", "err": "#c92a2a",
    },
}


# ============================================================================
# Secret storage - OS credential vault via keyring
#   Windows -> Credential Manager    macOS -> Keychain    Linux -> Secret Service
# ============================================================================


class SecretStore:
    """Stores the broker password in the OS credential vault.

    Falls back to memory-only storage (lost on exit) if no keyring backend is
    usable, so the app still runs rather than refusing to start.
    """

    def __init__(self) -> None:
        self.available = False
        self.error: str | None = None
        self.backend_name = "memory (not persisted)"
        self._memory: dict[str, str] = {}
        self._keyring = None
        try:
            import keyring
            from keyring.backends.fail import Keyring as FailKeyring

            backend = keyring.get_keyring()
            if isinstance(backend, FailKeyring):
                raise RuntimeError("no usable keyring backend on this system")
            self._keyring = keyring
            self.available = True
            self.backend_name = type(backend).__name__
        except Exception as exc:  # pragma: no cover - environment dependent
            self.error = f"{type(exc).__name__}: {exc}"

    @staticmethod
    def key_for(broker: dict) -> str:
        """Credential account name - unique per host/port/user."""
        return f"{broker.get('host', '')}:{broker.get('port', '')}:{broker.get('username', '')}"

    def get(self, key: str) -> str:
        if self.available:
            try:
                return self._keyring.get_password(KEYRING_SERVICE, key) or ""
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
        return self._memory.get(key, "")

    def set(self, key: str, password: str) -> tuple[bool, str]:
        if self.available:
            try:
                if password:
                    self._keyring.set_password(KEYRING_SERVICE, key, password)
                else:
                    self.delete(key)
                return True, f"Password saved to {self.backend_name}"
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                return False, f"Could not save to credential store: {exc}"
        self._memory[key] = password
        return False, "Credential store unavailable - password kept for this session only"

    def delete(self, key: str) -> None:
        if self.available:
            try:
                self._keyring.delete_password(KEYRING_SERVICE, key)
            except Exception:
                pass
        self._memory.pop(key, None)


# ============================================================================
# Local encryption
#
# The settings file holds your broker host, username and every topic and
# payload you have saved - enough to describe your internal setup, so it is not
# left lying around as plain text.
#
# At-rest key    A random 32-byte key, generated once per machine and kept in
#                the OS credential vault next to the password. It never leaves
#                the machine and is never written to disk in the clear, so a
#                copied vault file is useless on any other computer.
# Export key     Derived from a passphrase you type, with PBKDF2-HMAC-SHA256.
#                Used only for Export/Import profile, which is how a setup is
#                deliberately moved between machines.
#
# If the cryptography package is missing the app still runs, but falls back to
# a plain-text settings file and says so loudly in the Debug tab.
# ============================================================================

PBKDF2_ROUNDS = 480_000
PROFILE_MAGIC = b"MQTTTRIGGER-PROFILE-1\n"

PROFILE_EXPORT = "Export profile..."
PROFILE_IMPORT = "Import profile..."


def _load_fernet():
    """Returns (Fernet, PBKDF2HMAC, hashes) or None if unavailable."""
    try:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        return Fernet, PBKDF2HMAC, hashes
    except Exception:
        return None


class LocalVault:
    """Encrypts the settings file with a machine-local key.

    The key lives in the OS credential vault, so the encrypted file is readable
    only by this account on this machine.
    """

    def __init__(self, secrets: SecretStore) -> None:
        self.available = False
        self.error: str | None = None
        self._fernet = None

        crypto = _load_fernet()
        if crypto is None:
            self.error = ("cryptography package not installed - settings will be "
                          "saved as plain text")
            return
        if not secrets.available:
            self.error = ("no OS credential vault available to hold the encryption "
                          "key - settings will be saved as plain text")
            return

        Fernet = crypto[0]
        try:
            key = secrets.get(KEYRING_VAULT_ACCOUNT)
            if not key:
                key = Fernet.generate_key().decode("ascii")
                stored, message = secrets.set(KEYRING_VAULT_ACCOUNT, key)
                if not stored:
                    self.error = f"could not store the encryption key: {message}"
                    return
            self._fernet = Fernet(key.encode("ascii"))
            self.available = True
        except Exception as exc:  # pragma: no cover - environment dependent
            self.error = f"{type(exc).__name__}: {exc}"

    def encrypt(self, plaintext: str) -> bytes:
        if not self.available:
            return plaintext.encode("utf-8")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, blob: bytes) -> str:
        if not self.available:
            return blob.decode("utf-8")
        return self._fernet.decrypt(blob).decode("utf-8")


class ProfileCrypto:
    """Passphrase-encrypted profile files, for moving a setup between machines.

    Layout:  MAGIC | 16-byte salt | Fernet token
    The key is derived from the passphrase with PBKDF2-HMAC-SHA256, so the file
    is only as strong as the passphrase - pick a real one.
    """

    class Error(Exception):
        pass

    @staticmethod
    def _derive(passphrase: str, salt: bytes) -> bytes:
        import base64

        crypto = _load_fernet()
        if crypto is None:
            raise ProfileCrypto.Error(
                "The cryptography package is not installed, so profiles cannot be "
                "encrypted or opened. Install it with:  pip install cryptography")
        _, PBKDF2HMAC, hashes = crypto
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                         iterations=PBKDF2_ROUNDS)
        return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))

    @staticmethod
    def encrypt(payload: dict, passphrase: str) -> bytes:
        crypto = _load_fernet()
        if crypto is None:
            raise ProfileCrypto.Error(
                "The cryptography package is not installed, so profiles cannot be "
                "encrypted. Install it with:  pip install cryptography")
        Fernet = crypto[0]
        salt = os.urandom(16)
        token = Fernet(ProfileCrypto._derive(passphrase, salt)).encrypt(
            json.dumps(payload).encode("utf-8"))
        return PROFILE_MAGIC + salt + token

    @staticmethod
    def decrypt(blob: bytes, passphrase: str) -> dict:
        crypto = _load_fernet()
        if crypto is None:
            raise ProfileCrypto.Error(
                "The cryptography package is not installed, so profiles cannot be "
                "opened. Install it with:  pip install cryptography")
        Fernet = crypto[0]
        if not blob.startswith(PROFILE_MAGIC):
            raise ProfileCrypto.Error(
                f"That is not a {APP_NAME} profile file.")
        body = blob[len(PROFILE_MAGIC):]
        if len(body) < 17:
            raise ProfileCrypto.Error("The profile file is truncated or damaged.")
        salt, token = body[:16], body[16:]
        try:
            raw = Fernet(ProfileCrypto._derive(passphrase, salt)).decrypt(token)
        except ProfileCrypto.Error:
            raise
        except Exception:
            raise ProfileCrypto.Error(
                "Wrong passphrase, or the file has been altered since it was written.")
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ProfileCrypto.Error("The profile decrypted but its contents are not valid.")
        if not isinstance(data, dict):
            raise ProfileCrypto.Error("The profile decrypted but its contents are not valid.")
        return data


# ============================================================================
# Config
# ============================================================================


@dataclass
class Preset:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "New message"
    topic: str = DEFAULT_TOPIC
    payload: str = DEFAULT_PAYLOAD
    interval: float = 60.0
    qos: int = 0
    retain: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "Preset":
        p = cls()
        p.id = str(data.get("id") or p.id)
        p.name = str(data.get("name", p.name))
        p.topic = str(data.get("topic", p.topic))
        p.payload = str(data.get("payload", p.payload))
        try:
            p.interval = max(MIN_INTERVAL_S, float(data.get("interval", p.interval)))
        except (TypeError, ValueError):
            p.interval = 60.0
        try:
            p.qos = int(data.get("qos", 0))
        except (TypeError, ValueError):
            p.qos = 0
        p.qos = p.qos if p.qos in (0, 1, 2) else 0
        p.retain = bool(data.get("retain", False))
        return p

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "topic": self.topic,
            "payload": self.payload, "interval": self.interval,
            "qos": self.qos, "retain": self.retain,
        }


class Config:
    def __init__(self, vault: "LocalVault | None" = None) -> None:
        self.vault = vault
        self.appearance = "System"
        self.broker = dict(DEFAULT_BROKER)
        self.presets: list[Preset] = []
        self.last_selected: str | None = None
        self.autoscroll = True
        self.migrated_from_plaintext = False
        self.load_error: str | None = None

    @classmethod
    def load(cls, vault: "LocalVault | None" = None) -> "Config":
        cfg = cls(vault)
        raw = cfg._read_settings()
        if raw is None:
            cfg.presets = [cfg._seed_preset()]
            return cfg

        cfg.appearance = raw.get("appearance", "System")
        if cfg.appearance not in ("System", "Light", "Dark"):
            cfg.appearance = "System"
        broker = raw.get("broker") or {}
        cfg.broker = {**DEFAULT_BROKER, **{k: broker[k] for k in broker if k in DEFAULT_BROKER}}
        cfg.presets = [Preset.from_dict(p) for p in raw.get("presets", []) if isinstance(p, dict)]
        if not cfg.presets:
            cfg.presets = [cfg._seed_preset()]
        cfg.last_selected = raw.get("last_selected")
        cfg.autoscroll = bool(raw.get("autoscroll", True))
        return cfg

    @staticmethod
    def _seed_preset() -> Preset:
        return Preset(name="Example message", topic=DEFAULT_TOPIC,
                      payload=DEFAULT_PAYLOAD, interval=60.0)

    def _read_settings(self) -> dict | None:
        """Reads the encrypted vault, falling back to a pre-1.3 plain config.json.

        Returns None when there is nothing to read, or when what is there cannot
        be used - in which case the bad file is kept rather than overwritten.
        """
        if CONFIG_FILE.exists():
            try:
                text = self.vault.decrypt(CONFIG_FILE.read_bytes()) if self.vault \
                    else CONFIG_FILE.read_text(encoding="utf-8")
                return json.loads(text)
            except Exception as exc:
                # Wrong key or damaged file. Keep it - deleting it would throw
                # away every saved message with no way back.
                self.load_error = (
                    f"Could not read {CONFIG_FILE.name} ({type(exc).__name__}). It has "
                    f"been kept as {CONFIG_FILE.name}.bad and a fresh one started. "
                    f"This usually means the encryption key in the credential vault "
                    f"was removed, or the file came from another machine.")
                try:
                    CONFIG_FILE.replace(CONFIG_FILE.with_name(CONFIG_FILE.name + ".bad"))
                except Exception:
                    pass
                return None

        # Pre-1.3 plain-text settings - read once, then re-save encrypted.
        if LEGACY_CONFIG_FILE.exists():
            try:
                data = json.loads(LEGACY_CONFIG_FILE.read_text(encoding="utf-8"))
                self.migrated_from_plaintext = True
                return data
            except Exception:
                self.load_error = (f"Could not read the old {LEGACY_CONFIG_FILE.name}; "
                                   f"starting fresh.")
                return None
        return None

    def to_dict(self) -> dict:
        return {
            "version": 2,
            "appearance": self.appearance,
            "broker": self.broker,
            "presets": [p.to_dict() for p in self.presets],
            "last_selected": self.last_selected,
            "autoscroll": self.autoscroll,
        }

    def save(self) -> tuple[bool, str]:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            blob = self.vault.encrypt(json.dumps(self.to_dict(), indent=2)) if self.vault \
                else json.dumps(self.to_dict(), indent=2).encode("utf-8")
            tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
            tmp.write_bytes(blob)
            tmp.replace(CONFIG_FILE)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

        # Only once the encrypted copy is safely on disk: remove the old
        # plain-text file, so the details stop existing in the clear.
        if self.migrated_from_plaintext and self.vault and self.vault.available:
            try:
                LEGACY_CONFIG_FILE.unlink()
                self.migrated_from_plaintext = False
            except Exception:
                pass
        return True, str(CONFIG_FILE)

    def find(self, preset_id: str | None) -> Preset | None:
        return next((p for p in self.presets if p.id == preset_id), None)


# ============================================================================
# MQTT connection
# ============================================================================


class MqttManager:
    """Owns a single shared paho client. All publishing goes through here."""

    def __init__(self, emit) -> None:
        self._emit = emit                     # emit(channel, level, text)
        self._lock = threading.RLock()
        self._client: mqtt.Client | None = None
        self._connack = threading.Event()
        self._connack_rc: int | None = None
        self._connack_text = ""
        self._connected = threading.Event()
        self._intentional_disconnect = False
        self.broker: dict = dict(DEFAULT_BROKER)
        self.password: str = ""

    # -- state ------------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def configure(self, broker: dict, password: str) -> None:
        with self._lock:
            self.broker = dict(broker)
            self.password = password

    # -- callbacks --------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        rc = getattr(reason_code, "value", reason_code)
        self._connack_rc = int(rc)
        self._connack_text = str(reason_code)
        if rc == 0:
            self._connected.set()
            self._emit("debug", "ok", f"CONNACK ok - connected to "
                                      f"{self.broker['host']}:{self.broker['port']}")
        else:
            self._connected.clear()
            self._emit("debug", "err", f"CONNACK refused - {reason_code}")
        self._connack.set()

    def _on_disconnect(self, client, userdata, flags=None, reason_code=None, properties=None):
        was = self._connected.is_set()
        self._connected.clear()
        if self._intentional_disconnect:
            self._emit("debug", "info", "Disconnected (requested).")
        elif was:
            self._emit("debug", "warn", f"Connection lost ({reason_code}). "
                                        f"Auto-reconnect will retry.")

    def _on_log(self, client, userdata, level, buf):
        if level >= mqtt.MQTT_LOG_WARNING:
            self._emit("debug", "warn", f"paho: {buf}")

    # -- lifecycle --------------------------------------------------------
    def connect(self, timeout: float = CONNECT_TIMEOUT_S) -> tuple[bool, str]:
        """Blocking connect. Safe to call from any thread, repeatedly."""
        with self._lock:
            if self._connected.is_set():
                return True, "Already connected"

            self._teardown()
            broker = self.broker
            client_id = broker.get("client_id") or f"mqtt-trigger-{uuid.uuid4().hex[:8]}"
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_log = self._on_log
            client.reconnect_delay_set(min_delay=1, max_delay=30)

            if broker.get("username"):
                client.username_pw_set(broker["username"], self.password or None)

            if broker.get("use_tls", True):
                try:
                    validate = bool(broker.get("validate_cert", False))
                    client.tls_set(cert_reqs=ssl.CERT_REQUIRED if validate else ssl.CERT_NONE)
                    if not validate:
                        client.tls_insecure_set(True)
                except Exception as exc:
                    return False, f"TLS setup failed: {exc}"

            self._client = client
            self._connack.clear()
            self._connack_rc = None
            self._intentional_disconnect = False

            self._emit("debug", "info",
                       f"Connecting to {broker['host']}:{broker['port']} "
                       f"(TLS {'on' if broker.get('use_tls') else 'off'}, "
                       f"cert validation {'on' if broker.get('validate_cert') else 'off'}) "
                       f"as client id '{client_id}'")
            try:
                client.connect(broker["host"], int(broker["port"]),
                               keepalive=int(broker.get("keepalive", 60)))
            except Exception as exc:
                self._teardown()
                msg = f"{type(exc).__name__}: {exc}"
                self._emit("debug", "err", f"Connect failed - {msg}")
                return False, msg

            client.loop_start()
            if not self._connack.wait(timeout):
                self._teardown()
                msg = f"No response from broker within {timeout:.0f}s"
                self._emit("debug", "err", msg)
                return False, msg

            if self._connack_rc != 0:
                msg = f"Broker refused connection: {self._connack_text}"
                self._teardown()
                return False, msg

            return True, f"Connected to {broker['host']}:{broker['port']}"

    def disconnect(self) -> None:
        with self._lock:
            self._intentional_disconnect = True
            self._teardown()

    def _teardown(self) -> None:
        client, self._client = self._client, None
        self._connected.clear()
        if client is None:
            return
        try:
            client.disconnect()
        except Exception:
            pass
        try:
            client.loop_stop()
        except Exception:
            pass

    # -- publishing -------------------------------------------------------
    def publish(self, topic: str, payload: str, qos: int, retain: bool,
                timeout: float = 10.0) -> tuple[bool, str]:
        client = self._client
        if client is None or not self._connected.is_set():
            return False, "Not connected"
        try:
            info = client.publish(topic, payload, qos=qos, retain=retain)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return False, mqtt.error_string(info.rc)
        try:
            info.wait_for_publish(timeout=timeout)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if not info.is_published():
            return False, f"Not acknowledged within {timeout:.0f}s"
        return True, f"mid={info.mid}"


# ============================================================================
# Loop runner - one thread per running preset
# ============================================================================


class LoopRunner(threading.Thread):
    """Publishes one preset repeatedly until stopped."""

    def __init__(self, preset: Preset, manager: MqttManager, events: queue.Queue) -> None:
        super().__init__(daemon=True, name=f"loop-{preset.name}")
        self.preset_id = preset.id
        self.name_label = preset.name
        self.topic = preset.topic
        self.payload = preset.payload
        self.interval = max(MIN_INTERVAL_S, float(preset.interval))
        self.qos = preset.qos
        self.retain = preset.retain
        self._manager = manager
        self._events = events
        self._stop = threading.Event()
        self.sent = 0
        self.failed = 0

    def stop(self) -> None:
        self._stop.set()

    def _put(self, kind: str, **data) -> None:
        self._events.put({"kind": kind, "preset_id": self.preset_id, **data})

    def run(self) -> None:
        if not self._manager.is_connected:
            self._put("log", channel="debug", level="info",
                      text=f"[{self.name_label}] connecting before first publish...")
            ok, msg = self._manager.connect()
            if not ok:
                self._put("log", channel="activity", level="err",
                          text=f"[{self.name_label}] cannot start - {msg}")
                self._put("stopped", reason=msg)
                return

        self._put("started", interval=self.interval)

        while not self._stop.is_set():
            ok, detail = self._manager.publish(self.topic, self.payload, self.qos, self.retain)
            if ok:
                self.sent += 1
                self._put("log", channel="activity", level="tx",
                          text=f"TX  {self.topic}  (QoS {self.qos}"
                               f"{', retain' if self.retain else ''})  "
                               f"[{self.name_label} #{self.sent}]\n     {self.payload_oneline()}")
            else:
                self.failed += 1
                self._put("log", channel="activity", level="err",
                          text=f"FAIL {self.topic}  [{self.name_label}] - {detail}")
                if detail == "Not connected":
                    # Wait for auto-reconnect rather than spinning at full rate.
                    self._put("log", channel="debug", level="warn",
                              text=f"[{self.name_label}] publish skipped, waiting for reconnect")

            self._put("tick", sent=self.sent, failed=self.failed,
                      next_at=time.monotonic() + self.interval)
            if self._stop.wait(self.interval):
                break

        self._put("stopped", reason="Stopped", sent=self.sent, failed=self.failed)

    def payload_oneline(self, limit: int = 400) -> str:
        flat = " ".join(self.payload.split())
        return flat if len(flat) <= limit else flat[:limit] + " ..."


# ============================================================================
# Broker settings dialog
# ============================================================================


class BrokerDialog(ctk.CTkToplevel):
    def __init__(self, master: "App", broker: dict, password: str) -> None:
        super().__init__(master)
        self.app = master
        self.result: tuple[dict, str] | None = None

        self.title("Broker settings")
        self.geometry("520x470")
        self.resizable(False, False)
        self.transient(master)
        self.grid_columnconfigure(1, weight=1)

        row = 0
        ctk.CTkLabel(self, text="Broker connection",
                     font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=row, column=0, columnspan=2, padx=20, pady=(20, 12), sticky="w")

        def field(label: str, value: str, show: str | None = None) -> ctk.CTkEntry:
            nonlocal row
            row += 1
            ctk.CTkLabel(self, text=label, anchor="w").grid(
                row=row, column=0, padx=(20, 10), pady=6, sticky="w")
            entry = ctk.CTkEntry(self, show=show)
            entry.insert(0, value)
            entry.grid(row=row, column=1, padx=(0, 20), pady=6, sticky="ew")
            return entry

        self.host_entry = field("Host", str(broker.get("host", "")))
        self.port_entry = field("Port", str(broker.get("port", 8883)))
        self.user_entry = field("Username", str(broker.get("username", "")))
        self.pass_entry = field("Password", password, show="•")

        row += 1
        self.show_pass = ctk.CTkCheckBox(self, text="Show password",
                                         command=self._toggle_password)
        self.show_pass.grid(row=row, column=1, padx=(0, 20), pady=(0, 6), sticky="w")

        self.keepalive_entry = field("Keepalive (s)", str(broker.get("keepalive", 60)))
        self.client_entry = field("Client ID (blank = random)", str(broker.get("client_id", "")))

        row += 1
        self.tls_check = ctk.CTkCheckBox(self, text="Use TLS / encryption")
        self.tls_check.grid(row=row, column=1, padx=(0, 20), pady=(12, 4), sticky="w")
        if broker.get("use_tls", True):
            self.tls_check.select()

        row += 1
        self.cert_check = ctk.CTkCheckBox(self, text="Validate server certificate")
        self.cert_check.grid(row=row, column=1, padx=(0, 20), pady=4, sticky="w")
        if broker.get("validate_cert", False):
            self.cert_check.select()

        row += 1
        self.status = ctk.CTkLabel(self, text="", anchor="w", wraplength=460,
                                   justify="left", font=ctk.CTkFont(size=12))
        self.status.grid(row=row, column=0, columnspan=2, padx=20, pady=(12, 4), sticky="w")

        row += 1
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=row, column=0, columnspan=2, padx=20, pady=(8, 18), sticky="ew")
        buttons.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(buttons, text="Test connection", width=140,
                      fg_color="transparent", border_width=1,
                      command=self._test).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(buttons, text="Cancel", width=90, fg_color="transparent",
                      border_width=1, command=self.destroy).grid(row=0, column=1, padx=(0, 8))
        ctk.CTkButton(buttons, text="Save", width=90,
                      command=self._save).grid(row=0, column=2)

        self.after(150, self._grab)

    def _grab(self) -> None:
        try:
            self.grab_set()
            self.focus_force()
        except Exception:
            pass

    def _toggle_password(self) -> None:
        self.pass_entry.configure(show="" if self.show_pass.get() else "•")

    def _collect(self) -> tuple[dict, str] | None:
        host = self.host_entry.get().strip()
        if not host:
            self.status.configure(text="Host cannot be empty.", text_color="#e03131")
            return None
        try:
            port = int(self.port_entry.get().strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            self.status.configure(text="Port must be a number between 1 and 65535.",
                                  text_color="#e03131")
            return None
        try:
            keepalive = max(5, int(self.keepalive_entry.get().strip() or 60))
        except ValueError:
            self.status.configure(text="Keepalive must be a whole number of seconds.",
                                  text_color="#e03131")
            return None

        broker = {
            "host": host,
            "port": port,
            "username": self.user_entry.get().strip(),
            "use_tls": bool(self.tls_check.get()),
            "validate_cert": bool(self.cert_check.get()),
            "keepalive": keepalive,
            "client_id": self.client_entry.get().strip(),
        }
        return broker, self.pass_entry.get()

    def _test(self) -> None:
        collected = self._collect()
        if collected is None:
            return
        broker, password = collected
        self.status.configure(text="Testing...", text_color="#868e96")

        def worker() -> None:
            probe = MqttManager(lambda *a: None)
            probe.configure(broker, password)
            ok, msg = probe.connect(timeout=CONNECT_TIMEOUT_S)
            probe.disconnect()
            self.after(0, lambda: self.status.configure(
                text=("OK - " if ok else "Failed - ") + msg,
                text_color="#2f9e44" if ok else "#e03131"))

        threading.Thread(target=worker, daemon=True).start()

    def _save(self) -> None:
        collected = self._collect()
        if collected is None:
            return
        self.result = collected
        self.app.apply_broker_settings(*collected)
        self.destroy()


# ============================================================================
# Passphrase prompt - for encrypted profile export / import
# ============================================================================


class PassphraseDialog(ctk.CTkToplevel):
    """Asks for a passphrase. Blocks until closed; read .result afterwards.

    result is None if cancelled, otherwise (passphrase, include_password).
    """

    MIN_LENGTH = 8

    def __init__(self, master, title: str, explain: str, confirm: bool,
                 offer_password: bool = False) -> None:
        super().__init__(master)
        self.result: tuple[str, bool] | None = None
        self._confirm = confirm

        self.title(title)
        self.geometry("520x330" if confirm else "520x250")
        self.resizable(False, False)
        self.transient(master)
        self.grid_columnconfigure(0, weight=1)

        row = 0
        ctk.CTkLabel(self, text=title,
                     font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=row, column=0, padx=20, pady=(20, 6), sticky="w")

        row += 1
        ctk.CTkLabel(self, text=explain, anchor="w", justify="left", wraplength=470,
                     font=ctk.CTkFont(size=12)).grid(
            row=row, column=0, padx=20, pady=(0, 12), sticky="w")

        row += 1
        self.entry = ctk.CTkEntry(self, show="•", placeholder_text="Passphrase")
        self.entry.grid(row=row, column=0, padx=20, pady=4, sticky="ew")

        self.entry2 = None
        if confirm:
            row += 1
            self.entry2 = ctk.CTkEntry(self, show="•", placeholder_text="Repeat passphrase")
            self.entry2.grid(row=row, column=0, padx=20, pady=4, sticky="ew")

        row += 1
        self.show = ctk.CTkCheckBox(self, text="Show passphrase", command=self._toggle)
        self.show.grid(row=row, column=0, padx=20, pady=(6, 4), sticky="w")

        self.include_password = None
        if offer_password:
            row += 1
            self.include_password = ctk.CTkCheckBox(
                self, text="Include the broker password in the file")
            self.include_password.grid(row=row, column=0, padx=20, pady=4, sticky="w")

        row += 1
        self.status = ctk.CTkLabel(self, text="", anchor="w", wraplength=470,
                                   justify="left", font=ctk.CTkFont(size=12))
        self.status.grid(row=row, column=0, padx=20, pady=(6, 2), sticky="w")

        row += 1
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=row, column=0, padx=20, pady=(6, 18), sticky="ew")
        buttons.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(buttons, text="Cancel", width=90, fg_color="transparent",
                      border_width=1, command=self.destroy).grid(row=0, column=1, padx=(0, 8))
        ctk.CTkButton(buttons, text="OK", width=90, command=self._ok).grid(row=0, column=2)

        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.after(150, self._grab)

    def _grab(self) -> None:
        try:
            self.grab_set()
            self.entry.focus_force()
        except Exception:
            pass

    def _toggle(self) -> None:
        show = "" if self.show.get() else "•"
        self.entry.configure(show=show)
        if self.entry2 is not None:
            self.entry2.configure(show=show)

    def _ok(self) -> None:
        passphrase = self.entry.get()
        if not passphrase:
            self.status.configure(text="Enter a passphrase.", text_color="#e03131")
            return
        if self._confirm:
            if len(passphrase) < self.MIN_LENGTH:
                self.status.configure(
                    text=f"Use at least {self.MIN_LENGTH} characters - this passphrase is "
                         f"the only thing protecting the file.", text_color="#e03131")
                return
            if passphrase != self.entry2.get():
                self.status.configure(text="The two passphrases do not match.",
                                      text_color="#e03131")
                return
        include = bool(self.include_password.get()) if self.include_password else False
        self.result = (passphrase, include)
        self.destroy()

    @classmethod
    def ask(cls, master, title: str, explain: str, confirm: bool,
            offer_password: bool = False) -> tuple[str, bool] | None:
        dialog = cls(master, title, explain, confirm, offer_password)
        master.wait_window(dialog)
        return dialog.result


# ============================================================================
# Main application
# ============================================================================


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        self.secrets = SecretStore()
        self.vault = LocalVault(self.secrets)
        self.config_data = Config.load(self.vault)
        self.password = self.secrets.get(SecretStore.key_for(self.config_data.broker))

        self.events: queue.Queue = queue.Queue()
        self.mqtt = MqttManager(self._emit_from_thread)
        self.mqtt.configure(self.config_data.broker, self.password)

        self.runners: dict[str, LoopRunner] = {}
        self.run_state: dict[str, dict] = {}     # preset_id -> {sent, failed, next_at}
        self._restart_pending: set[str] = set()  # loops to relaunch once stopped
        self._pending_reconnect = False          # drop the connection first?
        self.preset_buttons: dict[str, ctk.CTkButton] = {}
        self.selected_id: str | None = None
        self._dirty = False

        ctk.set_appearance_mode(self.config_data.appearance)
        ctk.set_default_color_theme("blue")

        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1150x800")
        self.minsize(960, 640)

        self._build_ui()
        self._refresh_preset_list()

        first = self.config_data.find(self.config_data.last_selected) or self.config_data.presets[0]
        self._select_preset(first.id)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind(f"<{MODIFIER}-s>", lambda _e: self._save_preset())
        self.bind(f"<{MODIFIER}-Return>", lambda _e: self._send_once())

        self.log("activity", "info", f"{APP_NAME} {APP_VERSION} ready. "
                                     f"Not connected - press Connect or Start a loop.")
        self.log("activity", "info", f"Shortcuts: {MODIFIER_LABEL}+S saves the current "
                                     f"message, {MODIFIER_LABEL}+Enter sends it once.")
        self.log("debug", "info", f"Platform: {sys.platform} | Settings file: {CONFIG_FILE}")
        if self.secrets.available:
            self.log("debug", "info", f"Credential store: {self.secrets.backend_name}")
        else:
            self.log("debug", "warn",
                     f"Credential store unavailable ({self.secrets.error}). "
                     f"Passwords will not persist between launches.")
        if self.vault.available:
            self.log("debug", "info",
                     "Settings are encrypted at rest; the key is held in the "
                     "credential store and stays on this machine.")
        else:
            self.log("debug", "warn",
                     f"Settings are NOT encrypted: {self.vault.error}.")
        if self.config_data.load_error:
            self.log("debug", "err", self.config_data.load_error)
            self.log("activity", "err", self.config_data.load_error)
        if self.config_data.migrated_from_plaintext:
            self.log("debug", "info",
                     f"Imported settings from the old plain-text "
                     f"{LEGACY_CONFIG_FILE.name}. It is deleted once the encrypted "
                     f"copy is written.")
            self.config_data.save()

        self.after(120, self._pump_events)
        self.after(500, self._tick_ui)
        if not self.config_data.broker.get("host"):
            self.after(700, self._prompt_first_run)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=3)
        self.grid_rowconfigure(2, weight=2)

        self._build_header()
        self._build_body()
        self._build_log()
        self._build_statusbar()

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, corner_radius=0, height=64)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(header, text=APP_NAME,
                     font=ctk.CTkFont(size=20, weight="bold")).grid(
            row=0, column=0, padx=(20, 12), pady=14, sticky="w")

        self.conn_label = ctk.CTkLabel(header, text="●  Disconnected",
                                       font=ctk.CTkFont(size=13), text_color="#868e96")
        self.conn_label.grid(row=0, column=1, padx=8, pady=14, sticky="w")

        self.connect_btn = ctk.CTkButton(header, text="Connect", width=110,
                                         command=self._toggle_connection)
        self.connect_btn.grid(row=0, column=2, padx=6, pady=14)

        ctk.CTkButton(header, text="Broker settings", width=130, fg_color="transparent",
                      border_width=1, command=self._open_broker_dialog).grid(
            row=0, column=3, padx=6, pady=14)

        # Moving a setup between machines. The encrypted settings file cannot
        # simply be copied - its key never leaves the machine that made it.
        self.profile_menu = ctk.CTkOptionMenu(
            header, width=110, values=[PROFILE_EXPORT, PROFILE_IMPORT],
            command=self._on_profile_action)
        self.profile_menu.set("Profile")
        self.profile_menu.grid(row=0, column=4, padx=6, pady=14)

        self.theme_switch = ctk.CTkSegmentedButton(
            header, values=["System", "Light", "Dark"], width=210,
            command=self._on_theme_change)
        self.theme_switch.set(self.config_data.appearance)
        self.theme_switch.grid(row=0, column=5, padx=(6, 20), pady=14)

    def _build_body(self) -> None:
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=16, pady=(12, 6))
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        # ---- left: preset list -----------------------------------------
        left = ctk.CTkFrame(body, width=280)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.grid_propagate(False)
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(left, text="SAVED MESSAGES", anchor="w",
                     font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=0, column=0, padx=14, pady=(14, 6), sticky="ew")

        self.preset_list = ctk.CTkScrollableFrame(left, fg_color="transparent")
        self.preset_list.grid(row=1, column=0, sticky="nsew", padx=6, pady=0)
        self.preset_list.grid_columnconfigure(0, weight=1)

        btns = ctk.CTkFrame(left, fg_color="transparent")
        btns.grid(row=2, column=0, sticky="ew", padx=10, pady=10)
        btns.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(btns, text="+ New", command=self._new_preset).grid(
            row=0, column=0, padx=(0, 4), pady=3, sticky="ew")
        ctk.CTkButton(btns, text="Duplicate", fg_color="transparent", border_width=1,
                      command=self._duplicate_preset).grid(
            row=0, column=1, padx=(4, 0), pady=3, sticky="ew")
        ctk.CTkButton(btns, text="Delete", fg_color="transparent", border_width=1,
                      text_color=("#c92a2a", "#ff6b6b"), hover_color=("#ffe3e3", "#4d1f1f"),
                      command=self._delete_preset).grid(
            row=1, column=0, padx=(0, 4), pady=3, sticky="ew")
        ctk.CTkButton(btns, text="Stop all", fg_color="transparent", border_width=1,
                      command=self._stop_all).grid(
            row=1, column=1, padx=(4, 0), pady=3, sticky="ew")

        # ---- right: editor ---------------------------------------------
        right = ctk.CTkFrame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(1, weight=1)
        right.grid_rowconfigure(3, weight=1)

        ctk.CTkLabel(right, text="Name", anchor="w", width=90).grid(
            row=0, column=0, padx=(16, 8), pady=(16, 4), sticky="w")
        self.name_entry = ctk.CTkEntry(right)
        self.name_entry.grid(row=0, column=1, padx=(0, 16), pady=(16, 4), sticky="ew")
        self.name_entry.bind("<KeyRelease>", self._mark_dirty)

        ctk.CTkLabel(right, text="Topic", anchor="w", width=90).grid(
            row=1, column=0, padx=(16, 8), pady=4, sticky="w")
        self.topic_entry = ctk.CTkEntry(right, font=ctk.CTkFont(family=MONO_FONT, size=13))
        self.topic_entry.grid(row=1, column=1, padx=(0, 16), pady=4, sticky="ew")
        self.topic_entry.bind("<KeyRelease>", self._mark_dirty)

        payload_head = ctk.CTkFrame(right, fg_color="transparent")
        payload_head.grid(row=2, column=0, columnspan=2, padx=16, pady=(10, 2), sticky="ew")
        payload_head.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(payload_head, text="Payload", anchor="w",
                     font=ctk.CTkFont(size=13, weight="bold")).grid(row=0, column=0, sticky="w")
        self.json_status = ctk.CTkLabel(payload_head, text="", anchor="e",
                                        font=ctk.CTkFont(size=12))
        self.json_status.grid(row=0, column=1, sticky="e", padx=8)
        ctk.CTkButton(payload_head, text="Format JSON", width=110, height=26,
                      fg_color="transparent", border_width=1,
                      command=self._format_json).grid(row=0, column=2)

        self.payload_box = ctk.CTkTextbox(right, font=ctk.CTkFont(family=MONO_FONT, size=13),
                                          wrap="none", undo=True)
        self.payload_box.grid(row=3, column=0, columnspan=2, padx=16, pady=(0, 8), sticky="nsew")
        self.payload_box.bind("<KeyRelease>", self._on_payload_change)

        # interval / qos / retain
        opts = ctk.CTkFrame(right, fg_color="transparent")
        opts.grid(row=4, column=0, columnspan=2, padx=16, pady=(0, 8), sticky="ew")

        ctk.CTkLabel(opts, text="Send every").grid(row=0, column=0, padx=(0, 8))
        self.interval_entry = ctk.CTkEntry(opts, width=80, justify="right")
        self.interval_entry.grid(row=0, column=1)
        self.interval_entry.bind("<KeyRelease>", self._mark_dirty)
        ctk.CTkLabel(opts, text="seconds").grid(row=0, column=2, padx=(6, 10))
        self.interval_preset = ctk.CTkOptionMenu(
            opts, width=110, values=["1 s", "5 s", "10 s", "30 s", "1 min", "5 min", "15 min"],
            command=self._apply_interval_preset)
        self.interval_preset.set("Quick set")
        self.interval_preset.grid(row=0, column=3, padx=(0, 24))

        ctk.CTkLabel(opts, text="QoS").grid(row=0, column=4, padx=(0, 6))
        self.qos_menu = ctk.CTkOptionMenu(opts, width=70, values=["0", "1", "2"],
                                          command=lambda _v: self._mark_dirty())
        self.qos_menu.grid(row=0, column=5, padx=(0, 16))
        self.retain_check = ctk.CTkCheckBox(opts, text="Retain", command=self._mark_dirty)
        self.retain_check.grid(row=0, column=6)

        # action buttons
        actions = ctk.CTkFrame(right, fg_color="transparent")
        actions.grid(row=5, column=0, columnspan=2, padx=16, pady=(0, 14), sticky="ew")
        actions.grid_columnconfigure(4, weight=1)

        self.start_btn = ctk.CTkButton(actions, text="▶  Start loop", width=130,
                                       fg_color="#2f9e44", hover_color="#268a3a",
                                       command=self._start_loop)
        self.start_btn.grid(row=0, column=0, padx=(0, 8))
        self.stop_btn = ctk.CTkButton(actions, text="■  Stop", width=100,
                                      fg_color="#c92a2a", hover_color="#a51f1f",
                                      state="disabled", command=self._stop_loop)
        self.stop_btn.grid(row=0, column=1, padx=(0, 8))
        ctk.CTkButton(actions, text="Send once", width=110, fg_color="transparent",
                      border_width=1, command=self._send_once).grid(row=0, column=2, padx=(0, 8))
        self.save_btn = ctk.CTkButton(actions, text="Save", width=100, fg_color="transparent",
                                      border_width=1, command=self._save_preset)
        self.save_btn.grid(row=0, column=3)

        self.preset_status = ctk.CTkLabel(actions, text="Idle", anchor="e",
                                          font=ctk.CTkFont(size=12))
        self.preset_status.grid(row=0, column=4, sticky="e", padx=8)

    def _build_log(self) -> None:
        wrapper = ctk.CTkFrame(self, fg_color="transparent")
        wrapper.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 8))
        wrapper.grid_columnconfigure(0, weight=1)
        wrapper.grid_rowconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(wrapper, height=220)
        self.tabs.grid(row=0, column=0, sticky="nsew")
        self.tabs.add("Activity")
        self.tabs.add("Debug")

        self.log_boxes: dict[str, ctk.CTkTextbox] = {}
        for tab, channel in (("Activity", "activity"), ("Debug", "debug")):
            frame = self.tabs.tab(tab)
            frame.grid_columnconfigure(0, weight=1)
            frame.grid_rowconfigure(0, weight=1)
            box = ctk.CTkTextbox(frame, font=ctk.CTkFont(family=MONO_FONT, size=12),
                                 wrap="none", state="disabled")
            box.grid(row=0, column=0, sticky="nsew")
            self.log_boxes[channel] = box

        controls = ctk.CTkFrame(wrapper, fg_color="transparent")
        controls.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        controls.grid_columnconfigure(0, weight=1)
        self.autoscroll_check = ctk.CTkCheckBox(controls, text="Auto-scroll",
                                                command=self._toggle_autoscroll)
        if self.config_data.autoscroll:
            self.autoscroll_check.select()
        self.autoscroll_check.grid(row=0, column=0, sticky="w")
        ctk.CTkButton(controls, text="Export log...", width=110, height=28,
                      fg_color="transparent", border_width=1,
                      command=self._export_log).grid(row=0, column=1, padx=6)
        ctk.CTkButton(controls, text="Clear", width=80, height=28, fg_color="transparent",
                      border_width=1, command=self._clear_log).grid(row=0, column=2)

        self._apply_log_colors()

    def _build_statusbar(self) -> None:
        bar = ctk.CTkFrame(self, corner_radius=0, height=30)
        bar.grid(row=3, column=0, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        self.status_left = ctk.CTkLabel(bar, text="Ready", anchor="w",
                                        font=ctk.CTkFont(size=12))
        self.status_left.grid(row=0, column=0, padx=16, pady=4, sticky="w")
        self.status_right = ctk.CTkLabel(bar, text="0 loops running", anchor="e",
                                         font=ctk.CTkFont(size=12))
        self.status_right.grid(row=0, column=1, padx=16, pady=4, sticky="e")

    # -------------------------------------------------------------- theme
    def _on_theme_change(self, value: str) -> None:
        ctk.set_appearance_mode(value)
        self.config_data.appearance = value
        self._apply_log_colors()
        self.config_data.save()
        self._refresh_preset_list()

    def _apply_log_colors(self) -> None:
        mode = ctk.get_appearance_mode()
        colors = LOG_COLORS.get(mode, LOG_COLORS["Dark"])
        for box in self.log_boxes.values():
            for tag, color in colors.items():
                box.tag_config(tag, foreground=color)

    # --------------------------------------------------------------- log
    def log(self, channel: str, level: str, text: str) -> None:
        """Append a line to a log tab. Main thread only."""
        box = self.log_boxes.get(channel)
        if box is None:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        box.configure(state="normal")
        box.insert("end", f"{stamp}  ", "ts")
        box.insert("end", text + "\n", level)

        line_count = int(box.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES:
            box.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")
        box.configure(state="disabled")
        if self.config_data.autoscroll:
            box.see("end")

    def _emit_from_thread(self, channel: str, level: str, text: str) -> None:
        self.events.put({"kind": "log", "channel": channel, "level": level, "text": text})

    def _clear_log(self) -> None:
        channel = "activity" if self.tabs.get() == "Activity" else "debug"
        box = self.log_boxes[channel]
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.configure(state="disabled")

    def _toggle_autoscroll(self) -> None:
        self.config_data.autoscroll = bool(self.autoscroll_check.get())
        self.config_data.save()

    def _export_log(self) -> None:
        channel = "activity" if self.tabs.get() == "Activity" else "debug"
        default = f"mqtt-{channel}-{datetime.now():%Y%m%d-%H%M%S}.log"
        path = filedialog.asksaveasfilename(
            parent=self, title="Export log", initialfile=default,
            defaultextension=".log", filetypes=[("Log files", "*.log"), ("All files", "*.*")])
        if not path:
            return
        try:
            Path(path).write_text(self.log_boxes[channel].get("1.0", "end"), encoding="utf-8")
            self.status_left.configure(text=f"Log exported to {path}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not write file:\n{exc}", parent=self)

    # ----------------------------------------------------------- presets
    def _refresh_preset_list(self) -> None:
        for widget in self.preset_list.winfo_children():
            widget.destroy()
        self.preset_buttons.clear()

        for i, preset in enumerate(self.config_data.presets):
            running = preset.id in self.runners
            selected = preset.id == self.selected_id
            marker = "●" if running else "○"
            btn = ctk.CTkButton(
                self.preset_list,
                text=f"{marker}  {preset.name}",
                anchor="w", height=36, corner_radius=6,
                fg_color=("#dbe4ff", "#2b3a55") if selected else "transparent",
                hover_color=("#e7ecff", "#33425e"),
                text_color=("#1a1a1a", "#f0f0f0"),
                command=lambda pid=preset.id: self._select_preset(pid),
            )
            btn.grid(row=i, column=0, sticky="ew", pady=2, padx=2)
            self.preset_buttons[preset.id] = btn

    def _update_preset_button(self, preset_id: str) -> None:
        preset = self.config_data.find(preset_id)
        btn = self.preset_buttons.get(preset_id)
        if preset is None or btn is None:
            return
        marker = "●" if preset_id in self.runners else "○"
        selected = preset_id == self.selected_id
        btn.configure(text=f"{marker}  {preset.name}",
                      fg_color=("#dbe4ff", "#2b3a55") if selected else "transparent")

    def _select_preset(self, preset_id: str) -> None:
        if self._dirty and self.selected_id and self.selected_id != preset_id:
            keep = messagebox.askyesnocancel(
                APP_NAME, "Save changes to the current message first?", parent=self)
            if keep is None:
                return
            if keep:
                self._save_preset()

        self.selected_id = preset_id
        preset = self.config_data.find(preset_id)
        if preset is None:
            return

        self.name_entry.delete(0, "end")
        self.name_entry.insert(0, preset.name)
        self.topic_entry.delete(0, "end")
        self.topic_entry.insert(0, preset.topic)
        self.payload_box.delete("1.0", "end")
        self.payload_box.insert("1.0", preset.payload)
        self.interval_entry.delete(0, "end")
        self.interval_entry.insert(0, self._format_interval(preset.interval))
        self.qos_menu.set(str(preset.qos))
        if preset.retain:
            self.retain_check.select()
        else:
            self.retain_check.deselect()

        self.config_data.last_selected = preset_id
        self._dirty = False
        self.save_btn.configure(text="Save")
        self._validate_json()
        self._refresh_preset_list()
        self._update_action_buttons()

    @staticmethod
    def _format_interval(value: float) -> str:
        return str(int(value)) if float(value).is_integer() else f"{value:g}"

    def _mark_dirty(self, _event=None) -> None:
        self._dirty = True
        self.save_btn.configure(text="Save *")

    def _on_payload_change(self, _event=None) -> None:
        self._mark_dirty()
        self._validate_json()

    def _validate_json(self) -> bool:
        text = self.payload_box.get("1.0", "end").strip()
        if not text:
            self.json_status.configure(text="empty payload", text_color="#e8590c")
            return False
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            self.json_status.configure(text=f"not valid JSON - line {exc.lineno}: {exc.msg}",
                                       text_color="#e8590c")
            return False
        self.json_status.configure(text="valid JSON", text_color="#2f9e44")
        return True

    def _format_json(self) -> None:
        text = self.payload_box.get("1.0", "end").strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            messagebox.showwarning(APP_NAME, f"Cannot format - payload is not valid JSON.\n\n"
                                             f"Line {exc.lineno}: {exc.msg}", parent=self)
            return
        self.payload_box.delete("1.0", "end")
        self.payload_box.insert("1.0", json.dumps(parsed, indent=2))
        self._on_payload_change()

    def _apply_interval_preset(self, value: str) -> None:
        seconds = {"1 s": 1, "5 s": 5, "10 s": 10, "30 s": 30,
                   "1 min": 60, "5 min": 300, "15 min": 900}.get(value)
        if seconds is None:
            return
        self.interval_entry.delete(0, "end")
        self.interval_entry.insert(0, str(seconds))
        self.interval_preset.set("Quick set")
        self._mark_dirty()

    def _read_editor(self) -> Preset | None:
        """Validate the editor fields and return them as a Preset (unsaved)."""
        preset = self.config_data.find(self.selected_id)
        if preset is None:
            return None

        name = self.name_entry.get().strip() or "Untitled"
        topic = self.topic_entry.get().strip()
        if not topic:
            messagebox.showwarning(APP_NAME, "Topic cannot be empty.", parent=self)
            return None
        payload = self.payload_box.get("1.0", "end").strip()
        if not payload:
            messagebox.showwarning(APP_NAME, "Payload cannot be empty.", parent=self)
            return None

        raw = self.interval_entry.get().strip().replace(",", ".")
        try:
            interval = float(raw)
        except ValueError:
            messagebox.showwarning(APP_NAME, f"'{raw}' is not a valid number of seconds.",
                                   parent=self)
            return None
        if interval < MIN_INTERVAL_S:
            messagebox.showwarning(
                APP_NAME, f"Interval must be at least {MIN_INTERVAL_S} seconds.", parent=self)
            return None

        return Preset(id=preset.id, name=name, topic=topic, payload=payload,
                      interval=interval, qos=int(self.qos_menu.get()),
                      retain=bool(self.retain_check.get()))

    def _save_preset(self) -> bool:
        edited = self._read_editor()
        if edited is None:
            return False

        if not self._validate_json():
            proceed = messagebox.askyesno(
                APP_NAME,
                "The payload is not valid JSON. Save and send it as raw text anyway?",
                parent=self)
            if not proceed:
                return False

        index = next((i for i, p in enumerate(self.config_data.presets)
                      if p.id == edited.id), None)
        if index is None:
            return False
        self.config_data.presets[index] = edited

        ok, detail = self.config_data.save()
        if not ok:
            messagebox.showerror(APP_NAME, f"Could not save config:\n{detail}", parent=self)
            return False

        self._dirty = False
        self.save_btn.configure(text="Save")
        self._update_preset_button(edited.id)
        self.status_left.configure(text=f"Saved '{edited.name}'")

        # A running loop keeps its old settings until restarted - do that for
        # the user so what is on screen is what is being sent.
        if edited.id in self.runners:
            self.log("activity", "info",
                     f"[{edited.name}] settings changed - restarting loop")
            self._restart_pending.add(edited.id)
            self._stop_loop(preset_id=edited.id, silent=True)
        return True

    def _new_preset(self) -> None:
        preset = Preset(name=f"Message {len(self.config_data.presets) + 1}")
        self.config_data.presets.append(preset)
        self.config_data.save()
        self._dirty = False
        self._refresh_preset_list()
        self._select_preset(preset.id)
        self.name_entry.focus_set()

    def _duplicate_preset(self) -> None:
        source = self.config_data.find(self.selected_id)
        if source is None:
            return
        copy = Preset(name=f"{source.name} (copy)", topic=source.topic,
                      payload=source.payload, interval=source.interval,
                      qos=source.qos, retain=source.retain)
        self.config_data.presets.insert(
            self.config_data.presets.index(source) + 1, copy)
        self.config_data.save()
        self._dirty = False
        self._refresh_preset_list()
        self._select_preset(copy.id)

    def _delete_preset(self) -> None:
        preset = self.config_data.find(self.selected_id)
        if preset is None:
            return
        if len(self.config_data.presets) == 1:
            messagebox.showinfo(APP_NAME, "At least one saved message must remain.",
                                parent=self)
            return
        if not messagebox.askyesno(APP_NAME, f"Delete '{preset.name}'?", parent=self):
            return
        if preset.id in self.runners:
            self._stop_loop(preset_id=preset.id, silent=True)
        index = self.config_data.presets.index(preset)
        self.config_data.presets.remove(preset)
        self.config_data.save()
        self._dirty = False
        self._refresh_preset_list()
        neighbour = self.config_data.presets[max(0, index - 1)]
        self._select_preset(neighbour.id)

    # -------------------------------------------------------- connection
    def _toggle_connection(self) -> None:
        if self.mqtt.is_connected:
            if self.runners and not messagebox.askyesno(
                    APP_NAME, f"{len(self.runners)} loop(s) are running. "
                              f"Disconnecting will stop them. Continue?", parent=self):
                return
            self._stop_all(silent=True)
            self.mqtt.disconnect()
            self._set_connection_ui(False)
            self.log("activity", "info", "Disconnected from broker.")
            return

        if not self._require_broker():
            return

        self.connect_btn.configure(state="disabled", text="Connecting...")
        self.status_left.configure(text="Connecting...")

        def worker() -> None:
            ok, msg = self.mqtt.connect()
            self.events.put({"kind": "connect_result", "ok": ok, "message": msg})

        threading.Thread(target=worker, daemon=True).start()

    def _set_connection_ui(self, connected: bool) -> None:
        if connected:
            self.conn_label.configure(
                text=f"●  Connected — {self.config_data.broker['host']}",
                text_color="#2f9e44")
            self.connect_btn.configure(text="Disconnect", state="normal")
        else:
            self.conn_label.configure(text="●  Disconnected", text_color="#868e96")
            self.connect_btn.configure(text="Connect", state="normal")

    def _require_broker(self) -> bool:
        """True if a broker is configured; otherwise nudges the user and returns False."""
        if self.config_data.broker.get("host"):
            return True
        self.log("activity", "warn", "No broker host set - open Broker settings first.")
        self.status_left.configure(text="No broker configured")
        if messagebox.askyesno(APP_NAME,
                               "No broker is configured yet.\n\nOpen Broker settings now?",
                               parent=self):
            self._open_broker_dialog()
        return False

    def _open_broker_dialog(self) -> None:
        BrokerDialog(self, self.config_data.broker, self.password)

    def apply_broker_settings(self, broker: dict, password: str) -> None:
        changed = broker != self.config_data.broker or password != self.password
        self.config_data.broker = broker
        self.password = password
        self.mqtt.configure(broker, password)

        ok, detail = self.config_data.save()
        if not ok:
            messagebox.showerror(APP_NAME, f"Could not save config:\n{detail}", parent=self)

        stored, message = self.secrets.set(SecretStore.key_for(broker), password)
        self.log("debug", "ok" if stored else "warn", message)

        if changed and self.mqtt.is_connected:
            self.log("activity", "info", "Broker settings changed - reconnecting.")
            if self.runners:
                # Drop the old connection only once every loop using it has
                # exited, then relaunch them against the new settings.
                self._pending_reconnect = True
                self._restart_pending.update(self.runners.keys())
                self._stop_all(silent=True)
            else:
                self.mqtt.disconnect()
                self._set_connection_ui(False)
        self.status_left.configure(text="Broker settings saved")

    # -------------------------------------------------- first run / profiles
    def _prompt_first_run(self) -> None:
        """No broker configured yet - offer the two ways to get one."""
        self.log("activity", "info",
                 "No broker configured yet. Open Broker settings to enter one, or "
                 "use Profile > Import profile if a colleague sent you an encrypted "
                 "profile file.")
        wants_import = messagebox.askyesno(
            APP_NAME,
            f"{APP_NAME} ships without any broker details - you supply your own, and "
            f"they stay encrypted on this machine.\n\n"
            f"Do you have a profile file to import?\n\n"
            f"Yes  -  import an encrypted .mqttprofile\n"
            f"No   -  type the broker details in yourself",
            parent=self)
        if wants_import:
            self._import_profile()
        else:
            self._open_broker_dialog()

    def _on_profile_action(self, choice: str) -> None:
        self.profile_menu.set("Profile")
        if choice == PROFILE_EXPORT:
            self._export_profile()
        elif choice == PROFILE_IMPORT:
            self._import_profile()

    def _export_profile(self) -> None:
        answer = PassphraseDialog.ask(
            self, "Export profile",
            "Writes your broker settings and saved messages to a single encrypted "
            "file, so you can move them to another machine or hand them to a "
            "colleague. The file is unreadable without this passphrase - there is no "
            "recovery if you forget it.",
            confirm=True, offer_password=bool(self.password))
        if answer is None:
            return
        passphrase, include_password = answer

        payload = self.config_data.to_dict()
        payload.pop("last_selected", None)
        payload["exported_by"] = f"{APP_NAME} {APP_VERSION}"
        payload["exported_at"] = datetime.now().isoformat(timespec="seconds")
        if include_password:
            payload["password"] = self.password

        default = f"mqtt-trigger-profile-{datetime.now():%Y%m%d}{PROFILE_SUFFIX}"
        path = filedialog.asksaveasfilename(
            parent=self, title="Export profile", initialfile=default,
            defaultextension=PROFILE_SUFFIX,
            filetypes=[("MQTT Trigger profile", f"*{PROFILE_SUFFIX}"), ("All files", "*.*")])
        if not path:
            return
        try:
            Path(path).write_bytes(ProfileCrypto.encrypt(payload, passphrase))
        except ProfileCrypto.Error as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not write the profile:\n{exc}", parent=self)
            return

        note = " (including the broker password)" if include_password else \
               " (without the broker password)"
        self.status_left.configure(text=f"Profile exported to {path}")
        self.log("activity", "ok", f"Profile exported{note} to {path}")

    def _import_profile(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="Import profile",
            filetypes=[("MQTT Trigger profile", f"*{PROFILE_SUFFIX}"), ("All files", "*.*")])
        if not path:
            return
        try:
            blob = Path(path).read_bytes()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not read the file:\n{exc}", parent=self)
            return

        answer = PassphraseDialog.ask(
            self, "Import profile",
            f"Enter the passphrase for {Path(path).name}. Its broker settings and "
            f"messages will replace what is currently in the app.",
            confirm=False)
        if answer is None:
            return

        try:
            data = ProfileCrypto.decrypt(blob, answer[0])
        except ProfileCrypto.Error as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return

        presets = [Preset.from_dict(p) for p in data.get("presets", []) if isinstance(p, dict)]
        broker = data.get("broker") or {}
        if not isinstance(broker, dict) or not broker.get("host"):
            messagebox.showerror(APP_NAME, "That profile has no broker settings in it.",
                                 parent=self)
            return

        if self.runners and not messagebox.askyesno(
                APP_NAME, f"{len(self.runners)} loop(s) are running. Importing stops "
                          f"them. Continue?", parent=self):
            return
        self._stop_all(silent=True)

        self.config_data.presets = presets or [Config._seed_preset()]
        self.config_data.last_selected = None
        password = data.get("password") or ""
        merged = {**DEFAULT_BROKER, **{k: broker[k] for k in broker if k in DEFAULT_BROKER}}

        self._refresh_preset_list()
        self._select_preset(self.config_data.presets[0].id)
        self.apply_broker_settings(merged, password or self.password)

        self.log("activity", "ok",
                 f"Imported {len(self.config_data.presets)} message(s) and broker "
                 f"settings from {Path(path).name}"
                 + (" (password included)" if password else " (no password in file)"))
        if not password:
            self.log("activity", "info",
                     "The profile carried no password - open Broker settings and add "
                     "one before connecting.")
        self.status_left.configure(text=f"Profile imported from {path}")

    def _flush_restarts(self) -> None:
        """Relaunch loops that were stopped only to pick up new settings."""
        pending, self._restart_pending = list(self._restart_pending), set()
        for offset, pid in enumerate(pending):
            self.after(150 + offset * 100, lambda p=pid: self._start_loop(preset_id=p))

    # ---------------------------------------------------------- sending
    def _send_once(self) -> None:
        if not self._require_broker():
            return
        if self._dirty and not self._save_preset():
            return
        preset = self.config_data.find(self.selected_id)
        if preset is None:
            return

        self.status_left.configure(text=f"Sending '{preset.name}'...")

        def worker() -> None:
            if not self.mqtt.is_connected:
                ok, msg = self.mqtt.connect()
                if not ok:
                    self.events.put({"kind": "log", "channel": "activity", "level": "err",
                                     "text": f"[{preset.name}] cannot send - {msg}"})
                    self.events.put({"kind": "connect_result", "ok": False, "message": msg})
                    return
                self.events.put({"kind": "connect_result", "ok": True, "message": msg})

            ok, detail = self.mqtt.publish(preset.topic, preset.payload,
                                           preset.qos, preset.retain)
            flat = " ".join(preset.payload.split())
            if ok:
                self.events.put({
                    "kind": "log", "channel": "activity", "level": "tx",
                    "text": f"TX  {preset.topic}  (QoS {preset.qos}"
                            f"{', retain' if preset.retain else ''})  "
                            f"[{preset.name} - single]\n     {flat}"})
            else:
                self.events.put({"kind": "log", "channel": "activity", "level": "err",
                                 "text": f"FAIL {preset.topic}  [{preset.name}] - {detail}"})

        threading.Thread(target=worker, daemon=True).start()

    def _start_loop(self, preset_id: str | None = None) -> None:
        if preset_id is None:
            if not self._require_broker():
                return
            if self._dirty and not self._save_preset():
                return
            preset_id = self.selected_id
        preset = self.config_data.find(preset_id)
        if preset is None or preset.id in self.runners:
            return

        runner = LoopRunner(preset, self.mqtt, self.events)
        self.runners[preset.id] = runner
        self.run_state[preset.id] = {"sent": 0, "failed": 0, "next_at": None}
        runner.start()

        self.log("activity", "info",
                 f"[{preset.name}] loop started - every {self._format_interval(preset.interval)}s "
                 f"to {preset.topic}")
        self._update_preset_button(preset.id)
        self._update_action_buttons()
        self._update_running_count()

    def _stop_loop(self, preset_id: str | None = None, silent: bool = False) -> None:
        pid = preset_id or self.selected_id
        runner = self.runners.get(pid)
        if runner is None:
            return
        runner.stop()
        if not silent:
            preset = self.config_data.find(pid)
            self.status_left.configure(
                text=f"Stopping '{preset.name if preset else pid}'...")

    def _stop_all(self, silent: bool = False) -> None:
        if not self.runners:
            return
        for pid in list(self.runners):
            self._stop_loop(preset_id=pid, silent=True)
        if not silent:
            self.log("activity", "info", "Stop all - every loop is shutting down.")

    def _update_action_buttons(self) -> None:
        running = self.selected_id in self.runners
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")

    def _update_running_count(self) -> None:
        count = len(self.runners)
        self.status_right.configure(
            text=f"{count} loop{'' if count == 1 else 's'} running")

    # ------------------------------------------------------ event loops
    def _pump_events(self) -> None:
        """Drain worker-thread events on the UI thread."""
        try:
            for _ in range(200):
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        except Exception as exc:  # never let the pump die
            try:
                self.log("debug", "err", f"UI event error: {type(exc).__name__}: {exc}")
            except Exception:
                pass
        finally:
            self.after(120, self._pump_events)

    def _handle_event(self, event: dict) -> None:
        kind = event.get("kind")

        if kind == "log":
            self.log(event["channel"], event["level"], event["text"])

        elif kind == "connect_result":
            ok = event["ok"]
            self._set_connection_ui(ok and self.mqtt.is_connected)
            self.status_left.configure(text=event["message"])
            self.log("activity", "ok" if ok else "err",
                     event["message"] if ok else f"Connection failed - {event['message']}")

        elif kind == "started":
            self._set_connection_ui(self.mqtt.is_connected)

        elif kind == "tick":
            state = self.run_state.setdefault(event["preset_id"], {})
            state.update(sent=event["sent"], failed=event["failed"],
                         next_at=event["next_at"])

        elif kind == "stopped":
            pid = event["preset_id"]
            runner = self.runners.pop(pid, None)
            self.run_state.pop(pid, None)
            preset = self.config_data.find(pid)
            label = preset.name if preset else pid
            if runner is not None:
                self.log("activity", "info",
                         f"[{label}] loop stopped - {runner.sent} sent, {runner.failed} failed")
            self._update_preset_button(pid)
            self._update_action_buttons()
            self._update_running_count()
            self.status_left.configure(text=f"'{label}' stopped")

            if self._pending_reconnect:
                if not self.runners:          # last one out closes the socket
                    self._pending_reconnect = False
                    self.mqtt.disconnect()
                    self._set_connection_ui(False)
                    self._flush_restarts()
            elif pid in self._restart_pending:
                self._restart_pending.discard(pid)
                self.after(150, lambda p=pid: self._start_loop(preset_id=p))

    def _tick_ui(self) -> None:
        """Half-second refresh of the countdown / running indicators."""
        state = self.run_state.get(self.selected_id)
        if self.selected_id in self.runners and state:
            next_at = state.get("next_at")
            remaining = max(0.0, next_at - time.monotonic()) if next_at else 0.0
            self.preset_status.configure(
                text=f"Running — {state.get('sent', 0)} sent"
                     + (f", {state['failed']} failed" if state.get("failed") else "")
                     + f" — next in {remaining:0.0f}s",
                text_color="#2f9e44")
        elif self.selected_id in self.runners:
            self.preset_status.configure(text="Starting...", text_color="#e8590c")
        else:
            self.preset_status.configure(text="Idle", text_color=("#868e96", "#868e96"))
        self.after(500, self._tick_ui)

    # ------------------------------------------------------------ close
    def _on_close(self) -> None:
        if self.runners and not messagebox.askyesno(
                APP_NAME, f"{len(self.runners)} loop(s) are still running.\n\nQuit anyway?",
                parent=self):
            return
        if self._dirty:
            keep = messagebox.askyesnocancel(
                APP_NAME, "Save changes to the current message before quitting?", parent=self)
            if keep is None:
                return
            if keep:
                self._save_preset()

        for runner in list(self.runners.values()):
            runner.stop()
        deadline = time.monotonic() + 2.0
        for runner in list(self.runners.values()):
            runner.join(timeout=max(0.0, deadline - time.monotonic()))
        self.mqtt.disconnect()
        self.config_data.save()
        self.destroy()


def main() -> None:
    try:
        app = App()
        app.mainloop()
    except Exception as exc:
        # A GUI app has no console when frozen - surface crashes in a dialog.
        import traceback
        try:
            import tkinter.messagebox as mb
            mb.showerror(APP_NAME, f"{APP_NAME} failed to start:\n\n"
                                   f"{type(exc).__name__}: {exc}\n\n"
                                   f"{traceback.format_exc()}")
        except Exception:
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
