# ChangeLog

This document logs the changes per release of TWCManager.

## v1.2.0 - Current development branch

  * Added systemd service definition (thanks @nean-and-i)
  * Polling of Vehicle VIN on detection of charging vehicle (for those firmwares which support it) and access to Vehicle VIN via Status modules and Web Interface
  * Various updates to documentation (thanks @MikeBishop and @neilrees)
  * Web interface now uses AJAJ for dynamic asyncrhonous updates rather than page refresh
     * This change additionally introduces new REST API commands 
  * Handling of module import issues such as modules not existing (thanks @AndySchroder)
  * Use actual mains voltage per phase as reported by some TWC firmwares (thanks @MikeBishop)
  * Report background thread exceptions to the console (thanks @neilrees)
  * Added history recording for Slave TWC amps offered, with API function to query average amp history (thanks @MikeBishop).
  * Added recording of charge sessions per vehicle VIN, with rudimentary total kWh consumed calculations.
  * Added support for SolarEdge API EMS Module, thanks to prototype provided by Picar on TMC Forums.
  * Bugfixes
    * Fixed situation where fakeMaster == 2 installations do not recieve Status module lifetime kWh and voltage per phase readings [Backported to 1.1.8]
    * Fixed unnecessary 60 second delay to processing background queue introduced by lifetime kWh and voltage per phase polling [Backported to 1.1.8] (Thanks @MikeBishop)
    * Fixed a condition in which flex and alternative maximum clash on charging rate (thanks @MikeBishop)

## v1.1.8 - 2020-04-26

  * Significant improvements in Tesla Powerwall2 EMS module function and stability (thanks @MikeBishop)
  * New module instantiation system removing static references to most modules
     * Integration of Tesla Vehicle API into module architecture (thanks @MikeBishop)
  * Policy Handling improvements
     * Modularization of the Policy engine, with ability to add additional constraints to policy definitions and to perform OR comparisons in policy conditions (thanks @MikeBishop)
     * Make policy match operators case-insensitive (thanks @MikeBishop)
     * Re-order policy to avoid Track Green Energy overriding scheduled charging, and to add a new emergency policy entry point at the start of the policy (thanks @MikeBishop)
     * Addition of Policy Latching and Flex for advanced policy use-cases (thanks @MikeBishop)
     * Documentation explaining the policy system (thanks @MikeBishop)
  * Tesla API & Vehicle Improvements
    * Introduce vehicle SOC charge limiting per policy (thanks @MikeBishop)
    * Query Tesla API to fetch Stormwatch detection data (thanks @MikeBishop)
    * Improved Arrival/Departure detection via the Tesla API (thanks @MikeBishop)
    * Reduced vehicle wake events due to opportunistic wake calls (thanks @MikeBishop)
  * Improved logging system which clearly shows the module which produced the log message and the log priority.
  * Completed the modularization of the RS485 interface code to allow alternative interfaces, and introduced two new interface modules (Dummy and TCP)
  * Ensure that status output formula balances for improved readability (thanks @MikeBishop)
  * Formatting improvements for the built-in webserver
  * Support for tracking lifetime kWh and voltage per phase of slave TWCs, this includes:
    * Publishing of new Status (HASS and MQTT) values for lifetime kWh and voltage per phase
    * Polling of this value for TWCs with newer firmwares that provide it
  * Implement Stop command for TWCs (not recommended, see linked documentation)
  * Bugfixes
    * Remove duplicate conditional check in TeslaAPI module (thanks @MikeBishop)
    * Clarify the polarity of amperage offset in configuration file (thanks @MikeBishop)
    * Fix message length restriction on newer firmwares (thanks @nean-and-i)
    * Fix lag between Green Energy fetch and evaluation (thanks @MikeBishop)

## v1.1.7 - 2020-03-09

  * Dropped the default policy check timer down to 30 seconds from 60 seconds after positive feedback on lower calculation intervals
  * Dropped cache time in all EMS modules down to 10 seconds to ensure that the lower calculation intervals work correctly
  * Added ability to override the policy check timer to set it to any value desired
  * Added ability to override the policy entirely from the configuration file (with caveat that this is not recommended)
  * Added ability to extend the policy at two points (before and after) to add extra rules in whilst retaining the rest of the policy
  * MQTT control command chargeNow will now accept a payload specifying the number of amps and duration to charge
  * Added support for remote debugging using the Python Visual Studio remote debugger (ptvsd) (thanks @MikeBishop)
  * Significant improvements to Powerwall2 EMS module including authentication fixes, addition of Operating State to module state tracking, fixing a HTTPS certificate validation issue and stability testing (thanks @MikeBishop)
  * Early framework for centralised module instantiation
  * Allow use of module parameters within policy rules (thanks @MikeBishop)
  * Added setup.py setuptools script, which will shortly become the recommended installation method
  * Bugfixes
     * Fixed an issue where the policy based charging rules could not access settings variables due to an error in string offset (thanks @AndySchroder).
     * Fixed a bug with debug output if the charger is configured to draw more amps than the wiring can handle (thanks @AndySchroder).
     * Fixed an issue with the debug web interface (WebIPC) which impacted sending custom commands to the TWC and recieving the result
     * Fixed the refresh image for the IPC web interface (thanks @notreallybob).
     * Fixed a situation where amp calculations can lead to a value of -1 due to previously used default values. Any amp related defaults are now 0, with exception handling for negative values.
     * Fixed an error in policy rule where green energy charging was offset by 1 hour (thanks @MikeBishop)
     * Fixed Powerwall2 authentication process (using cookies rather than Authorization header) (thanks @MikeBishop)

## v1.1.6 - 2020-02-02

  * Implement policy-based charging, where individual charging logic (for scheduled, non-scheduled, green energy and charge now logic) is now centralised.
    * Benefits of this approach:
      * Single point within the code where we set the amps to share.
      * Defined priority to ensure one case does not override another.
      * Lays foundation for future advanced use cases.
  * TeslaAPI: Add optional override to not instruct a vehicle to stop charging via Tesla API (if the policy would otherwise dictate this) if the vehicle SOC is below a defined (optional) minimum. This avoids excessive battery drain which may be harmful to the vehicle's battery. Instead of stopping the charge session, the vehicle will continue to charge at ```minAmpsPerTWC``` until minimum SOC is met (thanks @MikeBishop).
  * Powerwall2 EMS: Feature to limit charging when Powerwall2 state of charge is below a defined minimum, to avoid depleting Powerwall2 during low energy generation (thanks @MikeBishop).
  * Bugfix: HASS EMS module was non-functional due to an error introduced during config separation. Fixed in v1.1.6.
  * Bugfix: Makefile update to install php7.3 on raspbian (thanks @MikeBishop)
  * Bugfix: Fix typo which did not set desiredAmps to 0 when total amps available for all chargers was less than 1 (thanks @MikeBishop)
  * Bugfix: Fix crash condition in dumpState function for WebIPC control interface (thanks @MikeBishop)
  * Bugfix: Correctly display multiple chargers in web (RPC) interface (merged from upstream @cdragon)

## v1.1.5 - 2019-11-02

  * Standardised module configuration handling (which helps reduce the number of exceptions related to configuration)
  * Added configuration to enable/disable and configure HTTPControl module
  * Added Grid Status, Voltage and Password Authentication support to Tesla Powerwall2 EMS module
  * Tested and fixed TWCManager Slave Mode functionality

## v1.1.4 - 2019-10-22

  * Implemented Tesla login / token retrieval for HTTP Config module.
  * Improved debug logging for Tesla API Vehicle module.
  * Modularised the Web IPC interface for the external web server control component.
  * Fix: Status output now subtracts charger load from consumption.
  * Re-implemented the Tesla API queries using python's request module rather than external shell calls to curl. Provides better exception handling and less shell command/subprocess interaction.
  * Breaking Change: Improved Settings Storage for non-volatile settings (not configuration) storage. 
    * This will require a manual port of the settings from ```/etc/twcmanager/TWCManager.settings``` to ```/etc/twcmanager/settings.json```
    * Due to a small user base, this is not done automatically. If there is demand for a port method, an issue can be raised for a feature request, and I'll create a one time script to port old settings to new settings.

## v1.1.3 - 2019-10-17

  * Added new HTTP Control Module for (limited) in-built HTTP control of TWCManager.
  * Added new Tesla Powerwall 2 EMS module for solar/consumption tracking.
  * Separation of Slave TWC code from Master TWC code - this adds stability by r
emoving a large number of global variables and reduces complexity for future fea
ture improvements.
     * Please Note: This is a major structural change to TWCManager, and is expected to take some time to fully test and validate. A benefit of this change is that now TWCSlave is modular, we can fully implement modular control interfaces.

## v1.1.2 - 2019-10-14

  * Improvements to HomeAssistant EMS module to avoid setting Consumption/Generation values to zero as a result of a connection failure, and better exception handling in general for the module.
  * Added EMS support for TED (The Energy Detective)
  * Added Control module support with a first control module for MQTT, with initial commands to stop the TWCManager daemon and start charging immediately.
  * Improvements to the MQTT and HASS topic/sensor names for Slave TWCs. (4 byte instead of 2 byte)

## v1.1.0 - 2019-10-12

  * Separation of Tesla API class from TWCManager code, this allows pluggable modules for interfacing with vehicles.
  * Added Fronius EMS module - read generation and consumption values from a Fronius inverter.
  * Added MQTT authentication for MQTT Status module.
  
### Fixes

  * Fixed settings file location path which had issues if no trailing slash.

## v1.0.2 - 2019-10-11

  * Option to subtract charger utilization from consumption value if consumption meter is counting charger load.
  * Updated status output to more clearly define the generation and consumption values being tracked.

## v1.0.1 - 2019-10-06

This release is a bugfix release for v1.0.0. It contains the following fixes:

  * Fixed: MQTT status connection timeout causes a delay to heartbeat timers, which then cause TWCManager reconnections to Slaves
  * Fixed: MQTT and HASS status update rates are too high. All updates now rate limited to one per minute.

## v1.0.0 - 2019-10-05

This module provides HomeAssistant sensor functionality to the TWCManager project. It is the first release after the fork and restructuring of the code and configuration files.

  * Forked original repository by cdragon
  * Integrated multi-charger and multi-car MQTT integration (Status Module) from flodorn's repository
  * Split the Sensor (EMS) and Status functions out of the main script
  * Created HomeAssistant EMS and Status modules and tested successfully
