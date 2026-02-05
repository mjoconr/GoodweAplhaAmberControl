#!/usr/bin/env python3
import argparse
import sys
import time

from pymodbus.client import ModbusTcpClient


def _try_calls(label, f, attempts, debug=False):
    last_exc = None
    for i, (desc, kwargs) in enumerate(attempts, start=1):
        try:
            if debug:
                print(f"[debug] {label} attempt {i}: {desc}")
            return f(**kwargs)
        except TypeError as e:
            last_exc = e
            if debug:
                print(f"[debug] {label} attempt {i} failed: {e}")
            continue
    raise last_exc


def read_holding_registers_compat(client, address, count, unit, debug=False):
    """
    Try multiple pymodbus signatures:
      - Newer: read_holding_registers(address, *, count=..., slave=...)
      - Some:  read_holding_registers(address, count=..., slave=...)
      - Older: read_holding_registers(address, count, unit=...) (rare)
      - Fallback: without unit/slave (if client is configured with default)
    """
    def call(**kwargs):
        return client.read_holding_registers(**kwargs)

    attempts = [
        ("address + count= + slave=", {"address": address, "count": count, "slave": unit}),
        ("address + count= (no slave)", {"address": address, "count": count}),
        # very old / odd variants (won't hurt to try)
        ("address + count (positional) + unit=", {"address": address, "count": count, "unit": unit}),
        ("address + count (positional style)", {"address": address, "count": count}),
    ]

    # Some versions don't accept unit= at all; _try_calls will skip it automatically.
    return _try_calls("read_holding_registers", call, attempts, debug=debug)


def write_register_compat(client, address, value, unit, debug=False):
    """
    Try multiple pymodbus signatures:
      - Newer: write_register(address, value, *, slave=...)
      - Older: write_register(address, value, unit=...)
      - Fallback: without unit/slave
    """
    def call(**kwargs):
        return client.write_register(**kwargs)

    attempts = [
        ("address + value + slave=", {"address": address, "value": value, "slave": unit}),
        ("address + value (no slave)", {"address": address, "value": value}),
        # very old / odd variants
        ("address + value + unit=", {"address": address, "value": value, "unit": unit}),
    ]

    return _try_calls("write_register", call, attempts, debug=debug)


def main() -> int:
    p = argparse.ArgumentParser(description="Disable GoodWe reg 256 power limit by setting it to 100%.")
    p.add_argument("--host", required=True, help="Inverter Modbus-TCP IP/host (e.g. 192.168.178.63)")
    p.add_argument("--port", type=int, default=502, help="Modbus-TCP port (default 502)")
    p.add_argument("--unit", type=int, default=247, help="Modbus unit-id (default 247)")
    p.add_argument("--timeout", type=float, default=3.0, help="TCP timeout seconds (default 3.0)")
    p.add_argument("--retries", type=int, default=3, help="Retries (default 3)")
    p.add_argument("--value", type=int, default=100, help="Value to write to reg 256 (default 100)")
    p.add_argument("--debug", action="store_true", help="Print which pymodbus call signature is used")
    args = p.parse_args()

    if not (0 <= args.value <= 100):
        print("[error] --value must be in range 0..100")
        return 2

    client = ModbusTcpClient(host=args.host, port=args.port, timeout=args.timeout)

    try:
        for attempt in range(1, args.retries + 1):
            if client.connect():
                break
            print(f"[warn] connect failed (attempt {attempt}/{args.retries})")
            time.sleep(0.3)
        else:
            print("[error] could not connect to Modbus-TCP server")
            return 1

        # Read current reg 256
        rr = read_holding_registers_compat(client, address=256, count=1, unit=args.unit, debug=args.debug)
        if hasattr(rr, "isError") and rr.isError():
            print(f"[warn] read reg 256 failed: {rr}")
        else:
            cur = rr.registers[0]
            print(f"[info] current reg256 = {cur}%")

        # Write reg 256
        wr = write_register_compat(client, address=256, value=args.value, unit=args.unit, debug=args.debug)
        if hasattr(wr, "isError") and wr.isError():
            print(f"[error] write reg 256 failed: {wr}")
            return 1

        # Read back
        rr2 = read_holding_registers_compat(client, address=256, count=1, unit=args.unit, debug=args.debug)
        if hasattr(rr2, "isError") and rr2.isError():
            print(f"[warn] readback reg 256 failed: {rr2}")
            return 0

        newv = rr2.registers[0]
        print(f"[ok] reg256 now = {newv}%")
        return 0

    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

