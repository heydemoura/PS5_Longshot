# The capsule collider's particle pass: specification

What the kernel computes, bit for bit. `INTERFACE.md` says how it is called; `cloth_capsule.h` is the C interface.

## 1. What the pass does

A cloth's particles are pushed out of a capsule-shaped collider and their motion along the surface is damped by
friction. The capsule is the segment from A to B swept by a radius. For each particle in a list: find the closest point
of the segment, test whether the particle's sphere (its own radius) reaches into the capsule, and if it does, move the
particle onto the capsule's surface (a new position) and change its previous position so that its motion relative to the
collider's moving surface loses the tangential part scaled by friction (a new previous position).

The list is done in two ways, which differ in a few details (sections 4 and 5):
- **batches**: 16 particles at a time, as four groups of four ("place" 0..3 in the group). Each particle's radius and
  friction come from the batch's own arrays.
- **singles**: the particles left over after the batches, one at a time. Each particle's radius and friction come from
  the particle's own data.

Everything is single precision (binary32). Vectors have four lanes x, y, z, w. Lane w of a position is carried through
the arithmetic and stored like the others (it is not a coordinate, but its bits matter).

## 2. Data

- Positions and previous positions: two arrays of 16-byte slots (x, y, z, w floats), slot p for particle p. The pass
  reads and writes both. They never overlap each other or any input (the caller makes sure).
- Batches: per batch 16 particle indices (u16), 16 radii (float), 16 friction factors (float), in three arrays; batch i
  is entry 16 i .. 16 i + 15 of each. Particle j of a batch (j = 0..15) is in group j div 4 at place j mod 4.
- Singles: the particle indices (u16); per particle p its own data at 16 bytes a particle: radius at +8, friction
  factor at +12.
- Constants of the call (`ClothCapsule`): A, B (four lanes each), the radius `rad`, `k` = 1 / |B - A|^2 (given; use it
  as given), `eps[place]` and `one[place]` (batches only), the time step `dt`, the collider body's centre `c` (x, y, z),
  linear velocity `v` (all four lanes) and angular velocity `w` (x, y, z), and whether the float mode flushes subnormals
  to zero (FZ).
- `AB = B - A`, all four lanes.

Notation: `a + b`, `a - b`, `a * b` are single-precision operations rounded to nearest (section 6 says when that is the
Arm's result). Parentheses give the order of evaluation; it matters. A "lane-wise" vector operation does the same on
each lane named.

## 3. Per particle: the contact test (common part)

For a particle with position P (four lanes) and radius R:

1. Segment parameter:
   - `e = P - A` (lanes x, y, z), `dot = (AB.x * e.x + AB.y * e.y) + AB.z * e.z` (the x and y products added first,
     then the z product).
   - `t` = `dot * k`, then clamped to the segment. The clamp differs between batches and singles (sections 4 and 5).
2. Closest point and offset, all four lanes: `C = A + AB * t` (lane-wise, `t` the same in every lane; lane w too:
   `C.w = A.w + AB.w * t`), `d = P - C`.
3. Squared distance: `L = (d.x * d.x + d.y * d.y) + d.z * d.z` (singles), and for batches `L = eps[place] + that`
   (`eps` added last).
4. Inverse distance `r`:
   - if `L <= c0` then `r = +0`, where `c0` is the float with bits `0x007fffff` (a subnormal) when FZ is off and `+0`
     when FZ is on (the comparison flushes it). A NaN `L` is not `<= c0`, so it takes the next case.
   - else `r = r2` from the Arm's reciprocal square root estimate and two Newton steps, exactly:
     `r0 = rsqrt_est(L)`, `r1 = r0 * rsqrt_step(L, r0 * r0)`, `r2 = r1 * rsqrt_step(L, r1 * r1)` (the squares `r0 * r0`
     and `r1 * r1` are ordinary single-precision products, rounded; only the step's own product is not), where
     - `rsqrt_est(x)` is the Arm's single-precision reciprocal square root estimate (`kc::frsqrte_bits` in
       `cloth_common.hpp` for a positive normal x; `rsqrt_est(+0) = +inf`, `rsqrt_est(+inf) = +0`, `rsqrt_est(NaN) = NaN`);
     - `rsqrt_step(a, b) = (3 - a * b) / 2` with the product not rounded (a fused operation: one rounding of the whole), and
       `rsqrt_step(a, b) = 1.5` when one of a, b is an infinity and the other a zero; a NaN operand gives a NaN.
     (Batches compute r2 for every particle and then replace it by +0 where `L <= c0`; singles skip the steps. The
     result is the same.)
5. Contact: `s_c = ((L * r) - rad) - R`; the particle is in contact when `s_c < 0` (an ordered comparison: a NaN is
   not in contact).

## 4. Batches (16 particles at a time)

Per batch, in batch order; each batch whole before the next (a later batch may name a particle an earlier one wrote:
it reads the new values).

- Inputs per particle j: P = positions[index j], R = the batch's radius j; and only for a particle in contact,
  Q = previous positions[index j] and f = the batch's friction j.
- The batch's 16 indices must be all different. A batch with a repeated index is left (section 7: `CLOTH_CAP_INDEX`).
- Clamp: `t = amin(amax(dot * k, +0), one[place])` with the Arm's rules:
  - `amax(x, +0)` (the Arm's maximum, FPMax): x when x > 0; +0 when x is -0, +0 or negative; x when x is a NaN (the
    NaN passes on).
  - `amin(x, y)` (the Arm's minimum, FPMin): a NaN operand gives the default NaN; else the smaller; of two equal values
    the bitwise OR of their bits (so amin(-0, +0) = -0).
- `eps[place]` in L and `one[place]` in the clamp are taken by the particle's place in its group (0..3).
- All 16 contact tests come first (they read only the batch's P and R and the constants), then the responses
  (section 3's test decides which particles respond), then the stores.
- Response of each particle in contact: section 5.1 gives `P'` and `TF` (with the batch's R and f).
- Stores, per group of four, among the particles of the group in contact, taken in place order 0..3:
  - every one in contact stores its `P'` to its position slot;
  - previous positions: the FIRST one in contact (place `a`) stores `Q(a) + TF(a)`, except that when there is a SECOND
    one in contact (place `b`), the first's slot gets `TF(b) + (Q(a) + TF(a))` instead, and `b`'s own previous-position
    slot is NOT written; the third and fourth in contact each store their own `Q + TF`.
  - (Yes: the second particle's friction term goes into the first particle's previous position. That is the game's
    behaviour and it is kept.)
  - A particle not in contact writes nothing. A batch with no contact writes nothing.
- `contacts` (when asked for): bit j set when particle j of the batch was in contact.

## 5. Singles (the particles left over, one at a time)

Per particle in list order; each particle's stores are done before the next particle is read (an index may repeat:
the later one reads what the earlier one stored).

- Inputs, with p the particle's index read from the list: P = positions[p], R = the float at data + 16 p + 8; for a
  particle in contact also Q = previous positions[p], f = the float at data + 16 p + 12.
- Clamp: `t0 = dot * k`; `t = +0` when `t0 < 0`; `t = 1.0` when `t0 > 1`; else `t = t0` (so -0 stays -0 and a NaN
  stays).
- No eps in L (section 3).
- In contact: section 5.1 with this particle's R and f; store `P'` to its position slot, then `Q + TF` to its previous
  position slot. Not in contact: nothing is written.
- `contacts`: how many of the particles done were in contact.

### 5.1 The response of a particle in contact (batches and singles)

With P, Q, C, d, r, R, f of the particle:

1. Normal: `n = d * r` (all four lanes).
2. Surface point: `S = C + n * rad` (lanes x, y, z).
3. Push out: `m = n * (P - S)` (lanes x, y, z), `dS = (m.x + m.y) + m.z`, `s = R - dS`, `P' = P + n * s` (all four
   lanes, `s` the same in each; so `P'.w = P.w + n.w * s`).
4. The surface's velocity at S: with `u = S - c` (lanes x, y, z), the cross product `X = w.y * u.z - u.y * w.z`,
   `Y = w.z * u.x - u.z * w.x`, `Z = w.x * u.y - u.x * w.y` (each: the two products, then their difference), and
   `V = v + (X, Y, Z, +0)` lane-wise (so `V.w = v.w + (+0)`: a -0 becomes +0).
5. Relative motion: `rel = (P' - Q) - V * dt` (all four lanes).
6. Tangential part: `m2 = n * rel` (lanes x, y, z), `dn = (m2.x + m2.y) + m2.z`, `T = rel - n * dn` (all four lanes).
7. `TF = T * f` (all four lanes). The new previous position is built from `Q` and `TF` as section 4 or 5 says.

## 6. Exactness: when host arithmetic gives the Arm's bits

The game runs with round to nearest, default-NaN mode off, no traps, flush-to-zero on or off (`fz`), and the status
register already holding every cumulative flag (so no flag can change: the kernel reports none). Under these rules:

- Every NaN the Arm produces or passes on here is the default NaN `0x7fc00000` as long as every float the pass reads is
  a number or exactly `0x7fc00000`. A host may produce other NaN bits (the host's invalid operation gives `0xffc00000`):
  every NaN stored must be written as `0x7fc00000`. A float read that is a NaN with other bits makes its batch or
  particle one to leave (section 7). The floats read: A, B, rad, k, dt, c (x, y, z), v (four lanes), w (x, y, z), and
  for batches eps, one, each P (four lanes) and R of the batch, and Q (four lanes) and f of each particle in contact;
  for singles P (four lanes) and R, and Q (four lanes) and f when in contact.
- With FZ off, IEEE round to nearest on numbers is the Arm's result (subnormals included).
- With FZ on, it is the Arm's result as long as no value read or computed is a nonzero of magnitude at most 2^-126
  (bits `0x00000001..0x00800000` with either sign): then nothing is flushed and no result was tiny before rounding. A
  batch or particle where such a value occurs (in any value its stores or its contact test depend on) is left.
- `rsqrt_est` is exact as given for a positive normal input, +0, +inf or a NaN; for a negative or subnormal input the
  batch or particle is left.
- `rsqrt_step(a, b)`: the product `a * b` of two singles is exact in double precision; for `a * b` in [1/8, 8],
  `(3 - a * b)` is exact in double precision too, and rounding it once to single and halving gives the Arm's value (or
  `fmaf(-a, b, 3) * 0.5` with a hardware FMA). The other cases handled: a NaN operand (NaN), an infinity and a zero
  (1.5). Any other product leaves the batch or particle.
- `amin` and `amax` as section 4 (they never meet a tiny value under FZ, by the rule above).
- Signed zeros follow IEEE (x - x = +0, (-0) + (-0) = -0, -0 + +0 = +0, ...). The game's cases where it matters: the
  clamps (sections 4 and 5), `V.w`, and particles exactly on the segment.

These conditions are sufficient, not required: a kernel may handle more (exactly), but every batch or particle it does
must give the game's bits.

## 7. Leaving work to the game's own code

The kernel stops at the first batch (or single) it cannot do exactly and reports how far it got: `done` whole batches
(particles) from the first, and why it stopped (`CLOTH_CAP_INDEX`: a batch with a repeated index; `CLOTH_CAP_VALUES`:
a value outside section 6). Nothing of the batch or particle it stopped at, or of any after it, may be written; the
game's own code then does them, starting from what the kernel left in memory. Batches and particles before it are
stored as sections 4 and 5 say.

## 8. Values handed back

For every way out:
- `done`: the number of batches (singles: particles) done whole, from the first; `n` when all were done.
- `why`: `CLOTH_CAP_ALL` when `done == n`, else the reason the next one was left.
- batches, `contacts` (if asked): for each batch done, which of its 16 particles were in contact.
- singles, `contacts`: how many of the particles done were in contact.

Nothing else is handed back: what the rest of the game's routine needs after the pass (where the list continues, how
many particles are left) follows from `done`, and the caller sets it.
