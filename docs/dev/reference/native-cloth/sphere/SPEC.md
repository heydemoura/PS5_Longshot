# The sphere contact pass: what it computes

One call of the pass pushes the cloth's particles out of a sphere-shaped collider that may be moving, and applies a
friction step to the particles it pushed. The particles come in **records** of 16; a call does its records in order.
Everything below is single-precision (binary32) arithmetic as the Arm architecture defines it (section 7 says when
the host's own IEEE arithmetic gives the same bits). The cumulative float status cannot change (section 7.1), so the
pass's only effects are the slot stores of section 4.

## 1. Inputs

Per call (`cloth_sphere.h` names the fields):

| name here | meaning |
|---|---|
| `C = (cx, cy, cz)`, `rho` | the sphere's centre and radius (`sphere[0..2]`, `sphere[3]`) |
| `eps[0..3]` | a small positive constant added to a squared distance, one per **place** 0..3 of a particle in its group (section 2); all four are equal in the game (2^-23), but each particle takes its own place's value |
| `dt` | the time step |
| `B = (bx, by, bz)` | the point of the collider's body that its angular velocity turns about |
| `v = (vx, vy, vz, vw)` | the body's linear velocity, all four lanes (the fourth lane is used: section 3.3) |
| `w = (wx, wy, wz)` | the body's angular velocity |
| `fz` | flush-to-zero: bit 24 of the float control word `fpcr` |

Per record `r` (0 .. records-1), per particle `k` (0..15) of the record:

| name here | meaning |
|---|---|
| `i_k` | the particle's number (unsigned 16-bit, `indices + 32 r + 2 k`) |
| `R_k` | its contact radius for this call (`radii + 64 r + 4 k`) |
| `F_k` | its friction factor (`frictions + 64 r + 4 k`) |
| `P_k = (Px, Py, Pz, Pw)` | its position: the four floats of slot `i_k` of `positions` |
| `Q_k = (Qx, Qy, Qz, Qw)` | its previous position: the four floats of slot `i_k` of `previous` |

`P_k` and `Q_k` are the slot contents as the record finds them: after every store of the records before it, and before
any store of its own (section 5).

## 2. Groups and places

The 16 particles of a record form four **groups** of four: particles 0-3, 4-7, 8-11, 12-15. A particle's **place** is
its position in its group, `k mod 4`; it selects `eps[k mod 4]`. Groups matter for the previous-position stores
(section 4); everything else is per particle.

## 3. Per particle

Each line is one rounded operation, evaluated exactly as written: the parentheses give the order, and the operand order
is the one written (it decides nothing in the cases the kernel does: section 7.2).

### 3.1 The contact test (every particle of the record)

```
dx = Px - cx          dy = Py - cy          dz = Pz - cz
sxy = dx*dx + dy*dy                 (the two products first, then their sum)
sq  = dz*dz + sxy
L   = eps[k mod 4] + sq
r   = InvRoot(L)                    (section 3.4)
len = L * r
pen = (len - rho) - R_k
contact  <=>  pen < 0               (ordered: a NaN pen is no contact; under flush-to-zero a subnormal pen is a zero
                                     first, so no contact)
```

### 3.2 The push out (a particle in contact)

```
dw = Pw - rho                       (the fourth lane takes the sphere's radius as its "centre")
nx = dx * r     ny = dy * r     nz = dz * r     nw = dw * r
Sx = cx + nx*rho    Sy = cy + ny*rho    Sz = cz + nz*rho          (the product first; S is the surface point)
ex = Px - Sx        ey = Py - Sy        ez = Pz - Sz
depth = R_k - ( nz*ez + (nx*ex + ny*ey) )
P'x = Px + nx*depth    P'y = Py + ny*depth    P'z = Pz + nz*depth    P'w = Pw + nw*depth
```

(The game evaluates `depth` with the outer sum's two operands in either order, depending on the group and the set of
particles in contact; the two orders give the same bits whenever the kernel does the record, section 7.2, so the order
above can be used throughout.)

### 3.3 The friction step (a particle in contact)

The body's velocity at the surface point, `V = v + w x (S - B)`:

```
ax = Sx - bx        ay = Sy - by        az = Sz - bz
Vx = vx + ( wy*az - ay*wz )
Vy = vy + ( wz*ax - az*wx )
Vz = vz + ( wx*ay - ax*wy )
Vw = vw + (+0)                      (a real addition of positive zero: -0 becomes +0, a NaN stays a NaN)
```

The motion relative to the body and its part along the normal:

```
relx = (P'x - Qx) - Vx*dt     rely = (P'y - Qy) - Vy*dt     relz = (P'z - Qz) - Vz*dt     relw = (P'w - Qw) - Vw*dt
dn   = (nx*relx + ny*rely) + nz*relz                 (the fourth lane takes no part)
Tx = relx - nx*dn     Ty = rely - ny*dn     Tz = relz - nz*dn     Tw = relw - nw*dn
Gx = Tx * F_k         Gy = Ty * F_k         Gz = Tz * F_k         Gw = Tw * F_k
Q'x = Qx + Gx         Q'y = Qy + Gy         Q'z = Qz + Gz         Q'w = Qw + Gw
```

### 3.4 InvRoot(L): the refined reciprocal square root

```
e0 = Estimate(L)                    the Arm's reciprocal square root estimate (the manual's FPRSqrtEstimate)
y1 = e0 * Step(L, e0*e0)
y2 = y1 * Step(L, y1*y1)
r  = y2, except r = +0 (all bits zero) when L <= 0x007fffff read as a float (the largest subnormal; the comparison
     also flushes both sides to zero under flush-to-zero, which gives the same set): that is, when L is zero, negative
     or subnormal. A NaN L gives a NaN r (the comparison is false).
```

- `Estimate(x)`: for a positive normal `x`, the bits `kc::frsqrte_bits(bits of x)` (`cloth_common.hpp`; checked against
  the manual on every positive normal float). `Estimate(+0) = +inf`, `Estimate(+inf) = +0`, `Estimate(NaN) = NaN`;
  negative and subnormal inputs only reach `r` through the `+0` rule above (the kernel may decline them, section 6).
- `Step(a, b)` (the manual's FPRSqrtStepFused): `(3 - a*b) / 2` computed exactly and **rounded once**; `Step(inf, 0)`
  and `Step(0, inf)` (either sign) are `1.5`; a NaN operand gives a NaN. Ways to get its bits on the host:
  `fmaf(-a, b, 3.0f) * 0.5f` (one rounding of `3 - a*b`, then an exact halving), or, without a fused multiply-add,
  `(float)(3.0 - (double)a * (double)b) * 0.5f`, which is exact when `a*b` lies in `[1/8, 8]` (`kc::rsq_step` in
  `cloth_common.hpp` shows both forms for one step).
- For the values the game has (`L` at least `eps > 0`), `L` is a positive normal number, `+inf` (then `r = +0`), or a
  NaN.
- Every product and step result here is an Arm operation like any other: under flush-to-zero a subnormal one is a zero
  (section 6, "Flush-to-zero"). This matters for `L` near the top of the float range (`e0*e0` below 2^-126).

## 4. The stores of a record

For each group, let its particles in contact be `a < b < c < d` (by `k`; zero to four of them):

- **Positions**: every particle in contact gets `P'` (all four lanes) in its position slot `i_k`.
- **Previous positions**:
  - one particle in contact (`a`): slot `i_a` gets `Q'_a`;
  - two or more: slot `i_a` gets the **pair sum** `G_b + Q'_a` lane by lane (`G_b` the friction term of section 3.3
    of particle `b`, the first operand; `Q'_a = Q_a + G_a`), and slot `i_b` gets **nothing** (its previous position
    stays as it was); every further particle in contact (`c`, `d`) gets its own `Q'` in slot `i_c`, `i_d`.

That is the game's behaviour (the first two particles in contact of a group share one store), not a simplification.
Particles not in contact get no store. Every stored NaN is the default NaN `0x7fc00000` (section 7.2).

Within a record the order of the stores does not matter: the record's 16 particle numbers are distinct (section 6) and
the two arrays meet neither each other nor any input. Every value a record computes is from the slot contents before
the record.

## 5. Records in order

Record `r + 1` starts after every store of record `r`: a particle number may appear in several records, and a later
record reads what an earlier one stored. The kernel does records 0, 1, 2, ... and stops at the first record it declines
(section 6), writing nothing for that record or any after it.

## 6. Declining

The kernel reports `done` (records done) and, when `done < records`, `why`. It must write nothing for a record it does
not do.

**Must decline the whole call** (`done = 0`, `why = CLOTH_SPHERE_FLOAT_MODE`) unless the float control is round to
nearest with no special mode except flush-to-zero (`fpcr & 0x06C8FF07` is zero; bit 24, flush-to-zero, either way) and
the cumulative float status already holds all six exception flags (`fpsr & 0x9f == 0x9f`: invalid, divide by zero,
overflow, underflow, inexact, input denormal).

**Must decline a record** (`why = CLOTH_SPHERE_REPEATED`) whose 16 particle numbers are not all distinct.

**Must decline a record** (`why = CLOTH_SPHERE_VALUES`) when a NaN other than `0x7fc00000` is among the values its
stored results or its contact decisions depend on (it may decline whenever any value the record reads is such a NaN).

**Flush-to-zero: decline, or reproduce the flush.** With flush-to-zero on, the Arm flushes every subnormal operand
and every tiny result of EVERY operation of section 3, the intermediate values of `InvRoot` included (`e0*e0`, the
first step's result, `y1`, `y1*y1`, the second step's result, `y2`: for a very large `L`, `e0*e0` is subnormal, the Arm
flushes it to zero and the step becomes exactly 1.5, while the host's arithmetic keeps it). A kernel that uses the
host's IEEE arithmetic must therefore decline (`why = CLOTH_SPHERE_VALUES`) a record in which any value that a stored
result or a contact decision depends on, read or computed, intermediate or not, is nonzero with magnitude at most
2^-126 (a subnormal, a tiny result, or a result the flush makes ambiguous at exactly 2^-126); it may check more values
than that (all the record's values, say). Reproducing the flush exactly instead (the `cw::arm_*` helpers) is allowed.

**May decline a record** (`why = CLOTH_SPHERE_VALUES`) when, for a value the record uses:
- the estimate's input is negative (not -0) or subnormal;
- a `Step(a, b)` with neither operand a NaN and not the `inf * 0` case has `a*b` outside `[1/8, 8]` (only if the kernel
  uses the binary64 form of section 3.4 there).

A kernel that declines only in these cases does about 64 % of the harness's particles (section 4 of `HARNESS.md`);
declining more costs native share, never exactness.

## 7. Float rules

### 7.1 Status

The kernel runs only when every cumulative flag is already set (section 6), so no operation can change the float
status, and the kernel reports none.

### 7.2 Values

With flush-to-zero off, or on and no value of the record in `(0, 2^-126]` in magnitude, and every NaN the record reads
being the default NaN `0x7fc00000`:
- each addition, subtraction and multiplication is IEEE binary32 round-to-nearest-even, on the Arm and on the host
  (overflow to infinity, signed zeros, `inf - inf` and `0 * inf` invalid);
- every NaN the Arm produces is `0x7fc00000`: a NaN operand is the default NaN, and an invalid operation gives the
  default NaN. The host's own NaN may differ (x86 makes `0xffc00000` for an invalid operation): every NaN the kernel
  stores must be written as `0x7fc00000`;
- so the operand order of an addition or multiplication does not change the bits (it only chooses between two NaN
  operands, and both are the default NaN);
- the comparisons are ordered (`pen < 0` is false for a NaN, `-0 < 0` is false).

Under flush-to-zero the Arm treats a subnormal operand as a zero of its sign and replaces a result that is tiny before
rounding (`|exact| < 2^-126`) by a zero of its sign; the declines of section 6 keep such values out of the records the
kernel does.

The particles' fourth lane (`w`) holds anything in the game: most often the default NaN, else normal values up to about
2^127. The rules above cover it (infinities and NaNs flow through it as IEEE says, every stored NaN made `0x7fc00000`).
`cloth_wlane.hpp` has exact one-operation Arm helpers (`cw::arm_add`, `cw::arm_sub`, `cw::arm_mul` with flush-to-zero)
for a kernel that wants to handle lane-3 values outside the rule of this section instead of declining them.

## 8. Values handed back at the loop's end

The pass is the record loop of a larger routine. After the loop, the routine reads exactly one value the loop leaves:
the position in the record list past the last record done. It is defined by `done` alone (the list's start plus 32
bytes a record), so the kernel hands back **only `done`** (and `why`), on every way out:

| way out | `done` | `why` |
|---|---|---|
| every record done | `records` | `CLOTH_SPHERE_ALL` |
| stopped at record `j` (declined: values or a repeated number) | `j` (records `0 .. j-1` stored, nothing of `j` or later) | `CLOTH_SPHERE_VALUES` / `CLOTH_SPHERE_REPEATED` |
| the float mode (the whole call) | `0` | `CLOTH_SPHERE_FLOAT_MODE` |

Nothing else of the loop's end state is read afterwards (the routine's other registers and its scratch memory are written
again before any read, or are not read at all), so the kernel computes nothing else. When `done < records` the game's own
loop does records `done` onwards from the slots as the kernel left them.
