# Brief for the session that writes the Prepare Matrices kernel

This is the complete instruction the implementing session receives. It has access only to the files named here and to
the output of `check.py`.

---

Write `cloth_matrices.cpp`, the host kernel for one routine of the game's Havok cloth ("Prepare Matrices": a list of
4x4 matrix products in single precision), from the specification in this folder. TIME BOX: 30 minutes; at 30 minutes
stop and report where you are.

You may read ONLY these files in `C:\Repos\OoT-Modding\longshot-dev\cloth-spec\matrices\`: `SPEC.md`, `INTERFACE.md`,
`HARNESS.md`, `cloth_matrices.h`, `cloth_wlane.hpp`, `cloth_common.hpp`. Do NOT open or search anything else: not
`check.py`'s source (run it, do not read it), not the harness folder `longshot-dev\cloth-rewrite-tools`, not any Longshot
repository or worktree (`C:\Repos\Longshot*`) or its git history, not the translated game code
(`build-hookshot\...\gen`), not the game image, not other folders under `longshot-dev` or `build-native`, not notes.
Your only feedback is `check.py`'s output (pass or fail, and for a mismatch the case's inputs, the expected bytes and
yours).

Write one file, `G:\agent-runs\cloth-spec\matrices\cloth_matrices.cpp`, in your own structure and names, implementing
`cloth_matrices.h` exactly as `INTERFACE.md` describes. Follow `SPEC.md`'s formulas, operand order and evaluation order
exactly: the results must be bit-identical, float status flags included, and so must the seven values left for the
caller (`SPEC.md` 6: they depend on the count's case). You may include the kit's helpers
(`cloth_wlane.hpp`, `cloth_common.hpp`); `check.py` puts them beside your file when it builds.

Speed is a requirement, not a bonus: the kernel must be at least as fast as the numbers in `HARNESS.md` (section 3),
within measurement noise, for the game's float state and the clean one. Use SSE vectors for the common case (SPEC.md
5 says when host arithmetic gives the Arm's bits) and the lane-by-lane rules only where they are needed. If it stays
slower, report which part and why.

Check with (from `C:\Repos\OoT-Modding`):

```
venv\Scripts\python.exe -I longshot-dev\cloth-spec\matrices\check.py G:\agent-runs\cloth-spec\matrices\cloth_matrices.cpp
```

then the same with `--bench --no-exact` for the speed (`--cases 2000 --seeds 11` is a quick exactness run while you
work). Done means: `OVERALL: PASS` at the default size (seeds 11 and 12, 20,000 cases a seed, all three builds) and the
bench at or under the numbers in HARNESS.md.

Report: done or not; the last check's summary lines (per build: native, declined, MISMATCHES; OVERALL); the bench
numbers against HARNESS.md's; the file's line count; the elapsed time; anything in SPEC.md or INTERFACE.md that was
unclear or that you had to find out from a mismatch.
