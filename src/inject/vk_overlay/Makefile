# AESTERIA in-game overlay — Vulkan implicit layer (C). Needs `vulkan-headers`.
#   make                # build libaesteria_overlay.so
#   make smoke          # positive control: draw over vkcube, never the game
#   make install        # register the manifest in ~/.local/share/vulkan/implicit_layer.d
#   make uninstall      # remove it
#   make clean
#
# The layer is INERT unless AESTERIA_OVERLAY=1 is set, so installing the manifest does not
# put it in the path of every Vulkan app on the desktop — the loader skips a layer whose
# enable_environment is unset before loading the .so at all.

CC       ?= gcc
CFLAGS   ?= -O2 -Wall -Wextra
CFLAGS   += -fPIC -Wno-unused-parameter -std=gnu11
TARGET    = libaesteria_overlay.so
MANIFEST  = aesteria_overlay.json
LAYERDIR  = $(HOME)/.local/share/vulkan/implicit_layer.d

all: $(TARGET)

aesteria_overlay_layer.o: aesteria_overlay_layer.c
	$(CC) $(CFLAGS) -c -o $@ $<

# Link to a temp then rename(2) into place: the game maps this .so for a whole session, and
# a direct `-o $@` truncates the mapped inode, faulting the running process. The rename
# leaves the old inode alive for the live game; the NEXT launch picks up the new file. So
# `make` is safe at any time, game up or not.
$(TARGET): aesteria_overlay_layer.o
	$(CC) -shared -fPIC -o $@.tmp $^ -lpthread
	mv -f $@.tmp $@

# The positive control, and it runs BEFORE the game is ever launched with this: if the block
# does not appear over vkcube, the layer is broken and nothing is learned by risking the
# account. A negative result means nothing without a positive control first.
smoke: $(TARGET)
	@command -v vkcube >/dev/null || { echo "vkcube not installed (pacman -S vulkan-tools)"; exit 1; }
	VK_LAYER_PATH=$(CURDIR) VK_INSTANCE_LAYERS=VK_LAYER_AESTERIA_overlay \
	AESTERIA_OVERLAY=1 AESTERIA_OVERLAY_ANYPROC=1 vkcube

install: $(TARGET)
	mkdir -p $(LAYERDIR)
	ln -sf $(CURDIR)/$(MANIFEST) $(LAYERDIR)/$(MANIFEST)
	@echo "registered: $(LAYERDIR)/$(MANIFEST)"
	@echo "inert until AESTERIA_OVERLAY=1 is in the launch environment."

uninstall:
	rm -f $(LAYERDIR)/$(MANIFEST)
	@echo "removed: $(LAYERDIR)/$(MANIFEST)"

clean:
	rm -f $(TARGET) *.o

.PHONY: all smoke install uninstall clean
