# MQTT Trigger

A desktop app for firing saved MQTT messages at a broker on demand — for when
something goes wrong with a system and you need to push a known command without
editing a script first.

Runs on **Windows** and **macOS**. Built from the original `mqtt_publisher.py`,
which is still here and still works.

> **This app ships with no broker details in it.** There is no default host, no
> default username and no default topic — you enter your own on first run, and
> they stay encrypted on your machine. Nothing is uploaded anywhere.

---

## Getting the app

### Windows

Download **`MQTT-Trigger-1.3.exe`** from the
[Releases page](https://github.com/ApollosRGB/Gradion-MQTT-message-sender/releases)
and run it. It is a single self-contained file — the machine running it does
**not** need Python installed.

To rebuild after changing the code, double-click **`build_exe.bat`**.

### macOS

Download the **`.dmg`** for your Mac from the
[Releases page](https://github.com/ApollosRGB/Gradion-MQTT-message-sender/releases):

| Your Mac | File |
|---|---|
| Apple Silicon (M1/M2/M3/M4) | `MQTT-Trigger-1.3-macOS-arm64.dmg` |
| Intel | `MQTT-Trigger-1.3-macOS-x86_64.dmg` |

Not sure which you have?  → menu → **About This Mac**. "Apple M…" means Apple
Silicon.

Open the `.dmg` and drag **MQTT Trigger** onto the **Applications** shortcut
next to it.

**First launch is blocked by Gatekeeper**, because the app is not code-signed —
expected for an internally built tool, not a sign anything is wrong.
Right-click the app → **Open** → **Open**. If macOS still refuses:

```bash
xattr -dr com.apple.quarantine "/Applications/MQTT Trigger.app"
```

To build the disk image yourself on a Mac, double-click
**`build_dmg.command`** (or `build_app.command` for a plain `.app`). If Finder
refuses to run it, `chmod +x build_dmg.command` once in Terminal first. A build
only runs on the architecture that produced it, so build on each kind of Mac
you need to support — that is why the release ships two images.

### Running from source (any platform)

```bash
pip install -r requirements.txt
python mqtt_trigger_app.py
```

---

## First run

The app opens with nothing configured and offers you two ways forward.

**a) Type the details in** — click **Broker settings**, fill in host, port,
username, password and TLS options, hit **Test connection**, then **Save**.

**b) Import a profile** — if a colleague sent you an encrypted `.mqttprofile`
file, choose **Profile → Import profile…** and enter the passphrase they gave
you. Their broker settings and saved messages land in your copy of the app.

Until a broker is set, Connect / Send once / Start loop will tell you so rather
than failing with a confusing connection error.

---

## Where your details live, and how they are protected

Everything stays on the machine you run the app on. There is no server, no
account, no telemetry and no sync.

| What | Where it is kept | How it is protected |
|---|---|---|
| Broker password | OS credential vault (Windows Credential Manager / macOS Keychain / Linux Secret Service) | Managed by the OS, under the service name `MQTTTrigger` |
| Host, username, topics, payloads, theme | `config.vault` in your app-settings folder | Encrypted with AES via Fernet. The key is random per machine and is itself held in the OS credential vault |
| Exported profiles | Wherever you save the `.mqttprofile` file | Encrypted with a passphrase you choose, key derived with PBKDF2-HMAC-SHA256 (480,000 rounds) |

Settings folder:

| Platform | Location |
|---|---|
| Windows | `%APPDATA%\MQTTTrigger\` |
| macOS | `~/Library/Application Support/MQTTTrigger/` |
| Linux | `~/.config/MQTTTrigger/` |

Because the at-rest key never leaves the machine that generated it, **copying
`config.vault` to another computer will not work** — the app there cannot read
it, and will say so rather than silently discarding it. Use Export / Import
profile to move a setup, which is what that feature is for.

If you upgrade from 1.1 or earlier, your old plain-text `config.json` is read
once, re-saved encrypted, and then deleted. Nothing is lost.

> **What encryption at rest does and does not do.** It stops your broker details
> being readable by anything that can see the file — a backup, a synced folder,
> someone glancing at your disk, a support bundle. It is **not** protection
> against someone already logged in as you on your own machine: the OS hands
> your credential vault to programs running as you, by design. Treat it as
> "these details are not lying around in the clear", not as a safe.

---

## Sharing a setup with a colleague

1. **Profile → Export profile…**
2. Choose a passphrase (8 characters minimum — it is the only thing protecting
   the file, and there is no recovery if you forget it).
3. Optionally tick **Include the broker password in the file**. Leave it
   unticked and the recipient supplies their own password.
4. Send them the `.mqttprofile` file. **Send the passphrase by a different
   route** — if the file and its passphrase travel in the same chat, the
   encryption has bought you nothing.

They import it with **Profile → Import profile…**. A wrong passphrase, a
truncated file or an altered one is rejected with a clear message rather than
producing garbage.

---

## Using it

**Saved messages** (left panel) — each one holds a name, topic, payload,
interval, QoS and retain flag.

| Button | What it does |
|---|---|
| **+ New** | Blank message |
| **Duplicate** | Copy the selected one — handy for variants of the same command |
| **Delete** | Remove it (one message must always remain) |
| **Stop all** | Stop every running loop at once |

**Editing** — change the fields on the right and press **Save**
(`Ctrl+S`, or `Cmd+S` on macOS). The Save button shows `Save *` while you have
unsaved changes. The badge above the payload box tells you live whether it is
valid JSON, and **Format JSON** tidies up the indentation. Non-JSON payloads are
allowed — you just get a confirmation prompt.

**Sending**

- **Send once** (`Ctrl+Enter` / `Cmd+Enter`) — one message, right now.
- **▶ Start loop** — sends immediately, then repeats every N seconds until you
  press **■ Stop**.
- **Send every \_\_ seconds** — the interval. Type any number (decimals fine,
  minimum 0.1), or pick from the **Quick set** dropdown. Saving while a loop is
  running restarts it automatically so the new rate takes effect immediately.

**Several loops at once** — every message runs independently. Start three at
three different rates if you want; `●` marks a running message in the list, `○`
an idle one, and the bottom-right corner counts the active loops.

**Connecting** — the app connects on its own the first time you Start or Send.
The **Connect** button in the header is there if you want to connect ahead of
time so the first message goes out instantly. If the connection drops mid-loop
it reconnects on its own and carries on.

---

## Watching what goes out

The bottom half has two tabs:

- **Activity** — every publish, colour-coded: blue `TX` lines with topic, QoS
  and the exact payload sent; red `FAIL` lines with the reason; grey notes for
  loops starting and stopping.
- **Debug** — connection events, CONNACK results, TLS settings in use, client
  ID, reconnect warnings, and whether encryption at rest is active. This is the
  tab to look at when a connection won't come up.

**Auto-scroll** keeps the newest line in view; **Export log…** writes the
current tab to a `.log` file; **Clear** empties it. The view holds the last
2000 lines.

Exported logs contain the topics and payloads you sent — treat them the same way
you would treat the settings.

---

## Light / dark

The **System / Light / Dark** switch is top-right. *System* follows your OS
theme on both Windows and macOS. Your choice is remembered between launches.

---

## The original script

`mqtt_publisher.py` still publishes a fixed message on a timer, for terminals
and cron-style automation. It reads everything from the environment, so the file
itself says nothing about your broker:

| Variable | Meaning |
|---|---|
| `MQTT_HOST` | broker hostname (required) |
| `MQTT_TOPIC` | topic to publish to (required) |
| `MQTT_PORT` | broker port (default 8883) |
| `MQTT_USERNAME` | broker username (optional) |
| `MQTT_PASSWORD` | broker password (prompted if unset) |
| `MQTT_INTERVAL` | seconds between messages (default 60) |
| `MQTT_PAYLOAD` | JSON payload to publish |
| `MQTT_TLS` | `1` or `0` — use TLS (default 1) |
| `MQTT_VERIFY` | `1` or `0` — validate the certificate (default 0) |

```bash
export MQTT_HOST="broker.example.com"
export MQTT_TOPIC="example/device/cmd"
export MQTT_USERNAME="alice"
python mqtt_publisher.py            # prompts for the password
```

```powershell
$env:MQTT_HOST = "broker.example.com"
$env:MQTT_TOPIC = "example/device/cmd"
python mqtt_publisher.py
```

Leave `MQTT_PASSWORD` unset and it prompts without echoing, which keeps the
password out of your shell history.

Launchers: `Run_MQTT_Publisher.bat` (Windows), `Run_MQTT_Publisher.command` (macOS).

---

## Notes

- Closing the window while loops are running asks for confirmation first.
- Changing broker settings while loops are running stops them, reconnects with
  the new settings, and restarts them.
- Custom icon: drop an `icon.ico` (Windows) or `icon.icns` (macOS) next to the
  build script and rebuild — it gets picked up automatically.
- `build/`, `dist/` and `*.spec` are build working files. They are gitignored
  and are recreated on each build.
- Releases are built by `.github/workflows/release.yml` — pushing a `v*` tag
  builds the Windows `.exe` and both macOS `.dmg` files and attaches them to the
  release.

---

## Contributing

Pull requests welcome. One rule, because this repository is public:

**Never commit a real broker hostname, username, password, topic or payload.**
Use `example.com` / `example/device/cmd` style placeholders. Real values belong
in your local settings or an exported profile, not in the source.
