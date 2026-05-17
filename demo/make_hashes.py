#!/usr/bin/env python3
"""Hash + size table for the demo artifacts."""
import hashlib, os, json
from pathlib import Path

rows = []
for p in sorted(Path("audio").iterdir()):
    if not p.is_file(): continue
    data = p.read_bytes()
    sha  = hashlib.sha256(data).hexdigest()
    rows.append((str(p), len(data), sha))

print(f"{'file':<32}{'size':>12}   sha256")
print("-" * 110)
for name, size, sha in rows:
    print(f"{name:<32}{size:>12}   {sha}")

with open("logs/hashes.json", "w") as f:
    json.dump([{"path": n, "size": s, "sha256": h} for n, s, h in rows], f, indent=2)

# 同时输出 GitHub-flavored markdown 表
md = ["| File | Size (bytes) | SHA-256 |", "|------|-------------:|---------|"]
for n, s, h in rows:
    md.append(f"| `{n}` | {s:,} | `{h[:16]}…{h[-8:]}` |")
Path("logs/hashes.md").write_text("\n".join(md) + "\n")
print("\nWrote logs/hashes.json + logs/hashes.md")
