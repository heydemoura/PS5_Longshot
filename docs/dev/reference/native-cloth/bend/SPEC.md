# The cloth bending pass: behavioural spec

One pass of the cloth solver's bending constraints (Havok cloth, as the game runs it), to be answered on the host with
the game's exact bits and float status. This file says what the pass computes, precisely enough for identical bits and
flags, and which records a kernel does itself (section 4). `INTERFACE.md` gives the C entry point; `HARNESS.md` how a
candidate is checked against the game's own routine.

## 0. Conventions

**Numbers.** Every float is IEEE binary32. Every operation below is ONE Arm single-precision operation (add, subtract,
multiply, divide) with its own rounding to nearest even; there is no fused multiply-add anywhere except inside the
reciprocal square root step (0.4), no reassociation, and grouping is exactly as written: `(a + b) + c` is two additions
in that order. Operand order is as written (`a * b`: `a` is the first operand); it matters only for which NaN a result
carries (0.2).

**Vectors.** A *vector* has four lanes `(x, y, z, w)` = lanes 0, 1, 2, 3; every vector operation is done on all four
lanes, lane `w` included, because its result is stored and its status flags count. A *pair* has two lanes `(p0, p1)`;
a pair operation computes those two lanes only. `v * f` with a vector `v` and a single float `f` multiplies each lane of
`v` by `f` (`v.x * f`, ...; `v`'s lane is the first operand). `P4(p)` = the vector `(p0, p1, p0, p1)` made from a pair
(no arithmetic). `bcast(f)` = `(f, f, f, f)`.

**Arm rules for one operation** (the call runs with round to nearest, default-NaN mode off, no traps; flush to zero is
on or off as the call says, `fz`). They matter for lane `w` (which can hold anything) and for any value outside the
class of section 4; for a record in the class they reduce to plain IEEE (4.5):
- 0.1 Flush to zero (`fz` = 1): a nonzero subnormal operand is read as a zero of its sign and raises IDC (each operand,
  before NaNs are looked at); a result whose exact value is nonzero and below 2^-126 in magnitude (tininess BEFORE
  rounding) becomes a zero of its sign and raises UFC. With `fz` = 0, subnormal results are kept (UFC when inexact).
- 0.2 NaNs (default-NaN mode off): if either operand is a signalling NaN, the first such operand (first operand first) is
  returned quieted (bit 22 set) and IOC is raised; else if either is a quiet NaN, the first such operand is returned as
  it is. An invalid operation without a NaN operand (inf - inf, 0 * inf, 0 / 0, inf / inf) gives the default NaN
  `0x7fc00000` and raises IOC.
- 0.3 Otherwise IEEE: exact zeros (a sum of zeros is +0 unless both are -0; an exact cancellation is +0; a product's sign
  is the XOR), infinities, overflow to infinity with OFC, division of a nonzero by zero gives an infinity and DZC.
- IXC (inexact) is always set already when the kernel runs; it is never reported.
The helpers `cw::arm_add`, `cw::arm_sub`, `cw::arm_mul` in `cloth_wlane.hpp` implement exactly this for one lane and
return the flags they raise.

**0.4 The reciprocal square root of a squared length** `RS(L)` (L is a pair whose two lanes are equal):
- The *tiny test*: a quiet compare of `L` with `TINY`, the largest subnormal (bits `0x007fffff`), on lane 0. It passes
  when `L > TINY` or when `L` is a NaN (unordered). A quiet compare raises IOC only for a signalling NaN; under flush to
  zero both operands are flushed first, so `TINY` reads as 0 and **IDC is raised by every tiny test when `fz` = 1**.
- If the test fails: `RS(L) = (+0, +0)`.
- If it passes, on both lanes: `e = estimate(L)`, the Arm single-precision reciprocal square root estimate (an 8-bit
  table; `kc::frsqrte_bits` in `cloth_common.hpp` gives it for a positive normal `L`); `e2 = e * e`;
  `g = step(L, e2)`, the Arm fused reciprocal square root step: `(3 - L * e2) / 2` computed exactly and rounded ONCE;
  `RS(L) = e * g`. (`kc::rsq_step` in `cloth_common.hpp` is this whole function for `L` zero or in the class of
  section 4, with the range test of 4.3.)

## 1. The data

### 1.1 Inputs (INTERFACE.md says how each reaches the kernel)
- **Particle slots**: 16-byte slots of four floats `(x, y, z, w)`, a particle position in `x, y, z`; `w` holds other
  data the pass carries along. Slot number `s` (an unsigned 16-bit number) is at `slots + 16 s`. Read and written.
- **Particle data**: 16 bytes a particle, the same numbering; the float at `+4` is the particle's inverse mass. Read only.
- **Records**: `n` records of 32 bytes, one bending constraint each (1.2). Read only.
- **The scale `k`**: one float (the time-step scale), read anew for every record.
- **Three pairs read once at entry**: the threshold `THR = (thr0, thr1)`, the numerator `NUM = (num0, num1)` (normally
  `(1, 1)`), the alternative stiffness `ALT = (alt0, alt1)` (normally `(0, 0)`).
- `fz`: flush to zero on or off for the whole call.

### 1.2 One record (32 bytes)
| Offset | Type | Name | Meaning |
|---|---|---|---|
| +0x00 | float | `f0` | stencil coefficient of particle A |
| +0x04 | float | `f1` | stencil coefficient of particle D |
| +0x08 | float | `f2` | stencil coefficient of particle B |
| +0x0c | float | `f3` | stencil coefficient of particle C |
| +0x10 | float | `f4` | stiffness factor (times `k`) |
| +0x14 | float | `f5` | gain of the dihedral term |
| +0x18 | u16 | `sa` | slot of particle A |
| +0x1a | u16 | `sd` | slot of particle D |
| +0x1c | u16 | `sb` | slot of particle B |
| +0x1e | u16 | `sc` | slot of particle C |

Geometrically: triangles (A, B, C) and (D, B, C) share the edge B-C; the record pulls the four particles along one
correction vector, each by its coefficient, the stiffness and its inverse mass.

## 2. The pass

If `n < 1` (the count read as a signed 32-bit number) nothing is read or written. Otherwise `THR`, `NUM`, `ALT` are read
once, then records `i = 0 .. n-1` are done in order, each completely (its four stores) before the next is read.

### 2.1 Memory order (aliasing is real: slots may repeat within a record, and the records, the particle data and `k` may
lie inside the slots' memory)
- Record `i` reads its 32 bytes, its four slots A = slot `sa`, B = slot `sb`, C = slot `sc`, D = slot `sd`, the four
  inverse masses `ma, mb, mc, md` (particle data of `sa, sb, sc, sd`, at `+4`) and `k`, all AFTER record `i-1`'s four
  stores and BEFORE any store of its own.
- It stores four 16-byte slots, in this order: A' to slot `sa`, D' to `sd`, B' to `sb`, C' to `sc`.
- Three values are read again from memory between the stores: `Dr` = slot `sd` read after A' is stored, `Br` = slot `sb`
  read after D' is stored, `Cr` = slot `sc` read after B' is stored. Reading them from memory at those points is always
  right. When slot numbers repeat, a later store overwrites an earlier one and a reload sees the earlier store, so a
  kernel that keeps the values in registers instead must use `Dr = (sd == sa) ? A' : D`; `Br = (sb == sd) ? D' :
  (sb == sa) ? A' : B`; `Cr = (sc == sb) ? B' : (sc == sd) ? D' : (sc == sa) ? A' : C` (slots are 16-byte aligned
  from one base: two slots are the same memory or do not overlap).
- Nothing of record `i + 1` (its bytes, slots, masses, `k`) may be read before record `i`'s last store: a store may have
  changed it.

### 2.2 One record

Vectors, all four lanes (lane `w` included):
1. `E1 = A - B`, `E2 = C - B`, `E3 = D - B`.
2. With `Rot(v) = (v.y, v.z, v.x, v.y)`: `P = E2 * Rot(E1)`, `Q = E1 * Rot(E2)`, `N1 = P - Q`;
   `P2 = E3 * Rot(E2)`, `Q2 = E2 * Rot(E3)`, `N2 = P2 - Q2`.
   (`N1`, `N2` are the two triangles' normals with their components in the order (z, x, y): lane `w` of each is
   computed and then not used.)
3. Squared lengths, pairs with two equal lanes (each `+` one addition, each square one multiplication):
   `L1 = (N1.y * N1.y + N1.z * N1.z) + N1.x * N1.x`;
   `L2 = N2.x * N2.x + (N2.y * N2.y + N2.z * N2.z)`.
   `r1 = RS(L1)`, `r2 = RS(L2)` (0.4).
4. `Prime(N) = (N.y, N.z, N.x, +0)` (lane moves, no arithmetic): `N1p = Prime(N1)`, `N2p = Prime(N2)`.
   `M = N1p * bcast(r1.lane0) + N2p * bcast(r2.lane0)` (two vector products, then their sum, first product first).
   `len1 = L1 * r1`, `len2 = L2 * r2` (pairs).
   `MM = M * M` (vector, lane `w` included); `Lm = (MM.x + MM.y) + MM.z` (pair, equal lanes); `rm = RS(Lm)`.
5. `EE = E2 * E2` (vector; lane `w` is computed and not used); `Le2 = EE.z + (EE.x + EE.y)` (pair, equal lanes).
   `ll = len1 * len2` (pair).
6. `Dw = D * f1`, `Aw = A * f0`, `Nm = M * bcast(rm.lane0)`, `AD = Aw + Dw` (vectors).
7. `quo = NUM / Le2` (pair: `num0 / Le2`, `num1 / Le2`; ALWAYS computed, its flags count even when it is not used).
   `sel = (the exponent field of Le2's lane 0 is zero) ? (+0, +0) : quo` (a selection on the bits: Le2 zero or
   subnormal gives `(+0, +0)`).
   `t = ll * sel` (pair: `ll * sel0`, `ll * sel1`).
8. `kq = f4 * k` (a single float).
   `u = t * f5`, `uu = u * u` (pairs). `mask = uu > THR` per lane, a SIGNALLING compare: a NaN operand (quiet or
   signalling) gives false and raises IOC; under flush to zero both operands are flushed first (IDC for a subnormal).
   `st = mask ? ALT : (kq, kq)` per lane (a bitwise selection: lane j is `altj` where the mask is set, else `kq`).
9. `Bw = B * f2`, `ABD = AD + Bw`, `Cw = C * f3`, `cv = ABD + Cw` (vectors; so `cv = ((A f0 + D f1) + B f2) + C f3`).
   `TN = Nm * P4(t)`, `G = TN * f5`, `cvp = G + cv` (vectors; lanes x, z use `t0`, lanes y, w use `t1`).
10. Per particle (pairs): `ka = f0 * st`, `kd = st * f1`, `kb = st * f2`, `kc = st * f3` (`f0 * st` is
    `(f0 * st0, f0 * st1)`, `st * f1` is `(st0 * f1, st1 * f1)`); `KA = ka * ma`, `KD = kd * md`, `KB = kb * mb`,
    `KC = kc * mc`.
11. The moves and stores (vectors; again lanes x, z use the pair's lane 0, lanes y, w its lane 1):
    `dA = P4(KA) * cvp`; `A' = A + dA`; store A' to slot `sa`.
    `dD = cvp * P4(KD)`; reload `Dr`; `D' = Dr + dD`; store D' to slot `sd`.
    `dB = cvp * P4(KB)`; reload `Br`; `B' = Br + dB`; store B' to slot `sb`.
    `dC = cvp * P4(KC)`; reload `Cr`; `C' = dC + Cr`; store C' to slot `sc`.

Every operation listed is done for every record, on every lane listed (pairs: two lanes, vectors: four), and its flags
count; the order in which they are done does not change the result (the float status is sticky), only which are done.

### 2.3 Lane `w`
Lane `w` of a vector never feeds lanes x, y, z or a pair: `Rot` and `Prime` drop it, the squared lengths add lanes
x, y, z only, and every value it is multiplied by (`E1.y`, `E2.y`, `E3.y`, the coefficients, the pairs' lane 1, `rm`,
`r1`, `r2`) comes from elsewhere. In lane `w` the record computes, with the Arm rules of section 0 and in this operand
order:
- `E1.w = A.w - B.w`, `E2.w = C.w - B.w`, `E3.w = D.w - B.w`;
- `P.w = E2.w * E1.y`, `Q.w = E1.w * E2.y`, `N1.w = P.w - Q.w`; `P2.w = E3.w * E2.y`, `Q2.w = E2.w * E3.y`,
  `N2.w = P2.w - Q2.w`; `EE.w = E2.w * E2.w` (these five results are not used; their flags count);
- `MM.w = M.w * M.w` with `M.w = (+0 * r1) + (+0 * r2)`, `Nm.w = M.w * rm` (from lanes x-z data: for a record in the
  class these are +0 and raise nothing);
- `Dw.w = D.w * f1`, `Aw.w = A.w * f0`, `AD.w = Aw.w + Dw.w`, `Bw.w = B.w * f2`, `ABD.w = AD.w + Bw.w`,
  `Cw.w = C.w * f3`, `cv.w = ABD.w + Cw.w`;
- `TN.w = Nm.w * t1`, `G.w = TN.w * f5` (for a record in the class a zero whose sign is that of `t1 * f5`),
  `cvp.w = G.w + cv.w`;
- `dA.w = KA1 * cvp.w`, `A'.w = A.w + dA.w`; `dD.w = cvp.w * KD1`, `D'.w = Dr.w + dD.w`;
  `dB.w = cvp.w * KB1`, `B'.w = Br.w + dB.w`; `dC.w = cvp.w * KC1`, `C'.w = dC.w + Cr.w`.
Lane `w` of the stored slots is these values exactly, NaN payloads included (0.2), and their flags count.

## 3. The float status the pass raises
Report every flag the operations of section 2 raise (Arm bit layout: IOC 0x01, DZC 0x02, OFC 0x04, UFC 0x08, IXC 0x10,
IDC 0x80), IXC aside. For records in the class of section 4 that is exactly:
- IDC, once any record was done, when `fz` = 1 (the tiny tests of 0.4 flush `TINY`);
- per record whose `Le2` is zero: the quotient's flags: lane j with `numj` nonzero divides a nonzero by zero (DZC),
  with `numj` zero computes 0 / 0 (IOC);
- whatever lane `w` raises (2.3; any of IOC, OFC, UFC, IDC).
Nothing else: lanes x, y, z and the pairs of a record in the class raise no other flag.

## 4. What the kernel does itself, and what it must decline

The kernel is called with `n >= 1`, flush to zero on or off, the float modes otherwise at their defaults, and IXC set
(the hook's gate). It does records from the first while each is in the class below, and stops before the first record
that is not, having written nothing of it (`why` says why; INTERFACE.md). A record in the class must be done.

**R** is the set of floats that are zero (either sign) or have a magnitude in [2^-63, 2^63): bits without the sign
`0x00000000` or `0x20000000` to `0x5effffff`. (`kc::in_r`.)

### 4.1 Once for the call (`why` = ENTRY, done = 0)
Decline the whole call when a lane of `THR` is a NaN or a nonzero subnormal, or a lane of `NUM` or `ALT` is not in R.

### 4.2 The record class (`why` = CLASS)
A record is in the class when every one of these is in R (lanes x, y, z of vectors; both lanes of pairs; lane `w`
never counts):
- the inputs: A, B, C, D (lanes x-z), `f0` to `f5`, `k`, `ma`, `md`, `mb`, `mc`;
- `E1`, `E2`, `E3`, `P`, `Q`, `P2`, `Q2`, `N1`, `N2`;
- the three squares, the partial sum and `L1`; likewise for `L2`; `r1`, `r2`, `len1`, `len2`;
- `N1p * r1`, `N2p * r2` and `M` (all four lanes: lane `w` of these is +0 in the class);
- the three squares of `MM`, the partial sum, `Lm`, `rm`; the three squares of `EE`, the partial sum, `Le2`; `ll`;
- `Dw`, `Aw`, `AD`, `Nm` (all four lanes);
- `quo` (both lanes) when `Le2`'s exponent field is nonzero; `kq`; `t`; `u`;
- `Bw`, `ABD`, `Cw`, `cv`; `TN`, `G` (all four lanes); `cvp`;
- `ka`, `kd`, `kb`, `kc`, `KA`, `KD`, `KB`, `KC`;
- `dA`, `dD`, `dB`, `dC`; the reloaded `Dr`, `Br`, `Cr`.
Not checked: `uu` (only compared), the estimate `e`, `e2` and the step `g` of 0.4, and the stored sums A', D', B', C'
(each the sum of two R values: zero or a normal number, exact as IEEE rounds it).

### 4.3 The step range (`why` = STEP)
Where the fused step of 0.4 is computed without a hardware fused multiply-add, it is exact only when `L * e2` lies in
[1/8, 8] (`kc::rsq_step` sets `bad` otherwise). The kernel stops before a record where one of its three steps (for
`L1`, `L2`, `Lm`, when the tiny test passes) has `L * e2` outside [1/8, 8]; this is checked in every build (it never
happens for values in R: `L * e2` is about 1).

### 4.4 Lane `w` never stops a record
Whatever lane `w` holds (NaNs of any payload, infinities, subnormals, huge values), a record whose lanes x-z are in the
class is done, lane `w` computed by 2.3 with the Arm rules and its flags reported. Two exact ways, both described in
`cloth_wlane.hpp`: every lane `w` operation through `cw::arm_add` / `arm_sub` / `arm_mul`; or the host vector's own lane
`w` with every NaN made `0x7fc00000` (`cw::w_canon`) under the conditions given there (the call's status already holds
IOC, OFC and UFC; every NaN read into lane `w` is `0x7fc00000`; under flush to zero no lane `w` value of the record is a
nonzero of magnitude at most 2^-126).

### 4.5 Why the class is exact
For values in R: a product or quotient of two of them is zero or in [2^-126, 2^126), a normal number; a sum or difference
is zero or a multiple of 2^-86 below 2^64, a normal number. So no result is tiny, infinite or NaN, IEEE round to nearest
on the host gives the Arm bits, flush to zero changes nothing, no flag but IXC is raised (plus those of section 3), and
the operand order of an operation does not matter in lanes x-z. Exact zeros follow IEEE on both (an exact cancellation
is +0). The estimate is a table lookup; the step is one rounding of an exact value (with a hardware fused multiply-add,
`fmaf(-L, e2, 3) * 0.5f`; without one, in binary64 under the range of 4.3).

### 4.6 After a stop
The kernel has stored records `0 .. done-1` (each exactly as the pass would) and nothing of record `done`; the game's
routine then runs records `done .. n-1` itself. The status reported covers records `0 .. done-1` only.
