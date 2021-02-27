# aWATTar Power Pricing Module

## Introduction

This module queries the Market Price data published by aWATTar for their energy customers. Please note that this is market pricing and not retail pricing, so whilst it provides insight into the price movements, it does not represent the exact cost to user.

Currently, we only read the first price returned in the API call, which is the current market price.

aWATTar's policy is to allow up to 100 queries per day of the data service. This equates to around 4 queries per hour or one every 15 minutes. We limit the query interval to this amount.

### Status

| Detail          | Value                          |
| --------------- | ------------------------------ |
| **Module Name** | aWATTarPricing                 |
| **Module Type** | Power Pricing Module (Pricing) |
| **Features**    | Import Pricing                 |
| **Status**      | New                            |

## Configuration

The following table shows the available configuration parameters for the aWATTer pricing module.

| Parameter   | Value         |
| ----------- | ------------- |
| enabled     | *required* Boolean value, ```true``` or ```false```. Determines whether we will poll the aWATTar API for pricing. |

### JSON Configuration Example

```
"pricing": {
  "aWATTar": {
    "enabled": true,
}
```
