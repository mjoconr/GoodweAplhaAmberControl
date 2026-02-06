#!/usr/bin/env python3
import argparse
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple


def _u16_to_i16(u: int) -> int:
    u = int(u) & 0xFFFF
    return u - 0x10000 if u & 0x8000 else u


def _split_host_port(s: str) -> Tuple[str, int]:
    s = (s or "").strip()
    if ":" in s and not s.startswith("["):
        host, port = s.rsplit(":", 1)
        try:
            return host, int(port)
        except Exception:
            return host, 502
    return s, 502


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return int(default)
    try:
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return float(default)
    try:
        return float(str(v).strip())
    except Exception:
        return float(default)


def _try_read_input(modbus: Any, reg: int, count: int) -> Tuple[Optional[List[int]], Optional[str]]:
    try:
        regs = modbus.read_input_u16s(int(reg), int(count))
        return [int(x) for x in regs], None
    except Exception as e:
        return None, str(e)


def _try_read_holding(modbus: Any, reg: int, count: int) -> Tuple[Optional[List[int]], Optional[str]]:
    try:
        regs = modbus.read_u16s(int(reg), int(count))
        return [int(x) for x in regs], None
    except Exception as e:
        return None, str(e)


def _dt_decode(regs: List[int], base: int = 30100) -> Dict[str, Any]:
    def at(addr: int) -> int:
        i = int(addr) - int(base)
        if i < 0 or i >= len(regs):
            raise IndexError(f"addr {addr} out of range")
        return int(regs[i]) & 0xFFFF

    vpv1 = at(30103) / 10.0
    ipv1 = at(30104) / 10.0
    vpv2 = at(30105) / 10.0
    ipv2 = at(30106) / 10.0

    # optional PV3
    vpv3 = 0.0
    ipv3 = 0.0
    try:
        vpv3 = at(30107) / 10.0
        ipv3 = at(30108) / 10.0
    except Exception:
        pass

    pv_est = int(round((vpv1 * ipv1) + (vpv2 * ipv2) + (vpv3 * ipv3)))
    gen_w = _u16_to_i16(at(30128))
    temp_c = _u16_to_i16(at(30141)) / 10.0

    return {
        "vpv1_v": vpv1,
        "ipv1_a": ipv1,
        "vpv2_v": vpv2,
        "ipv2_a": ipv2,
        "vpv3_v": vpv3,
        "ipv3_a": ipv3,
        "pv_est_w": pv_est,
        "gen_w": gen_w,
        "temp_c": temp_c,
    }


def _et_decode(regs: List[int], base: int = 35100) -> Dict[str, Any]:
    def u16_at(addr: int) -> int:
        i = int(addr) - int(base)
        if i < 0 or i >= len(regs):
            raise IndexError(f"addr {addr} out of range")
        return int(regs[i]) & 0xFFFF

    def u32_at(addr: int) -> int:
        i = int(addr) - int(base)
        if i < 0 or i + 1 >= len(regs):
            raise IndexError(f"addr {addr} out of range")
        return ((int(regs[i]) & 0xFFFF) << 16) | (int(regs[i + 1]) & 0xFFFF)

    ppv1 = u32_at(35105)
    ppv2 = u32_at(35109)
    ppv3 = u32_at(35113)
    ppv4 = u32_at(35117)
    pv_est = int(max(0, ppv1) + max(0, ppv2) + max(0, ppv3) + max(0, ppv4))

    gen_w = _u16_to_i16(u16_at(35138))
    temp_c = _u16_to_i16(u16_at(35176)) / 10.0

    # active power (fallback)
    feed_w = _u16_to_i16(u16_at(35140))

    return {
        "pv_est_w": pv_est,
        "gen_w": gen_w,
        "feed_w": feed_w,
        "temp_c": temp_c,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe GoodWe Modbus register maps and show decoded runtime values")
    ap.add_argument("--host", default=os.getenv("GOODWE_HOST", ""), help="GoodWe host or host:port (default: GOODWE_HOST)")
    ap.add_argument("--port", type=int, default=0, help="Modbus TCP port (default: from host or 502)")
    ap.add_argument("--unit", type=int, default=_env_int("GOODWE_UNIT", 247), help="Modbus unit/slave (default: GOODWE_UNIT or 247)")
    ap.add_argument("--timeout", type=float, default=_env_float("MODBUS_TIMEOUT_SEC", 3.0), help="Timeout seconds")
    ap.add_argument("--retries", type=int, default=_env_int("MODBUS_RETRIES", 3), help="Retries")
    ap.add_argument("--debug", action="store_true", help="Enable verbose pymodbus compat logging")
    args = ap.parse_args()

    if not args.host:
        print("ERROR: set GOODWE_HOST or pass --host")
        return 2

    host, port = _split_host_port(args.host)
    if args.port:
        port = int(args.port)

    # Import control.py after parsing args so we can enable debug reliably.
    import control as ctl  # type: ignore

    if args.debug:
        ctl.DEBUG = True

    modbus = ctl.GoodWeModbus(host=f"{host}:{port}", unit=int(args.unit), timeout_sec=float(args.timeout), retries=int(args.retries))

    print(f"[probe] host={host}:{port} unit={args.unit} timeout={args.timeout}s retries={args.retries} debug={bool(args.debug)}")

    if not modbus.connect():
        print("[probe] ERROR: could not connect")
        return 2

    try:
        print("\n=== DT / D-NS family (GW5000-DNS) runtime map ===")
        dt_regs, dt_err = _try_read_input(modbus, 30100, 73)
        if dt_regs is None:
            print(f"  FAIL: read_input_registers 30100 count=73 -> {dt_err}")
        else:
            d = _dt_decode(dt_regs, 30100)
            print("  OK: input regs 30100..30172")
            print(f"    gen_w     (30128) = {d['gen_w']} W")
            print(f"    pv_est_w  (calc)  = {d['pv_est_w']} W  [from vpv*ipv]")
            print(f"    temp_c    (30141) = {d['temp_c']:.1f} C")
            print(f"    vpv1/ipv1 (30103/30104) = {d['vpv1_v']:.1f} V / {d['ipv1_a']:.1f} A")
            print(f"    vpv2/ipv2 (30105/30106) = {d['vpv2_v']:.1f} V / {d['ipv2_a']:.1f} A")
            if d.get("vpv3_v", 0) or d.get("ipv3_a", 0):
                print(f"    vpv3/ipv3 (30107/30108) = {d['vpv3_v']:.1f} V / {d['ipv3_a']:.1f} A")

            ap1, ap_err = _try_read_input(modbus, 30196, 1)
            if ap1 is None:
                print(f"    feed_w    (30196) = ? (not available) [{ap_err}]")
            else:
                feed_w = _u16_to_i16(ap1[0])
                print(f"    feed_w    (30196) = {feed_w} W  (signed)")

        print("\n=== ET family runtime map (for comparison) ===")
        et_regs, et_err = _try_read_input(modbus, 35100, 125)
        if et_regs is None:
            print(f"  FAIL: read_input_registers 35100 count=125 -> {et_err}")
        else:
            e = _et_decode(et_regs, 35100)
            print("  OK: input regs 35100..35224")
            print(f"    gen_w    (35138) = {e['gen_w']} W")
            print(f"    pv_est_w (35105..35118) = {e['pv_est_w']} W")
            print(f"    feed_w   (35140) = {e['feed_w']} W")
            print(f"    temp_c   (35176) = {e['temp_c']:.1f} C")

        print("\n=== Extended meter / comm block (optional) ===")
        mt_regs, mt_err = _try_read_input(modbus, 36000, 0x2D)
        if mt_regs is None:
            print(f"  FAIL: read_input_registers 36000 count=45 -> {mt_err}")
        else:
            # Based on goodwe ET map: rssi=36001, meter_comm_status=36004, active_power_total=36008
            rssi = _u16_to_i16(mt_regs[1])
            comm = mt_regs[4]
            ptotal = _u16_to_i16(mt_regs[8])
            print("  OK: input regs 36000..36044")
            print(f"    wifi/rssi (36001) = {rssi}")
            print(f"    meterOK   (36004) = {comm}")
            print(f"    feed_w    (36008) = {ptotal} W (signed)")

        print("\n=== Limiter registers (holding) ===")
        lim_regs, lim_err = _try_read_holding(modbus, 291, 3)
        if lim_regs is None:
            print(f"  FAIL: read_holding_registers 291 count=3 -> {lim_err}")
        else:
            print(f"  291(export_switch)={lim_regs[0]}  292(export_pct)={lim_regs[1]}  293(export_pct10)={lim_regs[2]}")

        act, act_err = _try_read_holding(modbus, 256, 1)
        if act is None:
            print(f"  FAIL: read_holding_registers 256 count=1 -> {act_err}")
        else:
            print(f"  256(active_pct)={act[0]}")

        print("\n=== Suggested mapping for control.py log fields ===")
        print("  gen:   DT/D-NS -> input 30128 (signed W)")
        print("  pv_est: DT/D-NS -> derived from (30103,30104,30105,30106[,30107,30108])")
        print("  feed:  DT/D-NS -> input 30196 (signed W) if present")
        print("  temp:  DT/D-NS -> input 30141 (signed /10 C)")
        print("  wifi:  optional -> input 36001 (if your firmware exposes it)")
        print("  meterOK: optional -> input 36004 (if your firmware exposes it)")

        print("\n[probe] Done.")

    finally:
        try:
            modbus.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
