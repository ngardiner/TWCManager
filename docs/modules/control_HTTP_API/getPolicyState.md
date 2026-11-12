# getPolicyState API Command

## Introduction

The getPolicyState API command returns the active policy and, for every
policy, the current value of each side of each of its conditions and whether
each condition and group of conditions is true now. The Policy page reads this
command every few seconds to keep itself current.

## Format of request

An example of how to call this function via cURL is:

```
curl -X GET http://192.168.1.1:8080/api/getPolicyState
```

## Format of response

```json
{
    "fingerprint": "4bd3391c0062",
    "now": 1794195366.74,
    "active": "Non Scheduled Charging",
    "policies": [
        {
            "name": "Scheduled Charging",
            "nodes": {
                "g-0": {
                    "match": 0,
                    "value": 1,
                    "truth": false
                },
                "g": {
                    "truth": false
                }
            }
        },
        {
            "name": "Non Scheduled Charging",
            "nodes": {
                "g-0": {
                    "match": 0,
                    "value": 3,
                    "truth": true
                },
                "g": {
                    "truth": true
                }
            }
        }
    ]
}
```

Times are in seconds since the epoch (Unix time).

* `fingerprint` changes when the page's layout would (for example, when the
  list of policies changes). Clients can ignore it.
* `now` is the server's time when the response was made.
* `active` is the name of the policy in effect now.
* `policies` lists every policy, in the order they are checked, with its `name`
  and `nodes`, the state of each of its conditions and groups of conditions:
    * `g` is the policy's whole set of conditions, all of which must be true;
      `g-0`, `g-1` and so on are its conditions in order. A condition that is
      itself a group of conditions has the group's ID followed by `-0`, `-1`
      and so on for its members (for example `g-2-0`); groups alternate
      between all-must-be-true and any-may-be-true at each level.
    * For a condition, `match` and `value` are the current values of its two
      sides, and `truth` whether it holds now.
    * For a group, `truth` is whether the group holds now.
