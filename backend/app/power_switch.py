"""GPIO power switch for the radio's DC supply.

Drives a relay / MOSFET on a configurable BCM pin via gpiozero (lgpio backend,
Raspberry Pi 5 compatible). The radio's serial interface cannot power the rig
on/off, so remote power is done by switching its 12 V line.

The switch degrades gracefully: if no pin is configured or gpiozero/lgpio are
unavailable, ``available`` is False and the API reports the reason instead of
crashing.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("tmv71")

# The radio needs its DC line to drop properly before it comes back up: power
# re-applied a fraction of a second after it was cut leaves the rig's MCU hung
# (lit but deaf on CAT), and only another, slower cycle recovers it.
MIN_OFF_S = 3.0


class PowerSwitch:
    def __init__(self, pin: int | None = None, active_high: bool = True):
        self.active_high = active_high
        self.pin: int | None = None
        self.error: str | None = None
        self._dev = None
        self._off_at: float | None = None
        self.configure(pin)

    def configure(self, pin: int | None) -> None:
        """(Re)bind to a BCM pin. None disables the switch."""
        self.close()
        self.pin = pin
        self.error = None
        if pin is None:
            return
        try:
            from gpiozero import OutputDevice
            # initial_value=None keeps the pin in its current state, so
            # (re)configuring or a backend restart never toggles the radio.
            self._dev = OutputDevice(pin, active_high=self.active_high,
                                     initial_value=None)
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self._dev = None
            log.error("GPIO power switch init failed (pin %s): %s", pin, exc)

    @property
    def available(self) -> bool:
        return self._dev is not None

    @property
    def state(self) -> bool | None:
        """True=on, False=off, None=unknown/unavailable."""
        if not self._dev:
            return None
        try:
            return bool(self._dev.value)
        except Exception:  # noqa: BLE001
            return None

    def off_remaining(self) -> float:
        """Seconds the radio still has to stay dark before powering it back on."""
        if self._off_at is None:
            return 0.0
        return max(0.0, MIN_OFF_S - (time.monotonic() - self._off_at))

    def set(self, on: bool) -> None:
        if not self._dev:
            raise RuntimeError(self.error or "GPIO power switch not configured")
        if on:
            self._off_at = None
            self._dev.on()
        else:
            self._off_at = time.monotonic()
            self._dev.off()

    def close(self) -> None:
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:  # noqa: BLE001
                pass
        self._dev = None

    def status(self) -> dict:
        return {"available": self.available, "on": self.state,
                "pin": self.pin, "active_high": self.active_high,
                "error": self.error}
