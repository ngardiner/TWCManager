# getVehicle API Command

## Introduction

The getVehicle API command returns one vehicle, by VIN, with what the
vehicle's page shows: everything [getVehicles](getVehicles.md) gives for it,
plus its pending commands, its Bluetooth (BLE) status and its charge session
count. The vehicle's page reads this command every few seconds to keep itself
current. No token or other secret is ever included.

## Format of request

The getVehicle API command takes the VIN as the last part of its path.

An example of how to call this function via cURL is:

```
curl -X GET http://192.168.1.1:8080/api/getVehicle/5YJ3E1EA1KF000001
```

A VIN TWCManager does not know returns status 404.

## Format of response

```json
{
    "fingerprint": "e3ae5aaba3ca",
    "now": 1794195366.70,
    "budget": 0.1,
    "spend": {
        "fleet": false,
        "telemetry": false
    },
    "vehicle": {
        "key": "5YJ3E1EA1KF000001",
        "vin": "5YJ3E1EA1KF000001",
        "name": "",
        "anonymous": false,
        "point": null,
        "appearance": {},
        "presence": "presumed",
        "watch": {
            "watched": true,
            "why": "no status source",
            "via": null,
            "last": "",
            "lastAt": null,
            "nextAt": null
        },
        "providers": [],
        "lastSeenAt": 1794195366.66,
        "lastSeenSource": "created",
        "heldLimit": null,
        "spend": null,
        "pending": [],
        "ble": null,
        "chargeSessions": 0
    }
}
```

Times are in seconds since the epoch (Unix time).

* `fingerprint` changes when the page's layout would. Clients can ignore it.
* `now` is the server's time when the response was made. Work out how long ago
  something happened from `now`, not from the client's clock.
* `budget` and `spend` are as in [getVehicles](getVehicles.md), without this
  month's totals.
* `vehicle` holds the fields [getVehicles](getVehicles.md) lists for a vehicle,
  and:
    * `pending` lists each change TWCManager wants made to the car that the car
      has not made yet (such as its charge rate or charge limit): the value
      wanted, the command in flight, if any, and since when; why it has not
      been sent, if it has not; and the last outcome.
    * `ble` is the car's Bluetooth status when the TeslaBLE module is loaded
      and the car's VIN is known (`null` otherwise): its pairing status and label, whether the car was in
      range at the last scan and when that was, whether a scan is pending or
      possible, and the last pairing attempt.
    * `chargeSessions` is how many charge sessions the car has had here.
