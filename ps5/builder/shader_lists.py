#!/usr/bin/env python3
"""The two shader lists a PS5 title folder carries, so its first start can build the game's pipelines before play
(gpu/vk/vk_draw.cpp prepare_run, precompile_run) instead of compiling each the first time it is drawn.

    python -I ps5/builder/shader_lists.py --builder <longshot-builder> --src <folder with assets/> --game <game.nup | dump folder>
                                          --work <dir> --out <title folder> [--jobs N]

Both are made by upstream's builder (`longshot-builder dev console-lists`, builder/crates/longshot-builder/src/dev):

- longshot_shaders.bin: every shader program in the player's own game (build-assets' census), read from the .nup in place
  through its romfs index (`longshot-builder index`, into work/index: a few MB, once per .nup), or from an unpacked dump's
  romfs folder. It holds game code: it is made on the player's machine from their game, never shipped or committed.
- longshot_pipeline_states_shipped.bin: the pipeline state list of assets/pipeline_states/ (no game code: hashes, formats and
  draw state), merged as a release's, less the lists converted from another emulator's records (TRIMMED): those were never
  drawn by Longshot itself, none of their records turned up in any recorded list, the PS5's own play included (2026-11-19:
  0 of 13,446), and they would add a fifth to the first start's work on the console.

Why on the PS5: a pipeline the driver compiles in play leaves its compile's memory in the title heap, freed but kept (a cold
driver cache: 906 MiB of heap mapped for 489 in use, against 570 for 485 warm, in the same Kokiri walk on a base console), and
the base console then runs out entering a new area (Castle Town, issues #4 and #24). Built before play and in a launch of its
own, which ends and gives that memory back, play then finds every listed pipeline in the driver's cache.
"""
import argparse
import os
import subprocess
import sys

# contributed lists left out of the PS5's list (above): converted, not recorded
TRIMMED = ("pmwork-personal-fritchii-recipe",)
SEED = "longshot_shaders.bin"
LIST = "longshot_pipeline_states_shipped.bin"


def say(text):
    print(text, flush=True)


def make(builder, src, game, work, out, jobs=1):
    """Both lists into out. game: the .nup, or a dump's folder with romfs/"""
    if os.path.isfile(game):
        index = os.path.join(work, "index")
        stamp = "%s %d %d" % (os.path.abspath(game), os.path.getsize(game), int(os.path.getmtime(game)))
        mark = os.path.join(index, ".from")
        if not (os.path.exists(mark) and open(mark).read() == stamp):
            os.makedirs(index, exist_ok=True)
            subprocess.run([builder, "index", "--game", game, "--out", index], check=True, stdout=subprocess.DEVNULL)
            with open(mark, "w") as f:
                f.write(stamp)
        source = ["--index", index]
    else:
        romfs = os.path.join(game, "romfs")
        if not os.path.isdir(romfs):
            say("note: no romfs/ in %s, so no shader lists: the game compiles its shaders as it plays" % game)
            return
        source = ["--romfs", romfs]
    r = subprocess.run([builder, "dev", "console-lists"] + source + ["--repo", src, "--out", out, "--leave-out", ",".join(TRIMMED),
                                                                    "--jobs", str(jobs)], capture_output=True, text=True)
    for line in (r.stdout + r.stderr).splitlines():
        say(line)
    if r.returncode:
        sys.exit(r.returncode)
    for name in (SEED, LIST):
        if not os.path.exists(os.path.join(out, name)):
            sys.exit("shader lists: %s was not made" % name)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--builder", required=True, help="upstream's builder, built for this PC (longshot-builder)")
    ap.add_argument("--src", required=True, help="the folder with assets/ (the repository, or the kit's src)")
    ap.add_argument("--game", required=True, help="the .nup, or an unpacked dump's folder")
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True, help="the title folder")
    ap.add_argument("--jobs", type=int, default=1)
    a = ap.parse_args()
    make(a.builder, os.path.abspath(a.src), os.path.abspath(a.game), os.path.abspath(a.work), os.path.abspath(a.out), a.jobs)


if __name__ == "__main__":
    sys.exit(main())
