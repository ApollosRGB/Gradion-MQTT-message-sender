# MQTT Trigger

A desktop app that does three jobs against one broker connection:

- **Messages** — fire saved MQTT messages on demand, or on a repeating timer,
  for when something goes wrong with a system and you need to push a known
  command without editing a script first.
- **Robot simulator** *(new in 2.0)* — stand in for a robot arm that is not
  built yet. Each simulated robot waits for a trigger on its own topic, then
  reports `running`, counts products, and reports `finished` and `stopped` when
  the trigger goes away.
- **Topic monitor** *(new in 2.3)* — point it at a topic and it keeps every
  update that arrives there, so an AGV reporting for itself can be checked here
  rather than in a second tool alongside this one.

Runs on **Windows** and **macOS**. Built from the original `mqtt_publisher.py`,
which is still here and still works.

> **This app ships with no broker details in it.** There is no default host, no
> default username and no default topic — you enter your own on first run, and
> they stay encrypted on your machine. Nothing is uploaded anywhere.

---

## Getting the app

### Windows

Download **`MQTT-Trigger-2.4.1.exe`** from the
[Releases page](https://github.com/ApollosRGB/Gradion-MQTT-message-sender/releases)
and run it. It is a single self-contained file — the machine running it does
**not** need Python installed.

To rebuild after changing the code, double-click **`build_exe.bat`**.

### macOS

Download the **`.dmg`** for your Mac from the
[Releases page](https://github.com/ApollosRGB/Gradion-MQTT-message-sender/releases):

| Your Mac | File |
|---|---|
| Apple Silicon (M1/M2/M3/M4) | `MQTT-Trigger-2.4.1-macOS-arm64.dmg` |
| Intel | `MQTT-Trigger-2.4.1-macOS-x86_64.dmg` |

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

Once a broker **is** set, the app connects by itself on launch and subscribes to
every simulated robot's trigger topic. That matters: a trigger that arrives
before the app is subscribed is gone, and the robot would sit there looking
broken.

---

## Where your details live, and how they are protected

Everything stays on the machine you run the app on. There is no server, no
account, no telemetry and no sync.

| What | Where it is kept | How it is protected |
|---|---|---|
| Broker password | OS credential vault (Windows Credential Manager / macOS Keychain / Linux Secret Service) | Managed by the OS, under the service name `MQTTTrigger` |
| Host, username, topics, payloads, simulated robots, watched topics, theme | `config.vault` in your app-settings folder | Encrypted with AES via Fernet. The key is random per machine and is itself held in the OS credential vault |
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

A profile carries the broker settings, the saved messages, the simulated
robots **and** the watched topics, so a colleague ends up with the same line
laid out the same way. Caught messages are not in it — those are a recording of
your session, not a setting.

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

The window has three tabs — **Messages**, **Robot simulator** and **Topic
monitor** — sharing one broker connection and one log at the bottom.

---

## Messages

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
unsaved changes.

The badge above the payload box says what it is holding. **valid JSON** for an
object or an array, which **Format JSON** will tidy the indentation of, and
**raw text — sent as typed** for everything else. Raw is a payload in its own
right, not a mistake: a payload of `True` puts the five characters `True` on the
topic, which is exactly what a trigger topic carries. The **True** and **False**
buttons next to the badge fill the box with those words in one click. Only a
payload that opens with `{` or `[` and then fails to parse is flagged, and that
is the only case that asks you to confirm on save.

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

## Robot simulator

Simulates one or more robot arms reporting to a line controller. The app never
starts a run on its own: it waits for a trigger message, exactly as a real robot
would.

### The topics

Each robot has a **base topic**, and the other two follow from it:

| | Topic | Who publishes |
|---|---|---|
| Status | `<base>/status` | this app |
| Trigger | `<base>/cmd/trigger` | you, or the real line controller |

For `Openmind/robot01` that gives `Openmind/robot01/status` and
`Openmind/robot01/cmd/trigger`. Rename the base topic and both follow it. If
your line does not lay its topics out that way, type either one in by hand and
it stays put.

The app ships with **Openmind robot01** and **KUKA robot02** ready to go. Use
**+ Add** for more — every robot has its own topics, interval and counters, and
they all run independently.

### What a run looks like

Send `{"trigger": "true"}` to the trigger topic and the robot starts. With the
interval at 5 seconds:

```
t=0s    {"state":1,"stateName":"running","goodProduct":0,"badProduct":0,
         "errorCode":0,"errorName":"","ts":"2026-08-24T14:34:00.680+07:00"}
t=5s    ... "goodProduct":1 ...
t=10s   ... "goodProduct":2 ...
```

Send `false` and it winds down. How long it sits on `finished` is its own
setting — **not** the product interval — so a line that ticks every second can
still take ten to clear down:

```
at once        {"state":3,"stateName":"finished","goodProduct":2, ...}
stopped delay  {"state":0,"stateName":"stopped","goodProduct":0,"badProduct":0, ...}
later
```

Set that delay to `0` and `stopped` follows `finished` immediately.

The counters reset with every run, so the next trigger starts again from zero.
`ts` is this machine's local time with milliseconds and its real UTC offset.

**Trigger payloads** are read leniently. The plain word on its own is the
normal case — `true` starts a run, `false` ends one — and these all work too:

```json
{"trigger": "true"}   {"trigger": true}   {"trigger": 1}   {"trigger": "start"}
```

Bare `1`, `start`, `on` also start; `0`, `off` and `stop` also stop. Case does
not matter, so `False` from a Python publisher is read the same as `false`. Anything else is logged and ignored rather than guessed at.
A trigger for a robot that is already running is ignored, and so is a stop for
one that is not running.

### Driving it without a second tool

**Test the trigger: send true / send false** publishes the trigger for you, to
the robot's own trigger topic. It goes out as the bare word — `true` or
`false`, no JSON wrapper — which is what the line controller puts on the topic.
It comes back through the subscription like any other message, so it exercises
exactly the path the controller will.

### Faults

Nothing goes wrong unless you make it. While a robot is running:

- **+1 bad product** — bumps `badProduct` and publishes straight away.
- **Inject error…** — choose the `state`, `stateName`, `errorCode` and
  `errorName` to report. Tick **Keep reporting this until I clear it** and every
  status from then on carries the error (the products keep counting); leave it
  unticked for a single error message.
- **Clear error** — back to `running`.
- **Force stop** — abandons the run *without* the finished and stopped
  messages. For when you want to simulate a robot that fell off the network.

### Settings

**One product every \_\_ seconds** is the tick rate. **Stopped message \_\_
seconds after finished** is separate from it — the two answer different
questions, so changing how fast products come off the line does not change how
long the robot sits on `finished`. **QoS** defaults to 1 and **Retain** is off;
all four are per robot. **Save** stores the robot; a robot that is mid-run
keeps the topic and interval it started with until the next trigger.

Quitting the app abandons any run in progress rather than holding the window
open for one more interval — nothing further is published.

---

## Topic monitor

*New in 2.3.* The other two tabs are about what you send. This one is about
what comes back — an AGV, a PLC, a robot that is already real, anything that
reports for itself. Give it a topic and every update published there is kept,
laid out and exportable, with no second tool open beside this one.

### Watching a topic

**Watched topics** (left panel) — each entry holds a name, a topic, a QoS and
whether it is currently watching. **+ Add** makes one, **Duplicate** copies the
selected one, **Delete** removes it, and **Clear caught** empties the buffer
without touching the list.

Fill in **Topic**, tick **Watching**, press **Save** — or just press **Enter**
in the box, or click away from it, which saves the edit for you. A topic that
was typed but never saved is not a topic anybody is subscribed to, and that is
the difference between a watch that looks live and one that is.

The subscription goes out straight away when the app is connected, and on the
next connection otherwise — so a watch set up before the broker is reachable
still works once it is.

*New in 2.3.1.* Asking is not the same as being subscribed. A broker is free to
turn a filter down — an ACL that does not cover it is the usual reason — or to
grant it at a lower QoS than you asked for, and neither arrives as an error you
would otherwise see. The app now waits for the broker's answer and says what it
was:

| The watch shows | What the broker said |
|---|---|
| ● | subscribed, at the QoS asked for |
| ● *granted QoS 0, not 1* | subscribed, with weaker delivery guarantees |
| ✕ *Broker refused this topic* | turned down — nothing published there will arrive |

A refusal goes into the Activity log in red as well. Without this a refused
topic looks exactly like a quiet one, and a `.../state` that never updates is
indistinguishable from an AGV that is standing still.

Wildcards are the point of this, not an extra:

| Topic | Catches |
|---|---|
| `agv/01/state` | that one topic |
| `agv/+/state` | the state of every AGV, one level down |
| `agv/#` | everything published anywhere under `agv` |

`#` has to be the last level, `+` has to fill a whole level, and neither may sit
next to other characters — the app says which rule a topic breaks rather than
sending a subscription the broker will reject. Untick **Watching** to stop a
watch without losing it; the entry stays, and the subscription is dropped.

Watching costs nothing on the publishing side: the trigger topics the simulator
needs and the topics you are watching are merged into one subscription list, at
the higher QoS wherever they overlap.

### Reading what arrived

The feed underneath shows **the watch selected on the left**, one line per
message — time, `[R]` if the broker sent it as a retained message, topic, then
the payload flattened onto one line. Click a line and the panel at the bottom
shows that message in full: topic, QoS, retained or not, the timestamp it was
caught at, and the payload laid out as JSON when it is JSON, or exactly as it
arrived when it is not.

Picking a different watch switches the feed to that topic. The others keep
catching in the background — their counts carry on climbing in the list — they
are simply not on screen, which is what stops a 10 Hz topic burying the one you
came here to read. **Show all in one feed**, above the list, puts every watch
back into a single feed when comparing them is the point.

- **Follow latest** (ticked by default) keeps the newest message selected and in
  view. Clicking a line unticks it, because you are reading that one now — tick
  it again to go back to following.
- **Filter** narrows the feed to messages whose topic or payload contains what
  you type. It filters the view, not the buffer; clearing it brings everything
  back.
- **Pause** stops keeping messages. The subscription stays up — pausing means
  "stop filling the buffer", not "stop listening" — so ticking it again picks
  up from whatever arrives next.
- **Copy payload** puts the selected payload on the clipboard, formatted the way
  the panel shows it.
- **Export**, above the feed, asks which before it asks where:

  | Choice | Writes |
  |---|---|
  | **This message…** | only the message selected in the feed — for pulling one state update out of a long session without trimming a file by hand afterwards |
  | **Everything in the feed…** | every message the feed is showing, so the selected watch and the filter both apply |

  **This message…** names the file after the topic, so `uagv/v2/mfr/agv1/state`
  gives `mqtt-message-0042-uagv-v2-mfr-agv1-state.json`.

Both write `.json` (one object per message, with sequence number, timestamp,
topic, QoS, retain flag and payload) or `.csv` (the same, one row per message);
the file extension you choose decides the format, and the shape is the same
either way, so one reader handles both.

The buffer holds the last 2000 messages across every watch; older ones fall off
the end. A count next to each watch shows how many messages it has caught, and
the counter above the feed reads `5 of 340 caught` when what is on screen is a
subset of what is held.

### From a script

The tab is a front end for one method, so anything driving the app can watch a
topic the same way:

```python
app.watch_topic("agv/+/state", qos=1, name="AGV fleet")

app.latest("agv/01/state").payload     # the last update from that AGV
app.captured("agv/#")                  # everything caught below agv/, oldest first
app.unwatch_topic("agv/+/state")       # stop, keeping what was caught
```

`watch_topic` subscribes, saves the watch so it comes back next launch, and
returns it. Asking for a topic that is already watched re-enables it and raises
its QoS if needed rather than adding a second copy. It returns `None`, and says
why in the log, for a topic no broker would accept.

---

## Watching what goes out

The bottom half has two tabs:

- **Activity** — every publish, colour-coded: blue `TX` lines with topic, QoS
  and the exact payload sent; purple `RX` lines for triggers arriving; red
  `FAIL` lines with the reason; grey notes for loops and simulations starting
  and stopping. Messages caught by a watch do **not** get a line each — a
  device publishing ten times a second would bury everything else — so the
  first sighting of each topic is noted here and the rest go to the Topic
  monitor.
- **Debug** — connection events, CONNACK results, `SUB` / `UNSUB` lines for the
  trigger and watched topics, TLS settings in use, client ID, reconnect
  warnings, and
  whether encryption at rest is active. This is the tab to look at when a
  connection won't come up, or when a trigger you sent never arrived.

**Auto-scroll** keeps the newest line in view; **Export log…** writes the
current tab to a `.log` file; **Clear** empties it. The view holds the last
2000 lines.

Exported logs contain the topics and payloads you sent, and exported captures
contain what your devices published — treat both the same way you would treat
the settings.

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

- Closing the window while loops or simulations are running asks for
  confirmation first.
- If the connection drops, the app reconnects and re-subscribes to the trigger
  and watched topics on its own — a dropped session otherwise loses its
  subscriptions silently.
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
