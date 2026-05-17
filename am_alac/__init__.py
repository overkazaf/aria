"""am_alac — clean-room Python re-implementation of an Apple Music
ALAC download / decryption orchestration layer.

Scope
=====
This package is an independent Python implementation of the orchestration
pipeline only. The actual FairPlay decryption is delegated, over loopback
TCP, to a separately-built DRM service (referred to here as **aria**).
That service's internals are intentionally NOT shipped in this repository.

Pipeline
========
1. apple_api.get_song_meta(adam_id)              → SongMeta
2. apple_api.get_album_meta(album_id)            → AlbumMeta
3. aria_rpc.fetch_master_playlist(adam_id)       → URL of enhanced HLS master
4. m3u8_select.pick_alac_variant(...)            → (stream_url, [skd_keys])
5. m4s_parser.fetch_and_parse(stream_url)        → list[Sample]
6. aria_rpc.decrypt_samples(samples, keys, ...)  → list[bytes]
7. m4a_writer.patch_in_place(input_m4s, ...)     → output m4a
"""

__version__ = "0.1.0"
