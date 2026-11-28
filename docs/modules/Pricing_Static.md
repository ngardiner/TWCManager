# Static Pricing Module

## Introduction

The Static pricing module is for users on a fixed-rate electricity tariff who
want their charging policy to be able to reference a price. It performs no
network access; it simply reports the import and export prices you configure.

Prices are available to charging policy as `modules.StaticPricing.importPrice`
and `modules.StaticPricing.exportPrice`; see [Pricing Modules](Pricing.md).

### Status

| Detail          | Value                          |
| --------------- | ------------------------------ |
| **Module Name** | Static                         |
| **Module Type** | Pricing                        |
| **Features**    | Import price, Export price     |
| **Status**      | Implemented, Tested            |

## Configuration

| Parameter      | Value |
| -------------- | ----- |
| enabled        | *required* Boolean, `true` or `false`. |
| currency       | *optional* Currency symbol for display (default `$`). |
| peak.import    | *optional* Fixed import price in currency per kWh. |
| peak.export    | *optional* Fixed export (feed-in) price in currency per kWh. |

### JSON Configuration Example

```
"pricing": {
  "Static": {
    "enabled": true,
    "currency": "$",
    "peak": { "import": 0.30, "export": 0.10 }
  }
}
```
