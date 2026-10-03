/* probe.c — AESTERIA's first injection test for AION 2.
 *
 * THE QUESTION THIS ANSWERS, AND THE ONE IT DELIBERATELY DOES NOT
 *   Everything AESTERIA can do today is done from OUTSIDE the process, via /proc/<pid>/mem,
 *   which NCGuard cannot observe. Getting INSIDE buys exactly two things we cannot otherwise
 *   have: a thread of our own in the game's address space, and (later) the ability to call
 *   into the game. This probe establishes the first and nothing else.
 *
 *   It performs NO function interposition. It hooks nothing, patches nothing, writes to no
 *   game memory, and reads nothing but /proc/self. That is the point: if the client fails to
 *   reach the world with this loaded, the cause is "a foreign Linux .so is in the address
 *   space", with no second variable to argue about. Adding a hook here would make a negative
 *   result uninterpretable. (spike discipline: a negative means nothing unless the
 *   only thing that changed is the thing you are testing.)
 *
 * WHY THIS IS THE LOW-EXPOSURE FORM OF INJECTION
 *   LD_PRELOAD loads a LINUX ELF into the wine process. NCGuard (bb64.dll) is user-mode and
 *   walks the WINDOWS PEB module list, where this object does not appear — the same boundary
 *   that makes /proc/<pid>/mem invisible. That is an argument from mechanism; the
 *   MEASUREMENT is the client reaching in-world with this loaded, which is the whole test.
 *
 * WHAT TO READ AFTERWARDS
 *   /tmp/aesteria-probe.log. The timestamps are the instrument. A log that simply continues
 *   past character-select means the mechanism is tolerated. A log that STOPS at a repeatable
 *   offset after load is an anti-cheat rejection with a clock on it — that is exactly how
 *   an XignCode rejection of another title showed up at ~11 s — and the offset tells you which
 *   stage of NCGuard's init did it.
 *
 * STAGES — escalate ONE per launch, so each result is attributable:
 *   (default)                 identify the process, one line, then nothing.
 *   AESTERIA_PROBE_MAPS=1     snapshot /proc/self/maps once, late enough to see bb64.dll.
 *   AESTERIA_PROBE_THREAD=1   keep a heartbeat thread for the session. THE real question:
 *                             can we host a runtime in here at all?
 *
 * BUILD: make      USE: see tools/aesteria_wrap.sh (do not hand-set LD_PRELOAD).
 */

#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>
#include <unistd.h>

#define PROBE_LOG "/tmp/aesteria-probe.log"
#define PROBE_MAPS "/tmp/aesteria-probe-maps.txt"

static struct timespec g_t0;
static int g_is_game = 0;

/* Seconds since the probe loaded. The ABSOLUTE time matters less than the offset: an
 * anti-cheat kill lands at a repeatable number of seconds after process start. */
static double elapsed(void){
    struct timespec now; clock_gettime(CLOCK_MONOTONIC,&now);
    return (double)(now.tv_sec-g_t0.tv_sec) + (double)(now.tv_nsec-g_t0.tv_nsec)/1e9;
}

static void plog(const char *fmt, ...){
    FILE *f = fopen(PROBE_LOG,"a");
    if(!f) return;
    time_t t = time(NULL); struct tm tm; localtime_r(&t,&tm);
    char stamp[32]; strftime(stamp,sizeof stamp,"%H:%M:%S",&tm);
    fprintf(f,"[%s +%7.3fs pid=%d] ", stamp, elapsed(), (int)getpid());
    va_list ap; va_start(ap,fmt); vfprintf(f,fmt,ap); va_end(ap);
    fputc('\n',f);
    fclose(f);                 /* reopened per line: the log survives a hard process kill */
}

/* argv[0]'s basename, from /proc/self/cmdline.
 * Matching argv[0] rather than the whole cmdline is REQUIRED here: the AION 2 launch chain
 * passes the game's full path to steam.exe, the proton python3 and the reaper, so all of
 * their cmdlines CONTAIN "AION2.exe" without being the game. Under wine /proc/self/exe is
 * the wine loader, so argv[0] is where the Windows binary name actually lives.
 *
 * ⚠ BUT NOT YET AT CONSTRUCTOR TIME. wine rewrites its argv to the Windows command line
 * during startup, so this returns the wine loader's name at load and the game's name a
 * moment later. Anything that needs the answer at load will get "no". See heartbeat(). */
static void self_argv0(char *out, size_t cap){
    out[0] = '\0';
    FILE *f = fopen("/proc/self/cmdline","rb");
    if(!f) return;
    char buf[4096];
    size_t n = fread(buf,1,sizeof(buf)-1,f);
    fclose(f);
    if(n == 0) return;
    buf[n] = '\0';
    const char *base = buf;
    for(const char *p=buf; *p; p++) if(*p=='\\' || *p=='/') base = p+1;
    size_t n_base = strlen(base);
    if(n_base >= cap) n_base = cap - 1;      /* bounded explicitly so the copy cannot truncate
                                              * behind our back, and the compiler can see it */
    memcpy(out, base, n_base);
    out[n_base] = '\0';
}

/* Copy our own maps out, and summarise what matters: is NCGuard in here with us, and how
 * much anonymous executable memory is there (AION2.exe is packed, so its real code lives in
 * anonymous regions — this is the INSIDE view of what the offline captures see). */
static void snapshot_maps(const char *why){
    FILE *in = fopen("/proc/self/maps","rb");
    if(!in){ plog("maps: cannot open (%s)", strerror(errno)); return; }
    char path[256];
    snprintf(path,sizeof path,"%s.%d",PROBE_MAPS,(int)getpid());
    FILE *out = fopen(path,"wb");
    char line[4096];
    unsigned long anon_exec=0, total=0; int saw_bb64=0, saw_self=0;
    while(fgets(line,sizeof line,in)){
        if(out) fputs(line,out);
        total++;
        if(strstr(line,"bb64.dll")) saw_bb64=1;
        if(strstr(line,"libaesteria_probe.so")) saw_self=1;
        /* "executable, and no backing file" -> unpacked code. Count FIELDS rather than
         * sniffing for a trailing space: a maps line is
         * `addr perms offset dev inode [pathname]`, so exactly 5 fields means anonymous,
         * and that holds whether or not the kernel pads before an absent pathname. */
        if(strstr(line," r-xp ") || strstr(line," rwxp ")){
            int fields = 0;
            for(const char *p=line; *p; ){
                while(*p==' '||*p=='\t'||*p=='\n') p++;
                if(!*p) break;
                fields++;
                while(*p && *p!=' ' && *p!='\t' && *p!='\n') p++;
            }
            if(fields == 5) anon_exec++;
        }
    }
    fclose(in);
    if(out) fclose(out);
    plog("maps[%s]: %lu regions, %lu anonymous-executable, bb64.dll=%s, self-mapped=%s -> %s",
         why, total, anon_exec, saw_bb64?"PRESENT":"absent", saw_self?"yes":"NO", path);
}

static void *heartbeat(void *arg){
    (void)arg;
    /* PHASE 1 — resolve identity, which could NOT be answered in the constructor.
     * wine rewrites argv to the Windows command line during startup, so a process that was
     * anonymous at load announces itself as AION2.exe a moment later. Poll for it. Every
     * other process in the chain (services.exe, explorer.exe, the wine helpers) never
     * resolves, and its thread simply exits — so we do not leave a thread sitting in
     * fifteen processes for the session. */
    if(!g_is_game){
        for(int i=0;i<120;i++){                       /* up to 2 minutes, 1 s apart */
            struct timespec s = { .tv_sec = 1, .tv_nsec = 0 };
            nanosleep(&s,NULL);
            char argv0[256];
            self_argv0(argv0,sizeof argv0);
            if(strcasecmp(argv0,"AION2.exe") == 0){
                g_is_game = 1;
                plog("IDENTIFIED as the game after %.1fs — argv is now '%s'", elapsed(), argv0);
                break;
            }
        }
        if(!g_is_game){
            plog("never became the game — thread exiting (this is a launch-chain helper)");
            return NULL;
        }
        if(getenv("AESTERIA_PROBE_MAPS")) snapshot_maps("at-identification");
    }

    /* PHASE 2 — the interesting window is the first minute: NCGuard initialises, then the
     * client reaches title/character-select/world. Log densely there, sparsely after. */
    int maps_done = 0;
    double t_id = elapsed();
    for(unsigned i=0;;i++){
        double e = elapsed();
        if(e - t_id < 60.0 || i % 12 == 0) plog("heartbeat");
        if(!maps_done && e >= 20.0){       /* late enough that wine has loaded the game's DLLs */
            snapshot_maps("t+20s");
            maps_done = 1;
        }
        struct timespec ts = { .tv_sec = 5, .tv_nsec = 0 };
        nanosleep(&ts,NULL);
    }
    return NULL;
}

__attribute__((constructor))
static void probe_init(void){
    clock_gettime(CLOCK_MONOTONIC,&g_t0);

    char argv0[256];
    self_argv0(argv0,sizeof argv0);
    g_is_game = (strcasecmp(argv0,"AION2.exe") == 0) ||
                (getenv("AESTERIA_PROBE_ANYPROC") &&
                 getenv("AESTERIA_PROBE_ANYPROC")[0]=='1');

    /* ⚠ LOG UNCONDITIONALLY, AND THE REASON IS A BUG THIS FILE ALREADY HAD.
     *
     * This used to `return` silently unless argv[0] was AION2.exe, to keep the log readable
     * across the ~15-process launch chain. On the first live run (2026-09-19, --mode probe)
     * that produced an EMPTY LOG while /proc/<pid>/maps showed the .so mapped into the game
     * perfectly well — the probe worked and could not say so.
     *
     * Why: an ELF constructor runs at exec time, and at that moment wine has NOT yet
     * rewritten its argv to the Windows command line. argv[0] only becomes
     * `…\AION2.exe` later in startup; at load it is still the wine loader. So the identity
     * test was being asked before it could possibly be answered, and its failure silenced
     * the one output the whole test exists to produce.
     *
     * A probe's job is evidence. Fifteen lines is not a readability problem; an empty log
     * after a launch you cannot repeat cheaply is a real one. */
    plog("=== AESTERIA probe loaded into '%s' (identified as game: %s) ===",
         argv0, g_is_game ? "yes" : "not yet — wine rewrites argv later");

    if(!g_is_game && !getenv("AESTERIA_PROBE_THREAD")) return;
    plog("stages: maps=%s thread=%s",
         getenv("AESTERIA_PROBE_MAPS")   ? "on" : "off",
         getenv("AESTERIA_PROBE_THREAD") ? "on" : "off");

    /* Only the game; otherwise MAPS+THREAD would drop a maps file for each of the
     * ~15 launch-chain processes. A helper's snapshot comes at identification time,
     * which for a helper never arrives. */
    if(g_is_game && getenv("AESTERIA_PROBE_MAPS")) snapshot_maps("load");

    if(getenv("AESTERIA_PROBE_THREAD")){
        pthread_t th;
        pthread_attr_t at;
        pthread_attr_init(&at);
        pthread_attr_setdetachstate(&at,PTHREAD_CREATE_DETACHED);
        /* A small stack: this thread only formats short strings, and a foreign 8 MB stack in
         * the game's address space is a needless difference from a clean process. */
        pthread_attr_setstacksize(&at,256*1024);
        int rc = pthread_create(&th,&at,heartbeat,NULL);
        pthread_attr_destroy(&at);
        if(rc == 0) plog("heartbeat thread started");
        else        plog("heartbeat thread FAILED: %s", strerror(rc));
    }
}

__attribute__((destructor))
static void probe_fini(void){
    /* Reached on a clean exit only. If the last line in the log is a heartbeat and never
     * this one, the process was killed rather than shut down — which is the distinction the
     * whole test turns on. */
    if(g_is_game) plog("=== probe unloading (clean process exit) ===");
}
