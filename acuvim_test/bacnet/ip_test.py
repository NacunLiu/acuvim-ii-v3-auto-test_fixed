# Description: BACnet/IP connection test for Acuvim II v3, using bacpypes3.
#
# Replaces the old YABE-screenshot approach for the IP path: this talks real
# BACnet/IP, so pass/fail is deterministic instead of an image comparison.
#
# Flow:
#   1. Stand up a tiny local BACnet/IP application.
#   2. Send a (optionally targeted) Who-Is to the meter's IP.
#   3. On I-Am, read the device object's object-name as a round-trip check.
#
# The meter must have BACnet/IP enabled on its Ethernet (AXM-WEB2) module and be
# reachable at `target_ip`. Results are logged; run_bacnet_ip_test returns
# (passed: bool, detail: str).

import asyncio
import logging

from bacpypes3.app import Application
from bacpypes3.local.device import DeviceObject
from bacpypes3.local.networkport import NetworkPortObject
from bacpypes3.pdu import Address

logger = logging.getLogger(__name__)

# BACnet/UDP default port. Override via run_bacnet_ip_test(port=...) if needed.
BACNET_PORT = 47808


def _build_app(local_addr):
    """Create a minimal BACnet/IP client application bound to local_addr.

    local_addr is the PC's NIC address on the meter's subnet, e.g.
    '192.168.61.10/24'. The CIDR prefix is required by bacpypes3.
    """
    device = DeviceObject(
        objectIdentifier='device,599',
        objectName='AcuTestClient',
        maxApduLengthAccepted=1024,
        segmentationSupported='segmentedBoth',
        vendorIdentifier=555,
    )
    network_port = NetworkPortObject(
        f'{local_addr}:{BACNET_PORT}',
        objectIdentifier='network-port,1',
        objectName='NP-1',
    )
    return Application.from_object_list([device, network_port])


async def run_bacnet_ip_test(target_ip, local_addr, device_instance=None, timeout=5):
    """Verify BACnet/IP communication with the meter.

    Args:
        target_ip: meter IP, e.g. '192.168.61.42'.
        local_addr: PC NIC address with CIDR, e.g. '192.168.61.10/24'.
        device_instance: expected BACnet device instance; if given, the Who-Is
            is scoped to it and the result is asserted to match.
        timeout: seconds to wait for the I-Am.

    Returns:
        (passed: bool, detail: str)
    """
    app = _build_app(local_addr)
    try:
        addr = Address(target_ip)
        logger.info(f'BACnet/IP Who-Is -> {target_ip} (instance={device_instance})')
        if device_instance is not None:
            i_ams = await app.who_is(device_instance, device_instance, addr, timeout)
        else:
            i_ams = await app.who_is(address=addr, timeout=timeout)

        if not i_ams:
            return False, f'No I-Am received from {target_ip}'

        iam = i_ams[0]
        found = iam.iAmDeviceIdentifier  # ('device', instance)
        found_instance = found[1]
        if device_instance is not None and found_instance != device_instance:
            return False, f'Device instance mismatch: expected {device_instance}, got {found_instance}'

        # Round-trip ReadProperty to confirm we can actually talk to it.
        name = await app.read_property(addr, found, 'object-name')
        return True, f'device instance {found_instance}, object-name="{name}"'

    except Exception as e:
        return False, f'BACnet/IP error: {e}'
    finally:
        app.close()


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO)
    # Usage: python bacnet_ip.py <target_ip> <local_addr/cidr> [device_instance]
    target = sys.argv[1] if len(sys.argv) > 1 else '192.168.61.42'
    local = sys.argv[2] if len(sys.argv) > 2 else '0.0.0.0/24'
    inst = int(sys.argv[3]) if len(sys.argv) > 3 else None
    ok, detail = asyncio.run(run_bacnet_ip_test(target, local, inst))
    print('PASS' if ok else 'FAIL', '-', detail)
