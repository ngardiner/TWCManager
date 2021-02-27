# Static Power Pricing Module

## Introduction

The Static Power Pricing Module allows configuration of power prices in environments where you do not have access to an API or dynamic source of pricing data.

It currently only allows configuration of peak import and export rates.

### Status

| Detail          | Value                          |
| --------------- | ------------------------------ |
| **Module Name** | Static                         |
| **Module Type** | Power Pricing Module (Pricing) |
| **Features**    | Export, Import Pricing         |
| **Status**      | New, Work In Progress          |

## Configuration

The following table shows the available configuration parameters for the Static Pricing module.

| Parameter     | Value         |
| ------------- | ------------- |
| enabled       | *required* Boolean value, ```true``` or ```false```. Determines whether we will use this pricing module. |
| peak > export | *optional* The export (sell to grid) price of power during the peak period  |
| peak > import | *optional* The import (buy from grid) price of power during the peak period |

### JSON Configuration Example

```
"pricing": {
  "Static": {
    "enabled": true,
    "peak": {
      "import": 0.10,
      "export": 0.01
    }
}
```
