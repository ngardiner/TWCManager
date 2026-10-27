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


def vehicleRecordKey(vehicles, vin):
    # settings["Vehicles"] keys may be in any case (older releases stored form
    # VINs as typed). The stored key for vin, else None. The caller holds
    # settingsLock.
    if vin in vehicles:
        return vin
    upper = vin.upper()
    return next(
        (k for k in vehicles if isinstance(k, str) and k.upper() == upper), None
    )


def storedBleStatus(vehicles, vin):
    # The persisted pairing state of vin's record: {"status", "since"}, status
    # "unpaired" when none is stored. The caller holds settingsLock.
    rec = vehicles.get(vehicleRecordKey(vehicles, vin))
    ble = rec.get("ble") if isinstance(rec, dict) else None
    if isinstance(ble, dict) and ble.get("status"):
        return {"status": ble["status"], "since": ble.get("since")}
    return {"status": "unpaired", "since": None}


# ISO 3779: letters transliterate to digits (no I, O or Q), each position has
# a weight, and position 9 is the weighted sum mod 11 (X for 10)
_TRANSLIT = str.maketrans("ABCDEFGHJKLMNPRSTUVWXYZ", "12345678123457923456789")
_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)


def vinCheckDigitValid(vin: str) -> bool:
    if len(vin) != 17:
        return False
    vin = vin.upper()
    digits = vin.translate(_TRANSLIT)
    if not (digits.isascii() and digits.isdigit()):
        return False
    total = sum(int(d) * w for d, w in zip(digits, _WEIGHTS))
    return vin[8] == ("X" if total % 11 == 10 else str(total % 11))
