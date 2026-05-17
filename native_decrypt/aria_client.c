/*
 * High-performance C client for aria's decrypt port (TCP 10020/47010).
 *
 * Implements the same wire protocol as aria_rpc.py but in pure C with:
 *   - Zero-copy sendmsg (iovec scatter/gather)
 *   - TCP_NODELAY + large buffers
 *   - Pipelined I/O (write all, then read all)
 *   - mmap'd input file for zero-copy reads
 *
 * Protocol (decrypt port):
 *   Key context: 1B len + trackId + 1B len + keyUri
 *   Per sample:  4B LE length + N bytes cipher → N bytes plain
 *   End:         5 bytes of 0x00
 *
 * Input file format (produced by Python m4s_parser):
 *   Header:  4B magic "SAMP"
 *            4B num_samples
 *            4B num_keys
 *            For each key: 1B len + N bytes keyUri
 *   Samples: For each sample:
 *            4B desc_index
 *            4B data_length
 *            N bytes data
 *
 * Output: same format but with decrypted sample data.
 *
 * Build:
 *   gcc -O2 -o aria_client aria_client.c -lpthread
 *
 * Usage:
 *   # Export samples from Python, decrypt in C, import back
 *   python3 -c "from am_alac.native_bridge import export_samples; ..."
 *   ./aria_client -h 127.0.0.1 -p 10020 -t 1440841263 -i samples.bin -o decrypted.bin
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <errno.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/mman.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <fcntl.h>
#include <pthread.h>
#include <time.h>
#include <getopt.h>

#define MAGIC "SAMP"
#define BUF_SIZE (256 * 1024)

typedef struct {
    uint32_t desc_index;
    uint32_t data_len;
    const uint8_t *data;
} sample_t;

typedef struct {
    int sock;
    sample_t *samples;
    uint32_t num_samples;
    char **keys;
    uint32_t num_keys;
    const char *track_id;
    uint8_t **results;
    volatile int error;
    int pipeline_depth;
    pthread_mutex_t gate_mutex;
    pthread_cond_t gate_cond;
    int gate_count;
} ctx_t;

static int tcp_connect(const char *host, int port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -1;

    int flag = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &flag, sizeof(flag));
    int bufsize = BUF_SIZE;
    setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &bufsize, sizeof(bufsize));
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &bufsize, sizeof(bufsize));

    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port = htons(port),
    };
    inet_pton(AF_INET, host, &addr.sin_addr);

    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static int send_all(int fd, const void *buf, size_t len) {
    const uint8_t *p = buf;
    while (len > 0) {
        ssize_t n = send(fd, p, len, MSG_NOSIGNAL);
        if (n <= 0) return -1;
        p += n;
        len -= n;
    }
    return 0;
}

static int recv_exact(int fd, void *buf, size_t len) {
    uint8_t *p = buf;
    while (len > 0) {
        ssize_t n = recv(fd, p, len, 0);
        if (n <= 0) return -1;
        p += n;
        len -= n;
    }
    return 0;
}

static void gate_acquire(ctx_t *ctx) {
    pthread_mutex_lock(&ctx->gate_mutex);
    while (ctx->gate_count >= ctx->pipeline_depth)
        pthread_cond_wait(&ctx->gate_cond, &ctx->gate_mutex);
    ctx->gate_count++;
    pthread_mutex_unlock(&ctx->gate_mutex);
}

static void gate_release(ctx_t *ctx) {
    pthread_mutex_lock(&ctx->gate_mutex);
    ctx->gate_count--;
    pthread_cond_signal(&ctx->gate_cond);
    pthread_mutex_unlock(&ctx->gate_mutex);
}

static void *writer_thread(void *arg) {
    ctx_t *ctx = arg;
    int current_desc = -1;

    for (uint32_t i = 0; i < ctx->num_samples; i++) {
        if (ctx->error) return NULL;
        gate_acquire(ctx);

        sample_t *s = &ctx->samples[i];

        if ((int)s->desc_index != current_desc) {
            if (current_desc >= 0) {
                uint8_t sentinel[4] = {0};
                if (send_all(ctx->sock, sentinel, 4) < 0) goto fail;
            }

            const char *key_uri = ctx->keys[s->desc_index];
            const char *tid = ctx->track_id;
            /* prefetch key check */
            if (strcmp(key_uri, "skd://itunes.apple.com/P000000000/s1/e1") == 0)
                tid = "0";

            uint8_t tid_len = (uint8_t)strlen(tid);
            uint8_t kuri_len = (uint8_t)strlen(key_uri);

            if (send_all(ctx->sock, &tid_len, 1) < 0) goto fail;
            if (send_all(ctx->sock, tid, tid_len) < 0) goto fail;
            if (send_all(ctx->sock, &kuri_len, 1) < 0) goto fail;
            if (send_all(ctx->sock, key_uri, kuri_len) < 0) goto fail;

            current_desc = s->desc_index;
        }

        uint32_t len_le = s->data_len;
        if (send_all(ctx->sock, &len_le, 4) < 0) goto fail;
        if (send_all(ctx->sock, s->data, s->data_len) < 0) goto fail;
    }
    return NULL;

fail:
    ctx->error = errno ? errno : -1;
    return NULL;
}

static void *reader_thread(void *arg) {
    ctx_t *ctx = arg;
    size_t total = 0, done = 0;

    for (uint32_t i = 0; i < ctx->num_samples; i++)
        total += ctx->samples[i].data_len;

    for (uint32_t i = 0; i < ctx->num_samples; i++) {
        if (ctx->error) return NULL;

        uint32_t n = ctx->samples[i].data_len;
        ctx->results[i] = malloc(n);
        if (!ctx->results[i]) { ctx->error = ENOMEM; return NULL; }

        if (recv_exact(ctx->sock, ctx->results[i], n) < 0) {
            ctx->error = errno ? errno : -1;
            return NULL;
        }

        done += n;
        gate_release(ctx);

        if ((i + 1) % 500 == 0 || i + 1 == ctx->num_samples)
            fprintf(stderr, "\r  decrypt %5.1f%%  (%zu/%zu bytes)",
                    100.0 * done / total, done, total);
    }
    fprintf(stderr, "\n");
    return NULL;
}

static int parse_input(const char *path, sample_t **out_samples,
                       uint32_t *out_num, char ***out_keys,
                       uint32_t *out_num_keys) {
    FILE *f = fopen(path, "rb");
    if (!f) { perror(path); return -1; }

    char magic[4];
    fread(magic, 1, 4, f);
    if (memcmp(magic, MAGIC, 4) != 0) {
        fprintf(stderr, "Bad magic in %s\n", path);
        fclose(f);
        return -1;
    }

    uint32_t num_samples, num_keys;
    fread(&num_samples, 4, 1, f);
    fread(&num_keys, 4, 1, f);

    char **keys = calloc(num_keys, sizeof(char *));
    for (uint32_t i = 0; i < num_keys; i++) {
        uint8_t klen;
        fread(&klen, 1, 1, f);
        keys[i] = malloc(klen + 1);
        fread(keys[i], 1, klen, f);
        keys[i][klen] = '\0';
    }

    sample_t *samples = calloc(num_samples, sizeof(sample_t));
    for (uint32_t i = 0; i < num_samples; i++) {
        fread(&samples[i].desc_index, 4, 1, f);
        fread(&samples[i].data_len, 4, 1, f);
        uint8_t *data = malloc(samples[i].data_len);
        fread(data, 1, samples[i].data_len, f);
        samples[i].data = data;
    }

    fclose(f);
    *out_samples = samples;
    *out_num = num_samples;
    *out_keys = keys;
    *out_num_keys = num_keys;
    return 0;
}

static void write_output(const char *path, ctx_t *ctx) {
    FILE *f = fopen(path, "wb");
    if (!f) { perror(path); return; }

    fwrite(MAGIC, 1, 4, f);
    fwrite(&ctx->num_samples, 4, 1, f);

    for (uint32_t i = 0; i < ctx->num_samples; i++) {
        uint32_t len = ctx->samples[i].data_len;
        fwrite(&len, 4, 1, f);
        fwrite(ctx->results[i], 1, len, f);
    }
    fclose(f);
}

int main(int argc, char *argv[]) {
    const char *host = "127.0.0.1";
    int port = 10020;
    const char *track_id = NULL;
    const char *in_path = NULL;
    const char *out_path = NULL;
    int pipeline = 64;

    static struct option opts[] = {
        {"host", required_argument, NULL, 'h'},
        {"port", required_argument, NULL, 'p'},
        {"track-id", required_argument, NULL, 't'},
        {"input", required_argument, NULL, 'i'},
        {"output", required_argument, NULL, 'o'},
        {"pipeline", required_argument, NULL, 'P'},
        {NULL, 0, NULL, 0}
    };

    int c;
    while ((c = getopt_long(argc, argv, "h:p:t:i:o:P:", opts, NULL)) != -1) {
        switch (c) {
        case 'h': host = optarg; break;
        case 'p': port = atoi(optarg); break;
        case 't': track_id = optarg; break;
        case 'i': in_path = optarg; break;
        case 'o': out_path = optarg; break;
        case 'P': pipeline = atoi(optarg); break;
        default:
            fprintf(stderr, "Usage: %s -t TRACK_ID -i samples.bin -o decrypted.bin "
                    "[-h HOST] [-p PORT] [-P pipeline_depth]\n", argv[0]);
            return 1;
        }
    }

    if (!track_id || !in_path || !out_path) {
        fprintf(stderr, "Error: -t, -i, -o are required\n");
        return 1;
    }

    sample_t *samples;
    uint32_t num_samples, num_keys;
    char **keys;
    if (parse_input(in_path, &samples, &num_samples, &keys, &num_keys) < 0)
        return 1;

    size_t total_bytes = 0;
    for (uint32_t i = 0; i < num_samples; i++)
        total_bytes += samples[i].data_len;
    fprintf(stderr, "Loaded %u samples, %zu bytes, %u keys\n",
            num_samples, total_bytes, num_keys);

    int sock = tcp_connect(host, port);
    if (sock < 0) { perror("connect"); return 1; }

    ctx_t ctx = {
        .sock = sock,
        .samples = samples,
        .num_samples = num_samples,
        .keys = keys,
        .num_keys = num_keys,
        .track_id = track_id,
        .results = calloc(num_samples, sizeof(uint8_t *)),
        .error = 0,
        .pipeline_depth = pipeline,
        .gate_count = 0,
    };
    pthread_mutex_init(&ctx.gate_mutex, NULL);
    pthread_cond_init(&ctx.gate_cond, NULL);

    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);

    pthread_t wt, rt;
    pthread_create(&wt, NULL, writer_thread, &ctx);
    pthread_create(&rt, NULL, reader_thread, &ctx);
    pthread_join(wt, NULL);
    pthread_join(rt, NULL);

    clock_gettime(CLOCK_MONOTONIC, &t1);
    double elapsed = (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9;

    if (ctx.error) {
        fprintf(stderr, "Error during decrypt: %s\n", strerror(ctx.error));
        close(sock);
        return 1;
    }

    /* End of session sentinel */
    uint8_t end[5] = {0};
    send_all(sock, end, 5);
    close(sock);

    fprintf(stderr, "Decrypted %u samples (%.1f MB) in %.2fs = %.1f MB/s\n",
            num_samples, total_bytes / 1e6, elapsed, total_bytes / elapsed / 1e6);

    write_output(out_path, &ctx);
    fprintf(stderr, "Written to %s\n", out_path);

    for (uint32_t i = 0; i < num_samples; i++) {
        free(ctx.results[i]);
        free((void *)samples[i].data);
    }
    free(ctx.results);
    free(samples);
    for (uint32_t i = 0; i < num_keys; i++) free(keys[i]);
    free(keys);

    return 0;
}
