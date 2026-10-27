#!/usr/bin/env python3
"""One-shot QPIGS2 diagnostic for Axpert USB. Does not publish to MQTT."""
import os
import time
import select
import crcmod.predefined

DEVICE = "/dev/hidraw0"
COMMAND = "QPIGS2"
MAX_SECONDS = 20
QUIET_SECONDS = 1.0

def read_available(fd):
    chunks = []
    while True:
        ready, _, _ = select.select([fd], [], [], 0)
        if not ready:
            break
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)

def main():
    crc_func = crcmod.predefined.mkCrcFun("xmodem")
    command_bytes = COMMAND.encode("ascii")
    crc_bytes = bytes.fromhex(f"{crc_func(command_bytes):04x}")
    first = command_bytes + crc_bytes
    second = b"\r" + b"\x00" * 7

    print(f"Opening {DEVICE} for one-shot {COMMAND} diagnostic")
    fd = os.open(DEVICE, os.O_RDWR | os.O_NONBLOCK)
    try:
        stale = read_available(fd)
        if stale:
            print(f"Discarded stale bytes before command ({len(stale)}): {stale!r}")
        else:
            print("No immediately available stale USB data before command.")

        print("Waiting 0.35 seconds before first write...")
        time.sleep(0.35)
        n1 = os.write(fd, first)
        print(f"First write: {n1} bytes: {first!r}")

        print("Waiting 0.35 seconds before carriage-return padding...")
        time.sleep(0.35)
        n2 = os.write(fd, second)
        print(f"Second write: {n2} bytes: {second!r}")

        print("Waiting 0.25 seconds before reading...")
        time.sleep(0.25)

        deadline = time.monotonic() + MAX_SECONDS
        last_data_at = None
        chunks = []
        while time.monotonic() < deadline:
            wait = 0.15
            if last_data_at is not None:
                quiet_left = QUIET_SECONDS - (time.monotonic() - last_data_at)
                if quiet_left <= 0:
                    break
                wait = min(wait, quiet_left)
            ready, _, _ = select.select([fd], [], [], wait)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                continue
            if not chunk:
                continue
            chunks.append(chunk)
            last_data_at = time.monotonic()
            print(f"USB READ CHUNK ({len(chunk)} bytes): {chunk!r}")
            if b"\r" in chunk and last_data_at is not None:
                # Continue briefly to catch any immediately trailing bytes.
                pass

        raw = b"".join(chunks)
        print(f"\nUSB RAW RESPONSE ({len(raw)} bytes): {raw!r}")
        if not raw:
            print("RESULT: No USB response received.")
        else:
            start = raw.find(b"(")
            end = raw.find(b"\r", start + 1) if start >= 0 else -1
            if start >= 0 and end >= 0:
                frame = raw[start:end + 1]
                print(f"RESULT: Complete frame candidate: {frame!r}")
                print("Check whether the frame contains the expected PV2 values.")
            else:
                print("RESULT: Bytes received, but no complete '(' ... CR frame was found.")
                print("The bytes may be delayed/stale data from another inverter command.")
    finally:
        os.close(fd)

if __name__ == "__main__":
    main()
