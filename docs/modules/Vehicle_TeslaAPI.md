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
| `wakeDelayMins` | No | Minutes to wait before sending a Fleet API wake command (default: 3). During the delay the car may self-start from the TWC power offer. Set to 0 to wake immediately. |

If you do set `teslaApiClientSecret`, keep `config.json` readable only by the
`twcmanager` user.

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
a vehicle it only reads. For a read-only vehicle, TeslaAPI sends no command and records
no API error, and the next vehicle module (such as TeslaBLE) is tried.

If Tesla still refuses an Owner API command for a vehicle that reported `"off"`,
TWCManager treats that vehicle as read-only until it restarts and logs a warning
naming the cause:

- HTTP 403, or a response naming the Vehicle Command Protocol: the vehicle needs
  signed commands after all.
- HTTP 410 or 412, or a response saying the endpoint is "only available on
  fleetapi": the Owner API no longer takes commands for it.

Either way, reads continue, no API error is recorded (so there is no error backoff),
and the command falls through to the next vehicle module.

When you also hold a Fleet API token that can send commands, commands go through the
Fleet API (and the proxy, if set) for every vehicle, as before. The Owner API token
carries commands only when no Fleet API token can.

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
