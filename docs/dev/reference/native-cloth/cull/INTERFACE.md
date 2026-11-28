# The capsule cull: the C++ interface

The candidate is one file, `cloth_cull.cpp`, implementing the one function of `cloth_cull.h` (this folder). C++17,
x86-64, SSE / SSE4.1 intrinsics allowed where the build has them (`__SSE4_1__`), with a plain path for a build without
(the harness builds `-march=x86-64-v3`, `-march=x86-64-v2` and plain `-march=x86-64`). Flags:
`-O2 -ffp-contract=off -fno-strict-aliasing -fno-exceptions -fno-rtti`. No new build files, no other source file, no
global state that outlives a call, thread-safe (several threads call it at once).

The kit's helpers may be included: `cloth_wlane.hpp` (namespace `cw`: the Arm's comparisons lane by lane with their
flags, `cw::arm_fcmge`, `cw::arm_fcmp_zero_z`; the flag bits `cw::IOC`, `cw::IDC`) and `cloth_common.hpp` (namespace
`kc`: `ld4` / `st4u` unaligned SSE loads and stores, `KC_SPLAT(v, i)` a lane broadcast, `KC_SIMD` = 1 when SSE4.1 is
available). Both are header-only; using them is optional, and most of what they hold is for other cloth routines.

## The function

```
void cloth_cull_81b5a3c0(const ClothCullArgs *a, ClothCullResult *r);
```

Arguments (`ClothCullArgs`):

| Field | Meaning |
|---|---|
| `positions` | `n` positions, 16 bytes each (four floats x, y, z, w); particle `i`'s at `positions + 16 i` |
| `masses` | mass form: particle `i`'s inverse mass, a float at `masses + 16 i` (only the first 4 of each 16 bytes are its; the last particle's 4 bytes are the last readable ones); `NULL` in the mask form |
| `masks` | mask form: particle `i`'s collision mask, a 32-bit integer at `masks + 4 i`; `NULL` in the mass form |
| `box` | box test on: the low corner (four floats), then the high corner (four floats), 32 bytes; `NULL` when the box test is off |
| `list` | the output: unsigned 16-bit indices, entry `j` at `list + 2 j`; room for `n` entries |
| `n` | 1 to 65535 |
| `group` | mask form: the collider's group bits `g` |
| `mass_form` | 1: the mass form; 0: the mask form |
| `box_test` | 1: the box test is on; 0: off |
| `fz` | 1 when flush-to-zero is on, else 0 |

Results (`ClothCullResult`), all as SPEC.md 5 and 6 define them:

| Field | Meaning |
|---|---|
| `count` | the number of particles taken |
| `status` | the flags raised: `CLOTH_CULL_IOC` (0x01), `CLOTH_CULL_IDC` (0x80), exactly those the pass raises |
| `inside_all[4]`, `inside_yz[4]`, `inside_z[4]` | the last particle's verdict vectors, valid as SPEC.md 6 says |
| `mass_examined`, `last_mass` | whether the pass ended by examining the last particle's inverse mass, and that mass's bits |
| `word_set`, `word` | the last-decision word |

All pointers are host pointers, any alignment (use unaligned loads and stores). Inputs are read-only.

**What it does:** writes `list[0 .. 2 count)` exactly as SPEC.md 3 and 4 say and fills `*r`, nothing else (no write to
the list past the last entry taken). Every field of `*r` that SPEC.md 6 says is handed back in the case at hand must be
set; a field it calls "any value" may be left as it is. The hook calls it only with `1 <= n <= 65535` (it handles an
empty pass itself).

**It never declines.** Every call that reaches the kernel is done, whatever the values (NaNs, infinities, subnormals,
an inverted box, garbage in lane 3). The hook (Longshot's `game/oot_cloth.c`, and the check's own shim) declines before
calling the kernel when:
- the float mode is other than round to nearest with no special mode but flush-to-zero (the lifted loop runs);
- a range is not mapped (the positions, the masses or masks, the box, the list);
- more than 65535 particles;
- the list meets the positions, the masses or masks, or the box.

The kernel may therefore assume: `list` overlaps no input; all inputs stay unchanged during the call.

**Float environment on the host:** the kernel runs with the host's default MXCSR (round to nearest, exceptions masked,
no flush-to-zero, no denormals-are-zero) and must leave MXCSR as it found it. The x86 status bits in MXCSR are not
looked at; the flags must come from the kernel's own reasoning (SPEC.md 2, 5 and 7).

## What is checked of a call

The pass is a loop inside a larger routine of the game. After it, the routine reads on: the memory the pass wrote, the
cumulative float status, and the values SPEC.md 6 hands back. The kernel produces the list, the status and those
values; the hook puts the values where the routine finds them and sets the rest of the loop's end state (which does not
depend on the data) itself. The check compares the whole state at the loop's end with the game's.
