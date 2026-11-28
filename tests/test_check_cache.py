"""The checks' compile cache (tests/check_cache.py): the same compile twice is a hit that gives the same program, a changed
header is a miss, a failed compile is not kept, path-independent sources in a new temporary folder still hit, what it can't key
goes to the compiler untouched, and a check started through check_cache_boot.py has its compiler calls cached.

    python -I -B tests/test_check_cache.py [--cxx clang++]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

RUN_ALL_ARGS = ["--cxx", "{cxx}"]

HERE = os.path.dirname(os.path.abspath(__file__))
CXX = ["clang++"]
PROGRAM = '#include "value.h"\n#include <cassert>\n#include <cstdio>\nint main(){assert(VALUE>0);std::printf("%d\\n",VALUE);}\n'


class CompileCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="check-cache-test-")
        self.dir = self.tmp.name
        self.cache = os.path.join(self.dir, "cache")
        self.stats = os.path.join(self.dir, "stats.txt")
        self.src = self.folder("src")
        self.write(self.src, "value.h", "#define VALUE 7\n")
        self.write(self.src, "main.cpp", PROGRAM)

    def tearDown(self):
        self.tmp.cleanup()

    def folder(self, name):
        path = os.path.join(self.dir, name)
        os.makedirs(path, exist_ok=True)
        return path

    def write(self, folder, name, text):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write(text)

    def compile(self, *args, src=None, out="a.exe"):
        """Through the cache: (exit code, output, the program's path)"""
        src = src or self.src
        exe = os.path.join(self.folder("out"), out)
        r = subprocess.run([sys.executable, "-I", "-B", os.path.join(HERE, "check_cache.py"), "--cache", self.cache,
                            "--stats", self.stats, "--", CXX[0], "-std=c++17", *args, os.path.join(src, "main.cpp"),
                            "-o", exe], cwd=self.dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return r.returncode, r.stdout.decode("utf-8", "replace"), exe

    def run_program(self, exe):
        return subprocess.run([exe], stdout=subprocess.PIPE, check=True).stdout.decode().strip()

    def words(self):
        with open(self.stats, encoding="ascii") as f:
            return f.read().split()

    def test_same_compile_hits(self):
        self.assertEqual(self.compile()[0], 0)
        os.remove(os.path.join(self.dir, "out", "a.exe"))
        code, _, exe = self.compile()
        self.assertEqual(code, 0)
        self.assertEqual(self.words(), ["miss", "hit"])
        self.assertEqual(self.run_program(exe), "7")

    def test_program_named_without_extension_hits(self):
        # (`-o fixture` on Windows writes fixture.exe: the cache keeps and gives back that file)
        self.compile(out="fixture")
        made = os.path.join(self.dir, "out", "fixture" + (".exe" if os.name == "nt" else ""))
        os.remove(made)
        self.assertEqual(self.compile(out="fixture")[0], 0)
        self.assertEqual(self.words(), ["miss", "hit"])
        self.assertEqual(self.run_program(made), "7")

    def test_changed_header_misses(self):
        self.compile()
        self.write(self.src, "value.h", "#define VALUE 8\n")
        code, _, exe = self.compile()
        self.assertEqual(code, 0)
        self.assertEqual(self.words(), ["miss", "miss"])
        self.assertEqual(self.run_program(exe), "8")

    def test_changed_flag_misses(self):
        self.compile()
        self.compile("-DEXTRA=1")
        self.assertEqual(self.words(), ["miss", "miss"])

    def test_source_in_a_new_folder_hits(self):
        self.write(self.src, "main.cpp", 'int main(){return 0;}\n')
        self.compile()
        other = self.folder("elsewhere")
        self.write(other, "main.cpp", 'int main(){return 0;}\n')
        code, _, exe = self.compile(src=other, out="b.exe")
        self.assertEqual(code, 0)
        self.assertEqual(self.words(), ["miss", "hit"])
        self.assertEqual(self.run_program(exe), "")

    def test_source_path_is_program_data(self):
        self.write(self.src, "main.cpp", '#include <cstdio>\nint main(){std::puts(__FILE__);}\n')
        code, _, exe = self.compile()
        self.assertEqual(code, 0)
        self.assertEqual(self.run_program(exe), os.path.join(self.src, "main.cpp"))
        other = self.folder("elsewhere")
        self.write(other, "main.cpp", '#include <cstdio>\nint main(){std::puts("<src>");}\n')
        code, _, exe = self.compile(src=other)
        self.assertEqual(code, 0)
        self.assertEqual(self.run_program(exe), "<src>")
        self.assertEqual(self.words(), ["miss", "miss"])

    def test_preprocessor_warning_cannot_hide_failure(self):
        self.write(self.src, "main.cpp", 'int main(){return 0;}\n// comment\n')
        self.assertEqual(self.compile("-Werror")[0], 0)
        self.write(self.src, "main.cpp", 'int main(){return 0;}\n#warning regression\n')
        code, output, _ = self.compile("-Werror")
        self.assertNotEqual(code, 0)
        self.assertIn("regression", output)
        self.assertEqual(self.words(), ["miss", "pass"])

    def test_compiler_aliases_have_distinct_keys(self):
        sys.path.insert(0, HERE)
        try:
            import check_cache
        finally:
            sys.path.pop(0)
        compiler = shutil.which(CXX[0])
        if not compiler or os.name == "nt":
            self.skipTest("compiler symlinks unavailable")
        aliases = [os.path.join(self.dir, name) for name in ("x86_64-pc-linux-gnu-clang++",
                                                              "x86_64-unknown-linux-gnu-clang++")]
        for alias in aliases:
            os.symlink(os.path.abspath(compiler), alias)
        keys = [check_cache.key_of([alias, os.path.join(self.src, "main.cpp"), "-o", "out.exe"], self.dir)
                for alias in aliases]
        self.assertTrue(all(keys))
        self.assertNotEqual(keys[0][0], keys[1][0])
        for alias in (aliases[0], aliases[1], aliases[1]):
            cmd = [sys.executable, "-I", "-B", os.path.join(HERE, "check_cache.py"), "--cache", self.cache,
                   "--stats", self.stats, "--", alias, os.path.join(self.src, "main.cpp"), "-o",
                   os.path.join(self.dir, "program")]
            self.assertEqual(subprocess.run(cmd, cwd=self.dir).returncode, 0)
        self.assertEqual(self.words(), ["miss", "miss", "hit"])

    def test_named_library_passes_through_after_removal(self):
        if os.name == "nt" or not shutil.which("ar"):
            self.skipTest("requires ar and a Unix linker")
        self.write(self.src, "main.cpp", 'extern "C" int foo();\nint main(){return foo();}\n')
        self.write(self.dir, "foo.cpp", 'extern "C" int foo(){return 0;}\n')
        obj = os.path.join(self.dir, "foo.o")
        library = os.path.join(self.dir, "libfoo.a")
        subprocess.run([CXX[0], "-c", os.path.join(self.dir, "foo.cpp"), "-o", obj], check=True)
        subprocess.run(["ar", "rcs", library, obj], check=True)
        env = dict(os.environ, LIBRARY_PATH=self.dir)
        cmd = [sys.executable, "-I", "-B", os.path.join(HERE, "check_cache.py"), "--cache", self.cache,
               "--stats", self.stats, "--", CXX[0], os.path.join(self.src, "main.cpp"), "-lfoo", "-o",
               os.path.join(self.dir, "program")]
        self.assertEqual(subprocess.run(cmd, env=env, cwd=self.dir).returncode, 0)
        os.remove(library)
        self.assertNotEqual(subprocess.run(cmd, env=env, cwd=self.dir).returncode, 0)
        self.assertEqual(self.words(), ["pass", "pass"])

    def test_prune_only_owned_entries_and_handles_unreadable_dirs(self):
        sys.path.insert(0, HERE)
        try:
            import check_cache
        finally:
            sys.path.pop(0)
        check_cache.FOLDER[0] = self.cache
        self.addCleanup(lambda: check_cache.FOLDER.__setitem__(0, None))
        unrelated = self.folder("cache/other/old")
        owned = self.folder("cache/" + check_cache.NAMESPACE + "/aa/" + "a" * 64)
        self.write(owned, "meta.json", json.dumps({"key": "a" * 64, "version": check_cache.VERSION}))
        old = time.time() - 20 * 86400
        os.utime(unrelated, (old, old))
        os.utime(owned, (old, old))
        check_cache.prune()
        self.assertTrue(os.path.isdir(unrelated))
        self.assertFalse(os.path.exists(owned))
        with mock.patch.object(check_cache.os, "listdir", side_effect=PermissionError):
            self.assertEqual(check_cache.prune(), (0, 0))
        base = os.path.join(self.cache, check_cache.NAMESPACE)
        original = os.listdir
        with mock.patch.object(check_cache.os, "listdir", side_effect=lambda p: original(p) if p == base else
                               (_ for _ in ()).throw(PermissionError())):
            self.assertEqual(check_cache.prune(), (0, 0))

    def test_failed_compile_is_not_kept(self):
        self.write(self.src, "value.h", "#define VALUE (\n")
        for _ in range(2):
            code, out, _ = self.compile()
            self.assertNotEqual(code, 0)
            self.assertIn("error", out)
        self.assertNotIn("hit", self.words())
        self.assertFalse(any(os.scandir(self.cache)) if os.path.isdir(self.cache) else False)

    def test_warnings_replayed_on_a_hit(self):
        self.write(self.src, "main.cpp", PROGRAM.replace("int main(){", "int main(){int unused;"))
        first = self.compile("-Wall")[1]
        second = self.compile("-Wall")[1]
        self.assertIn("unused", first)
        self.assertEqual(first, second)
        self.assertEqual(self.words(), ["miss", "hit"])

    def test_what_it_cannot_key_passes_through(self):
        for flags in (["-xc++"], ["-L" + self.dir], ["-MD"], ["-save-temps"]):
            self.compile(*flags)
        self.assertEqual(self.words(), ["pass"] * 4)

    def test_turned_off(self):
        env = dict(os.environ, LONGSHOT_CHECK_CACHE="0", LONGSHOT_CHECK_CACHE_STATS=self.stats)
        sys.path.insert(0, HERE)
        try:
            import check_cache
        finally:
            sys.path.pop(0)
        r = subprocess.run([sys.executable, "-I", "-B", "-c", "import sys; sys.path.insert(0, %r); import check_cache;"
                            " print(check_cache.root())" % HERE], env=env, stdout=subprocess.PIPE)
        self.assertEqual(r.stdout.decode().strip(), "None")
        self.assertTrue(check_cache.is_compiler(CXX[0]))

    def test_a_check_through_the_boot(self):
        # (what run_all.py does: the check's own subprocess call to the compiler goes through the cache)
        check = os.path.join(self.dir, "fake_check.py")
        with open(check, "w", encoding="utf-8") as f:
            f.write("import subprocess, sys\n"
                    "subprocess.run([sys.argv[1], '-std=c++17', sys.argv[2], '-o', sys.argv[3]], check=True)\n"
                    "print(subprocess.run([sys.argv[3]], stdout=subprocess.PIPE, check=True).stdout.decode().strip())\n")
        env = dict(os.environ, LONGSHOT_CHECK_CACHE=self.cache, LONGSHOT_CHECK_CACHE_STATS=self.stats)
        outs = []
        for name in ("one.exe", "two.exe"):
            r = subprocess.run([sys.executable, "-I", "-B", os.path.join(HERE, "check_cache_boot.py"), check, CXX[0],
                                os.path.join(self.src, "main.cpp"), os.path.join(self.folder("out"), name)],
                               env=env, cwd=self.dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertEqual(r.returncode, 0, r.stdout.decode("utf-8", "replace"))
            outs.append(r.stdout.decode().strip())
        self.assertEqual(outs, ["7", "7"])
        self.assertEqual(self.words(), ["miss", "hit"])


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cxx", default="clang++")
    a, rest = p.parse_known_args()
    CXX[0] = a.cxx
    unittest.main(argv=[sys.argv[0]] + rest)
