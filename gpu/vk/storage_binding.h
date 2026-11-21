// Where a storage buffer binding starts for an address the game gave: the address rounded down to the binding's
// alignment (vk_internal.h storage_binding_align: the device's, 16 bytes at least, 256 on the PS5). The translation counts
// a storage offset from there and takes the rest of the address into the offset itself (Eden's StorageOffset,
// Profile::min_ssbo_alignment), so whoever reads a storage buffer with a translated offset (a handle program's:
// vk_shader.cpp stage_storage_base) starts here, as the descriptor writer binds it (vk_draw.cpp).
// Pure, for tests/test_storage_binding_start.py.
#pragma once
#include <cstdint>

namespace mkwvk {
/// address rounded down to align (a power of two; anything else: the address as it is)
constexpr uint64_t storage_binding_start(uint64_t address, uint64_t align) {
    return align > 1 && (align & (align - 1)) == 0 ? address & ~(align - 1) : address;
}
} // namespace mkwvk
