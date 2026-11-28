"""scripts/sl_sdk.py: NVIDIA Streamline's file table, the release zip's and each file's SHA-256 checks, the runtime beside
longshot.exe and NVIDIA's license gate, without the network (a small zip of the same layout stands in for NVIDIA's).

Run with: python -I tests/test_sl_sdk.py
"""
import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sl_sdk_under_test", ROOT / "scripts" / "sl_sdk.py")
sl_sdk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sl_sdk)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def archive(contents):
    """A zip of path -> bytes, as NVIDIA's release zip lays its files out"""
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for path, data in contents.items():
            z.writestr(path, data)
    return b.getvalue()


class Opener:
    """urlopen over one zip; records what was asked for"""
    def __init__(self, data):
        self.data, self.asked = data, []

    def __call__(self, url, timeout=None):
        if timeout != sl_sdk.TIMEOUT:
            raise AssertionError("urlopen without the timeout (%r)" % (timeout,))
        self.asked.append(url)
        return io.BytesIO(self.data)


class TableTests(unittest.TestCase):
    def test_pinned_release(self):
        self.assertRegex(sl_sdk.VERSION, r"^\d+\.\d+\.\d+$")
        self.assertEqual(sl_sdk.ZIP, "streamline-sdk-v%s.zip" % sl_sdk.VERSION)
        self.assertEqual(sl_sdk.URL, "https://github.com/NVIDIA-RTX/Streamline/releases/download/v%s/%s"
                         % (sl_sdk.VERSION, sl_sdk.ZIP))
        self.assertRegex(sl_sdk.ZIP_SHA256, r"^[0-9a-f]{64}$")

    def test_every_file_has_a_sha256_and_one_place(self):
        table = sl_sdk.files()
        for path, sha in table:
            self.assertRegex(sha, r"^[0-9a-f]{64}$", path)
            self.assertFalse(path.startswith("/") or ".." in path.split("/"), path)
        self.assertEqual(len({p.lower() for p, _ in table}), len(table))

    def test_the_licenses_come_with_every_fetch(self):
        build = {p for p, _ in sl_sdk.files(runtime=False)}
        for license in ("license.txt", "bin/x64/nvngx_dlss.license.txt", "bin/x64/reflex.license.txt"):
            self.assertIn(license, build)   # (with --build-only too: they govern the headers as much as the runtime)

    def test_build_only_leaves_out_the_runtime(self):
        full = {p for p, _ in sl_sdk.files()}
        build = {p for p, _ in sl_sdk.files(runtime=False)}
        self.assertEqual(full - build, {p for p, _ in sl_sdk.RUNTIME})
        self.assertFalse([p for p in build if p.endswith(".dll")])

    def test_the_headers_vk_dlssg_includes_are_there(self):
        source = (ROOT / "gpu" / "vk" / "vk_dlssg.cpp").read_text(encoding="utf-8")
        included = set(re.findall(r'#include "(sl[a-z_]*\.h)"', source))
        self.assertTrue(included >= {"sl.h", "sl_dlss_g.h", "sl_reflex.h", "sl_pcl.h", "sl_security.h"})
        build = {p for p, _ in sl_sdk.files(runtime=False)}
        for header in included:
            self.assertIn("include/" + header, build)

    def test_the_runtime_is_what_vk_dlssg_loads(self):
        names = sl_sdk.runtime_names()
        self.assertEqual(sorted(names), sorted(["sl.interposer.dll", "sl.common.dll", "sl.dlss_g.dll", "sl.reflex.dll",
                                                "sl.pcl.dll", "nvngx_dlssg.dll", "NvLowLatencyVk.dll"]))
        source = (ROOT / "gpu" / "vk" / "vk_dlssg.cpp").read_text(encoding="utf-8")
        for name in names:
            self.assertIn(name, source)   # (its own comment lists what it loads from beside longshot.exe)
        # (only the plugins it loads: not DLSS Super Resolution's, which is NGX's own runtime in the game's ngx folder)
        self.assertNotIn("nvngx_dlss.dll", names)
        self.assertNotIn("sl.dlss.dll", names)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="longshot-sl-sdk-")
        self.addCleanup(self.temp.cleanup)
        self.sdk = os.path.join(self.temp.name, "sdk")
        self.good = {"license.txt": b"license", "include/sl.h": b"header", "bin/x64/sl.interposer.dll": b"interposer",
                     "bin/x64/nvngx_dlssg.dll": b"dlssg", "docs/other.md": b"not taken"}
        self.build = [(p, digest(self.good[p])) for p in ("license.txt", "include/sl.h")]
        self.runtime = [(p, digest(self.good[p])) for p in ("bin/x64/sl.interposer.dll", "bin/x64/nvngx_dlssg.dll")]
        self.zip = archive(self.good)
        for patch in (mock.patch.object(sl_sdk, "BUILD", self.build), mock.patch.object(sl_sdk, "RUNTIME", self.runtime),
                      mock.patch.object(sl_sdk, "ZIP_SHA256", digest(self.zip))):
            patch.start()
            self.addCleanup(patch.stop)

    def test_fetch_takes_the_table_out_of_the_zip_and_deletes_it(self):
        opener = Opener(self.zip)
        self.assertTrue(sl_sdk.fetch(self.sdk, opener, say=lambda *_: None))
        self.assertEqual(opener.asked, [sl_sdk.URL])
        self.assertEqual(Path(self.sdk, "bin", "x64", "sl.interposer.dll").read_bytes(), b"interposer")
        self.assertTrue(sl_sdk.headers(self.sdk))
        self.assertEqual(sorted(os.listdir(self.sdk)), ["bin", "include", "license.txt"])   # (no zip, nothing else of it)
        self.assertFalse(sl_sdk.fetch(self.sdk, opener, say=lambda *_: None))   # (all there and right: nothing downloaded)
        self.assertEqual(len(opener.asked), 1)

    def test_build_only_takes_no_runtime(self):
        sl_sdk.fetch(self.sdk, Opener(self.zip), say=lambda *_: None, runtime=False)
        self.assertTrue(sl_sdk.headers(self.sdk))
        self.assertFalse(os.path.exists(os.path.join(self.sdk, "bin")))

    def test_a_wrong_zip_is_not_kept(self):
        bad = archive(dict(self.good, **{"include/sl.h": b"not nvidia's"}))
        with self.assertRaisesRegex(ValueError, "%s is not the file expected" % re.escape(sl_sdk.ZIP)):
            sl_sdk.fetch(self.sdk, Opener(bad), say=lambda *_: None)
        self.assertEqual(os.listdir(self.sdk), [])   # (neither the zip nor its .part, nor anything out of it)

    def test_a_wrong_file_in_the_zip_is_not_kept(self):
        bad = archive(dict(self.good, **{"bin/x64/nvngx_dlssg.dll": b"not nvidia's"}))
        with mock.patch.object(sl_sdk, "ZIP_SHA256", digest(bad)):
            with self.assertRaisesRegex(ValueError, "nvngx_dlssg.dll is not the file expected"):
                sl_sdk.fetch(self.sdk, Opener(bad), say=lambda *_: None)
        dll = Path(self.sdk, "bin", "x64", "nvngx_dlssg.dll")
        self.assertFalse(dll.exists())
        self.assertFalse(Path(str(dll) + ".part").exists())
        self.assertFalse(Path(self.sdk, sl_sdk.ZIP).exists())

    def test_a_file_missing_from_the_zip_is_said(self):
        short = {p: d for p, d in self.good.items() if p != "include/sl.h"}
        data = archive(short)
        with mock.patch.object(sl_sdk, "ZIP_SHA256", digest(data)):
            with self.assertRaisesRegex(ValueError, "include/sl.h is not in"):
                sl_sdk.fetch(self.sdk, Opener(data), say=lambda *_: None)

    def test_a_wrong_file_there_is_fetched_again(self):
        header = Path(self.sdk, "include", "sl.h")
        sl_sdk.fetch(self.sdk, Opener(self.zip), say=lambda *_: None)
        header.write_bytes(b"old")
        self.assertEqual(sl_sdk.missing(self.sdk), [("include/sl.h", digest(b"header"))])
        self.assertTrue(sl_sdk.fetch(self.sdk, Opener(self.zip), say=lambda *_: None))
        self.assertEqual(header.read_bytes(), b"header")

    def test_runtime_beside_the_exe(self):
        sl_sdk.fetch(self.sdk, Opener(self.zip), say=lambda *_: None)
        game = Path(self.temp.name, "game")
        (game / "bin").mkdir(parents=True)
        self.assertEqual(Path(sl_sdk.beside_exe(str(game))), game / "bin")
        placed = sl_sdk.install_runtime(self.sdk, sl_sdk.beside_exe(str(game)))
        self.assertEqual(sorted(Path(p).name for p in placed), ["nvngx_dlssg.dll", "sl.interposer.dll"])
        self.assertEqual((game / "bin" / "sl.interposer.dll").read_bytes(), b"interposer")
        self.assertEqual(os.listdir(game), ["bin"])   # (beside longshot.exe only)
        flat = Path(self.temp.name, "flat")   # (a development build: longshot.exe in the folder itself)
        flat.mkdir()
        self.assertEqual(Path(sl_sdk.beside_exe(str(flat))), flat)

    def test_a_runtime_file_of_another_release_there_is_left_alone(self):
        # (a player's own nvngx_dlssg.dll, or another release's: neither replaced nor joined by this release's files, since
        # Streamline's interposer and plugins go together; what is kept is said, and deleting it gets this release's)
        sl_sdk.fetch(self.sdk, Opener(self.zip), say=lambda *_: None)
        game = Path(self.temp.name, "game")
        game.mkdir()
        (game / "nvngx_dlssg.dll").write_bytes(b"the player's")
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            placed = sl_sdk.install_runtime(self.sdk, str(game))
        self.assertEqual((game / "nvngx_dlssg.dll").read_bytes(), b"the player's")
        self.assertEqual(sorted(os.listdir(game)), ["nvngx_dlssg.dll"])
        self.assertEqual(placed, [])
        self.assertIn("nvngx_dlssg.dll", said.getvalue())
        self.assertIn(sl_sdk.VERSION, said.getvalue())
        # (this release's own file there already is no other: the rest is copied beside it, and nothing is said)
        (game / "nvngx_dlssg.dll").write_bytes(Path(self.sdk, "bin", "x64", "nvngx_dlssg.dll").read_bytes())
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            placed = sl_sdk.install_runtime(self.sdk, str(game))
        self.assertEqual(sorted(Path(p).name for p in placed), ["nvngx_dlssg.dll", "sl.interposer.dll"])
        self.assertEqual(said.getvalue(), "")


class LicenseTests(unittest.TestCase):
    def test_notice_names_nvidia_the_release_and_the_licenses(self):
        self.assertIn("NVIDIA", sl_sdk.NOTICE)
        self.assertIn(sl_sdk.RELEASE_URL, sl_sdk.NOTICE)
        for license in ("license.txt", "nvngx_dlss.license.txt", "reflex.license.txt"):
            self.assertIn(license, sl_sdk.NOTICE)
        self.assertIn("Never share", sl_sdk.NOTICE)

    def test_flag_or_a_yes(self):
        self.assertTrue(sl_sdk.accepted(True, ask=lambda _: self.fail("asked"), interactive=False))
        self.assertFalse(sl_sdk.accepted(False, ask=lambda _: self.fail("asked"), interactive=False))
        self.assertTrue(sl_sdk.accepted(False, ask=lambda _: "yes", interactive=True))
        self.assertFalse(sl_sdk.accepted(False, ask=lambda _: "", interactive=True))

    def test_nothing_downloaded_without_acceptance(self):
        with mock.patch.object(sl_sdk, "fetch", side_effect=AssertionError("fetched")), \
                mock.patch.object(sl_sdk, "accepted", return_value=False), \
                mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(sl_sdk.main(["x"]), 2)
        self.assertIn("not accepted", err.getvalue())


class FolderTests(unittest.TestCase):
    """Where the SDK goes: the folder named, else the caller's default (scripts/fetch.py streamline: the development
    folder's sdk/sl), else work/sl-sdk in the checkout"""

    def run_main(self, argv, **kw):
        got = []
        with mock.patch.object(sl_sdk, "fetch", side_effect=lambda sdk, **k: got.append((sdk, k.get("runtime")))), \
                mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(sl_sdk.main(["--accept-nvidia-license"] + argv, **kw), 0)
        return got, out.getvalue()

    def test_the_folder_named_first(self):
        with tempfile.TemporaryDirectory(prefix="longshot-sl-sdk-") as tmp:
            named, default = os.path.join(tmp, "named"), os.path.join(tmp, "default")
            self.assertEqual(self.run_main([named], default=default)[0], [(os.path.abspath(named), True)])
            got, out = self.run_main([], default=default)
            self.assertEqual(got, [(os.path.abspath(default), True)])
            self.assertIn("LONGSHOT_SL_SDK=%s" % os.path.abspath(default), out)
            self.assertEqual(self.run_main(["--build-only"], default=default)[0], [(os.path.abspath(default), False)])
        own = os.path.abspath(os.path.join(sl_sdk.ROOT, "work", "sl-sdk"))
        self.assertEqual(self.run_main([])[0], [(own, True)])

    def test_fetch_py_streamline_hands_over_with_the_development_folder(self):
        spec = importlib.util.spec_from_file_location("fetch_under_test", ROOT / "scripts" / "fetch.py")
        fetch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fetch)
        seen = []
        fake = SimpleNamespace(main=lambda argv, default=None: seen.append((argv, default)) or 0)
        fake_spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda m: None))
        with mock.patch.object(importlib.util, "module_from_spec", return_value=fake), \
                mock.patch.object(importlib.util, "spec_from_file_location", return_value=fake_spec) as located, \
                mock.patch.object(fetch, "dev_sdk", side_effect=lambda name: "dev/sdk/" + name):
            self.assertEqual(fetch.main(["streamline", "--build-only"]), 0)
        self.assertEqual(Path(located.call_args[0][1]), ROOT / "scripts" / "sl_sdk.py")
        self.assertEqual(seen, [(["--build-only"], "dev/sdk/sl")])


if __name__ == "__main__":
    unittest.main()
