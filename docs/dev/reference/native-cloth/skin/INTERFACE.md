# Object-space skinning: the C++ interface

The candidate is one file, `cloth_skin.cpp`, implementing the one function of `cloth_skin.h` (this folder). C++17,
x86-64, SSE / SSE4.1 intrinsics allowed where the build has them (`__SSE4_1__`), with a plain path for a build without
(the harness builds `-march=x86-64-v3`, `-march=x86-64-v2` and plain `-march=x86-64`). Flags:
`-O2 -ffp-contract=off -fno-strict-aliasing -fno-exceptions -fno-rtti`. No new build files, no other source file, no
global state that outlives a call, thread-safe (several threads call it at once).

The kit's helpers may be included: `cloth_wlane.hpp` (namespace `cw`: the Arm's single-precision addition and
multiplication lane by lane with their flags, `cw::arm_add`, `cw::arm_mul`; the flag bits `cw::IOC` etc.;
`cw::DEFAULT_NAN`; `cw::FAST_FLAGS`; `cw::w_canon` to make every NaN lane of an SSE vector the default NaN) and
`cloth_common.hpp` (namespace `kc`: `ld4` / `st4` unaligned SSE loads and stores, `ld4u` / `st4u` on `uint32_t[4]`,
`KC_SPLAT(v, i)` a lane broadcast, `KC_SIMD` = 1 when SSE4.1 is available, `ld16`, `fb` / `bf` float bits, `in_r` the
"plain value" test of SPEC.md 5, `Checks` a packed version of it). Both are header-only; using them is optional.

## The function

```
uint32_t cloth_skin_81db9a40(const ClothSkinParams *p, const ClothSkinBlock *b, ClothSkinLast *last);
```

`ClothSkinParams` (the same for every block of a call):

| Field | Meaning |
|---|---|
| `pos_low[4]` | the low 16 bits of each position lane's 32-bit integer (SPEC.md 3 step 1) |
| `weight_scale[4]` | float bits: the factor weight lane `q` is multiplied by |
| `fourth_weight` | float bits: lane 3 of a 3-influence vertex's raw weights, used as it is |
| `fz` | 1 when flush-to-zero is on, else 0 |
| `fpsr_in` | the status flags already set (`CLOTH_SKIN_*` bits) |

`ClothSkinBlock` (one block of 16 vertices):

| Field | Meaning |
|---|---|
| `influences` | `n`: 4, 3, 2 or 1 bone influences per vertex |
| `positions` | 16 x 8 bytes: each vertex's four unsigned 16-bit values |
| `record` | the block's record (SPEC.md 1); readable for `CLOTH_SKIN_READABLE(n)` bytes |
| `bones`, `bone_base` | bone `k`'s 64-byte matrix at `bones + 64 * (k - bone_base)`; every bone the record names is `>= bone_base` |
| `out`, `out_base` | the slot at 32-bit offset `o` is at `out + (o - out_base)`; every slot the block stores has `o >= out_base` |
| `first`, `stride` | vertex `i`'s slot offset is `(uint32_t)((uint32_t)(first + num_i) * stride)` (SPEC.md 1) |

`ClothSkinLast` (written): the values of vertex 15 that SPEC.md 6 lists for the block's `n`, four lanes each as bit
patterns; the fields SPEC.md 6 marks "-" for that `n` are not looked at.

All pointers are host pointers, any alignment (use unaligned loads and stores). Inputs are read-only.

**What it does:** writes the 16 output slots exactly as SPEC.md 3 and 4 say (in vertex order), fills `*last` as
SPEC.md 6 says, nothing else (no other memory, nothing past a slot's 16 bytes), and returns the status bits of SPEC.md 4.
Return value `R` is correct when `(fpsr_in | R) == (fpsr_in | raised)`: bits already set at the call, and IXC (always set
at a call), may be omitted. `last` meets neither the slots nor any input.

**It never declines.** Every block that reaches the kernel is done, whatever the values. The hook (Longshot's
`game/oot_cloth.c`, and the check's own) decides before calling the kernel, and does the block itself in the game's
translated code when:
- the float mode is other than round to nearest with no special mode but flush-to-zero, or the inexact flag IXC is not
  yet set;
- a range is not mapped (the positions, the record, the bones the record names, the slots, the operator's fields);
- the block is not one of the kernel's four formats (SPEC.md 7: 16-bit weights);
- the block's slots meet anything the block reads (positions, record, the named bones, the control byte, the operator's
  fields the routine reads for every vertex).

The kernel may therefore assume: no slot overlaps an input; all inputs stay unchanged during the call; `influences` is
4, 3, 2 or 1.

**Float environment on the host:** the kernel runs with the host's default MXCSR (round to nearest, exceptions masked,
no flush-to-zero, no denormals-are-zero) and must leave MXCSR as it found it. The x86 status bits in MXCSR are not
looked at; the flags must come from the kernel's own reasoning (SPEC.md 2 and 5).

## What is checked of a call

The kernel replaces the vertex loop of one block inside a larger routine; the hook walks the blocks and calls it once
per block. After the loop the routine's code can see: the memory written, the cumulative float status, the values of
SPEC.md 6 (for each influence count, those of its last block), and the loop's integer state (pointers, counters: the
hook's own work, not the kernel's). The check compares all of it at the loop's end against the game's code
(HARNESS.md).
