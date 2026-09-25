# Performance Benchmark: sidecar vs wrapper

**Date**: 2026-09-25  
**Machine**: Dell workstation, 80 cores, Ubuntu 22.04 x86_64  
**Track**: "The Fate of Ophelia" by Taylor Swift (2650 samples, 47.7 MB ALAC 24-bit/48kHz)

## Binary Comparison

| Metric | wrapper_new | sidecar_v2 |
|--------|-------------|------------|
| Binary size | 20,464 bytes | 39,584 bytes |
| Source lines | N/A (closed) | 924 lines C |
| Compiler | Android clang 12.0.8 | GCC 14.2.0 |
| Type | ELF executable | ELF PIE executable |
| Stripped | No | No |

## Startup Time (cold start, ports ready)

| Run | wrapper_new | 
|-----|-------------|
| 1 | 2,455 ms |
| 2 | 2,448 ms |
| 3 | 2,446 ms |
| **Average** | **2,450 ms** |

> **Note**: sidecar_v2 in `--userns` mode with `--no-pty` currently has a startup issue where the main binary hangs during re-initialization inside the user namespace. When using pre-populated auth files (after wrapper has run once), sidecar_v2 starts in ~4-5s. This is being investigated — the root cause is the `openpty()` call failing inside user namespace due to missing `/dev/pts`, and the main binary's re-init behavior differing under `CLONE_NEWNS`.

## Decrypt Throughput (wrapper_new, non-persistent connections)

| Run | Samples | Size | Time | Throughput | Changed |
|-----|---------|------|------|------------|---------|
| 1 | 2,650 | 47.7 MB | 3.672s | 13.0 MB/s | 2650/2650 |
| 2 | 2,650 | 47.7 MB | 3.144s | 15.2 MB/s | 2650/2650 |
| 3 | 2,650 | 47.7 MB | 3.139s | 15.2 MB/s | 2650/2650 |
| **Average** | | | **3.318s** | **14.5 MB/s** | |

## Sequential Multi-Track Stability (5 tracks, no restart)

| Track ID | Title | Samples | Size | Time | Result |
|----------|-------|---------|------|------|--------|
| 1850496033 | The Fate of Ophelia | 2,650 | 47.7 MB | 3.11s | ✓ |
| 1850496037 | Elizabeth Taylor | 2,441 | 43.2 MB | 2.84s | ✓ |
| 1850496038 | Opalite | 2,759 | 52.0 MB | 3.40s | ✓ |
| 1850496249 | Father Figure | 2,494 | 43.4 MB | 2.96s | ✓ |
| 1850496250 | Eldest Daughter | 2,887 | 49.3 MB | 3.22s | ✓ |
| **Total** | | **13,231** | **235.6 MB** | **15.53s** | **5/5 ✓** |

Average: 3.11s/track, 15.2 MB/s throughput

## Memory Usage (RSS)

| Component | RSS |
|-----------|-----|
| wrapper_new launcher | 1.6 MB |
| main (decrypt daemon) | 52.1 MB |
| sidecar_v2 launcher | 1.7 MB |
| **Total per instance** | **~54 MB** |

## Known Issues

1. **sidecar_v2 + userns PTY**: `openpty()` fails with "No such device" inside user namespace because `/dev/pts` devpts mount is not available. Workaround: use `--no-pty` flag. Fix: mount devpts inside namespace or skip PTY in userns mode automatically.

2. **sidecar_v2 re-init hang**: When sidecar_v2 triggers main's re-initialization inside a user namespace + mount namespace, the main binary hangs during auth state generation. wrapper_new avoids this by only using `CLONE_NEWUSER` (no `CLONE_NEWNS`), letting bind-mounts be done externally with sudo.

3. **DNS seeding permission**: sidecar_v2 tries to copy `/etc/resolv.conf` into rootfs before namespace setup but fails with "Permission denied" if rootfs/etc is owned by root. Workaround: manually `sudo cp` DNS files, or ensure rootfs/etc is writable.
