"""Tests for the pairing agent."""

from __future__ import annotations

import pytest
from dbus_fast import DBusError

from bluetooth_classic.agent import PairingAgent

DEVICE = "/org/bluez/hci0/dev_AA"
STRANGER = "/org/bluez/hci0/dev_BB"


def call(name: str, agent: PairingAgent, *args: object) -> object:
    # dbus-fast wraps exported methods; call the originals.
    return getattr(PairingAgent, name).__wrapped__(agent, *args)


def test_agent_answers_for_its_device() -> None:
    agent = PairingAgent(DEVICE)
    assert call("RequestPinCode", agent, DEVICE) == "0000"
    assert call("RequestPasskey", agent, DEVICE) == 0
    call("RequestConfirmation", agent, DEVICE, 123456)
    call("RequestAuthorization", agent, DEVICE)
    call("AuthorizeService", agent, DEVICE, "uuid")
    call("DisplayPinCode", agent, DEVICE, "1234")
    call("DisplayPasskey", agent, DEVICE, 1234, 0)
    call("Release", agent)
    call("Cancel", agent)


def test_agent_uses_the_pin_given() -> None:
    agent = PairingAgent(DEVICE, pin="1234")
    assert call("RequestPinCode", agent, DEVICE) == "1234"
    assert call("RequestPasskey", agent, DEVICE) == 1234
    assert call("RequestPasskey", PairingAgent(DEVICE, pin="abcd"), DEVICE) == 0


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("RequestPinCode", ()),
        ("RequestPasskey", ()),
        ("RequestConfirmation", (1,)),
        ("RequestAuthorization", ()),
        ("AuthorizeService", ("uuid",)),
        ("DisplayPinCode", ("1",)),
        ("DisplayPasskey", (1, 0)),
    ],
)
def test_agent_rejects_other_devices(name: str, args: tuple[object, ...]) -> None:
    with pytest.raises(DBusError):
        call(name, PairingAgent(DEVICE), STRANGER, *args)
