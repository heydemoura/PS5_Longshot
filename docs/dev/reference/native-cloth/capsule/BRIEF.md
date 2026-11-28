# Brief for the session that writes the capsule collision kernel

This is the complete instruction the implementing session receives. It has access only to the files named here and to
the output of `check.py`.

---

Write `cloth_capsule.cpp`, the host kernel for the capsule collider's particle pass of the game's Havok cloth (particles
pushed out of a capsule, their previous positions corrected for friction against the collider's moving surface; in
batches of 16 particles and, for the particles left over, one at a time), from the specification in this folder. TIME
BOX: 60 minutes; at 60 minutes stop and report where you are.

You may read ONLY these files in `C:\Repos\OoT-Modding\longshot-dev\cloth-spec\capsule\`: `SPEC.md`, `INTERFACE.md`,
`HARNESS.md`, `cloth_capsule.h`, `cloth_wlane.hpp`, `cloth_common.hpp`. Do NOT open or search anything else: not
`check.py`'s source (run it, do not read it), not `LIVE.md` or the `result` folder, not the harness folder
`longshot-dev\cloth-rewrite-tools`, not any Longshot repository or worktree (`C:\Repos\Longshot*`, `G:\Longshot*`) or its
git history, not the translated game code (`build-hookshot\...\gen`), not the game image, not other folders under
`longshot-dev` or `build-native`, not notes. Your only feedback is `check.py`'s output (pass or fail, and for a mismatch
the case's inputs, the expected bytes and yours).

Write one file, `G:\agent-runs\cloth-spec\capsule\cloth_capsule.cpp`, in your own structure and names, implementing
`cloth_capsule.h` exactly as `INTERFACE.md` describes. Follow `SPEC.md` exactly: the stored positions and previous
positions must be byte-identical to the game's, including lane w, signed zeros and NaN bits (`SPEC.md` 6), and the
store rule of a batch (`SPEC.md` 4: the first two particles in contact in a group share one previous-position store).
The values handed back are only those of `SPEC.md` 8 (`done`, `why`, `contacts`). You may include the kit's helpers
(`cloth_wlane.hpp`, `cloth_common.hpp`); `check.py` puts them beside your file when it builds.

Speed is a requirement, not a bonus: the kernel must be at least as fast as the numbers in `HARNESS.md` (section 3),
within measurement noise, for the game's float state and the clean one, batches and singles, and it must do at least
the share of particles natively that `HARNESS.md` gives (section 2): leaving more batches to the game than needed is
exact but fails that requirement. Use SSE vectors for the batch's 16 contact tests (four groups of four lanes) and the
lane-by-lane path only where it is needed; the plain x86-64 build (no SSE4.1) must be exact too.

Check with (from `C:\Repos\OoT-Modding`; the script puts the compiler on PATH itself):

```
venv\Scripts\python.exe -I longshot-dev\cloth-spec\capsule\check.py G:\agent-runs\cloth-spec\capsule\cloth_capsule.cpp
```

then the same with `--bench --no-exact` for the speed (`--cases 2000 --seeds 11` is a quick exactness run while you
work; the first build of a work folder compiles the game's translated routine and takes several minutes, later builds
reuse it). Done means: `OVERALL: PASS` at the default size (seeds 11 and 12, 20,000 cases a seed, all three builds), the
native share at or above HARNESS.md's, and the bench at or under the numbers in HARNESS.md.

Report: done or not; the last check's summary lines (per build: native share, batches and singles native and left,
stops by reason, MISMATCHES; OVERALL); the bench numbers against HARNESS.md's; the file's line count; the elapsed time;
anything in SPEC.md or INTERFACE.md that was unclear or that you had to find out from a mismatch.
