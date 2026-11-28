# The cloth link (stretch) pass: behavioural spec

One pass of the cloth solver's link constraints (Havok cloth's standard link constraint set, as the game runs it), to be
answered on the host with the game's exact bits and float status. The pass is a loop inside a larger function of the
game; the kernel does the loop. This file says what the loop computes, precisely enough for identical bits and flags,
which links a kernel does itself (section 4) and what it hands back for the rest of the function (section 5).
`INTERFACE.md` gives the C entry point; `HARNESS.md` how a candidate is checked against the game's own loop.

## 0. Conventions

**Numbers.** Every float is IEEE binary32. Every operation below is ONE Arm single-precision operation (add, subtract,
multiply) with its own rounding to nearest even; there is no fused multiply-add anywhere except inside the reciprocal
square root step (0.4), no division, no reassociation, and grouping is exactly as written: `(a + b) + c` is two
additions in that order. Operand order is as written (`a * b`: `a` is the first operand); it matters only for which NaN
a result carries (0.2).

**Vectors.** A *vector* has four lanes `(x, y, z, w)` = lanes 0, 1, 2, 3, the four floats of a 16-byte slot in memory
order. Every vector operation is done on all four lanes, lane `w` included, because its result is stored and its status
flags count. `v * f` with a vector `v` and a single float `f` multiplies each lane of `v` by `f` (`v.x * f`, ...;
`v`'s lane is the first operand).

**Arm rules for one operation** (the call runs with round to nearest, default-NaN mode off, no traps; flush to zero is
on or off as the call says, `fz`). They matter for lane `w` (which can hold anything) and for any value outside the
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

**0.4 The reciprocal square root of a squared length** `RS(L)` (`L` a single float):
- The *tiny test*: a quiet compare of `L` with `TINY`, the largest subnormal (bits `0x007fffff`). It passes when
  `L > TINY` or when `L` is a NaN (unordered). A quiet compare raises IOC only for a signalling NaN; under flush to zero
  both operands are flushed first, so `TINY` reads as 0 and **IDC is raised by every tiny test when `fz` = 1**.
- If the test fails: `RS(L) = +0`.
- If it passes: `e = estimate(L)`, the Arm single-precision reciprocal square root estimate (an 8-bit table;
  `kc::frsqrte_bits` in `cloth_common.hpp` gives it for a positive normal `L`); `e2 = e * e`;
  `g = step(L, e2)`, the Arm fused reciprocal square root step: `(3 - L * e2) / 2` computed exactly and rounded ONCE;
  `RS(L) = e * g` (`e` the first operand). (`kc::rsq_step` in `cloth_common.hpp` is this whole function for `L` zero or
  a positive normal, with the range test of 4.3.)

## 1. The data

### 1.1 Inputs (INTERFACE.md says how each reaches the kernel)
- **Particle slots**: 16-byte slots of four floats `(x, y, z, w)`, a particle position in `x, y, z`; `w` holds other
  data the pass carries along. Slot number `p` (an unsigned 16-bit number) is at `slots + 16 p`. Read and written.
- **Particle data**: 16 bytes a particle, the same numbering; the float at `+4` is the particle's inverse mass. Read
  only (but it may lie inside the slots' memory, so a slot store can change it).
- **Link records**: `n` records of 12 bytes, one distance constraint each (1.2). Read only (they too may lie inside the
  slots' memory).
- **The scale `s`**: one float, the same for every link of the call (the solve's time-step factor).
- `fz`: flush to zero on or off for the whole call.

### 1.2 One link record (12 bytes)
| Offset | Type | Name | Meaning |
|---|---|---|---|
| +0x00 | u16 | `a` | the first particle's slot number |
| +0x02 | u16 | `b` | the second particle's slot number |
| +0x04 | float | `rest` | the rest length |
| +0x08 | float | `k` | the stiffness (Havok's per-link stiffness, already holding the particles' share of the mass) |

Geometrically: the link moves its two particles along the line between them so that their distance comes closer to
`rest`, each in proportion to its inverse mass.

## 2. The pass

The pass is called with `n >= 1` links. Links `i = 0 .. n-1` are done in order, each completely (its two stores) before
the next is read.

### 2.1 Memory order (aliasing is real: `a` may equal `b`, and the records and the particle data may lie inside the
slots' memory)
- Link `i` reads its 12 bytes, slot `b` (B), slot `a` (A), and the two inverse masses `ma` (particle data of `a`, at
  `+4`) and `mb` (of `b`), all AFTER link `i-1`'s two stores and BEFORE any store of its own.
- It stores A' to slot `a`, THEN reads slot `b` again (`Br`), then stores B' to slot `b`. Reading `Br` from memory at
  that point is always right. Slots are 16-byte slots from one base, so two slots are the same memory or do not
  overlap: a kernel that keeps the values in registers instead uses `Br = (b == a) ? A' : B`.
- Nothing of link `i + 1` (its bytes, its slots, its masses) may be read before link `i`'s last store: a store may have
  changed it.

### 2.2 One link
With A, B the two slots as read, `ma`, `mb`, `rest`, `k` as read, and the call's scale `s`:
1. `D = B - A` (vector, all four lanes; B's lane is the first operand).
2. `Q = D * D` (vector, all four lanes; lane `w` is computed and not used: its flags count).
3. `L = (Q.x + Q.y) + Q.z` (single floats: two additions in that order; `Q.x` is the first operand of the first).
   `r = RS(L)` (0.4).
4. `len = L * r`.
5. `dif = len - rest`.
6. `N = D * r` (vector).
7. `c1 = dif * k`; `c = c1 * s`.
8. `T = N * c` (vector).
9. `TA = T * ma`, `TB = T * mb` (vectors).
10. `A' = A + TA` (vector); store A' to slot `a`.
11. Read slot `b` again: `Br` (2.1). `B' = Br - TB` (vector); store B' to slot `b`.

Every operation listed is done for every link, on every lane listed, and its flags count; the order in which
independent operations are done does not change the result (the float status is sticky), only which are done.
(The pass is a position-based distance constraint: `len` is the link's current length `|D|`, and both particles move
along `D / |D|` by `(len - rest) * k * s`, scaled by their inverse masses, A towards B and B towards A.)

### 2.3 Lane `w`
Lane `w` of a vector never feeds lanes x, y, z or a single float: `L` adds lanes x, y, z only, and every single float a
vector is multiplied by (`r`, `c`, `ma`, `mb`) comes from elsewhere. In lane `w` a link computes, with the Arm rules of
section 0 and in this operand order:
- `D.w = B.w - A.w`; `Q.w = D.w * D.w` (not used; its flags count);
- `N.w = D.w * r`; `T.w = N.w * c`; `TA.w = T.w * ma`; `TB.w = T.w * mb`;
- `A'.w = A.w + TA.w`; `Br.w` = (`b == a`) ? `A'.w` : `B.w`; `B'.w = Br.w - TB.w`.
Lane `w` of the stored slots is these values exactly, NaN payloads included (0.2), and their flags count.

## 3. The float status the pass raises
Report every flag the operations of section 2 raise (Arm bit layout: IOC 0x01, DZC 0x02, OFC 0x04, UFC 0x08, IXC 0x10,
IDC 0x80), IXC aside. For links in the class of section 4 that is exactly:
- IDC, once any link was done, when `fz` = 1 (the tiny test of 0.4 flushes `TINY`, every link, whatever `L` is);
- whatever lane `w` raises (2.3; any of IOC, OFC, UFC, IDC).
Nothing else: lanes x, y, z and the single floats of a link in the class raise no other flag.

## 4. What the kernel does itself, and what it must decline

The kernel is called with `n >= 1`, flush to zero on or off, the float modes otherwise at their defaults, and IXC set
(the hook's gate). It does links from the first while each is in the class below, and stops before the first link that
is not, having written nothing of it (`why` says why; INTERFACE.md). A link in the class must be done. A link outside
the class may be done too, but only with the game's exact bits and flags (every rule of section 0); when in doubt, stop.

Two sets of floats (by the bits without the sign):
- **G**: zero, or a magnitude in [2^-125, FLT_MAX]: `0x00000000`, or `0x01000000` to `0x7f7fffff`.
- **H**: zero, or a magnitude in [2^-74, FLT_MAX]: `0x00000000`, or `0x1a800000` to `0x7f7fffff` (H is part of G).

### 4.1 Once for the call (`why` = ENTRY, done = 0)
Decline the whole call when the scale `s` is not in G.

### 4.2 The link class (`why` = CLASS)
A link is in the class when all of these hold (lanes x, y, z of vectors; lane `w` never counts):
- in G: A, B; `Q`; `Q.x + Q.y`; `L`; when the tiny test passes, `e2`, `g` and `r` of 0.4; `len`; `rest`; `k`; `dif`;
  `c1`; `TA`, `TB`; A', B';
- in H: `D`; `r`; `ma`, `mb`; `c`; `N`, `T`;
- no product of two nonzero factors is zero: `e2 = e * e`, `r = e * g`, `len = L * r`, `c1 = dif * k`, `c = c1 * s`
  (such a product underflowed: UFC on the Arm).
Not checked: the estimate `e` (a table value), and `Br` (it is A' or B, both checked).

### 4.3 The step range (`why` = STEP)
Where the fused step of 0.4 is computed without a hardware fused multiply-add, it is exact only when `L * e2` lies in
[1/8, 8] (`kc::rsq_step` sets `bad` otherwise). The kernel stops before a link whose step (when the tiny test passes)
has `L * e2` outside [1/8, 8]; this is checked in every build (it never happens for `L` in G: `L * e2` is about 1).

### 4.4 Lane `w` never stops a link
Whatever lane `w` holds (NaNs of any payload, infinities, subnormals, huge values), a link whose lanes x-z are in the
class is done, lane `w` computed by 2.3 with the Arm rules and its flags reported. Two exact ways, both described in
`cloth_wlane.hpp`: every lane `w` operation through `cw::arm_add` / `arm_sub` / `arm_mul`; or the host vector's own lane
`w` with every NaN made `0x7fc00000` (`cw::w_canon`) under the conditions given there (the call's status already holds
IOC, OFC and UFC; `A.w` and `B.w` are not NaNs other than `0x7fc00000`; under flush to zero none of the link's lane `w`
values `A.w`, `B.w`, `D.w`, `Q.w`, `N.w`, `T.w`, `TA.w`, `TB.w`, `A'.w`, `B'.w` is a nonzero of magnitude at most
2^-126).

### 4.5 Why the class is exact
Every operand and every result of a link in the class is zero or a normal number of magnitude at least 2^-125 and at
most FLT_MAX, so: no operand is subnormal, infinite or NaN (flush to zero has nothing to flush, no IOC); a host result of
magnitude at least 2^-125 means the exact value is above 2^-126 (rounding moves it by less than 2^-149), so the Arm
result is not tiny either (no UFC, no flush); a finite result means no overflow on either. Zeros: a sum or difference of
two such values is zero only by exact cancellation (+0 on both), a product with a zero factor is exact (the same signed
zero on both), and a product of two nonzero factors is never zero (the H bounds keep the vector products at 2^-148 or
more; the single products are checked directly). So IEEE round to nearest on the host gives the Arm bits and raises
nothing but IXC; operand order does not matter in lanes x-z. The estimate is a table lookup; the step is one rounding of
an exact value (with a hardware fused multiply-add, `fmaf(-L, e2, 3) * 0.5f`; without one, in binary64 under the range
of 4.3). For `L` in G the tiny test passes exactly when `L` is nonzero, flush to zero or not.

### 4.6 After a stop
The kernel has stored links `0 .. done-1` (each exactly as the pass would) and nothing of link `done`; the game's loop
then runs links `done .. n-1` itself, from the same state (nothing but memory is carried from one link to the next). The
status reported covers links `0 .. done-1` only.

## 5. Values handed back at the loop's end

The rest of the game's function runs after the loop. When the kernel did EVERY link (`done == n`), it hands back what
the loop leaves behind about its last link (link `n - 1`), so that the function continues exactly as after the game's
loop. All are bits exactly as below, NaN payloads and signed zeros included:

| Value | What it is, for the last link |
|---|---|
| `a`, `b` | its two slot numbers, as read from its record (0 .. 65535) |
| `inv_len` | its `r` (2.2 step 3): `RS(L)`, `+0` when its tiny test failed |
| `inv_mass_a`, `inv_mass_b` | its `ma`, `mb`, the bits as read |
| `stiffness` | its `k`, the bits as read |
| `pos_a` | A: slot `a` as it read it at its start (16 bytes, lanes x y z w) |
| `pos_b_again` | `Br`: slot `b` as it read it again after storing A' (so A' when `a == b`, else B) |
| `pos_b_new` | B': what it stored to slot `b` (16 bytes) |

There is one way out of the loop: after its last link. When the kernel stops part way (`done < n`) these values are not
read: the game's loop does links `done .. n-1` and leaves them itself. What else the loop leaves (the count of links left,
which is zero; where the records were read; a constant of the tiny test) is known to the hook from the call, not from
the kernel.
