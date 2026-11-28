# Native cloth: the thirteen kernels

The game's Havok cloth (the townspeople's clothes, Link's cap) runs on thirteen host kernels behind
`HOS_NATIVE_CLOTH`: three whole functions and ten loops inside larger routines. Each kernel was written from a
behavioural spec, and each hook hands back only what the code after it reads. This page says how the kernels were
made, how they are hooked, what was proven and what was found after the proofs, and how to test a change. The glue is
`game/oot_cloth.c`; its top comment is the short version.

## The pieces

| Piece (folder) | Game routine | Site | Stops part way? | What the hook hands back |
|---|---|---|---|---|
| Bending pass (`bend`) | f_81c7f880, whole | `cloth.bend` | yes: the routine finishes the rest | the slots it writes |
| Anchor pass (`anchor`) | f_81c7bd00, whole | `cloth.anchor` | no | the slots it writes |
| Prepare Matrices (`matrices`) | f_81cdff40, whole | `cloth.matrices` | no | the output, and seven vector values |
| Link pass (`link`) | loop in f_81c86720 | `cloth.links` | yes, at a link | nothing (the slots) |
| Bend links (`bend_link`) | loop in f_81c7d2e0 | `cloth.bendLinks` | yes, at a link | nothing (the slots) |
| Tethers (`tether`) | loop in f_81c88940 | `cloth.tethers` | yes, at a record | nothing (the slots) |
| Particle boxes (`aabb`) | loop in f_81b38800 | `cloth.boxes` | no | the three boxes' six corners |
| Capsule cull (`cull`) | loop in f_81b5a3c0 | `cloth.cull` | no | the count taken (and its list) |
| Verlet step (`verlet`) | loop in f_81c66a60 | `cloth.verlet` | no | nothing (the positions) |
| Capsule collision (`capsule`) | batches and left-over particles in f_81b8c000 | `cloth.capsule`, `cloth.capsuleTail` | yes | the list position after the batches |
| Sphere collision (`sphere`) | batches in f_81c1a240 | `cloth.sphere` | yes | the list position after the batches |
| Skinning (`skin`) | blocks in f_81db9a40 | `cloth.skin` | yes, at a block | nothing (the vertices) |
| Bone deform (`bones`) | loop in f_81ce6500 | `cloth.bones` | no | nothing (the bones) |

What stays translated: the rest of each routine around its loop (the link, bend link and tether routines are Havok
timer scopes around their loops), the sphere's left-over particles (1 to 15 a call: there is no kernel for them), the
skinning blocks in the 16-bit weight formats, and any call or record a kernel declines.

## What a hook hands back

The rule, from the review of this change: memory, the return value and the callee-saved registers are what a caller
can see. A hook writes those and what the rest of its own routine reads, and no other register and no flags.

- **A whole function** writes the memory the routine writes. Prepare Matrices also writes seven vector values: 20 of
  its 60 call sites read them when the next vertex has no bone influences.
- **A loop done whole** writes what the code after the loop reads, found by a data-flow pass over the routine after
  the loop: for most pieces nothing. The site then jumps to the loop's way out. Two pieces also set a pointer that the
  loop's set-up loads and the routine uses later (the boxes' particle data pointer, the cull's frame word), because
  the jump passes over that set-up.
- **A loop a kernel stops part way** leaves the loop's own state for the first record not done (the count left, the
  record pointer, the constants the loop's set-up put in registers), and the translated loop goes on from there.

A word of a routine's stack frame that only a later, unrelated function could read as an uninitialised local is not
reproduced.

## The hooks are hook sites

[hooks.md](../hooks.md) defines the forms. The cloth has 14 engine sites and 11 check-mode sites, `cloth.*` in
`game/sites/sites.toml`, the build's `locations.toml` and `game/sites.def`.

- The three whole functions are `entry` sites that can return.
- Each loop is a `jump` site to the loop's way out. A jump has one target, so a site sits where one is enough: the
  links', bend links' and tethers' at the last instruction of the loop's set-up, the capsule's two and the sphere's at
  the branches into their loops. There "every record done" is a non-zero answer, and "part done" is an answer of 0 with
  the registers set for the first record left. The skinning site is at a block's head, inside the loop: it is asked
  again at each block the kernel did not take.
- The sites at the ways out (`cloth.*End`) are check mode's compares. The start sets their callbacks only with
  `HOS_NATIVE_CLOTH=check`; otherwise each costs a flag's load.
- The anchor pass and Prepare Matrices each exist a second time, word for word, elsewhere in the game's code, so
  nothing inside them finds them once: their signatures end in the first word of the function that follows. The
  anchor's twin is its caller's other branch. It is not hooked; the same kernel would be exact for it.

## The float status

The lift computes the game's floats by the host's model (`builder/crates/sem/src/fp.rs`): none of FPSR's cumulative
flags is raised. So the gate is FPCR alone (its default but for FZ), each kernel is told every cumulative flag is set
already, and the hooks put nothing into FPSR. A gate that asked for the inexact flag, as this change's first version
did under the earlier lift, would never open.

The kernels themselves follow the Arm. Where the lift's model and the Arm differ (under FZ the Arm judges a result
tiny before rounding, the model after: a result that rounds up to the smallest normal), a lane a kernel does by the
Arm's rules gives the Arm's result. Check mode compares against the translated code, so such a lane would show as a
mismatch there. None has been seen in the game.

## How each kernel was made

For each piece a kit was written: `SPEC.md` (what the routine computes: the data, every case, every formula, the order
of the float operations where the bits depend on it; no instruction names, registers, addresses or branch layout),
`INTERFACE.md` and a header (arguments and results by meaning), and a pass / fail check. The kernel was then written by
a separate run that was given only that kit: not the earlier kernel, not the game's code, not its disassembly.
`BRIEF.md` in each folder is the instruction that run received. Twelve kernels were written by Grok 4.7 and one by
Codex (the bending pass); Codex also did a speed pass on the bend links kernel, working only from the kit and that file.

The check runs the game's own routine from the user's dump under Unicorn and compares it with the kernel: every byte of
memory the case can touch, and what a caller (or the rest of the routine) can see. It needs a game dump and is not in
this repository. Each kit was proven first: the earlier kernel passes it, and deliberately broken copies fail.

## What was proven, and what was found after

1. **Each kernel against the game's routine**: 0 mismatches on at least four seeds of 20,000 cases, two of which the
   implementing run never saw, in three builds (x86-64-v3, x86-64-v2, plain x86-64).
2. **Each piece's real glue against the game's routine**, the callback of `game/oot_cloth.c` itself in place of the
   kit's stand-in: 0 mismatches on seeds 11 and 31, 20,000 cases, three builds; and check mode's own compare reports a
   deliberately broken kernel.
3. **Three faults the kits' cases never reached, found after the wiring and fixed.** All are flush-to-zero cases on a
   fast (host float) path, where an intermediate result that is tiny was not flushed as the Arm flushes it:
   - the capsule's left-over particles: three lane-3 intermediates (two products, a difference) were not tested;
   - the capsule's batches: x, y and z were not tested after the contact test (it needs a coordinate of exactly zero
     and a push near 2^-133);
   - the anchor pass's vector path: lane-3 products, partial sums and a difference were not tested.

   The kits' random cases had no small normals in lane 3 (values a little above the smallest normal, whose products
   and differences are tiny while the inputs pass the input checks), and no scene scaled far enough down for x, y and
   z. Each fix sends such a record to the kernel's lane-by-lane path, was shown to fail before and pass after on a new
   class of cases against the game's routine, and has a test in the suite that the earlier kernel fails. The other ten
   kernels were audited for the same class, by reading and with 20,000 small-normal cases each against the game's
   routine: none had it. The fixes cost about 3 % on the capsule's batch kernel and on the anchor kernel under FZ.
4. **In the game**: `HOS_NATIVE_CLOTH=check` in the child Market, Kakariko Village and the Lost Woods, 45 s each: 6.2
   million calls compared, 0 mismatches; a kernel took every call but three bending calls.

What this does not give: the completeness of a kernel's checks is argued by hand from the float rules and backed by
the searches above, not proven formally. The lesson of point 3 is that a kit's random cases must include, per lane,
values whose products and differences are tiny, with FZ on.

## Changing a kernel

A kernel is its spec's: it is not reworked from the game's code. A fault is fixed against the spec, with a case that
fails first. Three kinds of edit have been made since the kernels were written: `(void)` and `[[maybe_unused]]` for
builds with warnings as errors, one warning pragma (the tether kernel's deliberate `&` on booleans), and the three
fixes above.

## Testing

1. The suites: `python tests/run_all.py cloth test_switch_table test_game_boundary`. `tests/cloth_glue.py` builds the
   glue for a test with stand-ins for `game_host.h` and `sites.h`; `tests/test_oot_cloth.py` also runs the hooks as the
   lift lowers them. Where a site is placed is checked by `cargo test -p sites` and `longshot-builder dev sites`.
2. A build, then the game with `HOS_NATIVE_CLOTH=check` in a place with cloth: every `MISMATCHES` count in the
   `oot: cloth` log lines must be 0, and "not compared" says how many calls were done in part.
3. Speed: main against the change, and the change with `HOS_NATIVE_CLOTH=0` against main, interleaved runs. The cloth
   limits the frame in the Lost Woods (child); in the Market and Kakariko it is about a millisecond a frame. Measured on
   main be663a08a (Ryzen 9 5900X, RTX 3090, uncapped): Lost Woods 118.2 -> 125.4 frames a second and the process's CPU
   time a frame 43.95 -> 40.09 ms; the Market and Kakariko within the runs' spread; `=0` within the spread of main.

`HOS_CLOTH_DECLINES=1` says why calls run translated; `HOS_CLOTH_CAPTURE=<folder>` writes declined calls for a replay.

## Open points

- The link pass at 42 links with clean lanes measured 8 to 10 % slower than the earlier kernel in two rounds on a
  busy PC; every other row was level or faster. It should be timed again.
- The skinning kernel is level with the earlier one in the x86-64-v2 build, 4 to 5 % slower on the smallest calls.
- `bend/SPEC.md` section 4.2 puts "(all four lanes)" after lists of several names; the scope should be said per name.
- The anchor pass's twin (f_81c7cb40) and Prepare Matrices' (f_81b54500) run translated.
- Linux has not been built or run with this change.
