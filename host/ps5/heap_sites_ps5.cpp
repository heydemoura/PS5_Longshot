// (PS5) Who holds the platform heap, by the code that asked for it: HOS_PS5_HEAP_SITES=1 in the game folder's env file.
//
// 2026-11-18 the heap grew from 954 to 1614 MiB in 9 minutes (1500 MiB of it in blocks in use), and was still growing
// when direct memory ran out; the meter (win32_ps5.c cpu_meter) gives only its total. malloc and the rest already go
// to the platform heap through the SDK's own link-time wraps (libps5platform.a heap_wrap.c, PS5_Vulkan radv-link.sh);
// here the heap's own calls those wraps make (ps5_heap_malloc, _calloc, _memalign, _realloc, _free) and operator new
// are wrapped in turn (ps5/tools/link.sh --wrap), and one allocation in about every 256 KiB asked for is noted with the
// caller's return address, the estimate it stands for, and the pointer; free takes the note out again. (The caller of
// a heap call is the SDK wrap's: it keeps a frame pointer, so it is __builtin_return_address(1); heap_wrap.o is the
// only caller of ps5_heap_*.) Nothing is added to the blocks themselves: a block the console's libc frees, or hands back, is the
// block it gave. Every 60 s the kernel log gets the callers holding the most and those that grew the most:
//   [ps5] heap sites: ... / [ps5] heap sites grown: ... / [ps5] heap sites held: ...
// with their addresses in the executable (subtract 0x400000 for llvm-symbolizer against work/ps5/link/llvm-pie.elf:
// ~/longshot/ps5/scripts/heap_sites_symbolize.sh). Off (the default), each call costs one load.
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <new>
#include <pthread.h>

extern "C" {
void* __real_ps5_heap_malloc(size_t);
void* __real_ps5_heap_calloc(size_t, size_t);
void* __real_ps5_heap_memalign(size_t, size_t);
void* __real_ps5_heap_realloc(void*, size_t);
void __real_ps5_heap_free(void*);
void* __real__Znwm(size_t);
void* __real__Znam(size_t);
void* __real__ZnwmRKSt9nothrow_t(size_t, const std::nothrow_t&);
void* __real__ZnamRKSt9nothrow_t(size_t, const std::nothrow_t&);
void* __real__ZnwmSt11align_val_t(size_t, std::align_val_t);
void* __real__ZnamSt11align_val_t(size_t, std::align_val_t);
void* __real__ZnwmSt11align_val_tRKSt9nothrow_t(size_t, std::align_val_t, const std::nothrow_t&);
int sceKernelUsleep(unsigned microseconds);
}

namespace {
constexpr int64_t kInterval = 256 << 10;   // (bytes asked for between two notes, on average)
constexpr uint32_t kSites = 8192, kBuckets = 1u << 16, kNodes = 1u << 17;

std::atomic<bool> on{false};
std::atomic<uint64_t> dropped{0};   // (notes not taken: no node free, or no site left)
// A thread's own state, by pthread_self in a table: not thread_local, which this build makes emulated TLS
// (__emutls_get_address), whose first use on a thread calls malloc, back into this
struct Mine {
    std::atomic<uintptr_t> owner{0};
    int64_t until = 0;   // (this thread's bytes before its next note)
    uint32_t rng = 0;
    int inside = 0;      // (in a wrapped operator new, or the report: the malloc beneath is not noted again)
};
constexpr uint32_t kThreads = 4096;
Mine threads_[kThreads];
Mine* mine() {
    const uintptr_t self = (uintptr_t)pthread_self();
    uint32_t i = (uint32_t)((self * 0x9E3779B97F4A7C15ull) >> 52) & (kThreads - 1);
    for (uint32_t n = 0; n < 64; n++, i = (i + 1) & (kThreads - 1)) {
        uintptr_t have = threads_[i].owner.load(std::memory_order_acquire);
        if (have == self) return &threads_[i];
        if (!have && threads_[i].owner.compare_exchange_strong(have, self, std::memory_order_acq_rel)) return &threads_[i];
        if (have == self) return &threads_[i];
    }
    return nullptr;   // (no room: this thread's calls are not noted)
}

struct Site {
    std::atomic<uintptr_t> pc{0};
    std::atomic<int64_t> live{0}, notes{0};
    int64_t reported = 0;   // (live at the last report: the reporter's own)
};
Site sites[kSites];

struct Node {
    uintptr_t ptr;
    int64_t weight;
    uint32_t site;
    Node* next;
};
Node nodes[kNodes];
Node* pool = nullptr;
std::atomic<bool> pool_ready{false};
std::atomic<uint8_t> pool_lock{0};
std::atomic<Node*> buckets[kBuckets];
std::atomic<uint8_t> bucket_lock[kBuckets];

void lock(std::atomic<uint8_t>& l) {
    while (l.exchange(1, std::memory_order_acquire))
        while (l.load(std::memory_order_relaxed)) __builtin_ia32_pause();
}
void unlock(std::atomic<uint8_t>& l) { l.store(0, std::memory_order_release); }

uint32_t bucket_of(uintptr_t p) { return (uint32_t)(((p >> 4) * 0x9E3779B97F4A7C15ull) >> 48) & (kBuckets - 1); }

uint32_t site_of(uintptr_t pc) {
    uint32_t i = (uint32_t)((pc * 0x9E3779B97F4A7C15ull) >> 51) & (kSites - 1);
    for (uint32_t n = 0; n < kSites; n++, i = (i + 1) & (kSites - 1)) {
        uintptr_t have = sites[i].pc.load(std::memory_order_acquire);
        if (have == pc) return i;
        if (!have) {
            if (sites[i].pc.compare_exchange_strong(have, pc, std::memory_order_acq_rel)) return i;
            if (have == pc) return i;
        }
    }
    return kSites;   // (full)
}

Node* node_take() {
    lock(pool_lock);
    Node* n = pool;
    if (n) pool = n->next;
    unlock(pool_lock);
    return n;
}
void node_give(Node* n) {
    lock(pool_lock);
    n->next = pool, pool = n;
    unlock(pool_lock);
}

void forget(void* p) {
    if (!p || !on.load(std::memory_order_relaxed)) return;
    const uint32_t b = bucket_of((uintptr_t)p);
    if (!buckets[b].load(std::memory_order_acquire)) return;   // (nearly every free: no note in its bucket)
    lock(bucket_lock[b]);
    Node* found = nullptr;
    Node* prev = nullptr;
    for (Node* n = buckets[b].load(std::memory_order_relaxed); n; prev = n, n = n->next)
        if (n->ptr == (uintptr_t)p) {
            found = n;
            if (prev) prev->next = n->next;
            else buckets[b].store(n->next, std::memory_order_release);
            break;
        }
    unlock(bucket_lock[b]);
    if (!found) return;
    sites[found->site].live.fetch_sub(found->weight, std::memory_order_relaxed);
    sites[found->site].notes.fetch_sub(1, std::memory_order_relaxed);
    node_give(found);
}

void note(void* p, size_t size, uintptr_t pc) {
    if (!p || !on.load(std::memory_order_relaxed)) return;
    Mine* m = mine();
    if (!m || m->inside) return;
    m->until -= (int64_t)size;
    if (m->until > 0) return;
    m->rng = m->rng * 1664525u + 1013904223u + (uint32_t)(uintptr_t)m;
    m->until = kInterval / 2 + (int64_t)(m->rng >> 8) % kInterval;   // (from half to one and a half intervals on)
    const int64_t weight = (int64_t)size > kInterval ? (int64_t)size : kInterval;
    const uint32_t s = site_of(pc);
    Node* n = s < kSites ? node_take() : nullptr;
    if (!n) {
        dropped.fetch_add(1, std::memory_order_relaxed);
        return;
    }
    n->ptr = (uintptr_t)p, n->weight = weight, n->site = s;
    const uint32_t b = bucket_of((uintptr_t)p);
    lock(bucket_lock[b]);
    Node* stale = nullptr;   // (the same address noted before and freed where free is not wrapped: the old note goes)
    Node* prev = nullptr;
    for (Node* m = buckets[b].load(std::memory_order_relaxed); m; prev = m, m = m->next)
        if (m->ptr == (uintptr_t)p) {
            stale = m;
            if (prev) prev->next = m->next;
            else buckets[b].store(m->next, std::memory_order_relaxed);
            break;
        }
    n->next = buckets[b].load(std::memory_order_relaxed);
    buckets[b].store(n, std::memory_order_release);
    unlock(bucket_lock[b]);
    sites[s].live.fetch_add(weight, std::memory_order_relaxed);
    sites[s].notes.fetch_add(1, std::memory_order_relaxed);
    if (stale) {
        sites[stale->site].live.fetch_sub(stale->weight, std::memory_order_relaxed);
        sites[stale->site].notes.fetch_sub(1, std::memory_order_relaxed);
        node_give(stale);
    }
}

struct Inside {
    Mine* m = on.load(std::memory_order_relaxed) ? mine() : nullptr;
    Inside() { if (m) m->inside++; }
    ~Inside() { if (m) m->inside--; }
};

struct Row {
    uint32_t site;
    int64_t key;
};
Row rows[kSites];

// the n rows with the largest keys first (n small: a selection, no allocation)
uint32_t top(uint32_t count, uint32_t n) {
    n = n < count ? n : count;
    for (uint32_t i = 0; i < n; i++) {
        uint32_t best = i;
        for (uint32_t j = i + 1; j < count; j++)
            if (rows[j].key > rows[best].key) best = j;
        const Row t = rows[i];
        rows[i] = rows[best], rows[best] = t;
    }
    return n;
}

void* report_loop(void*) {
    if (Mine* m = mine()) m->inside = 1;
    for (;;) {
        sceKernelUsleep(60u * 1000000u);
        int64_t live_all = 0, notes_all = 0;
        uint32_t used = 0;
        static int64_t delta[kSites];
        for (uint32_t i = 0; i < kSites; i++) {
            if (!sites[i].pc.load(std::memory_order_acquire)) continue;
            used++;
            const int64_t l = sites[i].live.load(std::memory_order_relaxed);
            live_all += l, notes_all += sites[i].notes.load(std::memory_order_relaxed);
            delta[i] = l - sites[i].reported;
            sites[i].reported = l;
        }
        fprintf(stderr, "[ps5] heap sites: ~%lld MiB held by %u callers (%lld notes, one per ~256 KiB asked for; %llu not taken)\n",
                (long long)(live_all >> 20), used, (long long)notes_all,
                (unsigned long long)dropped.load(std::memory_order_relaxed));
        for (int pass = 0; pass < 2; pass++) {
            uint32_t count = 0;
            for (uint32_t i = 0; i < kSites; i++)
                if (sites[i].pc.load(std::memory_order_relaxed))
                    rows[count++] = {i, pass == 0 ? delta[i] : sites[i].live.load(std::memory_order_relaxed)};
            const uint32_t n = top(count, 16);
            char line[1400];
            int k = snprintf(line, sizeof line, "[ps5] heap sites %s:", pass == 0 ? "grown in 60 s" : "held");
            for (uint32_t r = 0; r < n && k < (int)sizeof line - 80; r++) {
                if (pass == 0 && rows[r].key < (1 << 20)) break;
                const Site& s = sites[rows[r].site];
                k += snprintf(line + k, sizeof line - k, " %#llx %+lld/%lld MiB;", (unsigned long long)s.pc.load(),
                              (long long)(delta[rows[r].site] >> 20), (long long)(s.live.load() >> 20));
            }
            fprintf(stderr, "%s\n", line);
        }
    }
    return nullptr;
}
}  // namespace

extern "C" void hos_ps5_heap_sites_start(void) {
    const char* e = getenv("HOS_PS5_HEAP_SITES");
    if (!e || e[0] != '1' || pool_ready.exchange(true)) return;
    for (uint32_t i = 0; i < kNodes; i++) nodes[i].next = i + 1 < kNodes ? &nodes[i + 1] : nullptr;
    pool = &nodes[0];
    on.store(true, std::memory_order_release);
    pthread_t t;
    if (pthread_create(&t, nullptr, report_loop, nullptr) == 0) pthread_detach(t);
    fprintf(stderr, "[ps5] heap sites: on (HOS_PS5_HEAP_SITES=1): who holds the heap, every 60 s\n");
}

#define PC ((uintptr_t)__builtin_return_address(0))
#define PC_OF_WRAP ((uintptr_t)__builtin_return_address(1))   // (through the SDK's malloc wrap, above)
namespace {
extern "C" void ps5_heap_trim(void) __attribute__((weak));
extern "C" void hos_ps5_swap_need(size_t bytes);
std::atomic<int> waits_told{0};
/// (PS5) A heap call that found no memory waits for some, 20 ms at a time up to 5 s, the heap trimmed first, before it
/// returns NULL: the game's pages, the heap and the renderer share one pool, and a burst takes the last of it for a moment.
/// The host's C code mostly does not check (rx_backend.c collect_in's realloc wrote at address 0 and ended the title,
/// 2026-11-18 14:50, with 259 MiB free 5 s before). operator new's own wait (vk_main.cpp ps5_new_waits) follows this one
template <typename F>
void* waited(F again, size_t n) {
    if (!n) return nullptr;
    if (waits_told.fetch_add(1, std::memory_order_relaxed) < 40)
        fprintf(stderr, "[ps5] heap: no memory for %zu bytes: waiting for some\n", n);
    if (ps5_heap_trim) ps5_heap_trim();
    hos_ps5_swap_need(n);   // (the game's cold pages to the swap file first: host/ps5/guest_swap_ps5.inc)
    for (int tries = 0; tries < 250; tries++) {
        sceKernelUsleep(20000);
        if (void* p = again()) return p;
    }
    fprintf(stderr, "[ps5] heap: no memory for %zu bytes after 5 s\n", n);
    return nullptr;
}
}  // namespace

extern "C" {
__attribute__((noinline)) void* __wrap_ps5_heap_malloc(size_t n) {
    void* p = __real_ps5_heap_malloc(n);
    if (!p) p = waited([&] { return __real_ps5_heap_malloc(n); }, n);
    note(p, n, PC_OF_WRAP);
    return p;
}
__attribute__((noinline)) void* __wrap_ps5_heap_calloc(size_t c, size_t n) {
    void* p = __real_ps5_heap_calloc(c, n);
    if (!p) p = waited([&] { return __real_ps5_heap_calloc(c, n); }, c * n);
    note(p, c * n, PC_OF_WRAP);
    return p;
}
__attribute__((noinline)) void* __wrap_ps5_heap_memalign(size_t a, size_t n) {
    void* p = __real_ps5_heap_memalign(a, n);
    if (!p) p = waited([&] { return __real_ps5_heap_memalign(a, n); }, n);
    note(p, n, PC_OF_WRAP);
    return p;
}
__attribute__((noinline)) void* __wrap_ps5_heap_realloc(void* old, size_t n) {
    void* p = __real_ps5_heap_realloc(old, n);
    if (!p && n) p = waited([&] { return __real_ps5_heap_realloc(old, n); }, n);
    if (p || !n) forget(old);   // (failed: the old block is still there, and its note)
    note(p, n, PC_OF_WRAP);
    return p;
}
__attribute__((noinline)) void __wrap_ps5_heap_free(void* p) {
    forget(p);
    __real_ps5_heap_free(p);
}
#define WRAP_NEW(name, params, args, size)                       \
    __attribute__((noinline)) void* __wrap_##name params {       \
        void* p;                                                 \
        {                                                        \
            Inside in;                                           \
            p = __real_##name args;                              \
        }                                                        \
        note(p, size, PC);                                       \
        return p;                                                \
    }
WRAP_NEW(_Znwm, (size_t n), (n), n)
WRAP_NEW(_Znam, (size_t n), (n), n)
WRAP_NEW(_ZnwmRKSt9nothrow_t, (size_t n, const std::nothrow_t& t), (n, t), n)
WRAP_NEW(_ZnamRKSt9nothrow_t, (size_t n, const std::nothrow_t& t), (n, t), n)
WRAP_NEW(_ZnwmSt11align_val_t, (size_t n, std::align_val_t a), (n, a), n)
WRAP_NEW(_ZnamSt11align_val_t, (size_t n, std::align_val_t a), (n, a), n)
WRAP_NEW(_ZnwmSt11align_val_tRKSt9nothrow_t, (size_t n, std::align_val_t a, const std::nothrow_t& t), (n, a, t), n)
}
