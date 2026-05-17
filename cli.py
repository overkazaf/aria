#!/usr/bin/env python3
"""aria CLI — download Apple Music tracks from the command line.

Usage:
    python cli.py search "Beatles" --limit 5
    python cli.py song 1440841263
    python cli.py download 1440841263
    python cli.py download 1440841263 --fmt alac
    python cli.py download 1440841263 1441164589 --fmt aac -o ./music
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from am_alac import apple_api, aria_rpc, m3u8_select, m4s_parser, m4a_writer, decryptor, aac_decrypt


def cmd_search(args):
    with apple_api.AppleMusicClient() as ac:
        token = apple_api.get_web_token()

    import httpx
    headers = {"Authorization": f"Bearer {token}", "Origin": "https://music.apple.com"}
    r = httpx.get(
        f"https://amp-api.music.apple.com/v1/catalog/{args.storefront}/search",
        params={"term": args.query, "types": args.type, "limit": str(args.limit)},
        headers=headers, timeout=10)
    data = r.json()

    for typ in args.type.split(","):
        typ = typ.strip()
        items = data.get("results", {}).get(typ, {}).get("data", [])
        if items:
            print(f"\n{typ} ({len(items)} results):")
            for item in items:
                a = item["attributes"]
                extra = ""
                if "artistName" in a:
                    extra += f" — {a['artistName']}"
                if "albumName" in a:
                    extra += f" [{a['albumName']}]"
                print(f"  {item['id']:>12}  {a.get('name', '?')}{extra}")


def cmd_song(args):
    with apple_api.AppleMusicClient() as ac:
        song = ac.get_song(args.song_id, args.storefront)
    print(f"  Title:    {song.title}")
    print(f"  Artist:   {song.artist}")
    print(f"  Album:    {song.album}")
    print(f"  ISRC:     {song.isrc}")
    print(f"  Track:    {song.disc_number}-{song.track_number}")
    print(f"  Traits:   {song.audio_traits}")
    print(f"  ADM:      {song.is_apple_digital_master}")
    print(f"  HLS:      {'yes' if song.enhanced_hls else 'no'}")


def cmd_download(args):
    os.makedirs(args.output, exist_ok=True)
    fmt = args.fmt.lower()

    for song_id in args.song_ids:
        print(f"\n{'='*50}")
        print(f"  Downloading {song_id} [{fmt.upper()}]")
        print(f"{'='*50}")

        t0 = time.monotonic()

        if fmt == "alac":
            try:
                result = decryptor.decrypt_one_track(
                    song_id=song_id,
                    out_dir=args.output,
                    storefront=args.storefront,
                    aria_host=args.aria_host,
                    aria_decrypt_port=args.decrypt_port,
                    aria_m3u8_port=args.m3u8_port,
                    progress=_progress,
                )
                elapsed = time.monotonic() - t0
                print(f"\n  Output:   {result.out_path}")
                print(f"  ALAC:     {result.bit_depth}-bit / {result.sample_rate} Hz")
                print(f"  Samples:  {result.samples_count}")
                print(f"  Size:     {result.decrypted_bytes:,} bytes")
                print(f"  Time:     {elapsed:.1f}s")
            except Exception as e:
                print(f"  ERROR: {e}")

        elif fmt == "aac":
            try:
                out_path = os.path.join(args.output, f"{song_id}.m4a")
                result = aac_decrypt.download_aac(song_id, out_path)
                elapsed = time.monotonic() - t0
                print(f"  Output:   {result.out_path}")
                print(f"  Size:     {result.size:,} bytes")
                print(f"  Time:     {elapsed:.1f}s")
            except Exception as e:
                print(f"  ERROR: {e}")


def _progress(stage, info):
    if stage == "decrypt-progress":
        done, total = info
        pct = (100 * done / total) if total else 0
        print(f"\r  decrypt {pct:5.1f}%  ({done}/{total} bytes)",
              end="", flush=True)
        if done == total:
            print()
        return
    print(f"  [{stage}] {info}")


def main():
    p = argparse.ArgumentParser(prog="aria", description="Apple Music CLI")
    p.add_argument("--storefront", default="us", help="storefront code (default: us)")
    sub = p.add_subparsers(dest="command")

    # search
    s = sub.add_parser("search", help="Search Apple Music catalog")
    s.add_argument("query", help="search term")
    s.add_argument("--type", default="songs,albums,artists")
    s.add_argument("--limit", type=int, default=10)

    # song
    s = sub.add_parser("song", help="Show song metadata")
    s.add_argument("song_id")

    # download
    s = sub.add_parser("download", help="Download track(s)")
    s.add_argument("song_ids", nargs="+", help="one or more song IDs")
    s.add_argument("--fmt", default="aac", choices=["aac", "alac"])
    s.add_argument("-o", "--output", default="./output")
    s.add_argument("--aria-host", default="127.0.0.1")
    s.add_argument("--decrypt-port", type=int, default=47010)
    s.add_argument("--m3u8-port", type=int, default=47020)

    args = p.parse_args()
    if not args.command:
        p.print_help()
        return 1

    {"search": cmd_search, "song": cmd_song, "download": cmd_download}[args.command](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
