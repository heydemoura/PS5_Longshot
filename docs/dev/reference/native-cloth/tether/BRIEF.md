# Brief for the session that writes the tether pass kernel

This is the complete instruction for the implementing session. It has access only to the files named here and to the
output of `check.py`.

---

Write `cloth_tether.cpp`, the host kernel for one loop of a Havok cloth solver (the tether pass: each record pulls one
particle back towards an anchor particle when their distance exceeds a rest length), from the specification in this
folder. TIME BOX: 45 minutes; at 45 minutes stop and report where you are.

You may read ONLY these files in `C:\Repos\OoT-Modding\longshot-dev\cloth-spec\tether\`: `SPEC.md`, `INTERFACE.md`,
`HARNESS.md`, `cloth_tether.h`, `cloth_common.hpp`, `cloth_wlane.hpp`. Do NOT open or search anything else: not
`check.py` or `shim_81c88940.c` beyond running `check.py`, not the work folders it builds in, not
`longshot-dev\cloth-rewrite-tools` or any other folder of this repository, not the Longshot repositories or worktrees
or their git history, not the translated game code, not the game image, not notes. Your only feedback is `check.py`'s
output (pass or fail, and for a mismatch the case's inputs, the expected bytes and values and yours).

Write the one file `G:\agent-runs\cloth-spec\tether\cloth_tether.cpp`, in your own structure and names, implementing
`cloth_tether_81c88940` exactly as `INTERFACE.md` describes. Follow `SPEC.md`'s formulas and operand order exactly:
the stored bytes, the status bits and the last record's handed-back values must be identical to the game's, and a
record you cannot answer exactly must not be written (stop before it; the caller's loop does the rest). Use the helpers
in `cloth_common.hpp` and `cloth_wlane.hpp` as you see fit (include them; do not copy or change them).

Speed is a requirement, not a bonus: the kernel must be at least as fast as the numbers in `HARNESS.md` (mean ns a
call with clean `w` lanes: 190 at 16 records, 682 at 64, 2,719 at 256, 10,625 at 1024; with the game's `w` mix: 224,
809, 3,166, 12,697), within measurement noise (the PC is shared with other jobs: run the bench twice). Do the four
lanes together with SSE where the spec computes vectors, keep the per-record checks cheap (the usual record is plain:
everything in the class, only `w` outside it), and do not copy what you need not. If it stays slower, report which
part and why.

Check with (from `C:\Repos\OoT-Modding`; `check.py` puts the compiler on PATH itself):

```
venv\Scripts\python.exe -I longshot-dev\cloth-spec\tether\check.py G:\agent-runs\cloth-spec\tether\cloth_tether.cpp --work G:\agent-runs\cloth-spec\tether\work
```

then with `--no-exact --bench` added. Use `--cases 2000 --seeds 11` for quick rounds while you work. Done means:
`OVERALL: PASS` at the default size (seeds 11 and 12, 20,000 cases each, all three builds), the line `plain cloth calls
... not done whole: {}` for every build, and the bench means at or under the numbers above.

Report: done or not; the final check's summary lines (calls done whole, stopped part way and none done, by reason, per
seed; records done; mismatches); the bench's `=> mean` lines against the numbers above; the file's line count; elapsed
time; anything in the spec you found unclear or had to guess.
