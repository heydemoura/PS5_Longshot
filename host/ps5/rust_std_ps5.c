/* (PS5) What the Rust standard library built for FreeBSD asks of libc that no module a title loads exports. The game's
   Rust (longshot-rs) uses none of them: std's backtrace symbolizer walks the loaded objects (dl_iterate_phdr: none is
   said), its process and socket code names the other two. accept4 and pthread_setname_np are host/ps5/win32_ps5.c's. */
#include <stddef.h>
int dl_iterate_phdr(int (*callback)(void *, size_t, void *), void *data) { (void)callback, (void)data; return 0; }
int getpeereid(int s, unsigned *euid, unsigned *egid) { (void)s, (void)euid, (void)egid; return -1; }
int killpg(int pgrp, int sig) { (void)pgrp, (void)sig; return -1; }
