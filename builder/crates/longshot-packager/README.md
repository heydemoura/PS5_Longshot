# longshot-packager

What packages a release of `longshot-builder`: the release's build jobs
([MagicBeanAdmin/Release](https://git.magicbean.dev/MagicBeanAdmin/Release)'s `longshot.yml`) build it for their own
Linux runner and run it; it is never shipped. It is a program of its own, so that neither platform's job waits for the
other's builder, and the builder players run carries no packaging.

```
python -I scripts/longshot_builder.py packager    # build-linux/longshot-packager, as the release builds it
cargo run --release -p longshot-packager -- ...   # a developer's
```

## Main parts

```
longshot-packager tools check | install | add | make        (tools.lock)
longshot-packager release sources | unpack | attach | dry-run | last-objects | fetch-bases | deltas | files | recomplauncher
longshot-packager compile --work <dir> --third-party-objects <dir> | --dry-run
longshot-packager rust-key [--root <dir>]
```

Each takes `--output json`: the builder's JSON Lines events on stdout and nothing else, as `longshot-builder dev` says
them (build-core's [`report::console`](../build-core/src/report/console.rs)).

- [`src/main.rs`](src/main.rs): the commands. `tools` is build-driver's `tools::cli`, the code of
  `longshot-builder dev tools`; `rust-key` is build-compile's `rust::source_key`.
- [`src/release_cmd.rs`](src/release_cmd.rs): `release`, a release's packaging: `sources` (the pack the shipped program
  carries, by build-driver's `sources` writer: the same files give the same bytes), `unpack` (a pack into a plain
  folder, checked), `attach` (the pack appended to a linked program and read back), `dry-run <program>` (a player's
  build's dry run, with and without AMD FSR 4, checked), `recomplauncher <full file>` (RecompLauncher's zip: the program
  as `Longshot.exe`, made a windowed program, or `Longshot.x86_64`, stored, the same files giving the same bytes);
- [`src/release_objects.rs`](src/release_objects.rs): `last-objects`, the last signed release's third-party objects and
  pack;
- [`src/release_deltas.rs`](src/release_deltas.rs): `fetch-bases` (the channel's last 5 releases' full files, from the
  release API by curl, each checked against its manifest), `deltas` (`zstd --patch-from` from each, checked to give the
  full file back, and the manifest), `files` (a release's stamps, bases, deltas and manifest together);
- [`src/compile.rs`](src/compile.rs): the two game-free compiles of `longshot-builder compile`, through
  `build_driver::build::compile_only`: `--third-party-objects` (the release's prebuilt set) and `--dry-run` (for
  `release dry-run` over a tree whose own builder can't run on the runner: Windows').

## Used by / uses

Used by nothing in the workspace (it is a program). Uses `build-driver`, `build-compile` (`rust`), `build-core`,
`release` (the signature's keys, the windowed program), serde and serde_json (the manifest) and crc32fast (the zip).

## Rules it keeps

- **It holds no copy of the builder's logic**: keys, the pack, the deltas and the manifest are the libraries' and its
  own, built from the same commit as the program it packages.
- **It is never shipped**, and packages with nothing made from a game.

## Tests

```
cargo test -p longshot-packager
```

See [releases](../../../docs/dev/reference/releases.md#the-release-workflow).
