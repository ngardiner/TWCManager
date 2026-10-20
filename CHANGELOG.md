# ChangeLog

This document logs the changes per release of TWCManager.

## v1.4.1 - Upcoming

* Features
    * (@MikeBishop) - The vehicle page shows BLE status (not in range, in range, pairing, paired, pairing failed, key refused) with a Bluetooth scan for the car
    * (@MikeBishop) - The Vehicles page lists the vehicle registry (tier, providers, last seen, held limit) and can add a vehicle by VIN so a VIN-less TWC with BLE only can be set up; a vehicle added by VIN that nothing else has seen can be forgotten from its page
    * (@MikeBishop) - Gen3 Wall Connectors each get their own target; several units on one bus share the headroom. TWCManager names the car on a Gen3 with a free read of the energy site's live status. This needs the Gen3 registered in the Tesla app and a token that can read energy data (a Fleet API token with the `energy_device_data` scope); a unit's optional `din` key fixes a wrong DIN guess
    * (@MikeBishop) - `controller.Gen2TWCs.ranges` sets a per-TWC `minAmps` (in place of minAmpsPerTWC) and `maxAmps`
    * (@MikeBishop) - TeslaBLE's default command timeout is 15 s (was 5 s); authenticated commands often needed more than 5 s
    * (@MikeBishop) - `atHomeRadius` defaults to 1,000 ft (was 10,560 ft); a configured value still wins. Home is no longer taken from the first car position seen: it moves only to a fresh position (by the car's GPS time) of a car when a TWC reads its VIN, or one read over BLE, or is set on the Settings page. A car-only install with no TWC and no BLE must set home on the Settings page: a warning says so once, and until then cars known only through vehicle APIs are not treated as home and get no limit
    * (@MikeBishop) - The Vehicles page shows each car's presence (Home, Probably home, Away), and a vehicle's page lists its pending commands, one line each: in flight, or why not sent. The debug dump no longer carries the per-field summary
    * (@MikeBishop) - A Gen2 TWC's unplug ends its plug session (before, 5 seconds after charging stopped) and is recognised from its status 00 when the TWC's maximum amps dropped from above 0 to 0, the 00 lasts 60 seconds and live telemetry does not say the car is plugged in (tuned on two cars); the log says why (`TWC <id>: 00 onset ...`, `unplugged, <VIN> departs`)
    * (@MikeBishop) - INFO log lines for each reconciler outcome, each paid Fleet API request (endpoint and price, never the URL, VIN or token), and each telemetry change of plug state, drive state or geofence
* Architecture
    * (@MikeBishop) - Serialise settings.json saves under a settings lock so a save no longer races writers on other threads
    * (@MikeBishop) - One vehicle registry keyed by VIN holds every per-vehicle field with its source and age; reads cascade BLE then cloud and never wake a car unless asked; a TWC-reported VIN is confirmed by a second query before a vehicle is created; vehicles unseen for 30 days (config vehicle.forgetAfterDays) are forgotten
    * (@MikeBishop) - The debug API `/api/sendTeslaAPICommand` now takes the vehicle's VIN (`vehicleVIN`) instead of the numeric vehicle ID (`vehicleID`); the web UI and debug dump read vehicles from the registry
    * (@MikeBishop) - A providerSuppressions entry for HomeAssistant now also drops Home Assistant vehicle reads, not only its commands
    * (@MikeBishop) - Vehicle channels are providers that declare capabilities with per-operation cost and locality; commands walk a fixed order (TWC, BLE, then cloud; billed last) with per-provider suppressions (settings providerSuppressions); commandPolicy maps onto them; HomeAssistant, when loaded, joins the command order last (after Fleet) and is suppressed under ble_only; the debug API goes through the same order
    * (@MikeBishop) - Charging endpoints (a TWC, or a car whose amps BLE, Fleet or HomeAssistant can set) are grouped into charging points by the car they serve, so a car that stops charging on its TWC gets no second power share or Fleet charge-rate commands. The distributor allocates per point, an idle TWC counts nothing against the budget, and a car's amps are queued to the vehicle worker instead of being sent to the Fleet API from the RS-485 loop
    * (@MikeBishop) - The power distributor never has the Fleet API set a car's amps where a TWC or BLE can (Charge Rate Control modes 2 and 3 still may). A BLE-paired car on a mobile connector gets its rate over BLE only, with no Fleet fallback. Home Assistant can now set a car's amps; it is treated as billed and tried only after the Fleet API
    * (@MikeBishop) - Settings page labels say "vehicle command (BLE or API)" and "local"/"cloud" where they said "Tesla API"; the stored values are unchanged
    * (@MikeBishop) - Held charge limits in settings `chargeLimits` are keyed by VIN; entries keyed by the Tesla API's numeric vehicle ID are re-keyed when the vehicle list names them. After a downgrade, only 1.3.x's TeslaBLE path reads the VIN-keyed entries; its Tesla API path, keyed by numeric ID, starts over
    * (@MikeBishop) - Vehicle commands are no longer fired at a car: policy, the power distributor and the TWC heartbeat set the charge limit, charging on/off and amps each car should have, and one reconciler delivers them through the provider order with one attempt in flight per car, classified outcomes and backoff; a pending stop is cancelled when charging is wanted again. A paid-tier provider (Fleet API, Home Assistant, the Owner API for a car listed with `command_signing: off`) is used at once when no free one can serve the car, else only after the free ones have failed for 5 minutes (charge limit) or 60 seconds (start/stop, amps). It sends a desired value once, and once more after a failure, or after a reading shows the command reached the car. If no send reached the car, a reading from the car or a pending start allows two more. That is at most four commands per desired value; each may be preceded by a wake and a vehicle_data read. A start waits for that car's limit change unless the limit cannot be delivered. A charge limit changed in the car or the Tesla app is adopted, not undone, until the next policy decision. Removes VehiclePriority, the per-VIN vehicle command dispatch and the per-module charge-limit copies
    * (@MikeBishop) - TeslaBLE no longer retries internally or opens a circuit breaker; `maxRetries`, `retryDelay` and `circuitBreaker*` are ignored. A command that times out or fails to connect is retried once, two seconds later; the reconciler decides any later retry or cloud fallback
    * (@MikeBishop) - Held charge limits saved under a numeric Tesla vehicle ID that no Tesla account lists are dropped with one warning, once a vehicle list has come back
* Bugfixes
    * (@MikeBishop) - When TWCManager shares power with Tesla API cars, the distributor gives an idle TWC a target of 0 and counts nothing against the budget. On the wire the TWC is offered 0 when no car is charging (a Gen2 treats 0 as its hardware minimum), and its hardware minimum (usually 6 A) when another car is charging and there is at least minAmpsPerTWC of power per car, so it still sees a car plug in. Brief overshoot is fine: the configured limit is already the 80% continuous rating and the next cycle rebalances
    * (@MikeBishop) - A car read while its TWC held the 6 A pilot it keeps for a sleeping car is no longer capped at 6 A (or offered nothing, when the TWC measured above 240 V), which left it uncharged and unwoken overnight. A car's limit is its own maximum charge current (from BLE, the Tesla API or TeslaMate, else `wiringMaxAmpsPerTWC`), never the pilot it last saw, and a raise to the wiring maximum still reaches a car whose own maximum is lower. A plugged-in car whose charging point refuses power that is there is logged at INFO (`<point>: <VIN> is plugged in, but its limits leave it none of <n> W`), at most hourly
    * (@MikeBishop) - A targeted start, stop or charge limit sent through Home Assistant now affects only the named car; a command for every car still reaches every Home Assistant car
    * (@MikeBishop) - A Gen2 RS-485 adapter that is missing or can't be opened no longer stops TWCManager from starting: the Gen2 link logs one error and retries every 30 seconds, so a Gen3-only or vehicle-only install needs no interface change, and a late USB adapter is picked up without a restart. The Gen2 link is not opened on the Gen3 controller's port
    * (@MikeBishop) - Presence follows events as they arrive (plug-ins, unplugs, positions, drive starts, BLE contact) and needs no paid read: a car is Home, Away or Probably home, probably-home cars are counted in `numCarsAtHome` and `minBatteryLevelAtHome`, a Home Assistant zone state of home with no coordinates no longer places a car, and a charge limit, start or rate for a car waits until it is plugged in or confirmed home. A TWC's own commands are never held back
    * (@MikeBishop) - A policy with no limit (-1) releases the car to its own limit and is applied like any limit; a departing car's original limit is restored wherever it is. A car is woken only when it is expected to charge once everything wanted is applied (plugged in or arriving, no stop wanted, its TWC or charger offered power, and its limit above its battery level; an unknown limit or battery level counts as charging), and after a start not before TeslaMate or Fleet Telemetry has reported once (or 60 seconds have passed), whatever the direction of the change; a car expected to stay idle gets changes only when it is awake anyway. Restores on departure and stops never wake
    * (@MikeBishop) - A plug-in with no VIN from a Gen2 or Gen3 TWC is matched to the one car that can be there (plugged in just now, BLE-read at that pilot and draw, or BLE-reached) and left unnamed when several could be; on a Gen2 that reads VINs, the VIN read replaces the match; the match never BLE-reads a car known asleep; a Gen2's match runs on the BLE worker, and a Gen3's vitals never read BLE. An unnamed TWC's rate now goes only to cars plugged in here, no longer to the first car; a status 00 alone no longer opens an unnamed session, and a Gen2 plug counts only while the TWC reports one. The 5, 20 and 45 minute reads after power drops stay
    * (@MikeBishop) - Removed the `/api/checkArrival` and `/api/checkDeparture` hint endpoints (they now return 404); presence no longer needs them
    * (@MikeBishop) - A guest car on the same TWC can keep a car that left idle counted as home until that car reports a position outside the radius or starts driving; a VIN a TWC rejected during a charge session stays rejected until the unplug
    * (@MikeBishop) - Whether a car is at home is re-checked on every policy pass from its latest position from any source, including the free TeslaMate and Fleet Telemetry feeds, so no paid read is needed to place a car; a car leaves when a newer position places it away, not only when its TWC's power drops, and a position no newer than TWCManager's last limit write to the car does not count as leaving (see Policy Customization, Charge Limits, which also covers running with no home location set)
    * (@MikeBishop) - A failed BLE command no longer falls through to a billed Fleet API command at once (#698 for lowering); Home Assistant is used only when the Fleet API is unavailable or has refused, never after a plain Fleet failure, so one change is never billed twice; Home Assistant can now restore a car's normal limit; charge limit lowers never wake the car over BLE (#698)
    * (@MikeBishop) - Tesla API vehicle reads time out after 10 seconds, and a read sends at most three vehicle_data requests, counting the resend after a token refresh. A Fleet API token refused again right after a refresh is removed, as any rejected token is (#689); an HTTP 412 on a command stops commands with that token until it changes, without stopping reads
    * (@MikeBishop) - Storm Watch's Tesla API requests time out after 10 seconds, and their errors log only the exception type, never its text
    * (@MikeBishop) - Under Charge Rate Control mode 2 (and mode 3 below 6 A), the TWC offers the car's share until the car's rate is confirmed, then its maximum; a rate command that keeps failing no longer leaves the TWC at its maximum while the car draws more than its share. A car no provider can set the rate for is held to its share by the TWC
    * (@MikeBishop) - A charge limit is only changed when its current value is known (from any source); TeslaBLE no longer sets a limit it could not read
    * (@MikeBishop) - Owner API commands for a `command_signing: off` vehicle sit just ahead of the Fleet API in the command order, so with both tokens such a vehicle's commands go to the free Owner API, and to the Fleet API once Tesla refuses them. A 410 or 412 (or "only available on fleetapi") stops Owner API commands and wakes for every car on that token until it is entered or synced again, logged once as an error; reads continue (#659)

## v1.4.0 - 2026-10-26

* Features
    * (@MikeBishop) - Apply charge limit over BLE
    * (@MikeBishop) - Fetch charge and location state over BLE, falling back to Fleet API when BLE is unavailable
    * (@ngardiner) - FleetAPI Authorization Code web login (PKCE by default, optional client-secret flow), with auto-capture callback and paste-back, ported from v1.3.4 (closes #639)
    * Add vehicle commandPolicy (prefer_ble/ble_only/api_only) to restrict state-changing commands to BLE or API, capping billed Fleet API command spend; configurable from the Settings page, with optional hard override in config.json (closes #651)
    * Add MQTT control topics for nonScheduledAmpsMax and nonScheduledAction to allow policy control via MQTT (closes #475)
    * (@MikeBishop) - Accept a bare access or refresh token on the Tesla login paste-back (#687)
    * (@MikeBishop) - Hold an Owner API token and a Fleet API token side by side: reads use the free Owner API, commands use Fleet, and TeslaMate sync can stay on beside a Fleet login (#692)
    * (@MikeBishop) - Show each Tesla token slot's status on the Settings page, with a Remove token button (#692)
* Architecture
    * Remove the legacy PHP web interface and its lighttpd/PHP install from `make webinstall` (PHP2Shell, CVE-2026-109618/CVE-2026-109619); use the built-in web interface
    * Remove Tesla Owner API login flow; FleetAPI, TeslaMate token sync, manual token entry and BLE remain the supported paths, with Owner API tokens used for reads, and for commands only to vehicles Tesla reports with `command_signing: off`
    * (@MikeBishop) - Saved Tesla tokens migrate to separate Owner and Fleet settings on first start; downgrading afterwards loses the Owner API token (#692)
    * (@MikeBishop) - TWC abstraction layer with EVSEController/EVSEInstance interface (ported from #483)
    * (@ngardiner) - Gen3 TWC support via Neurio Modbus emulation
    * (@MikeBishop) - Run vehicle commands on a dedicated worker thread and serialize tesla-control calls, so BLE retries no longer stall the RS485 loop (#668)
    * (@MikeBishop) - Shut down gracefully on SIGTERM, saving settings and sending pending stop-charging commands before exit (#668)
    * (@MikeBishop) - Evaluate charging policy on its own thread instead of the RS485 loop, only when acting as master (#670)
* Bugfixes
    * Fix: Add missing module-level time import in HASS, EmonCMS, Growatt, IotaWatt, SmartMe and TCP modules, which crashed EMS polling and caused charge current oscillation (closes #644)
    * Fix: Restore minInterval parameter on update_location, fixing TypeError that broke API start/stop charging and home location correction
    * Fix: Correct invalid logging level in update_vehicle_data error handler which crashed when logging a failed API response
    * Fix: VehiclePriority now treats TeslaAPI "error" string return as failure, enabling correct fallback to BLE and failure stat tracking
    * Fix: Do not seed or reset home location from a stale GPS fix; check gps_as_of age and force a fresh fetch when a TWC-connected VIN corrects home (closes #423)
    * Fix: Skip start-charge commands (API and BLE) when the car is waiting on its own Scheduled Charging/Departure timer; disable via respectVehicleSchedule (closes #493)
    * Fix: Remove post-command ping from TeslaBLE charge control; command success is sufficient proof of BLE connectivity (closes #650)
    * Fix: Treat Tesla BLE idempotent-success responses as success in parseCommandOutput (closes #649)
    * Fix: Remove cryptography<3.4 upper bound; modern versions ship binary wheels and the Rust build requirement is no longer a concern (closes #647)
    * Fix: RS485 read returns empty bytes after reconnect instead of None, preventing crash on socket disconnect (closes #461)
    * Fix: Stop flooding VIN queries when non-Tesla vehicle or CAN-disabled TWC returns all-zero VIN data (closes #296)
    * Fix: WebIPCControl recovers from ExistentialError when IPC queue is removed at runtime (closes #192)
    * Fix: Remove mislabeled "power" status update in TWCSlave that reported amps instead of watts (closes #325)
    * Fix: Cast numeric strings from settings in policyValue() so non-scheduled charge rate is applied correctly (closes #370)
    * Fix: Create the CSV and File logging output directory if it does not exist, instead of failing at startup
    * Fix: MQTTControl no longer returns a value from __init__ when the broker connection fails
    * Fix: Correct RecieverID key in TWCProtocol heartbeat parsing and build Dummy slave heartbeats via the protocol module
    * Fix: BLE no longer re-sends charge start commands every poll cycle once the car reports it is already in the desired state (closes #652)
    * (@MikeBishop) - Fix: Resend charge amps while a slave is stuck in state 08 at minimum rate (#669)
    * Fix: Remove dead Tesla email/password login path that called a non-existent apiLogin method
    * Fix: Remove hardcoded /home/twcmanager fallback path for tesla-control binary; use PATH lookup only (closes #600)
    * Fix: limitOverride no longer applies charge limit cap when charging has resumed, preventing 60% limit being set instead of stopping charge (closes #586)
    * Fix: Transient API errors (vehicle unavailable, operation_timedout) no longer trigger full exponential backoff, preventing hour-long delays in stop-charge commands (closes #377)
    * Fix: Negate negative generation values from EMS modules that report generation as negative watts (closes #442)
    * Fix: Add "context deadline exceeded" to transient API errors so wake-up timeouts don't trigger full backoff (closes #593)
    * Fix: Reset historyAvgAmps to 0 after each history snapshot so kWh counter doesn't accumulate across periods (closes #470)
    * Fix: Log a clear error and stop retrying when Tesla Vehicle Command Protocol is required, instead of silently failing (closes #580)
    * Fix: Skip waking vehicles known to be away from home to prevent unnecessary API calls draining the battery (closes #466, #209)
    * Fix: RS485 interface now logs a clear error and unloads gracefully when no port is configured, allowing TCP interface to take over (closes #612)
    * Fix: RS485 getBufferLen() and send() now handle SerialException and attempt reconnect, preventing crashes on socket disconnection (closes #461)
    * Fix: Track Green Energy policy no longer activates when Non-Scheduled action is set to Do Not Charge (closes #527)
    * Fix: Enphase local API now supports JWT Bearer token authentication required by Envoy firmware 7.x (closes #424)
    * Fix: Publish charger_load_w status on every heartbeat so Home Assistant entities don't disappear after HA reboot (closes #462)
    * Fix: Log a warning at startup when minAmpsPerTWC exceeds wiringMaxAmpsPerTWC, which would prevent charging from ever starting (closes #24)
    * Fix: Set stopAskingToStartCharging=True when a vehicle departs home, preventing unnecessary wake attempts on absent vehicles (closes #590)
    * Fix: Trust TeslaMate asleep/offline state in is_awake() to avoid Fleet API status polls when vehicle is confirmed sleeping
    * Fix: Fleet API wake minimization - 3-minute pre-wake delay (configurable wakeDelayMins), 30-minute retry backoff, no API polls during hold windows
    * (@MikeBishop) - Fix: Import serial at module level in RS485 so SerialException handling and reconnect work (#673)
    * (@MikeBishop) - Fix: TeslaAPI error handlers no longer crash when a request never went out (#682)
    * (@MikeBishop) - Fix: TeslaMate telemetry now attaches to the API vehicle when BLE handles all commands (#671)
    * (@MikeBishop) - Fix: TeslaMate token sync now accepts resyncs and reads tokens from current TeslaMate versions (#679)
    * (@MikeBishop) - Fix: Classify Owner API tokens as read-only and refuse vehicle commands on them instead of sending them to the Fleet API (#679)
    * (@MikeBishop) - Fix: Power distributor skipped every other EVSE, leaving it on a stale target (#696)
    * (@MikeBishop) - Fix: Tesla API charge start/stop for one vehicle no longer wakes or commands every vehicle (#697)
    * (@MikeBishop) - Fix: Don't send paid Fleet API charge-rate commands to a car a TWC is serving (closes #672)
    * (@MikeBishop) - Fix: Don't resend an unchanged Fleet API charge rate, and time out the request after 10s (#694)
    * (@MikeBishop) - Fix: Refresh Owner API tokens with client_id ownerapi at auth.tesla.com, as 1.3.3 does (#687)
    * (@MikeBishop) - Fix: A rejected pasted refresh token keeps the previous tokens, and a 401 on refresh clears the token type (#687)
    * (@MikeBishop) - Fix: Check for a car coming online after a Fleet API wake_up instead of waking it again 30 minutes later (#699)
    * (@MikeBishop) - Fix: Stale TeslaMate asleep state no longer stops the free vehicle state check (#699)
    * (@MikeBishop) - Fix: Allow Owner API commands per vehicle when Tesla reports `command_signing: off`; other vehicles stay read-only (#659)
    * (@MikeBishop) - Fix: Run tesla-control in the host network namespace under Docker, and a BLE command timeout no longer kills TWCManager (#711)
    * Fix: Cancel a pending stop-charging command when charging should continue (closes #667)
    * Fix: Queue charge start/stop commands per vehicle and send at most one per vehicle per minute, so a refused stop is no longer retried every loop
    * (@tmark00) - Fix: A null Fleet API voltage or current while a car is unplugged no longer stops TWC heartbeats (closes #713)
    * Fix: Serialize Tesla token refreshes, so two threads no longer spend the same single-use refresh token and lose both tokens (closes #690)
    * Fix: Fronius read timeouts are logged and skipped instead of raising a traceback
    * Fix: An HTTP POST without a Content-Length header no longer crashes the request handler (closes #666)
    * Fix: A 401 on a Tesla API vehicle command refreshes an expired token and retries once, and clears a dead token so commands fall back to BLE (closes #689)
    * (@tmark00) - Fix: The power distributor budgets with the measured TWC voltage instead of defaultVoltage, so a TWC is no longer offered more current than the policy asks for (closes #715)
    * Fix: A Gen2 TWC that reports no voltage falls back to defaultVoltage on every configured phase, not just phase A
    * Fix: The vehicle page shows BLE pairing status, and tells you to tap your key card after a pairing request
    * Fix: VehiclePriority answers enabled() directly instead of dispatching and logging it several times a second
    * Fix: MQTTStatus logs one line while the broker is unreachable instead of one per skipped publish
    * Fix: HTTPControl falls back to IPv4 on a host with IPv6 disabled (closes #721)
    * (@MikeBishop) - Fix: The Docker image finds tesla-control on its PATH, so BLE works, and the docs download compose files that exist (#717)

## v1.3.4 - 2026-04-24
* Features
    * Implement missing API endpoints: `setPolicy`, `setLatLon`, `setConsumptionOffset`
    * Add centralized API input validation (`APIValidator`) across all endpoints
* Architecture & Improvements
    * (@MikeBishop) - Improve home location detection when vehicle is charging via TWC
    * (@ngardiner) - Add aWATTar pricing module with dynamic rate-based charging optimization
    * (@deece) - Tighter Home Assistant integration with MQTT autodiscovery, persistent connections, and vehicle control via Tesla Fleet integration
    * (@ngardiner) - Post-merge improvements: input validation, error handling, comprehensive logging, rate limiting, and thread safety for MQTT and Home Assistant modules
    * (@ngardiner) - Centralized LoggerFactory for configurable logging prefixes
    * (@ngardiner) - Vehicle priority abstraction proxy for module fallback with retry multiplier logic
    * (@RichieB2B) - Add Fleet Telemetry MQTT support with typed updates and online state tracking
    * (@ngardiner) - Initial commit of local Tesla BLE vehicle control module with stability improvements
    * (@MikeBishop) - Fall-back to VIN as vehicle name if API does not specify a name
    * (@MikeBishop) - Dampen API calls to reduce unnecessary calls
    * (@RichieB2B) - Allow limiting of maximum grid power import, introduce getConsumptionAmps for EMS modules
    * (@ngardiner) - Add support for decrypting TeslaMate API credentials using the encryption key
    * (@ngardiner) - Refactor maxAmpsToDivideFromGrid to global configuration parameter
    * (@VIDGuide) - Add Charge Now duration display with remaining minutes and current amps in web UI
* Bugfixes
    * (@MikeBishop) - Explicitly request drive_state data to fix apparent issue with older models, and remove endpoints that are not used
    * (@dtiefnig) - Specify access scope for token refresh
    * (@evheros) - Fix typo in log levels
    * (@RichieB2B / @evheros) - Fix incorrect return variable name in BLE Peering code
    * (@dehsgr) - Fix Makefile issues (alternate HOME support and BLE permissions)
    * (@RichieB2B) - Avoid wake up loop during battery health check by applying per-vehicle retry backoff
    * (@RichieB2B) - Cache vehicle.is_awake() state for 2 minutes to reduce Tesla Fleet API calls
    * (@RichieB2B) - Fix NameError in BLE Peering code
    * (@ngardiner) - Atomic writes for settings file to avoid data loss
    * (@ngardiner) - BLE pipe management with state tracking and cleanup
    * (@ngardiner) - Subprocess timeout handling with graceful shutdown
    * Update Docker documentation for Compose V2 and modern Raspberry Pi OS

## v1.3.2 - 2023-03-12
* (@RichieB2B) - Nicer looking log prefixes for EMS modules
* (@RichieB2B) - Support for new fleet API and vehicle command proxy
* (@RichieB2B) - Upgrade MQTT support for paho v2/MQTTv5
* (@RichieB2B) - Enable reporting of Slave error messages
* (@RichieB2B) - Reset stopAskingToStartCharging more often
* Bugfixes
    * (@RichieB2B) - Fix issues with mysql status logging statements
    * (@ngardiner) - Fix issues preventing web UI display when internet access is unavailable

## v1.3.1 - 2023-11-19
* (@RichieB2B) - Support for new fleet API endpoint for location telemetry
* (@RichieB2B) - More accurate getConsumptionAmps for DSMRreader (esp. for unbalanced 3-phase circuits)
* Bugfixes
     * (@ngardiner) - Avoid web interface crash when ampsList cannot be derived from minAmpsPerTWC and wiringMaxAmpsPerTWC (root cause under investigation)
     * (@ngardiner) - Fix issue with dependency versions

## v1.3.0 - 2023-05-28

* Bugfixes
     * (@dtiefnig) - Fix issue with logic around detecting instances of multi-phase system incompatibility
     * (@dtiefnig) - Fix issue with zero-byte VINs causing infinite VIN fetch retries
     * (@MikeBishop) - Fix rounding in calculation of current offer when calculating delta to new offer to improve accuracy
     * (@ngardiner) - Adjust voltage / phase detection to handle combinations of 1/2/3 phase systems, as long as all slave TWCs have the same number of active phases
     * (@ccutrer) - Fix openhab integration to properly parse floats
     * (@ngardiner) - Detect issues with serial connection (RS485) and automatically reconnect
     * (@RichieB2B) - Fix MQTT EMS module (missing brokerPort)
     * (@RichieB2B) - Fix large steps in ChargeNow duration in the web interface
     * (@mattiasclaesson) - Remove unnecessary kWh suffix from web UI

* Features

    * (@hopfi2k) - Major "Modern Theme" update. Switched to Bootstrap 5.02, bug fixes, now fully responsive
    * (@ngardiner) - Add Home Location view and modification controls to settings page
    * (@ngardiner) - Add MQTT EMS module (currently pre-release for testing)
    * (@dtiefnig) - Add switch to output plain text Console output (turn off color)
    * (@dtiefnig) - Unify single and multi-car charge at home behaviour
    * (@dtiefnig) - Add Modbus TCP support for SolarEdge EMS module
    * (@dtiefnig / @MikeBishop) - Update Tesla API charging states
    * (@dtiefnig) - Reference entrypoint script from within Docker container to remove dependency on external source tree
    * (@ngardiner) - Add Current Policy as status value (to be published to HASS / MQTT)
    * (@MikeBishop) - Fix API interactions impacted by Tesla's vehicle_data endpoint changes
    * (@MikeBishop) - Implement improved VIN Check routine
    * (@RichieB2B) - Add option to leverage Tesla API for lower amp control, otherwise leverage TWC
    * (@RichieB2B) - Add DSM Reader EMS Module
    * (@cods4) - Update to documentation 

## v1.2.6 - 2022-02-28

  * Bugfixes:
     * (@ngardiner) - Fix for exception regarding serialization of bytes object in JSON data for Tesla Auth Flow
  * (@ngardiner) - First revision of self-update mechanism for TWCManager - will be activated once v1.3.0 is released

## v1.2.5 - 2022-02-27

  * Bugfixes:
     * (@ngardiner) - Fix debug settings form issues
     * (@ngardiner) - Fix token refresh timing for manually entered tokens
  * (@ngardiner) - Add support for multiple Fronius inverters
  * (@MikeBishop) - Add support for detecting sunrise and sunset times based on home location using sunrise-sunset.org API
  * (@ngardiner) - Replace Tesla Login flow with one that is workable with the latest (millionth?) iteration of Tesla API auth flow
  * (@MikeBishop) - Add configuration parameter to always perform Green Energy checking, including outside of daylight hours (maintaining current default of off for now)

## v1.2.4 - 2022-02-15

  * **Breaking Changes**
      * Due to changes introduced by Python 3.9, the minimum version of Python supported from v1.2.4 forward is **Python 3.6**
          * This means that v1.2.3 is the end of the line for Debian Stretch based machines - upgrade to buster to use v1.2.4 and above.
      * The configuration parsing engine has been changed to PyYAML
          * This may cause your config file to stop parsing correctly. If so, my apologies, however it does allow us to support both JSON and YAML formats going forward.
  * Bugfixes:
      * (@MikeBishop) - Fix debounce mechanism to avoid continuous charging when minimum generation is not met in some circumstances
      * (@ngardiner) - Align web interface TWCID so that it is lowercase, avoiding issues with JavaScript and Jinja2 misalignment
      * (@ngardiner) - Change cryptography module dependency to avoid versions requiring rust compiler (no module named setuptools_rust error)
      * (@ngardiner) - Add logic to set token expiry time when manually adding tokens
      * (@blach) - Fix Tesla API token refresh logic
 * (@MikeBishop) - Change to Powerwall Auth handling to expire auth tokens earlier due to changes in Powerwall auth handling
 * (@ngardiner) - Add version check to Web UI
 * (@ngardiner) - Add handling of recaptcha challenge (with DNS workaround), and provide other options if this is not possible
 * (@ngardiner) - Add Tesla API debug interface to allow sending Tesla API commands
 * (@ngardiner) - Added TeslaMate sync functionality to allow sync of telemetry and API token details
 * (@ngardiner) - In line with changes made by setuptools team to deprecate setup.py as command line tool, switched to native python build tools
 * (@MikeBishop) - Restrict dampening to green policies only, and prefer to keep charging vs stop/start
 * (@ngardiner) - Reduce potential Tesla API backoff time
 * (@deece) - Addition of Open Energy Monitor EMS module
 * (@dehsgr) - Addition of URL EMS module
 * (@tjikkun) - Set HASS sensor state class to make it work with statistics dashboard

## v1.2.3 - 2021-08-10

  * **NOTE**: v1.2.3 contains a potentially breaking change for users of OpenWB or the Legacy Web Interface
     * As of v1.2.3, the Legacy Web Interface and its dependencies such as lighttpd and php are no longer installed by default.
     * Please see the documentation for instructions on how to install it, if required. Most users will not need this.
  * **NOTE**: Starting from v1.2.3, Modern theme is the default theme for TWCManager, unless configured otherwise.
  * **NOTE**: v1.2.3 introduces a dedicated user account for TWCManager. For Manual install, you'll need to run ```make install``` after upgrading.
  * (@Saftwerk) - Add both Generation and Consumption support to Volkszahler EMS module
  * (@VIDGuide) - Improvements to the Modern UI - Show charger load and offered amps, and open GitHub link in a new window.
  * (@VIDGuide) - Add SoC % to Modern UI interface, and display charge time indicator per TWC
  * (@VIDGuide) - Fixed Modern UI layout on mobile, added Stop Charge Now button and fixed Charge Now control spacing.
  * (@VIDGuide) - Add Timezone parameter to docker-compose files to allow specifying container timezone
  * (@the-dbp) - Added Growatt EMS module
  * (@jherby2k) - Align API and HASS Status module values
  * (@mvaneijken) - Add MySQL logging module Port parameter
  * (@ngardiner) - Refactor TWCManager structure to allow for entire project to be packaged into pypi packaging for easy install/upgrades
  * (@jherby2k) - Add IotaWatt EMS interface
  * (@mvaneijken) - Added P1 Monitor EMS module
  * (@jherby2k) - Add support for HomeAssistant integration
  * (@MikeBishop) - Add support for deleting tasks, avoid sending stop commands under some conditions to avoid unnecessary transitions

## v1.2.2 - 2021-06-09

  * (@ngardiner) - Added SmartPi EMS interface
  * (@Saftwerk, @ngardiner) - Added Volkszahler EMS interface
  * (@ngardiner) - Added functionality to Dummy module to emulate TWC communication to the point that Policy selection occurs
  * (@MikeBishop) - Implement Policy Shortcut function to allow Charge Now to take immediate effect
  * (@GMerg) - Added OpenWeatherMap EMS interface
  * (@ngardiner) - Added VIN Management functionality, where vehicles can be allowed or denied charging based on VIN. With this, we introduce the ability to define vehicle groups, with future functionality allowing policy settings to be applied to these groups.
  * (@ngardiner) - Addition of a debug interface which allows tuning advanced inner workings of TWCManager, and allows sending commands to TWCs.
  * (@ngardiner) - Added support for Tesla MFA authentication flows
  * (@MikeBishop) - Improve API error handling, removing transient error delays and replacing with an exponential backoff mechanism to avoid delaying other background tasks.
  * (@MikeBishop) - Added debounce dampening for situations where intermittent consumption spikes / loads cause TWC to start and stop charging frequently.
  * (@ngardiner) - Add new consumption offset handling which allows for dynamic configuration of offsets in Watts and Amps via web and API
  * Bugfixes
    * (@ngardiner) - Better handling of permissions issues when attempting to save settings.json - alerts user to check file permissions via Web Interface
    * (@ngardiner) - Fixed issue with logging errors when a certain exception is raised in the Snapshot History function
    * (@ngardiner) - Fixed issue with Modern web interface Charge Now setting not working
    * (@ngardiner) - Fix behaviour of Stop Responding to Slaves charge stop mode, by re-enabling slave communication after 60 seconds
    * (@ngardiner) - Fix issues with subtractChargerLoad when using one of the (few) EMS modules which only provide Generation values. Previously, we only subtracted the Charger Load from Consumption which doesn't work in Generation-only measurement environments.
    * (@leeliu) - Fix TWC ID display for Modern theme which was truncating trailing zeros
    * (@ngardiner) - Fix bug in Stop Responding to Slaves routine caused by incorrect reference to time function

## v1.2.1 - 2021-04-04

  * Added support for Kostal inverters (Pico/Plenticore) (thanks @hopfi2k)
  * Added support for smart-me.com inverter API
  * Added support for serving static files via the inbuilt HTTPControl web server (thanks @hopfi2k)
  * Adjust charger load calculation based on real power measurements (thanks @dschuesae)
  * Introduce Scheduled Flex Charging feature (thanks @dschuesae)
  * Introduce new FileLogging module to allow logging to text file (thanks @dschuesae)
  * Moved debug logging to Logging modules to allow logging to file, database or other targets
  * Updated legacy Web UI to allow all EU/US amp selections (thanks @dschuesae)
  * Fix Web UI favicon (thanks @hopfi2k)
  * Added support for web themes, to allow changing the web UI to alternate views
  * Added Phase 1 of Charge Scheduling support, with backwards compatible charge scheduling (finally...) in the new UI
  * Added support for local query of Enphase EMS systems (previously cloud-only)
  * Set the legacy web UI module (WebIPC) to disabled by default. Avoids an error when running as a service, and is about time given it is deprecated.
  * Expose all time properties to the policy module for evaluation (thanks @MikeBishop)
  * Impovements to policy page in Web UI to show the value of policy parameters (thanks @MikeBishop)
  * Move grace period functionality for vehicles connected prior to policy evaluation to the master module, which opens the door to policy evaluation based on vehicle arrival/VIN (thanks @MikeBishop)
  * Split and show the values of Charger Load and Other Load in console output when the Subtract Charger Load setting is enabled (thanks @mikey4321)
  * Added EMS module support for SmartMe API
  * Added EMS module for Efergy (thanks @juanjoqg)
  * Added Graph visualisation for supported Logging modules - MySQL currently (thanks @juanjoqg)
  * Updates to accomodate Powerwall authentication flow changes (thanks @MikeBishop)
  * Do not override the charge_amps set in a policy when running checkGreenEnergy, allowing for Green Energy tracking numbers to be updated when Charge Now or Scheduled Charging policies are active (thanks @MikeBishop)
  * Significant overhaul of logging module interface to utilize the python logger architecture rather than implementing our own infrastructure (thanks @tjikkun!)
  * When competing background tasks are submitted, update the existing task details rather than dropping it completely (thanks @MikeBishop)
  * Bugfixes
      * Add a sleep of 5 seconds when waking car up to avoid an infinite loop (thanks @dschuesae)
      * Fix a bug with the legacy web interface which causes the Resume Track Green Energy setting of None to fail. Also added a deprecation notice to the web interface to ensure people don't inadvertently use it over the modular interface.
      * Fixed the Enphase EMS module which was reporting generation values as consumption (thanks @integlikewoah)
      * Added fix to avoid exception if an incoming TWC message is passed as an immutable bytes object to the unescape_msg function
      * Fix for the Fronius EMS module to query at System context rather than Device context which was failing to work in some installations due to Device ID mismatch
      * Fix dummy interface to load in place of RS485 interface for testing (thanks @tjikkun)
      * Add routines to avoid errors when settings keys are not defined (thanks @tjikkun)
      * Kostal EMS module no longer loads if not configured (thanks @MikeBishop)

## v1.2.0 - 2020-10-09

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
  * Improved HASS status updates - sets sensor class and unit (thanks @dschuesae)
  * Added a fallback Tesla API stop mechanism to reduce the charge limit if the stop command via Tesla API fails to stop the vehicle from charging. This only works when the SOC is 50% or higher due to limitations in the API (thanks @MikeBishop)
  * Added new Logging module support, which takes console messages and modularises them to allow output to other mechanisms such as CSV files or Databases.
  * Bugfixes
    * Fixed situation where fakeMaster == 2 installations do not recieve Status module lifetime kWh and voltage per phase readings [Backported to 1.1.8]
    * Fixed unnecessary 60 second delay to processing background queue introduced by lifetime kWh and voltage per phase polling [Backported to 1.1.8] (Thanks @MikeBishop)
    * Fixed a condition in which flex and alternative maximum clash on charging rate (thanks @MikeBishop)
    * Fixed a number of issues with voltage and amperage calculation (thanks @dschuesae)
    * Fixed error with scheduling via old web UI and day index (thanks @dschuesae)
    * Removed Track green energy non-scheduled charging action from old Web UI (as new policy engine is incompatible) and moved it to new Web UI to allow re-introduction.

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
  * Add policy webhook support (thanks @MikeBishop)
  * Added support for the SolarLog EMS Module (thanks @dschuesae)
  * Added support for OpenHab EMS Module (thanks @Frajul)
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
