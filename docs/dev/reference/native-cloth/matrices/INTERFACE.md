# Prepare Matrices: the C++ interface

The candidate is one file, `cloth_matrices.cpp`, implementing the one function of `cloth_matrices.h` (this folder).
C++17, x86-64, SSE / SSE4.1 intrinsics allowed where the build has them (`__SSE4_1__`), with a plain path for a build
without (the harness builds `-march=x86-64-v3`, `-march=x86-64-v2` and plain `-march=x86-64`). Flags:
`-O2 -ffp-contract=off -fno-strict-aliasing -fno-exceptions -fno-rtti`. No new build files, no other source file, no
global state that outlives a call, thread-safe (several threads call it at once).

The kit's helpers may be included: `cloth_wlane.hpp` (namespace `cw`: the Arm's single-precision addition and
multiplication lane by lane with their flags, `cw::arm_add`, `cw::arm_mul`; the flag bits `cw::IOC` etc.;
`cw::DEFAULT_NAN`; `cw::w_canon` to make every NaN lane of an SSE vector the default NaN) and `cloth_common.hpp`
(namespace `kc`: `ld4` / `st4` unaligned SSE loads and stores, `KC_SPLAT(v, i)` a lane broadcast, `KC_SIMD` = 1 when
SSE4.1 is available, `in_r` the "plain value" test of SPEC.md 5). Both are header-only; using them is optional.

## The function

```
uint32_t cloth_matrices_81cdff40(const ClothMatricesArgs *a);
```

| Field | Meaning |
|---|---|
| `model` | `M`: 64 bytes, four columns of four floats |
| `bones` | the bone matrices; bone `k` at `bones + 64 k` (only those the indices name are guaranteed readable) |
| `index` | `N` unsigned 16-bit little-endian bone indices |
| `in` | `N` matrices, 64 bytes each |
| `out` | `N` matrices, 64 bytes each: the results (written) |
| `count_word` | the routine's count word: `N = 16 * ((count_word >> 4) & 0xffff) + (count_word & 15)`, and which entries are "in a group" (SPEC.md 1, 3) |
| `fz` | 1 when flush-to-zero is on, else 0 |
| `fpsr_in` | the status flags already set at the call (`CLOTH_MX_*` bits) |
| `left` | output: `CLOTH_MX_LEFT` (7) columns of 16 bytes, `left[k]` at `left + 16 k`: the values left for the caller (SPEC.md 6); every byte counts |

All pointers are host pointers, any alignment (use unaligned loads and stores). Inputs are read-only.

**What it does:** writes `out[0 .. 64 N)` exactly as SPEC.md 3 says and the seven columns at `left` exactly as SPEC.md
6 says (all 16 bytes of each, four lanes), nothing else (no other memory, no write past the last entry's 64 bytes), and
returns the status bits of SPEC.md 4. The hook calls it only when `N >= 1` (for `N = 0` it leaves everything as it was);
`left` meets neither the output nor any input. Return value `R` is correct when
`(fpsr_in | R) == (fpsr_in | raised)`: bits already set at the call, and IXC (always set at a call), may be omitted.

**It never declines.** Every call that reaches the kernel is done, whatever the values (NaNs, infinities, subnormals,
huge or tiny numbers, garbage in lane 3). The hook (Longshot's `game/oot_cloth.c`, and the check's own shim) declines
before calling the kernel when:
- the float mode is other than round to nearest with no special mode but flush-to-zero, or the inexact flag IXC is
  not yet set (the lifted routine runs);
- a range is not mapped (`M`, the indices, the entries, the output, the bones up to the largest index);
- the output meets anything the routine reads (`M`, the indices, the entries, the named bones), or any of these meets
  the routine's own stack use: the `0xc30` bytes below the stack pointer, which the game's routine uses as scratch.

The kernel may therefore assume: `out` overlaps no input; all inputs stay unchanged during the call.

**Float environment on the host:** the kernel runs with the host's default MXCSR (round to nearest, exceptions masked,
no flush-to-zero, no denormals-are-zero) and must leave MXCSR as it found it. The x86 status bits in MXCSR are not
looked at; the flags must come from the kernel's own reasoning (SPEC.md 2 and 5).

## What is checked of a call

The routine is a leaf with no return value. A caller can see, after it returns: the memory it wrote, the cumulative
float status, the callee-saved registers, the stack pointer, and (on the one path SPEC.md 6 describes) the seven
columns it leaves. The kernel produces the memory, the status and the seven columns; the hook puts the seven columns
where the caller finds them and leaves every other register as it was at the call (the callee-saved ones are then what
the routine restores). Nothing else of the routine's exit state (its other scratch registers, its flags, its stack
scratch below the stack pointer) is reproduced or compared.
