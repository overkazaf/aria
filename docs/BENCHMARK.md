# Benchmark: sidecar v3 vs wrapper_new

**Environment**: Dell PowerEdge R730, 80 cores (Xeon E5-2698 v4), 94 GB RAM, Ubuntu 22.04  
**Track**: "The Fate of Ophelia" — Taylor Swift (2650 samples, 47.7 MB ALAC 24-bit/48kHz)  
**Date**: 2026-09-25

## Pipeline Breakdown

Both sidecar and wrapper execute the same `/system/bin/main` binary for FairPlay decryption. The differences are in the launcher overhead and lifecycle management.

| Phase | sidecar v3 | wrapper_new | Notes |
|-------|-----------|-------------|-------|
| **Cold startup** | 3391 ms | 2338 ms | wrapper is 31% faster (no PTY, simpler namespace) |
| CDN download (47.7 MB) | 1.80s | 1.80s | Same (Apple CDN, independent of launcher) |
| fMP4 parse (2650 samples) | 45 ms | 45 ms | Same (Python m4s_parser) |
| **Decrypt serial+NODELAY** | 4.36s (10.9 MB/s) | 3.51s (13.6 MB/s) | wrapper 20% faster (less PTY/namespace overhead on TCP path) |
| **Decrypt pipelined+NODELAY** | 3.12s (15.3 MB/s) | 3.18s (15.0 MB/s) | Effectively tied (~2% sidecar edge, within variance) |
| Pipelining speedup | 1.4x | — | Serial→pipelined improvement |
| m4a write (in-place patch) | ~100 ms | ~100 ms | Same |
| Audio verify (ffmpeg) | ~0.5s | ~0.5s | Same |
| **Total E2E** (warm cache) | 0.5s | 0.5s | Same (file cached) |
| **Total E2E** (cold) | ~8.8s | ~8.5s | Comparable |

## TCP Optimization Impact

| Mode | Time | Throughput | Status |
|------|------|-----------|--------|
| **No TCP_NODELAY** (Nagle default) | ConnectionReset | — | main times out waiting for data |
| **TCP_NODELAY only** (serial) | 3.51-4.36s | 10.9-13.6 MB/s | Works but suboptimal |
| **TCP_NODELAY + pipelining** | 3.12-3.18s | 15.0-15.3 MB/s | Optimal — 57x vs original Go impl |

TCP_NODELAY is **essential** — without it, Nagle algorithm delays small packets (sample headers ~4 bytes) causing main to timeout and reset the connection. The original Go implementation (193s) suffered from this plus lack of pipelining.

## Resource Usage

| Metric | sidecar v3 | wrapper_new |
|--------|-----------|-------------|
| Binary size | 39 KB | 20 KB |
| Launcher RSS | 1.7 MB | 1.6 MB |
| main RSS | 58.5 MB | 52.2 MB |
| Total RSS | 60.2 MB | 53.8 MB |

## Stability

| Test | sidecar v3 | wrapper_new |
|------|-----------|-------------|
| 5-track sequential | 5/5 pass | 5/5 pass |
| Multi-track (19 tracks) | 19/19 pass | — |
| Crash recovery | Auto-restart (SIGUSR1) | Process exits |

## Feature Comparison

| Capability | wrapper_new | sidecar v3 |
|-----------|-------------|------------|
| Root required | No | **No** |
| Source available | Closed binary | **935 lines C (open)** |
| Output visibility | Fully buffered | **PTY line-buffered** |
| Port readiness | None | **--wait-ports** |
| Graceful restart | Not supported | **SIGUSR1** |
| DNS bootstrap | None | **Auto-seed** |
| JSON events | None | **--json-events** |
| Grace shutdown | Immediate kill | **Configurable --grace-secs** |
| Multi-generation | Process dies | **In-process restart** |
| systemd ready-fd | None | **--ready-fd** |

## Conclusion

**Performance**: wrapper_new has a slight edge in cold startup (-31%) and serial decrypt (-20%) due to its leaner namespace setup and absence of PTY. In pipelined mode (the production path), both are effectively identical (~15 MB/s, within measurement variance).

**Architecture**: sidecar v3's advantages are in operability — PTY visibility, port readiness detection, graceful restart, and open source code. These don't show up in microbenchmarks but matter significantly in production (crash recovery, debugging, monitoring integration).

**The real speedup**: The 57x optimization (193s → 3.4s) comes from TCP_NODELAY + pipelining in `aria_rpc.py`, not from the launcher. Both sidecar and wrapper benefit equally from this optimization.

## Methodology

- Each test run on a quiescent system (no competing workloads)
- Cold startup: `date +%s%N` before launch, poll `ss -tlnp` until port appears
- Decrypt times: `time.time()` around `aria_rpc.decrypt_samples_pipelined()` / manual serial loop
- Memory: `ps aux` RSS column
- 3-5 runs per test, reporting median or best as noted
- Same encrypted stream pre-downloaded for all decrypt tests (eliminates CDN variance)
