# SPDX-License-Identifier: GPL-3.0-or-later
"""The mesh draw log counters stop counting once they stop logging.

Two "log the first N" counters run on every mesh draw: mesh_program_locked's (vk_shader.cpp, 6 lines of the draw's
stages) and the replay's NOP_DRAW_MESH (vk_replay.cpp, 8 lines of the draw's arguments). Both were incremented on every
draw, not only while under their limit, so after 2^31 mesh draws (a long session: hours at the game's few hundred mesh
draws a frame) the signed int overflowed, undefined behaviour that in practice wrapped negative and logged every mesh
draw again, the stages read back and parsed for each one.

Built from the sources' own text (the two counters' lines, their `static` taken off so the check can read them), with
stubs for the log calls: after 1000 draws each logged its limit and its counter stopped there.
    python -I -B tests/test_mesh_log_counters.py [--cxx clang++]
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))   # (python -I leaves the check's own folder off the path)
from srccut import between, block

RUN_ALL_ARGS = ["--cxx", "{cxx}"]

PRELUDE = r'''
#include <cstdint>
#include <cstdio>
struct NvnState {};
struct DrawMesh { int kind, x, y, z; unsigned long long indirect, count_addr; int max, stride; };
int stage_logs = 0, args_logs = 0;
void log_program_stages(const NvnState&, const char*) { ++stage_logs; }
void logf(const char*, ...) { ++args_logs; }
int logged, logged_mesh;   // (the counters, out of their functions)
void shader_draw(const NvnState& s) {
'''

CHECKS = r'''
int main() {
 NvnState s;
 for (int i = 0; i < 1000; i++) { shader_draw(s); replay_draw(); }
 int failures = 0;
 auto eq = [&](long long got, long long want, const char* what) {
  if (got != want) { std::printf("%s: %lld, not %lld\n", what, got, want); ++failures; }
 };
 eq(stage_logs, 6, "mesh_program_locked's stage lines");
 eq(logged, 6, "mesh_program_locked's counter after 1000 mesh draws");
 eq(args_logs, 8, "the replay's mesh draw lines");
 eq(logged_mesh, 8, "the replay's counter after 1000 mesh draws");
 if (!failures) std::printf("mesh log counter checks passed\n");
 return failures ? 1 : 0;
}
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument('--cxx', default=os.environ.get('CXX') or 'c++')
    a = p.parse_args()
    root = a.source_root
    shader = (root / 'gpu/vk/vk_shader.cpp').read_text(encoding='utf-8')
    replay = (root / 'gpu/vk/vk_replay.cpp').read_text(encoding='utf-8')
    locked = block(shader, 'const Program* mesh_program_locked(')
    stages = between(locked, 'static int logged;', 'if (!mesh)').replace('static int logged;', '', 1)
    draw = between(block(replay, 'case NOP_DRAW_MESH: {'), 'static int logged_mesh;', 'DrawArgs da;')
    draw = draw.replace('static int logged_mesh;', '', 1)
    text = PRELUDE + stages + '\n}\nvoid replay_draw() {\n const DrawMesh d{};\n' + draw + '\n}\n' + CHECKS
    with tempfile.TemporaryDirectory(prefix='longshot-mesh-log-counters-') as tmp:
        src = Path(tmp) / 'check.cpp'
        exe = Path(tmp) / ('check.exe' if os.name == 'nt' else 'check')
        src.write_text(text, encoding='utf-8')
        subprocess.run([a.cxx, '-std=c++20', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-Wno-unused-parameter',
                        str(src), '-o', str(exe)], check=True, timeout=180)
        r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
        if r.returncode != 0 or r.stdout.strip() != 'mesh log counter checks passed':
            raise SystemExit('FAIL:\n%s%s' % (r.stdout, r.stderr))
    print('mesh log counter checks passed')


if __name__ == '__main__':
    main()
