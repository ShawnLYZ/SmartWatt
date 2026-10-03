"""Capture real ADC samples over serial into S1's fixture format.

This is what makes the capture-and-replay equivalence test possible, and
that test is the most valuable structural check in the project: it proves
the ADC driver is the ONLY difference between hardware and laptop, which is
the entire point of Seam 2.

A CAPTURE IS COMPLETE OR IT IS NOTHING. The device dumps exactly the rows it
acquired in one unbroken second and then prints "CAPTURE complete"; the
fixture is written only if every one of those rows arrived. A row lost to a
serial glitch does not make a shorter capture, it makes a different one --
zero-crossing windows spanning a gap that never existed -- and the
equivalence test would then compare the device against a waveform it never
saw.

It also never waits forever. The first version looped until it had enough
rows, so if opening the port reset the board and the CAPTURE command was
lost while it booted, the tool hung with no message. The port is now opened
without asserting DTR or RTS (the lines an ESP32 dev board's auto-reset
circuit listens to), given time to settle, the command is re-sent if the
board is seen rebooting, and a deadline turns silence into an error that
says what to check.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import serial

#: What main.cpp's handle_capture() prints once the rows are out.
COMPLETE = "CAPTURE complete"
#: setup()'s first line. Seeing it means the board has just rebooted.
BANNER = "SmartWatt bring-up"
#: calibration.h's kAdcMax.
ADC_MAX = 4095


class CaptureError(RuntimeError):
    """The device did not deliver a complete, intact capture."""


class Port(Protocol):
    def write(self, data: bytes) -> object: ...
    def readline(self) -> bytes: ...


def parse_row(line: str) -> str | None:
    """A `v,i_low,i_high` row of raw counts, normalised; None if not one."""
    parts = line.split(",")
    if len(parts) != 3:
        return None
    try:
        values = [int(part) for part in parts]
    except ValueError:
        return None
    if not all(0 <= value <= ADC_MAX for value in values):
        return None
    return ",".join(str(value) for value in values)


def capture(
    port: Port,
    samples: int,
    timeout_s: float,
    clock: Callable[[], float] = time.monotonic,
) -> list[str]:
    """Sends CAPTURE and returns exactly `samples` rows, or raises.

    Lines that are not rows -- status lines already queued when the command
    arrived, the boot log, an ESP-IDF log line -- are skipped. What decides
    completeness is the count at "CAPTURE complete": a row mangled in
    transit stops parsing and leaves the count short, which is refused.
    """
    deadline = clock() + timeout_s
    port.write(b"CAPTURE\n")
    resent = False
    rows: list[str] = []

    while clock() < deadline:
        line = port.readline().decode("ascii", "ignore").strip()
        if not line:
            continue
        if line.startswith(COMPLETE):
            if len(rows) != samples:
                raise CaptureError(
                    f"the device sent {len(rows)} intact rows, not {samples}: "
                    "the capture is not contiguous. Run it again."
                )
            return rows
        row = parse_row(line)
        if row is not None:
            rows.append(row)
        elif BANNER in line and not resent:
            # The board rebooted after the command was sent, so the command
            # was lost. setup() prints the banner after Serial.begin(), so a
            # command sent now is buffered and read once loop() starts. A
            # reboot in the MIDDLE of a dump gets the same resend; the rows
            # already counted then overshoot the total, which is refused.
            port.write(b"CAPTURE\n")
            resent = True

    raise CaptureError(
        f"no '{COMPLETE}' within {timeout_s:.0f} s ({len(rows)} rows so far). "
        "Is this the board's UART port, is the serial monitor closed, and is "
        "the SmartWatt firmware running?"
    )


def open_port(name: str, baud: int) -> serial.Serial:
    """Opens `name` without asserting DTR or RTS.

    pyserial asserts both on open by default, and an ESP32 dev board's
    auto-reset circuit turns DTR/RTS transitions into a reset (or a drop
    into the bootloader). Setting both false before open() is the most
    software can do; if the board still resets, capture() re-sends.
    """
    port = serial.Serial()
    port.port = name
    port.baudrate = baud
    port.timeout = 0.5
    port.dtr = False
    port.rts = False
    port.open()
    return port


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", required=True, help="e.g. COM5")
    # 115200, not the 921600 the plan first quoted: platformio.ini's
    # monitor_speed and main.cpp's Serial.begin() are both 115200, and a
    # capture tool that defaults to a baud the device never runs at cannot
    # work out of the box. --baud remains available to override on either
    # side, together, if that ever changes. At 115200 a 4000-row capture of
    # ~15-byte lines is roughly 6 seconds -- CAPTURE itself is worth
    # spending that on, since it drains a buffer the device already
    # acquired at the true 4 kHz sample rate; see main.cpp's CAPTURE
    # handler.
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--samples", type=int, default=4000)
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--settle", type=float, default=2.0,
        help="seconds to wait after opening the port, so a board that reset "
             "on open has booted before CAPTURE is sent",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0,
        help="seconds to wait for the whole capture before giving up",
    )
    args = parser.parse_args(argv)

    try:
        with open_port(args.port, args.baud) as port:
            time.sleep(args.settle)
            port.reset_input_buffer()
            rows = capture(port, args.samples, args.timeout)
    except (CaptureError, serial.SerialException) as error:
        print(f"capture failed: {error}", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # LF on every platform, like the other fixtures: .gitattributes marks
    # them -text because the C side reads them byte for byte, and on Windows
    # write_text would otherwise translate every newline to CRLF.
    out.write_text(
        "v,i_low,i_high\n" + "\n".join(rows) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"wrote {len(rows)} samples to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
