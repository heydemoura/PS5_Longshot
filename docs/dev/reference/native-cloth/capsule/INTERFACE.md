# The kernel's interface

`cloth_capsule.h` (C, `extern "C"`). The kernel is one C++ file, `cloth_capsule.cpp`, that includes `cloth_capsule.h`
and may include the kit's helpers `cloth_common.hpp` and `cloth_wlane.hpp` (the Arm's float rules: the reciprocal
square root estimate's table `kc::frsqrte_bits`, a minimum with the Arm's signed zeros, lane helpers). It is compiled
as C++17 with `-O2 -ffp-contract=off -fno-strict-aliasing -fno-exceptions -fno-rtti` and one of `-march=x86-64-v3`
(SSE4.1, AVX2, FMA), `-march=x86-64-v2` (SSE4.1, no FMA) or plain `-march=x86-64` (SSE2 only); it must build and be
exact under all three (`__SSE4_1__` and `__FMA__` say what is there; `KC_SIMD` in `cloth_common.hpp` is 1 with SSE4.1).
No other library than the C/C++ standard headers and the x86 intrinsics headers. No global state that changes between
calls; the two functions may be called from several threads on different data.

## Entry points

```c
void cloth_capsule_batches(ClothCapBatches *b);
void cloth_capsule_singles(ClothCapSingles *s);
```

### `cloth_capsule_batches`

In: `b->k` (the constants, `ClothCapsule`), `b->indices`, `b->radii`, `b->frictions` (batch 0's entries; batch i at
+32 i, +64 i, +64 i bytes), `b->pos`, `b->prev` (slot p at +16 p), `b->n` (>= 1), `b->contacts` (NULL or room for `n`
entries). Every out field is garbage on entry.

Does batches 0, 1, ... in order (SPEC.md 3, 4). Out: `b->done` (batches done whole), `b->why` (SPEC.md 7 and 8), and,
when `b->contacts` is not NULL, `b->contacts[i]` for each batch i < `done` (bit j: particle j in contact). Entries of
`contacts` at `done` and after may be anything.

Reads: the constants; for each batch it does or examines, its 16 indices, radii and frictions, the position slots of
its particles and the previous-position slots of those in contact. Writes: only position and previous-position slots
of particles in contact in the batches done (SPEC.md 4's store rule), and `contacts`. Nothing of batch `done` or later.

### `cloth_capsule_singles`

In: `s->k`, `s->indices` (u16 each), `s->pos`, `s->prev`, `s->data` (particle p's 16 bytes at +16 p: radius at +8,
friction at +12), `s->n` (>= 1, at most 15). Out fields garbage on entry.

Does particles 0, 1, ... in order (SPEC.md 3, 5). Out: `s->done`, `s->why`, `s->contacts` (SPEC.md 8).

Reads: the constants; per particle done or examined, its index, its position slot, its data, and its previous-position
slot when in contact. Writes: only the two slots of each particle done that was in contact (position first, then
previous position, before the next particle is read). Nothing of particle `done` or later.

## When it is called (what it may assume)

- The float mode is the game's: round to nearest, default-NaN mode off, no traps; flush-to-zero as `k->fz` says.
  The status flags already hold invalid operation, divide by zero, overflow, underflow, inexact and input denormal, so
  no flag can change: the kernel reports no float status and need not track flags.
- The slots of the particles named, the arrays and the constants are readable; the position and previous-position slots
  of the particles named are writable; none of them overlaps another array or the constants (the caller checked).
- Indices in a batch may repeat (SPEC.md 4: such a batch is left). Indices of singles may repeat (each particle reads
  what the one before it stored).

## When it must stop (decline)

At the first batch or particle for which it cannot give the game's bits (SPEC.md 6 and 7): it writes nothing of it or
after it, sets `done` to the number before it and `why` to `CLOTH_CAP_INDEX` (a batch with a repeated index) or
`CLOTH_CAP_VALUES` (values outside what it handles). The game's own code does the rest. Stopping more often than needed
is exact but slower: `HARNESS.md` gives the share of particles the kernel is expected to do.

## The float status

Not reported (see above). The kernel's own host float environment: the default (round to nearest, no flush, no
exceptions enabled); it must not change MXCSR.
