"""Parse Apple Music's encrypted fragmented MP4 (the `_m.mp4` segment) into
a list of encrypted samples plus the ALAC magic cookie.

The on-disk layout is a standard ISO BMFF fragmented MP4 with `enca`
sample entries (encrypted audio) wrapping an `alac` codec config. Each
`mdat` is a contiguous run of per-sample ciphertexts; offsets/sizes come
from the matching `traf.trun` (or `tfhd` / `mvex.trex` defaults).

Reverse-engineered from a prior reference implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from struct import unpack_from
from typing import Iterator, Optional

import httpx

from .aria_rpc import Sample


# ──────────────────────────── data types ───────────────────────────────────

@dataclass
class AlacCookie:
    """ALAC codec config payload (the FullBox payload of the `alac` box)."""
    frame_length:        int   # samples per ALAC frame, typically 4096
    compatible_version:  int   # always 0
    bit_depth:           int   # 16 / 24
    pb:                  int   # ALAC tuning parameter
    mb:                  int   # ALAC tuning parameter
    kb:                  int   # ALAC tuning parameter
    num_channels:        int   # 1 / 2
    max_run:             int
    max_frame_bytes:     int
    avg_bit_rate:        int
    sample_rate:         int   # 44100 / 48000 / 96000 / 192000
    raw_payload:         bytes  # full original `alac` box payload

    @classmethod
    def from_full_box_payload(cls, payload: bytes) -> "AlacCookie":
        """Parse the 24-byte FullBox payload (a prior reference implementation).

        Big-endian layout:
            u32 frame_length
            u8  compatible_version
            u8  bit_depth
            u8  pb
            u8  mb
            u8  kb
            u8  num_channels
            u16 max_run
            u32 max_frame_bytes
            u32 avg_bit_rate
            u32 sample_rate
        """
        if len(payload) < 24:
            raise ValueError(f"alac cookie too short: {len(payload)} < 24")
        (frame_length,) = unpack_from(">I", payload, 0)
        (compatible_version, bit_depth, pb, mb, kb, num_channels) = \
            unpack_from(">BBBBBB", payload, 4)
        (max_run, max_frame_bytes, avg_bit_rate, sample_rate) = \
            unpack_from(">HIII", payload, 10)
        return cls(
            frame_length        = frame_length,
            compatible_version  = compatible_version,
            bit_depth           = bit_depth,
            pb                  = pb,
            mb                  = mb,
            kb                  = kb,
            num_channels        = num_channels,
            max_run             = max_run,
            max_frame_bytes     = max_frame_bytes,
            avg_bit_rate        = avg_bit_rate,
            sample_rate         = sample_rate,
            raw_payload         = bytes(payload[:24]),
        )


@dataclass
class ParsedSong:
    raw:              bytes      # full input M4S, kept for the writer to patch
    alac:             AlacCookie
    samples:          list[Sample]
    total_data_size:  int        # sum of all sample sizes (for progress)


# ──────────────────────────── ISO BMFF helpers ─────────────────────────────

@dataclass
class BoxLocation:
    offset:       int
    size:         int            # full box size including header
    type:         bytes
    header_size:  int            # 8 (32-bit size) or 16 (64-bit size)

    @property
    def payload_offset(self) -> int:
        return self.offset + self.header_size

    @property
    def end_offset(self) -> int:
        return self.offset + self.size


def iter_boxes(buf: bytes, start: int = 0, end: Optional[int] = None
               ) -> Iterator[BoxLocation]:
    """Yield every top-level box in `buf[start:end]`."""
    if end is None:
        end = len(buf)
    cursor = start
    while cursor < end:
        if cursor + 8 > end:
            return
        (size_field,) = unpack_from(">I", buf, cursor)
        box_type = bytes(buf[cursor + 4: cursor + 8])
        if size_field == 1:
            (size_field,) = unpack_from(">Q", buf, cursor + 8)
            header_size = 16
        elif size_field == 0:
            yield BoxLocation(cursor, end - cursor, box_type, 8)
            return
        else:
            header_size = 8
        yield BoxLocation(cursor, size_field, box_type, header_size)
        cursor += size_field


# Container box types whose children are plain boxes (no FullBox header).
_PLAIN_CONTAINERS = {
    b"moov", b"trak", b"mdia", b"minf", b"stbl",
    b"mvex", b"moof", b"traf", b"udta", b"mfra", b"edts",
}
# Container types whose payload begins with a 4-byte version/flags word
# AND a 4-byte entry_count BEFORE its children. We need to skip both.
_FULL_BOX_LIST_CONTAINERS = {b"stsd"}


def _container_child_range(parent: BoxLocation) -> tuple[int, int]:
    """Return (child_start, child_end) for descending into a container box."""
    payload_start = parent.payload_offset
    if parent.type in _FULL_BOX_LIST_CONTAINERS:
        payload_start += 4 + 4   # skip version+flags + entry_count
    return payload_start, parent.end_offset


def find_path_all(buf: bytes, path: list[bytes], start: int = 0,
                  end: Optional[int] = None) -> list[BoxLocation]:
    """Find every leaf at `path` (matching multiple occurrences at each level)."""
    if end is None:
        end = len(buf)
    if not path:
        return []
    target_type = path[0]
    rest = path[1:]
    matches: list[BoxLocation] = []
    for box in iter_boxes(buf, start, end):
        if box.type != target_type:
            continue
        if not rest:
            matches.append(box)
            continue
        child_start, child_end = _container_child_range(box)
        matches.extend(find_path_all(buf, rest, child_start, child_end))
    return matches


def find_path_first(buf: bytes, path: list[bytes], start: int = 0,
                    end: Optional[int] = None) -> Optional[BoxLocation]:
    matches = find_path_all(buf, path, start, end)
    return matches[0] if matches else None


# ──────────────────────────── box payload parsers ─────────────────────────

@dataclass
class TrexDefaults:
    track_id:                              int
    default_sample_description_index:      int
    default_sample_duration:               int
    default_sample_size:                   int
    default_sample_flags:                  int


def parse_trex(buf: bytes, box: BoxLocation) -> TrexDefaults:
    """trex (FullBox)."""
    p = box.payload_offset + 4   # skip version+flags
    track_id, dsd_idx, duration, size, flags = unpack_from(">IIIII", buf, p)
    return TrexDefaults(
        track_id                          = track_id,
        default_sample_description_index  = dsd_idx,
        default_sample_duration           = duration,
        default_sample_size               = size,
        default_sample_flags              = flags,
    )


@dataclass
class TfhdHeader:
    flags:                       int
    track_id:                    int
    base_data_offset:            Optional[int]
    sample_description_index:    Optional[int]
    default_sample_duration:     Optional[int]
    default_sample_size:         Optional[int]
    default_sample_flags:        Optional[int]


_TFHD_FLAG_BASE_DATA_OFFSET     = 0x000001
_TFHD_FLAG_SD_INDEX             = 0x000002
_TFHD_FLAG_DEFAULT_DURATION     = 0x000008
_TFHD_FLAG_DEFAULT_SIZE         = 0x000010
_TFHD_FLAG_DEFAULT_FLAGS        = 0x000020


def parse_tfhd(buf: bytes, box: BoxLocation) -> TfhdHeader:
    """tfhd (FullBox).  Optional fields are gated by `flags` bits."""
    (flags_word,) = unpack_from(">I", buf, box.payload_offset)
    flags = flags_word & 0x00_FF_FF_FF
    cursor = box.payload_offset + 4

    (track_id,) = unpack_from(">I", buf, cursor); cursor += 4

    base_data_offset = None
    if flags & _TFHD_FLAG_BASE_DATA_OFFSET:
        (base_data_offset,) = unpack_from(">Q", buf, cursor); cursor += 8

    sd_index = None
    if flags & _TFHD_FLAG_SD_INDEX:
        (sd_index,) = unpack_from(">I", buf, cursor); cursor += 4

    default_duration = None
    if flags & _TFHD_FLAG_DEFAULT_DURATION:
        (default_duration,) = unpack_from(">I", buf, cursor); cursor += 4

    default_size = None
    if flags & _TFHD_FLAG_DEFAULT_SIZE:
        (default_size,) = unpack_from(">I", buf, cursor); cursor += 4

    default_flags = None
    if flags & _TFHD_FLAG_DEFAULT_FLAGS:
        (default_flags,) = unpack_from(">I", buf, cursor); cursor += 4

    return TfhdHeader(
        flags                     = flags,
        track_id                  = track_id,
        base_data_offset          = base_data_offset,
        sample_description_index  = sd_index,
        default_sample_duration   = default_duration,
        default_sample_size       = default_size,
        default_sample_flags      = default_flags,
    )


_TRUN_FLAG_DATA_OFFSET           = 0x000001
_TRUN_FLAG_FIRST_SAMPLE_FLAGS    = 0x000004
_TRUN_FLAG_SAMPLE_DURATION       = 0x000100
_TRUN_FLAG_SAMPLE_SIZE           = 0x000200
_TRUN_FLAG_SAMPLE_FLAGS          = 0x000400
_TRUN_FLAG_SAMPLE_CTS_OFFSET     = 0x000800


@dataclass
class TrunRun:
    flags:    int
    entries:  list[tuple[Optional[int], Optional[int]]]   # [(size, duration), ...]


def parse_trun(buf: bytes, box: BoxLocation) -> TrunRun:
    """trun (FullBox).  Returns (flags, list[(size_or_None, duration_or_None)])."""
    (flags_word,) = unpack_from(">I", buf, box.payload_offset)
    flags = flags_word & 0x00_FF_FF_FF
    cursor = box.payload_offset + 4
    (sample_count,) = unpack_from(">I", buf, cursor); cursor += 4
    if flags & _TRUN_FLAG_DATA_OFFSET:        cursor += 4
    if flags & _TRUN_FLAG_FIRST_SAMPLE_FLAGS: cursor += 4

    entries: list[tuple[Optional[int], Optional[int]]] = []
    for _ in range(sample_count):
        sample_duration = None
        sample_size = None
        if flags & _TRUN_FLAG_SAMPLE_DURATION:
            (sample_duration,) = unpack_from(">I", buf, cursor); cursor += 4
        if flags & _TRUN_FLAG_SAMPLE_SIZE:
            (sample_size,) = unpack_from(">I", buf, cursor); cursor += 4
        if flags & _TRUN_FLAG_SAMPLE_FLAGS:        cursor += 4
        if flags & _TRUN_FLAG_SAMPLE_CTS_OFFSET:   cursor += 4
        entries.append((sample_size, sample_duration))
    return TrunRun(flags=flags, entries=entries)


# ──────────────────────────── public parsers ───────────────────────────────

def _extract_alac_cookie(buf: bytes) -> AlacCookie:
    """Walk `moov.trak.mdia.minf.stbl.stsd` and pull out the `alac` payload.

    The first SampleEntry is `enca` (encrypted audio); inside it sits the
    real `alac` codec config box plus a `sinf` (scheme info).
    """
    stsd = find_path_first(
        buf, [b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd"])
    if stsd is None:
        raise ValueError("missing moov.trak.mdia.minf.stbl.stsd")

    # stsd payload: 4B version+flags + 4B entry_count + entries
    (entry_count,) = unpack_from(">I", buf, stsd.payload_offset + 4)
    cursor = stsd.payload_offset + 4 + 4
    end_of_stsd = stsd.end_offset

    while cursor < end_of_stsd and entry_count > 0:
        (entry_size,) = unpack_from(">I", buf, cursor)
        entry_type = bytes(buf[cursor + 4: cursor + 8])
        if entry_type == b"enca":
            # AudioSampleEntry: 8 hdr + 28 audio fields = 36 byte fixed prelude.
            child_search_start = cursor + 36
            child_search_end = cursor + entry_size
            for child in iter_boxes(buf, child_search_start, child_search_end):
                if child.type == b"alac":
                    # `alac` is a FullBox: skip 4-byte version+flags
                    payload = bytes(buf[child.payload_offset + 4: child.end_offset])
                    return AlacCookie.from_full_box_payload(payload)
        cursor += entry_size
        entry_count -= 1

    raise ValueError("no `alac` codec config found inside stsd.enca")


def _walk_fragments_into_samples(
    buf: bytes, trex_defaults: TrexDefaults
) -> list[Sample]:
    """For each (moof, mdat) pair: produce one or more `Sample` objects."""
    moof_locations = [b for b in iter_boxes(buf) if b.type == b"moof"]
    mdat_locations = [b for b in iter_boxes(buf) if b.type == b"mdat"]
    if len(moof_locations) != len(mdat_locations):
        raise ValueError(
            f"moof/mdat count mismatch: {len(moof_locations)} vs {len(mdat_locations)}")

    samples: list[Sample] = []
    for moof, mdat in zip(moof_locations, mdat_locations):
        # tfhd inside this moof.traf
        tfhd_box = find_path_first(
            buf, [b"traf", b"tfhd"], moof.payload_offset, moof.end_offset)
        if tfhd_box is None:
            raise ValueError("missing moof.traf.tfhd")
        tfhd = parse_tfhd(buf, tfhd_box)

        sd_index = tfhd.sample_description_index or trex_defaults.default_sample_description_index
        if sd_index > 0:
            sd_index -= 1   # convert from 1-based stsd index to 0-based key index

        # all trun runs in this moof.traf (sometimes there are several)
        trun_boxes = find_path_all(
            buf, [b"traf", b"trun"], moof.payload_offset, moof.end_offset)
        if not trun_boxes:
            raise ValueError("missing moof.traf.trun")

        # mdat payload starts after its header
        mdat_cursor = mdat.payload_offset
        mdat_end = mdat.end_offset

        for trun_box in trun_boxes:
            run = parse_trun(buf, trun_box)
            for entry_size, entry_duration in run.entries:
                # Resolve sample size: trun > tfhd > trex defaults
                if run.flags & _TRUN_FLAG_SAMPLE_SIZE:
                    sample_size = entry_size
                elif tfhd.default_sample_size is not None:
                    sample_size = tfhd.default_sample_size
                else:
                    sample_size = trex_defaults.default_sample_size

                # Resolve sample duration the same way
                if run.flags & _TRUN_FLAG_SAMPLE_DURATION:
                    duration = entry_duration
                elif tfhd.default_sample_duration is not None:
                    duration = tfhd.default_sample_duration
                else:
                    duration = trex_defaults.default_sample_duration

                if mdat_cursor + sample_size > mdat_end:
                    raise ValueError(
                        f"trun consumes past mdat end "
                        f"({mdat_cursor}+{sample_size} > {mdat_end})")
                s = Sample(
                    data=bytes(buf[mdat_cursor: mdat_cursor + sample_size]),
                    desc_index=sd_index,
                    duration=duration or 0,
                )
                s.file_offset = mdat_cursor
                samples.append(s)
                mdat_cursor += sample_size

        if mdat_cursor != mdat_end:
            raise ValueError(
                f"mdat tail mismatch: cursor={mdat_cursor} mdat_end={mdat_end}")

    return samples


def parse_m4s(buf: bytes) -> ParsedSong:
    """Parse an Apple Music encrypted ALAC fragmented MP4 into samples.

    Walks moov.mvex.trex for defaults, moov.trak.mdia.minf.stbl.stsd for
    the ALAC magic cookie, and every (moof.traf.{tfhd,trun}, mdat) pair
    for the actual ciphertext samples.

    Mirrors a prior reference implementation.
    """
    trex_box = find_path_first(buf, [b"moov", b"mvex", b"trex"])
    if trex_box is None:
        raise ValueError("missing moov.mvex.trex")
    trex_defaults = parse_trex(buf, trex_box)

    alac_cookie = _extract_alac_cookie(buf)
    samples = _walk_fragments_into_samples(buf, trex_defaults)

    return ParsedSong(
        raw                = buf,
        alac               = alac_cookie,
        samples            = samples,
        total_data_size    = sum(len(s.data) for s in samples),
    )


def fetch_and_parse(url: str, *, client: Optional[httpx.Client] = None,
                    chunk_size: int = 1 << 20) -> ParsedSong:
    """Download `_m.mp4` and parse it. `client` is optional for connection reuse."""
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            buffer = bytearray()
            for chunk in response.iter_bytes(chunk_size):
                buffer.extend(chunk)
        return parse_m4s(bytes(buffer))
    finally:
        if own_client:
            client.close()
