# The bending pass kernel: the C++ interface

The candidate is ONE file, `cloth_bend.cpp`, implementing `cloth_bend_pass` of `cloth_bend.h` (this folder). C++17,
x86-64, SSE / SSE4.1 / AVX / FMA intrinsics allowed where the build has them (test `__SSE4_1__`, `__AVX__`, `__FMA__`),
built with `-O2 -ffp-contract=off -fno-strict-aliasing` for `-march=x86-64-v3`, `-march=x86-64-v2` and `-march=x86-64`
(plain SSE2: the kernel must build and be exact there too; it may be a slower form). It may include only `cloth_bend.h`,
`cloth_wlane.hpp`, `cloth_common.hpp` (this folder; use them as they are, do not copy them into the file) and standard
or compiler headers. No `-ffast-math`, no new build files, no other source files.

## The call

```c
void cloth_bend_pass(ClothBendPass *p);
```

The hook (the glue that hooks the game's routine; not yours) calls it for a whole call of the game's bending pass, once
it has:
- found `n >= 1` (a count below 1: the routine does nothing and the kernel is not called);
- checked the float modes: flush to zero on or off (`p->fz`), everything else at its default (round to nearest, no
  default-NaN mode, no traps), and IXC already set in the guest's float status (`p->fpsr_in & 0x10`);
- resolved the guest pointers: `slots` and `particles` each with their whole reach (65,536 slots of 16 bytes), `records`
  for `32 * n` bytes, `scale` for 4 bytes, all mapped;
- read the three pairs the routine reads once at entry (`threshold`, `numerator`, `alt_stiffness`; their bits), having
  made sure no slot store can reach where they came from.

The host's float environment is its default (MXCSR `0x1f80`: round to nearest, no flush to zero, no denormals-are-zero,
every exception masked). The kernel may rely on it and must leave it as it found it.

### Inputs (`ClothBendPass`, see the header's comments)
| Field | Meaning (SPEC.md) |
|---|---|
| `slots` | the particle slots (1.1); read and written |
| `particles` | the particle data; the float at `+4` of particle `s` is its inverse mass. May be the same memory as `slots`. |
| `records` | `n` records of 32 bytes (1.2). May lie inside the slots' memory (then the pass's own stores change later records). |
| `scale` | the float `k`, read anew for every record (it may lie inside the slots' memory) |
| `n` | the record count, >= 1 |
| `threshold[2]`, `numerator[2]`, `alt_stiffness[2]` | `THR`, `NUM`, `ALT` as float bits |
| `fz` | flush to zero (1) or not (0) |
| `fpsr_in` | the status flags set when the call starts (bit layout of SPEC.md 3) |

### What it may read and write
- Read: the records, the slots and particle data of the slot numbers the records name, `*scale`; nothing else of the
  guest. Reads must follow SPEC.md 2.1 (a record's reads after the previous record's stores; the three reloads after
  the stores named there). The pointers are `uint8_t`: copy with `memcpy` or unaligned loads (`_mm_loadu_ps`); nothing
  is guaranteed aligned beyond 4 bytes.
- Write: only the four slots of each record it does, 16 bytes each, in the order of SPEC.md 2.1, nothing of a record it
  does not do. No other guest memory, no global state that changes the results of a later call.

### Outputs
| Field | Meaning |
|---|---|
| `done` | the records done and stored, `0 .. n`; record `done` (when `done < n`) was not touched |
| `fpsr` | the flags the pass raised over records `0 .. done-1` (SPEC.md 3), IXC aside; bits already in `fpsr_in` may be included or left out (the hook ORs this in). Ignored when `done == 0`. |
| `slot_lo`, `slot_hi` | the lowest and highest slot number stored over the records done (`slot_lo > slot_hi` when none). The hook marks those pages as written. |
| `why` | `CLOTH_BEND_ALL` when `done == n`; else `CLOTH_BEND_ENTRY` (SPEC.md 4.1, then `done` = 0), `CLOTH_BEND_CLASS` (4.2), `CLOTH_BEND_STEP` (4.3), or `CLOTH_BEND_OTHER` (any other reason; avoid) |

### What the hook does with it
- `done == 0`: the game's routine runs the whole call (so nothing may have been written).
- `done == n`: nothing more; the slots hold the result, the hook ORs `fpsr` into the guest's float status.
- `0 < done < n`: the hook ORs `fpsr` in, then runs the game's routine on records `done .. n-1` (the same state as the
  pass reaching record `done`: nothing but memory is carried from one record to the next).

There is no return value and no register state to give back: the game's routine is a leaf that changes only the slots and
the float status as far as any caller can see (its caller reads none of the scratch registers it leaves).

## Speed
`HARNESS.md` gives the speed to meet (ns a record, the kernel we have now, on the bench of `check.py --bench`). The
pass is latency-bound per record (three reciprocal square roots and a division in a chain, then the stores that the next
record may read); work that is independent of that chain (the class checks) should stay off it, and the common case
(every record in the class, lane `w` holding `0x7fc00000` or plain values) should run without per-lane scalar work.
