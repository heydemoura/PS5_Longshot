/* (longshot, PS5) When the pictures stop: host/ps5/trace_ps5.c's rule, apart so that it is tested off the console
   (tests/test_ps5_trace.py). The count of presents is looked at every so often; after_ms with no new one is a stall, said
   once, and the next present ends it. Nothing is said before the first picture. */
#pragma once
#include <stdint.h>

typedef struct {
    long long seen;       /* (the count at the last look that found a new one) */
    int64_t changed_ms;   /* (when that was) */
    int told;             /* (this stall was said) */
} StallWatch;
enum { STALL_NONE = 0, STALL_BEGAN, STALL_ENDED };

/* One look. STALL_BEGAN: no new picture for *lasted_ms (after_ms or more), said once a stall. STALL_ENDED: the first
   picture after a stall that was said, *lasted_ms after the last one before it */
static inline int stall_watch_step(StallWatch *w, long long presents, int64_t now_ms, int64_t after_ms, int64_t *lasted_ms) {
    if (presents != w->seen) {
        const int was = w->told;
        *lasted_ms = now_ms - w->changed_ms;
        w->seen = presents, w->changed_ms = now_ms, w->told = 0;
        return was ? STALL_ENDED : STALL_NONE;
    }
    if (!w->told && presents > 0 && now_ms - w->changed_ms >= after_ms) {
        w->told = 1;
        *lasted_ms = now_ms - w->changed_ms;
        return STALL_BEGAN;
    }
    return STALL_NONE;
}
