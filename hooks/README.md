# hooks/ — placeholder analysis tooling

> The scripts in this directory are **structural templates only**.
> All target-specific identifiers (library names, function symbols, binary
> paths, offsets, codenames) have been **intentionally redacted** and
> replaced with generic placeholders such as `<TARGET_DRM_LIB>`,
> `<TARGET_DECRYPT_FUNC>`, `<TARGET_APP_LIB>`, `<DRM_BIN>`.

## Files

| File | Purpose | What you need to fill in |
|------|---------|--------------------------|
| `extract_key.js` | Frida hook on `AES_cbc_encrypt` (BoringSSL / OpenSSL) to capture the first key passed to the DRM helper | `<TARGET_DRM_LIB>` / `<DRM_BIN>` (the binary whose `libcrypto.so` you want to attach to) |
| `key_hook.c` | `LD_PRELOAD`-style C interpose of `AES_cbc_encrypt` (notes on why this typically does **not** work against Android `linker64`) | `<TARGET_DECRYPT_FUNC>` and target binary identification |
| `dfa_attack.py` | Differential Fault Analysis sketch — flip random bytes in the loaded library's `.rodata` via `/proc/PID/mem`, observe ciphertext diffs | `<TARGET_DRM_LIB>` name + `.rodata` resolution + sample-injection plumbing |

## Why this is left as an exercise

These scripts were originally written against a specific commercial DRM
helper.  Publishing the target-specific details would carry needless
risk for the author (current employment / non-compete) without adding
real educational value: the techniques are well-known and well
documented elsewhere, and the *interesting* part of any such attack is
target identification and white-box understanding — not the hooking
boilerplate.

If you want to reproduce or extend this work:

- **Identify your own target** — any commercial white-box AES
  implementation you have a legal reason to study.
- **Replace the placeholders** with the matching binary name, symbol,
  rodata layout, etc.
- **Reach out to the author** if you'd like to compare notes on a
  similar target; non-actionable, methodology-level discussion is
  welcome.
