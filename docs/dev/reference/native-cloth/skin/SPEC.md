# Object-space skinning by bone weights: behavioural spec

A cloth's vertices are kept as 16-bit local positions. Each frame they are skinned: every vertex is moved by a blend of
up to four bone matrices, weighted by 8-bit weights, and the result is stored as a four-float column. The work comes in
*blocks* of 16 vertices; all vertices of a block have the same number of bone influences (4, 3, 2 or 1). The kernel does
one block. This file says what it computes precisely enough for identical bits and float status flags. `INTERFACE.md`
gives the C interface, `HARNESS.md` how a candidate is checked against the game's own code.

## 0. Conventions

**Floats.** Every value is an IEEE binary32 float, handled as its 32-bit pattern. Every operation below is one Arm
single-precision addition or multiplication with its own rounding (round to nearest, ties to even): no fused
multiply-add, no reassociation, no wider intermediate that is not rounded back after each operation. Grouping is exactly
as written: `(a + b) + c` is two additions, the first `a + b`. **Operand order matters**, also for the commutative
operations: it decides which NaN an operation returns (section 2). `a + b` has first operand `a`, second `b`; `a * b`
likewise.

**Vectors.** A *column* is four floats, lanes 0..3, 16 bytes, lane 0 at the lowest address. A *matrix* is four columns,
64 bytes, column 0 first. For a column `V`, `V[l]` is its lane `l`. Column operations work lane by lane on all four
lanes: `V + W` is `(V[0] + W[0], ..., V[3] + W[3])` and `V * s` for a scalar `s` is `(V[0] * s, ..., V[3] * s)`, with
the column's lane the FIRST operand and the scalar the SECOND in every lane. All four lanes are always computed, lane 3
included, whatever it holds: its results are stored or handed back where this file says so, and the flags of its
operations always count.

**Integers** are little-endian. `u16(p)` is the unsigned 16-bit value at byte address `p`, `u8(p)` the byte.

## 1. The data

What is the same for every block (`ClothSkinParams`):

- `pos_low[0..3]`: four 16-bit values, the low halves of the position integers (section 3, step 1). 0 in the game.
- `weight_scale[0..3]`: four floats; weight lane `q` is multiplied by `weight_scale[q]`. 1/255 (`0x3b808081`) in every
  lane in the game.
- `fourth_weight`: one float, lane 3 of the weights of a 3-influence vertex, before the scale (section 3, step 3). 1.0
  in the game.
- `fz` (flush-to-zero on or off) and `fpsr_in` (the status flags already set).

One block (`ClothSkinBlock`), `n` = its influence count, 4, 3, 2 or 1:

- **Positions:** vertex `i` (0..15) has four unsigned 16-bit values `P_i[0..3]` at `positions + 8 i + 2 q`.
- **The record** (`record`), the block's entry in the stream of its format, laid out as:

  | bytes | what |
  |---|---|
  | `0 .. 31` | `num_i = u16(record + 2 i)`: vertex `i`'s output number |
  | `32 .. 32 + 32 n - 1` | the bone indices: vertex `i`'s influence `k` (`k < n`) is bone `idx_{i,k} = u16(record + 32 + 2 (n i + k))` |
  | `32 + 32 n ..` | the weights (`n >= 2`), at `W = record + 32 + 32 n`: `n = 4`: four bytes a vertex, vertex `i`'s at `W + 4 i`; `n = 3`: three bytes a vertex, at `W + 3 i`; `n = 2`: two bytes a vertex, at `W + 2 i`. `n = 1` has no weights. |

  A record is `32 + 32 n + 16 n'` bytes (`n' = n` for `n >= 2`, `0` for `n = 1`): `0xe0`, `0xb0`, `0x80`, `0x40`. A
  2-influence block also reads the two bytes right after its weights (section 3, step 3); the hook guarantees those
  readable (`CLOTH_SKIN_READABLE`).
- **Bones:** bone `k`'s matrix is 64 bytes at `bones + 64 (k - bone_base)`; columns `B[0..3]`.
- **Output:** vertex `i`'s slot is at the 32-bit offset `o_i = ((first + num_i) mod 2^32) * stride mod 2^32`
  (unsigned 32-bit arithmetic, wrapping), host address `out + (o_i - out_base)`. The 16-byte result is stored there.
  Slots of different vertices may overlap (equal numbers, a stride below 16, a stride of 0).

What is read: the positions, the record, the named bones. What is written: the 16 slots, nothing else in memory; besides
them, the values of section 6 are handed back. The hook guarantees that no slot meets anything the block reads, so the
order of reads and writes between vertices does not matter, but the order of the stores does (section 4).

## 2. The arithmetic of one operation (the Arm's rules, FPCR as the hook guarantees it)

The hook calls the kernel only with round to nearest, the default-NaN mode off, no alternative half precision, no
exception traps; flush-to-zero (`fz`) is on or off; and the inexact flag IXC already set. One addition or multiplication
of `a` (first operand) and `b` (second):

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

**Conversions** (integer to float): a 32-bit integer converted to binary32 is rounded once to nearest even; it raises
only IXC when inexact (never anything else: the result is an integer value, never subnormal, never flushed, never NaN).

The kit's helper `cloth_wlane.hpp` implements the operation rules for one lane: `cw::arm_add(a, b, fz, flags)` and
`cw::arm_mul(a, b, fz, flags)` on bit patterns (first operand first), ORing the flags into `flags`. They are the
reference for every case, the rare ones included; they are slow.

## 3. One vertex

For vertex `i` of a block with `n` influences, in this order of evaluation for the float bits (the flags do not depend
on the order: they are the union over all operations):

1. **The position integers.** For each lane `q = 0..3`: `z[q] = (P_i[q] << 16) | pos_low[q]`, a 32-bit pattern (the
   stored 16-bit value is the integer's upper half).
2. **The scaled position** `pos`: `c[q]` = `z[q]` read as a signed (two's complement) 32-bit integer and converted to
   float (section 2, conversions); `f` = the float whose bit pattern is `z[3]`. Then `pos[q] = c[q] * f` for
   `q = 0..3` (`c[q]` first, `f` second). All four lanes, lane 3 included (`pos[3]` is `z[3]`'s integer value times
   `z[3]`'s float value; it is computed and its flags count).
3. **The weights** (`n >= 2` only; `n = 1` computes none and raises nothing here). A four-lane column of raw values
   `r[0..3]`, then `w[q] = r[q] * weight_scale[q]` for `q = 0..3` (`r[q]` first):
   - `n = 4`: `r[q]` = the byte `u8(W + 4 i + q)` converted to float (exact), `q = 0..3`.
   - `n = 3`: `r[q]` = `u8(W + 3 i + q)` converted, `q = 0..2`; `r[3] = fourth_weight` exactly as given (not a byte, not
     converted).
   - `n = 2`: `r[q]` = `u8(W + 2 i + q)` converted, `q = 0..3`: lanes 2 and 3 are the NEXT two bytes (the next vertex's
     weights; for vertex 15 the two bytes after the weights area). Lanes 2 and 3 of `w` are computed (their flags count)
     but not used further.
   Of `w`, influence `k` uses `w[k]` (`k < n`); for `n = 3` lane 3 is computed and not used further.
4. **The bone matrices.** For `k < n`: `B_k` = the matrix of bone `idx_{i,k}`, columns `B_k[0..3]` (all four lanes
   read as they are).
5. **The blend** `M`, four columns `M[0..3]`:
   - `n = 1`: `M[c] = B_0[c]` exactly as read (no operation).
   - `n >= 2`: the *terms* `T[k][c] = B_k[c] * w[k]` (each lane: the bone's lane first, `w[k]` second), for every
     influence `k < n` and column `c = 0..3`; then `M[c]` is their sum in influence order, the running sum first:
     `n = 2`: `M[c] = T[0][c] + T[1][c]`; `n = 3`: `M[c] = (T[0][c] + T[1][c]) + T[2][c]`;
     `n = 4`: `M[c] = ((T[0][c] + T[1][c]) + T[2][c]) + T[3][c]`.
6. **The transform** (each lane: the column's lane first, the scalar second; sums with the operands in the order
   written):
   - `y_term = M[1] * pos[1]`
   - `x_term = M[0] * pos[0]`
   - `z_term = M[2] * pos[2]`
   - `x_sum = x_term + M[3]` for `n = 4` and `n = 3`; **`x_sum = M[3] + x_term` (`M[3]` first) for `n = 2` and
     `n = 1`.** The two orders give the same value; they differ only when both lanes are NaNs (which one is returned,
     section 2 step 2).
   - `xy_sum = y_term + x_sum`
   - `out = z_term + xy_sum`
7. **Stored:** `out` (all four lanes, 16 bytes) at vertex `i`'s slot.

Count, a vertex: `n = 1`: 3 column multiplications and 3 additions, plus the 4 lane multiplications of `pos`;
`n >= 2`: in addition 4 lane multiplications for `w`, `4 n` column multiplications for the terms and `4 (n - 1)` column
additions for `M`. Every one of them is performed on all four lanes and contributes its flags.

## 4. The block, and its flags

The vertices are done in order `i = 0 .. 15`, each stored as it is done; where two slots overlap, the later vertex's
bytes are the ones left. The status bits the block raises are the union, over every lane of every operation of every
vertex, of the flags section 2 gives: IOC, OFC, UFC, IDC (`fz` on only), IXC. DZC is never raised. The kernel returns
them (INTERFACE.md: bits already set in `fpsr_in`, and IXC, may be left out). The kernel never declines a block: every
value (NaNs, infinities, subnormals, huge or tiny numbers, garbage in lane 3) is done by these rules.

## 5. Exactness notes (not rules: consequences of sections 2 and 3, for a fast implementation)

- **Plain values.** If every float input that is an operand (`f`; for `n >= 2` `weight_scale[0..3]` and, for `n = 3`,
  `fourth_weight`; every lane of every bone column used) and every value computed that is an operand or a sum (`pos`,
  `w`, every partial and final sum of `M`, `x_sum`, `xy_sum`, `out`; all lanes) is zero or of magnitude in
  `[2^-63, 2^63)`, then every product is zero or in `[2^-126, 2^126)` and every sum is not tiny and finite: host IEEE
  binary32 arithmetic (SSE packed single-precision multiply, add and integer conversion with the default MXCSR: round to
  nearest, no flush, no denormals-are-zero) gives the Arm's bits, `fz` flushes nothing, no NaN arises, and the only flag
  is IXC. The bounds are on the bit patterns: magnitude bits `0x20000000 .. 0x5effffff`, or zero. (`c[q]` and the
  converted weight bytes are integers below `2^31` in magnitude: always zero or in the range.)
- **Flags already set.** When IOC, OFC and UFC are all already set in `fpsr_in`, flags cost nothing to report, and only
  the bits matter. Then, if every NaN among the float inputs read or given (`f`, the bone columns, `weight_scale`,
  `fourth_weight`) is exactly `0x7fc00000` (the default NaN), and with `fz` on no value of the vertex (those inputs,
  `c`, `pos`, `w`, every term, every product of step 6, every sum; all lanes) is a nonzero of magnitude at most
  `2^-126`, host IEEE arithmetic gives the Arm's value in every lane except that a NaN result may have other bits: every
  NaN result is `0x7fc00000` on the Arm (both NaN sources are then the default NaN), so replace every NaN lane computed
  by `0x7fc00000` (`cw::w_canon`). (`2^-126` itself is excluded because, under `fz`, a result that rounds up to exactly
  `2^-126` from below is a flushed zero on the Arm.) The game itself runs with `fz` on and IOC, OFC, UFC, IXC set.
- **Otherwise** the vertex needs the lane-by-lane rules of section 2 (`cw::arm_add` / `cw::arm_mul`). Inputs with NaN
  payloads, signalling NaNs, infinities, subnormals and huge values do occur in the checks, also in `f`,
  `weight_scale` and `fourth_weight`.
- The two orders of `x_sum` can only show in the bits when two NaNs meet there; a vector path that has excluded NaNs
  (first bullet) or made every NaN the default NaN (second bullet) need not distinguish them.
- The check runs the kernel on any mix of these per vertex: deciding per vertex (or per block) which way to go is
  enough, as long as each vertex's results and flags are the ones above.

## 6. Values handed back at the loop's end

After a block, the game's code that follows can see some values of the block's LAST vertex (vertex 15) that are not
stored to memory. The kernel hands them back in `ClothSkinLast` (all 16 bytes of each, four lanes): each is a value the
maths above already computes (or reads), with exactly its bits (NaN choice, flush and all, as section 2 gives them for
that operation); nothing extra is computed and no flag is added. The vertex loop of a block has one way out (after
vertex 15), so each value has one definition, by the block's influence count `n`:

| field | meaning (of vertex 15) | `n = 4` | `n = 3` | `n = 2` | `n = 1` |
|---|---|---|---|---|---|
| `out` | the stored result | yes | yes | yes | yes |
| `xy_sum` | `y_term + x_sum` | yes | yes | yes | yes |
| `x_sum` | step 6's `x_sum` | yes | - | - | yes |
| `y_term` | `M[1] * pos[1]` | yes | yes | yes | - |
| `pos` | the scaled position (step 2) | - | - | yes | - |
| `blend2` | `M[2]` (for `n = 1`: bone 0's column 2 as read) | - | yes | - | yes |
| `blend3` | `M[3]` (for `n = 1`: bone 0's column 3 as read) | yes | yes | yes | yes |
| `blend2_head` | column 2's running sum before the last influence's term is added: `n = 4`: `(T[0][2] + T[1][2]) + T[2][2]`; `n = 3`: `T[0][2] + T[1][2]`; `n = 2`: `T[0][2]` | yes | yes | yes | - |
| `last_col2` | `B_{n-1}[2]`, column 2 of the last influence's bone matrix, as read | yes | yes | yes | - |
| `term[1][1]` | `T[1][1]` | yes | yes | yes | - |
| `term[1][2]` | `T[1][2]` | yes | yes | - | - |
| `term[1][3]` | `T[1][3]` | yes | yes | yes | - |
| `term[2][0]`, `term[2][1]`, `term[2][3]` | `T[2][0]`, `T[2][1]`, `T[2][3]` | yes | yes | - | - |
| `term[2][2]` | `T[2][2]` | yes | - | - | - |
| `term[3][1]`, `term[3][3]` | `T[3][1]`, `T[3][3]` | yes | - | - | - |

A field marked "-" is not looked at for that `n` and may hold anything. No integer value is handed back: the last
vertex's slot offset and bone indices are facts of the record, which the hook reads itself.

## 7. Around the kernel (what the hook does; for understanding only)

- The routine walks a list of blocks, one *control byte* each. Control bytes 0, 1, 2, 3 are blocks of 4, 3, 2, 1
  influences with 8-bit weights: the hook calls the kernel with `n = 4 - byte`. Bytes 4 to 7 are blocks of 8, 7, 6, 5
  influences with 16-bit weights, which this kernel does not do (the game's own code does them). A byte above 7 is an
  empty block: no vertices, nothing read or written. Each format has its own stream of records; the positions advance
  by 16 vertices for every non-empty block.
- `first`, the output base and `stride` belong to the skinning operator and are the same for every block; the
  hook reads them and passes them in. The bone matrices are the ones the routine prepared before the loop.
- Every vertex of these four formats has at least one influence and reads all four columns of every bone it names:
  nothing carried over from a vertex, block or call before enters the arithmetic, and no value given at entry other than
  section 1's is used.
- The hook ORs the returned flags into the status register and puts the values of section 6 where the code after the
  loop finds them; for several blocks, the values of the last block of each influence count are the ones that remain.
