# Brief for the session that writes the capsule cull kernel

This is the complete instruction the implementing session receives. It has access only to the files named here and to
the output of `check.py`.

---

Write `cloth_cull.cpp`, the host kernel for one loop of the game's Havok cloth (the "capsule cull": the list of the
cloth's particles that lie inside a capsule collider's box and either can move or are in the collider's group; float
comparisons only, no arithmetic), from the specification in this folder. TIME BOX: 45 minutes; at 45 minutes stop and
report where you are.

You may read ONLY these files in `C:\Repos\OoT-Modding\longshot-dev\cloth-spec\cull\`: `SPEC.md`, `INTERFACE.md`,
`HARNESS.md`, `cloth_cull.h`, `cloth_wlane.hpp`, `cloth_common.hpp`. Do NOT open or search anything else: not
`check.py`'s source (run it, do not read it), not `LIVE.md` or the `result` folder, not the harness folder
`longshot-dev\cloth-rewrite-tools`, not any Longshot repository or worktree (`C:\Repos\Longshot*`, `G:\Longshot*`) or its
git history, not the translated game code (`build-hookshot\...\gen`), not the game image, not other folders under
`longshot-dev` or `build-native`, not notes. Your only feedback is `check.py`'s output (pass or fail, and for a mismatch
the case's inputs, the expected bytes and yours).

Write one file, `G:\agent-runs\cloth-spec\cull\cloth_cull.cpp`, in your own structure and names, implementing
`cloth_cull.h` exactly as `INTERFACE.md` describes. Follow `SPEC.md` exactly: the list must be byte-identical, the float
status flags identical, and so must the values handed back at the loop's end (`SPEC.md` 6: the count, the last
particle's verdict vectors, whether the pass ended on its inverse mass, and the last-decision word, each in the cases
the table names). You may include the kit's helpers (`cloth_wlane.hpp`, `cloth_common.hpp`); `check.py` puts them
beside your file when it builds.

Speed is a requirement, not a bonus: the kernel must be at least as fast as the numbers in `HARNESS.md` (section 3),
within measurement noise, for the game's float state and the clean one, both forms. Use SSE vectors for the box test
(`SPEC.md` 7 says how host comparisons give the Arm's answers and when the flags follow from the values) and the
lane-by-lane helpers only where they are needed. If it stays slower, report which part and why.

Check with (from `C:\Repos\OoT-Modding`; the script puts the compiler on PATH itself):

```
venv\Scripts\python.exe -I longshot-dev\cloth-spec\cull\check.py G:\agent-runs\cloth-spec\cull\cloth_cull.cpp
```

then the same with `--bench --no-exact` for the speed (`--cases 2000 --seeds 11` is a quick exactness run while you
work). Done means: `OVERALL: PASS` at the default size (seeds 11 and 12, 20,000 cases a seed, all three builds) and the
bench at or under the numbers in HARNESS.md.

Report: done or not; the last check's summary lines (per build: native, declined, MISMATCHES; OVERALL); the bench
numbers against HARNESS.md's; the file's line count; the elapsed time; anything in SPEC.md or INTERFACE.md that was
unclear or that you had to find out from a mismatch.
