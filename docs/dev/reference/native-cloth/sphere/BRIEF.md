# Brief for the session that writes the sphere contact kernel

This is the complete instruction the implementing session receives. It has access only to the files named here and to
the output of `check.py`.

---

Write `cloth_sphere.cpp`, the host kernel for one loop of the game's Havok cloth (the "sphere contact pass": records of
16 particles pushed out of a moving sphere, with a friction step on the previous positions of the particles pushed),
from the specification in this folder. TIME BOX: 60 minutes; at 60 minutes stop and report where you are.

You may read ONLY these files in `C:\Repos\OoT-Modding\longshot-dev\cloth-spec\sphere\`: `SPEC.md`, `INTERFACE.md`,
`HARNESS.md`, `cloth_sphere.h`, `cloth_common.hpp`, `cloth_wlane.hpp`. Do NOT open or search anything else: not
`check.py`'s source or the other files of this folder (run `check.py`, do not read it), not the `result` folder, not the
harness folder `longshot-dev\cloth-rewrite-tools`, not any Longshot repository or worktree (`C:\Repos\Longshot*`,
`G:\Longshot*`) or its git history, not the translated game code (`build-hookshot\...\gen`), not the game image, not other
folders under `longshot-dev`, `build-native` or `G:\agent-runs`, not notes. Your only feedback is `check.py`'s output
(pass or fail, and for a mismatch the case's inputs, where it differed, the expected bytes and yours).

Write one file, `G:\agent-runs\cloth-spec\sphere\cloth_sphere.cpp`, in your own structure and names, implementing
`cloth_sphere.h` exactly as `INTERFACE.md` describes. Follow `SPEC.md` exactly: the stored slots must be byte-identical
to the game's, including the pair store of SPEC.md 4 (the first two particles in contact of a group share one store to
the previous positions), every stored NaN `0x7fc00000`, and the records done (`done`, SPEC.md 8) right on every way out.
The structure of the computation is in SPEC.md 3 (the contact test for every particle, then the push and the friction
step for the particles in contact); describe-by-meaning names there map onto the header's fields as INTERFACE.md lists.
You may include the kit's helpers (`cloth_common.hpp`, `cloth_wlane.hpp`); `check.py` puts them beside your file when
it builds.

Speed is a requirement, not a bonus: the kernel must be at least as fast as the numbers in `HARNESS.md` (section 4),
within measurement noise, in every row. Use SSE vectors (for example the four particles of a group in one vector per
coordinate, or one particle's four lanes in one vector), with the plain path for the build without SSE4.1. The decline
checks (SPEC.md 6) can be done on whole vectors (an unsigned minimum or maximum of the values' bits) rather than value by
value. If it stays slower, report which part and why.

Check with (from `C:\Repos\OoT-Modding`; the script puts the compiler on PATH itself):

```
venv\Scripts\python.exe -I longshot-dev\cloth-spec\sphere\check.py G:\agent-runs\cloth-spec\sphere\cloth_sphere.cpp
```

then the same with `--bench --no-exact` for the speed (`--seeds 11 --cases 2000 --builds v3` is a quick exactness run
while you work; the first run of all compiles the game routine once, a few minutes). Done means: `OVERALL: PASS` at the
default size (seeds 11 and 12, 20,000 cases a seed, all three builds), a native share near the one HARNESS.md gives, and
the bench at or under the numbers in HARNESS.md.

Report: done or not; the last check's summary lines (per build: records done, part way, none by reason, MISMATCHES,
the native share; OVERALL); the bench numbers against HARNESS.md's; the file's line count; the elapsed time; anything in
SPEC.md or INTERFACE.md that was unclear or that you had to find out from a mismatch.
