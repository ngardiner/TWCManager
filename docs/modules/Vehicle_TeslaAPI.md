# Tesla FleetAPI Integration

## Introduction

TWCManager exists to **drive charging** - it starts, stops and rate-limits your
vehicle. The Tesla FleetAPI login covered here provides the cloud connection used to
read vehicle state (state of charge, location, online status) for policy and home
geofencing, and to issue charge commands.

> **Important: FleetAPI login alone cannot drive charging.** Tesla now requires the
> **Vehicle Command Protocol** for charge commands on essentially all vehicles - the
> legacy REST charge endpoints return `403 "Tesla Vehicle Command Protocol required"`.
> So to actually control charging you must **also** run the Tesla Vehicle Command proxy
> (set `teslaProxy`) **or** use the local [TeslaBLE](Vehicle_TeslaBLE.md) module. The
> login + tokens give you data and authentication; the proxy or BLE give you control.
> See [Driving charging](#driving-charging-proxy-or-ble) below.

Tesla retired the legacy Owner API login, so cloud access now generally requires you
to register your own application with Tesla and complete an OAuth login. An Owner API
token from another tool (such as TeslaMate) still reads vehicle state, and commands
only some vehicles; see [Owner API tokens](#owner-api-tokens). If you would rather not
register an application at all, TeslaBLE controls charging locally over Bluetooth and
needs no Tesla account.

## Owner API and Fleet API

TWCManager can hold an Owner API token and a Fleet API token side by side, and uses each for what it does best.

The Owner API is deprecated by Tesla and may stop working without notice. Its free reads are worth having while it lasts, but don't depend on it alone: for commands, pair the car over [Bluetooth](Vehicle_TeslaBLE.md) or set up the Fleet API; for news of the car, use [TeslaMate](Vehicle_TeslaMate.md) or [Fleet Telemetry](Vehicle_FleetTelemetryMQTT.md).

| | Owner API | Fleet API |
|-|-|-|
| Status | Deprecated by Tesla; may stop working without notice | Supported |
| Getting a token | Pasted, or synced from [TeslaMate](Vehicle_TeslaMate.md) | Your own registered app and the login below, or synced |
| Reads (charge state, location) | Free | Billed (about $0.002 a read) |
| Commands | Free, but only for cars Tesla lists with `command_signing: off` (see [Owner API tokens](#owner-api-tokens)) | Billed (about $0.001 a command, $0.02 a wake), through the Vehicle Command proxy |
| Free online check (asleep or awake) | Yes | Yes |

Reads go to the free Owner API when you have both. Both APIs come after Bluetooth (TeslaBLE) in the command order, and a billed command is sent only when no free path can serve the car, or after the free ones have failed for a while (see Vehicle Command Policy in [Settings](../Settings.md#vehicle-command-policy)).

A charge read happens when something needs it (a command, a TWC's plug event) and when the [vehicle watch](../Settings.md#vehicle-watch) asks: on a change such as an arrival, a wake or a plug-in, and on a clock for a car charging at home, every 5 minutes over the Owner API, or every 15 minutes on the Fleet API and only while a policy reads its battery level. The watch's checks of whether a car is asleep or awake use the free online check. A read stays fresh for 5 minutes for every caller. The watch's billed Fleet API reads draw from its monthly budget. `cloudUpdateInterval` does not set how often cars are read (1.4.1).

## Prerequisites

- A Tesla account that owns the vehicle(s).
- An application registered at https://developer.tesla.com with the **Authorization
  Code (Third-Party Tokens)** grant type (provides a client ID; a client secret is also
  issued but the default PKCE login does not use it).
- **To drive charging (required):** the Tesla Vehicle Command proxy **or** the TeslaBLE
  module. Tesla's REST charge commands are deprecated and now return
  `403 "Tesla Vehicle Command Protocol required"` on essentially all vehicles (confirmed
  even on a 2019 Model 3), so FleetAPI by itself reads data but cannot start/stop charge.
  See [Driving charging](#driving-charging-proxy-or-ble).

## Registering your application

1. Sign in at https://developer.tesla.com and create an application.
2. For the **OAuth grant type**, choose **Authorization Code (Third-Party Tokens)**.
   This is the user-authorization flow TWCManager uses. Do **not** choose **Client
   Credentials (Partner Tokens)** - that is for partner/machine tokens and does not log
   a user in.
3. Note the generated **client ID**. A **client secret** is also generated, but the
   default login does not use it (see Auth methods below).
4. Set the **allowed scopes** to include charging control, vehicle data and location,
   e.g. `vehicle_device_data`, `vehicle_cmds`, `vehicle_charging_cmds`,
   `vehicle_location` (plus `openid` and `offline_access`).
5. Add an **allowed origin** and one or more **redirect URIs**. Tesla ties the app to
   a **public HTTPS domain you control** (the allowed origin) - this is the domain
   where Tesla fetches your public key from
   `https://<domain>/.well-known/appspecific/com.tesla.3p.public-key.pem` during the
   partner/Vehicle Command setup. It **cannot be `localhost` or a LAN IP**, and it is
   **not** TWCManager's own address. The redirect URI must be HTTPS, registered in
   advance, and sit under that same domain; TWCManager must send it back exactly.

   > **Do not expose TWCManager directly to the internet.** It controls your charger
   > and has minimal authentication. The redirect domain is a domain you own; it does
   > not need to reach TWCManager at all (see paste-back below).

   Two ways to receive the authorization code:
   - **Paste-back (recommended).** Register any redirect URI under your domain - it can
     point at a static page or somewhere TWCManager can't see. After approving access
     you copy the URL from your browser's address bar and paste it into TWCManager.
     No inbound exposure of TWCManager is required.
   - **Auto-capture (advanced).** Only if you already run a reverse proxy you control
     that terminates HTTPS and forwards `/teslaAccount/callback` to TWCManager on your
     LAN (e.g. `https://twc.yourdomain.com/teslaAccount/callback` -> `192.168.x.y:8080`).
     Tesla returns the browser to that URL and login completes with no copy/paste.
     This still must not be a TWCManager instance published openly to the internet.

## Auth methods (PKCE vs client secret)

The Authorization Code flow can be completed two ways. TWCManager picks automatically
based on whether you configure a client secret:

- **PKCE (default, recommended).** No client secret. TWCManager proves the request with
  a PKCE code challenge and exchanges the code at `auth.tesla.com`. The account region
  is derived from the returned token, so you don't need to set it. This is what most
  registered apps use. Just set `teslaApiClientID` and `teslaApiRedirectUri`.
- **Client secret (optional/advanced).** If you set `teslaApiClientSecret`, TWCManager
  instead sends the secret plus a regional `audience` and exchanges the code at the
  regional `fleet-auth` endpoint. This also requires `teslaApiRegion`.

## Configuration

Set the following in the `config` section of `config.json`:

| Key | Required | Description |
|-----|----------|-------------|
| `teslaApiClientID` | Yes | Client ID of your registered application |
| `teslaApiRedirectUri` | Yes | Redirect URI, exactly matching one registered on the app (under your public domain - not TWCManager's address) |
| `teslaApiClientSecret` | No | Only to force the client-secret exchange instead of PKCE (advanced) |
| `teslaApiRegion` | No | Only used with `teslaApiClientSecret`: `NA` (default), `EU` or `CN` |
| `teslaApiScope` | No | Override the requested OAuth scopes |
| `wakeDelayMins` | No | Minutes to wait before sending a Fleet API wake command (default: 3). During the delay the car may self-start from the TWC power offer. Set to 0 to wake immediately. After a wake that answers asleep, offline or waking, the free online check looks every 5 s for up to 45 s; a car still not online then waits 30 minutes for the next wake. |

If you do set `teslaApiClientSecret`, keep `config.json` readable only by the
`twcmanager` user.

> **Energy features need the `energy_device_data` scope.** Two features read
> your Tesla energy site rather than a car: naming the car on each
> [Gen3 Wall Connector](../Gen3_Status.md#which-car-is-on-a-gen3), and the
> [Powerwall](EMS_Powerwall2.md) module's Storm Watch status. Both need a
> Fleet API token with `energy_device_data`, which the default login does not
> ask for. Add it to your app's allowed scopes at developer.tesla.com, set
> `teslaApiScope` to the default list plus `energy_device_data`:
>
> `"teslaApiScope": "openid offline_access vehicle_device_data vehicle_cmds vehicle_location vehicle_charging_cmds energy_device_data"`
>
> and log in again. Without it, those two features don't work, and
> TWCManager logs once which scope is missing. The Powerwall's own local
> readings don't need it.

## Logging in

1. Open the TWCManager web UI. When no token is stored, the home page shows a Tesla
   login section.
2. Click **Log in to Tesla** (with PKCE there is no region to choose; the client-secret
   method shows a region selector first).
3. Approve access in the Tesla consent page.
4. Completion:
   - Paste-back (typical): copy the full URL you were redirected to from your browser's
     address bar and paste it into the box, then click **Save Token**.
   - Auto-capture: only if your redirect URI is served by a reverse proxy that forwards
     to TWCManager, you are returned automatically and the tokens are stored.

Once tokens are stored, TWCManager refreshes them automatically (the refresh token is
rotated on each refresh and persisted to the settings file).

## Driving charging (proxy or BLE)

Logging in gets you a token and lets TWCManager **read** vehicle state. To actually
**start, stop and rate-limit charging** - the reason TWCManager exists - you need one of:

- **Tesla Vehicle Command proxy.** Run the
  [vehicle-command](https://github.com/teslamotors/vehicle-command) HTTP proxy, which
  signs commands with your app key, and point TWCManager at it with `teslaProxy`
  (HTTPS URL) and `teslaProxyCert`. TWCManager then sends charge commands through the
  proxy instead of the deprecated REST endpoints. Partner-account registration and
  hosting your public key at `/.well-known/appspecific/com.tesla.3p.public-key.pem` are
  prerequisites of that proxy setup (the same key/domain used for Fleet Telemetry), not
  handled by TWCManager.
- **TeslaBLE.** Control charging locally over Bluetooth with no proxy and no cloud
  command path. See [TeslaBLE](Vehicle_TeslaBLE.md).

Without one of these, charge commands fail with
`403 "Tesla Vehicle Command Protocol required"` and TWCManager logs a clear error and
stops retrying (it will still read state via the FleetAPI). FleetAPI data + (proxy or
BLE) control is the intended, complete configuration.

## Owner API tokens

An Owner API token (pasted, or synced from [TeslaMate](Vehicle_TeslaMate.md)) reads
vehicle state through the Owner API. Whether it can also send charge commands depends
on the vehicle. Tesla's vehicle list (`/api/1/products`) reports a `command_signing`
value for each vehicle:

- `"off"`: Tesla accepts Owner API commands for that vehicle, so TWCManager sends
  charge commands (start/stop, charge limit, charge rate, wake) for it with the Owner
  API token.
- `"required"`: the vehicle needs signed commands, and Owner API commands return
  `403 "Tesla Vehicle Command Protocol required"`. TWCManager treats it as read-only
  and logs once that its charge commands need a Fleet API token with the Vehicle
  Command proxy (`teslaProxy`) or the TeslaBLE module.
- Missing, or any other value: treated as read-only.

TWCManager decides this per vehicle, so one account can have a vehicle it commands and
a vehicle it only reads. A read-only vehicle gets no command from the Owner API and no
API error is recorded; the next provider in the command order (such as TeslaBLE or
Home Assistant) carries its commands.

Owner API commands cost nothing, but they sit with the Fleet API in the command order,
just ahead of it: TWCManager uses them after TeslaBLE has failed for a while, with the
same limit on repeated sends, and never to set a car's charge rate where a TWC or BLE
can.

If Tesla still refuses an Owner API command (HTTP 403, or the "Vehicle Command
Protocol required" error) for a vehicle that reported `"off"`, TWCManager treats that
vehicle as read-only until it restarts and logs a warning. This does not pause reads,
and other vehicles keep their Owner API commands. The next provider in the command
order can send at once: the refused Owner API command does not count against its
limit on repeated sends.

If Tesla stops taking Owner API commands altogether (HTTP 410 or 412, or an error
saying the endpoint is "only available on fleetapi"), TWCManager logs one error and
sends no further Owner API commands or wakes, for any vehicle, until the Owner API
token is entered again or synced again from TeslaMate (which tries once more). Reads
continue with the same token, and no API error is recorded, so the next provider in
the command order carries the commands.

When you also hold a Fleet API token that can send commands, each command goes to the
first provider in the command order that can send it. A vehicle Tesla reports as
`"off"` gets its commands through the Owner API, at no cost; once Tesla refuses them
(either case above), the Fleet API (and the proxy, if set) sends them, at once. Every
other vehicle gets its commands through the Fleet API, as before, and is not logged as
read-only.

Not yet confirmed:

- Whether `"off"` always means a pre-2021 Model S or X. It has been seen on a 2018
  Model X.
- Whether Tesla returns other `command_signing` values, or changes a vehicle's value
  over time. TWCManager reads the vehicle list once, so it sees a change only after a
  restart.
- Whether charge start and stop behave like the charge limit and charge rate commands
  on `"off"` vehicles. Only the latter two (and wake) have been tried.
- Whether the Fleet API vehicle list includes `command_signing`. When the vehicles are
  listed through the Fleet API, a missing value leaves them read-only for Owner API
  commands.

## Alternatives to the web login

- Manual token entry: the Settings page has a manual API token override if you already
  hold FleetAPI tokens.
- [TeslaMate](Vehicle_TeslaMate.md) token sync.
- [TeslaBLE](Vehicle_TeslaBLE.md) for local, account-free control.

## Troubleshooting

- "Tesla FleetAPI is not configured": `teslaApiClientID` or `teslaApiRedirectUri` is
  missing (the client secret is optional).
- "state mismatch": the login was started too long ago or in another browser session.
  Start the login again.
- A Tesla error code on the result page usually indicates a mismatch between the
  registered redirect URI / scopes and what was requested. Re-check the application
  settings at developer.tesla.com.
