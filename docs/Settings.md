# Settings

The following settings (outside of the configuration file) are available via the inbuilt Web Interface:

## Web Interface Theme

Allows you to select which theme you'd like to use for the TWCManager interface.

## Home Location

Home is set or moved only from a fresh position read of a car: a position under 10 minutes old, by the fix's own GPS time. That happens when a TWC reads the car's VIN, or when the car's position arrives over BLE. When no home is set yet, a fresh position that arrives while a TWC is naming the car sets it too. A position not read over BLE must also have been read after the car reached its TWC (its plug-in or the VIN read), so a read from before the drive home never moves home, and once home is set, only a BLE position (or the Settings page) moves it. Home is never set from a car's first position otherwise. A car-only install with no TWC and no BLE must set home on the Settings page (use the vehicle location button), or with `homeLat` and `homeLon` through the [setSetting API](modules/control_HTTP_API/setSetting.md). Until then TWCManager logs a warning once, and a car known only through vehicle APIs is never treated as home and gets no limit. `atHomeRadius` defaults to 1,000 feet.

## Stop Charging Method

This option, which defaults to a vehicle command, allows you to specify how you would like TWCManager to stop a car from charging. The following options exist, and each has some information to help you decide on which approach to use:

   * Vehicle command (BLE or API)
      * This method sends a stop charging command to vehicles which are detected as being "at home" (see Charge Limits in Policy Customization for how that is decided), over BLE where available and the Tesla API otherwise (see Vehicle Command Policy below).
      * This method is only effective for Tesla vehicles. Any non-Tesla vehicles are not visible via the Tesla API and will not be stopped, even if the available power falls below the minimum power per TWC.
      * For those vehicles, they will continue to charge, but at the minimum power per TWC rate.
   * Stop Responding to Slaves
      * This method will cause TWCManager to stop responding to Slave TWCs when the allocated amps per TWC falls below the minimum amps per TWC value. It takes approximately 30 seconds from the moment that we stop responding to slaves until they stop charging connected vehicles.
      * This has the effect, if any vehicle is connected (not just Tesla vehicles) of stopping the TWC from offering charge to a vehicle. The green light on the TWC will remain steady, whilst the red light will blink on the TWC whilst the communication ceases, and no updates will be recieved from a TWC for that period of time.
      * For non-Tesla vehicles, this has the effect of stopping them from charging. It is not known on a per-vehicle basis (until more information is submitted) what the behaviour of those vehicles are.
      * For Tesla vehicles, this method is effective up to three times during a single charging session. The vehicle will allow this until the third instance, at which point it will refuse to resume charging until it is unplugged and re-plugged.
   * Send stop command
      * For TWCs running version v4.5.3 or later, there is a stop command embedded in the Firmware. The stop command appears to take the approach of disconnecting the relay, without sending any CAN bus messages.
      * With the DIP switch in position 1 (CAN protocol enabled), this has the unfortunate outcome for Tesla vehicles of entirely stopping the vehicle from charging immediately on reciept of the message. 
      * With the DIP switch in position 2 (CAN protocol disabled), it will stop Tesla vehicles from charging, but the vehicle will eventually (after a number of interruptions) decide that the charger is broken and will refuse to start charging again.
      * [This thread](https://teslamotorsclub.com/tmc/threads/new-wall-connector-load-sharing-protocol.72830/page-24) provides the observations of those who have tested this command.
      * Whilst this option is offered primarily for the benefit of non-Tesla vehicles, it's not recommended for use with Tesla vehicles.

### When There Isn't Power for Every Car

When power runs short, TWCManager first gives each charging station a place while its minimum still fits, cars already charging first, then cars that would charge. The stations with a place share the power at one level, each between its minimum and maximum.

A charging car that loses its place is not stopped straight away. Its charging station is turned down to its minimum, and the car is stopped only if the shortage lasts `startStopDelay` seconds with its charging current steady. Once power is back, a car turned down stays at its minimum, and a car stopped stays stopped, until power has been back for `startStopDelay` too. This applies to every kind of charging station: Gen2 and Gen3 Wall Connectors, and other EVSEs where TWCManager sets the car's own charge rate. On a Gen2, a car drawing 4 A or less is not stopped for lack of power, and a car gets power again as soon as its Wall Connector has some to offer.

The wait is there to ride out an energy monitor's dips. Under a policy with a fixed charge rate, such as Scheduled Charging or Charge Now, there are no such dips, so a car without a place is stopped at once.

`startStopDelay` is set in config.json, under `config`. The default is 60 seconds. If you use Track Green Energy with loads that cycle, such as an oven or a dryer, a longer delay (several minutes) might be better at riding out their dips. That is a choice for your own installation; the default suits most.

### The Car's Own Charging Schedule

Use one scheduler, not both: either TWCManager's Scheduled Charging, or the car's own scheduled charging or departure time. The two don't coordinate, and each may start or stop charging when the other doesn't expect it. In particular, a car set for a departure time can start charging earlier than you expect when its charging station offers little power, because it plans to reach its charge limit by departure at the rate it is offered.

`respectVehicleSchedule` (in config.json, under `config`, on by default) only stops TWCManager from starting a car whose own scheduled charging is waiting to begin, which would override the car's schedule. It doesn't make the two schedules work together.

### Non-Scheduled Power Action & Charge Rate

This setting determines what TWCManager should do if there is no scheduled charging rate and outside of the Track Green Energy hours. The available options are:

      * Charge at specified Non-Scheduled Charge Rate

This option will allow vehicles to charge outside of Scheduled and Track Green Energy timings at the specified charge rate. This is the default setting (and the default Charge Rate is 6A).

      * Do not Charge

The Do not Charge action states that outside of scheduled or Track Green Energy hours, the TWCManager should set the available amps to 0 for Non-Scheduled Charging.

      * Track Green Energy

This option continues Track Green Energy outside its usual hours, which run from sunrise to sunset (6am to 8pm until TWCManager has looked up sunrise and sunset for your home location). With it, charging follows your generation and consumption at any hour.

### Charge Authorization Mode

The Charge Authorization Mode determines how we determine if a vehicle that starts to charge from a TWC should be allowed to do so. There are two options available:

   * Vehicles can charge unless explicitly blocked

This setting is the default, and specifies that any vehicle which requests to start charging can do so, unless it is explicitly added to the Deny Charging group.

   * Vehicles can only charge if explicitly allowed

This setting specifies that any vehicle which requests to start charging will be blocked, unless it has been added to the Allow Charging group.

### Vehicle Command Policy

Vehicle commands (start and stop charging, set the charge rate, set the charge limit, wake) go to the first ready provider in a fixed order: the TWC itself, then BLE, then the cloud, with billed providers (the Owner API, which Tesla does not bill but which is paced like a billed provider, for a car Tesla lists with `command_signing: off`; then the Fleet API, then Home Assistant) last. Free providers are always tried first. A billed provider is used only when no free one can serve the car, or after the free ones have failed for 5 minutes (charge limit) or 60 seconds (start/stop, amps), and sends each desired value at most four times. A start is done while the TWC the car is plugged into measures at least 1 A, in the last 30 seconds and since the car last reported, so no start or retry goes to a car already charging. The Vehicle Command Policy suppresses providers in that order:

   * `prefer_ble` - Prefer local (BLE), fall back to cloud after a delay. Nothing is suppressed. This is the default.
   * `ble_only` - Local only - never send billed cloud commands. Every non-local provider (the Owner API, the Fleet API and Home Assistant) is suppressed for commands; cloud reads still run.
   * `api_only` - Cloud only - never use BLE. TeslaBLE is suppressed for every operation, reads as well as commands.

Setting `commandPolicy` under `vehicle` in config.json hard-sets the policy and disables this control.

#### Who sets a car's charge rate

Every Fleet API command is billed, so TWCManager never uses the Fleet API to set a car's charge rate where a local path can: the TWC the car is plugged into, or BLE for a paired car. Charge Rate Control modes 2 and 3, which ask for the rate as a vehicle command, are the exception: there the rate goes through the vehicle command order, and the Fleet API may carry it. A car on a TWC gets its rate from the TWC. A BLE-paired car on a mobile connector (or any EVSE TWCManager does not control) gets its rate over BLE only (1.4.1). If the BLE command fails, the Fleet API does not step in. To let the Fleet API carry the rate for such a car, use `api_only`. Under Charge Rate Control mode 2 (and mode 3 below 6 A), the TWC offers the car's share, not its maximum, until the car's rate is confirmed by a delivered command or a reading; a rate that cannot be delivered leaves the TWC enforcing the share.

#### BLE retries

A BLE command that times out or fails to connect is retried once, two seconds later; any other failure is reported straight away. The vehicle command order then decides when to try again and whether to fall back to the cloud. The `maxRetries`, `retryDelay`, `circuitBreakerThreshold` and `circuitBreakerTimeout` keys under `config.moduleConfiguration.TeslaBLE` are ignored (1.4.1).

#### providerSuppressions

For finer control than the policy, the `providerSuppressions` setting (in settings.json, or under `vehicle` in config.json, which wins for a provider named in both) lists, per provider, the operations it may not perform:

```json
"providerSuppressions": {
  "FleetAPI": ["setAmps", "wake"],
  "HomeAssistant": ["*"]
}
```

Provider names are `Gen2TWC`, `Gen3TWC`, `TeslaBLE`, `OwnerAPI`, `FleetAPI` and `HomeAssistant`. The telemetry feeds (TeslaMate, Fleet Telemetry) only report, so they are not providers and cannot be suppressed. Capability names are the reads `readCharge` (charge state and location) and `wallConnectorVins`, and the commands `setChargeLimit`, `startCharging`, `stopCharging`, `setAmps` and `wake`; `"*"` suppresses every operation. Suppressing `readCharge` also takes the provider out of vehicle reads. Unknown names are ignored, including the earlier `readList`, `readOnline`, `readLocation`, `readPlug`, `readVin` and `readAmps`, which nothing ever read. The suppressions apply on top of the Vehicle Command Policy.

### Vehicle watch

A TWC tells TWCManager when a car plugs in, charges and leaves. A car on any other EVSE (a mobile connector, a third-party charging station) gets no such report. The vehicle watch notices its arrival, plug-in, charge start, charge end and departure, so the car gets its place when power is shared, and a car that is done or gone stops being counted.

The watch looks after every car that no TWC names and that no live telemetry feed (TeslaMate, Fleet Telemetry) covers. A TWC reports its own car's changes, and a feed reports changes itself. When a feed goes down, the watch picks the car up at its next due check, which is at most one cadence later. A car with no VIN is never watched.

The watch never wakes a car and never keeps one awake. It works in three rings, cheapest first:

* **Passive listening.** A Bluetooth scan for the car's advertisement, a step in the house's consumption, and what the TWCs and feeds report. All free, and none touches the car. One `bluetoothctl` scan listens for every car at once.
* **Status checks.** Is the car asleep, awake or offline? Over BLE this is the keyless sleep-safe check; through the cloud it is the free online check. Both are free and never wake the car. An asleep car is not charging and not moving, so this answer alone carries the watch most of the time.
* **Charge reads.** Battery level, charge state, amps and location. Over BLE or the Owner API a read is free. On the Fleet API it costs $0.002, taken from the watch's budget. A car that is awake and not charging is read when something changes: it woke, it arrived, it plugged in, its charge-port door opened (seen by BLE's check), or the consumption step asks for it. A car only the Tesla API can see is also read while it stays awake (below). After a wake-triggered read, an awake car at home is not read again for `sleepCourtesySecs`; a plug-in, a door opening and a consumption-step read skip that wait. A read asked for by a change that fails is tried once more, `chargingReadSecs` later (or once the `sleepCourtesySecs` wait is over, if the read was subject to it). An asleep car is never read. While a car is charging at home, it is read on a clock, but only if a provider can set its amps, and on the Fleet API only if a policy reads its battery level (below). A car that can only be watched is not read for its amps.

The status check runs on a clock set by what the car is doing:

| Where the car is | Status check | Charge read |
|---|---|---|
| Home, asleep | every 15 minutes (`homeAsleepSecs`) | none |
| Home, awake | every 4 minutes (`homeAwakeSecs`) | on a change, then not again for 20 minutes (`sleepCourtesySecs`); a car only the Tesla API sees, again every 20 minutes while it stays awake |
| Home, charging | every 4 minutes (`homeAwakeSecs`); asleep means the charge ended | every 5 minutes over BLE or the Owner API (`chargingReadSecs`); on the Fleet API every 15 minutes (`cloudChargingReadSecs`), and only when a policy reads the battery level |
| Away, asleep | every 30 minutes (`awayAsleepSecs`) | none |
| Away, awake, or sleep state not known yet | every 5 minutes (`awayAwakeSecs`) | on a wake; a car only the Tesla API sees, again every 20 minutes from its last fix |

An away car is not asked about until it could be back. That time is its last position fix plus the straight-line distance to home at 120 km/h (75 mph, `returnKph`), and never less than 300 seconds. Before it, nothing runs for that car.

On the Fleet API a charging car is read every `cloudChargingReadSecs` only while a policy reads its battery level: a policy condition naming `modules.TeslaAPI.minBatteryLevelAtHome`, or the limit override that stops a car through its charge limit. Otherwise it is not read while it charges. Its share is released when it falls asleep, about 15 minutes after its charge ends, and the consumption step works from its last known draw.

#### Cars only the Tesla API can see

Where no TeslaBLE module is loaded, or its radio cannot work (no controller, an adapter powered off or blocked, or `bluetoothd` not answering), a watched car with a Tesla API connection is seen only through the API. A car follows these rules only while the radio is down, and returns to Bluetooth when it is back; its watch line on the Vehicles page says "cloud". Asleep or awake says nothing about where a car is, but a read on its wake learns the rest. For such a car the watch:

* checks its sleep state on the cadences above, for free;
* reads it (charge state and position) when a check finds it woke, home or away;
* reads it again while it stays awake and not charging, once its last position fix is `sleepCourtesySecs` old. At home that is one read per 20 minutes awake, and a parked car usually sleeps within about 15 minutes, so in practice it is one read per wake. A car that drives off while awake is seen gone within one window. The window never keeps a car awake.

After a fix away from home, nothing runs for the car until its return time; a fix 3 hours out means 3 hours with no request. On the Owner API all of this is free. On the Fleet API it is about one $0.002 read per wake at home, plus one per wake away once its return time has passed: about $0.01 a day for a car with a few trips, out of the budget. Fleet Telemetry reports these changes itself and costs less (see [Fleet Telemetry](modules/Vehicle_FleetTelemetryMQTT.md)), but it is not required.

#### Cars Bluetooth can hear

With the TeslaBLE module loaded and its radio working, the watch makes no cloud status checks. The only cloud status check is a command held for a sleeping car, which checks the car's sleep state for free every 15 minutes at home and every 30 minutes elsewhere, and only while it is held.

A car in Bluetooth range answers BLE's check even asleep, so two missed checks in a row run a range scan, whatever the car last reported. A scan that also misses marks the car out of range. Where no scan is possible, the second miss does that itself for an away car. A car at home stays on Bluetooth's checks, and an awake car with a Tesla API connection gets one charge read through the cloud, once, until Bluetooth answers again, which can find that it has left.

A car Bluetooth has lost (away, with two misses) is scanned (or pinged, where no scan is possible) every 150 seconds (`scanAwaySecs`) for `awayAsleepSecs` after its last sign of life, then every `awayAsleepSecs`, whatever it last reported.

After a restart, the first pass asks for no arrival, wake or plug-in read, since what a car was before the restart is no news; a car charging at home is still read. A car first seen later still gets its arrival read.

A TWC-named car is read while its TWC reports current, every `chargingReadSecs` over BLE or the Owner API, or every `cloudChargingReadSecs` on the Fleet API while a policy reads its battery level, and when it plugs in. Nothing else about it is watched.

#### Consumption step

With a consumption meter configured, the watch also looks for a step in the house's consumption that nothing known explains. When the load rises or falls by at least a car's minimum draw (or `emsNoiseWatts`, if that is higher) and the next sample holds it, the watch reads every candidate car at once. A rise asks about awake cars, or cars whose state is not yet known, that are not charging; a fall asks about cars known to be charging. A car known to be away is never asked; a "probably home" car is asked once its return time has passed. A rise reads each car at most once every `sleepCourtesySecs`, so a load that cycles (an air conditioner, an oven) never keeps a car awake; a fall reads each car at most once every 5 minutes. A step is only a trigger: a clothes dryer looks the same, and the car's own read decides. With no consumption meter this step is off.

#### Budget

Cloud reads draw from a budget, `monthlyBudgetUSD`, which defaults to $3. Tesla's free credit is $10 a month, and it also covers commands, wakes and Fleet Telemetry, so the default leaves room for the rest. At $0.002 a read, $3 is about 1,500 reads a month. The budget refills steadily and holds at most one day's worth. A read caused by a change may spend all of it; a routine read (amps while charging) leaves half a day's worth in reserve. When the budget runs out, cloud reads stop and the free status checks continue. The log says once when a read caused by a change is refused for lack of budget, and once when it is available again; routine reads stop quietly at the reserve. The budget is saved, so a restart does not refill it. Only the watch's own reads draw from it. Commands and the reads made before them do not.

Free reads (BLE and the Owner API) are never held back by the budget.

#### The `watch` settings

All keys are optional. The `watch` block goes at the top level of `config.json`, beside `vehicle`:

```
"watch": {
    "enabled": true,
    "homeAsleepSecs": 900,
    "homeAwakeSecs": 240,
    "awayAsleepSecs": 1800,
    "awayAwakeSecs": 300,
    "chargingReadSecs": 300,
    "cloudChargingReadSecs": 900,
    "scanAwaySecs": 150,
    "sleepCourtesySecs": 1200,
    "returnKph": 120,
    "monthlyBudgetUSD": 3.0,
    "emsTrigger": true,
    "emsNoiseWatts": 300
}
```

`enabled` turns the whole watch off. `emsTrigger` turns off the consumption step alone. While it is on, an energy source is configured and a car is watched, the source is read on every policy pass, as `policy.alwaysPollEMS` does, not only under a green policy; the step needs a fresh reading to see a car start or stop on another charger.

The `cloudUpdateInterval` setting is the Powerwall module's cloud cache time. It does not set how often cars are read; the watch does.

The Vehicles page shows, for each car, whether it is watched (and if not, why), the last check and its answer, and when the next one is due.

### Consumption Offsets

Consumption Offsets are values which are applied to the Consumption value retrieved from EMS modules. Consumption Offsets can be either positive or negative values, and are often used to control the behaviour of TWCManager by making it appear as though the consumption is higher or lower than it is, or allows specifying an average consumption value for installations where it is not possible to query consumption data.

The value supplied can be:

   * Expressed in Amps. This is internally converted into watts by TWCManager, by multiplying with the current grid voltage, meaning the actual applied value of this offset will vary with voltage variations.
      * You would use an offset in Amps if you wanted to express an offset in circuit capacity - for example, if you already have a 1A load on a 20A circuit which is not metered, specifying +1A of consumption offset will treat that 1A as metered consumption.

   * Expressed in Watts. This allows for a specific power value to be specified. There will be no variance to offsets expressed this way.
      * You might use an offset like this to control the way that you treat Generation and Consumption data from Solar installations. For example, if you wanted to reduce the value measured from a Solar Array by 500W, you could add 500W of consumption via an offset.

This is most often given a value equal to the average amount of power consumed by everything other than car charging. 

For example, if your house uses an average of 2.8A to power computers, lights, etc while you expect the car to be charging, you could add an offset of 2.8A.

If you have solar panels, look at your utility meter while your car charges.

If it says you're using 0.67kW, that means you could either:
   * Add an offset for 0.67kW * 1000 / 240V = 2.79A assuming you're on the North American 240V grid. 
   * Add an offset for 670W, which would universally treat that 0.67kW as consumption regardless of grid voltage.

In other words, during car charging, you want your utility meter to show a value close to 0kW meaning no energy is being sent to or from the grid.

If you are able to obtain consumption details from an energy management system, don't add consumption offsets (unless you need them for other purposes), as TWCManager will query your EMS to determine the power being consumed.

### Tesla API tokens

The Settings page shows a slot for an Owner API token and one for a Fleet API token, each with its status and a Remove token button. TWCManager can hold both: reads use the free Owner API, and commands go through the [vehicle command order](#vehicle-command-policy). See [Tesla Fleet API](modules/Vehicle_TeslaAPI.md) for logging in, and [TeslaMate](modules/Vehicle_TeslaMate.md) for syncing a token from TeslaMate.

### Manual Tesla API key override

If you already hold Tesla API tokens from another application, you can paste them here instead of logging in. The Settings page links a <a href="http://registration.teslatasks.com/generateTokens">third-party token generator</a>; using it means giving that service your Tesla credentials. A pasted access token goes into the Owner API or the Fleet API slot according to its type, with the refresh token if you paste one too. A refresh token pasted on its own replaces the tokens in the slot of its type and is refreshed at once; if Tesla rejects it, the previous tokens are kept.

Pasting a token replaces the tokens stored in that slot. Back up your settings.json file before entering a token manually if you may need to revert.
