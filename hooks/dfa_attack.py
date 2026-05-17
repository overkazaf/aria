#!/usr/bin/env python3
"""DFA attack on <TARGET_DRM_LIB>'s white-box AES via /proc/PID/mem fault injection.

Strategy:
  1. Send the SAME encrypted sample repeatedly to aria (via TCP 10020)
  2. Record the correct decrypted output once (golden reference)
  3. For each fault attempt:
     a. Write a single random byte to a random offset in <TARGET_DRM_LIB>'s .rodata
     b. Send the same sample again, record the (faulty) decrypted output
     c. Restore the original byte
     d. If faulty output differs from golden in exactly 4 bytes → valid DFA fault
  4. Collect ~200 valid faults, then run phoenixAES to recover the round key

Target: <TARGET_DRM_LIB> .rodata segment (likely contains AES T-tables)
"""
import os
import sys
import struct
import socket
import random
import time
import json
from pathlib import Path

# ── Config ──
ARIA_HOST = "127.0.0.1"
ARIA_DECRYPT_PORT = 10020
ARIA_M3U8_PORT = 20020
TARGET_PID = None  # set via --pid
ADAM_ID = "1440841263"

# <TARGET_DRM_LIB> .rodata segment (from /proc/PID/maps)
# Will be auto-detected
RODATA_START = 0
RODATA_END = 0
RODATA_FILE_OFFSET = 0


def find_target_rodata(pid: int) -> tuple:
    """Parse /proc/PID/maps to find <TARGET_DRM_LIB> .rodata (r-- segment)."""
    with open(f"/proc/{pid}/maps") as f:
        for line in f:
            if "<TARGET_DRM_LIB>" in line and "r--p" in line:
                parts = line.split()
                addr_range = parts[0]
                offset = int(parts[2], 16)
                lo, hi = addr_range.split("-")
                return int(lo, 16), int(hi, 16), offset
    raise RuntimeError("<TARGET_DRM_LIB> .rodata not found in maps")


def read_mem(pid: int, addr: int, size: int) -> bytes:
    with open(f"/proc/{pid}/mem", "rb") as f:
        f.seek(addr)
        return f.read(size)


def write_mem(pid: int, addr: int, data: bytes):
    with open(f"/proc/{pid}/mem", "r+b") as f:
        f.seek(addr)
        f.write(data)


def get_one_decrypt(sample_data: bytes, keys: list, track_id: str) -> bytes:
    """Send one sample to aria and get decrypted output."""
    s = socket.create_connection((ARIA_HOST, ARIA_DECRYPT_PORT), timeout=30)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    # Key context setup: use first non-prefetch key (index 1)
    prefetch_key = "skd://itunes.apple.com/P000000000/s1/e1"

    # Send prefetch key context first
    tid_b = b"0"
    kuri_b = prefetch_key.encode()
    s.sendall(bytes([len(tid_b)]) + tid_b + bytes([len(kuri_b)]) + kuri_b)

    # Send a dummy sample for prefetch (16 zero bytes)
    dummy = b"\x00" * 16
    s.sendall(struct.pack("<I", len(dummy)) + dummy)
    s.recv(len(dummy))  # discard

    # Switch to real key context
    s.sendall(b"\x00\x00\x00\x00")  # end prev key sentinel
    tid_b = track_id.encode()
    real_key = keys[1] if len(keys) > 1 else keys[0]
    kuri_b = real_key.encode()
    s.sendall(bytes([len(tid_b)]) + tid_b + bytes([len(kuri_b)]) + kuri_b)

    # Send the actual sample
    s.sendall(struct.pack("<I", len(sample_data)) + sample_data)

    # Receive decrypted output
    buf = bytearray()
    while len(buf) < len(sample_data):
        chunk = s.recv(len(sample_data) - len(buf))
        if not chunk:
            break
        buf.extend(chunk)

    # End session
    s.sendall(b"\x00\x00\x00\x00\x00")
    s.close()

    return bytes(buf)


def count_diff_bytes(a: bytes, b: bytes) -> int:
    return sum(1 for x, y in zip(a, b) if x != y)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="DFA attack on <TARGET_DRM_LIB>")
    parser.add_argument("--pid", type=int, required=True, help="aria child PID")
    parser.add_argument("--faults", type=int, default=300, help="number of fault attempts")
    parser.add_argument("--out", default="/tmp/dfa_faults.json", help="output file")
    parser.add_argument("--adam-id", default=ADAM_ID)
    args = parser.parse_args()

    pid = args.pid

    # 1. Find .rodata
    rodata_start, rodata_end, rodata_foff = find_target_rodata(pid)
    rodata_size = rodata_end - rodata_start
    print(f"[*] <TARGET_DRM_LIB> .rodata: {rodata_start:#x}-{rodata_end:#x} ({rodata_size} bytes)")

    # 2. Get sample data and keys via m3u8
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from am_alac import aria_rpc, m3u8_select, m4s_parser
    import httpx

    url = aria_rpc.fetch_master_playlist(args.adam_id, port=ARIA_M3U8_PORT)
    with httpx.Client(timeout=30, follow_redirects=True) as hc:
        mtxt, _ = m3u8_select.fetch_master(url, client=hc)
    var = m3u8_select.pick_alac_variant(mtxt, url)
    parsed = m4s_parser.fetch_and_parse(var.stream_url)

    # Pick a sample with desc_index=1 (real key, not prefetch)
    target_sample = None
    for s in parsed.samples:
        if s.desc_index == 1 and len(s.data) >= 256:
            target_sample = s
            break
    if target_sample is None:
        target_sample = parsed.samples[5]

    sample_data = target_sample.data
    print(f"[*] Target sample: {len(sample_data)} bytes, desc_index={target_sample.desc_index}")

    # 3. Get golden reference (correct decryption)
    print("[*] Getting golden reference...")
    golden = get_one_decrypt(sample_data, var.keys, args.adam_id)
    print(f"[*] Golden: {len(golden)} bytes, first 16: {golden[:16].hex()}")

    # 4. DFA fault injection loop
    faults = []
    attempts = 0
    valid_4byte = 0
    valid_other = 0

    print(f"[*] Starting DFA: {args.faults} attempts on .rodata ({rodata_size} bytes)")
    t0 = time.monotonic()

    for i in range(args.faults):
        # Pick random offset in .rodata
        offset = random.randint(0, rodata_size - 1)
        addr = rodata_start + offset

        # Read original byte
        orig = read_mem(pid, addr, 1)

        # Write fault byte (random, different from original)
        fault_byte = bytes([random.randint(0, 255)])
        while fault_byte == orig:
            fault_byte = bytes([random.randint(0, 255)])

        write_mem(pid, addr, fault_byte)

        try:
            faulty = get_one_decrypt(sample_data, var.keys, args.adam_id)
        except Exception as e:
            # Restore and skip
            write_mem(pid, addr, orig)
            print(f"  [{i}] ERROR: {e}")
            continue

        # Restore original byte
        write_mem(pid, addr, orig)

        attempts += 1

        if len(faulty) != len(golden):
            continue

        diff_count = count_diff_bytes(golden, faulty)

        if diff_count == 0:
            continue  # No effect — not in AES path

        if diff_count == 4:
            valid_4byte += 1
            fault_record = {
                "offset": offset,
                "orig": orig.hex(),
                "fault": fault_byte.hex(),
                "golden_hex": golden.hex(),
                "faulty_hex": faulty.hex(),
                "diff_count": diff_count,
            }
            faults.append(fault_record)
            print(f"  [{i}] ★ 4-byte diff at .rodata+{offset:#x} "
                  f"({valid_4byte} valid / {attempts} attempts)")
        elif 1 <= diff_count <= 16:
            valid_other += 1
            fault_record = {
                "offset": offset,
                "orig": orig.hex(),
                "fault": fault_byte.hex(),
                "golden_hex": golden.hex(),
                "faulty_hex": faulty.hex(),
                "diff_count": diff_count,
            }
            faults.append(fault_record)
            if valid_other <= 5:
                print(f"  [{i}] {diff_count}-byte diff at .rodata+{offset:#x}")

    dt = time.monotonic() - t0
    print(f"\n[*] Done: {attempts} attempts in {dt:.1f}s")
    print(f"    4-byte diffs: {valid_4byte}")
    print(f"    other diffs:  {valid_other}")
    print(f"    total valid:  {len(faults)}")

    # 5. Save faults
    with open(args.out, "w") as f:
        json.dump({
            "golden_hex": golden.hex(),
            "sample_len": len(sample_data),
            "faults": faults,
            "rodata_start": hex(rodata_start),
            "rodata_size": rodata_size,
        }, f, indent=2)
    print(f"[*] Saved to {args.out}")

    if valid_4byte >= 8:
        print(f"\n[!] Enough 4-byte faults ({valid_4byte}) for phoenixAES!")
        print(f"    Run: python3 -c \"")
        print(f"import phoenixAES, json")
        print(f"d = json.load(open('{args.out}'))['faults']")
        print(f"pairs = [(bytes.fromhex(f['golden_hex']), bytes.fromhex(f['faulty_hex'])) for f in d if f['diff_count']==4]")
        print(f"phoenixAES.crack_bytes(pairs)\"")


if __name__ == "__main__":
    main()
