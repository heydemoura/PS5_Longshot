# The Verlet integration step: the C++ interface

The candidate is one file, `cloth_verlet.cpp`, implementing the one function of `cloth_verlet.h` (this folder). C++17,
x86-64, SSE / SSE4.1 intrinsics allowed where the build has them (`__SSE4_1__`), with a plain path for a build without
(the harness builds `-march=x86-64-v3`, `-march=x86-64-v2` and plain `-march=x86-64`). Flags:
`-O2 -ffp-contract=off -fno-strict-aliasing -fno-exceptions -fno-rtti`. No new build files, no other source file, no
global state that outlives a call, thread-safe (several threads call it at once).

The kit's helpers may be included: `cloth_wlane.hpp` (namespace `cw`: the Arm's single-precision addition, subtraction
and multiplication lane by lane with their flags, `cw::arm_add`, `cw::arm_sub`, `cw::arm_mul`; the flag bits `cw::IOC`
etc.; `cw::DEFAULT_NAN`; `cw::FAST_FLAGS`; `cw::w_canon` to make every NaN lane of an SSE vector the default NaN) and
`cloth_common.hpp` (namespace `kc`: `ld4` / `st4` unaligned SSE loads and stores, `KC_SPLAT(v, i)` a lane broadcast,
`KC_SIMD` = 1 when SSE4.1 is available, `in_r` the "plain value" test of SPEC.md 6). Both are header-only; using them
is optional.

## The function

```
uint32_t cloth_verlet_81c66a60(const ClothVerletArgs *a);
```

This is the whole loop, not the routine around it: the code before and after it stays the game's (translated) code.

| Field | Meaning |
|---|---|
| `pos` | the positions, `16 N` bytes: read, and each particle's new position written (SPEC.md 3) |
| `prev` | the previous positions, `16 N` bytes: read, and each overwritten with the particle's position before the step |
| `force` | the gathered forces, `16 N` bytes, read |
| `scale` | the scale factors: `s_a` at `scale + 16 i`, `s_b` at `scale + 16 i + 4`. Only `16 (N - 1) + 8` bytes are guaranteed readable: do not read the last particle's other 8 bytes |
| `gravity` | `G`, 16 bytes, read |
| `groups`, `singles` | the particles: `groups` groups of four, then `singles` (0..3) one at a time, `N = 4 groups + singles >= 1`; the split matters only for the hand-back (SPEC.md 1, 5) |
| `dt2` | `h`, the squared time step, as binary32 bits |
| `damping` | `D`, four binary32 bit patterns, lane 0 first |
| `fz` | 1 when flush-to-zero is on, else 0 |
| `fpsr_in` | the status flags already set when the loop starts (`CLOTH_VL_*` bits) |
| `last` | output: the values handed back (`ClothVerletLast`, SPEC.md 5) |

All pointers are host pointers, any alignment (use unaligned loads and stores). `force`, `scale` and `gravity` are
read-only; they may overlap each other (they are only read).

**What it does:** writes `pos[0 .. 16 N)` and `prev[0 .. 16 N)` exactly as SPEC.md 3 says, the fields of `*last` that
SPEC.md 5 names for the call's case (all 16 bytes of each column field, all four lanes), nothing else (no other memory,
no write past the last particle's 16 bytes, nothing past `*last`), and returns the status bits of SPEC.md 4. The hook
calls it only when `N >= 1`; `last` meets no other range. Return value `R` is correct when
`(fpsr_in | R) == (fpsr_in | raised)`: bits already set at the call, and IXC (always set at a call), may be omitted.

**It never declines and never stops part way.** Every call that reaches the kernel is done whole, whatever the values
(NaNs, infinities, subnormals, huge or tiny numbers, garbage in lane 3). The hook (Longshot's `game/oot_cloth.c`, and the
check's own shim) declines before calling the kernel when:
- the float mode is other than round to nearest with no special mode but flush-to-zero, or the inexact flag IXC is
  not yet set (the translated loop runs);
- a range is not mapped (the positions, the previous positions, the forces, the scale factors, `G`);
- the positions or the previous positions meet each other or anything the loop reads (the forces, the scale factors,
  `G`).

The kernel may therefore assume: `pos` and `prev` overlap neither each other nor any input; all inputs stay unchanged
during the call.

**Float environment on the host:** the kernel runs with the host's default MXCSR (round to nearest, exceptions masked,
no flush-to-zero, no denormals-are-zero) and must leave MXCSR as it found it. The x86 status bits in MXCSR are not
looked at; the flags must come from the kernel's own reasoning (SPEC.md 2 and 6).

## What is checked of a call

The loop's effects that the rest of the game can see: the memory it writes, the cumulative float status, and the
values it leaves for the code after it. The kernel produces the memory, the status and the hand-back; the hook puts the
hand-back where the code after the loop finds it, and sets the loop's remaining end state itself from the arguments
(how far the array cursors moved, the count of singles and what testing it leaves). So the check compares everything
at the loop's end against the game's own code: every byte of the memory regions, the float status, and the whole
register state the code after the loop starts from. A wrong field of the hand-back shows by its name.
