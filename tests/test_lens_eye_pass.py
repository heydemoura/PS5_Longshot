"""The Lens of Truth eye pass (gpu/vk/eden/shader_recompiler/ir_opt/lens_eye_pass.h) on synthetic IR: the eye's edge takes the
picture's shape over 1.4 from the camera bank's projection and the program's other 16:9s that shape, the game's constants at 16:9 and with no sane projection, the eye 1.4
half heights wide at every shape, its ring's hole narrowed with it, its glow's x held at 16:9's side; the shapes it must not take are left as they are (tests/lens_eye_ir.cpp). And the wiring: every
graphics fragment translation runs it.

python -I -B tests/test_lens_eye_pass.py [--cxx clang++]
"""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import concurrent.futures
import os
import re
import subprocess
import tempfile
from pathlib import Path

RUN_ALL_ARGS = ["--cxx", "{cxx}"]

ROOT = Path(__file__).resolve().parents[1]
VK = ROOT / "gpu" / "vk"
ENV_ON_H = str(VK / "compat" / "longshot_env.h")


def build_and_run(cxx):
    sources = sorted((VK / "eden" / "shader_recompiler" / "frontend" / "ir").glob("*.cpp"))
    flags = ["-std=c++20", "-include", ENV_ON_H, "-O1", "-D_CRT_SECURE_NO_WARNINGS", "-DMKWVK_INCLUDES=2"]
    for header in ("algorithm", "cstdio", "cstdlib", "cstring", "functional", "string", "vector"):
        flags += ["-include", header]
    flags += ["-I" + str(p) for p in (VK / "compat", ROOT / "gpu" / "compat", VK / "eden", ROOT / "gpu" / "third_party" / "suyu")]
    with tempfile.TemporaryDirectory(prefix="lens-eye-") as temp:
        folder = Path(temp)
        sources = [ROOT / "tests" / "lens_eye_ir.cpp", *sources]
        objs = [folder / ("%03d.o" % i) for i in range(len(sources))]
        with concurrent.futures.ThreadPoolExecutor(os.cpu_count() or 2) as pool:
            list(pool.map(lambda job: subprocess.run([cxx, *flags, "-c", str(job[0]), "-o", str(job[1])], check=True),
                          zip(sources, objs)))
        exe = folder / "fixture.exe"
        subprocess.run([cxx, *map(str, objs), "-o", str(exe)], check=True)
        run = subprocess.run([str(exe)], capture_output=True, text=True)
        print(run.stdout, end="")
        print(run.stderr, end="")
        if run.returncode != 0 or "PASS" not in run.stdout:
            raise SystemExit("FAIL: the Lens of Truth eye pass on synthetic IR")


def wiring():
    shader = (VK / "vk_shader.cpp").read_text(encoding="utf-8")
    translate = re.search(r"std::unique_ptr<Translation> translate\(.*?\n}\n", shader, re.S)
    if not translate or "LensEyePass(t->program)" not in translate.group(0):
        raise SystemExit("FAIL: translate does not run the Lens of Truth eye pass")
    if "graphics && t->program.stage == Shader::Stage::Fragment" not in translate.group(0):
        raise SystemExit("FAIL: the eye pass not limited to graphics fragment programs")
    if not re.search(r"if \(fs && fs->lens_eye\) \{[^}]*LensEyeVertexPass\(vs->program, &swirl\)", shader):
        raise SystemExit("FAIL: the eye's vertex program not given its ring with the eye")
    print("wiring: every graphics fragment translation runs the eye pass, and the eye's vertex program its ring")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cxx", default="clang++")
    a = p.parse_args()
    wiring()
    build_and_run(a.cxx)
    print("PASS: the Lens of Truth eye pass")


if __name__ == "__main__":
    main()
