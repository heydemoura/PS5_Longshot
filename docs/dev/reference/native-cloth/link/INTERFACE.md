# The link pass kernel: the C++ interface

The candidate is ONE file, `cloth_link.cpp`, implementing `cloth_link_pass` of `cloth_link.h` (this folder). C++17,
x86-64, SSE / SSE4.1 / AVX / FMA intrinsics allowed where the build has them (test `__SSE4_1__`, `__AVX__`, `__FMA__`),
built with `-O2 -ffp-contract=off -fno-strict-aliasing` for `-march=x86-64-v3`, `-march=x86-64-v2` and `-march=x86-64`
(plain SSE2: the kernel must build and be exact there too; it may be a slower form). It may include only `cloth_link.h`,
`cloth_wlane.hpp`, `cloth_common.hpp` (this folder; use them as they are, do not copy them into the file) and standard
or compiler headers. No `-ffast-math`, no `long double`, no new build files, no other source files.

## The call

```c
void cloth_link_pass(ClothLinkPass *p);
```

The game's pass is a loop inside a larger function. The hook (the glue at the loop's entry; not yours) calls the kernel
for the whole loop, once it has:
- found `n >= 1` (the loop is entered only with at least one link);
- checked the float modes: flush to zero on or off (`p->fz`), everything else at its default (round to nearest, no
  default-NaN mode, no traps), and IXC already set in the guest's float status (`p->fpsr_in & 0x10`);
- resolved the guest pointers: `slots` and `particles` each with their whole reach (65,536 slots of 16 bytes), `links`
  for `12 * n` bytes, all mapped;
- read the scale (`p->scale`, the float's bits).

The host's float environment is its default (MXCSR `0x1f80`: round to nearest, no flush to zero, no denormals-are-zero,
every exception masked). The kernel may rely on it and must leave it as it found it.

### Inputs (`ClothLinkPass`, see the header's comments)
| Field | Meaning (SPEC.md) |
|---|---|
| `slots` | the particle slots (1.1); read and written |
| `particles` | the particle data; the float at `+4` of particle `p` is its inverse mass. May be the same memory as `slots` or lie inside it (any 4-byte offset). |
| `links` | `n` link records of 12 bytes (1.2). May lie inside the slots' memory (then the pass's own stores change later records). |
| `n` | the link count, 1 .. 2^31 - 1 |
| `scale` | `s` as float bits |
| `fz` | flush to zero (1) or not (0) |
| `fpsr_in` | the status flags set when the pass starts (bit layout of SPEC.md 3) |

### What it may read and write
- Read: the records, the slots and particle data of the slot numbers the records name; nothing else of the guest. Reads
  must follow SPEC.md 2.1 (a link's reads after the previous link's stores; the reload of slot `b` after the store of
  slot `a`). The pointers are `uint8_t`: copy with `memcpy` or unaligned loads (`_mm_loadu_ps`); nothing is guaranteed
  aligned beyond 4 bytes.
- Write: only the two slots of each link it does, 16 bytes each, in the order of SPEC.md 2.1, nothing of a link it does
  not do. No other guest memory, no global state that changes the results of a later call. Thread-safe: several threads
  call it at once on different cloths.

### Outputs
| Field | Meaning |
|---|---|
| `done` | the links done and stored, `0 .. n`; link `done` (when `done < n`) was not touched |
| `fpsr` | the flags the pass raised over links `0 .. done-1` (SPEC.md 3), IXC aside; bits already in `fpsr_in` may be included or left out (the hook ORs this in). Ignored when `done == 0`. |
| `slot_lo`, `slot_hi` | the lowest and highest slot number stored over the links done (`slot_lo > slot_hi` when none). The hook marks those pages as written. |
| `why` | `CLOTH_LINK_ALL` when `done == n`; else `CLOTH_LINK_ENTRY` (SPEC.md 4.1, then `done` = 0), `CLOTH_LINK_CLASS` (4.2), `CLOTH_LINK_STEP` (4.3), or `CLOTH_LINK_OTHER` (any other reason; avoid) |
| `end` | when `done == n`: the values of SPEC.md 5 about the last link, every field, every bit. Not read when `done < n`. |

### What the hook does with it
- `done == 0`: the game's loop runs the whole pass (so nothing may have been written).
- `done == n`: the slots hold the result; the hook ORs `fpsr` into the guest's float status, puts the `end` values where
  the rest of the game's function reads them, and continues the function after the loop.
- `0 < done < n`: the hook ORs `fpsr` in, then the game's loop runs links `done .. n-1` (from the same state as the
  loop reaching link `done`: nothing but memory is carried from one link to the next) and leaves its own end values.

## Speed
`HARNESS.md` gives the speed to meet (ns a link, the kernel we have now, on the bench of `check.py --bench`). The pass is
latency-bound per link: the chain from the previous link's store, through the loads, the squared length, the reciprocal
square root, the correction and the two stores, which the next link may read. Work that is independent of that chain
(the class checks, lane `w`) should stay off it, and the common case (every link in the class, lane `w` holding
`0x7fc00000` or plain values) should run without per-lane scalar work.
