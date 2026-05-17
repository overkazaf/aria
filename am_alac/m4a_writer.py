"""Write the decrypted samples back into a valid ALAC m4a file.

Strategy
========
We do a **structural patch in place** on the input fragmented MP4:

  1. `moov.trak.mdia.minf.stbl.stsd` — find the `enca` SampleEntry, drop
     its `sinf` child, rename it to `alac`. Fix the size of every
     containing box (stsd, stbl, minf, mdia, trak, moov).
  2. `mdat` payloads — overwrite each ciphertext sample with the matching
     plaintext. Per-sample lengths are preserved (Apple uses sample-AES,
     so plaintext_len == ciphertext_len).
  3. (`senc` boxes inside `traf` are left in place — ALAC decoders ignore
     unknown sample-encryption metadata.)

The result is a *fragmented* ALAC m4a with the same fragment layout as the
input. With `to_faststart_m4a()` we can pipe through ffmpeg `-c copy
-movflags +faststart` to get a non-fragmented faststart m4a (matching the
shape produced by a prior reference implementation4aFile).
"""
from __future__ import annotations

import os
import subprocess
from struct import pack_into, unpack_from
from typing import Optional

from .m4s_parser import (
    BoxLocation,
    ParsedSong,
    _container_child_range,
    find_path_all,
    find_path_first,
    iter_boxes,
    parse_trun,
    _TRUN_FLAG_SAMPLE_SIZE,
)


# ──────────────────────────── helpers ──────────────────────────────────────

def _find_ancestors(buf: bytes, path: list[bytes]) -> list[BoxLocation]:
    """Walk down `path` and return *every* box on it including the leaf.

    Used by patch_to_alac_m4a to identify all ancestor sizes that need to
    be bumped after we shrink the leaf box.
    """
    ancestors: list[BoxLocation] = []
    cursor_start, cursor_end = 0, len(buf)
    for i, target_type in enumerate(path):
        located: Optional[BoxLocation] = None
        for candidate in iter_boxes(buf, cursor_start, cursor_end):
            if candidate.type == target_type:
                located = candidate
                break
        if located is None:
            raise ValueError(
                f"missing {target_type!r} in path "
                f"{[t.decode() for t in path[:i+1]]}")
        ancestors.append(located)
        cursor_start, cursor_end = _container_child_range(located)
    return ancestors


def _set_box_size(buf: bytearray, box_offset: int, new_size: int) -> None:
    if not (0 <= new_size <= 0xFFFFFFFF):
        raise ValueError(
            f"box size out of range: {new_size} at offset {box_offset}")
    pack_into(">I", buf, box_offset, new_size)


# ──────────────────────────── public API ───────────────────────────────────

def patch_to_alac_m4a(
    song: ParsedSong,
    decrypted_samples: list[bytes],
    out_path: str,
) -> None:
    """Patch `song.raw` → `out_path` m4a:

       (1) rewrite mdat contents with decrypted samples (length-preserving)
       (2) inside stsd's `enca` SampleEntry, drop `sinf`, rename to `alac`
       (3) fix every ancestor box size on the path moov→…→stsd

    `decrypted_samples` must match `song.samples` 1:1 by index, and each
    plaintext must have the same byte length as its ciphertext.
    """
    if len(decrypted_samples) != len(song.samples):
        raise ValueError(
            f"sample count mismatch: "
            f"{len(decrypted_samples)} vs {len(song.samples)}")
    for i, (orig, plain) in enumerate(zip(song.samples, decrypted_samples)):
        if len(orig.data) != len(plain):
            raise ValueError(
                f"sample {i} length differs: "
                f"enc={len(orig.data)} dec={len(plain)}")

    output = bytearray(song.raw)

    # ── step 1: rewrite each mdat in place ────────────────────────────────
    plaintext_iter = iter(decrypted_samples)
    snapshot = bytes(output)
    for moof_box in iter_boxes(snapshot):
        if moof_box.type != b"moof":
            continue
        # adjacent mdat
        next_box = next(iter_boxes(snapshot, moof_box.end_offset), None)
        if next_box is None or next_box.type != b"mdat":
            continue

        mdat_cursor = next_box.payload_offset
        for trun_box in find_path_all(
                snapshot, [b"traf", b"trun"],
                moof_box.payload_offset, moof_box.end_offset):
            run = parse_trun(snapshot, trun_box)
            uses_per_sample_size = bool(run.flags & _TRUN_FLAG_SAMPLE_SIZE)
            for entry_size, _entry_duration in run.entries:
                if not uses_per_sample_size:
                    raise NotImplementedError(
                        "trun without per-sample size is not yet supported "
                        "by the writer (would need tfhd/trex defaults)")
                plaintext = next(plaintext_iter)
                if len(plaintext) != entry_size:
                    raise ValueError(
                        f"size mismatch: trun={entry_size} pt={len(plaintext)}")
                output[mdat_cursor: mdat_cursor + entry_size] = plaintext
                mdat_cursor += entry_size

    # ── step 2: drop sinf inside enca, rename enca → alac ─────────────────
    stsd_path = [b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd"]
    ancestors = _find_ancestors(output, stsd_path)
    parents = ancestors[:-1]   # moov..stbl
    stsd_box = ancestors[-1]

    # Iterate the stsd's SampleEntry list to find `enca`.
    entry_cursor = stsd_box.payload_offset + 4 + 4   # skip vf + entry_count
    end_of_stsd = stsd_box.end_offset
    sinf_size_removed = 0   # signed delta to propagate up
    enca_size_after_patch = 0
    enca_offset = -1

    while entry_cursor < end_of_stsd:
        (entry_size,) = unpack_from(">I", output, entry_cursor)
        entry_type = bytes(output[entry_cursor + 4: entry_cursor + 8])
        if entry_type != b"enca":
            entry_cursor += entry_size
            continue

        # AudioSampleEntry: 8-byte hdr + 28 audio fields = 36 byte prelude
        child_search_start = entry_cursor + 36
        child_search_end = entry_cursor + entry_size

        sinf_location: Optional[BoxLocation] = None
        for child in iter_boxes(output, child_search_start, child_search_end):
            if child.type == b"sinf":
                sinf_location = child
                break

        if sinf_location is not None:
            # Splice out the sinf bytes
            del output[sinf_location.offset: sinf_location.offset + sinf_location.size]
            sinf_size_removed = sinf_location.size
            # Update enca's own size field
            _set_box_size(output, entry_cursor, entry_size - sinf_size_removed)

        # Rename the entry: enca → alac (4-byte rename, no size change)
        output[entry_cursor + 4: entry_cursor + 8] = b"alac"
        enca_offset = entry_cursor
        enca_size_after_patch = entry_size - sinf_size_removed
        break

    if enca_offset < 0:
        raise ValueError("no enca SampleEntry found inside stsd")

    # If we removed bytes, every ancestor (stsd, stbl, minf, mdia, trak, moov)
    # must shrink by the same amount.
    if sinf_size_removed:
        for parent in ancestors:   # includes stsd itself
            _set_box_size(output, parent.offset, parent.size - sinf_size_removed)

    # ── step 3: write to disk ─────────────────────────────────────────────
    if out_path == "-":
        os.write(1, bytes(output))
    else:
        with open(out_path, "wb") as f:
            f.write(bytes(output))


def to_faststart_m4a(in_path: str, out_path: str) -> None:
    """Pipe a fragmented m4a through ffmpeg → non-fragmented faststart m4a.

    Runs `ffmpeg -i in -c copy -movflags +faststart out`.  Requires ffmpeg
    on `$PATH`.  Container-level metadata (stsz/stco/etc) is rebuilt by
    ffmpeg's mux; audio frames are bit-identical (`-c copy`).
    """
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", in_path,
            "-c", "copy",
            "-movflags", "+faststart",
            out_path,
        ],
        check=True,
    )


def _patch_stsd_inplace(path: str, song: ParsedSong) -> None:
    """Patch stsd in an on-disk file: enca → alac, remove sinf, fix sizes.

    Only reads the moov box region (~few KB) instead of the entire file.
    """
    file_size = os.path.getsize(path)

    # Step 1: Find moov box offset and size by scanning box headers
    moov_offset = -1
    moov_size = 0
    with open(path, "rb") as f:
        cursor = 0
        while cursor < file_size:
            f.seek(cursor)
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            sz = unpack_from(">I", hdr, 0)[0]
            btype = hdr[4:8]
            if sz == 1:
                ext = f.read(8)
                sz = unpack_from(">Q", ext, 0)[0]
            if sz == 0:
                sz = file_size - cursor
            if btype == b"moov":
                moov_offset = cursor
                moov_size = sz
                break
            cursor += sz

    if moov_offset < 0:
        raise ValueError("moov box not found in file")

    # Step 2: Read only the moov box
    with open(path, "rb") as f:
        f.seek(moov_offset)
        moov_raw = bytearray(f.read(moov_size))

    # Step 3: Patch within the moov buffer
    stsd_path = [b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd"]
    ancestors = _find_ancestors(moov_raw, stsd_path)
    stsd_box = ancestors[-1]

    entry_cursor = stsd_box.payload_offset + 4 + 4
    end_of_stsd = stsd_box.end_offset
    sinf_size_removed = 0

    while entry_cursor < end_of_stsd:
        (entry_size,) = unpack_from(">I", moov_raw, entry_cursor)
        entry_type = bytes(moov_raw[entry_cursor + 4: entry_cursor + 8])
        if entry_type != b"enca":
            entry_cursor += entry_size
            continue

        child_start = entry_cursor + 36
        child_end = entry_cursor + entry_size

        sinf_loc: Optional[BoxLocation] = None
        for child in iter_boxes(moov_raw, child_start, child_end):
            if child.type == b"sinf":
                sinf_loc = child
                break

        if sinf_loc is not None:
            del moov_raw[sinf_loc.offset: sinf_loc.offset + sinf_loc.size]
            sinf_size_removed = sinf_loc.size
            _set_box_size(moov_raw, entry_cursor, entry_size - sinf_size_removed)

        moov_raw[entry_cursor + 4: entry_cursor + 8] = b"alac"
        break

    if sinf_size_removed:
        for parent in ancestors:
            _set_box_size(moov_raw, parent.offset, parent.size - sinf_size_removed)

    # Step 4: Write patched moov back
    # If sinf was removed, moov shrank → need to shift all data after it.
    # Use a temp file to avoid reading entire file into RAM.
    if sinf_size_removed:
        import tempfile
        tmp = path + ".tmp"
        CHUNK = 1 << 20  # 1 MB chunks
        with open(path, "rb") as src, open(tmp, "wb") as dst:
            # Copy before moov
            remaining = moov_offset
            while remaining > 0:
                n = min(remaining, CHUNK)
                dst.write(src.read(n))
                remaining -= n
            # Write patched moov
            dst.write(bytes(moov_raw))
            # Skip original moov in src
            src.seek(moov_offset + moov_size)
            # Copy everything after moov in chunks
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
        os.replace(tmp, path)
    else:
        # No size change — just overwrite moov region in-place
        with open(path, "r+b") as f:
            f.seek(moov_offset)
            f.write(bytes(moov_raw))


def patch_file_streaming(
    song: ParsedSong,
    out_path: str,
    *,
    aria_host: str = "127.0.0.1",
    aria_port: int = 47010,
    track_id: str = "",
) -> None:
    """Write M4S to disk, decrypt samples in-place via aria, patch stsd.

    Peak memory = raw M4S + 1 sample (~16 KB), instead of raw + all plaintexts.
    """
    from . import aria_rpc
    import struct
    import socket

    with open(out_path, "wb") as f:
        f.write(song.raw)

    sock = aria_rpc._make_socket(aria_host, aria_port, 120.0)
    current_desc = None

    try:
        with open(out_path, "r+b") as f:
            for sample in song.samples:
                if sample.desc_index != current_desc:
                    if current_desc is not None:
                        sock.sendall(b"\x00\x00\x00\x00")
                    key_uri = song.keys[sample.desc_index] if hasattr(song, 'keys') else ""
                    tid = "0" if key_uri == aria_rpc.PREFETCH_KEY else track_id
                    tid_b = tid.encode("ascii")
                    kuri_b = key_uri.encode("utf-8")
                    sock.sendall(
                        bytes([len(tid_b)]) + tid_b
                        + bytes([len(kuri_b)]) + kuri_b
                    )
                    current_desc = sample.desc_index

                sock.sendall(struct.pack("<I", len(sample.data)) + sample.data)

                buf = bytearray()
                n = len(sample.data)
                while len(buf) < n:
                    chunk = sock.recv(min(n - len(buf), 262144))
                    if not chunk:
                        raise ConnectionError("aria closed")
                    buf.extend(chunk)

                if hasattr(sample, 'file_offset') and sample.file_offset is not None:
                    f.seek(sample.file_offset)
                    f.write(buf)

        sock.sendall(b"\x00\x00\x00\x00\x00")
    finally:
        sock.close()

    _patch_stsd_inplace(out_path, song)
