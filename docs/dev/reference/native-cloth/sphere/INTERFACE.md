# The sphere contact pass: the C++ interface

The candidate is one file, `cloth_sphere.cpp`, implementing the one function of `cloth_sphere.h` (this folder). C++17,
x86-64, SSE / SSE4.1 intrinsics allowed where the build has them (`__SSE4_1__`; FMA where `__FMA__`), with a plain path
for a build without (the harness builds `-march=x86-64-v3`, `-march=x86-64-v2` and plain `-march=x86-64`). Flags:
`-O2 -ffp-contract=off -fno-strict-aliasing -fno-exceptions -fno-rtti`. No other source file, no global state that
outlives a call, thread-safe (several threads call it at once).

The kit's helpers may be included: `cloth_common.hpp` (namespace `kc`: `kc::frsqrte_bits` the Arm's reciprocal square
root estimate for a positive normal float's bits, `kc::rsq_step` one estimate-and-step as an example of the fused step's
two host forms, `kc::fb` / `kc::bf` bit casts, `kc::ld16`, SSE load / store helpers, `KC_SIMD` = 1 with SSE4.1) and
`cloth_wlane.hpp` (namespace `cw`: one Arm addition, subtraction, multiplication with flush-to-zero and NaN rules,
`cw::w_canon` replacing NaN lanes by the default NaN, `cw::DEFAULT_NAN`). Both are header-only; most of what they hold
is for other cloth routines; using them is optional.

## The function

```
void cloth_sphere_records(const ClothSphereArgs *a, ClothSphereResult *r);
```

Arguments (`ClothSphereArgs`; every float is given as its 32 bits):

| field | meaning (SPEC.md 1) |
|---|---|
| `indices` | `records` x 16 particle numbers, unsigned 16-bit: record `r`'s `k`-th at `indices + 32 r + 2 k` |
| `radii` | `records` x 16 floats: record `r`'s `k`-th particle's contact radius `R_k` at `radii + 64 r + 4 k` |
| `frictions` | the same layout: the friction factor `F_k` |
| `positions` | particle `i`'s position: four floats at `positions + 16 i` (read and written) |
| `previous` | particle `i`'s previous position: four floats at `previous + 16 i` (read and written) |
| `records` | 1 or more |
| `fpcr`, `fpsr` | the float control and cumulative status (Arm layout: flush-to-zero is `fpcr` bit 24) |
| `sphere[4]` | `cx, cy, cz, rho` |
| `eps[4]` | `eps[0..3]`, by place |
| `dt` | the time step |
| `body[3]` | `bx, by, bz` |
| `velocity[4]` | `vx, vy, vz, vw` |
| `spin[3]` | `wx, wy, wz` |

Results (`ClothSphereResult`): `done` and `why` as SPEC.md 8 defines them (`CLOTH_SPHERE_ALL` 0, `_FLOAT_MODE` 1,
`_VALUES` 2, `_REPEATED` 3). `why` must be `CLOTH_SPHERE_ALL` exactly when `done == records`.

All pointers are host pointers, any alignment (use unaligned loads and stores). `indices`, `radii`, `frictions` and `*a`
are read-only.

**What it does:** the records in order (SPEC.md 3-5), each record's stores (SPEC.md 4) and nothing else: no write to a
slot of a particle not in contact, no write to the pair's second particle's previous position, no write for a record it
declines or any after it.

**What the caller guarantees** (the hook declines before calling otherwise): every particle number in every record is a
valid slot of both arrays; `positions` and `previous` do not overlap each other or any input; the inputs do not change
during the call.

**Float environment on the host:** the kernel runs with the host's default MXCSR (round to nearest, exceptions masked,
no flush-to-zero, no denormals-are-zero) and must leave MXCSR as it found it. It must not rely on the host's status bits.

## What is checked of a call

The pass is the record loop of a larger routine of the game. The check runs whole calls of that routine: the game's
instructions in an emulator against the translated routine with your kernel answering its record loop (through a shim:
the inputs gathered, `cloth_sphere_records` called, the routine's record position set from `done`). It compares, where
your kernel did one or more records, every byte of memory, the float status and the routine's live registers at the
loop's end and at the routine's return with the game's; where it did none, everything with the translated routine
alone (your kernel must have written nothing). `HARNESS.md` has the details.
