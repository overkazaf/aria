/*
 * LD_PRELOAD hook to intercept AES_cbc_encrypt and extract the key.
 *
 * This approach bypasses Frida entirely — we interpose the OpenSSL/BoringSSL
 * AES_cbc_encrypt function and log the key on first DECRYPT call.
 *
 * Build (on the target Linux host):
 *   gcc -shared -fPIC -o key_hook.so key_hook.c -ldl
 *
 * Usage:
 *   LD_PRELOAD=./key_hook.so ./aria -D 10020 -M 20020 -H 127.0.0.1
 *   # Then trigger a decrypt request; key will be written to /tmp/aes_key.hex
 *
 * PROBLEM: LD_PRELOAD won't work here because aria uses Android linker64
 * (not glibc ld.so), so it doesn't honor LD_PRELOAD for the chroot process.
 *
 * ALTERNATIVE: We use ptrace-based injection or read /proc/PID/mem directly.
 */

/* Since LD_PRELOAD won't work with Android linker, we use a different approach:
 * Read the process memory via /proc/PID/mem to find the AES key after the first
 * decryption. The key resides in the AES_KEY struct which is allocated on the
 * heap or stack during <TARGET_DECRYPT_FUNC>. */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>
#include <stdint.h>

/* Original AES_cbc_encrypt signature */
typedef void (*aes_cbc_encrypt_fn)(
    const uint8_t *in, uint8_t *out, size_t len,
    const void *key, uint8_t *ivec, int enc);

static aes_cbc_encrypt_fn real_aes_cbc_encrypt = NULL;
static int key_captured = 0;

void AES_cbc_encrypt(
    const uint8_t *in, uint8_t *out, size_t len,
    const void *key, uint8_t *ivec, int enc)
{
    if (!real_aes_cbc_encrypt) {
        real_aes_cbc_encrypt = (aes_cbc_encrypt_fn)dlsym(RTLD_NEXT, "AES_cbc_encrypt");
        if (!real_aes_cbc_encrypt) {
            fprintf(stderr, "[key_hook] FATAL: cannot find real AES_cbc_encrypt\n");
            abort();
        }
    }

    /* enc == 0 means DECRYPT */
    if (enc == 0 && !key_captured) {
        /* AES_KEY struct: first 16 bytes of rd_key = original AES-128 key */
        const uint8_t *raw = (const uint8_t *)key;
        FILE *f = fopen("/tmp/aes_key.hex", "w");
        if (f) {
            fprintf(f, "KEY=");
            for (int i = 0; i < 16; i++) fprintf(f, "%02x", raw[i]);
            fprintf(f, "\nIV=");
            for (int i = 0; i < 16; i++) fprintf(f, "%02x", ivec[i]);
            fprintf(f, "\nLEN=%zu\n", len);
            fclose(f);
        }
        fprintf(stderr, "\n[key_hook] === AES-128 KEY CAPTURED ===\n");
        fprintf(stderr, "[key_hook] KEY=");
        for (int i = 0; i < 16; i++) fprintf(stderr, "%02x", raw[i]);
        fprintf(stderr, "\n[key_hook] IV=");
        for (int i = 0; i < 16; i++) fprintf(stderr, "%02x", ivec[i]);
        fprintf(stderr, "\n[key_hook] Written to /tmp/aes_key.hex\n\n");
        key_captured = 1;
    }

    real_aes_cbc_encrypt(in, out, len, key, ivec, enc);
}
