# Benchmark: sidecar v3 vs wrapper_new

**Test environment**: Dell PowerEdge R730, 80 cores, 94 GB RAM, Ubuntu 22.04  
**Date**: 2026-09-25  
**Track**: "The Fate of Ophelia" by Taylor Swift (2650 samples, 47.7 MB ALAC 24-bit/48kHz)

## Results

| Metric | wrapper_new | sidecar v3 | Delta |
|--------|-------------|------------|-------|
| **Binary size** | 20 KB | 39 KB | +19 KB (source available) |
| **Cold startup** (median of 3) | 3633 ms | 3391 ms | **-7%** |
| **Decrypt throughput** (median of 5) | 15.0 MB/s | 15.3 MB/s | **+2%** |
| **Decrypt latency** (median) | 3.178s | 3.120s | **-2%** |
| **5-track sequential** | 5/5 | 5/5 | tie |
| **Memory (launcher)** | 1.6 MB | 1.7 MB | +0.1 MB |
| **Memory (main)** | 58.6 MB | 58.5 MB | tie |
| **E2E cold** (server pipeline) | — | 8.8s | — |
| **E2E warm** (cached) | — | 0.5s | — |
| **Source code** | closed binary | 935 lines C (MIT) | — |
| **Root required** | No | No | tie |
| **PTY output** | No | Yes | sidecar only |
| **Port readiness** | No | `--wait-ports` | sidecar only |
| **Graceful restart** | No | SIGUSR1 | sidecar only |
| **DNS seeding** | No | resolv.conf/hosts | sidecar only |
| **JSON events** | No | `--json-events` | sidecar only |
| **PID file** | No | `--pid-file` | sidecar only |

## Methodology

### Test A: Cold Startup
- Kill all processes, wait 2s
- Start binary, poll `ss -tlnp | grep 47010` every 100ms
- Measure wall clock from launch to first port response
- 3 runs per binary, report median

### Test B: Decrypt Throughput
- Pre-download and parse 47.7 MB encrypted M4S (2650 samples)
- Call `aria_rpc.decrypt_samples_pipelined()` with `timeout=600`
- Measure wall clock, compute MB/s
- 5 runs per binary, report median
- Verify: all 2650 samples changed (encrypted ≠ decrypted)

### Test C: Sequential Stability
- 5 different tracks decrypted one after another without process restart
- Each track: m3u8 resolution → download → parse → decrypt → verify
- Pass = all samples changed for all 5 tracks

### Test D: Memory
- `ps aux` RSS measurement after Test C
- Launcher = sidecar/wrapper process; Main = /system/bin/main child

### Test E: End-to-End
- Full aria server pipeline: HTTP API → metadata → HLS → parse → decrypt → m4a write → tag
- Cold = first request (triggers decrypt + cache)
- Warm = second request (served from cache)

## Raw Data

### Startup (ms)
| Run | wrapper_new | sidecar v3 |
|-----|-------------|------------|
| 1 | 3780 | 3761 |
| 2 | 3633 | 2452 |
| 3 | 3363 | 3391 |
| **Median** | **3633** | **3391** |

### Decrypt (seconds / MB/s)
| Run | wrapper_new | sidecar v3 |
|-----|-------------|------------|
| 1 | 3.663s / 13.0 | 3.631s / 13.1 |
| 2 | 3.178s / 15.0 | 3.126s / 15.3 |
| 3 | 3.257s / 14.6 | 3.120s / 15.3 |
| 4 | 3.118s / 15.3 | 3.098s / 15.4 |
| 5 | 3.107s / 15.3 | 3.642s / 13.1 |
| **Median** | **3.178s / 15.0** | **3.120s / 15.3** |

## Conclusion

sidecar v3 matches wrapper_new on all performance metrics while providing significantly better observability, lifecycle management, and maintainability. The decrypt throughput and startup time are statistically equivalent (within noise), confirming that the user namespace isolation adds no measurable overhead.

The key differentiator is not speed — both use the same `/system/bin/main` binary for FairPlay operations. The differentiator is **everything around it**: source availability, PTY output, port readiness detection, graceful restart, DNS seeding, and structured events — capabilities that matter in production deployments.
