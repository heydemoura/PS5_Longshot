# The anchor pass kernel: the C interface

The candidate is ONE file, `cloth_anchor.cpp`, implementing the one function of `cloth_anchor.h` (this folder):

```c
int cloth_anchor_81c7bd00(const ClothAnchorCall *call, ClothAnchorResult *res);
```

C++17 (the function itself `extern "C"`, as the header declares it), x86-64, SSE / SSE4.1 / AVX2 intrinsics allowed
behind the compiler's own feature macros (`__SSE4_1__`, `__AVX2__`, `__FMA__`): the file is built with
`-std=c++17 -O2 -ffp-contract=off -fno-strict-aliasing` three times, for `-march=x86-64-v3` (AVX2, FMA3),
`-march=x86-64-v2` (SSE4.2, no FMA) and `-march=x86-64` (SSE2 only), and must be exact in all three. No other source
files, no build files. It may include `cloth_anchor.h`, `cloth_common.hpp`, `cloth_wlane.hpp` (this folder, use them
freely, do not change them) and the standard C / C++ headers. No `-ffast-math`-style shortcuts, no `long double`.

## The call

`ClothAnchorCall` carries the routine's arguments by meaning (SPEC.md section 1) and the call's float context:

| Field | Meaning |
|---|---|
| `constraint` | guest address of the constraint object (SPEC.md 1.1) |
| `slots` | guest address of particle slot 0 (1.2) |
| `skin` | guest address of the skin (1.4) |
| `factor` | guest address of the factor (1.5) |
| `scale` | guest address of the scale object, or 0 (1.6) |
| `fz` | 1 when flush-to-zero is on for this call, else 0 |
| `fpsr` | the guest's float status bits at the call; IXC (bit 4) is always set |
| `range` | `range(addr, len)`: the host pointer of guest bytes `[addr, addr + len)`, or NULL when not all mapped |

The caller calls the kernel only when the float control is round to nearest with default-NaN off, no alternate
handling, no traps (flush-to-zero either way, given in `fz`), and IXC is already set: a kernel never reports IXC and
never meets another rounding mode.

**Memory.** All guest memory goes through `range`: ask before reading or writing a range (asking reads nothing; it may
be asked any number of times; it is cheap but not free, so ask for big spans once rather than per record). Guest
memory is one flat mapping: within a range that `range` returned, host and guest addresses differ by the same amount,
and the host pointers of two overlapping ranges overlap the same way. Write only the particle slots that the routine
writes, with the routine's bytes. Reading more than the routine reads is allowed (SPEC.md section 4, "Memory").

**The host float environment.** The kernel is entered with the x86 MXCSR at round to nearest, flush-to-zero and
denormals-are-zero off, all exceptions masked (status flags: anything). If it changes MXCSR it must put it back before
returning. x86 status flags are not the guest's: compute the Arm status bits as SPEC.md says (the helpers in
`cloth_wlane.hpp` return them), or prove them.

## The result

Return **1** when the call is done:
- guest memory written exactly as the routine writes it (SPEC.md 3.10, every record);
- `res->fpsr`: the status bits the routine raises (SPEC.md section 0; bits already set in `call->fpsr` may be left out,
  IXC never needs reporting). The caller ORs them into the guest's;
- `res->records`: the count of the list taken (0 when both lists are empty);
- `res->slot_lo`, `res->slot_hi`: the lowest and highest slot index stored (the caller marks those pages as written);
  `slot_lo > slot_hi` when nothing was stored. They must cover every slot stored;
- `res->why = CLOTH_ANCHOR_DONE`.

Return **0** when the call is declined: nothing written anywhere (a kernel that stored records before finding one it
cannot do puts every stored slot back to its entry bytes first), `res->why` = one of

| Reason | When |
|---|---|
| `CLOTH_ANCHOR_RANGE` | `range` returned NULL for a range the kernel needs |
| `CLOTH_ANCHOR_COUNT` | the count of the list taken is negative |
| `CLOTH_ANCHOR_VALUE` | a value for which the kernel cannot give the routine's exact bits or status bits |
| `CLOTH_ANCHOR_ALIAS` | a store could reach bytes the kernel holds from an earlier read that the routine reads again |
| `CLOTH_ANCHOR_LIMIT` | more records than the kernel's own bookkeeping holds (an undo list, say) |
| `CLOTH_ANCHOR_OTHER` | anything else |

The caller then runs the game's own routine on the unchanged memory. Declines cost speed, not correctness: a kernel
that declines plain cloth calls does not do its job (HARNESS.md gives the share the reference kernel answers).

There is no exit register state: the routine returns no value and leaves nothing else a caller can see, so the kernel
reports nothing but memory and the status bits.

Threads: the game runs cloth on several threads; the kernel must be reentrant (no writable globals shared between
calls; `thread_local` scratch is fine).
