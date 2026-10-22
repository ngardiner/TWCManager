Tesla Bluetooth Low Energy (BLE)
================================

## Introduction

This module provides experimental support for the Tesla BLE (Bluetooth Low Energy) interface to Tesla vehicles.

It is currently capable of starting and stopping charging, and controlling the charge rate in line with the configurable charge rate options (Settings > Charge Rate Control).

## Installation of tesla-control binary

The tesla-control binary can be built for you via the TWCManager Makefile.

### Installation Steps

  * Update twcmanager to at least vx.xx (update after release) to introduce Tesla BLE support (you could alternatively do this via the web-based update option)

```make install```

  * Install golang and the tesla-command BLE proxy

```make tesla-control```

This will download the required golang distribution, and build the tesla-control binary in the default HOME directory, which is ```/home/twcmanager```. You may have an alternate home location that you would like to use, in which case you would specify ```make HOME=/home/$USER tesla-control```.

If you would prefer to build tesla-control yourself, or you are able to install it as part of a binary package distribution, you can skip this step.

  * Restart twcmanager

```systemctl restart twcmanager```

### Configuring binary path

If you have installed tesla-control into a path which is outside of your system $PATH, or if TWCManager is unable to detect the location of tesla-control, you can configure the

```
    "config": {
        "teslaControlPath": "/usr/local/bin/tesla-control-local",
```

### Peering with your vehicle

Once TWCManager restarts, it will check for the tesla-control command. If this exists, you will see a new option under the Vehicles > [Vehicle VIN] menu, under the heading BLE Control. There will be an option to pair with the vehicle.

  * Ensure the vehicle is near to your TWCManager device, and that you have your key card available.
  * Click the Pair with Vehicle button, which will initiate Bluetooth Pairing
  * Enter the vehicle, tap your card on the card reader location in front of the center console and accept the peering

### Controlling charge rate using tesla-control

By default, TWCManager uses the TWC to control charge rate. This behaviour is configurable through the Web UI. To change this behaviour to leverage the BLE interface for controlling charge rate, perform the following steps:

   * Click on the Settings tab
   * Find the option "Charge Rate Control"
   * Select either of the following options:
      * Use a vehicle command (BLE or API) exclusively to control charge rate
      * Use TWC >= 6A + vehicle command (BLE or API) < 6A

## FAQs

* *Why does BLE currently require API as a fallback?*

TWCManager was written with an explicit dependency on the API. BLE replaces some but not all of the API's functionality. For example, we cannot discover all vehicles on an account with BLE.

The approach taken is to replace the API calls where an appropriate BLE replacement exists. This has the benefit of using API only as a fallback when BLE fails, which improves reliability.

* *Why does TWCManager sometimes not send a command to a sleeping car?*

Lowering a charge limit never wakes the car. Before a BLE command that may not wake the car, TWCManager runs a sleep-safe check (the car's answer says whether it is asleep); before such a Tesla API command, the free online check. If the car is asleep, the command waits, and this is logged once. While the car is known to be asleep, TWCManager runs nothing at all, so it does not retry every few minutes. The one exception is a car no live telemetry feed reports on: the vehicle watch's status check looks again every 15 minutes while the car is home, and every 30 minutes elsewhere. A held Tesla API command makes the free online check on the same clock, while it is held, unless the watch already asks the cloud (for a car only the Tesla API can see). An asleep answer changes nothing and is not logged again; an awake one sends the command.

The sleep-safe check needs no pairing: it works for any car in range, with no key. While the car is awake it also reports whether the charge-port door is open. A closed door counts as unplugged. An open door is only a hint, and a BLE charge read confirms whether the car is plugged in.

The car counts as awake again on a sign of life: a newer awake state from any source, a plug or unplug, a drive start or end, current drawn, or a successful wake. The waiting command is then tried again. Commands that may wake the car still wake it, except while the car's charger is offered 0 A; once the offer rises, the car is woken for the raise.

* *What does a range scan need, and what does "No range scan" mean?*

A range scan listens for the car's Bluetooth advertisement with `bluetoothctl`, so it needs the `bluez` package and a running `bluetoothd` with a powered, unblocked adapter on the host. In Docker, the image includes `bluez`. `bluetoothctl` talks to the host's `bluetoothd` through the `/run/dbus` mount and the `DBUS_SYSTEM_BUS_ADDRESS` setting in `contrib/docker/docker-compose.yml`, and `tesla-control` reaches the host's Bluetooth through `nsenter`, which needs the `CAP_SYS_PTRACE` capability listed there to open the host's network namespace. When no scan is possible, TWCManager logs a warning saying why (for example, an adapter powered off or blocked by rfkill), shows the reason on the vehicle's page in place of the Scan for vehicle button, and checks again every 10 minutes. What happens until then depends on the reason. If the radio itself cannot work (no controller, an adapter powered off or blocked, or `bluetoothd` not answering), the sleep-safe check cannot run either, so each car with a Tesla API connection is watched through the API alone, as if TeslaBLE were not loaded (see Vehicle watch in [Settings](../Settings.md)), until the radio is back. If only `bluetoothctl` is missing or cannot reach `bluetoothd`, the sleep-safe check still works: a car that leaves is noticed when it misses two checks, and its return when one answers, on the scan back-off. The warning ends with which of the two applies ("cars watched by the Tesla API" or "cars watched by pings alone"). A car at home that misses two checks in a row stays on Bluetooth's checks, and if it is awake it gets one charge read through the cloud (once, until Bluetooth answers again), which can see that it has left.

* *What are the BLE-specific considerations that need to be taken into account?*

BLE being a local wireless technology introduces a number of considerations. The distance between the TWC and the vehicle is important to consider, as weak signals can contribute to instability. If distance is a challenge, there are potential solutions such as an ESP-based tesla-control interface, however support for this does not exist yet.

* *I tried to use the ```teslaControlPath``` setting to call a script which then goes on to call tesla-control. After a period of time, TWCManager background tasks seem to get stuck. Why?

The tesla-control CLI interface has a number of failure cases in which it will hang indefinitely. We work around this by terminating the process after 15 seconds of inactivity (the `commandTimeout` setting), however this does not kill the entire process tree. If you are running the command through some sort of wrapper, it will become zombified whilst the tesla-control command will remain.

If you are attempting to wrap the tesla-control command and want to confirm that this is the issue, perform a ``ps aux | grep tesla-control``` and look for a process which was spawned more than 15 seconds ago.

You can work around this with a similar timeout in the wrapper script, eg ```timeout 14 tesla-control $@```
