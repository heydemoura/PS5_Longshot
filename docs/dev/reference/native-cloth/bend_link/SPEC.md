# The cloth bend link pass: behavioural spec

One pass of the cloth solver's bend links (Havok cloth, as the game runs it): minimum / maximum distance constraints
between two particles, done link by link, to be answered on the host with the game's exact bits and float status. This
file says what the pass computes, precisely enough for identical bits and flags, which links a kernel does itself
(section 4), and which values it hands back when it has done them all (section 5). `INTERFACE.md` gives the C entry
point; `HARNESS.md` how a candidate is checked against the game's own code.

The pass is a loop inside a larger game routine; the rest of that routine stays the game's. The kernel does the loop.

## 0. Conventions

**Numbers.** Every float is IEEE binary32. Every operation below is ONE Arm single-precision operation (add, subtract,
multiply, maximum) with its own rounding to nearest even; there is no fused multiply-add anywhere except inside the
reciprocal square root step (0.4), no reassociation, and grouping is exactly as written: `(a + b) + c` is two additions
in that order. Operand order is as written (`a * b`: `a` is the first operand); it matters only for which NaN a result
carries (0.2) and for the maximum of two zeros (0.5).

**Vectors.** A *vector* has four lanes `(x, y, z, w)` = lanes 0, 1, 2, 3; every vector operation is done on all four
lanes, lane `w` included, because its result is stored and its status flags count. A *pair* has two lanes `(p0, p1)`;
a pair operation computes those two lanes only. `v * f` with a vector `v` and a single float `f` multiplies each lane of
`v` by `f` (`v.x * f`, ...; `v`'s lane is the first operand). `P4(p)` = the vector `(p0, p1, p0, p1)` made from a pair
(no arithmetic). A single float `f` used where a pair is needed is the pair `(f, f)`.

**Arm rules for one operation** (the pass runs with round to nearest, default-NaN mode off, no traps; flush to zero is
on or off as the pass says, `fz`). They matter for lane `w` (which can hold anything) and for any value outside the
class of section 4; for a link in the class they reduce to plain IEEE (4.5):
- 0.1 Flush to zero (`fz` = 1): a nonzero subnormal operand is read as a zero of its sign and raises IDC (each operand,
  before NaNs are looked at); a result whose exact value is nonzero and below 2^-126 in magnitude (tininess BEFORE
  rounding) becomes a zero of its sign and raises UFC. With `fz` = 0, subnormal results are kept (UFC when inexact).
- 0.2 NaNs (default-NaN mode off): if either operand is a signalling NaN, the first such operand (first operand first) is
  returned quieted (bit 22 set) and IOC is raised; else if either is a quiet NaN, the first such operand is returned as
  it is. An invalid operation without a NaN operand (inf - inf, 0 * inf) gives the default NaN `0x7fc00000` and raises
  IOC.
- 0.3 Otherwise IEEE: exact zeros (a sum of zeros is +0 unless both are -0; an exact cancellation is +0; a product's sign
  is the XOR), infinities, overflow to infinity with OFC.
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

**0.5 The maximum** `MAX(a, b)` (one Arm maximum, per lane): under flush to zero both operands are flushed first (0.1);
NaNs as 0.2; otherwise the larger of the two, and of two equal values the bitwise AND of their bits (so the maximum of
`+0` and `-0`, either order, is `+0`; of `-0` and `-0` it is `-0`). `cw::arm_fmax` implements it for one lane. For
values in the class of section 4 it is the larger value, or the AND of the bits when they compare equal.

## 1. The data

### 1.1 Inputs (INTERFACE.md says how each reaches the kernel)
- **Particle slots**: 16-byte slots of four floats `(x, y, z, w)`, a particle position in `x, y, z`; `w` holds other
  data the pass carries along. Slot number `s` (an unsigned 16-bit number) is at `slots + 16 s`. Read and written.
- **Particle data**: 16 bytes a particle, the same numbering; the float at `+4` is the particle's inverse mass. Read only.
  It may be the same memory as the slots.
- **Links**: `n` links of 20 bytes, one constraint each (1.2). Read only; they may lie inside the slots' memory (then the
  pass's own stores change later links).
- **The clamp pair `c = (c0, c1)`**: given once for the whole pass (normally `(+0, +0)`).
- **The scale `s`**: one float, given once for the whole pass (positive, or a NaN).
- `fz`: flush to zero on or off for the whole pass.

### 1.2 One link (20 bytes)
| Offset | Type | Name | Meaning |
|---|---|---|---|
| +0x00 | u16 | `p1` | slot of the first particle |
| +0x02 | u16 | `p2` | slot of the second particle |
| +0x04 | float | `lo` | the shortest allowed distance |
| +0x08 | float | `hi` | the longest allowed distance |
| +0x0c | float | `k_lo` | the stiffness used when the particles are closer than `lo` |
| +0x10 | float | `k_hi` | the stiffness used when they are farther apart than `hi` |

`p1` and `p2` may be the same slot.

## 2. The pass

Links `i = 0 .. n-1` are done in order, each completely (its two stores) before the next is read.

### 2.1 Memory order (aliasing is real: `p1` may equal `p2`, and the links and the particle data may lie inside the
slots' memory)
- Link `i` reads its 20 bytes, its two slots `P1` = slot `p1` and `P2` = slot `p2`, and the two inverse masses `m1`
  (particle data of `p1`, at `+4`) and `m2` (of `p2`), all AFTER link `i-1`'s two stores and BEFORE any store of its own.
- It stores two 16-byte slots, in this order: `P1'` to slot `p1`, then `P2'` to slot `p2`.
- One value is read again between the stores: `P2r` = slot `p2` read after `P1'` is stored. Reading it from memory at
  that point is always right; a kernel that keeps values in registers must use `P2r = (p2 == p1) ? P1' : P2` (slots are
  16-byte aligned from one base: two slots are the same memory or do not overlap).
- Nothing of link `i + 1` (its bytes, slots, masses) may be read before link `i`'s last store: a store may have changed
  it.

### 2.2 One link

1. `d = P2 - P1` (vector).
2. `q = d * d` (vector; lane `w` is computed and not used).
3. `S = q.x + q.y`, `L = S + q.z` (pairs with two equal lanes: each lane is the same two additions).
4. `r = RS(L)` (0.4; a pair with two equal lanes).
5. `len = L * r` (pair).
6. `dn = d * r0` (vector: each lane of `d` times `r`'s lane 0; `r`'s two lanes are equal).
7. `ea = lo - len`, `eb = len - hi` (pairs: `lo` and `hi` are used as pairs `(lo, lo)`, `(hi, hi)`).
8. `a = MAX(c, ea)`, `b = MAX(c, eb)` (pairs, 0.5; lane j is `MAX(cj, eaj)`, `MAX(cj, ebj)`, the clamp the first
   operand).
9. `pa = a * k_lo`, `pb = b * k_hi` (pairs; `a`, `b` the first operands).
10. `df = pb - pa`, `corr = df * s` (pairs).
11. `dn2 = dn * P4(corr)` (vector; lanes x, z use `corr0`, lanes y, w use `corr1`; `dn`'s lane is the first operand).
12. `dv1 = dn2 * m1`, `P1' = P1 + dv1`; store `P1'` to slot `p1` (vectors).
13. `dv2 = dn2 * m2`; reload `P2r` (2.1); `P2' = P2r - dv2`; store `P2'` to slot `p2` (vectors).

Geometrically: `dn` is the unit direction from the first particle to the second, `pb` how far the link is too long
(times its stiffness), `pa` how far it is too short; each particle moves along the direction by the correction times its
inverse mass.

Every operation listed is done for every link, on every lane listed (pairs: two lanes, vectors: four), and its flags
count; the order in which they are done does not change the result (the float status is sticky), only which are done.

### 2.3 Lane `w`
Lane `w` of a vector never feeds lanes x, y, z or a pair: the squared length adds lanes x, y, z only, and every value
lane `w` is multiplied by (`r`, `corr1`, `m1`, `m2`) comes from elsewhere. In lane `w` the link computes, with the Arm
rules of section 0 and in this operand order:
- `d.w = P2.w - P1.w`;
- `q.w = d.w * d.w` (not used; its flags count);
- `dn.w = d.w * r0`, `dn2.w = dn.w * corr1`;
- `dv1.w = dn2.w * m1`, `P1'.w = P1.w + dv1.w`;
- `dv2.w = dn2.w * m2`, `P2'.w = P2r.w - dv2.w` (`P2r.w` is `P1'.w` when `p2 == p1`).
Lane `w` of the stored slots (and of the handed-back `p2_new` and `p2_step`, section 5) is these values exactly, NaN
payloads included (0.2), and their flags count.

## 3. The float status the pass raises
Report every flag the operations of section 2 raise (Arm bit layout: IOC 0x01, DZC 0x02, OFC 0x04, UFC 0x08, IXC 0x10,
IDC 0x80), IXC aside. For links in the class of section 4 that is exactly:
- IDC, once any link was done, when `fz` = 1 (the tiny test of 0.4 flushes `TINY`);
- whatever lane `w` raises (2.3; any of IOC, OFC, UFC, IDC).
Nothing else: lanes x, y, z and the pairs of a link in the class raise no other flag.

## 4. What the kernel does itself, and what it must decline

The kernel is called with `n >= 1`, flush to zero on or off, the float modes otherwise at their defaults, and IXC set
(the hook's gate). It does links from the first while each is in the class below, and stops before the first link that
is not, having written nothing of it (`why` says why; INTERFACE.md). A link in the class must be done.

**R** is the set of floats that are zero (either sign) or have a magnitude in [2^-63, 2^63): bits without the sign
`0x00000000` or `0x20000000` to `0x5effffff`. (`kc::in_r`.)

### 4.1 Once for the pass (`why` = ENTRY, done = 0)
Decline the whole pass when a lane of `c` or the scale `s` is not in R.

### 4.2 The link class (`why` = CLASS)
A link is in the class when every one of these is in R (lanes x, y, z of vectors; both lanes of pairs; lane `w`
never counts):
- the inputs: `P1`, `P2` (lanes x-z), `lo`, `hi`, `k_lo`, `k_hi`, `m1`, `m2`;
- `d`, `q`, `S`, `L`, `r`, `len`, `dn`;
- `ea`, `eb`, `pa`, `pb`, `df`, `corr`;
- `dn2`, `dv1`, `dv2`;
- the reloaded `P2r` (lanes x-z; it differs from `P2` only when `p2 == p1`).
Not checked: `a`, `b` (each is one of its two operands), the estimate `e`, `e2` and the step `g` of 0.4, and the stored
sums `P1'`, `P2'` (each the sum of two R values: zero or a normal number, exact as IEEE rounds it).

### 4.3 The step range (`why` = STEP)
Where the fused step of 0.4 is computed without a hardware fused multiply-add, it is exact only when `L * e2` lies in
[1/8, 8] (`kc::rsq_step` sets `bad` otherwise). The kernel stops before a link whose step (when the tiny test passes)
has `L * e2` outside [1/8, 8]; this is checked in every build (it never happens for values in R: `L * e2` is about 1).

### 4.4 Lane `w` never stops a link
Whatever lane `w` holds (NaNs of any payload, infinities, subnormals, huge values), a link whose lanes x-z are in the
class is done, lane `w` computed by 2.3 with the Arm rules and its flags reported. Two exact ways, both described in
`cloth_wlane.hpp`: every lane `w` operation through `cw::arm_add` / `arm_sub` / `arm_mul`; or the host vector's own lane
`w` with every NaN made `0x7fc00000` (`cw::w_canon`) under the conditions given there (the pass's status already holds
IOC, OFC and UFC; every NaN read into lane `w` is `0x7fc00000`; under flush to zero no lane `w` value of the link is a
nonzero of magnitude at most 2^-126).

### 4.5 Why the class is exact
For values in R: a product of two of them is zero or in [2^-126, 2^126), a normal number; a sum or difference is zero or
a multiple of 2^-86 below 2^64, a normal number. So no result is tiny, infinite or NaN, IEEE round to nearest on the host
gives the Arm bits, flush to zero changes nothing, no flag but IXC is raised (plus those of section 3), and the operand
order of an operation does not matter in lanes x-z. Exact zeros follow IEEE on both (an exact cancellation is +0). For
`L` in R the tiny test passes exactly when `L` is not zero. The estimate is a table lookup; the step is one rounding of
an exact value (with a hardware fused multiply-add, `fmaf(-L, e2, 3) * 0.5f`; without one, in binary64 under the range of
4.3). The maximum of two non-NaN values is the larger, or the AND of the bits of two equal ones (0.5).

### 4.6 After a stop
The kernel has stored links `0 .. done-1` (each exactly as the pass would) and nothing of link `done`; the game's code
then runs links `done .. n-1` itself, from link `done`, with the same clamp pair and scale. Nothing but memory is carried
from one link to the next, so nothing else is handed back at a stop. The status reported covers links `0 .. done-1` only.

## 5. Values handed back at the loop's end

The rest of the game's routine runs after the loop, and the values the loop's last link computed are still there for it.
So when the kernel has done EVERY link (`done == n`; this is the loop's only way out: it ends after link `n - 1`), it
hands back these values of the last link, link `n - 1`, each with exactly the bits section 2 gives it (NaN choice, flush
and all; lane `w` by 2.3). Nothing extra is computed and no flag is added.

| Field (`ClothBendLinkEnd`) | Value of the last link |
|---|---|
| `p1`, `p2` | its two slot numbers, as read |
| `p2_new[4]` | `P2'`, the second particle's new position as stored (all four lanes; it is what slot `p2` holds after the pass) |
| `p2_step[4]` | `dv2 = dn2 * m2` (all four lanes) |
| `m2` | the second particle's inverse mass, as read |
| `r` | `r0`, the reciprocal length (+0 when the tiny test failed) |
| `push[2]` | `pa = a * k_lo`, both lanes (`a = MAX(c, ea)`, 2.2 steps 8-9) |
| `k_lo`, `k_hi` | its two stiffnesses, as read |

When `done < n` nothing is handed back (the fields are not looked at). The other values the rest of the routine finds
at the loop's end (where the links end, that no link is left, the tiny constant, the clamp pair) follow from the inputs
alone and are not the kernel's business.
