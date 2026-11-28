# Brief for the session that writes the bend link pass kernel

This is the complete instruction the implementing session receives. It has access only to the files named here and to
the output of `check.py`.

---

Write `cloth_bend_link.cpp`, the host kernel for one pass of a cloth solver's bend links (minimum / maximum distance
constraints between two particles), from the specification in this folder. TIME BOX: 45 minutes; at 45 minutes stop and
report where you are.

You may read ONLY these files in `C:\Repos\OoT-Modding\longshot-dev\cloth-spec\bend_link\`: `SPEC.md`, `INTERFACE.md`,
`HARNESS.md`, `cloth_bend_link.h`, `cloth_wlane.hpp`, `cloth_common.hpp`. Do NOT open or search anything else: not
`check.py`'s source or its `harness\` folder beyond running it, not the work folder it builds in, not
`longshot-dev\cloth-rewrite-tools`, not any Longshot repository or worktree or its git history, not the translated game
code, not the game image, not notes or other kits. Your only feedback is `check.py`'s output (pass or fail, and for a
mismatch the case's inputs, the expected bytes and yours).

Write the one file `G:\agent-runs\cloth-spec\bend_link\cloth_bend_link.cpp`, in your own structure and names,
implementing `cloth_bend_link_pass` of `cloth_bend_link.h` exactly as `INTERFACE.md` describes. Follow `SPEC.md`'s
formulas, operand order and memory order exactly: the results must be bit-identical, including the float status, lane
`w`, the values handed back at the loop's end (section 5) and the links declined (section 4: every link in the class
must be done, every other one declined).

Speed is a requirement, not a bonus: the kernel must be at least as fast as the numbers in `HARNESS.md` section 2
(about 15-22 ns a link, whole calls), within measurement noise (`check.py --bench` prints `SPEED MEETS THE BAR`). Use SSE4.1 / AVX
vectors where the spec computes lanes together, keep the class checks off the per-link dependency chain, fill the
hand-back once after the last link (not on every link), and give plain SSE2 builds a correct (possibly slower) form. If
the kernel stays slower, report which part and why.

Check with (from `C:\Repos\OoT-Modding`; `check.py` puts the compiler's folder on PATH itself):
`venv\Scripts\python.exe -I longshot-dev\cloth-spec\bend_link\check.py G:\agent-runs\cloth-spec\bend_link\cloth_bend_link.cpp`,
then the same with `--bench`. Use `--cases 2000 --seeds 11` for quick rounds while working (about 20 seconds); the final
check is the default (about 2 minutes). Done means: `OVERALL: PASS` at the default size (seeds 11 and 12, 20,000 cases
each, all three builds: 0 mismatches and at least the reference's links done natively) and the bench at or under the
bar.

Report: done or not; the final check's summary lines (per seed: mismatches, links native against the reference's); the
bench table and ratio; the line count of your file; the elapsed time; anything in the spec you found unclear or wrong.
