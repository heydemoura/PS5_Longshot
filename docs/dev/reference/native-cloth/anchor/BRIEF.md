# Brief for the session that writes the anchor pass kernel

This is the complete instruction for the implementing session. It has access only to the files named here and to the
output of `check.py`.

---

Write `cloth_anchor.cpp`, the host kernel for one routine of a Havok cloth solver (the anchor pass: particles pulled
back towards skinned anchor points), from the specification in this folder. TIME BOX: 30 minutes; at 30 minutes stop
and report where you are.

You may read ONLY these files in `C:\Repos\OoT-Modding\longshot-dev\cloth-spec\anchor\`: `SPEC.md`, `INTERFACE.md`,
`HARNESS.md`, `cloth_anchor.h`, `cloth_common.hpp`, `cloth_wlane.hpp`. Do NOT open or search anything else: not
`check.py` or `shim_81c7bd00.c` beyond running `check.py`, not the work folders it builds in, not
`longshot-dev\cloth-rewrite-tools` or any other folder of this repository, not the Longshot repositories or worktrees
or their git history, not the translated game code, not the game image, not notes. Your only feedback is `check.py`'s
output (pass or fail, and for a mismatch the case's inputs, the expected bytes and yours).

Write the one file `G:\agent-runs\cloth-spec\anchor\cloth_anchor.cpp`, in your own structure and names, implementing
`cloth_anchor_81c7bd00` exactly as `INTERFACE.md` describes. Follow `SPEC.md`'s formulas and operand order exactly:
the stored bytes and the status bits must be identical to the game's, and a call you cannot answer exactly must be
declined with nothing written. Use the helpers in `cloth_common.hpp` and `cloth_wlane.hpp` as you see fit (include
them; do not copy or change them).

Speed is a requirement, not a bonus: the kernel must be at least as fast as the numbers in `HARNESS.md` (mean
1,181 ns a call at 64 records and 9,018 ns at 512 with clean `w` lanes; 1,228 and 9,320 ns with the game's `w` mix),
within measurement noise. Those numbers come from a reference that copies every slot it may write before it starts, so
that it can put them back on a decline; they are the least you must reach. The target is better: about 17 ns a record
(1,090 ns at 64 records, 8,700 ns at 512, clean `w` lanes), which a kernel reaches when keeping a decline possible costs
almost nothing on a call that is done whole (the usual case). Do the four lanes together with SSE where the spec computes vectors. If it stays slower,
report which part and why.

Check with (from `C:\Repos\OoT-Modding`):

```
venv\Scripts\python.exe -I longshot-dev\cloth-spec\anchor\check.py G:\agent-runs\cloth-spec\anchor\cloth_anchor.cpp --work G:\agent-runs\cloth-spec\anchor\work
```

then with `--no-exact --bench` added. Use `--cases 2000 --seeds 11` for quick rounds while you work. Done means:
`OVERALL: PASS` at the default size (seeds 11 and 12, 20,000 cases each, all three builds), the line `declines of plain
cloth calls ... with the gate open: {}` for every build, and the bench means at or under the numbers above.

Report: done or not; the final check's summary lines (calls done and declined by reason per seed, mismatches); the
bench's `=> mean` lines against the numbers above; the file's line count; elapsed time; anything in the spec you found
unclear or had to guess.
