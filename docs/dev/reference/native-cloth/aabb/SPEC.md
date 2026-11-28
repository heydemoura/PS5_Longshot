# Particle bounding boxes: behavioural spec

Once per cloth step the game takes three axis-aligned boxes over a cloth instance's particles: the box of their
positions, and two boxes of where the moving particles are predicted to sweep (half a step back and half a step ahead).
The kernel does the pass over the particles; the code around it (Havok's `hclSimClothInstance::updateAabbs`: a timer
scope before it, the margins added and the boxes stored after it) stays translated game code. This file says what the
pass computes precisely enough for identical bits and float status flags, and what it hands back to the code after
it. `INTERFACE.md` gives the C interface, `HARNESS.md` how a candidate is checked against the game's own code.

## 0. Conventions

**Floats.** Every value is an IEEE binary32 float, handled as its bit pattern. Every operation below is one Arm
single-precision operation with its own rounding (round to nearest, ties to even): no fused multiply-add, no
reassociation, no wider intermediate that is not rounded back after each operation. **Operand order matters**, also for
the commutative operations and for the minimum and maximum: it decides which NaN an operation returns (section 2).
`a - b`, `a + b`, `a * b`, `min(a, b)`, `max(a, b)` have first operand `a`, second `b`.

**Points.** A *point* is four floats, lanes 0..3, 16 bytes, lane 0 at the lowest address. Point operations work lane by
lane on all four lanes: `V - W` is `(V[0] - W[0], V[1] - W[1], V[2] - W[2], V[3] - W[3])`, `V * s` for a scalar `s` is
`(V[0] * s, ..., V[3] * s)` with the point's lane first and `s` second in every lane, `min(V, W)` is the lane-wise
minimum with `V`'s lane first. All four lanes are always computed, lane 3 included, whatever it holds (in the game's data
lane 3 is mostly the default NaN `0x7fc00000`, else ordinary or huge values): its results are handed back and the
flags of its operations count. No lane ever feeds another.

## 1. Inputs

- `n >= 1` particles, numbered `i = 0 .. n - 1`, done in that order.
- Particle `i`'s position `P_i`: a point at `pos + 16 i`.
- Its previous position `Q_i`: a point at `prev + 16 i`.
- Its collision mask `m_i`: an unsigned 32-bit little-endian word at `mask + 4 i`.
- Its inverse mass `w_i`: one float at `inv_mass + 16 i` (the other 12 bytes of each 16 are not used).
- `S`: the boxes' start value, a point (four bit patterns).
- `fz`: whether flush-to-zero is on.

Nothing is written to memory. What is read: every `P_i` and `m_i`; `w_i` only for a particle with `m_i = 0`; `Q_i` only
for a particle that is tracked (section 3).

## 2. The arithmetic of one operation (the Arm's rules, under the float mode the hook guarantees)

The hook calls the kernel only with round to nearest, the default-NaN mode off, no alternative half precision, no
exception traps; flush-to-zero (`fz`) is on or off. The kit's helper `cloth_wlane.hpp` implements each rule below for
one lane on bit patterns, first operand first, ORing the flags it raises into its last argument; they are the reference
for every case, the rare ones included, and they are slow.

**Subtraction, multiplication, addition** (`cw::arm_sub`, `cw::arm_mul`, `cw::arm_add`), of `a` (first) and `b`:
1. *Flush of inputs (only with `fz` on):* an operand that is a nonzero subnormal (exponent field 0, fraction not 0) is
   replaced by a zero of the same sign, and the input-denormal flag IDC is raised. Both operands, before NaNs are looked
   at.
2. *NaNs:* if `a` is a signalling NaN (exponent all ones, fraction nonzero, fraction bit 22 clear), the result is `a`
   with bit 22 set, and IOC is raised. Else if `b` is a signalling NaN: `b` with bit 22 set, IOC. Else if `a` is a quiet
   NaN: `a` unchanged (payload and sign kept). Else if `b` is a quiet NaN: `b`. No other flag. (For a subtraction, `b`
   is returned as it is, its sign not flipped.)
3. *Invalid operations:* `inf - inf` (infinities of equal signs subtracted), `inf + (-inf)`, and `0 * inf` in either
   order give the default NaN, bit pattern `0x7fc00000`, and raise IOC.
4. *Infinities and zeros:* as IEEE. `a - b` is `a + (-b)`. A sum of two zeros is `+0` unless both are `-0`. `x + 0` with
   `x` not zero is `x` exactly. An exact cancellation is `+0`. A product's sign is the exclusive or of the operands'
   signs; a product with a zero operand (and no infinity) is a zero of that sign. These raise nothing.
5. *Rounding:* otherwise the exact result is rounded once to binary32, to nearest even. If its magnitude **before
   rounding** is below `2^-126` (*tiny*): with `fz` on the result is a zero of the exact result's sign and UFC is raised
   (no rounding); with `fz` off it is the correctly rounded subnormal (or zero), UFC raised when that rounding was
   inexact. If the rounded magnitude exceeds the largest finite float, the result is an infinity of its sign and OFC is
   raised. IXC (inexact) is always already set when the kernel is called; it need not be tracked.

**Minimum and maximum** (`cw::arm_fmin`, `cw::arm_fmax`), of `a` (first) and `b`:
1. *Flush of inputs (only with `fz` on):* as above, both operands, IDC.
2. *NaNs:* exactly as step 2 above (a signalling NaN, first operand first, quieted with IOC; else a quiet NaN, first
   operand first, as it is). So the minimum of a NaN and a number is the NaN, never the number.
3. Otherwise the smaller (for the maximum, the larger) value, exactly the operand's bits after step 1. Of two equal
   values: the minimum is the bitwise OR of the two, the maximum the bitwise AND (the only equal values with different
   bits are `+0` and `-0`: `min(+0, -0) = min(-0, +0) = -0`, `max` of them `+0`). Nothing else is raised.

**Comparison with zero** (`cw::arm_fcmp_zero_z`), of `w`: with `fz` on a nonzero subnormal `w` is first replaced by a
zero of its sign, raising IDC. A signalling NaN raises IOC and compares *not equal*; a quiet NaN raises nothing and
compares not equal; otherwise `w` compares equal exactly when it is `+0` or `-0`. Nothing else is raised.

## 3. The pass

**Start.** Three boxes, each a pair of points (a minimum corner and a maximum corner):
- the positions' box `pos_min`, `pos_max`;
- the moving box `moving_min`, `moving_max`;
- the marked box `marked_min`, `marked_max`.

Every minimum corner starts as `S`, every maximum corner as `-S`: `S` with the sign bit (bit 31) of each lane flipped,
whatever the lane holds (a NaN too; no flag, no flush).

**Particle `i`, for `i = 0, 1, ..., n - 1` in order:**

1. *Skipped or tracked.* If `m_i = 0`, `w_i` is compared with zero (section 2: its flags count); particle `i` is
   **skipped** when `m_i = 0` and `w_i` compares equal to zero. Every other particle is **tracked**. (With `m_i != 0`
   the inverse mass is not read and nothing is compared.)
2. *Tracked only:* with `P = P_i`, `Q = Q_i`:
   - `D = P - Q` (the step's motion; `P` first)
   - `H = D * 0.5` (`D` first, the constant `0.5` = `0x3f000000` second)
   - `T = D * 1.5` (`D` first, `1.5` = `0x3fc00000` second)
   - `mid = Q + H` (`Q` first): the point half a step back from `P`, between `Q` and `P`
   - `ahead = Q + T` (`Q` first): the point half a step ahead of `P`
   - the moving box takes both points, `mid` first, the corner always the first operand:
     `moving_min = min(min(moving_min, mid), ahead)`, `moving_max = max(max(moving_max, mid), ahead)`
   - if bit 31 of `m_i` is set, the marked box takes both points the same way:
     `marked_min = min(min(marked_min, mid), ahead)`, `marked_max = max(max(marked_max, mid), ahead)`
3. *Every particle:* the positions' box takes `P_i`, the corner first:
   `pos_min = min(pos_min, P_i)`, `pos_max = max(pos_max, P_i)`.

All of these are point operations on all four lanes. The boxes are separate accumulators: only the order of the
operations on one corner matters for its bits (the order above), and the flags are a union.

Count per tracked particle: 1 subtraction, 2 multiplications, 2 additions, 4 minimum / maximum operations (8 when
marked), each on four lanes; per particle 2 more minimum / maximum operations on four lanes; per particle with `m_i = 0`
one comparison.

## 4. The flags of a pass

The status bits the pass raises are the union, over every lane of every operation of every particle (section 3), of the
flags section 2 gives: IOC, OFC, UFC, IDC (`fz` on only), IXC. DZC is never raised. The kernel returns them
(`INTERFACE.md`: bits already set in the status at the start, and IXC, may be left out).

## 5. Exactness notes (not rules: consequences of sections 2 and 3, for a fast implementation)

- **Plain values.** If lanes 0..2 of every point read (`P_i` of every particle, `Q_i` of every tracked particle) are zero
  or of magnitude in `[2^-63, 2^63)` (magnitude bits `0x20000000 .. 0x5effffff`, or zero), then in those lanes `D` is zero
  or a multiple of `2^-86` below `2^64`, `H` and `T` are never tiny or infinite, `mid` and `ahead` are multiples of
  `2^-87` below `2^65`: host IEEE binary32 arithmetic (SSE packed single precision with the default MXCSR: round to
  nearest, no flush, no denormals-are-zero) gives the Arm's bits, `fz` flushes nothing, no NaN arises and the only flag is
  IXC. The minimum and maximum of such values (neither a NaN nor subnormal) are the host's, except for the signed zeros'
  rule above (the host's packed minimum and maximum do not order `+0` and `-0`).
- **Lane 3.** Either lane 3 meets the same condition (then all four lanes are plain), or: IOC, OFC and UFC are already all
  set in `fpsr_in` (then the flags lane 3 can raise cost nothing; IDC is excluded next), every NaN read into lane 3 is
  exactly the default NaN `0x7fc00000`, and with `fz` on every lane 3 value read is zero, a NaN, an infinity or of
  magnitude at least `2^-100`. Then in lane 3 every value is zero, a NaN, an infinity or a multiple of `2^-124` (nothing is
  tiny, so `fz` flushes nothing), host IEEE arithmetic gives the Arm's value except a NaN's bits, and on the Arm every NaN
  lane 3 result is the default NaN (its NaN inputs are, and an invalid operation gives it): replace every NaN computed in
  lane 3 by `0x7fc00000` (`cw::w_canon` does that to every NaN lane of an SSE vector). The game itself runs with `fz` on
  and IOC, OFC, UFC, IXC set.
- **The masses.** If every inverse mass that is compared (`m_i = 0`) is neither a NaN nor a nonzero subnormal, the
  comparison raises nothing, and a zero of either sign skips.
- **The corners.** If `S` holds no signalling NaN and, with `fz` on, no nonzero subnormal, a corner never does (a minimum
  or maximum returns one of its operands, quieted and flushed). Then `min(corner, v)` with `v` as in the bullets above is:
  the corner where the corner's lane is a NaN; else `v` where `v`'s lane is a NaN; else the smaller, with the signed
  zeros' rule; and no flag. (`max` alike.) In the image the start value is `0x7f7fffee` in every lane.
- **Otherwise** a lane needs the rules of section 2 one lane at a time (`cloth_wlane.hpp`). Inputs with NaN payloads,
  signalling NaNs, infinities, subnormals, huge and tiny values, odd start values and odd inverse masses do occur in
  the checks. Nothing is written to memory, so a kernel may do a whole pass the fast way, find afterwards that a
  condition failed, and do the pass again by the rules.

## 6. Values handed back at the loop's end

The code after the pass reads more than the boxes. The kernel hands back, by meaning (the hook puts each where that
code finds it). There is one way out of the pass: every particle done (the kernel is never called with `n = 0`).

| Field | Value |
|---|---|
| `pos_min`, `pos_max` | the positions' box after the last particle (all four lanes each) |
| `moving_min`, `moving_max` | the moving box after the last particle (`S` and `-S` if no particle was tracked) |
| `marked_min`, `marked_max` | the marked box after the last particle (`S` and `-S` if none was tracked with bit 31 set) |
| `last_pos` | `P_{n-1}`, the last particle's position, its bits as read (no flush, no quieting) |
| `last_mask` | `m_{n-1}`, the last particle's mask word |
| `last_mid` | if the last particle was tracked: its `mid` (all four lanes as computed). If it was skipped: its inverse mass word `w_{n-1}`, its bits as read (not flushed), in lane 0, and zero (`0x00000000`) in lanes 1, 2 and 3 |
| `moved` | `1` when at least one particle was tracked, else `0` |
| `ahead` | the `ahead` point of the last tracked particle (the tracked particle with the highest index); not looked at when `moved = 0` |
| `ahead_step` | `T = D * 1.5` of that same particle (all four lanes as computed); not looked at when `moved = 0` |

Each is a value the maths above already computes or reads, with exactly its bits; nothing extra is computed and no flag
is added. The return value is the flags of section 4.
