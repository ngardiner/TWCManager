import re

WELL_FORMED = re.compile(r"[A-HJ-NPR-Z0-9]{17}")

# What older releases stored for a car whose VIN was not known.
PLACEHOLDER_VIN = "0" * 17


def isWellFormedVIN(vin: str) -> bool:
    return (
        bool(vin)
        and vin.isascii()
        and WELL_FORMED.fullmatch(vin.upper()) is not None
        and vin != PLACEHOLDER_VIN
    )
