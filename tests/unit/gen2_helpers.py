# Shared real-heartbeat driver for Gen2 tests (Phase 3). Works on the masters
# that tests/unit/test_gen2twc_provider.make() and the reconciler setup()
# build: it stubs the modules the heartbeat touches, completes the config and
# gives the master a bytes TWCID.
from unittest.mock import Mock


def drive(master, slave, amps_actual, state=0x04, amps_max=0.0):
    config = master.config.setdefault("config", {})
    config.setdefault("fakeMaster", 1)
    config.setdefault("displayMilliseconds", False)
    slave.configConfig.setdefault("fakeMaster", 1)
    slave.configConfig.setdefault("displayMilliseconds", False)
    if not isinstance(master.TWCID, (bytes, bytearray)):
        master.TWCID = b"\x77\x77"
    stubs = getattr(master, "_driveStubs", None)
    if stubs is None:
        policy = Mock()
        policy.policyIsGreen.return_value = False
        iface = Mock()
        stubs = {"Policy": policy, "Dummy": iface}
        master._driveStubs = stubs
        previous = master.getModuleByName

        def lookup(name):
            if name in stubs:
                return stubs[name]
            try:
                return previous(name)
            except Exception:
                return None

        master.getModuleByName = lookup
        master.getModulesByType = _withInterface(master.getModulesByType, iface)
    mx, ac = int(amps_max * 100), int(amps_actual * 100)
    slave.receive_slave_heartbeat(
        bytearray([state, mx >> 8, mx & 0xFF, ac >> 8, ac & 0xFF, 0, 0])
    )
    slave.send_master_heartbeat()
    return stubs["Dummy"]


def _withInterface(previous, iface):
    def getModulesByType(kind):
        if kind == "Interface":
            return [{"name": "Dummy", "ref": iface, "type": "Interface"}]
        return previous(kind)

    return getModulesByType
