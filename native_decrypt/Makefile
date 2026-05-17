CC = gcc
CFLAGS = -O2 -Wall
LDFLAGS = -lcrypto

all: decrypt_samples

decrypt_samples: decrypt_samples.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f decrypt_samples
