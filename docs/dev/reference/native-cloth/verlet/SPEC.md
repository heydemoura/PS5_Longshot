# The Verlet integration step: behavioural spec

The loop moves every particle of a cloth one time step forward by Verlet integration: from its position and its
previous position (which together give its velocity), the force gathered for it, a gravity vector, a damping factor and
the squared time step, it computes the new position, stores it, and keeps the old position as the new previous
position. All arithmetic is single precision. This file says what the loop computes precisely enough for identical bits
and float status flags, and which values it hands back to the code that runs after it. `INTERFACE.md` gives the C
interface, `HARNESS.md` how a candidate is checked against the game's own code.

## 0. Conventions

**Floats.** Every value is an IEEE binary32 float. Every operation below is one Arm single-precision addition,
subtraction or multiplication with its own rounding (round to nearest, ties to even): no fused multiply-add, no
reassociation, no wider intermediate that is not rounded back after each operation. Grouping is exactly as written.
**Operand order matters**, also for the commutative operations: it decides which NaN an operation returns (section 2).
`a + b` has first operand `a`, second `b`; `a - b` and `a * b` likewise.

**Vectors.** A *column* is four floats, lanes 0..3, 16 bytes, lane 0 at the lowest address. For a column `V`, `V[l]` is
its lane `l`. Column operations work lane by lane on all four lanes: `V + W` is `(V[0] + W[0], ..., V[3] + W[3])`;
`V * s` for a scalar `s` is `(V[0] * s, ..., V[3] * s)` with the column's lane the FIRST operand and the scalar the
SECOND in every lane. All four lanes are always computed and stored, lane 3 included, whatever it holds: its results
are stored and the flags of its operations count.

## 1. Inputs, outputs and the particle list

The particles are `i = 0 .. N - 1`, `N = 4 groups + singles` (`singles` is 0..3). They are done in index order: first
`groups` groups of four consecutive particles, then `singles` particles one at a time. The split into groups and singles
changes nothing in what is stored or in the flags; it decides only which particles' values are handed back (section 5).

Per particle `i`:
- `P` = the column at `pos + 16 i`: its position.
- `Q` = the column at `prev + 16 i`: its previous position.
- `A` = the column at `force + 16 i`: the force gathered for it.
- `s_a`, `s_b` = the floats at `scale + 16 i` and `scale + 16 i + 4`: its two scale factors. (The other 8 bytes of
  each 16 are not used.)

The same for every particle:
- `G` = the column at `gravity`.
- `D` = the damping column (`damping[0..3]`, one factor a lane).
- `h` = the squared time step (`dt2`), one float.

**Written:** for each particle, the column at `pos + 16 i` becomes its new position `P'` and the column at
`prev + 16 i` becomes its old position `P` (bit for bit as read). Nothing else in memory. (The hook guarantees that the
two written arrays meet neither each other nor anything read, so the order of reads and writes between particles does
not matter.) Besides memory, the loop hands back the values of section 5.

## 2. The arithmetic of one operation (the Arm's rules, FPCR as the hook guarantees it)

The hook calls the kernel only with round to nearest, the default-NaN mode off, no alternative half precision, no
exception traps; flush-to-zero (`fz`) is on or off. One addition, subtraction or multiplication of `a` (first operand)
and `b` (second):

1. **Flush of inputs (only when `fz` is on):** an operand that is a nonzero subnormal (exponent field 0, fraction not 0)
   is replaced by a zero of the same sign, and the input-denormal flag IDC is raised. This happens for both operands,
   before NaNs are looked at.
2. **NaNs:** if `a` is a signalling NaN (exponent all ones, fraction nonzero, fraction bit 22 clear), the result is `a`
   with bit 22 set, and IOC is raised. Else if `b` is a signalling NaN: `b` with bit 22 set, IOC. Else if `a` is a quiet
   NaN: `a` unchanged (payload and sign kept). Else if `b` is a quiet NaN: `b` unchanged (in a subtraction too: its
   sign is NOT flipped). No other flag.
3. **Invalid operations:** `inf + (-inf)` (infinities of opposite signs added), `inf - inf` (infinities of the same sign
   subtracted) and `0 * inf` in either order give the default NaN, bit pattern `0x7fc00000`, and raise IOC.
4. **Infinities and zeros:** as IEEE. `a - b` is `a + (-b)` for everything but NaNs. A sum of two zeros is `+0` unless
   both are `-0`. `x + 0` with `x` not zero is `x` exactly. An exact cancellation (`x + (-x)`, `x - x`) is `+0`. A
   product's sign is the exclusive or of the operands' signs; a product with a zero operand (and no infinity) is a zero
   of that sign. These raise nothing.
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

## 3. One particle

For particle `i`, eight column operations, each on all four lanes, in this dependency order (the flags do not depend on
the order: they are the union over all operations):

| | value | meaning |
|---|---|---|
| 1 | `g = G * s_a` | gravity scaled (`G`'s lane first, `s_a` second) |
| 2 | `t1 = A + g` | the force plus scaled gravity (`A` first) |
| 3 | `F = t1 * s_b` | the scaled force (`t1`'s lane first) |
| 4 | `t3 = F * h` | its displacement over the step (`F`'s lane first, `h` second) |
| 5 | `d = P - Q` | the velocity over the last step (`P` first) |
| 6 | `E = D * d` | the damped velocity (`D`'s lane first, `d`'s second) |
| 7 | `M = P + E` | the position moved by it (`P` first) |
| 8 | `P' = M + t3` | the new position (`M` first) |

**Stored:** `P'` at `pos + 16 i` and `P` (the position as read, bits unchanged) at `prev + 16 i`, all four lanes each.

`s_a`, `s_b` and `h` are used as read (a subnormal one is flushed by rule 2.1 in each operation it is an operand of, like
any operand). Count: 32 lane operations a particle, every one contributing its flags.

## 4. The flags of a call

The status bits the loop raises are the union, over every lane of every operation of every particle, of the flags
section 2 gives: IOC, OFC, UFC, IDC (`fz` on only), IXC. DZC is never raised. The kernel returns them (INTERFACE.md:
bits already set in the status at the call, and IXC, may be left out).

## 5. Values handed back at the loop's end

The code after the loop can find some of the loop's last values where the loop leaves them. To keep that state exact,
the kernel hands back these values (each a column of four lanes, all 16 bytes counting, unless said otherwise). Each is
a value section 3 already computes or reads, with exactly its bits (NaN choice, flush and all, as section 2 gives them
for that operation; the inputs bit for bit as read); nothing extra is computed and no flag is added.

**The last group** is particles `4 (groups - 1) + k`, `k = 0..3`, the last group of four (there is one when
`groups >= 1`). For it:

| field | value |
|---|---|
| `group_old[k]`, `k = 0..3` | `P` of particle `k` of the last group (its position before the step) |
| `group_moved[k]`, `k = 0..3` | `M` of particle `k` |
| `group_new[0]`, `group_new[1]` | `P'` of particles `k = 2` and `k = 3` |
| `group_push[0]`, `group_push[1]` | `F` of particles `k = 2` and `k = 3` |
| `group_damped[0]`, `group_damped[1]` | `E` of particles `k = 2` and `k = 3` |

**The last single** is particle `4 groups + singles - 1`, the last one done singly (there is one when `singles >= 1`).
For it:

| field | value |
|---|---|
| `single_new` | its `P'` |
| `single_moved` | its `M` |
| `single_old` | its `P` (as read) |
| `single_force` | its `A` (as read) |
| `single_scale_b` | its `s_b` (one float, as read) |

Every way out of the loop:
- `groups >= 1`, `singles = 0`: the last group's fields; the single's fields are not written.
- `groups = 0`, `singles >= 1`: the single's fields; the group's fields are not written.
- `groups >= 1`, `singles >= 1`: both, each as defined above (the last group is the last four particles before the
  singles; the last single is particle `N - 1`).
- `N = 0`: the kernel is not called; nothing is handed back.

A field the call does not write may hold anything afterwards: only the fields named for the case are read.

## 6. Exactness notes (not rules: consequences of sections 2 and 3, for a fast implementation)

- **Plain values.** If, for a particle, every input lane it reads (`P`, `Q`, `A`, `G`, `D` all four lanes; `s_a`, `s_b`,
  `h`) and every value it computes (`g`, `t1`, `F`, `t3`, `d`, `E`, `M`, `P'`, all four lanes) is zero or of magnitude
  in `[2^-63, 2^63)`, then every operand of every operation is such a value, no operation can be tiny, infinite or NaN,
  host IEEE binary32 arithmetic (SSE packed single-precision add, subtract and multiply with the default MXCSR: round to
  nearest, no flush, no denormals-are-zero) gives the Arm's bits, `fz` flushes nothing, and the only flag is IXC. The
  bounds on the bit patterns: magnitude bits `0x20000000 .. 0x5effffff`, or zero.
- **Flags already set.** When IOC, OFC and UFC are all already set in `fpsr_in`, flags cost nothing to report, and only
  the bits matter. Then, if every NaN among the particle's inputs (every lane read, the shared `G`, `D`, `h` included) is
  exactly `0x7fc00000` (the default NaN), and with `fz` on no value of the particle (inputs and every computed value,
  all lanes) is a nonzero of magnitude at most `2^-126`, host IEEE arithmetic gives the Arm's value in every lane except
  that a NaN result may have other bits: every NaN result is `0x7fc00000` on the Arm (both NaN sources are then the
  default NaN), so replace every NaN lane computed by `0x7fc00000` (`cw::w_canon` does it for an SSE vector); a value
  stored or handed back as read keeps its bits. (`2^-126` itself is excluded because, under `fz`, a result that rounds up
  to exactly `2^-126` from below is a flushed zero on the Arm.) With `fz` off no flag is new either (IDC needs `fz`).
- **Otherwise** the particle needs the lane-by-lane rules of section 2 (`cw::arm_add` / `cw::arm_sub` /
  `cw::arm_mul`). Inputs with NaN payloads, signalling NaNs, infinities, subnormals and huge values do occur in the
  checks, in any lane and in the shared values.
- **The game's data:** `fz` on, the status 0x9f (IOC, DZC, OFC, UFC, IXC and IDC all set); lanes 0..2 ordinary numbers; lane 3
  of the positions, previous positions, forces and `G` most often the default NaN, else an ordinary or a huge number.
  `G`'s lane 3 may be the default NaN or an ordinary number. The fast ways above are what a fast kernel takes for nearly
  every particle; whether one particle can take a fast way is a property of that particle's values (and the shared
  ones), not of the call: a call whose shared values are plain can still have particles that fit only the second way.
