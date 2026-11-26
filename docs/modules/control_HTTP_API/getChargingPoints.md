# getChargingPoints API Command

## Introduction

The getChargingPoints API command returns every charging point TWCManager
manages, with what each is offering and drawing now and the car on it, if
known. A charging point is a Gen2 TWC, a Gen3 Wall Connector, or another EVSE
whose charge rate TWCManager sets through the car. The home page (both themes)
reads this command every few seconds to keep itself current.

## Format of request

An example of how to call this function via cURL is:

```
curl -X GET http://192.168.1.1:8080/api/getChargingPoints
```

## Format of response

```json
{
    "fingerprint": "b92e9b9e6166",
    "now": 1794195356.72,
    "points": [
        {
            "id": "Point-Gen2TWC-4142",
            "controllers": ["Gen2TWCs"],
            "hasTWC": true,
            "vin": "5YJ3E1EA1KF000001",
            "name": "Model 3",
            "anonymous": false,
            "appearance": {},
            "ampsOffered": 16.0,
            "ampsDrawn": 15.8,
            "volts": [240.0, 0.0, 0.0],
            "minAmps": 12.0,
            "maxAmps": 80.0,
            "soc": 64,
            "chargeLimit": 80,
            "gen2": {
                "twcid": "4142",
                "state": 1,
                "version": 2,
                "lifetimekWh": 1520,
                "maxAmps": 80.0,
                "lastHeardAt": 1794195356.35,
                "currentVIN": "5YJ3E1EA1KF000001",
                "lastVIN": "5YJ3E1EA1KF000001",
                "chargingSince": 1794191756.0
            },
            "gen3": null
        },
        {
            "id": "Point-anon:Gen3TWC-192.168.1.50",
            "controllers": ["Gen3TWCs"],
            "hasTWC": true,
            "vin": null,
            "name": "",
            "anonymous": true,
            "appearance": {},
            "ampsOffered": 8.0,
            "ampsDrawn": 7.9,
            "volts": [240.0, 0.0, 0.0],
            "minAmps": 6.0,
            "maxAmps": 48.0,
            "soc": null,
            "chargeLimit": null,
            "gen2": null,
            "gen3": {
                "id": "Gen3TWC-192.168.1.50",
                "reachable": true,
                "state": 11,
                "vehicleConnected": true,
                "sessionEnergyWh": 2350.5
            }
        }
    ],
    "total": {
        "ampsOffered": 24.0,
        "ampsDrawn": 23.7,
        "maxAmps": 128.0,
        "lifetimekWh": 1520,
        "carsCharging": 2
    }
}
```

Times are in seconds since the epoch (Unix time).

* `fingerprint` changes when the page's layout would (for example, after a
  restart). Clients can ignore it.
* `now` is the server's time when the response was made. Work out how long ago
  something happened from `now`, not from the client's clock.
* `points` lists each charging point:
    * `id` names the charging point.
    * `controllers` says what TWCManager controls it through: `Gen2TWCs`,
      `Gen3TWCs`, or `VehicleEndpoints` (the car itself sets the rate).
    * `hasTWC` is true when a Gen2 or Gen3 TWC is part of the point, and false
      for a point known only through the car.
    * `vin`, `name`, `anonymous` and `appearance` describe the car on the point,
      if known: its VIN (`null` when not known), its name, whether it is a car
      known only by the TWC it is on, and its looks (model, paint, wheels) for
      drawing it.
    * `ampsOffered` and `ampsDrawn` are the amps TWCManager offers the point
      and the amps the car is drawing.
    * `volts` is the voltage on each phase.
    * `minAmps` and `maxAmps` are the least and most the point can be offered.
    * `soc` and `chargeLimit` are the car's battery level and charge limit in
      percent, `null` when not known.
    * `gen2` is `null` unless the point is a Gen2 TWC. Then it gives the TWC's
      ID, its reported state and protocol version, its lifetime kWh and maximum
      amps, when it was last heard from, the VIN it reports now and the last
      one it reported, and when the current charge started (`null` when not
      charging).
    * `gen3` is `null` unless the point is a Gen3 TWC. Then it gives the TWC's
      ID, whether its last status read succeeded, and what that read said: the
      TWC's state as it reports it, whether a car is connected, and the energy
      delivered in the current session in Wh. Each of these is `null` when the
      TWC has not reported it, and all three are `null` while its last status
      read failed.
* `total` adds up every listed point: `ampsOffered`, `ampsDrawn` and `maxAmps`
  (a Gen2 TWC's own maximum amps for a Gen2 point). `lifetimekWh` sums the
  Gen2 TWCs only, as only they report it. `carsCharging` is the number of listed
  points drawing current now.
