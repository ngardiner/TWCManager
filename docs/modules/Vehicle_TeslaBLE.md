Tesla Bluetooth Low Energy (BLE)
================================

## Introduction

This module provides experimental support for the Tesla BLE (Bluetooth Low Energy) interface to Tesla vehicles.

## Installation of tesla-control binary

The tesla-control binary can be built for you via the TWCManager Makefile.

### Installation Steps

  * Install golang and the tesla-command BLE proxy

```make tesla-control```

  * Update twcmanager (you could alternatively do this via the web-based update option)

```make install```

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

