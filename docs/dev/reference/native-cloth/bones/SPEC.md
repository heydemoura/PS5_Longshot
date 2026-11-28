# Mesh Bone Deform, the bone loop: behavioural spec

For a list of bones, the loop builds each bone's 4x4 frame from a triangle of cloth particles: the triangle's centre and
two edges give a local frame; a per-bone record of three columns says how to combine that frame into an aim direction, an
up direction and an origin; the aim and up directions give three orthogonal axes by cross products, each axis is scaled
by an estimate of its reciprocal length, and the three unit axes and the origin are stored as the bone's matrix. All in
single precision. This file says what it computes precisely enough for identical bits and float status flags, and what
the loop leaves behind for the rest of the game's routine (section 6). `INTERFACE.md` gives the C interface, `HARNESS.md`
how a candidate is checked against the game's own code.

## 0. Conventions

**Floats.** Every value is an IEEE binary32 float. Every operation below is one Arm single-precision addition,
subtraction, multiplication, or one of the three special operations of section 2.2 (the reciprocal square root estimate,
the reciprocal square root step, the quiet comparison), each with its own rounding (round to nearest, ties to even): no
fused multiply-add except where section 2.2 says "rounded once", no reassociation, no wider intermediate that is not
rounded back after each operation. Grouping is exactly as written: `(a + b) + c` is two additions, the first `a + b`.
**Operand order matters**, also for the commutative operations: it decides which NaN an operation returns (section 2).
`a + b` has first operand `a`, second `b`; `a - b` and `a * b` likewise.

**Columns.** A *column* is four floats, lanes 0..3, 16 bytes, lane 0 at the lowest address. For a column `V`, `V[l]` is
its lane `l`; `V.xyz` are lanes 0..2. Column operations work lane by lane on **all four lanes**: `V + W` is
`(V[0] + W[0], ..., V[3] + W[3])`; `V * s` for a scalar `s` is `(V[0] * s, ..., V[3] * s)` with the column's lane the
FIRST operand and the scalar the SECOND in every lane. Every lane of every column operation is computed and its flags
count, lane 3 included, whatever it holds (lane 3 often holds garbage, the default NaN or huge values; see the "flag-only
lane" below). `(a, b, c, d)` is the column with those lanes; `+0` is the bit pattern `0x00000000`.

**The lane rotation** `rot(V) = (V[1], V[2], V[0], V[1])`, and `r(q)` for a lane `q` is the lane it takes: `r(0) = 1`,
`r(1) = 2`, `r(2) = 0`, `r(3) = 1`.

## 1. Inputs

- **The pairs:** `count` pairs (`count` >= 1), pair `i` at `pairs + 4 i`: two unsigned 16-bit little-endian values,
  the bone's *output offset* `o_i` (first) and its *triangle offset* `t_i` (second), both byte offsets.
- **The triangles:** bone `i`'s triangle is the three unsigned 16-bit little-endian particle indices `j0, j1, j2` at byte
  offset `t_i` of the triangle array (at `t_i`, `t_i + 2`, `t_i + 4`; the offset may be odd: no alignment).
- **The positions:** particle `k`'s position is a column (16 bytes) at index `k` of the position array (16 bytes
  apart). The corners are `P0`, `P1`, `P2`, the positions of `j0`, `j1`, `j2`. All four lanes are read and used.
- **The records:** bone `i`'s record is 64 bytes at `records + 64 i`, four columns `R0` (bytes 0..15), `R1` (16..31,
  **never read**), `R2` (32..47), `R3` (48..63).
- **Two constants:** `C`, a column (the centre weights; `1/3` in every lane in the game), and `T`, one float (`1.0` in
  the game). They are the same for every bone.
- **The float mode:** flush-to-zero `fz` on or off (section 2), and the status flags already set at the start.

What is read: the pairs, the triangles they name, the positions those name, the records' columns 0, 2 and 3. What is
written: 64 bytes a bone at the output offset `o_i` of the output array, nothing else in memory (and the hand-back of
section 6). The hook guarantees that no output byte meets anything that is read, so reads never see the loop's own
stores. Two bones' outputs MAY overlap each other (equal offsets, or offsets 4, 16 or 1 byte apart): the bones are
stored in order, bone 0 first, and where outputs overlap the later bone's bytes are the ones left.

## 2. The arithmetic of one operation (the Arm's rules, the float mode as the hook guarantees it)

The hook calls the kernel only with round to nearest, the default-NaN mode off, no alternative half precision or
alternative float handling, no exception traps; flush-to-zero (`fz`) is on or off.

### 2.1 Addition, subtraction, multiplication

One operation of `a` (first operand) and `b` (second):

1. **Flush of inputs (only when `fz` is on):** an operand that is a nonzero subnormal (exponent field 0, fraction not 0)
   is replaced by a zero of the same sign, and the input-denormal flag IDC is raised. This happens for both operands,
   before NaNs are looked at.
2. **NaNs:** if `a` is a signalling NaN (exponent all ones, fraction nonzero, fraction bit 22 clear), the result is `a`
   with bit 22 set, and IOC is raised. Else if `b` is a signalling NaN: `b` with bit 22 set, IOC. Else if `a` is a quiet
   NaN: `a` unchanged (payload and sign kept). Else if `b` is a quiet NaN: `b`. No other flag. (A subtraction does not
   change the sign of a NaN `b`.)
3. **Invalid operations:** `inf + (-inf)` (an addition of infinities of opposite signs, a subtraction of infinities of
   equal signs) and `0 * inf` in either order give the default NaN, bit pattern `0x7fc00000`, and raise IOC.
4. **Infinities and zeros:** as IEEE. A sum of two zeros is `+0` unless both are `-0` (`a - b` is `a + (-b)`). `x + 0`
   with `x` not zero is `x` exactly. An exact cancellation `x + (-x)` is `+0`. A product's sign is the exclusive or of
   the operands' signs; a product with a zero operand (and no infinity) is a zero of that sign. These raise nothing.
5. **Rounding:** otherwise the exact result is rounded once to binary32, to nearest even.
   - If its magnitude **before rounding** is below `2^-126` (the result is *tiny*): with `fz` on, the result is a zero
     of the exact result's sign and UFC is raised (no rounding). With `fz` off, the result is the correctly rounded
     subnormal (or zero) and UFC is raised when that rounding was inexact.
   - If the rounded magnitude exceeds the largest finite float, the result is an infinity of its sign and OFC is raised.
   - IXC (inexact) is raised by any inexact result; it is always already set when the kernel is called, so a kernel need
     not track it.

The kit's helper `cloth_wlane.hpp` implements exactly these rules for one lane: `cw::arm_add(a, b, fz, flags)`,
`cw::arm_sub(a, b, fz, flags)` and `cw::arm_mul(a, b, fz, flags)` on bit patterns (first operand first), ORing the flags
into `flags`. They are the reference for every case, the rare ones included; they are slow.

### 2.2 The three special operations (on single values)

**The reciprocal square root estimate `est(x)`** (the Arm's single-precision reciprocal square root estimate):
1. With `fz` on, a nonzero subnormal `x` is replaced by a zero of its sign, IDC.
2. A NaN: a signalling one is returned with bit 22 set, IOC; a quiet one is returned as it is.
3. `+0` or `-0`: an infinity of the same sign, and the divide-by-zero flag DZC.
4. Any other negative value (`-inf` included): the default NaN `0x7fc00000`, IOC.
5. `+inf`: `+0`, no flag.
6. A positive finite `x` (a normal, or a subnormal when `fz` is off), with biased exponent field `e` and 23-bit fraction
   field `m`: for a subnormal (`e = 0`) first normalise: while bit 22 of `m` is 0, shift `m` left by one and subtract 1
   from `e`; then shift `m` left once more and keep its low 23 bits (the leading one is dropped). `e` is then 0 or
   negative. The table index is `256 * (e mod 2) + (m >> 15)`, where `e mod 2` is 1 for an odd `e` (two's complement:
   `e = -1` is odd). The result is positive, its exponent field `(380 - e) / 2` (integer division; `380 - e` is
   positive), its fraction field `table[index] << 15`. No flag. `table` is the 512-entry table `kc::RSQ.t` of the kit's
   `cloth_common.hpp`; for a positive normal `x` the whole result is `kc::frsqrte_bits(bits of x)`.

**The reciprocal square root step `step(a, b)`** (the Arm's fused reciprocal square root step: `(3 - a * b) / 2`):
1. `a` is negated first (its sign bit flipped, a NaN's too); call it `a'`.
2. With `fz` on, a nonzero subnormal `a'` or `b` is replaced by a zero of its sign, IDC (each).
3. NaNs, with `a'` the first operand and `b` the second: a signalling `a'` quieted (bit 22 set), IOC; else a signalling
   `b` quieted, IOC; else a quiet `a'` as it is; else a quiet `b`.
4. One operand an infinity and the other a zero (either order): `+1.5` (`0x3fc00000`), no flag.
5. Else an infinity operand: an infinity whose sign is the exclusive or of the signs of `a'` and `b`, no flag.
6. Else a zero operand: `+1.5`, no flag.
7. Else the exact value `(3 + a' * b) / 2`, **rounded once** to binary32 (nearest even): no rounding of the product or
   of the sum on the way. An exact zero is `+0`. If the rounded magnitude exceeds the largest finite float: an infinity
   of its sign, OFC. (The result is never tiny: no UFC.) Note that computing `a' * b` exactly in binary64 (it fits) and
   then `3 + that` in binary64 is a second rounding unless the binary64 sum is exact; a result rounded twice can differ
   from the once-rounded one at ties.

**The quiet comparison `L <= K`**, of a value `L` with the constant `K = 0x007fffff` (the largest subnormal,
`2^-126 - 2^-149`):
1. With `fz` on, both operands are flushed as in 2.1 step 1: `K` becomes `+0` and IDC is raised (so with `fz` on, every
   comparison raises IDC); a nonzero subnormal `L` also becomes a zero (IDC).
2. If `L` is a NaN: the comparison is false ("unordered"); a signalling NaN raises IOC, a quiet one nothing.
3. Else it is the ordered comparison of the values (`+0 = -0`).

## 3. One bone

For bone `i`, with its pair `(o_i, t_i)`, its corners `P0, P1, P2`, its record columns `R0, R2, R3`, and the constants
`C`, `T`. Every operation below is performed on all four lanes when it is a column operation, and contributes its flags.

1. **The centre.** `S01 = P0 + P1`; `S = S01 + P2`; `M = S * C` (lane by lane: `M[q] = S[q] * C[q]`, `S` first).
2. **The edges.** `E0 = P0 - M`; `E1 = P1 - M`.
3. **The normal.** For every lane `q` (0..3): `D_N[q] = E0[q] * E1[r(q)] - E1[q] * E0[r(q)]` (two products, the first
   written first operand first; then the first product minus the second). `N = (D_N[1], D_N[2], D_N[0], +0)`: the cross
   product `E0 x E1`.
   **The flag-only lane:** lane 3 of `S01`, `S`, `M`, `E0`, `E1`, of both products and of `D_N` is computed (its flags
   count) but its value is never used again: the next step replaces it.
4. **The local frame.** `E0' = (E0[0], E0[1], E0[2], +0)`, `E1' = (E1[0], E1[1], E1[2], +0)`, `N` (its lane 3 is the
   `+0` above), and `M' = (M[0], M[1], M[2], T)`.
5. **Two directions from the record**, each a combination of the frame's four columns by one record column (`Rk[l]` a
   scalar: a column times a scalar, the frame's lane first):
   - `A = M' * R0[3] + ((E0' * R0[0] + E1' * R0[1]) + N * R0[2])`
   - `Z = M' * R2[3] + ((E0' * R2[0] + E1' * R2[1]) + N * R2[2])`
   In words, for `A`: the inner sum `E0' * R0[0] + E1' * R0[1]` (first operand `E0' * R0[0]`), then that plus
   `N * R0[2]` (that first), then `M' * R0[3]` plus that (`M' * R0[3]` FIRST). The same shape for `Z` with `R2`. All
   four lanes (lane 3 is `T * R0[3] + ((0 * R0[0] + 0 * R0[1]) + 0 * R0[2])`: it can be a real value, a NaN, an
   infinity).
6. **The second axis.** For every lane `q`: `D_Y[q] = Z[q] * A[r(q)] - A[q] * Z[r(q)]`; `Y = (D_Y[1], D_Y[2], D_Y[0], +0)`
   (the cross product `Z x A`).
7. **The first axis.** For every lane `q`: `D_X[q] = Z[r(q)] * Y[q] - Z[q] * Y[r(q)]` (note the first product's
   operand order: `Z`'s rotated lane FIRST, `Y`'s lane second; the second product `Z[q]` first);
   `X = (D_X[1], D_X[2], D_X[0], +0)` (the cross product `Y x Z`).
8. **The three squared lengths and their reciprocal square roots.**
   - `QZ = Z * Z` (all four lanes); `Lz = (QZ[0] + QZ[1]) + QZ[2]`.
   - `QY = Y * Y` (all four lanes); `Ly = QY[2] + (QY[0] + QY[1])` (note: `QY[2]` FIRST).
   - `QX = X * X` (all four lanes); `Lx = QX[2] + (QX[0] + QX[1])` (`QX[2]` first).
   - **The estimate chain** of a squared length `L`: `Ec = est(L)`; `Sc = Ec * Ec`; `Fc = step(L, Sc)` (`L` is the first
     operand, negated by the step); `Gc = Ec * Fc` (`Ec` first). Four operations, single values, each with its flags.
   - `inv_len_z`: the chain of `Lz` is **always** performed (its flags count whatever follows); then if `Lz <= K` (the
     quiet comparison, section 2.2) `inv_len_z = +0`, else `inv_len_z = Gc` of that chain.
   - `inv_len_y`: if `Ly <= K` then `inv_len_y = +0` and the chain of `Ly` is NOT performed (no flags from it); else the
     chain of `Ly` is performed and `inv_len_y = Gc`.
   - `inv_len_x`: likewise with `Lx`: `+0` and no chain when `Lx <= K`; else the chain and its `Gc`.
   The order among these three blocks does not matter for the bits (each reads only its own values); the comparisons'
   flags count as well (IDC every comparison when `fz` is on).
9. **The origin.** `O = M' * R3[3] + ((E0' * R3[0] + E1' * R3[1]) + N * R3[2])`, the same shape and order as `A`.
10. **The unit axes.** `UZ = Z * inv_len_z`, `UY = Y * inv_len_y`, `UX = X * inv_len_x` (all four lanes, the axis's lane
    first, the scalar second).
11. **Stored**, 64 bytes at output offset `o_i`: `(UX[0], UX[1], UX[2], +0)`, `(UY[0], UY[1], UY[2], +0)`,
    `(UZ[0], UZ[1], UZ[2], +0)`, `(O[0], O[1], O[2], T)` (lane 3 of the last column: `T`'s bits as given, not computed).
    Lane 3 of the three axes is stored as `+0` whatever `UX[3]`, `UY[3]`, `UZ[3]` are (those are computed for their
    flags and for the hand-back).

Count: 25 column multiplications and 16 column additions / subtractions a bone, three two-addition sums (single
values), three comparisons and one to three estimate chains of four single operations.

## 4. The bones in order

Bone `0` to `count - 1`, each as section 3, stored in that order (section 1: a later bone's bytes win where outputs
overlap). There is no stop part way: every bone is done whatever its values.

## 5. The flags of a run

The status bits the loop raises are the union, over every operation of every bone (every lane of every column operation,
the three sums, the comparisons, the estimate chains performed), of the flags sections 2.1 and 2.2 give: IOC, DZC, OFC,
UFC, IDC (`fz` on only), IXC. The kernel returns them (INTERFACE.md: bits already set in the status at the start, and
IXC, may be left out). With `fz` on IDC is always raised (the comparisons). DZC comes from the estimate of a zero, so in
practice from `Lz = 0` (its chain always runs).

## 6. Values handed back at the loop's end

The loop has one way out: after its last bone (`count - 1`; the kernel is not called for a count below one). The rest
of the game's routine may read what the loop leaves behind, so the kernel hands back these values of the **last bone**,
each with exactly the bits the maths above gives it (NaN choice, flush and all); nothing extra is computed and no flag is
added:

| Field | Value (of the last bone) |
|---|---|
| `origin` | `(O[0], O[1], O[2], T)`: the stored fourth column |
| `unit_z`, `unit_y`, `unit_x` | `UZ`, `UY`, `UX`, **all four lanes** (lane 3 the computed `Z[3] * inv_len_z` etc., not the stored `+0`) |
| `axis_z`, `axis_y`, `axis_x` | `Z`, `Y`, `X`, all four lanes (`Y[3]` and `X[3]` are the `+0` put there) |
| `inv_len_z`, `inv_len_y`, `inv_len_x` | the three scale factors of step 8 (`+0` where the comparison held) |
| `origin_coords` | `R3`, the record's column 3, its 16 bytes as read |
| `chain_a`, `chain_b` | see below |
| `tri_second`, `tri_third` | `j1` and `j2`, the triangle's second and third particle index (zero-extended) |

`chain_a` and `chain_b`, two values of the last bone's estimate chains:
- if `Ly <= K` was false (the chain of `Ly` was performed): `chain_a = Fc` and `chain_b = Sc` of the chain of `Ly`;
- if `Ly <= K` was true: `chain_a = Ly` and `chain_b = QY[2]`;
- then, if `Lx <= K` was false (the chain of `Lx` was performed), `chain_a` is replaced by `Sc` of the chain of `Lx`
  (`chain_b` stays).

## 7. Exactness notes (not rules: consequences of sections 2 and 3, for a fast implementation)

- **Plain values.** If, for a bone, every operand and every result of every column operation of steps 1 to 10 (lanes 0..2
  of the flag-only steps 1 to 3; all four lanes of everything after), and the three sums of step 8, are zero or finite
  normals of magnitude above `2^-126` (bit patterns, magnitude bits `0x00800001 .. 0x7f7fffff`, or zero), and every
  operand of every multiplication is zero or of magnitude at least `2^-63`, then host IEEE binary32 arithmetic (SSE packed
  single precision with the default MXCSR: round to nearest, no flush, no denormals-are-zero) gives the Arm's bits for
  every column operation, `fz` flushes nothing, no NaN or infinity arises, and those operations raise only IXC.
  (The operand bound matters: without it, a product of two small normals can round to exactly zero, which passes "zero
  or normal" while the Arm raises UFC.)
- **The flag-only lane.** Lane 3 of steps 1 to 3 never reaches a stored or handed-back value; only its flags matter.
  When the status at the start already holds IOC, OFC and UFC, it can add nothing new (with `fz` on IDC is raised by
  every comparison anyway); and when its values are plain as above it raises nothing but IXC. Otherwise its eight
  operations need the lane-by-lane rules of section 2.1 for their flags.
- **Flags already set.** When IOC, OFC and UFC are all already set in the status at the start, only the bits matter.
  Then, if every NaN among the values read (lanes 0..2 of the corners and of `C`, all lanes of the record columns, `T`) is
  exactly `0x7fc00000` (the default NaN), and with `fz` on no value of the bone (operands and results of the column
  operations, products included, and the sums) is a nonzero of magnitude at most `2^-126`, host IEEE arithmetic gives
  the Arm's value in every lane except that a NaN result may have other bits: every NaN result is `0x7fc00000` on the
  Arm, so replace every NaN lane computed by `0x7fc00000` (`cw::w_canon`). (`2^-126` itself is excluded because, under
  `fz`, a result that rounds up to exactly `2^-126` from below is a flushed zero on the Arm.) The game itself runs with
  `fz` on and IOC, OFC, UFC, IXC set; its particles' lane 3 is mostly the default NaN.
- **The estimate chains** are single values and cheap; they must give the Arm's bits and flags in every case (the
  comparisons, too). A fast form: for a positive normal `L`, `est(L)` is `kc::frsqrte_bits`; if `Sc = Ec * Ec` is a
  normal (no flag but IXC) and `L * Sc` lies in `[1/8, 8]`, then `3 - L * Sc` computed in binary64 from the exact binary64
  product is exact, so `(float)(3.0 - (double)L * (double)Sc) * 0.5f` is `step(L, Sc)` rounded once (an FMA
  `fmaf(-L, Sc, 3.0f) * 0.5f` is the same); if that and `Gc = Ec * Fc` are normal, the chain raised nothing but IXC.
  Otherwise (zero, subnormal, NaN or infinite `L`, an overflowing or tiny `Sc`, `L * Sc` outside that range) use the
  rules of 2.2 and `cw::arm_mul`.
- **Otherwise** the bone needs the lane-by-lane rules (`cw::arm_add` / `cw::arm_sub` / `cw::arm_mul` and section 2.2).
  Inputs with NaN payloads, signalling NaNs, infinities, subnormals, huge and tiny values, degenerate triangles (two or
  three corners on one point: zero edges and normals), zero records, and other constants than the game's do occur in the
  checks.
- The operand orders of section 3 can only show in the bits when two NaNs meet in an operation; a vector path that has
  excluded NaNs (first bullet) or made every NaN the default NaN (third bullet) need not keep them.
