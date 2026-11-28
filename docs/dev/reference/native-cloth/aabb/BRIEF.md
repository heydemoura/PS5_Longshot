# Brief for the session that writes the particle bounding boxes kernel

This is the complete instruction the implementing session receives. It has access only to the files named here and to
the output of `check.py`.

---

Write `cloth_aabb.cpp`, the host kernel for one loop of the game's Havok cloth ("particle bounding boxes": three
axis-aligned boxes over a cloth's particles, in single precision), from the specification in this folder. TIME BOX: 45
minutes; at 45 minutes stop and report where you are.

You may read ONLY these files in `C:\Repos\OoT-Modding\longshot-dev\cloth-spec\aabb\`: `SPEC.md`, `INTERFACE.md`,
`HARNESS.md`, `cloth_aabb.h`, `cloth_wlane.hpp`, `cloth_common.hpp`. Do NOT open or search anything else: not
`check.py`'s source (run it, do not read it), not the harness folder `longshot-dev\cloth-rewrite-tools`, not any Longshot
repository or worktree (`C:\Repos\Longshot*`, `G:\Longshot*`) or its git history, not the translated game code
(`build-hookshot\...\gen`), not the game image, not other folders under `longshot-dev`, `build-native` or
`G:\agent-runs`, not notes. Your only feedback is `check.py`'s output (pass or fail, and for a mismatch the case's
inputs, the expected values and yours, by the result field's name).

Write one file, `G:\agent-runs\cloth-spec\aabb\cloth_aabb.cpp`, in your own structure and names, implementing
`cloth_aabb.h` exactly as `INTERFACE.md` describes. Follow `SPEC.md`'s rules, operand order and evaluation order
exactly: the results must be bit-identical, float status flags included, and so must every value handed back
(`SPEC.md` 6: the boxes, the last particle's values, the last tracked particle's values). You may include the kit's
helpers (`cloth_wlane.hpp`, `cloth_common.hpp`); `check.py` puts them beside your file when it builds.

Speed is a requirement, not a bonus: the kernel must be at least as fast as the numbers in `HARNESS.md` (section 3),
within measurement noise, for the game's float state and the clean one. Use SSE vectors for the common case (`SPEC.md`
5 says when host arithmetic gives the Arm's bits) and the lane-by-lane rules only where they are needed. If it stays
slower, report which part and why.

Check with (from `C:\Repos\OoT-Modding`; `check.py` puts the compiler folder
`C:\Repos\OoT-Modding\build-hookshot\builder\llvm-mingw\bin` on PATH itself):

```
venv\Scripts\python.exe -I longshot-dev\cloth-spec\aabb\check.py G:\agent-runs\cloth-spec\aabb\cloth_aabb.cpp --work G:\agent-runs\cloth-spec\aabb\work
```

then the same with `--bench --no-exact` for the speed (`--cases 2000 --seeds 11` is a quick exactness run while you
work). Done means: `OVERALL: PASS` at the default size (seeds 11 and 12, 20,000 cases a seed, all three builds) and the
bench at or under the numbers in HARNESS.md.

Report: done or not; the last check's summary lines (per build: done, declined, particles native, MISMATCHES; OVERALL);
the bench numbers against HARNESS.md's; the file's line count; the elapsed time; anything in SPEC.md or INTERFACE.md that
was unclear or that you had to find out from a mismatch.
