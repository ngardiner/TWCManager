# getPricing API Command

## Introduction

The getPricing API command returns the current import and export electricity
price from each loaded Pricing module (see `docs/modules/Pricing_*.md`). If no
Pricing module is configured, the module list is empty.

## Format of request

The getPricing API command is not accompanied by any payload. You should send a
blank payload when requesting this command.

An example of how to call this function via cURL is:

```
curl -X GET -d "" http://192.168.1.1:8080/api/getPricing
```

## Format of response

```json
{
    "modules": {
        "AmberPricing": {
            "importPrice": 0.32,
            "exportPrice": 0.10,
            "currency": "$"
        }
    },
    "currency": "$"
}
```
