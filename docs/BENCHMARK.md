# Benchmark: sidecar v4 vs wrapper_new

**Test environment**: Dell PowerEdge R730, 80 cores, 94 GB RAM, Ubuntu 22.04
**Date**: 2026-09-25

## Results

| Metric | wrapper_new | sidecar v4 | Winner |
|--------|-------------|------------|--------|
| **Cold startup** | 2338 ms | **2163 ms** | sidecar (-7.5%) |
| **Pipelined decrypt** | 3.18s (15.0 MB/s) | **3.12s** (15.3 MB/s) | tie |
| **Memory (main)** | 53.8 MB | **52.3 MB** | sidecar (-3%) |
| **Binary** | 20 KB | 35 KB | wrapper (closed) |
| **5-track stability** | 5/5 | 5/5 | tie |
| **Source** | closed | 935 lines C | sidecar |
| **PTY output** | no | yes | sidecar |
| **Port readiness** | no | --wait-ports | sidecar |
| **Graceful restart** | no | SIGUSR1 | sidecar |

## Pipeline Breakdown

| Phase | Time | Share |
|-------|------|-------|
| m3u8 resolve | 0.44s | 5% |
| CDN download (47.7 MB) | 1.80s | 22% |
| fMP4 parse (2650 samples) | 45ms | 1% |
| Decrypt (pipelined+NODELAY) | 3.12s | 38% |
| m4a write | ~100ms | 1% |
| Audio verify | ~0.5s | 6% |
| **Total** | **~8.3s** | **100%** |

## Key Optimizations (57x from baseline)

| Optimization | Before | After | Speedup |
|---|---|---|---|
| TCP_NODELAY | ConnectionReset | 4.36s | required |
| Pipelining | 4.36s | 3.12s | 1.4x |
| Streaming BMFF | 179 MB peak | 11 MB | 94% memory |
| sidecar v4 namespace | CLONE_NEWUSER+NS+PID | CLONE_NEWUSER+PID | -7.5% startup |
