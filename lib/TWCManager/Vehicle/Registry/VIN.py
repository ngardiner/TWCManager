import re

WELL_FORMED = re.compile(r"[A-HJ-NPR-Z0-9]{17}")


def isWellFormedVIN(vin: str) -> bool:
    return bool(vin) and WELL_FORMED.fullmatch(vin.upper()) is not None
