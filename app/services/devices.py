"""Receiver devices and the rules for choosing one.

Kept apart from :mod:`app.services.tsduck_service` because none of this touches
a TSDuck process: it is pure logic over what ``tslsdvb`` reported, which makes
it cheap to test and keeps device choices away from recording state.
"""

from dataclasses import dataclass


@dataclass
class DVBDevice:
    index: int
    name: str
    delivery_systems: list[str]


# DVB-T is the only delivery system verified end to end on real hardware, so it
# is the one the UI preselects whenever a receiver reports it.
VERIFIED_DELIVERY_SYSTEM = "DVB-T"


class DeviceSelectionError(ValueError):
    """Raised when the submitted receiver device or delivery system is unusable."""


def default_device(devices: list[DVBDevice]) -> DVBDevice | None:
    """Choose the device to preselect in the form.

    Exactly one device is chosen, so the rendered ``<option>`` list can never
    carry two ``selected`` attributes and leave the browser to pick the last.
    """
    for device in devices:
        if VERIFIED_DELIVERY_SYSTEM in device.delivery_systems:
            return device
    return devices[0] if devices else None


def default_delivery_system(device: DVBDevice | None) -> str:
    """Delivery system to preselect for a device."""
    if device is None or not device.delivery_systems:
        return ""
    if VERIFIED_DELIVERY_SYSTEM in device.delivery_systems:
        return VERIFIED_DELIVERY_SYSTEM
    return device.delivery_systems[0]


def available_delivery_systems(devices: list[DVBDevice]) -> list[str]:
    """Every delivery system the connected devices report, in device order.

    The form renders these once and the client filters them against the chosen
    device, so switching from the DVB-T to the DVB-C tuner exposes DVB-C
    without another round trip.
    """
    systems: list[str] = []
    for device in devices:
        for system in device.delivery_systems:
            if system not in systems:
                systems.append(system)
    return systems


def resolve_device(
    devices: list[DVBDevice],
    adapter: int,
    delivery_system: str,
) -> DVBDevice:
    """Validate the submitted device/system pair before any tuner is opened.

    ``tsp -a 1 --delivery-system DVB-T`` fails hard on the DVB-C tuner of a
    hybrid receiver, and ``tslsdvb`` indices shift when devices are plugged or
    unplugged. Both cases are rejected here with a readable message instead of
    a raw TSDuck error.
    """
    if not devices:
        raise DeviceSelectionError(
            "No receiver device found — connect a DVB receiver and reload the page"
        )

    device = next((d for d in devices if d.index == adapter), None)
    if device is None:
        available = ", ".join(str(d.index) for d in devices)
        raise DeviceSelectionError(
            f"Receiver device {adapter} not found (available: {available})"
        )

    if delivery_system not in device.delivery_systems:
        supported = ", ".join(device.delivery_systems)
        raise DeviceSelectionError(
            f"Device {device.index} ({device.name}) does not support "
            f"{delivery_system}; it reports {supported}"
        )

    return device
