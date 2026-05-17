"""Bridge between Python m4s_parser and the C aria_client binary.

Exports parsed samples to the binary format expected by aria_client,
and imports decrypted results back.
"""
from __future__ import annotations

import struct
import subprocess
import tempfile
import os
from typing import List, Optional

from .aria_rpc import Sample

MAGIC = b"SAMP"


def export_samples(
    samples: List[Sample],
    keys: list[str],
    path: str,
) -> None:
    """Write samples + keys to binary file for C aria_client."""
    with open(path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<II", len(samples), len(keys)))
        for k in keys:
            kb = k.encode("utf-8")
            f.write(struct.pack("B", len(kb)))
            f.write(kb)
        for s in samples:
            f.write(struct.pack("<II", s.desc_index, len(s.data)))
            f.write(s.data)


def import_results(path: str, expected_count: int) -> List[bytes]:
    """Read decrypted samples from C aria_client output."""
    results = []
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic != MAGIC:
            raise ValueError(f"bad magic: {magic!r}")
        num = struct.unpack("<I", f.read(4))[0]
        if num != expected_count:
            raise ValueError(f"expected {expected_count} samples, got {num}")
        for _ in range(num):
            length = struct.unpack("<I", f.read(4))[0]
            data = f.read(length)
            if len(data) != length:
                raise ValueError("truncated output")
            results.append(data)
    return results


def decrypt_via_native(
    samples: List[Sample],
    keys: list[str],
    track_id: str,
    *,
    host: str = "127.0.0.1",
    port: int = 10020,
    aria_client_bin: str = "aria_client",
    pipeline_depth: int = 64,
) -> List[bytes]:
    """Decrypt samples using the C aria_client binary.

    Falls back to Python if the binary is not found.
    """
    if not os.path.isfile(aria_client_bin):
        from . import aria_rpc
        return aria_rpc.decrypt_samples_pipelined(
            samples, keys, track_id, host=host, port=port)

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fin:
        in_path = fin.name
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fout:
        out_path = fout.name

    try:
        export_samples(samples, keys, in_path)
        cmd = [
            aria_client_bin,
            "-h", host,
            "-p", str(port),
            "-t", track_id,
            "-i", in_path,
            "-o", out_path,
            "-P", str(pipeline_depth),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(f"aria_client failed: {proc.stderr}")
        return import_results(out_path, len(samples))
    finally:
        try:
            os.unlink(in_path)
        except OSError:
            pass
        try:
            os.unlink(out_path)
        except OSError:
            pass
