# Brief for the session that writes the Verlet integration kernel

This is the complete instruction the implementing session receives. It has access only to the files named here and to
the output of `check.py`.

---

Write `cloth_verlet.cpp`, the host kernel for one loop of the game's Havok cloth (the Verlet integration step: every
particle's new position from its position, its previous position, the force gathered for it, gravity and damping, in
single precision), from the specification in this folder. TIME BOX: 45 minutes; at 45 minutes stop and report where
you are.

You may read ONLY these files in `C:\Repos\OoT-Modding\longshot-dev\cloth-spec\verlet\`: `SPEC.md`, `INTERFACE.md`,
`HARNESS.md`, `cloth_verlet.h`, `cloth_wlane.hpp`, `cloth_common.hpp`. Do NOT open or search anything else: not
`check.py`'s source (run it, do not read it), not the work folders it builds in, not the harness folder
`longshot-dev\cloth-rewrite-tools`, not any other folder of this repository, not any Longshot repository or worktree
(`C:\Repos\Longshot*`, `G:\Longshot*`) or its git history, not the translated game code (`build-hookshot\...\gen`), not
the game image, not notes. Your only feedback is `check.py`'s output (pass or fail, and for a mismatch the case's inputs,
the expected bytes and yours).

Write one file, `G:\agent-runs\cloth-spec\verlet\cloth_verlet.cpp`, in your own structure and names, implementing
`cloth_verlet_81c66a60` of `cloth_verlet.h` exactly as `INTERFACE.md` describes. Follow `SPEC.md`'s formulas and operand
order exactly: the stored bytes and the status bits must be identical to the game's, and so must the values handed back
in `ClothVerletLast` (`SPEC.md` 5: which particles they come from depends on the split into groups and singles). Use
the helpers in `cloth_common.hpp` and `cloth_wlane.hpp` as you see fit (include them; do not copy or change them);
`check.py` puts them beside your file when it builds.

Speed is a requirement, not a bonus: the kernel must be at least as fast as the numbers in `HARNESS.md` (section 3),
within measurement noise, for every row of the table: the game's float state (also with `G`'s lane 3 = 0) and the clean
one (also with the cloth far out, forces small or large, and mixed magnitudes). Use SSE vectors for the common case (SPEC.md 6
says when host arithmetic gives the Arm's bits) and the lane-by-lane rules only where they are needed. If it stays
slower, report which part and why.

Check with (from `C:\Repos\OoT-Modding`; the script puts the compiler on PATH itself):

```
venv\Scripts\python.exe -I longshot-dev\cloth-spec\verlet\check.py G:\agent-runs\cloth-spec\verlet\cloth_verlet.cpp --work G:\agent-runs\cloth-spec\verlet\work
```

then the same with `--bench --no-exact` for the speed (`--cases 2000 --seeds 11` is a quick exactness run while you
work). Done means: `OVERALL: PASS` at the default size (seeds 11 and 12, 20,000 cases a seed, all three builds) and the
bench at or under the numbers in HARNESS.md.

Report: done or not; the last check's summary lines (per build: native, declined, MISMATCHES; OVERALL); the bench
numbers against HARNESS.md's; the file's line count; the elapsed time; anything in SPEC.md or INTERFACE.md that was
unclear or that you had to find out from a mismatch.
