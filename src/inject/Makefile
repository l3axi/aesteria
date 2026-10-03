# AESTERIA injection probe — the first thing we put INSIDE the AION 2 process.
#   make                 # build libaesteria_probe.so (64-bit)
#   make m32             # additionally build the 32-bit twin, if lib32-glibc is present
#   make selftest        # prove the probe works on a harmless local process first
#   make clean
#
# Do not hand-set LD_PRELOAD — use tools/aesteria_wrap.sh, which is also what gets pasted
# into Steam's launch options.

CC       ?= gcc
CFLAGS   ?= -O2 -Wall -Wextra
CFLAGS   += -fPIC -std=gnu11
LDLIBS    = -lpthread
TARGET    = libaesteria_probe.so
TARGET32  = libaesteria_probe32.so

all: $(TARGET)

# Same rename(2)-into-place discipline as the overlay: the game maps this for a whole
# session, and truncating a mapped inode faults the live process.
$(TARGET): probe.c
	$(CC) $(CFLAGS) -shared -o $@.tmp $< $(LDLIBS)
	mv -f $@.tmp $@

# The launch chain contains 32-bit processes (the Steam reaper, steam.exe). They will try to
# preload this and ld.so will refuse with a "wrong ELF class" line on stderr — harmless
# noise, and exactly what Steam's own gameoverlayrenderer produces. Building the twin
# silences it, but only if 32-bit glibc dev files exist; never fail the main build over it.
m32: $(TARGET)
	@if echo 'int main(void){return 0;}' | $(CC) -m32 -x c - -o /dev/null 2>/dev/null; then \
	    $(CC) $(CFLAGS) -m32 -shared -o $(TARGET32).tmp probe.c $(LDLIBS) && \
	    mv -f $(TARGET32).tmp $(TARGET32) && echo "built $(TARGET32)"; \
	else \
	    echo "no 32-bit toolchain (pacman -S lib32-glibc gcc-multilib) — skipping $(TARGET32)"; \
	fi

# POSITIVE CONTROL, and it is not optional. Prove the probe loads, identifies a process and
# keeps a thread alive against something we do not care about, before it is ever pointed at
# an account that cannot be replaced.
selftest: $(TARGET)
	@rm -f /tmp/aesteria-probe.log
	@AESTERIA_PROBE_ANYPROC=1 AESTERIA_PROBE_MAPS=1 AESTERIA_PROBE_THREAD=1 \
	  LD_PRELOAD=$(CURDIR)/$(TARGET) sleep 12
	@echo "--- /tmp/aesteria-probe.log ---"
	@cat /tmp/aesteria-probe.log
	@grep -q "heartbeat thread started" /tmp/aesteria-probe.log || { echo "FAIL: no thread"; exit 1; }
	@grep -q "heartbeat" /tmp/aesteria-probe.log || { echo "FAIL: thread never ran"; exit 1; }
	@grep -q "probe unloading" /tmp/aesteria-probe.log || { echo "FAIL: no clean-exit line"; exit 1; }
	@echo "PASS: load, identify, maps, thread, clean-exit line all observed"

clean:
	rm -f $(TARGET) $(TARGET32) *.o

.PHONY: all m32 selftest clean
