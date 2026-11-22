/* (longshot, PS5) A few lines that outlive the title: /app0/longshot_trace.txt, each written and synced as it happens.
   klog says more, but only to someone recording it; after a session that ended badly this file is what the player has.

     - the start;
     - no picture for 2 s, and the pictures again (a hang of the GPU or of the game, a long load, the home screen);
     - the memory floor closing the title, an exit the title asked for (host/ps5/win32_ps5.c);
     - a crash's signal, the address it faulted on and where the program was.

   The signal handler only writes its line: it is installed to run once (SA_RESETHAND), so when it returns the instruction
   faults again with no handler and the console makes its own crash report, as before. (PS5_Longshot_Plus's crash_trace.txt,
   groovy2312, is where the idea is from; its handler ends the process, and the console's report with it.)

   HOS_PS5_TRACE=0: no file, no handler, no thread. The file is started again past 256 KB (the one before kept beside it). */
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <ucontext.h>
#include <unistd.h>
#include <ps5platform/context.h>   /* (the console's machine context: mc_rip) */
#include <ps5platform/kernel.h>
#include "stall_watch.h"

int sceKernelUsleep(unsigned microseconds);
void hos_ps5_klog_now(const char *fmt, ...);
void hos_ps5_trace(const char *fmt, ...);
extern volatile long long hos_vi_presents;   /* (host/nn_vi.c: the pictures presented so far) */

#define TRACE_PATH "/app0/longshot_trace.txt"
#define TRACE_OLD "/app0/longshot_trace.old.txt"
#define TRACE_MOST (256 << 10)
#define STALL_MS 2000
#define STALL_LINES 200   /* (a run's most: a session of loads does not fill the file) */

static int trace_state = -1;   /* (-1: not read yet) */
static int64_t trace_t0_ms;

static int64_t mono_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}
static int trace_on(void) {
    if (trace_state < 0) {
        const char *e = getenv("HOS_PS5_TRACE");
        trace_state = !(e && (!strcmp(e, "0") || !strcasecmp(e, "off") || !strcasecmp(e, "false") || !strcasecmp(e, "no")));
    }
    return trace_state;
}
/* (open, write, close: nothing is left open for a crash to lose, and O_SYNC has the line on the disk when write returns.
   Only calls a signal handler may make) */
static void trace_put(const char *text, size_t n) {
    const int fd = open(TRACE_PATH, O_WRONLY | O_CREAT | O_APPEND | O_SYNC, 0666);
    if (fd < 0) return;
    (void)!write(fd, text, n);
    close(fd);
}
void hos_ps5_trace(const char *fmt, ...) {
    if (!trace_on()) return;
    char line[480];
    const time_t now = time(NULL);
    struct tm tm;
    gmtime_r(&now, &tm);
    int n = snprintf(line, sizeof line, "%04d-%02d-%02d %02d:%02d:%02dZ +%llds  ", tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday,
                     tm.tm_hour, tm.tm_min, tm.tm_sec, (long long)((mono_ms() - trace_t0_ms) / 1000));
    va_list ap;
    va_start(ap, fmt);
    const int m = vsnprintf(line + n, sizeof line - (size_t)n - 1, fmt, ap);
    va_end(ap);
    if (m < 0) return;
    n += m < (int)sizeof line - n - 1 ? m : (int)sizeof line - n - 2;
    line[n++] = '\n';
    trace_put(line, (size_t)n);
}

/* ---- a crash's signal: its line, with nothing but the calls a handler may make ---- */
static char *put_text(char *p, const char *s) {
    while (*s) *p++ = *s++;
    return p;
}
static char *put_hex(char *p, uint64_t v) {
    *p++ = '0', *p++ = 'x';
    int started = 0;
    for (int shift = 60; shift >= 0; shift -= 4) {
        const unsigned d = (unsigned)(v >> shift) & 15;
        if (d || started || !shift) *p++ = (char)(d < 10 ? '0' + d : 'a' + d - 10), started = 1;
    }
    return p;
}
static char *put_dec(char *p, uint64_t v) {
    char digits[24];
    int n = 0;
    do digits[n++] = (char)('0' + v % 10), v /= 10; while (v);
    while (n) *p++ = digits[--n];
    return p;
}
static void trace_signal(int sig, siginfo_t *si, void *context) {
    const ucontext_t *uc = (const ucontext_t *)context;
    char line[256], *p = line;
    p = put_text(p, "signal ");
    p = put_dec(p, (uint64_t)sig);
    p = put_text(p, sig == SIGSEGV ? " (a read or write of memory that is not there)" : sig == SIGBUS ? " (a bad address)"
                    : sig == SIGILL ? " (not an instruction)" : sig == SIGFPE ? " (arithmetic)" : sig == SIGABRT ? " (abort)" : "");
    p = put_text(p, ": address ");
    p = put_hex(p, si ? (uint64_t)(uintptr_t)si->si_addr : 0);
    p = put_text(p, ", the program at ");
    p = put_hex(p, uc ? (uint64_t)uc->uc_mcontext.mc_rip : 0);
    p = put_text(p, " (hos_ps5_trace is at ");   /* (what places the program's addresses: the executable is loaded anywhere) */
    p = put_hex(p, (uint64_t)(uintptr_t)&hos_ps5_trace);
    p = put_text(p, "), ");
    p = put_dec(p, (uint64_t)((mono_ms() - trace_t0_ms) / 1000));
    p = put_text(p, " s after the start; the console's crash report follows\n");
    trace_put(line, (size_t)(p - line));
    /* (run once, SA_RESETHAND: back to the instruction, which faults again with no handler, and the console reports it) */
}

/* ---- the pictures stopping ---- */
static void *trace_watch(void *unused) {
    (void)unused;
    StallWatch w = {0};
    int lines = 0;
    for (;;) {
        int64_t lasted = 0;
        const int what = stall_watch_step(&w, __atomic_load_n(&hos_vi_presents, __ATOMIC_RELAXED), mono_ms(), STALL_MS, &lasted);
        if (what != STALL_NONE && lines < STALL_LINES) {
            if (what == STALL_BEGAN)
                hos_ps5_trace("no picture for %.1f s (the last was number %lld)", (double)lasted / 1000.0, w.seen);
            else
                hos_ps5_trace("pictures again, %.1f s after the last one", (double)lasted / 1000.0);
            if (++lines == STALL_LINES) hos_ps5_trace("(no more lines about pictures stopping in this run)");
        }
        sceKernelUsleep(250000);
    }
    return NULL;
}

/* (host/ps5/win32_ps5.c hos_ps5_memory_floor_start: after longshot_env.txt is read, which may turn this off) */
void hos_ps5_trace_start(void) {
    trace_t0_ms = mono_ms();
    if (!trace_on()) return;
    struct stat st;
    if (stat(TRACE_PATH, &st) == 0 && st.st_size > TRACE_MOST) (void)rename(TRACE_PATH, TRACE_OLD);
    hos_ps5_trace("start (%llu MiB of direct memory in all)", (unsigned long long)(sceKernelGetDirectMemorySize() >> 20));
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_sigaction = trace_signal;
    sa.sa_flags = SA_SIGINFO | SA_RESETHAND;
    sigemptyset(&sa.sa_mask);
    static const int signals[] = {SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGABRT};
    for (size_t i = 0; i < sizeof signals / sizeof *signals; i++) sigaction(signals[i], &sa, NULL);
    pthread_t t;
    if (pthread_create(&t, NULL, trace_watch, NULL) == 0) pthread_detach(t);
}
