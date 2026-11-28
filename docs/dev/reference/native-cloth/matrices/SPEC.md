# Prepare Matrices: behavioural spec

The routine computes, for a list of entries, `O = M x (B x C)`: each entry's own matrix `C`, transformed by the bone
matrix `B` the entry names, then by a common matrix `M`, all in single precision. This file says what it computes
precisely enough for identical bits and float status flags. `INTERFACE.md` gives the C interface, `HARNESS.md` how a
candidate is checked against the game's own routine.

## 0. Conventions

**Floats.** Every value is an IEEE binary32 float. Every operation below is one Arm single-precision addition or
multiplication with its own rounding (round to nearest, ties to even): no fused multiply-add, no reassociation, no
wider intermediate that is not rounded back after each operation. Grouping is exactly as written: `(a + b) + c` is two
additions, the first `a + b`. **Operand order matters**, also for the commutative operations: it decides which NaN an
operation returns (section 2). `a + b` has first operand `a`, second `b`; `a * b` likewise.

**Vectors.** A *column* is four floats, lanes 0..3, 16 bytes, lane 0 at the lowest address. A *matrix* is four columns,
64 bytes, column 0 first. For a column `V`, `V[l]` is its lane `l`. Column operations work lane by lane on all four lanes:
`V + W` is `(V[0] + W[0], V[1] + W[1], V[2] + W[2], V[3] + W[3])` and `V * s` for a scalar `s` is
`(V[0] * s, V[1] * s, V[2] * s, V[3] * s)`, with the column's lane the FIRST operand and the scalar the SECOND in every
lane. All four lanes are always computed and stored, lane 3 included, whatever it holds: its results are stored and the
flags of its operations count.

## 1. Inputs and the entry list

- `M`: one matrix, columns `M0, M1, M2, M3`.
- The bones: an array of matrices, 64 bytes apart. Entry `i` names bone `k_i`, the unsigned 16-bit little-endian value
  at `index + 2 i`; its matrix `B` (columns `B0, B1, B2, B3`) is at `bones + 64 k_i`.
- The entries: entry `i`'s matrix `C` (columns `C0, C1, C2, C3`) at `in + 64 i`.
- The output: entry `i`'s result `O` (columns `O0, O1, O2, O3`) at `out + 64 i`.
- **The count.** The 32-bit count word holds two fields: `G = (word >> 4) & 0xffff`, a number of *groups* of sixteen
  entries, and `R = word & 15`, the entries after them. The routine does `N = 16 G + R` entries, `i = 0 .. N - 1` in
  order. Bits 20..31 of the word are not used. An entry `i < 16 G` is *in a group*; its *position* is `i mod 16`.
  `N = 0` is a call that computes and writes nothing and raises no flag.

What is read: the 64 bytes of `M`, `2 N` bytes of indices, `64 N` bytes of entries, and the bone matrices the indices
name. What is written: `64 N` bytes of output, nothing else in memory; besides it, seven column values are handed
back to the caller (section 6). (The hook guarantees that the output meets nothing that is
read, so the order of reads and writes between entries does not matter.)

## 2. The arithmetic of one operation (the Arm's rules, FPCR as the hook guarantees it)

The hook calls the kernel only with round to nearest, the default-NaN mode off, no alternative half precision, no
exception traps; flush-to-zero (`fz`) is on or off. One addition or multiplication of `a` (first operand) and `b`
(second):

1. **Flush of inputs (only when `fz` is on):** an operand that is a nonzero subnormal (exponent field 0, fraction not 0)
   is replaced by a zero of the same sign, and the input-denormal flag IDC is raised. This happens for both operands,
   before NaNs are looked at.
2. **NaNs:** if `a` is a signalling NaN (exponent all ones, fraction nonzero, fraction bit 22 clear), the result is `a`
   with bit 22 set, and IOC is raised. Else if `b` is a signalling NaN: `b` with bit 22 set, IOC. Else if `a` is a quiet
   NaN: `a` unchanged (payload and sign kept). Else if `b` is a quiet NaN: `b`. No other flag.
3. **Invalid operations:** `inf + (-inf)` (infinities of opposite signs added) and `0 * inf` in either order give the
   default NaN, bit pattern `0x7fc00000`, and raise IOC.
4. **Infinities and zeros:** as IEEE. A sum of two zeros is `+0` unless both are `-0`. `x + 0` with `x` not zero is `x`
   exactly. An exact cancellation `x + (-x)` is `+0`. A product's sign is the exclusive or of the operands' signs; a
   product with a zero operand (and no infinity) is a zero of that sign. These raise nothing.
5. **Rounding:** otherwise the exact result is rounded once to binary32, to nearest even.
   - If its magnitude **before rounding** is below `2^-126` (the result is *tiny*): with `fz` on, the result is a zero
     of the exact result's sign and UFC is raised (no rounding). With `fz` off, the result is the correctly rounded
     subnormal (or zero) and UFC is raised when that rounding was inexact.
   - If the rounded magnitude exceeds the largest finite float, the result is an infinity of its sign and OFC is raised.
   - IXC (inexact) is raised by any inexact result; it is always already set when the kernel is called, so a kernel need
     not track it.

The kit's helper `cloth_wlane.hpp` implements exactly these rules for one lane: `cw::arm_add(a, b, fz, flags)` and
`cw::arm_mul(a, b, fz, flags)` on bit patterns (first operand first), ORing the flags into `flags`. They are the
reference for every case, the rare ones included; they are slow.

Note for a fast path: when no operand and no result of an operation is a NaN, an infinity, a subnormal or tiny, host
IEEE arithmetic gives the same bits and only IXC; see section 5.

## 3. One entry

For entry `i` with bone `B` (columns `B0..B3`) and own matrix `C` (columns `C0..C3`), in this order of evaluation for
the float bits (the flags do not depend on the order: they are the union over all operations):

**First product `T = B x C`**, four columns `T0..T3`. Lane 3 of `C`'s columns is never used.

- `T0 = (B0 * C0[0] + B1 * C0[1]) + B2 * C0[2]`
- `T1 = B2 * C1[2] + (B0 * C1[0] + B1 * C1[1])`
- `T2 = B2 * C2[2] + (B0 * C2[0] + B1 * C2[1])`
- `T3 = B2 * C3[2] + (B1 * C3[1] + (B3 + B0 * C3[0]))`

Each `Bk * Cj[l]` is a column times a scalar (the bone's lane first, `Cj[l]` second); every `+` is a column addition
with the operands in the order written: in `T0` the inner sum's first operand is `B0 * C0[0]`; in `T1` and `T2` the
outer sum's first operand is `B2 * Cj[2]` and the inner one's `B0 * Cj[0]`; in `T3` the innermost sum is
`B3 + (B0 * C3[0])` with `B3` first, the middle one `(B1 * C3[1]) + (that)`, the outer one `(B2 * C3[2]) + (that)`.

**Second product `O = M x T`**, the same shape with `M` for `B` and `T` for `C`. Lane 3 of `T`'s columns is never used
(it is computed and its flags count, but it feeds nothing).

- `O0 = (M0 * T0[0] + M1 * T0[1]) + M2 * T0[2]`
- `O1 = M2 * T1[2] + (M0 * T1[0] + M1 * T1[1])`
- `O2 = M2 * T2[2] + (M0 * T2[0] + M1 * T2[1])`
- `O3 = M2 * T3[2] + (M1 * T3[1] + S)`, where `S` is the innermost sum of `M3` and `P = M0 * T3[0]`:
  - **`S = M3 + P` (`M3` first) for an entry that is not in a group, or is in a group at position 15;**
  - **`S = P + M3` (`P` first) for an entry in a group at positions 0 to 14.**

  The two orders give the same value; they differ only when both `M3`'s lane and `P`'s lane are NaNs (which one is
  returned, section 2 step 2), and in which signalling NaN is quieted first.

**Named partial sums** (used by section 6; they are the inner sums above, the same operations, not extra ones):
`U0 = M0 * T0[0] + M1 * T0[1]` (the inner sum of `O0`), `U1 = M0 * T1[0] + M1 * T1[1]` (the inner sum of `O1`),
`U2 = M0 * T2[0] + M1 * T2[1]` (the inner sum of `O2`), `S` (above), and `V = M1 * T3[1] + S` (the middle sum of `O3`).

**Stored:** `O0, O1, O2, O3` (all four lanes each) at `out + 64 i`, column 0 first.

Count: 24 column multiplications and 18 column additions an entry (12 + 9 for `T`, 12 + 9 for `O`), 168 lane
operations. Every one of them is performed on all four lanes and contributes its flags.

## 4. The flags of a call

The status bits the call raises are the union, over every lane of every operation of every entry, of the flags section
2 gives: IOC, OFC, UFC, IDC (fz on only), IXC. DZC is never raised. A call with `N = 0` raises nothing. The kernel
returns them (INTERFACE.md: bits already set in the status at the call, and IXC, may be left out).

## 5. Exactness notes (not rules: consequences of sections 2 and 3, for a fast implementation)

- **Plain values.** If every input lane that is an operand (lanes 0..2 of `C`'s columns; all lanes of `B` and of `M`)
  and every sum (all lanes) is zero or of magnitude in `[2^-63, 2^63)`, then every product is zero or in
  `[2^-126, 2^126)` and every sum is not tiny and finite: host IEEE binary32 arithmetic (SSE packed single-precision multiply
  and add with the default MXCSR: round to nearest, no flush, no denormals-are-zero) gives the Arm's bits, `fz` flushes nothing, no NaN
  arises, and the only flag is IXC. Checking the sums, not the products, is enough; the bounds are on the bit patterns:
  magnitude bits `0x20000000 .. 0x5effffff`, or zero.
- **Flags already set.** When IOC, OFC and UFC are all already set in `fpsr_in`, flags cost nothing to report, and only
  the bits matter. Then, if every NaN among the operand inputs is exactly `0x7fc00000` (the default NaN), and with `fz`
  on no value of the entry (operand inputs, every product and every sum, all lanes) is a nonzero of magnitude at most
  `2^-126`, host IEEE arithmetic gives the Arm's value in every lane except that a NaN result may have other bits: every
  NaN result is `0x7fc00000` on the Arm (both NaN sources are then the default NaN), so replace every NaN lane computed
  by `0x7fc00000`. (`2^-126` itself is excluded because, under `fz`, a result that rounds up to exactly `2^-126` from
  below is a flushed zero on the Arm.) The game itself runs with `fz` on and IOC, OFC, UFC, IXC set.
- **Otherwise** the entry needs the lane-by-lane rules of section 2 (`cw::arm_add` / `cw::arm_mul`). Inputs with NaN
  payloads, signalling NaNs, infinities, subnormals and huge values do occur in the checks.
- The swapped sum of section 3 can only show in the bits when two NaNs meet there; a vector path that has excluded NaNs
  (first bullet) or made every NaN the default NaN (second bullet) need not distinguish the two orders.

## 6. Values left for the caller

The routine's callers (the skinning loops that use its output) can, on one path, use seven column values the routine
leaves behind: when the first vertex they process after the call has no bone influences, they blend with columns they
never loaded, and those hold what the routine left. To keep the game's result there too, the kernel hands back those
seven columns as `left[0] .. left[6]`, each a full column of four lanes (all 16 bytes count: the callers use all four
lanes). Each is a value the maths above already computes (or reads), with exactly its bits (NaN choice, flush and all,
as section 2 gives them for that operation); nothing extra is computed and no flag is added.

Which values, by the count's case (`G` groups, `R` entries after them, `N = 16 G + R`; "last" is entry `N - 1`;
`e = 16 (G - 1)` is the first entry of the last group):

| | `R >= 1` (any `G`) | `R = 0`, `G >= 1` |
|---|---|---|
| `left[0]` | `O1` of the last entry | `O3` of entry `e + 1` |
| `left[1]` | `U2` of the last entry | `O3` of entry `e + 12` |
| `left[2]` | `V` of the last entry | `O3` of entry `e + 0` |
| `left[3]` | `U0` of the last entry | `O3` of entry `e + 11` |
| `left[4]` | `M2`, the model's column 2, its bits as read | `O3` of entry `e + 10` |
| `left[5]` | `U1` of the last entry | `O3` of entry `e + 9` |
| `left[6]` | `S` of the last entry (`M3 + P`, `M3` first: the last entry is not in a group) | `O3` of entry `e + 8` |

(`O3`, `O1` are the stored columns; `U0`, `U1`, `U2`, `S`, `V` the named partial sums of section 3, all of the entry
named.) With `N = 0` nothing is left: the kernel is not called and the caller's columns keep what they held.
