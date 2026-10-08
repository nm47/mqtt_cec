# Project Context: MQTT-Controlled HDMI-CEC TV Power Service

## Overview

We have a Raspberry Pi connected to a TV via HDMI. This Pi has a **single responsibility**:
listen for MQTT power commands and control the TV’s power state using **HDMI-CEC**.

The Pi runs a **containerized service** that subscribes to MQTT topics, sends the matching
HDMI-CEC messages through the kernel CEC adapter (`/dev/cec0`), and publishes the TV's actual
power and input state.

The equivalent manual commands (handy for debugging with `docker exec mqtt_cec_controller ...`):

```bash
cec-ctl -d0 --to 0 --image-view-on                        # Power ON TV
cec-ctl -d0 --to 0 --standby                              # Power OFF TV
cec-ctl -d0 --to 0 --give-device-power-status             # Query power
cec-ctl -d0 --to 15 --active-source phys-addr=3.0.0.0     # Switch to HDMI3
cec-ctl -d0 -S                                            # Show bus topology
```

---

## Goals

### Functional Goals

* Subscribe to MQTT topics for TV power control and input switching
* Execute HDMI-CEC commands based on received messages
* Support:

  * Power ON
  * Power OFF
  * HDMI Input Switching (HDMI1-4)

### Operational Goals

* Run the service via **Docker Compose**
* Automatically restart the container if it crashes
* Allow the service to remain stopped if explicitly stopped by the user
* Be simple, reliable, and purpose-built (no extra features)

---

## MQTT Topic Pattern

This service uses **Tasmota-compatible** MQTT topic patterns for seamless integration with existing smart home devices.

### Topic Hierarchy

* **Command Topics** (subscribe):
  * `cmnd/{device}/POWER` - Power control commands: `ON`, `OFF`, `TOGGLE` (or `1`, `0`, `2`)
  * `cmnd/{device}/INPUT` - Input switching commands: `HDMI1`, `HDMI2`, `HDMI3`, `HDMI4`
  * An empty payload on either topic queries the current value
  * Retained commands are ignored, so they can't re-run on every reconnect
* **Status Topics** (publish):
  * `stat/{device}/RESULT` - Reply to every command, e.g. `{"POWER": "ON"}`
  * `stat/{device}/POWER`, `stat/{device}/INPUT` - Plain values, published on every change
* **Telemetry Topic** (publish): `tele/{device}/STATE`
  * Publishes periodic state updates as JSON
  * Published every 300 seconds (configurable)
  * Also published immediately whenever the TV's state changes, including changes made
    with the TV's own remote
* **Availability Topic** (publish): `tele/{device}/LWT`
  * Last Will and Testament for service availability
  * Values: `Online`, `Offline`

Where `{device}` is the configured device name (default: `tv`).

### Example Topics

With the default device name `tv`:

* Commands:
  * `cmnd/tv/POWER` (subscribe)
  * `cmnd/tv/INPUT` (subscribe)
* Status: `stat/tv/RESULT`, `stat/tv/POWER`, `stat/tv/INPUT` (publish)
* State: `tele/tv/STATE` (publish)
* Availability: `tele/tv/LWT` (publish)

### Telemetry Payload

State updates are published as JSON:

```json
{
  "Time": "2025-12-23T14:30:00",
  "Uptime": "0T12:34:56",
  "POWER": "ON",
  "INPUT": "HDMI4"
}
```

`POWER` is `ON`, `OFF` or `UNKNOWN`. `INPUT` is `HDMI1`-`HDMI4`, `TV` (the TV's own home
screen/apps) or `UNKNOWN` (nothing observed since startup).

## MQTT Behavior

* An MQTT broker must exist on the network
* The service **subscribes** to command topics and **publishes** telemetry
* Payloads are simple and explicit (e.g. `ON`, `OFF`)
* `POWER` reflects the TV's real state: the service polls it with `<Give Device Power Status>`
  and listens for the TV's `<Standby>` broadcasts
* `INPUT` follows the TV's `<Routing Change>` / `<Active Source>` announcements, and is set
  optimistically when a switch command is sent
* `ON`/`OFF` are idempotent: the TV's power is checked first and nothing is sent if it is
  already in the requested state, unless an opposite command is still pending
* `TOGGLE` reverses a pending command during the settling period; otherwise it queries
  the TV first. If that query fails, it sends no power command rather than guessing
  from cached state

---

## Containerization Requirements

### Dockerfile

* Build a minimal image capable of:

  * Connecting to an MQTT broker
  * Talking to the kernel CEC API (pure Python ioctls, no libcec)
* `v4l-utils` is included only for `cec-ctl` debugging
* Designed for ARM (Raspberry Pi), with the `vc4-kms-v3d` overlay providing `/dev/cec0`

### Docker Compose

* Define a single service for the TV controller
* Use a restart policy that:

  * Restarts on failure or reboot
  * Does **not** restart if explicitly stopped by the user
* Allow configuration via environment variables (MQTT host, topic, etc.)

---

## Runtime Behavior

1. Container starts
2. Claims a **Playback Device** logical address on `/dev/cec0` with the configured OSD name
   (this is the name the TV shows for the Pi's input)
3. Starts following CEC bus traffic and polling the TV's power status
4. Connects to the MQTT broker (retrying in the background if it is unreachable)
5. Subscribes to the command topics
6. On message, sends CEC messages:

   * `ON` → `<Image View On>` to the TV
   * `OFF` → `<Standby>` to the TV
   * `HDMIn` → broadcast `<Active Source>` with physical address `n.0.0.0`
     (plus `<Image View On>` when `n` is the Pi's own input, i.e. One Touch Play)
7. Publishes state whenever the TV reports a change
8. Answers the TV's queries (power status, `<Request Active Source>`), as a CEC follower must

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `CEC_ADAPTER` | `/dev/cec0` | Kernel CEC device |
| `CEC_OSD_NAME` | `Raspberry Pi` | Name shown by the TV for this input (max 14 chars) |
| `CEC_POLL_INTERVAL` | `30` | Seconds between power polls; `0` polls only at startup and when the TV wakes |
| `CEC_REMOTE_KEYS` | `true` | Turn TV remote buttons into keystrokes on the Pi (`vc4-hdmi` input device) while the Pi is the active input |

See `.env.example` for the MQTT settings.

## TV Notes (TCL 50S450R Roku TV)

Observed on the target TV:

* CEC 1.4. Supports power status queries, `<Image View On>`, `<Standby>`, and switching
  inputs via `<Active Source>` for other ports' physical addresses
* Does not support `<Set OSD String>`, `<Give Audio Status>` or system audio (audio goes
  out over optical, so volume control isn't available over CEC)
* Input switches take anywhere from ~1s to ~7s. Switch commands sent in quick succession
  can land out of order
* Keeps reporting `on` for ~3s after `<Standby>`, so polled states that contradict a recent
  command are ignored for 15s
* Sometimes sends each `<Report Power Status>` reply twice, and broadcasts `<Standby>` twice

## Tests

Install `requirements.txt`, then run `python -m unittest discover -s tests -v` from this
directory. The tests simulate the TV and MQTT client; no CEC hardware or broker is needed.

---

## Non-Goals

* No UI or web interface
* No Home Assistant auto-discovery (unless added later)
* No advanced CEC routing or device management

---

## Design Philosophy

* One job, done well
* Minimal dependencies
* Deterministic behavior
* Easy to reason about and recover
* Container-first deployment

This service exists solely to bridge **MQTT → HDMI-CEC power control** in the most reliable way possible.

---

## Migration Guide (v1.x to v2.x)

If you're upgrading from an older version that used the simple `MQTT_TOPIC` configuration, follow these steps:

### Configuration Changes

**Old configuration (deprecated):**
```bash
MQTT_TOPIC=tv/power
```

**New configuration (Tasmota-compatible):**
```bash
MQTT_DEVICE_NAME=tv
MQTT_STATE_INTERVAL=300  # Optional, defaults to 300 seconds
```

### Topic Changes

| Old Topic | New Topic | Purpose |
|-----------|-----------|---------|
| `tv/power` | `cmnd/tv/POWER` | Command topic (subscribe) |
| N/A | `tele/tv/STATE` | State telemetry (publish) |
| N/A | `tele/tv/LWT` | Availability status (publish) |

### Update Your Automation

Update any scripts or automation that send commands to the TV:

**Before:**
```bash
mosquitto_pub -t "tv/power" -m "ON"
```

**After:**
```bash
mosquitto_pub -t "cmnd/tv/POWER" -m "ON"
```

### v3: Direct kernel CEC

`cec-client` (libcec) and the `cec-ctl` subprocess are gone; the service talks to `/dev/cec0`
itself. `CEC_DEVICE` is ignored (it was actually cec-client's log level). In `docker-compose.yml`
the container now gets `/dev/cec0` instead of `/dev/vchiq` and no longer needs `privileged`.

### New Features

The updated service now publishes:

* **Availability status** via `tele/tv/LWT` (`Online`/`Offline`)
* **State telemetry** via `tele/tv/STATE` (JSON with current power state and uptime)

You can use these topics to monitor the service status in your home automation system.
