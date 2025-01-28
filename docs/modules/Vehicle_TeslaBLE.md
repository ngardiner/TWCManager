Tesla Bluetooth Low Energy (BLE)
================================

## Introduction

This module provides experimental support for the Tesla BLE (Bluetooth Low Energy) interface to Tesla vehicles.

It is currently capable of starting and stopping charging, and controlling the charge rate in line with the configurable charge rate options (Settings > Charge Rate Control).

## Installation of tesla-control binary

The tesla-control binary can be built for you via the TWCManager Makefile.

### Installation Steps

  * Update twcmanager to at least vx.xx to introduce Tesla BLE support (you could alternatively do this via the web-based update option)

```make install```

  * Install golang and the tesla-command BLE proxy

```make tesla-control```

This will download the required golang distribution, and build the tesla-control binary in the default HOME directory, which is ```/home/twcmanager```. You may have an alternate home location that you would like to use, in which case you would specify make HOME=x tesla-control.

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
  * Enter the vehicle, tap your card on the dash and accept the peering

### Controlling charge rate using tesla-control

By default, TWCManager uses the TWC to control charge rate. This behaviour is configurable through the Web UI. To change this behaviour to leverage the BLE interface for controlling charge rate, perform the following steps:

   * Click on the Settings tab
   * Find the option "Charge Rate Control"
   * Select either of the following options:
      * Use Tesla API Exclusively to control Charge Rate
      * Use TWC >= 6A + Tesla API <6A to control Charge Rate

## FAQs

* *Why does BLE currently require API as a fallback?*

TWCManager was written with an explicit dependency on the API. BLE replaces some but not all of the API's functionality. For example, we cannot discover all vehicles on an account with BLE.

The approach taken is to replace the API calls where an appropriate BLE replacement exists. This has the benefit of using API only as a fallback when BLE fails, which improves reliability.

* *What are the BLE-specific considerations that need to be taken into account?*

BLE being a local wireless technology introduces a number of considerations. The distance between the TWC and the vehicle is important to consider, as weak signals can contribute to instability. If distance is a challenge, there are potential solutions such as an ESP-based tesla-control interface, however support for this does not exist yet.

* *I tried to use the ```teslaControlPath``` setting to call a script which then goes on to call tesla-control. After a period of time, TWCManager background tasks seem to get stuck. Why?

The tesla-control CLI interface has a number of failure cases in which it will hang indefinitely. We work around this by terminating the process after 10 seconds of inactivity, however this does not kill the entire process tree. If you are running the command through some sort of wrapper, it will become zombified whilst the tesla-control command will remain.

If you are attempting to wrap the tesla-control command and want to confirm that this is the issue, perform a ``ps aux | grep tesla-control``` and look for a process which was spawned more than 10 seconds ago.

You can work around this with a similar timeout in the wrapper script, eg ```timeout 9 tesla-control $@```
