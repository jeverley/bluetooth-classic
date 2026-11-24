"""Tests for looking up RFCOMM channels with sdptool."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from bluetooth_classic import find_rfcomm_channel

ADDRESS = "00:11:22:33:44:55"

ANSWER = b"""Searching for SP on 00:11:22:33:44:55 ...
Service Name: Serial Port
Service RecHandle: 0x10003
Service Class ID List:
  "Serial Port" (0x1101)
Protocol Descriptor List:
  "L2CAP" (0x0100)
  "RFCOMM" (0x0003)
    Channel: 3
"""


class _Process:
    def __init__(self, stdout: bytes = b"", hang: bool = False) -> None:
        self.stdout = stdout
        self.hang = hang
        self.killed = False
        self.returncode: int | None = None

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.hang:
            await asyncio.sleep(10)
        self.returncode = 0
        return self.stdout, b""

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.returncode = -9
        return -9


def _spawn(process: _Process, calls: list[tuple[Any, ...]]) -> Any:
    async def spawn(*args: Any, **kwargs: Any) -> _Process:
        calls.append(args)
        return process

    return spawn


async def test_finds_the_channel() -> None:
    calls: list[tuple[Any, ...]] = []
    with patch("asyncio.create_subprocess_exec", _spawn(_Process(ANSWER), calls)):
        assert await find_rfcomm_channel("00-11-22-33-44-55") == 3
    assert calls == [("sdptool", "search", "--bdaddr", ADDRESS, "SP")]


async def test_no_such_service() -> None:
    calls: list[tuple[Any, ...]] = []
    with patch("asyncio.create_subprocess_exec", _spawn(_Process(b""), calls)):
        assert await find_rfcomm_channel(ADDRESS, "0x1234") is None
    assert calls[0][-1] == "0x1234"


async def test_sdptool_missing() -> None:
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        assert await find_rfcomm_channel(ADDRESS) is None


async def test_sdptool_times_out() -> None:
    process = _Process(hang=True)
    with patch("asyncio.create_subprocess_exec", _spawn(process, [])):
        assert await find_rfcomm_channel(ADDRESS, timeout=0.05) is None
    assert process.killed


async def test_bad_address() -> None:
    with pytest.raises(ValueError):
        await find_rfcomm_channel("not an address")


async def test_cancelling_the_lookup_stops_sdptool() -> None:
    process = _Process(hang=True)
    with patch("asyncio.create_subprocess_exec", _spawn(process, [])):
        task = asyncio.ensure_future(find_rfcomm_channel(ADDRESS))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert process.killed
