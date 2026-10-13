CC=gcc
CFLAGS=-Wall -Wextra -O2 $(shell pkg-config --cflags gtk+-3.0)
LDFLAGS=$(shell pkg-config --libs gtk+-3.0) -pthread -lm

# Optional C prototype. The app that launches is ./anything-recorder (Python).
anything-recorder-c: main.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f anything-recorder-c

.PHONY: clean
