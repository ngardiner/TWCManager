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

Honest Assessment of Current State
==================================

* Pairing works, but there isn't currently positive feedback that confirms peering, might need to be achieved through pinging vehicles

* Should co-exist fine with non-BLE installs. 

* BLE control is generally stable, but temperamental. I have observed that after a period of time, use of BLE control can contribute to walk up unlock not working or requiring bluetooth to be turned off/on on phone, there are also reports of potential instability if there is significant distance between the TWCManager device and the vehicle.
