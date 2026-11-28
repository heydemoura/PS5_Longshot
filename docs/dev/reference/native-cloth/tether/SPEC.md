# The tether pass: behavioural spec

One loop of the game's Havok cloth solver: for each record of a tether constraint it looks at two cloth particles, an
anchor particle `i` and a pulled particle `j`, and when `j` is farther from `i` than the record's rest length it moves
`j` back towards `i` by a share of the excess (one-sided: `i` is never moved). This file says what the loop computes,
precisely enough for identical float bits and identical float status flags. `INTERFACE.md` gives the C interface
(`cloth_tether.h`), `HARNESS.md` how a candidate is checked against the game's own code.

The loop sits inside a larger routine of the game (which times it); only the loop is the kernel's. What the rest of
the routine can observe of the loop, and so all a kernel has to produce: the bytes it stores (only particle slots), the
float status bits it raises, and a few values of the last record (section 5).

## 0. Conventions

**Numbers.** Every float is IEEE binary32. `0x...` values written next to a float are its bit pattern.

**Vectors.** A vector is four floats, lanes `x, y, z, w` (lanes 0 to 3, in memory order). An operation on vectors is done
lane by lane on all four lanes, the `w` lane included: `w` feeds nothing of `x, y, z` (and no scalar), but its result
is stored and its status flags count. `v * s` with a scalar `s` multiplies every lane of `v` by the same `s` (`v`'s lane
is the first operand).

**One operation = one Arm single-precision operation.** Addition, subtraction, multiplication: the exact result rounded
once to binary32, round to nearest even. No fused operations except the one in 3.5. Grouping is exactly as written:
`(a + b) + c` is two additions in that order. The **first operand** is the left one as written; it decides which NaN
comes out (below). The float control the loop runs under (guaranteed by the caller): round to nearest, default-NaN
mode off, no alternate handling, no traps; flush-to-zero on or off (`fz`).

**The Arm rules for an operation** (the Arm manual's FPAdd, FPSub, FPMul, FPMin, FPCompare, FPRound; `cloth_wlane.hpp`
implements each one exactly, bits and flags):
- *Flush to zero on input* (`fz` = 1 only): every operand that is a nonzero subnormal is replaced by a zero of its sign
  before anything else, and raises Input Denormal (IDC). This holds for every operation: the comparison and the
  minimum too, and constants too (3.4).
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
The loop's status result is the OR of the bits raised by every operation it performs on the way it takes (an operation
not performed raises nothing). The order of operations does not matter for the bits (they are sticky), only which
operations are performed and on which operands.

**The comparison** (3.4) is quiet: a quiet NaN operand gives "unordered" (not equal, not less, not greater) and raises
nothing; a signalling NaN operand gives unordered and raises IOC; with `fz` = 1 a nonzero subnormal operand is flushed
first and raises IDC; `+0 == -0`. It raises no other flag.

**The minimum** `min(a, b)` (FPMin): operands flushed as above (IDC); NaN operands as above (`a` first); otherwise the
smaller value, and of two equal values the bitwise OR of their bits, so `min(+0, -0) = min(-0, +0) = -0`. The result
is one of the (flushed) operands exactly: no rounding, no other flag.

## 1. The data

Guest memory, little-endian; the kernel sees it through host pointers (`INTERFACE.md`). "u16" is an unsigned 16-bit
integer, "f32" a float.

1.1 **The particle slots:** slot `k` is the vector (16 bytes) at `slots + 16 k`, for any `k` from 0 to 65535. These are
the only bytes the loop writes.

1.2 **The records:** `count` records of 12 bytes, record `k` at `records + 12 k`:

| Offset | Type | Meaning |
|---|---|---|
| +0 | u16 | `i`: the anchor particle (slot index) |
| +2 | u16 | `j`: the pulled particle (slot index) |
| +4 | f32 | `rest`: the rest length |
| +8 | f32 | `stiffness` |

`i` and `j` may be equal. The records may lie inside the slots (then a store can change a later record, 2.2).

1.3 **The scale factor** `s`: one float, given with the call, the same for every record. (The game reaches the loop only
with `s > 0` or `s` a NaN; a kernel is checked with any `s`.)

1.4 **The count** `count`: at least 1.

## 2. The loop

2.1 The records `k = 0, 1, ..., count - 1`, in order, each as section 3. The loop has one way out: after its last
record.

2.2 **What is read when.** Each record reads its own four fields, slot `j` and slot `i` afresh, all after the previous
record's store and before its own store. So a record sees every earlier record's store (in the slots, and in the
records themselves when they lie inside the slots), and none of its own.

## 3. One record

3.1 **Read** `i`, `j`, `rest`, `stiffness` from the record; `Pj` = slot `j`, `Pi` = slot `i` (vectors).

3.2 **The offset** (vectors): `D = Pj - Pi`.

3.3 **Squares and the squared length:** `Q = D * D` (all four lanes; `Q.w` is used by nothing, its flags count), then
the scalar `L = (Q.x + Q.y) + Q.z`.

3.4 **Is `L` above the largest subnormal?** Compare `L` with `m = 2^-126 - 2^-149` (bits `0x007fffff`, the largest
subnormal) as a quiet comparison (section 0). With `fz` = 1 both operands are flushed first: `m` always becomes `+0`
and raises IDC, so **every record of every call with `fz` = 1 raises IDC here**; a subnormal `L` becomes zero (IDC as
well). "Above" means `L > m` after flushing, or unordered (a NaN `L`).

3.5 **The reciprocal length `r`.**
- Not above (`L` is a zero or not above `m`): `r = +0`. Nothing is computed for it, no flag.
- Above: `L` is then a positive normal, `+inf` or a NaN (a signalling `L` cannot occur: it is the result of additions).
  Three steps:
  1. `e = estimate(L)`, the Arm's reciprocal square root estimate (FPRSqrtEstimate, single precision): for a positive
     normal `L` an 8-bit-accurate value from the Arm's table (`kc::rsq_step` in `cloth_common.hpp` contains it; it is
     exact for every positive normal float); `estimate(+inf) = +0`; a quiet NaN `L` gives that NaN unchanged. No flag in
     these cases.
  2. `x = e * e` (a multiplication, section 0).
  3. `t = (3 - L * x) / 2` as ONE fused operation (the Arm's FPRSqrtStepFused): the exact value of `(3 - L * x) / 2`
     rounded once (FPRound, section 0; an exact zero gives `+0`). Its operands go through the flush and NaN rules
     (`L` as the first operand); one operand infinite and the other zero (either way round) gives exactly `1.5`, no
     flag; otherwise an infinite operand gives `-inf`.
  4. `r = e * t` (a multiplication; `e` first).
  When `L` is a NaN, `r` is that NaN (it is the first operand of every product leading to `r`).

3.6 **The pull** (scalars):
- `len = L * r`;
- `ex = rest - len`;
- `mn = min(ex, +0)` (the minimum of section 0, `ex` the first operand; so `mn` is `ex` when the tether is stretched,
  else zero, and `rest = -0` with `len = +0` gives `-0`);
- `f = stiffness * mn` (`stiffness` first), then `g = f * s` (`f` first).

3.7 **The move** (vectors):
- `DD = D * r`;
- `DELTA = DD * g`;
- `P' = Pj + DELTA`;
- store `P'` (all 16 bytes) into slot `j`.

That is the whole record. In words: `D` points from the anchor to the pulled particle, `len` is its length, and with a
stretched tether (`len > rest`) the pulled particle moves along `-D` by `(len - rest) * stiffness * s`; a slack tether
moves it by zero (the store happens all the same, and the `w` lane is recomputed all the same).

The `w` lane in full: `D.w = Pj.w - Pi.w`, `Q.w = D.w * D.w` (flags only), `DD.w = D.w * r`, `DELTA.w = DD.w * g`,
`P'.w = Pj.w + DELTA.w`.

## 4. What must be equal, and stopping part way

A kernel does the records in order from the first, and may stop before any record (`INTERFACE.md`):
- **Records done** (`0 .. done - 1`): guest memory afterwards byte for byte as the loop leaves it after those records,
  and the reported status bits equal to the bits those records raise (a bit already set in the guest's status may be
  left out).
- **Records not done** (`done .. count - 1`): nothing of them written; the caller runs the game's own loop from record
  `done` on the memory as the kernel left it. A kernel may stop at any record; it MUST stop at a record it cannot
  answer with the loop's exact bits and status bits. Since a record writes only its own slot `j` and nothing is written
  before a record is complete, stopping needs no undo.
- **Aliasing:** 2.2. The record list may lie inside the slots, and any slot may be read by later records: a kernel that
  keeps a value read from memory (a record, a slot) across a store must read it again, or prove the store did not reach
  it.

## 5. Values handed back at the loop's end

When every record is done, the kernel hands back these values of the **last** record (`k = count - 1`), with exactly
the bits section 3 gives them (NaN choice, flush and all); nothing extra is computed and no flag is added. They are
values the rest of the game's routine may use after the loop.

| Name | Meaning |
|---|---|
| `i`, `j` | the last record's two indices as read (3.1) |
| `moved` | `P'` of the last record, all four lanes: the vector it stored |
| `delta` | `DELTA` of the last record, all four lanes (3.7; its `w` lane as section 3 computes it) |
| `pull` | `g` of the last record (3.6) |
| `len` | `len` of the last record (3.6) |
| `rlen` | `r` of the last record (3.5: `+0` when `L` was not above `m`) |
| `stiffness` | the last record's `stiffness`, its bits as read |

The loop's other state at its end (how many records are left, where the record list ends, its constants) follows from
`count` and the record list's address alone; the caller derives it. When the kernel stops part way, it hands back only
`done`: the game's loop goes on from that record boundary, and its state there likewise follows from `done`.

## 6. How exactness can be reached on x86 (guidance; the rules above are what counts)

These are properties of IEEE and Arm arithmetic, with the helpers in this folder that implement them:
- **The class.** When every operand of every operation is zero or of magnitude in `[2^-63, 2^63)` (`kc::in_r`), every
  product lies in `[2^-126, 2^126)` or is zero and every sum is zero or at least `2^-86` and below `2^64`: nothing is
  tiny, infinite or NaN, so the host's IEEE round-to-nearest gives the Arm's bits, flush-to-zero changes nothing, and
  the only flags are IXC (already set) and the per-record IDC of 3.4 when `fz` = 1. The minimum's operands need only be
  non-NaN and non-subnormal (`kc::fmin_arm`). `s` is an operand of every record's `g`.
- **The step of 3.5** with `L` and `x` in the class: `kc::rsq_step(L, bad)` computes `r` (an FMA where the build has
  one; else exactly in binary64), and sets `bad` when `L * x` is outside `[1/8, 8]`, where it cannot vouch for it. With
  `L` zero it returns `+0`, as 3.5 does.
- **The w lane.** `w` values in the game's data are often the default NaN or huge, outside the class, while `x, y, z`
  are in it. Lane `w` can be computed exactly whatever it holds with `cw::arm_add`, `cw::arm_sub`, `cw::arm_mul`
  (the same operations, operands in the same order, flags returned), or taken from the host vector when the conditions
  in `cloth_wlane.hpp`'s point 2 hold (`cw::w_canon`, `cw::canon_in`, `cw::fz_safe`). Lane `w` of 3.2, 3.3 and 3.7
  uses `Pj.w`, `Pi.w` and the scalars `r` and `g`.
- **A NaN stored** (or handed back) is the one place where hosts differ in bits (x86's default NaN is `0xffc00000`, the
  Arm's `0x7fc00000`, and x86 may pick the other operand's NaN): take it from the rules of section 0, or stop.
- The host float environment: round to nearest, its denormals-are-zero and flush-to-zero modes off (`INTERFACE.md`).
