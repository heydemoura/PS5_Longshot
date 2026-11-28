# The tether pass kernel: the C interface

The candidate is ONE file, `cloth_tether.cpp`, implementing the one function of `cloth_tether.h` (this folder):

```c
void cloth_tether_81c88940(const ClothTetherCall *call, ClothTetherResult *res);
```

C++17 (the function itself `extern "C"`, as the header declares it), x86-64, SSE / SSE4.1 / AVX2 intrinsics allowed
behind the compiler's own feature macros (`__SSE4_1__`, `__AVX2__`, `__FMA__`): the file is built with
`-std=c++17 -O2 -ffp-contract=off -fno-strict-aliasing` three times, for `-march=x86-64-v3` (AVX2, FMA3),
`-march=x86-64-v2` (SSE4.2, no FMA) and `-march=x86-64` (SSE2 only), and must be exact in all three. No other source
files, no build files. It may include `cloth_tether.h`, `cloth_common.hpp`, `cloth_wlane.hpp` (this folder, use them
freely, do not change them) and the standard C / C++ headers. No `-ffast-math`-style shortcuts, no `long double`.

## The call

`ClothTetherCall` carries the loop's inputs by meaning (SPEC.md section 1) and the call's float context:

| Field | Meaning |
|---|---|
| `slots` | host pointer of particle slot 0 (SPEC.md 1.1); all 65536 slots (1 MB) are mapped; read and written |
| `records` | host pointer of record 0 (1.2); `count` records of 12 bytes are mapped; read only |
| `count` | the number of records, at least 1, below 2^31 |
| `scale` | the bits of the scale factor `s` (1.3) |
| `fz` | 1 when flush-to-zero is on for this call, else 0 |
| `fpsr` | the guest's float status bits at the call; IXC (bit 4) is always set |

The two pointers come from one flat mapping of guest memory: when the record list lies inside the slots in the guest,
`records` lies inside `[slots, slots + 1 MB)` the same way, so a slot store can change a later record (SPEC.md 2.2).
Pointers have no alignment beyond what the guest gives (slots and records may be at any 4-byte boundary).

The caller calls the kernel only when the float control is round to nearest with default-NaN off, no alternate
handling, no traps (flush-to-zero either way, given in `fz`), and IXC is already set: a kernel never reports IXC and
never meets another rounding mode.

**Memory.** Read only the slots and the records; write only slot `j` of each record done, with the loop's bytes
(SPEC.md 3.7), and nothing for the records not done.

**The host float environment.** The kernel is entered with the x86 MXCSR at round to nearest, flush-to-zero and
denormals-are-zero off, all exceptions masked (status flags: anything). If it changes MXCSR it must put it back before
returning. x86 status flags are not the guest's: compute the Arm status bits as SPEC.md says (the helpers in
`cloth_wlane.hpp` return them), or prove them.

## The result

Fill `*res` (every field the caller reads below; the caller has not cleared it):
- `res->done`: how many records were done, from the first, `0 .. count`. Records `0 .. done - 1` are stored exactly as
  the loop stores them (SPEC.md 3.7, in order, each seeing the earlier stores); nothing of records `done ..` is
  written. The caller then runs the game's own loop from record `done` (when `done < count`).
- `res->fpsr`: the status bits raised by the records done (SPEC.md section 0 and 3.4; bits already set in
  `call->fpsr` may be left out, IXC never needs reporting). The caller ORs them into the guest's. When `done` is 0 the
  caller ignores it.
- `res->slot_lo`, `res->slot_hi`: the lowest and highest slot index stored (the caller marks those pages as written);
  `slot_lo > slot_hi` when nothing was stored. They must cover every slot stored.
- `res->why`: `CLOTH_TETHER_DONE` when `done == count`; otherwise why the kernel stopped at record `done`:
  `CLOTH_TETHER_VALUE` (a value for which it cannot give the loop's exact bits or status bits) or `CLOTH_TETHER_OTHER`.
- `res->last` (only when `done == count`): the last record's values, SPEC.md section 5: `i`, `j`, `moved[4]`,
  `delta[4]`, `pull`, `len`, `rlen`, `stiffness`, as the bits of binary32 values (indices as plain numbers), vectors
  lane `x` first. Every one of them is compared.

Stopping costs speed, not correctness: the game's loop does the rest exactly. A kernel that stops on plain cloth does
not do its job (HARNESS.md gives the share the reference kernel does). A kernel must stop at a record it cannot answer
exactly; it must not stop after writing part of a record (a record's only store is its last act).

There is no other exit state: the loop's position and count at its end, its constants and the condition flags follow
from `count` and the record list's address, and the caller sets them.

Threads: the game runs cloth on several threads; the kernel must be reentrant (no writable globals shared between
calls; `thread_local` scratch is fine).
