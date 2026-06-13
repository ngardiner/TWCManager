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
4. Add an **allowed origin** and one or more **redirect URIs**. Tesla ties the app to
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

## Configuration

Set the following in the `config` section of `config.json`:

| Key | Required | Description |
|-----|----------|-------------|
| `teslaApiClientID` | Yes | Client ID of your registered application |
| `teslaApiClientSecret` | Yes | Client secret of your registered application |
| `teslaApiRedirectUri` | Yes | Redirect URI, exactly matching one registered on the app (under your public domain - not TWCManager's address) |
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
   - Paste-back (typical): copy the full URL you were redirected to from your browser's
     address bar and paste it into the box, then click **Save Token**.
   - Auto-capture: only if your redirect URI is served by a reverse proxy that forwards
     to TWCManager, you are returned automatically and the tokens are stored.

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
