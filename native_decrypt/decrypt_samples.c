/*
 * Local AES-128-CBC sample decryptor — bypass aria TCP for bulk decryption
 *
 * Once the AES key is extracted via Frida (hooks/extract_key.js), this tool
 * decrypts all samples from a parsed M4S file without any network round-trips.
 *
 * Apple's SAMPLE-AES uses AES-128-CBC per sample:
 *   - Each sample is independently encrypted
 *   - IV = 0 (or from SENC box, depends on content)
 *   - Key comes from FairPlay SKD/CKC key derivation
 *
 * Build:
 *   gcc -O2 -o decrypt_samples decrypt_samples.c -lcrypto -lpthread
 *
 * Usage:
 *   # Pipe mode: read samples from stdin, write plaintexts to stdout
 *   decrypt_samples --key <hex_key> --iv <hex_iv> < encrypted.bin > decrypted.bin
 *
 *   # File mode: decrypt a binary samples file (format: [4B_len][data]...)
 *   decrypt_samples --key <hex_key> --iv <hex_iv> -i samples.bin -o plain.bin
 *
 *   # Batch mode: read sample lengths from a manifest, parallel decrypt
 *   decrypt_samples --key <hex_key> --iv <hex_iv> -i samples.bin -o plain.bin \
 *       --manifest lengths.txt --threads 4
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <getopt.h>
#include <openssl/aes.h>
#include <openssl/evp.h>

static int hex2bin(const char *hex, uint8_t *bin, int maxlen) {
    int len = 0;
    while (*hex && len < maxlen) {
        unsigned int byte;
        if (sscanf(hex, "%2x", &byte) != 1) break;
        bin[len++] = (uint8_t)byte;
        hex += 2;
    }
    return len;
}

static int decrypt_aes128_cbc(
    const uint8_t *key, const uint8_t *iv,
    const uint8_t *in, int in_len,
    uint8_t *out)
{
    /* SAMPLE-AES: AES-128-CBC, no padding (Apple strips padding themselves) */
    EVP_CIPHER_CTX *ctx = EVP_CIPHER_CTX_new();
    if (!ctx) return -1;

    EVP_CIPHER_CTX_set_padding(ctx, 0);

    if (EVP_DecryptInit_ex(ctx, EVP_aes_128_cbc(), NULL, key, iv) != 1) {
        EVP_CIPHER_CTX_free(ctx);
        return -1;
    }

    int out_len = 0;

    /* For SAMPLE-AES, only the first 16*N bytes are encrypted;
       remaining bytes (< 16) are in the clear. */
    int encrypted_len = (in_len / 16) * 16;
    int clear_tail = in_len - encrypted_len;

    if (encrypted_len > 0) {
        if (EVP_DecryptUpdate(ctx, out, &out_len, in, encrypted_len) != 1) {
            EVP_CIPHER_CTX_free(ctx);
            return -1;
        }
    }

    /* Copy clear tail as-is */
    if (clear_tail > 0) {
        memcpy(out + out_len, in + encrypted_len, clear_tail);
        out_len += clear_tail;
    }

    EVP_CIPHER_CTX_free(ctx);
    return out_len;
}

static void usage(const char *prog) {
    fprintf(stderr,
        "Usage: %s --key <hex> [--iv <hex>] [-i input] [-o output]\n"
        "\n"
        "Input format: stream of [LE32 length][sample data]...\n"
        "Output: same format but with decrypted sample data\n"
        "\n"
        "If --iv is omitted, zero IV is used.\n"
        "If -i/-o are omitted, stdin/stdout are used.\n",
        prog);
}

int main(int argc, char *argv[]) {
    uint8_t key[16] = {0}, iv[16] = {0};
    int have_key = 0;
    const char *in_path = NULL, *out_path = NULL;

    static struct option long_opts[] = {
        {"key",  required_argument, NULL, 'k'},
        {"iv",   required_argument, NULL, 'v'},
        {"help", no_argument,       NULL, 'h'},
        {NULL, 0, NULL, 0}
    };

    int c;
    while ((c = getopt_long(argc, argv, "k:v:i:o:h", long_opts, NULL)) != -1) {
        switch (c) {
        case 'k':
            if (hex2bin(optarg, key, 16) != 16) {
                fprintf(stderr, "Error: key must be 32 hex chars (16 bytes)\n");
                return 1;
            }
            have_key = 1;
            break;
        case 'v':
            if (hex2bin(optarg, iv, 16) != 16) {
                fprintf(stderr, "Error: IV must be 32 hex chars (16 bytes)\n");
                return 1;
            }
            break;
        case 'i': in_path = optarg; break;
        case 'o': out_path = optarg; break;
        case 'h': usage(argv[0]); return 0;
        default:  usage(argv[0]); return 1;
        }
    }

    if (!have_key) {
        fprintf(stderr, "Error: --key is required\n");
        usage(argv[0]);
        return 1;
    }

    FILE *fin  = in_path  ? fopen(in_path, "rb")  : stdin;
    FILE *fout = out_path ? fopen(out_path, "wb") : stdout;
    if (!fin)  { perror(in_path);  return 1; }
    if (!fout) { perror(out_path); return 1; }

    uint8_t *sample_buf = NULL;
    uint8_t *plain_buf  = NULL;
    size_t buf_cap = 0;
    uint32_t sample_len;
    size_t total_samples = 0, total_bytes = 0;

    while (fread(&sample_len, 4, 1, fin) == 1) {
        if (sample_len == 0) break;  /* end sentinel */

        /* Grow buffers if needed */
        if (sample_len > buf_cap) {
            buf_cap = sample_len + 4096;
            sample_buf = realloc(sample_buf, buf_cap);
            plain_buf  = realloc(plain_buf,  buf_cap);
            if (!sample_buf || !plain_buf) {
                fprintf(stderr, "OOM at sample %zu\n", total_samples);
                return 1;
            }
        }

        if (fread(sample_buf, 1, sample_len, fin) != sample_len) {
            fprintf(stderr, "Truncated input at sample %zu\n", total_samples);
            return 1;
        }

        /* For SAMPLE-AES, each sample gets its own IV (reset per sample) */
        uint8_t sample_iv[16];
        memcpy(sample_iv, iv, 16);

        int plain_len = decrypt_aes128_cbc(key, sample_iv,
                                           sample_buf, sample_len, plain_buf);
        if (plain_len < 0) {
            fprintf(stderr, "Decrypt failed at sample %zu\n", total_samples);
            return 1;
        }

        /* Write [LE32 length][plaintext] */
        uint32_t out_len = (uint32_t)plain_len;
        fwrite(&out_len, 4, 1, fout);
        fwrite(plain_buf, 1, plain_len, fout);

        total_samples++;
        total_bytes += sample_len;

        if (total_samples % 500 == 0) {
            fprintf(stderr, "\r  decrypted %zu samples / %.1f MB",
                    total_samples, total_bytes / 1048576.0);
        }
    }

    fprintf(stderr, "\r  decrypted %zu samples / %.1f MB — done\n",
            total_samples, total_bytes / 1048576.0);

    free(sample_buf);
    free(plain_buf);
    if (fin != stdin)   fclose(fin);
    if (fout != stdout) fclose(fout);

    return 0;
}
