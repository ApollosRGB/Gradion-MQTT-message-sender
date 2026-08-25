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
APP_VERSION = "2.2.0"
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

# A payload starting with one of these is meant to be JSON, so failing to parse
# is an error. Everything else is raw text and is sent exactly as typed.
JSON_OPENERS = "{["

# One-click payloads for the message editor - the bare words a trigger topic
# carries, with no JSON wrapper around them.
RAW_QUICK_PAYLOADS = ("True", "False")

MIN_INTERVAL_S = 0.1
MAX_LOG_LINES = 2000
CONNECT_TIMEOUT_S = 15

# The two top-level screens.
TAB_MESSAGES = "Messages"
TAB_SIMULATOR = "Robot simulator"

# ---------------------------------------------------------------------------
# Robot simulator
#
# A simulated robot publishes its state to <base>/status and is driven from
# <base>/cmd/trigger - the app never starts a run by itself, it only reacts to
# a trigger message. The state numbers are the contract with whatever reads the
# status topic; the names travel alongside so the topic stays readable to a
# human watching it in MQTT Explorer.
# ---------------------------------------------------------------------------

STATE_STOPPED = (0, "stopped")
STATE_RUNNING = (1, "running")
STATE_FINISHED = (3, "finished")

STATUS_SUFFIX = "/status"
TRIGGER_SUFFIX = "/cmd/trigger"

SIM_DEFAULT_INTERVAL = 5.0
SIM_DEFAULT_STOP_DELAY = 5.0   # finished -> stopped, independent of the tick rate
SIM_TRIGGER_QOS = 1            # QoS the trigger topics are subscribed at

# Payload words accepted on a trigger topic, on top of JSON true / false.
TRIGGER_TRUE_WORDS = {"true", "1", "on", "start", "yes", "run", "running", "go"}
TRIGGER_FALSE_WORDS = {"false", "0", "off", "stop", "no", "stopped", "end",
                       "finish", "finished"}
# Keys read from a JSON object payload, in order, matched case-insensitively.
TRIGGER_KEYS = ("trigger", "value", "command", "cmd", "state", "start")
TRIGGER_QUOTES = "\"'"   # stripped from a bare payload before matching

# Robots the app starts life with. These are simulator topics, not broker
# details - no host, user or password is implied by them.
DEFAULT_ROBOTS = [
    ("Openmind robot01", "Openmind/robot01"),
    ("KUKA robot02", "kuka/robot02"),
]

# Log colours, per appearance mode. tag_config takes a single colour, so these
# get re-applied whenever the theme changes.
LOG_COLORS = {
    "Dark": {
        "ts": "#6b7280", "info": "#9aa0a6", "tx": "#4dabf7",
        "rx": "#b197fc", "ok": "#51cf66", "warn": "#ffa94d", "err": "#ff6b6b",
    },
    "Light": {
        "ts": "#868e96", "info": "#495057", "tx": "#1971c2",
        "rx": "#6741d9", "ok": "#2f9e44", "warn": "#e8590c", "err": "#c92a2a",
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
# Robot simulator helpers
# ============================================================================


def _timestamp() -> str:
    """Now, as local time with milliseconds and this machine's UTC offset.

    Produces e.g. 2026-08-24T14:34:00.680+07:00 - the format the status
    payloads carry.
    """
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _coerce_trigger(value) -> bool | None:
    """True / False if `value` clearly means start or stop, else None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().strip(TRIGGER_QUOTES).lower()
        if word in TRIGGER_TRUE_WORDS:
            return True
        if word in TRIGGER_FALSE_WORDS:
            return False
    return None


def classify_payload(text: str) -> tuple[str, str]:
    """Say what is in a payload box, and give the user a line about it.

    A payload is bytes on a topic, not a JSON document. A bare True or False is
    exactly what a line controller puts on a trigger topic, so raw text is a
    first-class payload here and is published character for character. Only
    text that opens like JSON and then fails to parse is a mistake worth
    flagging - anything else is taken at its word.

    Returns one of "empty" / "broken" / "raw" / "json" with a short detail.
    """
    if not text:
        return "empty", "empty payload"
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        if text[:1] in JSON_OPENERS:
            line = getattr(exc, "lineno", None)
            where = f" - line {line}: {exc.msg}" if line else f" - {exc}"
            return "broken", f"not valid JSON{where}"
        return "raw", "raw text - sent as typed"
    if isinstance(parsed, (dict, list)):
        return "json", "valid JSON"
    # true / false / 42 / "word" - valid JSON, but on the wire it is the same
    # bare token the reader sees either way, so call it what it is.
    return "raw", "raw value - sent as typed"


def parse_trigger(payload: str) -> bool | None:
    """Read a trigger payload. None means "not understood - do nothing".

    Accepts {"trigger": "true"} as sent by the line controller, the same with a
    real boolean or a number, a few synonyms (start / stop / on / off), and a
    bare payload with no JSON object around it at all.
    """
    raw = payload.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return _coerce_trigger(raw)          # bare true / 1 / start / ...

    if isinstance(data, dict):
        lowered = {str(k).lower(): v for k, v in data.items()}
        for key in TRIGGER_KEYS:
            if key in lowered:
                return _coerce_trigger(lowered[key])
        return None
    return _coerce_trigger(data)


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


@dataclass
class Robot:
    """One simulated robot arm: where it reports, and what it listens on.

    status_topic and trigger_topic are normally left blank, in which case they
    follow base_topic. Filling one in pins it, for a line that does not lay its
    topics out the usual way.
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "New robot"
    base_topic: str = ""
    status_topic: str = ""
    trigger_topic: str = ""
    interval: float = SIM_DEFAULT_INTERVAL
    stop_delay: float = SIM_DEFAULT_STOP_DELAY
    qos: int = 1
    retain: bool = False

    @property
    def resolved_status(self) -> str:
        return self.status_topic.strip() or self._derive(STATUS_SUFFIX)

    @property
    def resolved_trigger(self) -> str:
        return self.trigger_topic.strip() or self._derive(TRIGGER_SUFFIX)

    def _derive(self, suffix: str) -> str:
        base = self.base_topic.strip().rstrip("/")
        return f"{base}{suffix}" if base else ""

    @classmethod
    def from_dict(cls, data: dict) -> "Robot":
        r = cls()
        r.id = str(data.get("id") or r.id)
        r.name = str(data.get("name", r.name))
        r.base_topic = str(data.get("base_topic", ""))
        r.status_topic = str(data.get("status_topic", ""))
        r.trigger_topic = str(data.get("trigger_topic", ""))
        try:
            r.interval = max(MIN_INTERVAL_S, float(data.get("interval", r.interval)))
        except (TypeError, ValueError):
            r.interval = SIM_DEFAULT_INTERVAL
        try:
            # Robots saved by 2.0 have no stop delay of their own - it was the
            # tick interval back then, so keep them behaving as they did.
            r.stop_delay = max(0.0, float(data.get("stop_delay", r.interval)))
        except (TypeError, ValueError):
            r.stop_delay = r.interval
        try:
            r.qos = int(data.get("qos", 1))
        except (TypeError, ValueError):
            r.qos = 1
        r.qos = r.qos if r.qos in (0, 1, 2) else 1
        r.retain = bool(data.get("retain", False))
        return r

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "base_topic": self.base_topic,
            "status_topic": self.status_topic, "trigger_topic": self.trigger_topic,
            "interval": self.interval, "stop_delay": self.stop_delay,
            "qos": self.qos, "retain": self.retain,
        }


class Config:
    def __init__(self, vault: "LocalVault | None" = None) -> None:
        self.vault = vault
        self.appearance = "System"
        self.broker = dict(DEFAULT_BROKER)
        self.presets: list[Preset] = []
        self.robots: list[Robot] = []
        self.last_selected: str | None = None
        self.last_selected_robot: str | None = None
        self.autoscroll = True
        self.migrated_from_plaintext = False
        self.load_error: str | None = None

    @classmethod
    def load(cls, vault: "LocalVault | None" = None) -> "Config":
        cfg = cls(vault)
        raw = cfg._read_settings()
        if raw is None:
            cfg.presets = [cfg._seed_preset()]
            cfg.robots = cfg._seed_robots()
            return cfg

        cfg.appearance = raw.get("appearance", "System")
        if cfg.appearance not in ("System", "Light", "Dark"):
            cfg.appearance = "System"
        broker = raw.get("broker") or {}
        cfg.broker = {**DEFAULT_BROKER, **{k: broker[k] for k in broker if k in DEFAULT_BROKER}}
        cfg.presets = [Preset.from_dict(p) for p in raw.get("presets", []) if isinstance(p, dict)]
        if not cfg.presets:
            cfg.presets = [cfg._seed_preset()]
        cfg.robots = [Robot.from_dict(r) for r in raw.get("robots", []) if isinstance(r, dict)]
        if not cfg.robots:
            # Pre-2.0 settings, or every robot deleted - start from the two
            # the app ships with rather than an empty simulator.
            cfg.robots = cfg._seed_robots()
        cfg.last_selected = raw.get("last_selected")
        cfg.last_selected_robot = raw.get("last_selected_robot")
        cfg.autoscroll = bool(raw.get("autoscroll", True))
        return cfg

    @staticmethod
    def _seed_preset() -> Preset:
        return Preset(name="Example message", topic=DEFAULT_TOPIC,
                      payload=DEFAULT_PAYLOAD, interval=60.0)

    @staticmethod
    def _seed_robots() -> list[Robot]:
        return [Robot(name=name, base_topic=base, interval=SIM_DEFAULT_INTERVAL)
                for name, base in DEFAULT_ROBOTS]

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
            "version": 3,
            "appearance": self.appearance,
            "broker": self.broker,
            "presets": [p.to_dict() for p in self.presets],
            "robots": [r.to_dict() for r in self.robots],
            "last_selected": self.last_selected,
            "last_selected_robot": self.last_selected_robot,
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

    def find_robot(self, robot_id: str | None) -> Robot | None:
        return next((r for r in self.robots if r.id == robot_id), None)


# ============================================================================
# MQTT connection
# ============================================================================


class MqttManager:
    """Owns a single shared paho client. All publishing goes through here."""

    def __init__(self, emit, on_message=None) -> None:
        self._emit = emit                     # emit(channel, level, text)
        self._on_message_cb = on_message      # on_message(topic, payload_text)
        self._subs: dict[str, int] = {}       # topic -> qos, reapplied on connect
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
            # A reconnect comes back with an empty session, so every
            # subscription has to be asked for again - otherwise triggers stop
            # arriving silently after a dropped connection.
            self._resubscribe()
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

    def _on_message(self, client, userdata, msg):
        if self._on_message_cb is None:
            return
        try:
            text = msg.payload.decode("utf-8", "replace")
        except Exception:
            text = repr(msg.payload)
        try:
            self._on_message_cb(msg.topic, text)
        except Exception as exc:
            self._emit("debug", "err",
                       f"Message handler error: {type(exc).__name__}: {exc}")

    # -- subscriptions ----------------------------------------------------
    def _resubscribe(self) -> None:
        """Reapply every wanted subscription. Runs after each CONNACK."""
        client = self._client
        if client is None:
            return
        for topic, qos in list(self._subs.items()):
            try:
                client.subscribe(topic, qos)
                self._emit("debug", "info", f"SUB  {topic}  (QoS {qos})")
            except Exception as exc:
                self._emit("debug", "err", f"Subscribe to {topic} failed - {exc}")

    def set_subscriptions(self, wanted: dict[str, int]) -> None:
        """Make the live subscriptions match `wanted` exactly."""
        with self._lock:
            client = self._client if self._connected.is_set() else None
            for topic in [t for t in self._subs if t not in wanted]:
                self._subs.pop(topic, None)
                if client is None:
                    continue
                try:
                    client.unsubscribe(topic)
                    self._emit("debug", "info", f"UNSUB {topic}")
                except Exception as exc:
                    self._emit("debug", "warn",
                               f"Unsubscribe from {topic} failed - {exc}")

            for topic, qos in wanted.items():
                if self._subs.get(topic) == qos:
                    continue
                self._subs[topic] = qos
                if client is None:
                    continue
                try:
                    client.subscribe(topic, qos)
                    self._emit("debug", "info", f"SUB  {topic}  (QoS {qos})")
                except Exception as exc:
                    self._emit("debug", "err", f"Subscribe to {topic} failed - {exc}")

    @property
    def subscriptions(self) -> dict[str, int]:
        return dict(self._subs)

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
            client.on_message = self._on_message
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
# Robot runner - one thread per simulated robot, from trigger to stopped
# ============================================================================


class RobotRunner(threading.Thread):
    """Runs one simulated robot for the length of one trigger-to-trigger run.

    The thread owns the whole sequence: the opening running message, a product
    counted every interval, then - once stopped - finished, a pause of one
    interval, and stopped. Anything asked for from the UI (a bad product, an
    injected error) is queued as a small job and applied at the next wake-up,
    so the counters are only ever touched by this thread.
    """

    def __init__(self, robot: Robot, manager: MqttManager, events: queue.Queue) -> None:
        super().__init__(daemon=True, name=f"robot-{robot.name}")
        self.robot_id = robot.id
        self.label = robot.name
        self.topic = robot.resolved_status
        self.interval = max(MIN_INTERVAL_S, float(robot.interval))
        self.stop_delay = max(0.0, float(robot.stop_delay))
        self.qos = robot.qos
        self.retain = robot.retain
        self._manager = manager
        self._events = events
        self._stop = threading.Event()      # finish tidily: finished -> stopped
        self._kill = threading.Event()      # drop it now, publish nothing more
        self._wake = threading.Event()      # cut the current wait short
        self._lock = threading.Lock()
        self._jobs: list[dict] = []
        self.good = 0
        self.bad = 0
        self.fault: dict | None = None      # held error, carried on every publish
        self.phase = "starting"

    # -- asked for from the UI thread -------------------------------------
    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def kill(self) -> None:
        """Abandon the run without publishing finished or stopped."""
        self._kill.set()
        self._stop.set()
        self._wake.set()

    def add_bad_product(self) -> None:
        self._submit({"job": "bad"})

    def inject_fault(self, fault: dict, sticky: bool) -> None:
        self._submit({"job": "fault", "fault": fault, "sticky": sticky})

    def clear_fault(self) -> None:
        self._submit({"job": "clear"})

    def _submit(self, job: dict) -> None:
        with self._lock:
            self._jobs.append(job)
        self._wake.set()

    # -- the run ----------------------------------------------------------
    def run(self) -> None:
        if not self._manager.is_connected:
            self._put("log", channel="debug", level="info",
                      text=f"[{self.label}] connecting before the first status...")
            ok, msg = self._manager.connect()
            if not ok:
                self._put("log", channel="activity", level="err",
                          text=f"[{self.label}] cannot start - {msg}")
                self._put("sim_stopped", good=0, bad=0)
                return

        self._set_phase("running")
        self._put("sim_started", interval=self.interval, topic=self.topic)
        self._publish(*STATE_RUNNING)          # goodProduct 0, on the trigger

        while not self._stop.is_set():
            if self._sleep(self.interval):
                break
            self.good += 1
            self._publish(*STATE_RUNNING)

        if not self._kill.is_set():
            self.phase = "finishing"
            self._put("sim_phase", phase="finishing",
                      next_at=time.monotonic() + self.stop_delay)
            self._publish(*STATE_FINISHED)      # carries the final count
            self._kill.wait(self.stop_delay)    # its own delay, then the reset
            if not self._kill.is_set():
                self._publish(*STATE_STOPPED, good=0, bad=0)

        self._set_phase("stopped")
        self._put("sim_stopped", good=self.good, bad=self.bad)

    def _sleep(self, seconds: float) -> bool:
        """Wait out one interval, doing queued jobs. True means stop now."""
        deadline = time.monotonic() + seconds
        self._put("sim_tick", good=self.good, bad=self.bad,
                  next_at=deadline, phase=self.phase)
        while True:
            if self._stop.is_set():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._wake.wait(remaining):
                self._wake.clear()
                if self._stop.is_set():
                    return True
                self._run_jobs()

    def _run_jobs(self) -> None:
        with self._lock:
            jobs, self._jobs = self._jobs, []
        for job in jobs:
            kind = job.get("job")
            if kind == "bad":
                self.bad += 1
                self._put("log", channel="activity", level="warn",
                          text=f"[{self.label}] bad product #{self.bad}")
                self._publish(*STATE_RUNNING)
            elif kind == "fault":
                fault = job.get("fault") or {}
                held = bool(job.get("sticky"))
                if held:
                    self.fault = fault
                code = fault.get("errorCode", 0)
                name = fault.get("errorName", "")
                self._put("log", channel="activity", level="warn",
                          text=f"[{self.label}] error {code} {name} - "
                               + ("held until cleared" if held else "one message"))
                self._publish(*STATE_RUNNING, fault=None if held else fault)
            elif kind == "clear":
                self.fault = None
                self._put("log", channel="activity", level="info",
                          text=f"[{self.label}] error cleared - back to running")
                self._publish(*STATE_RUNNING)
        if jobs:
            self._put("sim_counts", good=self.good, bad=self.bad,
                      fault=bool(self.fault))

    # -- publishing -------------------------------------------------------
    def _publish(self, state: int, state_name: str, good: int | None = None,
                 bad: int | None = None, fault: dict | None = None) -> None:
        code, error = 0, ""
        # A held error replaces the running state until it is cleared. finished
        # and stopped always go out as themselves - they end the run either way.
        applied = fault if fault is not None else (
            self.fault if state == STATE_RUNNING[0] else None)
        if applied:
            state = int(applied.get("state", state))
            state_name = str(applied.get("stateName", state_name))
            code = int(applied.get("errorCode", 0))
            error = str(applied.get("errorName", ""))

        payload = json.dumps({
            "state": state,
            "stateName": state_name,
            "goodProduct": self.good if good is None else good,
            "badProduct": self.bad if bad is None else bad,
            "errorCode": code,
            "errorName": error,
            "ts": _timestamp(),
        })

        ok, detail = self._manager.publish(self.topic, payload, self.qos, self.retain)
        if ok:
            self._put("log", channel="activity", level="tx",
                      text=f"TX  {self.topic}  (QoS {self.qos}"
                           f"{', retain' if self.retain else ''})  [{self.label}]"
                           f"\n     {payload}")
        else:
            self._put("log", channel="activity", level="err",
                      text=f"FAIL {self.topic}  [{self.label}] - {detail}")

    # -- plumbing ---------------------------------------------------------
    def _set_phase(self, phase: str) -> None:
        self.phase = phase
        self._put("sim_phase", phase=phase)

    def _put(self, kind: str, **data) -> None:
        self._events.put({"kind": kind, "robot_id": self.robot_id, **data})


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
# Inject-error dialog
# ============================================================================


class FaultDialog(ctk.CTkToplevel):
    """Asks what error a running robot should report.

    result is None if cancelled, otherwise (fault, sticky) where fault holds
    the state / stateName / errorCode / errorName to publish.
    """

    def __init__(self, master, label: str) -> None:
        super().__init__(master)
        self.result: tuple[dict, bool] | None = None

        self.title("Inject error")
        self.geometry("430x330")
        self.resizable(False, False)
        self.transient(master)
        self.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self, text=f"Error for {label}",
                     font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=0, column=0, columnspan=2, padx=20, pady=(20, 4), sticky="w")
        ctk.CTkLabel(self, text="Published on the status topic with the counts as "
                                "they stand.", anchor="w", wraplength=380,
                     justify="left", font=ctk.CTkFont(size=12)).grid(
            row=1, column=0, columnspan=2, padx=20, pady=(0, 10), sticky="w")

        row = 1

        def field(text: str, value: str) -> ctk.CTkEntry:
            nonlocal row
            row += 1
            ctk.CTkLabel(self, text=text, anchor="w").grid(
                row=row, column=0, padx=(20, 10), pady=6, sticky="w")
            entry = ctk.CTkEntry(self)
            entry.insert(0, value)
            entry.grid(row=row, column=1, padx=(0, 20), pady=6, sticky="ew")
            return entry

        self.state_entry = field("state", "2")
        self.name_entry = field("stateName", "error")
        self.code_entry = field("errorCode", "101")
        self.error_entry = field("errorName", "Gripper timeout")

        row += 1
        self.sticky_check = ctk.CTkCheckBox(
            self, text="Keep reporting this until I clear it")
        self.sticky_check.grid(row=row, column=1, padx=(0, 20), pady=(10, 4), sticky="w")

        row += 1
        self.status = ctk.CTkLabel(self, text="", anchor="w", wraplength=380,
                                   justify="left", font=ctk.CTkFont(size=12))
        self.status.grid(row=row, column=0, columnspan=2, padx=20, pady=(4, 0), sticky="w")

        row += 1
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=row, column=0, columnspan=2, padx=20, pady=(10, 18), sticky="ew")
        buttons.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(buttons, text="Cancel", width=90, fg_color="transparent",
                      border_width=1, command=self.destroy).grid(row=0, column=1, padx=(0, 8))
        ctk.CTkButton(buttons, text="Send", width=90,
                      command=self._ok).grid(row=0, column=2)

        self.after(150, self._grab)

    def _grab(self) -> None:
        try:
            self.grab_set()
            self.focus_force()
        except Exception:
            pass

    def _ok(self) -> None:
        try:
            state = int(self.state_entry.get().strip())
            code = int(self.code_entry.get().strip() or 0)
        except ValueError:
            self.status.configure(text="state and errorCode must be whole numbers.",
                                  text_color="#e03131")
            return
        self.result = ({
            "state": state,
            "stateName": self.name_entry.get().strip() or "error",
            "errorCode": code,
            "errorName": self.error_entry.get().strip(),
        }, bool(self.sticky_check.get()))
        self.destroy()

    @classmethod
    def ask(cls, master, label: str) -> "tuple[dict, bool] | None":
        dialog = cls(master, label)
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
        self.mqtt = MqttManager(self._emit_from_thread, self._on_mqtt_message)
        self.mqtt.configure(self.config_data.broker, self.password)

        self.runners: dict[str, LoopRunner] = {}
        self.run_state: dict[str, dict] = {}     # preset_id -> {sent, failed, next_at}
        self._restart_pending: set[str] = set()  # loops to relaunch once stopped
        self._pending_reconnect = False          # drop the connection first?
        self.preset_buttons: dict[str, ctk.CTkButton] = {}
        self.selected_id: str | None = None
        self._dirty = False

        # Robot simulator - one runner per robot currently mid-run.
        self.sim_runners: dict[str, RobotRunner] = {}
        self.sim_state: dict[str, dict] = {}     # robot_id -> {good, bad, next_at, phase}
        self._sim_restart: set[str] = set()      # triggered again while finishing
        self.robot_buttons: dict[str, ctk.CTkButton] = {}
        self.selected_robot_id: str | None = None
        self._sim_dirty = False
        self._sim_base_prev = ""                 # base the topic boxes were derived from

        ctk.set_appearance_mode(self.config_data.appearance)
        ctk.set_default_color_theme("blue")

        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1180x860")
        self.minsize(980, 700)

        self._build_ui()
        self._refresh_preset_list()

        first = self.config_data.find(self.config_data.last_selected) or self.config_data.presets[0]
        self._select_preset(first.id)

        self._refresh_robot_list()
        first_robot = (self.config_data.find_robot(self.config_data.last_selected_robot)
                       or self.config_data.robots[0])
        self._select_robot(first_robot.id)

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

        for robot in self.config_data.robots:
            self.log("activity", "info",
                     f"[{robot.name}] waiting for a trigger on {robot.resolved_trigger}"
                     f"  ->  status to {robot.resolved_status}")

        self.after(120, self._pump_events)
        self.after(500, self._tick_ui)
        if self.config_data.broker.get("host"):
            # Nothing can react to a trigger that arrives before we are
            # subscribed, so come up connected rather than waiting to be asked.
            self.after(400, self._autoconnect)
        else:
            self.after(700, self._prompt_first_run)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=3)
        self.grid_rowconfigure(2, weight=2)

        self._build_header()

        # Two screens over one connection and one log: the saved-message
        # sender, and the robot simulator.
        self.mode_tabs = ctk.CTkTabview(self, anchor="w")
        self.mode_tabs.grid(row=1, column=0, sticky="nsew", padx=10, pady=(8, 0))
        for tab in (TAB_MESSAGES, TAB_SIMULATOR):
            self.mode_tabs.add(tab)
            frame = self.mode_tabs.tab(tab)
            frame.grid_columnconfigure(0, weight=1)
            frame.grid_rowconfigure(0, weight=1)

        self._build_body()
        self._build_simulator()
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
        body = ctk.CTkFrame(self.mode_tabs.tab(TAB_MESSAGES), fg_color="transparent")
        body.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
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
        # A trigger topic carries the bare word, not a JSON wrapper - one click
        # to put exactly that in the box.
        for i, word in enumerate(RAW_QUICK_PAYLOADS):
            ctk.CTkButton(payload_head, text=word, width=62, height=26,
                          fg_color="transparent", border_width=1,
                          command=lambda w=word: self._set_raw_payload(w)).grid(
                row=0, column=2 + i, padx=(0, 6))
        ctk.CTkButton(payload_head, text="Format JSON", width=110, height=26,
                      fg_color="transparent", border_width=1,
                      command=self._format_json).grid(
            row=0, column=2 + len(RAW_QUICK_PAYLOADS))

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

    def _build_simulator(self) -> None:
        sim = ctk.CTkFrame(self.mode_tabs.tab(TAB_SIMULATOR), fg_color="transparent")
        sim.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        sim.grid_columnconfigure(1, weight=1)
        sim.grid_rowconfigure(0, weight=1)

        # ---- left: the robots ------------------------------------------
        left = ctk.CTkFrame(sim, width=280)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.grid_propagate(False)
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(left, text="SIMULATED ROBOTS", anchor="w",
                     font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=0, column=0, padx=14, pady=(14, 6), sticky="ew")

        self.robot_list = ctk.CTkScrollableFrame(left, fg_color="transparent")
        self.robot_list.grid(row=1, column=0, sticky="nsew", padx=6, pady=0)
        self.robot_list.grid_columnconfigure(0, weight=1)

        btns = ctk.CTkFrame(left, fg_color="transparent")
        btns.grid(row=2, column=0, sticky="ew", padx=10, pady=10)
        btns.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(btns, text="+ Add", command=self._new_robot).grid(
            row=0, column=0, padx=(0, 4), pady=3, sticky="ew")
        ctk.CTkButton(btns, text="Duplicate", fg_color="transparent", border_width=1,
                      command=self._duplicate_robot).grid(
            row=0, column=1, padx=(4, 0), pady=3, sticky="ew")
        ctk.CTkButton(btns, text="Delete", fg_color="transparent", border_width=1,
                      text_color=("#c92a2a", "#ff6b6b"), hover_color=("#ffe3e3", "#4d1f1f"),
                      command=self._delete_robot).grid(
            row=1, column=0, padx=(0, 4), pady=3, sticky="ew")
        ctk.CTkButton(btns, text="Stop all", fg_color="transparent", border_width=1,
                      command=lambda: self._sim_stop_all()).grid(
            row=1, column=1, padx=(4, 0), pady=3, sticky="ew")

        # ---- right: the selected robot ---------------------------------
        right = ctk.CTkFrame(sim)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(1, weight=1)
        right.grid_rowconfigure(6, weight=1)

        def field(row: int, label: str, mono: bool = False) -> ctk.CTkEntry:
            ctk.CTkLabel(right, text=label, anchor="w", width=110).grid(
                row=row, column=0, padx=(16, 8), pady=4,
                sticky="w")
            entry = ctk.CTkEntry(
                right, font=ctk.CTkFont(family=MONO_FONT, size=13) if mono else None)
            entry.grid(row=row, column=1, padx=(0, 16), pady=4, sticky="ew")
            return entry

        self.sim_name_entry = field(0, "Name")
        self.sim_name_entry.bind("<KeyRelease>", self._mark_sim_dirty)
        self.sim_base_entry = field(1, "Base topic", mono=True)
        self.sim_base_entry.bind("<KeyRelease>", self._on_sim_base_change)
        self.sim_status_entry = field(2, "Status topic", mono=True)
        self.sim_status_entry.bind("<KeyRelease>", self._mark_sim_dirty)
        self.sim_trigger_entry = field(3, "Trigger topic", mono=True)
        self.sim_trigger_entry.bind("<KeyRelease>", self._mark_sim_dirty)

        ctk.CTkLabel(right, text="Status is published here; the run starts and stops "
                                "on whatever arrives on the trigger topic.",
                     anchor="w", font=ctk.CTkFont(size=12),
                     text_color=("#868e96", "#868e96")).grid(
            row=4, column=1, padx=(0, 16), pady=(0, 6), sticky="w")

        opts = ctk.CTkFrame(right, fg_color="transparent")
        opts.grid(row=5, column=0, columnspan=2, padx=16, pady=(4, 8), sticky="ew")
        ctk.CTkLabel(opts, text="One product every").grid(row=0, column=0, padx=(0, 8))
        self.sim_interval_entry = ctk.CTkEntry(opts, width=80, justify="right")
        self.sim_interval_entry.grid(row=0, column=1)
        self.sim_interval_entry.bind("<KeyRelease>", self._mark_sim_dirty)
        ctk.CTkLabel(opts, text="seconds").grid(row=0, column=2, padx=(6, 10))
        self.sim_interval_preset = ctk.CTkOptionMenu(
            opts, width=110, values=["1 s", "2 s", "5 s", "10 s", "30 s", "1 min"],
            command=self._apply_sim_interval_preset)
        self.sim_interval_preset.set("Quick set")
        self.sim_interval_preset.grid(row=0, column=3, padx=(0, 24))

        ctk.CTkLabel(opts, text="QoS").grid(row=0, column=4, padx=(0, 6))
        self.sim_qos_menu = ctk.CTkOptionMenu(opts, width=70, values=["0", "1", "2"],
                                              command=lambda _v: self._mark_sim_dirty())
        self.sim_qos_menu.grid(row=0, column=5, padx=(0, 16))
        self.sim_retain_check = ctk.CTkCheckBox(opts, text="Retain",
                                                command=self._mark_sim_dirty)
        self.sim_retain_check.grid(row=0, column=6)

        # Kept apart from the tick rate: how fast products come off the line
        # and how long the robot sits on "finished" are different things.
        ctk.CTkLabel(opts, text="Stopped message").grid(
            row=1, column=0, padx=(0, 8), pady=(10, 0))
        self.sim_stop_entry = ctk.CTkEntry(opts, width=80, justify="right")
        self.sim_stop_entry.grid(row=1, column=1, pady=(10, 0))
        self.sim_stop_entry.bind("<KeyRelease>", self._mark_sim_dirty)
        ctk.CTkLabel(opts, text="seconds after finished", anchor="w").grid(
            row=1, column=2, columnspan=2, padx=(6, 10), pady=(10, 0), sticky="w")

        # ---- live panel ------------------------------------------------
        live = ctk.CTkFrame(right)
        live.grid(row=6, column=0, columnspan=2, padx=16, pady=(4, 8), sticky="nsew")
        live.grid_columnconfigure(0, weight=1)

        self.sim_phase_label = ctk.CTkLabel(
            live, text="Idle", anchor="w", font=ctk.CTkFont(size=15, weight="bold"))
        self.sim_phase_label.grid(row=0, column=0, padx=16, pady=(14, 2), sticky="w")

        self.sim_counts_label = ctk.CTkLabel(
            live, text="goodProduct 0   badProduct 0", anchor="w",
            font=ctk.CTkFont(family=MONO_FONT, size=13))
        self.sim_counts_label.grid(row=1, column=0, padx=16, pady=(0, 10), sticky="w")

        faults = ctk.CTkFrame(live, fg_color="transparent")
        faults.grid(row=2, column=0, padx=12, pady=(0, 12), sticky="w")
        self.sim_bad_btn = ctk.CTkButton(
            faults, text="+1 bad product", width=130, fg_color="transparent",
            border_width=1, command=self._sim_add_bad)
        self.sim_bad_btn.grid(row=0, column=0, padx=4)
        self.sim_inject_btn = ctk.CTkButton(
            faults, text="Inject error...", width=130, fg_color="transparent",
            border_width=1, command=self._sim_inject_fault)
        self.sim_inject_btn.grid(row=0, column=1, padx=4)
        self.sim_clear_btn = ctk.CTkButton(
            faults, text="Clear error", width=110, fg_color="transparent",
            border_width=1, command=self._sim_clear_fault)
        self.sim_clear_btn.grid(row=0, column=2, padx=4)
        self.sim_force_btn = ctk.CTkButton(
            faults, text="Force stop", width=110, fg_color="transparent",
            border_width=1, text_color=("#c92a2a", "#ff6b6b"),
            hover_color=("#ffe3e3", "#4d1f1f"), command=self._sim_force_stop)
        self.sim_force_btn.grid(row=0, column=3, padx=4)

        # ---- actions ---------------------------------------------------
        actions = ctk.CTkFrame(right, fg_color="transparent")
        actions.grid(row=7, column=0, columnspan=2, padx=16, pady=(0, 14), sticky="ew")
        actions.grid_columnconfigure(3, weight=1)

        self.sim_save_btn = ctk.CTkButton(actions, text="Save", width=100,
                                          command=self._save_robot)
        self.sim_save_btn.grid(row=0, column=0, padx=(0, 16))
        ctk.CTkLabel(actions, text="Test the trigger:").grid(row=0, column=1, padx=(0, 8))
        trigger_btns = ctk.CTkFrame(actions, fg_color="transparent")
        trigger_btns.grid(row=0, column=2)
        ctk.CTkButton(trigger_btns, text="send true", width=100, fg_color="#2f9e44",
                      hover_color="#268a3a",
                      command=lambda: self._send_test_trigger(True)).grid(
            row=0, column=0, padx=(0, 6))
        ctk.CTkButton(trigger_btns, text="send false", width=100, fg_color="#c92a2a",
                      hover_color="#a51f1f",
                      command=lambda: self._send_test_trigger(False)).grid(
            row=0, column=1)

        self.sim_hint = ctk.CTkLabel(actions, text="", anchor="e",
                                     font=ctk.CTkFont(size=12))
        self.sim_hint.grid(row=0, column=3, sticky="e", padx=8)

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
        self._refresh_robot_list()

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
        self._check_payload()
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
        self._check_payload()

    def _check_payload(self) -> str:
        """Label what is in the payload box. Returns the kind - see classify_payload."""
        kind, detail = classify_payload(self.payload_box.get("1.0", "end").strip())
        colour = {"json": "#2f9e44", "raw": ("#5c6f8a", "#8fa6c4")}.get(kind, "#e8590c")
        self.json_status.configure(text=detail, text_color=colour)
        return kind

    def _set_raw_payload(self, word: str) -> None:
        """Drop a bare word into the payload box, replacing whatever is there."""
        self.payload_box.delete("1.0", "end")
        self.payload_box.insert("1.0", word)
        self._on_payload_change()

    def _format_json(self) -> None:
        text = self.payload_box.get("1.0", "end").strip()
        if text[:1] not in JSON_OPENERS:
            messagebox.showinfo(APP_NAME, "Nothing to format - this payload is raw text "
                                          "and is sent exactly as typed.", parent=self)
            return
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

        # Raw text is a payload, not a failed attempt at JSON - only ask about
        # something that opens like JSON and then does not parse.
        if self._check_payload() == "broken":
            proceed = messagebox.askyesno(
                APP_NAME,
                "The payload opens like JSON but does not parse. "
                "Save and send it as raw text anyway?",
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

    # ------------------------------------------------------------ robots
    def _refresh_robot_list(self) -> None:
        for widget in self.robot_list.winfo_children():
            widget.destroy()
        self.robot_buttons.clear()

        for i, robot in enumerate(self.config_data.robots):
            running = robot.id in self.sim_runners
            selected = robot.id == self.selected_robot_id
            marker = "●" if running else "○"
            btn = ctk.CTkButton(
                self.robot_list,
                text=f"{marker}  {robot.name}",
                anchor="w", height=36, corner_radius=6,
                fg_color=("#dbe4ff", "#2b3a55") if selected else "transparent",
                hover_color=("#e7ecff", "#33425e"),
                text_color=("#1a1a1a", "#f0f0f0"),
                command=lambda rid=robot.id: self._select_robot(rid),
            )
            btn.grid(row=i, column=0, sticky="ew", pady=2, padx=2)
            self.robot_buttons[robot.id] = btn

    def _update_robot_button(self, robot_id: str) -> None:
        robot = self.config_data.find_robot(robot_id)
        btn = self.robot_buttons.get(robot_id)
        if robot is None or btn is None:
            return
        runner = self.sim_runners.get(robot_id)
        marker = "●" if runner is not None else "○"
        suffix = ""
        if runner is not None:
            suffix = f"   good {self.sim_state.get(robot_id, {}).get('good', runner.good)}"
        selected = robot_id == self.selected_robot_id
        btn.configure(text=f"{marker}  {robot.name}{suffix}",
                      fg_color=("#dbe4ff", "#2b3a55") if selected else "transparent")

    def _select_robot(self, robot_id: str) -> None:
        if self._sim_dirty and self.selected_robot_id and self.selected_robot_id != robot_id:
            keep = messagebox.askyesnocancel(
                APP_NAME, "Save changes to the current robot first?", parent=self)
            if keep is None:
                return
            if keep:
                self._save_robot()

        self.selected_robot_id = robot_id
        robot = self.config_data.find_robot(robot_id)
        if robot is None:
            return

        self.sim_name_entry.delete(0, "end")
        self.sim_name_entry.insert(0, robot.name)
        self.sim_base_entry.delete(0, "end")
        self.sim_base_entry.insert(0, robot.base_topic)
        self.sim_status_entry.delete(0, "end")
        self.sim_status_entry.insert(0, robot.resolved_status)
        self.sim_trigger_entry.delete(0, "end")
        self.sim_trigger_entry.insert(0, robot.resolved_trigger)
        self.sim_interval_entry.delete(0, "end")
        self.sim_interval_entry.insert(0, self._format_interval(robot.interval))
        self.sim_stop_entry.delete(0, "end")
        self.sim_stop_entry.insert(0, self._format_interval(robot.stop_delay))
        self.sim_qos_menu.set(str(robot.qos))
        if robot.retain:
            self.sim_retain_check.select()
        else:
            self.sim_retain_check.deselect()

        self._sim_base_prev = robot.base_topic.strip().rstrip("/")
        self.config_data.last_selected_robot = robot_id
        self._sim_dirty = False
        self.sim_save_btn.configure(text="Save")
        self._refresh_robot_list()
        self._update_sim_buttons()
        self._tick_sim()

    def _mark_sim_dirty(self, _event=None) -> None:
        self._sim_dirty = True
        self.sim_save_btn.configure(text="Save *")

    def _on_sim_base_change(self, _event=None) -> None:
        """Keep the derived topics following the base topic as it is typed.

        A topic the user has edited by hand no longer matches what the previous
        base would have produced, and is left alone.
        """
        base = self.sim_base_entry.get().strip().rstrip("/")
        previous = self._sim_base_prev
        for entry, suffix in ((self.sim_status_entry, STATUS_SUFFIX),
                              (self.sim_trigger_entry, TRIGGER_SUFFIX)):
            current = entry.get().strip()
            if current and current != f"{previous}{suffix}":
                continue
            entry.delete(0, "end")
            entry.insert(0, f"{base}{suffix}" if base else "")
        self._sim_base_prev = base
        self._mark_sim_dirty()

    def _apply_sim_interval_preset(self, value: str) -> None:
        seconds = {"1 s": 1, "2 s": 2, "5 s": 5, "10 s": 10,
                   "30 s": 30, "1 min": 60}.get(value)
        if seconds is None:
            return
        self.sim_interval_entry.delete(0, "end")
        self.sim_interval_entry.insert(0, str(seconds))
        self.sim_interval_preset.set("Quick set")
        self._mark_sim_dirty()

    def _read_sim_editor(self) -> Robot | None:
        robot = self.config_data.find_robot(self.selected_robot_id)
        if robot is None:
            return None

        name = self.sim_name_entry.get().strip() or "Untitled robot"
        base = self.sim_base_entry.get().strip().rstrip("/")
        status = self.sim_status_entry.get().strip()
        trigger = self.sim_trigger_entry.get().strip()

        # A topic that is just the base plus the usual suffix is stored blank,
        # so renaming the base later moves it too.
        if base:
            status = "" if status == f"{base}{STATUS_SUFFIX}" else status
            trigger = "" if trigger == f"{base}{TRIGGER_SUFFIX}" else trigger

        raw = self.sim_interval_entry.get().strip().replace(",", ".")
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

        raw_delay = self.sim_stop_entry.get().strip().replace(",", ".")
        try:
            stop_delay = float(raw_delay)
        except ValueError:
            messagebox.showwarning(
                APP_NAME, f"'{raw_delay}' is not a valid number of seconds.", parent=self)
            return None
        if stop_delay < 0:
            messagebox.showwarning(
                APP_NAME, "The stopped delay cannot be negative. Use 0 to send stopped "
                          "straight after finished.", parent=self)
            return None

        edited = Robot(id=robot.id, name=name, base_topic=base, status_topic=status,
                       trigger_topic=trigger, interval=interval, stop_delay=stop_delay,
                       qos=int(self.sim_qos_menu.get()),
                       retain=bool(self.sim_retain_check.get()))

        if not edited.resolved_status or not edited.resolved_trigger:
            messagebox.showwarning(
                APP_NAME, "Set a base topic, or fill in the status and trigger topics "
                          "yourself.", parent=self)
            return None
        if edited.resolved_status == edited.resolved_trigger:
            messagebox.showwarning(
                APP_NAME, "The status and trigger topics must differ - as written, the "
                          "robot would trigger itself.", parent=self)
            return None
        return edited

    def _save_robot(self) -> bool:
        edited = self._read_sim_editor()
        if edited is None:
            return False

        index = next((i for i, r in enumerate(self.config_data.robots)
                      if r.id == edited.id), None)
        if index is None:
            return False
        self.config_data.robots[index] = edited

        ok, detail = self.config_data.save()
        if not ok:
            messagebox.showerror(APP_NAME, f"Could not save config:\n{detail}", parent=self)
            return False

        self._sim_dirty = False
        self.sim_save_btn.configure(text="Save")
        self._sim_base_prev = edited.base_topic.strip().rstrip("/")
        self._update_robot_button(edited.id)
        self._sync_subscriptions()
        self.status_left.configure(text=f"Saved '{edited.name}'")

        twin = next((r for r in self.config_data.robots
                     if r.id != edited.id
                     and r.resolved_trigger == edited.resolved_trigger), None)
        if twin is not None:
            self.log("activity", "warn",
                     f"[{edited.name}] shares its trigger topic with '{twin.name}' - "
                     f"one trigger will start both.")
        if edited.id in self.sim_runners:
            self.log("activity", "info",
                     f"[{edited.name}] is mid-run - it keeps its current topic and "
                     f"interval until the next trigger.")
        return True

    def _new_robot(self) -> None:
        number = len(self.config_data.robots) + 1
        robot = Robot(name=f"Robot {number:02d}", base_topic=f"line/robot{number:02d}")
        self.config_data.robots.append(robot)
        self.config_data.save()
        self._sim_dirty = False
        self._refresh_robot_list()
        self._select_robot(robot.id)
        self._sync_subscriptions()
        self.sim_name_entry.focus_set()

    def _duplicate_robot(self) -> None:
        source = self.config_data.find_robot(self.selected_robot_id)
        if source is None:
            return
        copy = Robot(name=f"{source.name} (copy)", base_topic=source.base_topic,
                     status_topic=source.status_topic, trigger_topic=source.trigger_topic,
                     interval=source.interval, stop_delay=source.stop_delay,
                     qos=source.qos, retain=source.retain)
        self.config_data.robots.insert(self.config_data.robots.index(source) + 1, copy)
        self.config_data.save()
        self._sim_dirty = False
        self._refresh_robot_list()
        self._select_robot(copy.id)
        self.log("activity", "info",
                 f"[{copy.name}] copied from '{source.name}' - give it its own base "
                 f"topic before triggering it.")

    def _delete_robot(self) -> None:
        robot = self.config_data.find_robot(self.selected_robot_id)
        if robot is None:
            return
        if len(self.config_data.robots) == 1:
            messagebox.showinfo(APP_NAME, "At least one robot must remain.", parent=self)
            return
        if not messagebox.askyesno(APP_NAME, f"Delete '{robot.name}'?", parent=self):
            return
        runner = self.sim_runners.pop(robot.id, None)
        if runner is not None:
            runner.kill()
            self.sim_state.pop(robot.id, None)
        index = self.config_data.robots.index(robot)
        self.config_data.robots.remove(robot)
        self.config_data.save()
        self._sim_dirty = False
        self._refresh_robot_list()
        self._select_robot(self.config_data.robots[max(0, index - 1)].id)
        self._sync_subscriptions()
        self._update_running_count()

    # ------------------------------------------------- simulator controls
    def _sync_subscriptions(self) -> None:
        """Watch exactly the trigger topics the configured robots ask for."""
        wanted = {r.resolved_trigger: SIM_TRIGGER_QOS
                  for r in self.config_data.robots if r.resolved_trigger}
        self.mqtt.set_subscriptions(wanted)

    def _sim_start(self, robot: Robot) -> None:
        runner = self.sim_runners.get(robot.id)
        if runner is not None:
            if runner.phase == "finishing":
                self._sim_restart.add(robot.id)
                self.log("activity", "info",
                         f"[{robot.name}] triggered while finishing - a fresh run "
                         f"starts once the stopped message is out.")
            else:
                self.log("activity", "info",
                         f"[{robot.name}] already running - trigger ignored.")
            return
        if not robot.resolved_status:
            self.log("activity", "err",
                     f"[{robot.name}] has no status topic - nothing to publish to.")
            return

        runner = RobotRunner(robot, self.mqtt, self.events)
        self.sim_runners[robot.id] = runner
        self.sim_state[robot.id] = {"good": 0, "bad": 0, "next_at": None,
                                    "phase": "starting"}
        runner.start()
        self.log("activity", "ok",
                 f"[{robot.name}] START - one product every "
                 f"{self._format_interval(robot.interval)}s to {robot.resolved_status}")
        self._update_robot_button(robot.id)
        self._update_sim_buttons()
        self._update_running_count()

    def _sim_stop(self, robot: Robot, kill: bool = False, silent: bool = False) -> None:
        runner = self.sim_runners.get(robot.id)
        if runner is None:
            if not silent:
                self.log("activity", "info",
                         f"[{robot.name}] is not running - trigger ignored.")
            return
        if kill:
            runner.kill()
            if not silent:
                self.log("activity", "warn",
                         f"[{robot.name}] force stopped - no finished or stopped "
                         f"message sent.")
            return
        runner.request_stop()
        if not silent:
            self.log("activity", "info",
                     f"[{robot.name}] STOP - finished now, stopped in "
                     f"{self._format_interval(runner.stop_delay)}s")

    def _sim_stop_all(self, kill: bool = False, silent: bool = False) -> None:
        if not self.sim_runners:
            return
        for rid in list(self.sim_runners):
            robot = self.config_data.find_robot(rid)
            if robot is not None:
                self._sim_stop(robot, kill=kill, silent=True)
            else:
                self.sim_runners[rid].kill()
        self._sim_restart.clear()
        if not silent:
            self.log("activity", "info", "Stop all - every simulation is winding down.")

    def _selected_runner(self) -> "RobotRunner | None":
        return self.sim_runners.get(self.selected_robot_id)

    def _sim_add_bad(self) -> None:
        runner = self._selected_runner()
        if runner is not None:
            runner.add_bad_product()

    def _sim_inject_fault(self) -> None:
        runner = self._selected_runner()
        robot = self.config_data.find_robot(self.selected_robot_id)
        if runner is None or robot is None:
            return
        answer = FaultDialog.ask(self, robot.name)
        if answer is None:
            return
        fault, sticky = answer
        runner.inject_fault(fault, sticky)

    def _sim_clear_fault(self) -> None:
        runner = self._selected_runner()
        if runner is not None:
            runner.clear_fault()

    def _sim_force_stop(self) -> None:
        robot = self.config_data.find_robot(self.selected_robot_id)
        if robot is None or robot.id not in self.sim_runners:
            return
        if not messagebox.askyesno(
                APP_NAME, f"Force stop '{robot.name}'?\n\nThe finished and stopped "
                          f"messages will NOT be sent.", parent=self):
            return
        self._sim_stop(robot, kill=True)

    def _send_test_trigger(self, value: bool) -> None:
        """Publish a trigger to the robot's own trigger topic.

        Sent as a bare true / false, which is what the line controller puts on
        the topic. It comes back through the subscription like any other
        trigger, so this exercises the same path the controller does.
        """
        if not self._require_broker():
            return
        if self._sim_dirty and not self._save_robot():
            return
        robot = self.config_data.find_robot(self.selected_robot_id)
        if robot is None or not robot.resolved_trigger:
            return
        payload = "true" if value else "false"
        self._publish_async(robot.resolved_trigger, payload, qos=SIM_TRIGGER_QOS,
                            label=f"{robot.name} test trigger")

    def _publish_async(self, topic: str, payload: str, qos: int = 1,
                       retain: bool = False, label: str = "") -> None:
        """One publish on a worker thread, connecting first if it has to."""
        def worker() -> None:
            if not self.mqtt.is_connected:
                ok, msg = self.mqtt.connect()
                self.events.put({"kind": "connect_result", "ok": ok, "message": msg})
                if not ok:
                    return
            ok, detail = self.mqtt.publish(topic, payload, qos, retain)
            if ok:
                self.events.put({
                    "kind": "log", "channel": "activity", "level": "tx",
                    "text": f"TX  {topic}  (QoS {qos})  [{label}]\n     {payload}"})
            else:
                self.events.put({"kind": "log", "channel": "activity", "level": "err",
                                 "text": f"FAIL {topic}  [{label}] - {detail}"})

        threading.Thread(target=worker, daemon=True).start()

    # --------------------------------------------------- incoming triggers
    def _on_mqtt_message(self, topic: str, payload: str) -> None:
        """Called on the paho thread - hand it to the UI thread and return."""
        self.events.put({"kind": "mqtt_message", "topic": topic, "payload": payload})

    def _handle_mqtt_message(self, topic: str, payload: str) -> None:
        flat = " ".join(payload.split())
        self.log("activity", "rx", f"RX  {topic}\n     {flat or '(empty payload)'}")

        watchers = [r for r in self.config_data.robots if r.resolved_trigger == topic]
        if not watchers:
            self.log("debug", "warn", f"No robot watches {topic} - message ignored.")
            return

        wanted = parse_trigger(payload)
        if wanted is None:
            self.log("activity", "warn",
                     f"Trigger payload on {topic} not understood - ignored. "
                     f"Expected true or false, bare or as "
                     f"{{\"trigger\": \"true\"}}.")
            return

        for robot in watchers:
            if wanted:
                self._sim_start(robot)
            else:
                self._sim_stop(robot)

    def _tick_sim(self) -> None:
        """Half-second refresh of the live panel for the selected robot."""
        robot = self.config_data.find_robot(self.selected_robot_id)
        if robot is None:
            return
        runner = self.sim_runners.get(self.selected_robot_id)
        state = self.sim_state.get(self.selected_robot_id, {})

        if runner is None:
            waiting = robot.resolved_trigger or "(no trigger topic set)"
            self.sim_phase_label.configure(
                text=f"○  Idle - waiting for a trigger on {waiting}",
                text_color=("#868e96", "#868e96"))
            self.sim_counts_label.configure(text="goodProduct 0   badProduct 0")
            return

        good = state.get("good", runner.good)
        bad = state.get("bad", runner.bad)
        phase = state.get("phase", runner.phase)
        fault = runner.fault

        if phase == "finishing":
            next_at = state.get("next_at")
            remaining = max(0.0, next_at - time.monotonic()) if next_at else 0.0
            self.sim_phase_label.configure(
                text=f"■  Finished - stopped message in {remaining:0.0f}s",
                text_color="#e8590c")
        elif phase == "starting":
            self.sim_phase_label.configure(text="Starting...", text_color="#e8590c")
        elif fault:
            self.sim_phase_label.configure(
                text=f"▲  Error {fault.get('errorCode', 0)} "
                     f"{fault.get('errorName', '')} - held until cleared",
                text_color="#e03131")
        else:
            next_at = state.get("next_at")
            remaining = max(0.0, next_at - time.monotonic()) if next_at else 0.0
            self.sim_phase_label.configure(
                text=f"●  Running - next product in {remaining:0.0f}s",
                text_color="#2f9e44")

        self.sim_counts_label.configure(
            text=f"goodProduct {good}   badProduct {bad}")

    def _update_sim_buttons(self) -> None:
        running = self.selected_robot_id in self.sim_runners
        state = "normal" if running else "disabled"
        for btn in (self.sim_bad_btn, self.sim_inject_btn,
                    self.sim_clear_btn, self.sim_force_btn):
            btn.configure(state=state)

    def _autoconnect(self) -> None:
        if self.mqtt.is_connected or not self.config_data.broker.get("host"):
            return
        self.log("activity", "info",
                 "Connecting so the trigger topics are watched from the start...")
        self.connect_btn.configure(state="disabled", text="Connecting...")
        self.status_left.configure(text="Connecting...")

        def worker() -> None:
            ok, msg = self.mqtt.connect()
            self.events.put({"kind": "connect_result", "ok": ok, "message": msg})

        threading.Thread(target=worker, daemon=True).start()

    # -------------------------------------------------------- connection
    def _toggle_connection(self) -> None:
        if self.mqtt.is_connected:
            busy = len(self.runners) + len(self.sim_runners)
            if busy and not messagebox.askyesno(
                    APP_NAME, f"{busy} loop(s) and simulation(s) are running. "
                              f"Disconnecting will stop them. Continue?", parent=self):
                return
            self._stop_all(silent=True)
            self._sim_stop_all(kill=True, silent=True)
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

        if changed and self.sim_runners:
            self.log("activity", "warn",
                     "Broker settings changed - simulations stopped. Trigger them "
                     "again once the new connection is up.")
            self._sim_stop_all(kill=True, silent=True)

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
        payload.pop("last_selected_robot", None)
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
        robots = [Robot.from_dict(r) for r in data.get("robots", []) if isinstance(r, dict)]
        broker = data.get("broker") or {}
        if not isinstance(broker, dict) or not broker.get("host"):
            messagebox.showerror(APP_NAME, "That profile has no broker settings in it.",
                                 parent=self)
            return

        busy = len(self.runners) + len(self.sim_runners)
        if busy and not messagebox.askyesno(
                APP_NAME, f"{busy} loop(s) and simulation(s) are running. Importing "
                          f"stops them. Continue?", parent=self):
            return
        self._stop_all(silent=True)
        self._sim_stop_all(kill=True, silent=True)

        self.config_data.presets = presets or [Config._seed_preset()]
        self.config_data.robots = robots or Config._seed_robots()
        self.config_data.last_selected = None
        self.config_data.last_selected_robot = None
        password = data.get("password") or ""
        merged = {**DEFAULT_BROKER, **{k: broker[k] for k in broker if k in DEFAULT_BROKER}}

        self._refresh_preset_list()
        self._select_preset(self.config_data.presets[0].id)
        self._refresh_robot_list()
        self._select_robot(self.config_data.robots[0].id)
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
        loops, sims = len(self.runners), len(self.sim_runners)
        text = f"{loops} loop{'' if loops == 1 else 's'} running"
        if sims:
            text += f"  \u00b7  {sims} robot{'' if sims == 1 else 's'} simulating"
        self.status_right.configure(text=text)

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
            if ok:
                self._sync_subscriptions()

        elif kind == "mqtt_message":
            self._handle_mqtt_message(event["topic"], event["payload"])

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

        elif kind == "sim_started":
            self._set_connection_ui(self.mqtt.is_connected)

        elif kind in ("sim_tick", "sim_counts", "sim_phase"):
            state = self.sim_state.setdefault(event["robot_id"], {})
            for key in ("good", "bad", "next_at", "phase"):
                if key in event:
                    state[key] = event[key]
            self._update_robot_button(event["robot_id"])

        elif kind == "sim_stopped":
            rid = event["robot_id"]
            runner = self.sim_runners.pop(rid, None)
            self.sim_state.pop(rid, None)
            robot = self.config_data.find_robot(rid)
            label = robot.name if robot else rid
            if runner is not None:
                self.log("activity", "info",
                         f"[{label}] run ended - {runner.good} good, {runner.bad} bad")
            self._update_robot_button(rid)
            self._update_sim_buttons()
            self._update_running_count()
            if rid in self._sim_restart:
                self._sim_restart.discard(rid)
                if robot is not None:
                    self.after(150, lambda r=robot: self._sim_start(r))

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
        self._tick_sim()
        self.after(500, self._tick_ui)

    # ------------------------------------------------------------ close
    def _on_close(self) -> None:
        busy = []
        if self.runners:
            busy.append(f"{len(self.runners)} loop(s)")
        if self.sim_runners:
            busy.append(f"{len(self.sim_runners)} robot simulation(s)")
        if busy and not messagebox.askyesno(
                APP_NAME, f"{' and '.join(busy)} still running.\n\nQuit anyway?",
                parent=self):
            return
        if self._sim_dirty:
            keep = messagebox.askyesnocancel(
                APP_NAME, "Save changes to the current robot before quitting?", parent=self)
            if keep is None:
                return
            if keep:
                self._save_robot()
        if self._dirty:
            keep = messagebox.askyesnocancel(
                APP_NAME, "Save changes to the current message before quitting?", parent=self)
            if keep is None:
                return
            if keep:
                self._save_preset()

        for runner in list(self.runners.values()):
            runner.stop()
        # Quitting abandons a run rather than dragging the window open for the
        # length of one more interval to see the stopped message out.
        for sim in list(self.sim_runners.values()):
            sim.kill()
        deadline = time.monotonic() + 2.0
        for runner in list(self.runners.values()) + list(self.sim_runners.values()):
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
