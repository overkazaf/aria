"""Streaming ISO BMFF parser + decryptor — zero-buffer architecture.

The standard m4s_parser loads the entire M4S into RAM (~60 MB for a Hi-Res
track) before parsing. This module does everything in a single streaming
pass: it reads box headers from the HTTP response on-the-fly, parses
metadata boxes (ftyp, moov, moof) into small buffers, and streams mdat
payloads sample-by-sample directly to the aria decrypt socket — writing
each plaintext sample to disk immediately.

Peak memory: ~200 KB (moov + moof + 1 sample), regardless of file size.

Architecture:
    HTTP stream ──→ box_header_reader ──→ branch by type:
      ftyp/moov: buffer entirely (~few KB), parse trex/alac/stsd
      moof:      buffer entirely (~few KB), parse trun → sample_sizes[]
      mdat:      for each size in sample_sizes[]:
                   read(size) from HTTP → send to aria TCP → recv plaintext
                   → write to output file at correct offset
                   → memory freed immediately

This means a 500 MB Hi-Res album track uses the same ~200 KB peak memory
as a 5 MB AAC track.
"""
from __future__ import annotations

import os
import struct
import socket
import tempfile
from dataclasses import dataclass, field
from typing import Optional, Callable, BinaryIO, List

import httpx

from .aria_rpc import Sample, PREFETCH_KEY, PREFETCH_TRACK_ID
from .m4s_parser import (
    AlacCookie, BoxLocation, TrexDefaults, TfhdHeader, TrunRun,
    iter_boxes, find_path_first, find_path_all,
    parse_trex, parse_tfhd, parse_trun,
    _extract_alac_cookie,
    _TRUN_FLAG_SAMPLE_SIZE, _TRUN_FLAG_SAMPLE_DURATION,
)

_SOCK_BUFSIZE = 256 * 1024


@dataclass
class StreamingResult:
    out_path: str
    samples_count: int
    total_bytes: int
    sample_rate: int
    bit_depth: int


def _read_exact(stream, n: int) -> bytes:
    """Read exactly n bytes from an httpx streaming response iterator."""
    buf = bytearray()
    for chunk in stream:
        buf.extend(chunk)
        if len(buf) >= n:
            break
    if len(buf) < n:
        raise ConnectionError(f"HTTP stream ended early (got {len(buf)}/{n})")
    result = bytes(buf[:n])
    # Put back any excess into the stream — not possible with iter_bytes,
    # so we use a wrapper that handles this
    return result


@dataclass
class _BoxHeader:
    box_type: bytes   # 4 bytes, e.g. b"mdat"
    box_size: int     # total size including header
    header_size: int  # 8 or 16
    payload_size: int  # box_size - header_size


def _read_box_header(reader) -> Optional[_BoxHeader]:
    """Read an ISO BMFF box header from a byte iterator.
    Returns None on EOF."""
    hdr = reader.read(8)
    if len(hdr) < 8:
        return None
    size_field = struct.unpack(">I", hdr[:4])[0]
    box_type = hdr[4:8]
    header_size = 8
    if size_field == 1:
        ext = reader.read(8)
        if len(ext) < 8:
            return None
        size_field = struct.unpack(">Q", ext)[0]
        header_size = 16
    return _BoxHeader(
        box_type=box_type,
        box_size=size_field,
        header_size=header_size,
        payload_size=size_field - header_size,
    )


class _StreamReader:
    """Wraps httpx streaming response to provide read(n) interface."""

    def __init__(self, response: httpx.Response):
        self._iter = response.iter_bytes(32768)
        self._buf = bytearray()

    def read(self, n: int) -> bytes:
        while len(self._buf) < n:
            try:
                chunk = next(self._iter)
                self._buf.extend(chunk)
            except StopIteration:
                break
        result = bytes(self._buf[:n])
        self._buf = self._buf[n:]
        return result


def _make_aria_socket(host: str, port: int, timeout: float) -> socket.socket:
    s = socket.create_connection((host, port), timeout=timeout)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _SOCK_BUFSIZE)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _SOCK_BUFSIZE)
    s.settimeout(timeout)
    return s


def _recv_exact_sock(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), _SOCK_BUFSIZE))
        if not chunk:
            raise ConnectionError(f"aria closed (got {len(buf)}/{n})")
        buf.extend(chunk)
    return bytes(buf)


def stream_download_decrypt(
    stream_url: str,
    keys: list[str],
    track_id: str,
    out_path: str,
    *,
    aria_host: str = "127.0.0.1",
    aria_port: int = 47010,
    progress: Optional[Callable] = None,
) -> StreamingResult:
    """Download + decrypt in a single streaming pass. Peak memory ~200 KB.

    1. Opens HTTP stream to Apple CDN
    2. Reads box headers one by one
    3. For moov: buffers and parses (alac cookie, trex defaults)
    4. For moof: buffers and parses (trun → sample sizes + desc_index)
    5. For mdat: reads sample-by-sample from HTTP, sends to aria, writes plaintext to file
    6. Never holds more than one sample in memory
    """
    sock = _make_aria_socket(aria_host, aria_port, 120.0)
    current_desc = None
    sample_count = 0
    total_bytes = 0

    # Metadata extracted from moov
    trex: Optional[TrexDefaults] = None
    alac: Optional[AlacCookie] = None
    moov_raw: Optional[bytes] = None

    # Per-moof state
    pending_sample_sizes: List[tuple] = []  # [(size, desc_index, duration), ...]

    out_fd = open(out_path, "wb")

    try:
        with httpx.stream("GET", stream_url, follow_redirects=True, timeout=60) as resp:
            resp.raise_for_status()
            reader = _StreamReader(resp)

            while True:
                hdr = _read_box_header(reader)
                if hdr is None:
                    break

                if hdr.box_type in (b"ftyp", b"styp"):
                    # Small box, buffer and write to output
                    payload = reader.read(hdr.payload_size)
                    out_fd.write(struct.pack(">I", hdr.box_size))
                    out_fd.write(hdr.box_type)
                    if hdr.header_size == 16:
                        out_fd.write(struct.pack(">Q", hdr.box_size))
                    out_fd.write(payload)

                elif hdr.box_type == b"moov":
                    # Buffer entire moov (~few KB), parse metadata
                    payload = reader.read(hdr.payload_size)
                    moov_raw = (struct.pack(">I", hdr.box_size)
                                + hdr.box_type + payload)
                    # Parse trex + alac
                    trex_box = find_path_first(moov_raw, [b"moov", b"mvex", b"trex"])
                    if trex_box:
                        trex = parse_trex(moov_raw, trex_box)
                    alac = _extract_alac_cookie(moov_raw)
                    # Write moov to output
                    out_fd.write(moov_raw)

                elif hdr.box_type == b"moof":
                    # Buffer moof (~few KB), parse trun for upcoming mdat
                    payload = reader.read(hdr.payload_size)
                    moof_raw = (struct.pack(">I", hdr.box_size)
                                + hdr.box_type + payload)
                    # Write to output
                    moof_offset = out_fd.tell()
                    out_fd.write(moof_raw)

                    # Parse tfhd → desc_index, trun → sample sizes
                    pending_sample_sizes = []
                    tfhd_box = find_path_first(
                        moof_raw, [b"traf", b"tfhd"], 8, len(moof_raw))
                    if tfhd_box:
                        tfhd = parse_tfhd(moof_raw, tfhd_box)
                        sd_idx = tfhd.sample_description_index
                        if sd_idx is None and trex:
                            sd_idx = trex.default_sample_description_index
                        if sd_idx and sd_idx > 0:
                            sd_idx -= 1

                        trun_boxes = find_path_all(
                            moof_raw, [b"traf", b"trun"], 8, len(moof_raw))
                        for trun_box in trun_boxes:
                            run = parse_trun(moof_raw, trun_box)
                            for entry_size, entry_dur in run.entries:
                                sz = entry_size
                                if sz is None:
                                    sz = (tfhd.default_sample_size
                                          or (trex.default_sample_size if trex else 0))
                                dur = entry_dur
                                if dur is None:
                                    dur = (tfhd.default_sample_duration
                                           or (trex.default_sample_duration if trex else 0))
                                pending_sample_sizes.append((sz, sd_idx or 0, dur or 0))

                elif hdr.box_type == b"mdat":
                    # Stream mdat sample-by-sample — THIS IS THE KEY PART
                    # Write mdat header to output
                    mdat_hdr = struct.pack(">I", hdr.box_size) + hdr.box_type
                    if hdr.header_size == 16:
                        mdat_hdr = (struct.pack(">I", 1) + hdr.box_type
                                    + struct.pack(">Q", hdr.box_size))
                    out_fd.write(mdat_hdr)
                    mdat_data_offset = out_fd.tell()

                    for sz, desc_idx, dur in pending_sample_sizes:
                        # Read one sample from HTTP stream
                        cipher = reader.read(sz)
                        if len(cipher) != sz:
                            raise ConnectionError(
                                f"HTTP truncated: got {len(cipher)}/{sz}")

                        # Send to aria for decryption
                        if desc_idx != current_desc:
                            if current_desc is not None:
                                sock.sendall(b"\x00\x00\x00\x00")
                            key_uri = keys[desc_idx]
                            tid = PREFETCH_TRACK_ID if key_uri == PREFETCH_KEY else track_id
                            tid_b = tid.encode("ascii")
                            kuri_b = key_uri.encode("utf-8")
                            sock.sendall(
                                bytes([len(tid_b)]) + tid_b
                                + bytes([len(kuri_b)]) + kuri_b
                            )
                            current_desc = desc_idx

                        sock.sendall(struct.pack("<I", sz) + cipher)
                        plaintext = _recv_exact_sock(sock, sz)

                        # Write plaintext directly to output file
                        out_fd.write(plaintext)

                        sample_count += 1
                        total_bytes += sz

                        if progress and sample_count % 100 == 0:
                            progress(total_bytes, -1)

                    pending_sample_sizes = []

                else:
                    # Unknown box — buffer and write through
                    payload = reader.read(hdr.payload_size)
                    out_fd.write(struct.pack(">I", hdr.box_size))
                    out_fd.write(hdr.box_type)
                    if hdr.header_size == 16:
                        out_fd.write(struct.pack(">Q", hdr.box_size))
                    out_fd.write(payload)

        # End aria session
        sock.sendall(b"\x00\x00\x00\x00\x00")

    finally:
        sock.close()
        out_fd.close()

    if progress:
        progress(total_bytes, total_bytes)

    # Patch stsd: enca → alac, remove sinf
    from .m4a_writer import _patch_stsd_inplace
    from .m4s_parser import ParsedSong
    # We need a minimal ParsedSong for the stsd patcher
    _patch_stsd_inplace(out_path, ParsedSong(
        raw=b"", alac=alac, samples=[], total_data_size=total_bytes))

    return StreamingResult(
        out_path=out_path,
        samples_count=sample_count,
        total_bytes=total_bytes,
        sample_rate=alac.sample_rate if alac else 0,
        bit_depth=alac.bit_depth if alac else 0,
    )
