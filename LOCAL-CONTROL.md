Local Vehicle Control
=====================

This is a temporary readme for local control of Tesla Vehicles.

The situation at the moment is that while this functionality exists, it is very difficult to produce a mature interface, as:

  * Most python BLE modules do not cleanly install on RPi, for example:
    * SimplePyBLE doesn't have a wheel for arm6l and doesn't compile cleanly 
    * bleak requires some build, and does build successfully, but has a long-running bug which means it cannot pair with a Tesla
    * python3-bluez is an option but is unsupported going forward
    * pyteslable looks useful, but breaks with recent versions of the cryptography module due to the use of 4-byte nonces and requires manual editing of sources to fix that, plus requires SimplePyLBE.
  * Even the golang version distributed in some raspbian distributions is lower than the minimum required (1.20) for support with the tesla-control binary, although 1.20 will still fail due to a different issue and 1.21+ is required. We use 1.23 which is what Tesla tested it with.

Installation
============

  * Install golang and the tesla-command BLE proxy

make tesla-control

  * Update twcmanager (you could alternatively do this via the web-based update option)

make install

  * Restart twcmanager

systemctl restart twcmanager

Once TWCManager restarts, it will check for the tesla-control command. If this exists, you will see a new option under the Vehicles > [Vehicle VIN] menu, under the heading BLE Control. There will be an option to pair with the vehicle.

  * Ensure the vehicle is near to your TWCManager device, and that you have your key card available.
  * Click the Pair with Vehicle button, which will initiate Bluetooth Pairing
  * Enter the vehicle, tap your card on the dash and accept the peering


Honest Assessment of Current State
==================================

* Pairing works, but there isn't currently positive feedback that confirms peering, might need to be achieved through pinging vehicles

* Should co-exist fine with non-BLE installs. 

* Does not differentiate between vehicles, even though BLE is driven by VIN. Will stop or start all vehicles currently.

* Start and Stop charging works, BLE takes priority over API. Code should identify successful stop/start

* Does not control charge rate for vehicles, but could do so quite easily. Will be next step I would say.

* BLE control is generally stable, but temperamental. I have observed that after a period of time, use of BLE control can contribute to walk up unlock not working or requiring bluetooth to be turned off/on on phone, there are also reports of potential instability if there is significant distance between the TWCManager device and the vehicle.
