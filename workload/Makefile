# Build the demo workload for QNX 8.0 with the QNX SDP toolchain.
# On a Mac:  docker/sdp.sh sh bash -c ". /root/qnx800/qnxsdp-env.sh; make -C workload"
# Other target: add ARCH=x86_64 to the make command.
ARCH ?= aarch64le
CC = qcc -Vgcc_nto$(ARCH)
CFLAGS ?= -O2 -g -Wall -Wextra
OUT = build/$(ARCH)

all: $(OUT)/rtdemo

$(OUT)/rtdemo: rtdemo.c
	mkdir -p $(OUT)
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -rf build

.PHONY: all clean
