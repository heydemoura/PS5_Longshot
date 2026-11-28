# The bend link pass kernel: the C++ interface

The candidate is ONE file, `cloth_bend_link.cpp`, implementing `cloth_bend_link_pass` of `cloth_bend_link.h` (this
folder). C++17, x86-64, SSE / SSE4.1 / AVX / FMA intrinsics allowed where the build has them (test `__SSE4_1__`,
`__AVX__`, `__FMA__`), built with `-O2 -ffp-contract=off -fno-strict-aliasing` for `-march=x86-64-v3`,
`-march=x86-64-v2` and `-march=x86-64` (plain SSE2: the kernel must build and be exact there too; it may be a slower
form). It may include only `cloth_bend_link.h`, `cloth_wlane.hpp`, `cloth_common.hpp` (this folder; use them as they
are, do not copy them into the file) and standard or compiler headers. No `-ffast-math`, no new build files, no other
source files. Thread-safe: no global state that outlives a call (constant tables are fine).

## The call

```c
void cloth_bend_link_pass(ClothBendLinkPass *p);
```

The hook (the glue that hooks the game's routine at its loop; not yours) calls it for the whole loop, once it has:
- found `n >= 1` (with a count below 1 the loop does not run and the kernel is not called);
- checked the float modes: flush to zero on or off (`p->fz`), everything else at its default (round to nearest, no
  default-NaN mode, no traps), and IXC already set in the guest's float status (`p->fpsr_in & 0x10`);
- resolved the guest pointers: `slots` and `particles` each with their whole reach (65,536 slots of 16 bytes), `links`
  for `20 * n` bytes, all mapped;
- read the values the game reads once before the loop: the clamp pair and the scale (their bits).

The host's float environment is its default (MXCSR `0x1f80`: round to nearest, no flush to zero, no denormals-are-zero,
every exception masked). The kernel may rely on it and must leave it as it found it.

### Inputs (`ClothBendLinkPass`, see the header's comments)
| Field | Meaning (SPEC.md) |
|---|---|
| `slots` | the particle slots (1.1); read and written |
| `particles` | the particle data; the float at `+4` of particle `s` is its inverse mass. May be the same memory as `slots`. |
| `links` | `n` links of 20 bytes (1.2). May lie inside the slots' memory (then the pass's own stores change later links). |
| `n` | the link count, >= 1 |
| `clamp[2]` | `c` as float bits |
| `scale` | `s` as float bits |
| `fz` | flush to zero (1) or not (0) |
| `fpsr_in` | the status flags set when the pass starts (bit layout of SPEC.md 3) |

### What it may read and write
- Read: the links, the slots and particle data of the slot numbers the links name; nothing else of the guest. Reads
  must follow SPEC.md 2.1 (a link's reads after the previous link's stores; the reload after the first store). The
  pointers are `uint8_t`: copy with `memcpy` or unaligned loads (`_mm_loadu_ps`); nothing is guaranteed aligned beyond
  2 bytes for the links and 4 bytes for the slots.
- Write: only the two slots of each link it does, 16 bytes each, in the order of SPEC.md 2.1, nothing of a link it does
  not do; and the output fields of `*p`. No other guest memory, no global state that changes the results of a later
  call.

### Outputs
| Field | Meaning |
|---|---|
| `done` | the links done and stored, `0 .. n`; link `done` (when `done < n`) was not touched |
| `fpsr` | the flags the pass raised over links `0 .. done-1` (SPEC.md 3), IXC aside; bits already in `fpsr_in` may be included or left out (the hook ORs this in). Ignored when `done == 0`. |
| `slot_lo`, `slot_hi` | the lowest and highest slot number stored over the links done (`slot_lo > slot_hi` when none). The hook marks those pages as written. |
| `why` | `CLOTH_BEND_LINK_ALL` when `done == n`; else `CLOTH_BEND_LINK_ENTRY` (SPEC.md 4.1, then `done` = 0), `..._CLASS` (4.2), `..._STEP` (4.3), or `..._OTHER` (any other reason; avoid) |
| `end` | when `done == n`: the values handed back at the loop's end, SPEC.md 5 (every field, every lane, exact bits). Not looked at when `done < n`. |

### What the hook does with it
- `done == 0`: the game's loop runs every link (so nothing may have been written).
- `done == n`: the hook ORs `fpsr` into the guest's float status and gives the rest of the game's routine the values of
  `end` (and the ones that follow from the inputs, SPEC.md 5); the slots hold the result.
- `0 < done < n`: the hook ORs `fpsr` in, then the game's loop runs links `done .. n-1` itself, from link `done` (the same
  state as the pass reaching link `done`: nothing but memory is carried from one link to the next); `end` is not used.

## Speed
`HARNESS.md` gives the speed to meet (ns a link, the kernel we have now, on the bench of `check.py --bench`). The pass is
latency-bound per link (a reciprocal square root, then the corrections, then the stores that the next link may read);
work that is independent of that chain (the class checks) should stay off it, and the common case (every link in the
class, lane `w` holding `0x7fc00000` or plain values) should run without per-lane scalar work. Filling `end` costs
nothing per link if it is done once, after the last link.
