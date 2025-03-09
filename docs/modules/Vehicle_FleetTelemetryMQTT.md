# Tesla Fleet Telemetry Vehicle Integration via MQTT

## Introduction

Tesla <a href="https://developer.tesla.com/docs/fleet-api/fleet-telemetry">Fleet Telemetry</a> is the Tesla preferred way of receiving data from vehicles. It replaces polling the `vehicle_data` API endpoint for all Tesla's except pre-2021 Model S and X cars. It is faster and a lot <a href="https://developer.tesla.com">cheaper</a> than using the API.

The purpose of this integration is to allow users of TWCManager to receive real-time status data from their cars without constantly polling the Tesla Fleet API. If the telemetry data stops for more than 1 hour (for example during a long sleep) TWCManager will fall back to polling the `vehicle_data` API endpoint. When the telemetry stream is restored it will take over again. This means the API might still used once per long sleep/wake cycle.

## Requirements

You need to set up a Tesla Fleet Telemetry server with the MQTT data store configured. TWCManager will pick up the data from MQTT server. There are many guides on how to this but simply following the <a href="https://developer.tesla.com/docs/fleet-api/fleet-telemetry">instructions</a> from Tesla should work. These start with setting up a <a href="https://github.com/teslamotors/vehicle-command">vehicle-command</a> HTTP proxy. You need to do this only once. Once the key is paired with you vehicles you can reuse it to configure the fleet-telemetry config as well.

When the vehicle-command HTTP proxy is working, configure the appropriate settings in the TWCManager `config.json`:

```
        "teslaApiClientID": "client-id-of-registered-app",
        "teslaProxy": "https://localhost:4443",
        "teslaProxyCert": "/path/to/public_key.pem",
```

## Integrations

### Tesla Fleet Telemetry

The Tesla Fleet Telemetry server reference implementation comes with various data stores. Since TWCManager already uses MQTT for various integrations, using the MQTT data store was easy to implement.

#### Docker Configuration

In addition to the `docker-compose.json` method documented by Tesla a simple `docker create` or `docker run` command can be used to run the Tesla Fleet Telemetry server with the docker image from <a href="https://hub.docker.com/r/tesla/fleet-telemetry">tesla/fleet-telemetry</a> :

```
docker create \
  --name fleet-telemetry \
  --restart always \
  -p 4433:4433 \
  -v /etc/fleet-telemetry:/etc/fleet-telemetry \
  tesla/fleet-telemetry
```

#### Fleet Telemetry Server Configuration

Below is a working example configuration to place in `/etc/fleet-telemetry/config.json`

Some notes:

* Using a self-signed CA works just fine. Check out <a href="https://easy-rsa.readthedocs.io/en/latest/">easy-rsa</a> for a simple way to achieve this. Just make sure the CN of your server certificate matches the `hostname` in the `config.json`.
* You can run the server on any TCP port you like
* If your Telsa connects to your local Wifi network at home, it can stream the telemetry data directly to a server in your local network
* If you want to use the fleet telemetry during drivers, make sure the telemetry server TCP port is reachable from the public internet
* The MQTT setting `retained` is used so TWCManager can see which vehicles are sending telemetry data even if they are currently asleep 
* The Tesla Fleet Telemetry server reference implementation is secured with client TLS certificates. This means only Tesla cars can connect to it. Random internet bots probing your server IP and port will not get past this protection.


```
{
  "host": "0.0.0.0",
  "hostname": "telemetry.example.com",
  "port": 4433,
  "log_level": "info",
  "json_log_enable": true,
  "namespace": "tesla_telemetry",
  "reliable_ack": false,
  "monitoring": {
    "prometheus_metrics_port": 9090,
    "profiler_port": 4269,
    "profiling_path": "/tmp/trace.out"
  },
  "rate_limit": {
    "enabled": true,
    "message_interval_time": 30,
    "message_limit": 1000
  },
  "tls": {
    "server_cert": "/etc/fleet-telemetry/selfsigned-cert.pem",
    "server_key": "/etc/fleet-telemetry/selfsigned-key.pem"
  },
  "mqtt": {
    "broker": "192.168.1.2:1883",
    "client_id": "fleet-telemetry",
    "username": "mqttuser",
    "password": "mqttuser",
    "topic_base": "telemetry",
    "qos": 1,
    "retained": true,
    "connect_timeout_ms": 10000,
    "publish_timeout_ms": 2500,
    "disconnect_timeout_ms": 250,
    "connect_retry_interval_ms": 10000,
    "keep_alive_seconds": 30
  },
  "records": {
    "alerts": [
      "mqtt",
      "logger"
    ],
    "errors": [
      "mqtt",
      "logger"
    ],
    "V": [
      "mqtt",
      "logger"
    ],
    "connectivity": [
      "mqtt",
      "logger"
    ]
  }
}
```

#### Fleet Telemetry Server Registraton
Once your Telsa Fleet Telemetry Server is running you need to register it with your car(s). You need a working Tesla vehicle-command HTTP proxy for this. 

TWCManager does not require many data points to function. All data is only sent to the telemetry server when it changes but never more often than the configured `interval_seconds`. You can add more <a href="https://developer.tesla.com/docs/fleet-api/fleet-telemetry/available-data">available data</a> from your car if you need it for other purposes.

Save the config below to `fleet-telemetry-config.json`

* Replace `TESLA_VIN1` and `TESLA_VIN2` with the VIN(s) of your car(s)
* Replace `hostname` with the hostname of your server (should be resolvable in DNS!)
* Replace `port` with the TCP port where your server is reachable either inside your local network or on the internet (or preferably both)
* Replace the certificate in `"ca"` with the certificate of the CA you used. Either self-signed or commercial.

```
{
    "vins": [
        "TESLA_VIN1",
        "TESLA_VIN2"
    ],
    "config": {
        "hostname": "telemetry.example.com",
        "port": 4433,
        "ca": "-----BEGIN CERTIFICATE-----\nMIICETCCAZegAwIBAgIUV6uzlujiD32rb9vQO9jMvk6JWiIwCgYIKoZIzj0EAwIw\nHTEbMBkGA1UEAwwSTW91bnQgS25vd2xlZGdlIENBMB4XDTI1MDMwMTE2MzUyMFoX\nDTQ1MDIyNDE2MzUyMFowHTEbMBkGA1UEAwwSTW91bnQgS25vd2xlZGdlIENBMHYw\nEAYHKoZIzj0CAQYFK4EEACIDYgAEEfUrjGY1irTQfhw4pOINzOiBSaKoGZwFGo+M\nLK7qQNj1jp3dbx65tS53CtIqjDVL+Gmt9EqLAoSnzxtUGjiZi7uFgLfeUOzeP3vX\n4DxeIezfDLgU0NH4KJcDhnZM9ln6o4GXMIGUMAwGA1UdEwQFMAMBAf8wHQYDVR0O\nBBYEFBzwtrkUd6cnUaa+QMs1DHyvW1qbMFgGA1UdIwRRME+AFBzwtrkUd6cnUaa+\nQMs1DHyvW1qboSGkHzAdMRswGQYDVQQDDBJNb3VudCBLbm93bGVkZ2UgQ0GCFFer\ns5bo4g99q2/b0DvYzL5OiVoiMAsGA1UdDwQEAwIBBjAKBggqhkjOPQQDAgNoADBl\nAjEAyblL768SxxDqkNpGKHdn6aO+idhV/3j+eYdktJRJ2avqOT3rbwY8zjj5lXuv\nfFN1AjBnvcQBzIiUG5rLd4itSKl4FQkHMbCkOUNbqBlu8aT9YusMeMsfTq63KSc5\ns142lUE=\n-----END CERTIFICATE-----",
        "fields": {
            "BatteryLevel": {
                "interval_seconds": 60
            },
            "ChargeLimitSoc": {
                "interval_seconds": 60
            },
            "DetailedChargeState": {
                "interval_seconds": 60
            },
            "Gear": {
                "interval_seconds": 60
            },
            "VehicleName": {
                "interval_seconds": 60
            },
            "Location": {
                "interval_seconds": 60
            },
            "TimeToFullCharge": {
                "interval_seconds": 60
            }
        }
    }
}
```

Send this configration to the VINs inside it using the <a href="https://developer.tesla.com/docs/fleet-api/endpoints/vehicle-endpoints#fleet-telemetry-config-create">fleet_telemetry_config</a> API endpoint.

Example command:

```
curl -s -o out.json --cacert proxy.crt --header "Authorization: Bearer $TESLA_AUTH_TOKEN" -H 'Content-Type: application/json' --data @fleet-telemetry-config.json https://localhost:4443/api/1/vehicles/fleet_telemetry_config
```

#### TWCManager Configuration

```
   "vehicle": {
        "teslaFleetTelemetryMQTT": {
          "enabled": true,
          "syncTelemetry": true,
          "mqtt_host": "192.168.1.1",
          "mqtt_user": "mqttuser",
          "mqtt_pass": "mqttpass",
          "mqtt_prefix": "telemetry"
        },
```