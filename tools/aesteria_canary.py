import os, time, sys
MARKER = b"AESTERIA_CANARY_" + b"7f3d9c1e" * 8
buf = bytearray(64 * 1024 * 1024)          # 64 MB anonymous heap
for off in (0, 16*1024*1024, 48*1024*1024):
    buf[off:off+len(MARKER)] = MARKER
keep = buf
print(os.getpid(), flush=True)
time.sleep(300)
