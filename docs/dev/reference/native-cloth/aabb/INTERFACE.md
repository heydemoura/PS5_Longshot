# Particle bounding boxes: the C++ interface

The candidate is one file, `cloth_aabb.cpp`, implementing the one function of `cloth_aabb.h` (this folder). C++17,
x86-64, SSE / SSE4.1 intrinsics allowed where the build has them (`__SSE4_1__`), with a plain path for a build without
(the harness builds `-march=x86-64-v3`, `-march=x86-64-v2` and plain `-march=x86-64`). Flags:
`-O2 -ffp-contract=off -fno-strict-aliasing -fno-exceptions -fno-rtti`, warnings `-Wall -Wextra`. No new build files, no
other source file, no global state that outlives a call, thread-safe (several threads call it at once).

The kit's helpers may be included: `cloth_wlane.hpp` (namespace `cw`: the Arm's single-precision subtraction, addition,
multiplication, minimum, maximum and comparison with zero, lane by lane with their flags: `cw::arm_sub`, `cw::arm_add`,
`cw::arm_mul`, `cw::arm_fmin`, `cw::arm_fmax`, `cw::arm_fcmp_zero_z`; the flag bits `cw::IOC` etc., `cw::FAST_FLAGS`;
`cw::DEFAULT_NAN`; `cw::w_canon` to make every NaN lane of an SSE vector the default NaN) and `cloth_common.hpp`
(namespace `kc`: `ld4` / `ld4u` / `st4u` unaligned SSE loads and stores, `KC_SIMD` = 1 when SSE4.1 is available, `in_r`
the "plain value" test of SPEC.md 5, `Checks` a packed accumulator for that test, `fmin_arm4` the Arm's minimum of four
non-NaN lanes). Both are header-only; using them is optional, and not every function in them is needed here.

## The function

```
uint32_t cloth_aabb_81b38800(const ClothAabbArgs *a, ClothAabbResult *r);
```

| Argument field | Meaning |
|---|---|
| `pos` | `P_0 .. P_{n-1}`: the positions, 16 bytes a particle |
| `prev` | `Q_0 .. Q_{n-1}`: the previous positions, 16 bytes a particle |
| `mask` | `m_0 .. m_{n-1}`: the collision masks, a 32-bit word a particle |
| `inv_mass` | `w_i` at `inv_mass + 16 i`: the inverse masses, one float every 16 bytes (`16 (n - 1) + 4` bytes readable) |
| `n` | the number of particles, at least 1 |
| `start` | `S`, the boxes' start value: four bit patterns, lane 0 first |
| `fz` | 1 when flush-to-zero is on, else 0 |
| `fpsr_in` | the status flags already set when the pass starts (`CLOTH_AABB_*` bits) |

| Result field | Meaning (SPEC.md 6) |
|---|---|
| `pos_min`, `pos_max` | the positions' box |
| `moving_min`, `moving_max` | the moving box (the tracked particles' `mid` and `ahead` points) |
| `marked_min`, `marked_max` | the marked box (the same, tracked particles with mask bit 31 set) |
| `last_pos` | the last particle's position as read |
| `last_mask` | the last particle's mask word |
| `last_mid` | the last particle's `mid`, or (skipped) its inverse mass word in lane 0 and zeros in lanes 1-3 |
| `moved` | 1 when at least one particle was tracked, else 0 (no other value) |
| `ahead`, `ahead_step` | the last tracked particle's `ahead` and `T`; not looked at when `moved` is 0 |

All pointers are host pointers, any alignment (use unaligned loads). Inputs are read-only; `prev` may be the same array
as `pos`.

**What it does:** fills every field of `*r` as SPEC.md 6 says (all 16 bytes of each point) and returns the status bits
of SPEC.md 4. It writes nothing else: no memory, nothing past `*r`. Return value `R` is correct when
`(fpsr_in | R) == (fpsr_in | raised)`: bits already set at the start, and IXC (always set when it is called), may be
omitted.

**It never declines.** Every call that reaches the kernel is done, whatever the values (NaNs, infinities, subnormals,
huge or tiny numbers, garbage in lane 3, odd start values and inverse masses). The hook (Longshot's `game/oot_cloth.c`,
and the check's own shim) handles the rest before calling it:
- a count below 1 (the empty pass: the boxes at their start, no flag) is answered by the hook itself;
- the float mode other than round to nearest with no special mode but flush-to-zero, or the inexact flag IXC not yet
  set: the hook declines and the translated loop runs;
- a range not mapped: the hook declines.

**Float environment on the host:** the kernel runs with the host's default MXCSR (round to nearest, exceptions masked, no
flush-to-zero, no denormals-are-zero) and must leave MXCSR as it found it. The x86 status bits in MXCSR are not looked
at; the flags must come from the kernel's own reasoning (SPEC.md 2 and 5).

## What is checked of a call

The pass is a loop inside a larger routine; the routine's code after the loop reads the boxes and the other values
SPEC.md 6 lists. The hook puts each handed-back value where that code finds it and sets the rest of the loop's end state
itself (the positions past the arrays, the count, the two constants). `check.py` compares, where the hook did the pass,
every byte of the case's memory (nothing may change), the float status, and the whole state at the loop's end against
the game's own code; a mismatch is reported by the field's name.
