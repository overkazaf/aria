"""am_alac CLI — `python -m am_alac <song_id_or_url>`."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import apple_api, decryptor


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="am_alac",
        description="Apple Music ALAC decryptor (clean-room re-impl of a prior reference implementation).",
    )
    p.add_argument("target", nargs="+",
                   help="Apple Music song_id(s) or URL(s). "
                        "Pass multiple IDs for batch mode with prefetch.")
    p.add_argument("-o", "--out-dir", default="./alac_out", help="output directory")
    p.add_argument("--storefront", default="us")
    p.add_argument("--auth-token", help="Apple Music web-player JWT (auto if omitted)")
    p.add_argument("--media-user-token", help="Apple Music media-user-token")
    p.add_argument("--aria-host", default="127.0.0.1")
    p.add_argument("--decrypt-port", type=int, default=47010)
    p.add_argument("--m3u8-port", type=int, default=47020)
    p.add_argument("--max-sr", type=int, default=192_000,
                   help="max sample rate Hz (default 192000)")
    p.add_argument("--faststart", action="store_true",
                   help="produce non-fragmented faststart m4a (requires ffmpeg)")
    p.add_argument("--apple-proxy", help="HTTP(S) proxy for Apple API only")
    p.add_argument("--prefetch", type=int, default=2,
                   help="how many tracks to pre-download ahead (batch mode)")
    p.add_argument("--json", action="store_true",
                   help="emit JSON result to stdout instead of human text")
    return p.parse_args(argv)


def _resolve_song_id(target: str, args) -> tuple[str, str]:
    if target.isdigit():
        return target, args.storefront
    sf, album_id, song_id = decryptor._parse_apple_url(target)
    if song_id is not None:
        return song_id, sf
    if album_id:
        with apple_api.AppleMusicClient(
            authorization_token=args.auth_token,
            media_user_token=args.media_user_token,
            proxy=args.apple_proxy,
        ) as ac:
            alb = ac.get_album(album_id, sf)
            if not alb.tracks:
                raise SystemExit(f"album {album_id} has no tracks")
            return alb.tracks[0].id, sf
    raise SystemExit(f"could not resolve a song from: {target!r}")


def _human_progress(stage, info):
    if stage == "decrypt-progress":
        done, total = info
        pct = (100 * done / total) if total else 0
        print(f"\r  decrypt {pct:5.1f}%  ({done}/{total} bytes)",
              file=sys.stderr, end="", flush=True)
        if done == total:
            print(file=sys.stderr)
        return
    if stage == "track-start":
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"  {info}", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)
        return
    if stage == "error":
        print(f"  [ERROR] {info}", file=sys.stderr)
        return
    print(f"  [{stage}] {info}", file=sys.stderr)


def main(argv=None) -> int:
    args = _parse_args(argv)

    targets = args.target
    song_ids = []
    sf = args.storefront
    for t in targets:
        sid, sf = _resolve_song_id(t, args)
        song_ids.append(sid)

    t0 = time.monotonic()

    if len(song_ids) == 1:
        res = decryptor.decrypt_one_track(
            song_id=song_ids[0],
            out_dir=args.out_dir,
            storefront=sf,
            authorization_token=args.auth_token,
            media_user_token=args.media_user_token,
            aria_host=args.aria_host,
            aria_decrypt_port=args.decrypt_port,
            aria_m3u8_port=args.m3u8_port,
            max_sample_rate_hz=args.max_sr,
            apple_proxy=args.apple_proxy,
            faststart=args.faststart,
            progress=None if args.json else _human_progress,
        )
        results = [res]
    else:
        results = decryptor.decrypt_batch(
            song_ids,
            out_dir=args.out_dir,
            storefront=sf,
            authorization_token=args.auth_token,
            media_user_token=args.media_user_token,
            aria_host=args.aria_host,
            aria_decrypt_port=args.decrypt_port,
            aria_m3u8_port=args.m3u8_port,
            max_sample_rate_hz=args.max_sr,
            apple_proxy=args.apple_proxy,
            faststart=args.faststart,
            prefetch_depth=args.prefetch,
            progress=None if args.json else _human_progress,
        )

    elapsed = time.monotonic() - t0

    if args.json:
        out = [{
            "song_id": r.song_id, "out_path": r.out_path,
            "sample_rate": r.sample_rate, "bit_depth": r.bit_depth,
            "samples": r.samples_count, "bytes": r.decrypted_bytes,
            "elapsed": r.elapsed_seconds,
        } for r in results]
        print(json.dumps(out if len(out) > 1 else out[0], ensure_ascii=False))
    else:
        total_bytes = sum(r.decrypted_bytes for r in results)
        print(f"\nDone: {len(results)} track(s) in {elapsed:.1f}s")
        print(f"  throughput: {total_bytes/elapsed/1e6:.1f} MB/s")
        for r in results:
            print(f"  {r.out_path} ({r.elapsed_seconds:.1f}s)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
