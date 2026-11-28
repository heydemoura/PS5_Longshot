# The anchor pass: behavioural spec

One routine of the game's Havok cloth solver: for each record of an anchor constraint it takes a point of the animated
body mesh (an "anchor vertex", moved into place by the skin's 4x4 matrix) and, when the record's cloth particle has
drifted, moves the particle back by a share of the distance outside a radius around that point. This file says what
the routine computes, precisely enough for identical float bits and identical float status flags. `INTERFACE.md` gives
the C interface (`cloth_anchor.h`), `HARNESS.md` how a candidate is checked against the game's own routine.

What a caller of the routine can observe, and so all a kernel has to produce: the bytes it stores (only particle slots)
and the float status bits it raises. It returns no value.

## 0. Conventions

**Numbers.** Every float is IEEE binary32. `0x...` values written next to a float are its bit pattern.

**Vectors.** A vector is four floats, lanes `x, y, z, w` (lanes 0 to 3, in memory order). An operation on vectors is done
lane by lane on all four lanes, the `w` lane included: `w` feeds nothing of `x, y, z` (and no scalar), but its result
is stored and its status flags count. `(a, b, c, d)` is a vector; `v * s` with a scalar `s` multiplies every lane of
`v` by the same `s` (`v`'s lane is the first operand).

**One operation = one Arm single-precision operation.** Addition, subtraction, multiplication: the exact result rounded
once to binary32, round to nearest even. No fused operations except the one in 3.7. Grouping is exactly as written:
`((a + b) + c) + d` is three additions in that order. The **first operand** is the left one as written; it decides
which NaN comes out (below). The float control the routine runs under (guaranteed by the caller): round to nearest,
default-NaN mode off, no alternate handling, no traps; flush-to-zero on or off (`fz`).

**The Arm rules for an operation** (the Arm manual's FPAdd, FPSub, FPMul, FPMin, FPCompare, FPRound; `cloth_wlane.hpp`
implements each one exactly, bits and flags):
- *Flush to zero on input* (`fz` = 1 only): every operand that is a nonzero subnormal is replaced by a zero of its sign
  before anything else, and raises Input Denormal (IDC). This holds for every operation: the comparisons and the
  minimum too, and constants too (3.6).
- *NaN operands:* if an operand is a signalling NaN (exponent all ones, fraction nonzero, bit 22 clear), the result is
  the first such operand with bit 22 set, and Invalid (IOC) is raised; otherwise, if an operand is a quiet NaN, the
  result is the first such operand unchanged. "First" is the first operand as written.
- *Invalid:* `inf - inf` (also `inf + -inf`), `0 * inf` (either order): the default NaN `0x7fc00000`, IOC.
- Infinities and zeros as IEEE: a product's sign is the XOR of the signs; a sum of two zeros is `+0` unless both are
  `-0`; an exact cancellation `x - x` gives `+0`.
- *Rounding (FPRound):* an exact nonzero result of magnitude below `2^-126` (tiny, judged BEFORE rounding) is, with
  `fz` = 1, a zero of its sign with Underflow (UFC); with `fz` = 0 it is rounded to the nearest subnormal, with UFC
  when that rounding is inexact. A result past the largest float after rounding is an infinity of its sign with
  Overflow (OFC).
- Inexact (IXC) is raised by every inexact result, but the caller only calls a kernel when IXC is already set in the
  guest's float status, so it need not be reported.

**Status bits** (the guest's float status register): IOC bit 0, DZC bit 1, OFC bit 2, UFC bit 3, IXC bit 4, IDC bit 7.
The routine's status result is the OR of the bits raised by every operation it performs on the way it takes (an
operation not performed raises nothing). The order of operations does not matter for the bits (they are sticky), only
which operations are performed and on which operands.

**Comparisons** (3.6, 2.2, 3.8) are quiet: a quiet NaN operand gives "unordered" (not equal, not less, not greater) and
raises nothing; a signalling NaN operand gives unordered and raises IOC; with `fz` = 1 a nonzero subnormal operand is
flushed first and raises IDC; `+0 == -0`. They raise no other flag.

**The minimum** `min(a, b)` (FPMin): operands flushed as above (IDC); NaN operands as above (`a` first); otherwise the
smaller value, and of two equal values the bitwise OR of their bits, so `min(+0, -0) = min(-0, +0) = -0`. The result
is one of the (flushed) operands exactly: no rounding, no other flag.

## 1. The data

Guest memory, little-endian. Offsets are from the start of each object. "u16", "i32", "u64" are unsigned 16-bit,
signed 32-bit, unsigned 64-bit integers; "f32" a float.

1.1 **The constraint** (the routine's 1st argument):

| Offset | Type | Meaning |
|---|---|---|
| +0x28 | u64 | address of the short record list (16-byte records) |
| +0x30 | i32 | number of short records |
| +0x38 | u64 | address of the long record list (20-byte records) |
| +0x40 | i32 | number of long records |
| +0x6c | f32 | strength |

1.2 **The particle slots** (2nd argument, `slots`): slot `i` is the vector (16 bytes) at `slots + 16 i`, for any
`i` from 0 to 65535. These are the only bytes the routine writes.

1.3 The 3rd argument is not used.

1.4 **The skin** (4th argument):

| Offset | Type | Meaning |
|---|---|---|
| +0x18 | u64 | address of anchor vertex 0 (`vbase`) |
| +0x24 | u8 | the distance in bytes between consecutive anchor vertices (`stride`, 0 to 255) |
| +0x90 | 4 x f32 | matrix column `c0` (a vector) |
| +0xa0 | 4 x f32 | column `c1` |
| +0xb0 | 4 x f32 | column `c2` |
| +0xc0 | 4 x f32 | column `c3` |

Anchor vertex `a` is the vector at `vbase + stride * a` (an unsigned product, below 2^24); its `x, y, z` are used, its
`w` is read with it and not used.

1.5 **The factor** (5th argument): a float `factor` at +0. The routine reads the 8 bytes `[factor, factor + 8)`; the
second 4 are not used.

1.6 **The scale** (6th argument): an address, or 0 for none. When not 0: `sx`, `sy`, `sz`, three f32 at +0x18, +0x1c,
+0x20.

1.7 **The constant table.** Its address is the u64 stored in the guest cell `CLOTH_ANCHOR_CONSTANTS_CELL`
(`cloth_anchor.h`). Read from memory, never hard-coded (the harness changes them):

| Offset | Type | Meaning |
|---|---|---|
| +0x270 | 4 x f32 | `eps`, a vector (normally `2^-23` in every lane) |
| +0x50 | 2 x f32 | the limit pair `lim0`, `lim1` (normally `0, 0`) |

1.8 **The records.** A short record is 16 bytes, a long record 20:

| Offset | Type | Meaning |
|---|---|---|
| +0 | u16 | `p`: the particle (slot index) |
| +2 | u16 | `a`: the anchor vertex |
| +4 | f32 | `radius` |
| +8 | 8 bytes | not used (the routine may read bytes +8 to +11) |
| +16 | f32 | long records only: `stiffness` |

Record `i` of a list starts at `list + 16 i` (short) or `list + 20 i` (long). The routine reads, of a record, at most
its bytes +0 to +11 and (long records) +16 to +19: all inside `[list, list + size * n)`.

## 2. The call

2.1 **Strength.** `kf = strength * factor` (one multiplication, flags as section 0).

2.2 **Scaled or not.** When the scale address is 0: not scaled; nothing of the scale is read. Otherwise read `sx`, `sy`,
`sz`, then compare `sx` with `1.0`; only when they are equal compare `sy` with `1.0`; only when those are equal compare
`sz` with `1.0` (quiet comparisons, section 0). The call is **not scaled** when all three comparisons were made and
found equal, **scaled** otherwise (a NaN, or a value flushed to zero, is not equal to `1.0`).

2.3 **Which list.** Read the short count. When it is not zero, the call works on the short list with that count `n`.
When it is zero, read the long count; when that is not zero, the call works on the long list with that count `n`;
when both are zero the call ends here (it has written nothing; its status bits are those of 2.1 and 2.2). A count is a
signed 32-bit number; a negative count makes the routine run far past its list (a kernel declines it).

So there are four forms: short or long records, scaled or not.

2.4 **Read once, at entry** (with `n >= 1`): the list's address (the one of the list taken), the constant table's
address from the cell, then `eps` and the limit pair. These values, `kf`, the scaled decision and `n` hold for the
whole call: a record's store that changes any of these bytes in memory does not change what later records use.

2.5 **The records**, `i = 0, 1, ..., n - 1` in order, each as section 3. A record's reads all happen after the previous
record's store, so they see it.

## 3. One record

3.1 **Read**, each time afresh (any of these bytes may have been changed by an earlier record's store):
- from the record: `p`, `a`, `radius`, and for long records `stiffness`;
- from the skin: `vbase`, `stride` and the four columns `c0, c1, c2, c3`;
- `V`, anchor vertex `a` (1.4);
- `P`, slot `p`;
- scaled calls only: `sx` from the scale.

3.2 **The anchor point** (vectors): `T0 = c0 * V.x`, `T1 = c1 * V.y`, `T2 = c2 * V.z`, then
`W = ((c3 + T0) + T1) + T2`.

3.3 **The offset:** `D = eps + (P - W)` (vectors: first `P - W`, then `eps + ` that).

3.4 **Squares:** `Q = D * D` (all four lanes; `Q.w` is used by nothing, its flags count).

3.5 **Squared length** (scalar): `L = (Q.x + Q.y) + Q.z`.

3.6 **Is `L` above the largest subnormal?** Compare `L` with `m = 2^-126 - 2^-149` (bits `0x007fffff`, the largest
subnormal) as a quiet comparison (section 0). With `fz` = 1 both operands are flushed first: `m` always becomes `+0`
and raises IDC, so **every record of every call with `fz` = 1 raises IDC here**; a subnormal `L` becomes zero (IDC as
well). "Above" means `L > m` after flushing, or unordered (a NaN `L`).

3.7 **The reciprocal length `r`.**
- Not above (`L` is a zero or not above `m`): `r = +0`. Nothing is computed for it, no flag.
- Above: `L` is then a positive normal, `+inf` or a quiet NaN. Three steps:
  1. `e = estimate(L)`, the Arm's reciprocal square root estimate (FPRSqrtEstimate, single precision): for a positive
     normal `L` an 8-bit-accurate value from the Arm's table (`kc::rsq_step` in `cloth_common.hpp` contains it; it is
     exact for every positive normal float); `estimate(+inf) = +0`; a quiet NaN `L` gives that NaN unchanged. No flag in
     these cases.
  2. `x = e * e` (a multiplication, section 0).
  3. `s = (3 - L * x) / 2` as ONE fused operation (the Arm's FPRSqrtStepFused): the exact value of `(3 - L * x) / 2`
     rounded once (FPRound, section 0; an exact zero gives `+0`). Its operands go through the flush and NaN rules
     (`L` as the first operand); one operand infinite and the other zero (either way round) gives exactly `1.5`, no
     flag; otherwise an infinite operand gives `-inf`.
  4. `r = e * s` (a multiplication; `e` first).
  When `L` is a NaN, `r` is that NaN (it is the first operand of every product leading to `r`).

3.8 **The radius used.** Scaled calls: compare `sx` with `0` (quiet). `sel = +0` (bits `0x00000000`) when `sx < 0`,
else `sel = sx` with its bits unchanged (so `-0` stays `-0`, a NaN stays that NaN, a subnormal stays itself: the
flushing applies to the comparison only, it raises IDC there when `fz` = 1). Then `radius' = sel * radius`. Calls
that are not scaled: `radius' = radius` (no operation).

3.9 **The share** (scalars):
- `len = L * r`;
- `k = kf` (short records) or `k = kf * stiffness` (long records);
- `e = radius' - len`, then `g = e * k`;
- `c0 = min(g, lim0)`, `c1 = min(g, lim1)` (the minimum of section 0, `g` the first operand).

3.10 **The move** (vectors):
- `DD = D * r`;
- `DELTA = DD * (c0, c1, c0, c1)` (lane by lane: `x` and `z` by `c0`, `y` and `w` by `c1`);
- `P' = P + DELTA`;
- store `P'` (all 16 bytes) into slot `p`.

That is the whole record. In words: `W` is the skinned anchor point, `D` the particle's offset from it (nudged by
`eps`), `len` its length, and the particle is moved along `D` by `min((radius' - len) * k, limit)`, which with the
usual limit `0` only ever pulls a particle that is farther than `radius'` back towards the anchor.

## 4. What must be equal, and when to decline

A kernel answers a call whole or not at all (`INTERFACE.md`):
- **Done:** guest memory afterwards byte for byte as the routine leaves it, and the reported status bits equal to the
  routine's (a bit already set in the guest's status may be left out).
- **Declined:** nothing written anywhere the caller can see; the caller then runs the game's routine. A kernel may
  decline any call; it MUST decline a call it cannot answer with the routine's exact bits and status bits. A kernel that
  finds such a record after storing earlier ones must put every slot it stored back to its bytes at entry before it
  declines.
- **Memory:** ask the caller's `range` for every range before reading or writing it (NULL: decline). The routine reads
  only: the constraint's fields (1.1), the factor's 8 bytes, the scale's 12 bytes (when its address is not 0), the
  constants cell, `eps` and the pair (only when `n >= 1`), and per record the record, the skin's fields (1.4), the
  vertex, the slot and (scaled) `sx`. Asking for more (a whole 1 MB slot span, a vertex span of `stride * 65535 + 16`
  bytes) is allowed; it only costs a decline when that larger range is not mapped.
- **Aliasing:** the stores go to slots, and the slots may overlap anything else the routine reads (records, vertices,
  the skin, the scale, the constraint, the table). 2.4 and 3.1 say what is read once and what afresh. A kernel that
  keeps a value read afresh by the routine (for example the skin's columns or `sx`) across a store must decline when a
  store can reach those bytes (reason ALIAS), or read them again.

## 5. How exactness can be reached on x86 (guidance; the rules above are what counts)

These are properties of IEEE and Arm arithmetic, with the helpers in this folder that implement them:
- **The class.** When every operand of every operation is zero or of magnitude in `[2^-63, 2^63)` (`kc::in_r`), every
  product lies in `[2^-126, 2^126)` or is zero and every sum is zero or at least `2^-86` and below `2^64`: nothing is
  tiny, infinite or NaN, so the host's IEEE round-to-nearest gives the Arm's bits, flush-to-zero changes nothing, and
  the only flags are IXC (already set) and the per-record IDC of 3.6 when `fz` = 1. The minimum's operands need only be
  non-NaN and non-subnormal (`kc::fmin_arm`, `kc::fmin_arm4`).
- **The step of 3.7** with `L` and `x` in the class: `kc::rsq_step(L, bad)` computes `r` (an FMA where the build has
  one; else exactly in binary64), and sets `bad` when `L * x` is outside `[1/8, 8]`, where it cannot vouch for it.
- **The w lane.** `w` values in the game's data are often the default NaN or huge, outside the class, while `x, y, z`
  are in it. Lane `w` can be computed exactly whatever it holds with `cw::arm_add`, `cw::arm_sub`, `cw::arm_mul`
  (the same operations, operands in the same order, flags returned), or taken from the host vector when the conditions
  in `cloth_wlane.hpp`'s point 2 hold (`cw::w_canon`, `cw::canon_in`, `cw::fz_safe`). Lane `w` of 3.2 to 3.4 and 3.10
  uses the columns' and `eps`'s lane `w`, `P.w`, and the scalars `V.x`, `V.y`, `V.z`, `r` and `c1`.
- **A NaN stored** is the one place where hosts differ in bits (x86's default NaN is `0xffc00000`, the Arm's
  `0x7fc00000`, and x86 may pick the other operand's NaN): take it from the rules of section 0, or decline.
- The host float environment: round to nearest, its denormals-are-zero and flush-to-zero modes off (`INTERFACE.md`).
