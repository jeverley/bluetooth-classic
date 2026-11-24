"""Tests for BluetoothClassic against a fake BlueZ."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from dbus_fast import Variant

from bluetooth_classic import (
    BluetoothClassic,
    BluetoothClassicError,
    DeviceNotFoundError,
    NoAdapterError,
    PairingError,
    normalize_address,
)
from bluetooth_classic.client import _call
from bluetooth_classic.const import BLUEZ_SERVICE, OBJECT_MANAGER_INTERFACE

from .conftest import (
    ADAPTER,
    DEVICE,
    HCI0,
    SPEAKER,
    SPEAKER_ADDRESS,
    SPP,
    FakeBus,
    device_properties,
)


async def connect(bus: FakeBus, adapter: str | None = None) -> BluetoothClassic:
    """Return a connection opened on the fake bus, as async with would."""
    bluetooth = BluetoothClassic(adapter, bus=bus)  # type: ignore[arg-type]
    return await bluetooth.__aenter__()


def test_normalize_address() -> None:
    assert normalize_address(" 00-11-22-33-44-55 ") == SPEAKER_ADDRESS
    with pytest.raises(ValueError):
        normalize_address("not an address")


async def test_connect_picks_the_first_powered_adapter(bus: FakeBus) -> None:
    bus.objects["/org/bluez/hci0"][ADAPTER]["Powered"] = Variant("b", False)
    bus.objects["/org/bluez/hci1"] = {ADAPTER: {"Powered": Variant("b", True)}}
    bluetooth = await connect(bus)
    assert bluetooth.adapter == "hci1"


async def test_connect_uses_the_adapter_asked_for(bus: FakeBus) -> None:
    bus.objects["/org/bluez/hci1"] = {ADAPTER: {"Powered": Variant("b", True)}}
    assert (await connect(bus, "hci1")).adapter == "hci1"
    with pytest.raises(NoAdapterError):
        await connect(bus, "hci7")


async def test_connect_without_adapters(bus: FakeBus) -> None:
    del bus.objects[HCI0]
    with pytest.raises(NoAdapterError):
        await connect(bus)


async def test_connect_opens_and_closes_its_own_bus(bus: FakeBus) -> None:
    opened = AsyncMock(return_value=bus)
    with patch("bluetooth_classic.client.MessageBus") as message_bus:
        message_bus.return_value.connect = opened
        async with BluetoothClassic() as bluetooth:
            assert bluetooth.adapter == "hci0"
    assert bus.disconnected


async def test_a_given_bus_is_left_open(bus: FakeBus) -> None:
    async with BluetoothClassic(bus=bus):  # type: ignore[arg-type]
        pass
    assert not bus.disconnected


async def test_connect_reports_an_unreachable_bus() -> None:
    with patch("bluetooth_classic.client.MessageBus") as message_bus:
        message_bus.return_value.connect = AsyncMock(side_effect=OSError("no bus"))
        with pytest.raises(BluetoothClassicError):
            async with BluetoothClassic():
                pass


async def test_devices_and_device(bus: FakeBus) -> None:
    # Known on another adapter: not listed.
    bus.objects["/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF"] = {
        DEVICE: device_properties("AA:BB:CC:DD:EE:FF", "Other")
    }
    bluetooth = await connect(bus)
    devices = await bluetooth.devices()
    assert [d.address for d in devices] == [SPEAKER_ADDRESS]
    speaker = devices[0]
    assert speaker.name == "Speaker"
    assert speaker.adapter == "hci0"
    assert speaker.device_class == 0x240414
    assert not speaker.paired
    assert await bluetooth.device("00-11-22-33-44-55") == speaker
    assert await bluetooth.device("AA:BB:CC:DD:EE:FF") is None
    assert await bluetooth.devices(service=SPP) == []


async def test_discover_returns_classic_devices_that_answered(bus: FakeBus) -> None:
    phone = f"{HCI0}/dev_AA_BB_CC_DD_EE_FF"
    beacon = f"{HCI0}/dev_11_22_33_44_55_66"
    bus.on_discovery = [(SPEAKER, {"RSSI": Variant("n", -60)})]
    bus.added_on_discovery = {
        phone: device_properties("AA:BB:CC:DD:EE:FF", "Phone", RSSI=Variant("n", -70)),
        # A Low Energy device found by someone else's scan meanwhile.
        beacon: {
            "Address": Variant("s", "11:22:33:44:55:66"),
            "RSSI": Variant("n", -50),
            "ManufacturerData": Variant("a{qv}", {}),
        },
    }
    bluetooth = await connect(bus)
    found = await bluetooth.discover(timeout=0.05)
    assert {(d.address, d.rssi) for d in found} == {
        (SPEAKER_ADDRESS, -60),
        ("AA:BB:CC:DD:EE:FF", -70),
    }
    filters = [c[2][0] for c in bus.calls if c[0] == "SetDiscoveryFilter"]
    # Classic only while scanning, then cleared.
    assert filters[0]["Transport"].value == "bredr"
    assert filters[-1] == {}
    assert bus.members().count("StartDiscovery") == 1
    assert bus.members().count("StopDiscovery") == 1
    # Nothing left listening.
    assert bus.matches == []
    assert bus.handlers == []


async def test_find_device_by_address_when_it_answers(bus: FakeBus) -> None:
    bus.on_discovery = [
        # Low Energy advertising also brings a signal strength: not an answer.
        (
            SPEAKER,
            {"RSSI": Variant("n", -40), "ManufacturerData": Variant("a{qv}", {})},
        ),
        (SPEAKER, {"RSSI": Variant("n", -60), "TxPower": Variant("n", 4)}),
    ]
    bluetooth = await connect(bus)
    device = await bluetooth.find_device_by_address("00-11-22-33-44-55", timeout=1)
    assert device is not None
    assert device.address == SPEAKER_ADDRESS
    assert device.rssi == -60
    assert bus.members().count("StopDiscovery") == 1
    assert bus.matches == []


async def test_find_device_by_address_when_it_does_not_answer(bus: FakeBus) -> None:
    bluetooth = await connect(bus)
    assert await bluetooth.find_device_by_address(SPEAKER_ADDRESS, timeout=0.05) is None
    assert bus.members().count("StopDiscovery") == 1


async def test_find_device_by_address_finds_an_unknown_device(bus: FakeBus) -> None:
    phone = f"{HCI0}/dev_AA_BB_CC_DD_EE_FF"
    bus.added_on_discovery = {
        phone: device_properties("AA:BB:CC:DD:EE:FF", "Phone", RSSI=Variant("n", -70))
    }
    bluetooth = await connect(bus)
    device = await bluetooth.find_device_by_address("AA:BB:CC:DD:EE:FF", timeout=1)
    assert device is not None
    assert device.name == "Phone"
    assert device.rssi == -70


async def test_unpair_removes_the_device(bus: FakeBus) -> None:
    bluetooth = await connect(bus)
    await bluetooth.unpair(SPEAKER_ADDRESS)
    assert ("RemoveDevice", HCI0, [SPEAKER]) in bus.calls
    assert await bluetooth.device(SPEAKER_ADDRESS) is None
    # Unknown devices are left alone.
    await bluetooth.unpair(SPEAKER_ADDRESS)
    assert bus.members().count("RemoveDevice") == 1


async def test_using_it_outside_async_with_is_refused(bus: FakeBus) -> None:
    bluetooth = BluetoothClassic(bus=bus)  # type: ignore[arg-type]
    with pytest.raises(BluetoothClassicError):
        await bluetooth.devices()


async def test_pair_registers_an_agent_for_the_pairing_only(bus: FakeBus) -> None:
    bluetooth = await connect(bus)
    device = await bluetooth.pair(SPEAKER_ADDRESS, pin="1234")
    assert device.paired
    assert device.offers(SPP)
    assert bus.agent_answers == ["1234"]
    members = bus.members()
    assert members.index("RegisterAgent") < members.index("Pair")
    assert members.index("Pair") < members.index("UnregisterAgent")
    assert bus.exported == {}


async def test_pair_skips_a_paired_device(bus: FakeBus) -> None:
    bus.objects[SPEAKER][DEVICE]["Paired"] = Variant("b", True)
    bluetooth = await connect(bus)
    await bluetooth.pair(SPEAKER_ADDRESS)
    assert "Pair" not in bus.members()


async def test_pair_failure(bus: FakeBus) -> None:
    bus.pair_outcome = "org.bluez.Error.AuthenticationFailed"
    bluetooth = await connect(bus)
    with pytest.raises(PairingError):
        await bluetooth.pair(SPEAKER_ADDRESS)
    assert "UnregisterAgent" in bus.members()
    assert bus.exported == {}


async def test_pair_when_bluez_says_it_is_already_paired(bus: FakeBus) -> None:
    bus.pair_outcome = "org.bluez.Error.AlreadyExists"
    bluetooth = await connect(bus)
    await bluetooth.pair(SPEAKER_ADDRESS)


async def test_pair_times_out_and_cancels(bus: FakeBus) -> None:
    bus.pair_outcome = "hang"
    bluetooth = await connect(bus)
    with pytest.raises(PairingError):
        await bluetooth.pair(SPEAKER_ADDRESS, timeout=0.05)
    assert "CancelPairing" in bus.members()
    assert bus.exported == {}


async def test_pair_an_unknown_device(bus: FakeBus) -> None:
    bluetooth = await connect(bus)
    with pytest.raises(DeviceNotFoundError):
        await bluetooth.pair("AA:BB:CC:DD:EE:FF")


async def test_trust(bus: FakeBus) -> None:
    bluetooth = await connect(bus)
    device = await bluetooth.trust(SPEAKER_ADDRESS)
    assert device.trusted
    await bluetooth.trust(SPEAKER_ADDRESS)
    assert bus.members().count("Set") == 1


async def test_discover_leaves_out_devices_without_a_device_class(
    bus: FakeBus,
) -> None:
    # A Low Energy device whose only change is a signal strength looks like
    # an inquiry answer, but has no device class.
    beacon = f"{HCI0}/dev_11_22_33_44_55_66"
    bus.objects[beacon] = {
        DEVICE: {"Address": Variant("s", "11:22:33:44:55:66")},
    }
    bus.on_discovery = [
        (SPEAKER, {"RSSI": Variant("n", -60)}),
        (beacon, {"RSSI": Variant("n", -50)}),
    ]
    bluetooth = await connect(bus)
    found = await bluetooth.discover(timeout=0.05)
    assert [d.address for d in found] == [SPEAKER_ADDRESS]


async def test_scans_report_a_powered_off_adapter(bus: FakeBus) -> None:
    bus.errors["StartDiscovery"] = "org.bluez.Error.NotReady"
    bluetooth = await connect(bus)
    with pytest.raises(NoAdapterError):
        await bluetooth.discover(timeout=0.05)
    with pytest.raises(NoAdapterError):
        await bluetooth.find_device_by_address(SPEAKER_ADDRESS, timeout=0.05)
    # Nothing left behind: the filter is cleared and nothing listens.
    filters = [c[2][0] for c in bus.calls if c[0] == "SetDiscoveryFilter"]
    assert filters[-1] == {}
    assert bus.matches == []
    assert bus.handlers == []


async def test_pair_reports_a_powered_off_adapter(bus: FakeBus) -> None:
    bus.pair_outcome = "org.bluez.Error.NotReady"
    bluetooth = await connect(bus)
    with pytest.raises(NoAdapterError):
        await bluetooth.pair(SPEAKER_ADDRESS)
    assert bus.exported == {}


async def test_an_unanswered_call_times_out(bus: FakeBus) -> None:
    bus.unanswered.add("GetManagedObjects")
    with pytest.raises(BluetoothClassicError, match="no answer"):
        await _call(
            bus,  # type: ignore[arg-type]
            BLUEZ_SERVICE,
            "/",
            OBJECT_MANAGER_INTERFACE,
            "GetManagedObjects",
            timeout=0.05,
        )


async def test_pairing_is_not_cut_short_by_the_call_timeout(bus: FakeBus) -> None:
    bluetooth = await connect(bus)
    with patch("bluetooth_classic.client._call", wraps=_call) as call:
        await bluetooth.pair(SPEAKER_ADDRESS)
    pair = next(c for c in call.call_args_list if c.args[4] == "Pair")
    assert pair.kwargs["timeout"] is None
