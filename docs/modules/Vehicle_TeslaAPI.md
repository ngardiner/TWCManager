# Tesla FleetAPI Integration

## Introduction

TWCManager uses the Tesla FleetAPI to read vehicle state (state of charge, location)
and to start, stop and rate-limit charging for Tesla vehicles you own.

Tesla retired the legacy Owner API, so cloud control now requires you to register
your own application with Tesla and complete an OAuth login. If you would rather not
register an application, the [TeslaBLE](Vehicle_TeslaBLE.md) module controls charging
locally over Bluetooth and needs no Tesla account.

## Prerequisites

- A Tesla account that owns the vehicle(s).
- An application registered at https://developer.tesla.com (provides a client ID and
  client secret).
- For sending commands to vehicles built after 2021, the Tesla Vehicle Command proxy.
  See [the proxy](https://github.com/teslamotors/vehicle-command) and the `teslaProxy`
  / `teslaProxyCert` options in config.json. Partner account registration and hosting
  your public key are part of that proxy setup and are not handled by TWCManager.

## Registering your application

1. Sign in at https://developer.tesla.com and create an application.
2. Note the generated **client ID** and **client secret**.
3. Set the **allowed scopes** to include charging control and vehicle data, e.g.
   `vehicle_device_data`, `vehicle_cmds`, `vehicle_charging_cmds` (plus `openid` and
   `offline_access`).
4. Add an **allowed origin** and one or more **redirect URIs**. The redirect URI must
   match exactly what TWCManager sends. Two options:
   - Auto-capture: register the callback on your TWCManager instance, e.g.
     `https://your-twcmanager-host/teslaAccount/callback`. Tesla returns the browser
     to TWCManager and login completes with no copy/paste.
   - Paste-back: register any redirect URI you control. After login you copy the URL
     you were redirected to and paste it into TWCManager.

## Configuration

Set the following in the `config` section of `config.json`:

| Key | Required | Description |
|-----|----------|-------------|
| `teslaApiClientID` | Yes | Client ID of your registered application |
| `teslaApiClientSecret` | Yes | Client secret of your registered application |
| `teslaApiRedirectUri` | Yes | Redirect URI, matching one registered on the app |
| `teslaApiRegion` | No | Account region: `NA` (default), `EU` or `CN` |
| `teslaApiScope` | No | Override the requested OAuth scopes |

Keep `config.json` readable only by the `twcmanager` user, as it holds the client
secret.

## Logging in

1. Open the TWCManager web UI. When no token is stored, the home page shows a Tesla
   login section.
2. Choose your account region and click **Log in to Tesla**.
3. Approve access in the Tesla consent page.
4. Completion:
   - Auto-capture: if your redirect URI points at this instance, you are returned
     automatically and the tokens are stored.
   - Paste-back: copy the full URL you were redirected to and paste it into the box,
     then click **Save Token**.

Once tokens are stored, TWCManager refreshes them automatically (the refresh token is
rotated on each refresh and persisted to the settings file).

## Alternatives to the web login

- Manual token entry: the Settings page has a manual API token override if you already
  hold FleetAPI tokens.
- [TeslaMate](Vehicle_TeslaMate.md) token sync.
- [TeslaBLE](Vehicle_TeslaBLE.md) for local, account-free control.

## Troubleshooting

- "Tesla FleetAPI is not configured": one of `teslaApiClientID`,
  `teslaApiClientSecret` or `teslaApiRedirectUri` is missing.
- "state mismatch": the login was started too long ago or in another browser session.
  Start the login again.
- A Tesla error code on the result page usually indicates a mismatch between the
  registered redirect URI / scopes and what was requested. Re-check the application
  settings at developer.tesla.com.
