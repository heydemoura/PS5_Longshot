# The capsule cull: behavioural spec

Before the game's cloth solver tests its particles against a capsule collider, it makes the list of the particles the
collider can touch: those inside the collider's box (an axis-aligned box around the capsule) and either free to move
(an inverse mass that is not zero) or in the collider's collision group (a mask). This file says what that pass
computes, precisely enough for identical bytes and float status flags, and which values it hands back at its end.
`INTERFACE.md` gives the C interface, `HARNESS.md` how a candidate is checked against the game's own code.

## 0. Conventions

**Floats.** Every float is an IEEE binary32 value, handled by its bit pattern. The pass does no arithmetic: it only
compares. So nothing is rounded, and the only status flags it can raise are IOC (invalid operation) and IDC (input
denormal, with flush-to-zero only).

**Vectors.** A position or a box corner is four floats, lanes 0..3 (x, y, z, w), 16 bytes, lane 0 at the lowest
address. A *verdict vector* is four 32-bit lanes, each all ones (`0xffffffff`, true) or all zeros (false).

## 1. Inputs

- `n`: the number of particles, 1 to 65535. Particles are visited in order, `i = 0 .. n - 1`.
- Particle `i`'s position `P_i` (four lanes) at `positions + 16 i`.
- The *box test*, on or off for the whole pass. When on: the box's low corner `L` (four lanes) and high corner `H` (four
  lanes). They hold anything (an inverted box, NaNs, infinities, subnormals are all possible), and lane 3 is compared
  like the others.
- The *form*, one for the whole pass:
  - **mass form:** particle `i`'s inverse mass `m_i`, one float (at `masses + 16 i`);
  - **mask form:** particle `i`'s collision mask `k_i`, a 32-bit integer (at `masks + 4 i`), and the collider's group
    bits `g`, a 32-bit integer.
- `fz`: flush-to-zero on or off.

The inputs do not change during the pass (the list meets none of them).

## 2. The two comparisons (the Arm's rules, for the float modes the hook lets through)

**"At least", `GE(a, b)`** (true when `a >= b`; a *signalling* comparison), on bit patterns `a` (first operand) and
`b` (second):

1. Only when `fz` is on: each operand that is a nonzero subnormal (exponent field 0, fraction not 0) is replaced by a
   zero of the same sign, and IDC is raised. Both operands are looked at, before NaNs are.
2. If either operand is a NaN, quiet or signalling: the result is false, and IOC is raised.
3. Otherwise the result is whether `a >= b` as numbers: `-0` equals `+0`, infinities compare as the largest values of
   their sign. No flag.

**"Is zero", `Z(m)`** (a *quiet* comparison with zero):

1. Only when `fz` is on: a nonzero subnormal `m` is replaced by a zero of its sign, and IDC is raised.
2. If `m` is a NaN: the result is "not zero". IOC is raised only for a signalling NaN (exponent all ones, fraction not 0,
   fraction bit 22 clear); a quiet NaN raises nothing.
3. Otherwise "zero" exactly when `m` is `+0` or `-0`. No flag.

The kit's helper `cloth_wlane.hpp` implements both for one lane: `cw::arm_fcmge(a, b, fz, flags)` (all ones or 0) and
`cw::arm_fcmp_zero_z(m, fz, flags)` (true for "zero"), ORing the flags they raise into `flags`. They are the reference
for every case, the rare ones included.

## 3. One particle

**The box test** (only when the box test is on). For particle `i` with position `P = P_i`, for every lane `q = 0, 1, 2,
3` (all four, lane 3 included):

- `lower_q = GE(P[q], L[q])` (the position first),
- `upper_q = GE(H[q], P[q])` (the high corner first),
- `in_q = lower_q AND upper_q`.

All eight comparisons are made for every tested particle, whatever their results, and all their flags count (the
corners are compared again at every test). Then:

- the particle is **inside** when `in_0`, `in_1` and `in_2` are all true (lane 3's result does not decide);
- its verdict vectors (used only by section 6) are, with `yz = in_1 AND in_2`:
  - `inside_yz = (yz, yz, yz, yz)`,
  - `inside_z = (in_2, in_2, in_2, in_2)`,
  - `inside_all = (in_0 AND yz, in_1 AND yz, in_2 AND yz, in_3 AND yz)` (so lane 0 is "inside", lanes 1 and 2 are `yz`,
    lane 3 is `in_3 AND yz`).

**Whether the particle is taken:**

1. If the box test is on and the particle is not inside: not taken. Its inverse mass or mask is then not looked at,
   and nothing about it raises a flag beyond its box test.
2. Mass form: `m_i` is examined with `Z`; the particle is taken when `Z(m_i)` is "not zero". So a NaN inverse mass is
   taken; `+0`, `-0` and, with `fz` on, a subnormal are not. The flags of `Z` count.
3. Mask form: the particle is taken when `k_i AND g` is not 0.

**Taking it:** with `count` the number of particles taken before it, the index `i` is written as an unsigned 16-bit
little-endian value at `list + 2 count`, and `count` grows by one.

## 4. The pass

`count` starts at 0; particles `0 .. n - 1` are done in order as section 3 says. The list's entries come out in
increasing index order. What is written: exactly the `count` entries `list[0 .. 2 count)`, nothing else in memory (the
bytes after the last entry are not touched).

## 5. The flags of a pass

The status bits raised are the union, over every comparison made in the pass (every lane of every box test, every
examined inverse mass), of the flags section 2 gives: IOC, and IDC (only with `fz` on). No other flag. The order of the
comparisons does not matter for the union. A mask-form pass without the box test makes no comparison and raises nothing.

## 6. Values handed back at the loop's end

The pass always runs to its end (after particle `n - 1`). Besides the list and the flags, it hands back the following,
for the code that follows it. "The last particle" is particle `n - 1`.

| Value | What it is |
|---|---|
| `count` | the number of particles taken (= the number of list entries written) |
| `status` | the flags of section 5 |
| `inside_yz`, `inside_z` | box test on: the last particle's verdict vectors (section 3; every particle is tested, so these are from the last particle's test). Box test off: not handed back (any value) |
| `mass_examined` | 1 when the last thing the pass examined was the last particle's inverse mass: mass form, and either the box test is off or the last particle was inside. Else 0 (always 0 in the mask form) |
| `last_mass` | when `mass_examined`: the last particle's inverse mass, its 32 bits exactly as read (not flushed). Else any value |
| `inside_all` | box test on and `mass_examined` 0: the last particle's `inside_all` vector (section 3). Else any value |
| `word_set`, `word` | the *last-decision word*, below |

**The last-decision word** (`word_set` says whether it was set at all; `word` is its value when set):

| Case (the first that applies) | `word_set` | `word` |
|---|---|---|
| the last particle was taken | 1 | `count` (the final count, the last particle included) |
| mask form (the last particle not taken) | 1 | 0 |
| mass form with the box test (the last particle not taken) | 1 | `0xffffffff` if the last particle was inside (it was not taken because its inverse mass is zero), 0 if it was outside |
| mass form without the box test (the last particle not taken), `count >= 1` | 1 | `count` |
| mass form without the box test, `count = 0` | 0 | any value |

## 7. Exactness notes (not rules: consequences of sections 2 and 3, for a fast implementation)

- With no NaN operand, `GE(a, b)` is the host's ordered `a >= b` on the operands as step 1 leaves them (with `fz` on,
  a nonzero subnormal replaced by a zero of its sign first). A host comparison that is false for NaNs (SSE's ordered
  less-or-equal with the operands swapped: `L <= P` for `lower`, `P <= H` for `upper`) gives `in_q` for every lane,
  NaNs included; the flags then follow from the values alone: IOC when any compared lane (a tested position's, or a
  corner's, as at least one test is made when the box test is on) is a NaN; with `fz` on, IDC when any compared lane is
  a nonzero subnormal.
- For an examined inverse mass: IOC only for a signalling NaN; with `fz` on, IDC for a nonzero subnormal (which then
  counts as zero). A mass that is not a NaN and not subnormal is "zero" exactly when its bits without the sign are 0.
- The verdict vectors and the word matter only for the last particle; the flags and the list for every particle.
