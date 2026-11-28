"""Exercise retired capture admission with a full spare pool, using production get/free.

python -I -B tests/test_capture_spare_admission.py [--cxx g++]
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

RUN_ALL_ARGS = ["--cxx", "{cxx}"]
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from srccut import block


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cxx", default="c++")
    args = parser.parse_args()
    source = (ROOT / "gpu/vk/vk_memory.cpp").read_text()
    fixture = r"""
#include <algorithm>
#include <array>
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <mutex>
#include <vector>
using VkBuffer = unsigned;
using VmaAllocation = void*;
constexpr uint64_t MB = 1ull << 20;
struct FreeBuf { VkBuffer buffer; VmaAllocation alloc; void* map; uint64_t size; };
std::vector<FreeBuf> cap_free;
// This fixture is single-threaded. Track the lock explicitly: try_lock on a
// std::mutex already owned by this thread would itself be undefined behaviour.
struct TestMutex {
    bool held = false;
    void lock() { assert(!held); held = true; }
    void unlock() { assert(held); held = false; }
};
TestMutex cap_free_mu;
std::atomic<bool> cap_shutting_down{false};
uint64_t cap_free_bytes, cap_reserved_bytes;
int cap_prewarm_busy;
static uint64_t budget = 768*MB, shortfall;
static bool async_free;
static unsigned allocations, next_buffer = 100;
static std::vector<VkBuffer> destroyed, queued;
static uint64_t linux_pressure_shortfall() { return shortfall; }
static uint64_t cap_spare_max() { return budget; }
static bool cap_free_async_on() { return async_free; }
static void release(VkBuffer b, std::vector<VkBuffer>& into) {
    // Driver destruction/its hand-off must happen outside the spare pool lock.
    assert(!cap_free_mu.held);
    assert(std::find(destroyed.begin(), destroyed.end(), b) == destroyed.end());
    assert(std::find(queued.begin(), queued.end(), b) == queued.end());
    into.push_back(b);
}
static void destroy_buffer(VkBuffer b, VmaAllocation) { release(b, destroyed); }
static void destroy_buffer_later(VkBuffer b, VmaAllocation) { release(b, queued); }
static void cap_prewarm_class(uint64_t) {}
static bool env_on(const char*) { return false; }
static unsigned cap_usage() { return 1; }
static bool create_buffer_cached(uint64_t, unsigned, VkBuffer& b, VmaAllocation& a, void** m, bool) {
    ++allocations;
    b = next_buffer++;
    a = *m = &allocations;
    return true;
}
""" + "\n".join(block(source, marker) for marker in (
        "void cap_buf_free(", "uint64_t cap_buf_size(", "bool cap_buf_get(")) + r"""
static void reset(std::initializer_list<uint64_t> sizes) {
    cap_free.clear(); cap_free_bytes = cap_reserved_bytes = cap_prewarm_busy = 0;
    destroyed.clear(); queued.clear(); allocations = 0;
    budget = 768*MB; shortfall = 0; async_free = false;
    for (uint64_t size : sizes) {
        cap_free.push_back({next_buffer++, &allocations, &allocations, size*MB});
        cap_free_bytes += size*MB;
    }
}
static void cycle(uint64_t need) {
    VkBuffer b = 0; VmaAllocation a = nullptr; void* map = nullptr; uint64_t size = 0;
    assert(cap_buf_get(need*MB, b, a, map, size));
    cap_buf_free(b, a, map, size);
    uint64_t bytes = 0;
    for (const auto& f : cap_free) bytes += f.size;
    assert(bytes == cap_free_bytes);
    assert(cap_free.size() + cap_prewarm_busy <= 12);
    assert(bytes + cap_reserved_bytes <= std::min<uint64_t>(budget, (shortfall ? 32ull : 768ull)*MB));
}
int main() {
    // Twelve small spares cannot serve a 128 MiB class. The first returned large
    // buffer must displace a cold spare, rather than be allocated on every submit.
    reset({2,2,2,2,2,2,2,2,2,2,2,2});
    const auto oldest = cap_free.front().buffer;
    for (int i = 0; i < 16; ++i) cycle(96);
    std::printf("full-slot pool: %u driver allocations for 16 requests\n", allocations);
    std::fflush(stdout);
    assert(allocations == 1);
    assert(destroyed.size() == 1 && destroyed.front() == oldest);

    // Returning many small captures must not flush the only spare of each load
    // class. A load can then use an already-created large buffer immediately.
    reset({32,64,128,192,2,2,2,2,2,2,2,2});
    for (int i = 0; i < 48; ++i)
        cap_buf_free(next_buffer++, &allocations, &allocations, 2*MB);
    for (const uint64_t load_class : {32,64,128,192}) {
        const auto count = std::count_if(cap_free.begin(), cap_free.end(),
            [load_class](const FreeBuf& f) { return f.size == load_class*MB; });
        assert(count >= 1);
    }
    cycle(96);
    std::printf("small returns: %u driver allocations for the next load\n", allocations);
    std::fflush(stdout);
    assert(allocations == 0);

    // Prefer the most redundant class, not merely the first duplicate. Both
    // large spares must survive small returns for a pair of in-flight loads.
    reset({128,128,2,2,2,2,2,2,2,2,2,2});
    for (int i = 0; i < 48; ++i)
        cap_buf_free(next_buffer++, &allocations, &allocations, 2*MB);
    std::array<FreeBuf, 2> loads;
    for (auto& f : loads)
        assert(cap_buf_get(96*MB, f.buffer, f.alloc, f.map, f.size));
    assert(allocations == 0);
    for (const auto& f : loads) cap_buf_free(f.buffer, f.alloc, f.map, f.size);

    // Under a byte limit, the incoming size itself supplies the replacement
    // representative: replace the old 128 MiB spare, not a unique larger class.
    reset({256,192,128,64,32,16,8,4,2});
    const auto old_128 = cap_free[2].buffer;
    const auto old_256 = cap_free.front().buffer;
    budget = 704*MB;
    cap_buf_free(next_buffer++, &allocations, &allocations, 128*MB);
    assert(destroyed.size() == 1 && destroyed.front() == old_128);
    assert(std::any_of(cap_free.begin(), cap_free.end(),
        [old_256](const FreeBuf& f) { return f.buffer == old_256; }));
    assert(cap_free_bytes == 702*MB);

    // Model the retirement delay too: the returned mix must settle after warm-up,
    // even though the cache initially contains no spare for the large class.
    reset({2,2,2,2,2,2,2,2,2,2,2,2});
    std::deque<FreeBuf> in_flight;
    unsigned warmed = 0;
    for (int i = 0; i < 240; ++i) {
        VkBuffer b = 0; VmaAllocation a = nullptr; void* map = nullptr; uint64_t size = 0;
        const uint64_t need = (i % 6 == 0 ? 160 : i % 3 == 0 ? 8 : 2)*MB;
        assert(cap_buf_get(need, b, a, map, size));
        in_flight.push_back({b, a, map, size});
        if (in_flight.size() > 24) {
            const auto f = in_flight.front(); in_flight.pop_front();
            cap_buf_free(f.buffer, f.alloc, f.map, f.size);
        }
        if (i == 119) warmed = allocations;
    }
    assert(allocations == warmed);

    // The byte limit can be full even below the entry limit. Adapt to a new large
    // class without raising the 768 MiB ceiling.
    reset({128,128,128,128,128,128});
    async_free = true;
    for (int i = 0; i < 16; ++i) cycle(160);
    assert(allocations == 1 && queued.size() == 2 && destroyed.empty());

    // Likewise, cold large buffers must not force a fresh small allocation forever.
    reset({256,192,192,128});
    for (int i = 0; i < 16; ++i) cycle(2);
    assert(allocations == 1 && destroyed.size() == 1);

    // Reservations are promises to workers, not spare bytes/slots to steal.
    reset({2,2,2,2,2,2,2,2,2,2});
    cap_reserved_bytes = 128*MB; cap_prewarm_busy = 2;
    cycle(96);
    assert(cap_free.size() == 10 && destroyed.size() == 1);
    reset({32});
    budget = 64*MB; cap_reserved_bytes = 64*MB; cap_prewarm_busy = 1;
    cap_buf_free(next_buffer++, &allocations, &allocations, 8*MB);
    assert(cap_free_bytes == 32*MB && destroyed.size() == 1);

    // A speculative prewarm must not evict buffers real submissions returned.
    reset({2,2,2,2,2,2,2,2,2,2,2,2});
    cap_reserved_bytes = 128*MB; cap_prewarm_busy = 1;
    cap_buf_free(next_buffer++, &allocations, &allocations, 128*MB, true);
    assert(cap_free.size() == 12 && cap_free_bytes == 24*MB && destroyed.size() == 1);

    // Reject an uncacheable buffer without emptying the useful spare pool.
    reset({2,2});
    cap_buf_free(next_buffer++, &allocations, &allocations, 300*MB);
    assert(cap_free.size() == 2 && destroyed.size() == 1);
    shortfall = MB;
    cap_buf_free(next_buffer++, &allocations, &allocations, 64*MB);
    assert(cap_free.size() == 2 && destroyed.size() == 2);

    reset({2,2,2,2,2,2,2,2,2,2,2,2});
    budget = 32*MB;
    for (int i = 0; i < 16; ++i) cycle(17);
    assert(allocations == 1 && cap_free_bytes <= 32*MB);
}
"""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder)
        (path / "test.cpp").write_text(fixture)
        exe = path / "test.exe"
        subprocess.run([args.cxx, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pthread",
                        str(path / "test.cpp"), "-o", str(exe)], check=True)
        # The scenarios assume the default 2 MiB minimum, not a player's override.
        env = os.environ.copy()
        env.pop("RX_VK_CAP_MIN_MB", None)
        subprocess.run([str(exe)], check=True, timeout=15, env=env)


if __name__ == "__main__":
    main()
