/*
 * Frida hook for aria (`<DRM_BIN>`) — extract AES-128-CBC content key
 *
 * Hooks AES_cbc_encrypt in libcrypto.so to capture the key when aria
 * processes the first sample decryption. Once captured, the key is printed
 * to stdout as hex, then all subsequent samples can be decrypted locally
 * without going through the TCP protocol.
 *
 * Usage:
 *   # Start aria in one terminal
 *   ./aria -D 10020 -M 20020 -H 127.0.0.1
 *
 *   # Attach frida in another
 *   frida -n main -l extract_key.js
 *
 *   # Trigger a decryption (send one sample via TCP)
 *   python3 -c "from am_alac.aria_rpc import *; ..."
 *
 * AES_cbc_encrypt signature (BoringSSL/OpenSSL):
 *   void AES_cbc_encrypt(const uint8_t *in, uint8_t *out, size_t len,
 *                        const AES_KEY *key, uint8_t ivec[16], int enc);
 *
 *   key->rd_key[0..43] contains the expanded key schedule (176 bytes for AES-128)
 *   We extract the first 16 bytes of rd_key = original AES-128 key
 *
 *   enc == 0 means DECRYPT (what we want to intercept)
 */

'use strict';

const libcrypto = Module.findBaseAddress('libcrypto.so');
if (!libcrypto) {
    console.error('[!] libcrypto.so not found');
} else {
    console.log('[*] libcrypto.so base:', libcrypto);

    const AES_cbc_encrypt = Module.findExportByName('libcrypto.so', 'AES_cbc_encrypt');
    if (!AES_cbc_encrypt) {
        console.error('[!] AES_cbc_encrypt not found');
    } else {
        console.log('[*] AES_cbc_encrypt:', AES_cbc_encrypt);

        let keyExtracted = false;

        Interceptor.attach(AES_cbc_encrypt, {
            onEnter(args) {
                const inPtr = args[0];
                const outPtr = args[1];
                const len = args[2].toInt32();
                const keyPtr = args[3];    // AES_KEY struct
                const ivPtr = args[4];
                const enc = args[5].toInt32();

                if (enc === 0 && !keyExtracted) {
                    // DECRYPT mode — extract the key
                    // AES_KEY struct: first field is uint32_t rd_key[4*(rounds+1)]
                    // For AES-128: rounds=10, rd_key has 44 uint32_t = 176 bytes
                    // The original 16-byte key is the first 16 bytes of rd_key
                    const rawKey = keyPtr.readByteArray(16);
                    const iv = ivPtr.readByteArray(16);

                    console.log('\n[KEY EXTRACTED]');
                    console.log('  AES-128 key: ' + hexDump(rawKey, 16));
                    console.log('  IV:          ' + hexDump(iv, 16));
                    console.log('  data length: ' + len);
                    console.log('  direction:   DECRYPT');

                    // Also try to recover the original key from the expanded schedule
                    // For AES-128, the first 16 bytes of the schedule ARE the key
                    const keyHex = Array.from(new Uint8Array(rawKey))
                        .map(b => b.toString(16).padStart(2, '0')).join('');
                    const ivHex = Array.from(new Uint8Array(iv))
                        .map(b => b.toString(16).padStart(2, '0')).join('');

                    console.log('\n  === COPY THESE FOR LOCAL DECRYPTION ===');
                    console.log('  KEY=' + keyHex);
                    console.log('  IV=' + ivHex);

                    keyExtracted = true;
                }
            }
        });

        console.log('[*] Hook installed. Send a decrypt request to capture key.');
    }
}

function hexDump(buf, len) {
    return Array.from(new Uint8Array(buf.slice(0, len)))
        .map(b => b.toString(16).padStart(2, '0')).join(' ');
}
