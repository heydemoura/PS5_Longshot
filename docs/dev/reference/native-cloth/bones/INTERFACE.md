# Mesh Bone Deform, the bone loop: the C++ interface

The candidate is one file, `cloth_bones.cpp`, implementing the one function of `cloth_bones.h` (this folder). C++17,
x86-64, SSE / SSE4.1 intrinsics allowed where the build has them (`__SSE4_1__`), with a plain path for a build without
(the harness builds `-march=x86-64-v3`, `-march=x86-64-v2` and plain `-march=x86-64`). Flags:
`-O2 -ffp-contract=off -fno-strict-aliasing -fno-exceptions -fno-rtti`. No new build files, no other source file, no
global state that outlives a call, thread-safe (several threads call it at once).

The kit's helpers may be included: `cloth_wlane.hpp` (namespace `cw`: the Arm's single-precision addition, subtraction
and multiplication lane by lane with their flags, `cw::arm_add`, `cw::arm_sub`, `cw::arm_mul`; the flag bits `cw::IOC`
etc.; `cw::DEFAULT_NAN`; `cw::w_canon` to make every NaN lane of an SSE vector the default NaN; `cw::flush_in`,
`cw::nans` used by those) and `cloth_common.hpp` (namespace `kc`: the reciprocal square root estimate's table `kc::RSQ`
and `kc::frsqrte_bits` for a positive normal; `ld4` / `st4` unaligned SSE loads and stores, `ld16` an unaligned u16 load,
`KC_SPLAT(v, i)` a lane broadcast, `KC_SIMD` = 1 when SSE4.1 is available, `KC_FRSQRTS_FMA` = 1 when the build has FMA3).
Both are header-only; using them is optional. The estimate (for inputs other than positive normals), the step and the
quiet comparison of SPEC.md 2.2 are NOT in the helpers: write them from SPEC.md 2.2.

## The function

```
uint32_t cloth_bones_81ce6500(const ClothBonesArgs *a);
```

| Field | Meaning |
|---|---|
| `positions`, `pos_first` | particle `k`'s position (16 bytes) at `positions + 16 * (k - pos_first)`; `pos_first` is the lowest index any of the run's triangles holds, so every particle a triangle names is in range |
| `triangles`, `tri_first` | the triangle at byte offset `o` (three u16 indices) at `triangles + (o - tri_first)`; `tri_first` is the lowest triangle offset any pair holds |
| `pairs` | `count` pairs of two u16: (output byte offset, triangle byte offset) |
| `records` | `count` records of 64 bytes; record `i` at `records + 64 i`; its bytes 16..31 are never read (and may not be meaningful) |
| `out`, `out_first` | bone `i`'s 64 bytes go to `out + (o_i - out_first)`; `out_first` is the lowest output offset any pair holds |
| `count` | the number of bones, at least 1 |
| `centre_weight` | `C`, four floats as bits |
| `t` | `T`, one float as bits |
| `fz` | 1 when flush-to-zero is on, else 0 |
| `fpsr_in` | the status flags already set at the start (`CLOTH_BN_*` bits) |
| `last` | output: the hand-back of SPEC.md 6 (`ClothBonesLast`, every field written) |

All pointers are host pointers, any alignment (use unaligned loads and stores; triangle and output offsets can be odd).
Inputs are read-only.

**What it does:** writes every bone's 64 bytes exactly as SPEC.md 3 and 4 say (in bone order: where two bones' outputs
overlap, the later bone's bytes are left), and every field of `*last` exactly as SPEC.md 6 says, nothing else (no other
memory, no write outside the bones' 64-byte outputs), and returns the status bits of SPEC.md 5. Return value `R` is
correct when `(fpsr_in | R) == (fpsr_in | raised)`: bits already set at the start, and IXC (always set at a call), may be
omitted.

**It never declines** and never stops part way. Every run that reaches the kernel is done whole, whatever the values
(NaNs, infinities, subnormals, huge or tiny numbers, garbage in lane 3, degenerate triangles, other constants). The hook
(Longshot's `game/oot_cloth.c`, and the check's own shim) declines before calling the kernel when:
- the routine leaves before the loop (its own way out), or the bone count is below one (then the hook gives the loop's
  end state itself; the kernel is not called);
- the float mode is other than round to nearest with no special mode but flush-to-zero, or the inexact flag IXC is
  not yet set (the translated loop runs);
- a range is not mapped (the pairs, the records, the triangles the pairs name, the positions those name, the outputs);
- any output byte meets anything the loop reads (the pairs, the triangles, the positions, the records, the constants,
  the fields the hook reads them from).

The kernel may therefore assume: no output byte overlaps an input; all inputs stay unchanged during the run; outputs of
different bones may overlap each other.

**Float environment on the host:** the kernel runs with the host's default MXCSR (round to nearest, exceptions masked,
no flush-to-zero, no denormals-are-zero) and must leave MXCSR as it found it. The x86 status bits in MXCSR are not
looked at; the flags must come from the kernel's own reasoning (SPEC.md 2 and 7).

## What is checked of a run

The loop is part of a larger game routine. After it, the routine can see: the memory the loop wrote, the cumulative
float status, the values the loop leaves behind (SPEC.md 6), and the loop's own end state (where its walk through the
arrays stopped, its constants, the condition flags), which do not depend on the bones' values. The kernel produces the
memory, the status and the hand-back; the hook puts the hand-back where the routine finds it and sets the loop's own end
state itself. The check compares all of it against the game's own code (HARNESS.md).
