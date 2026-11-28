# CI

CI is Forgejo Actions (`.forgejo/workflows/`): it runs the checks; [MagicBeanAdmin/Release](https://git.magicbean.dev/MagicBeanAdmin/Release) publishes the releases and
deploys the website. No check needs the game: every one is game-free.

**Run CI's checks yourself:** `mise run ci` runs what CI checks on every change: the pinned test tools (`test-deps`),
every game-free check (`tests/run_all.py --strict`, the same line CI runs in parts) and the website's build and checks
(`scripts/website_check.sh`). `mise run test` runs the checks alone, and takes `run_all.py`'s arguments; `mise run lint`
runs pre-commit and actionlint as the `lint` job does. `tests/run_all.py --container` runs the checks in the CI
image itself ([below](#the-ci-images)).

## Forgejo Actions

Nothing that publishes a file or a package is made here: [MagicBeanAdmin/Release](https://git.magicbean.dev/MagicBeanAdmin/Release), on runners registered to
that repository alone, builds, signs and publishes the releases ([`longshot.yml`](https://git.magicbean.dev/MagicBeanAdmin/Release/src/branch/main/.forgejo/workflows/longshot.yml),
[releases](releases.md#the-release-workflow)), pushes [the CI images](#the-ci-images) ([`longshot-ci-image.yml`](https://git.magicbean.dev/MagicBeanAdmin/Release/src/branch/main/.forgejo/workflows/longshot-ci-image.yml)) uploads the build
tools ([`longshot-tools.yml`](https://git.magicbean.dev/MagicBeanAdmin/Release/src/branch/main/.forgejo/workflows/longshot-tools.yml), [bumping a tool](releases.md#bumping-a-tool)) and deploys the website
([`longshot-website.yml`](https://git.magicbean.dev/MagicBeanAdmin/Release/src/branch/main/.forgejo/workflows/longshot-website.yml), [its previews](#the-websites-preview)).

| Workflow | When | What it does |
|---|---|---|
| `ci.yml` | Pull request, push to `main` | `changes`: what the rest needs (the [CI images](#the-ci-images), and whether the compiling jobs run); `lint`: pre-commit on the changed files, ShellCheck on `scripts/` and actionlint; `third-party`: `scripts/check_third_party.py` and the guide screenshots' source review; `rust`: rustfmt, clippy, `cargo test` (with clang, lld and llvm-mc), then the synthetic game end to end from the same build; `host-windows`: the host's C for Windows with llvm-mingw; `renderer`: the renderer built alone (`longshot-builder compile --only renderer`, a developer's builder from cargo, tools.lock's compilers), for Linux and for Windows, each without and with AMD FSR 4; `tests`: `tests/run_all.py --strict` in 2 parts (1200 s a check; every part builds the developer `longshot-builder` first, for the site-hook checks, and runs the checks that declare `RUN_ALL_ALONE` (they measure time or CPU) one at a time after the others; the part whose checks run the release `longshot-builder` or `tests/test_lifter.py` builds those first; the shader recompiler's SPIR-V fixtures are three of its checks); `website`: the website's checks (`scripts/website_check.sh`), its `dist/` kept for the preview. On a pull request, `changes` skips `rust` and `host-windows` when [only docs change](#what-a-pull-request-runs), and `renderer` when no renderer file does |
| `pr-description.yml` | Pull request (opened, edited, pushed) | The title (a Conventional Commit) and the description against the template (`scripts/check_pr_description.py`), with main's script: no code of the pull request's runs. The title is here, not in `ci.yml`, because a title edit runs this file, and an edit's run of `ci.yml` would cancel the code's checks |
| `third-party.yml` | Nightly, by hand | The third-party parts compared with their upstream archives (`check_third_party.py --upstream`); the per-change check is `ci.yml`'s `third-party` |
| `tools-check.yml` | By hand (any branch) | Check tools: the build tools named made twice from their pins (`builder/tools/make.sh --check`) on a check runner, nothing uploaded ([bumping a tool](releases.md#bumping-a-tool)) |
| `vulkan-dlss.yml` | By hand | The Windows Vulkan back end built with NVIDIA's DLSS SDK, NVIDIA Streamline's SDK (DLSS Frame Generation) and AMD FSR 4 |
| `vulkan-fixtures.yml` | By hand | The authored Vulkan fixtures on Mesa's software driver |

### Runners

Every job runs on Linux; there is no Windows runner. Windows files are cross-compiled with llvm-mingw's Linux build and
started under Wine. The labels the workflows ask for, all on the image `docker://ghcr.io/catthehacker/ubuntu:act-24.04`:

| Label | For |
|---|---|
| `ubuntu-latest`, `ubuntu-24.04` | the checks. No secrets: a runner anyone in the team lends can carry these |
| `quick` | jobs of a few minutes (`changes`, `lint`, `third-party`, the title and description), on a runner kept for them so they never wait behind a build |

The server's runners share 2 cores (MagicBeanDev/Infra, `runner-dind`).

Register a runner with `forgejo-runner register --instance https://git.magicbean.dev --token <token> --labels ...` from
MagicBeanDev's *Settings > Actions > Runners*, and run `forgejo-runner daemon`. A runner lent by someone else gets
`ubuntu-latest` and `ubuntu-24.04` only.

### Caches

Each runner keeps its own cache (forgejo-runner's, on its disk): `ci.yml`'s jobs read it on every run and write it only
on a push to `main`. forgejo-runner 13 keeps a pull request's writes to that pull request (its write isolation key), so a
pull request can't put anything there that another run reads. The jobs that sign or publish a release read no cache at
all; the jobs that build a release read only [their own](#the-releases-cache).

| Cache (key) | What it holds | Who reads it |
|---|---|---|
| `git-base` | main's tree, a bare repository at depth 1 ([scripts/ci_git_base.sh](../../../scripts/ci_git_base.sh)) | `rust`, `host-windows`, `renderer`, `tests`, `website`: before `actions/checkout`, the workspace borrows its objects, so the fetch takes only what the commit adds (a lent runner fetched the whole tree, 26 MB, through Cloudflare in 2 to 4.5 minutes: now seconds). After the checkout every object is copied in and checked against its name (`git fsck`) |
| `rust-deps-<Rust>-<Cargo.lock>` | the registry crates (`/opt/cargo/registry`, the image's `CARGO_HOME`), downloaded and built, without the workspace's own: a checkout's new file times make cargo build those again whatever a cache holds ([scripts/ci_cargo_prune.py](../../../scripts/ci_cargo_prune.py)). One entry per lock, written when missing. When the lock has changed, the newest entry for the same Rust is restored instead, and cargo builds only the crates that changed | `rust`; `host-windows` and `renderer` read it |
| `<llvm-mingw>-archive`, `windows-tools-downloads-<tools.lock>` | the downloads, each checked by its size and SHA-256 when taken, written when missing | `host-windows`; `renderer` (Windows) reads `<llvm-mingw>-archive` |
| `renderer-tools-downloads-<system>-<tools.lock>` | tools.lock's archives the renderer compiles with (linux-x64's llvm, sysroot and CPM cache; windows-x64's CPM cache), each checked by its size and SHA-256 when taken, written when missing | `renderer` |
| `renderer-store-<system>-<renderer files>` | the renderer's compiled objects (`compile --store`), each by its key: its compiler, flags, source and headers (build-compile's store.rs), so an object of another tree is the one this compile would make. Written by a push to `main` whose renderer files have no entry yet, from an empty store, so an entry holds one tree's objects; a pull request restores the newest | `renderer` |
| `ccache-tests-part<K>of<N>-...` | ccache's objects, keyed by the compilers' versions and the shader recompiler's sources; the nearest older key is restored | `tests` (one per part) |
| `ccache-host-windows-<llvm-mingw>-<host/, game/, tools.lock>` | ccache's results of the host's `-fsyntax-only` compiles for Windows (`check_host_compile.py --launcher ccache`; `CCACHE_BASEDIR` the checkout and the work folder in it, the compiler checked by llvm-mingw's name, so the commands are the same every run: all 66 units hit on a second run, 12 s down to 2 s); the nearest older key is restored | `host-windows` |

The tools themselves are no cache: they are [the CI images](#the-ci-images). `ci.yml`'s jobs run with `CARGO_INCREMENTAL=0` and `CARGO_PROFILE_DEV_DEBUG=0`: incremental state is only ever
written in CI, and the dev builds link faster without debug info (a failing test still names its file and line).

### The CI images

Every `ci.yml` job that needs more than Python runs in one of two images (`container: image:`), so no job installs
anything:

| Image | Jobs | Parts (layers) | Size |
|---|---|---|---|
| `git.magicbean.dev/magicbeandev/longshot-ci-checks:<tag>` | `rust`, `host-windows`, `renderer`, `tests` | `system` (g++, clang, lld, llvm-mc, the sanitizer runtimes, spirv-tools, ccache, cmake, libdav1d: 943 MB), `python` (the tests' packages, pre-commit: 181 MB), `headers` (glslang 16.6.0, the SPIR-V and Vulkan headers: 71 MB), `node` (138 MB), `tools` (actionlint, ShellCheck, gitleaks: 37 MB), `rust` (mise.toml's Rust, minimal, rustfmt and clippy: 667 MB) | 2.1 GB |
| `git.magicbean.dev/magicbeandev/longshot-ci-lite:<tag>` | `lint`, `website` | `system-lite` (git, curl, Python: 128 MB), `python`, `node`, `tools`, `hooks` (pre-commit's hooks fetched: 63 MB) | 0.6 GB |

(Sizes unpacked, on the 78 MB Ubuntu base. `node` is in both: the actions run in it.)
[scripts/linux/ci_setup.sh](../../../scripts/linux/ci_setup.sh) installs every part, the one list, each removing its
own caches (apt's lists, docs and translations, pip's); [scripts/linux/checks.Dockerfile](../../../scripts/linux/checks.Dockerfile)
has a stage per image and a `RUN` per part on its pinned Ubuntu 24.04, and `tests/run_all.py --container` builds
`checks` locally (`scripts/linux/checks.sh`). Every download is pinned: the distribution's signed lists for apt,
mise.lock's files and SHA-256 for Node, actionlint, ShellCheck and gitleaks, [scripts/ci_rust.sh](../../../scripts/ci_rust.sh)'s
pinned rustup for Rust. gitleaks is mise.toml's prebuilt release, run by pre-commit's `gitleaks-system` hook: the
`golang` hook built it with a Go toolchain it downloaded, 300 MB.

- **A tag is its inputs' hash** ([scripts/linux/ci_image.sh](../../../scripts/linux/ci_image.sh) `tag NAME`: the
  image's name, the Dockerfile, `ci_setup.sh`, `ci_image.sh`, `ci_rust.sh`, `fetch_commit.sh`, `mise.toml`,
  `mise.lock`, `.pre-commit-config.yaml`). `ci.yml`'s `changes` job hashes the commit's inputs and asks the registry
  for both tags, anonymously (the organization is public: no credentials are needed to pull).
- **A change to the inputs checks itself without pushing.** Its tags are not in the registry yet, so its jobs run on
  the runners' own image (`ghcr.io/catthehacker/ubuntu:act-24.04`, Ubuntu 24.04 with Node) and run their image's
  parts first (`ci_setup.sh --github-env --image <name>`): the pull request's own tools, a few minutes more a job. A
  push to `main` of such a change does the same until the images are pushed.
- **Only main's images are pushed**, by MagicBeanAdmin/Release's [`longshot-ci-image.yml`](https://git.magicbean.dev/MagicBeanAdmin/Release/src/branch/main/.forgejo/workflows/longshot-ci-image.yml): after a push to `main` (and hourly) it builds the
  images whose tags the registry lacks, from `main`, and by hand it does the same or, with **publish** off, builds them
  all and pushes nothing. The runners give a job no Docker, so it is not `docker build`: `build` runs the image's parts
  in a job container of the Dockerfile's base and keeps each part's folders as a layer of its own, made the same way
  every time (sorted, no times, no owners), so a part whose files didn't change is the same layer and a runner pulls
  only the ones that did; `push`, which runs nothing of this repository's, stacks them on that base with crane
  (go-containerregistry, pinned by SHA-256), sets the images' environment (`ci_setup.sh env`), and pushes with Release's
  `PACKAGES_TOKEN` (`write:package`) over the LAN (Cloudflare cuts uploads at 100 MB). A pull request's code never runs
  there and gets no such token, and a tag in the registry is never pushed again: a pull request can't replace the
  images main's runs use. What it reads here: `ci_setup.sh` (`--image`, `parts`, `paths`, `env`), `ci_image.sh`
  (`tag`, `image`) and the Dockerfile's `FROM ubuntu:24.04@sha256:... AS base` line.

### The release's cache

The release builds ([`longshot.yml`](https://git.magicbean.dev/MagicBeanAdmin/Release/src/branch/main/.forgejo/workflows/longshot.yml)) keep two caches per platform on their own runners, written by no other
repository's jobs: the downloads (the pinned Rust's archives, the `.crate` files, tools.lock's archives, llvm-mingw),
each checked by its SHA-256 every time it is used, and cargo's target folder of the shipped builder's and
`longshot-packager`'s compiles, which is not checked again. Cargo takes a workspace crate's files' times for whether it changed, and a checkout makes every file
new, so the build records each tracked file's SHA-256 in the target folder (`scripts/ci_source_times.py record`) and
dates unchanged files back after a restore (`apply`).

### What a pull request runs

`ci.yml`'s `changes` job reads the pull request's changed files from the API ([scripts/ci_changes.py](../../../scripts/ci_changes.py))
and skips `rust` and `host-windows` when every one of them is in a place those jobs never read: `website/`,
`docs/` (but `docs/dev/reference/build-events.md`, which the builder's tests compare with its codes), the
other workflows' files and the top-level documents. The Rust workspace's tests and build scripts read `host/`, `gpu/`,
`game/`, `assets/`, `third_party/`, `scripts/`, `tests/` and `tools.lock`, so any other change runs them all. Anything
unsure runs them too: a push to `main`, a run by hand, an API that doesn't answer. `renderer` runs only when a changed
file is one `compile --only renderer` reads, or what builds it (`ci_changes.py`'s `RENDERER`: `gpu/`, the host and game
headers and the host files the renderer compiles, its third-party parts, its logo and profile, `builder/`, the Rust
pins, `tools.lock`, the CI image's tools, and its job's own files); anything unsure runs it too. `lint`, `third-party`, `tests` and
the website's checks run on every pull request (the checks and the site read files from all over the repository).

A skipped job is not a failed one: `main` requires no status checks, and Tooling's `forge.py pr status` counts
`skipped` as passed. A pull request's newer push cancels its older runs of `ci.yml` and `pr-description.yml` (one run per pull request at a time, the newest). Runs of `main` are never cancelled.

### The website's preview

A pull request that changes the site's sources (`website/`, `docs/`, `CONTRIBUTING.md`) gets a preview of the built site,
a version of the Worker with the alias `pr-<number>`, and the link is posted on it. The site is built once, by `ci.yml`'s
`website` job (`pull_request`, no secrets), which keeps `dist/` as the artifact `website-dist-<head commit>`. The upload
is MagicBeanAdmin/Release's [`longshot-website-preview.yml`](https://git.magicbean.dev/MagicBeanAdmin/Release/src/branch/main/.forgejo/workflows/longshot-website-preview.yml):
Release's workflows never run on a pull request, so MagicBeanDev/Infra's `release-trigger` starts it from the pull
request's webhook, with its number and head commit, once that artifact is kept. It is for a pull request from a branch
of Longshot on every push, or for a fork's when a maintainer adds the label `preview` after reading it (the label approves
the commit the pull request is at, and comes off after the run, so a later push needs it again). Release asks the forge
for the pull request as it is then (open, to main, that commit, changing what the site reads, the label), takes only the
artifact of `ci.yml`'s `pull_request` run at that commit, as files (a link, an absolute name or `..` is refused, the
number and size are capped), and uploads it beside main's `wrangler.jsonc` and Worker script with its own pinned
Wrangler. Nothing of the pull request is checked out or run where the Cloudflare token is; a change to the Worker itself
shows in its preview only once merged. If the `website` job failed or was cancelled, there is no preview.

### The website's deploy

[`longshot-website.yml`](https://git.magicbean.dev/MagicBeanAdmin/Release/src/branch/main/.forgejo/workflows/longshot-website.yml) builds the site from main (`scripts/website_check.sh`: `npm ci`, the Astro build
and its checks) in a job with no secrets, and deploys that `dist/` in another, the only one with the Cloudflare token,
which runs nothing of this repository. It runs after a push to `main` (release-trigger) and every hour, for the
progress data the build collects.

### Secrets

None is kept here. The signing key, the packages token and the Cloudflare token are ordinary secrets of
[MagicBeanAdmin/Release](https://git.magicbean.dev/MagicBeanAdmin/Release), whose runners are registered to that repository alone and whose workflows run only on a
schedule or by hand, so no job of a branch or a pull request here reaches them. The jobs here run with the job's own
token (`forgejo.token`), which on a push may write to the repository (releases, tags): none of the jobs that run a pull
request's code is given more.

Lend runners only to people you'd trust that far.

## Before you push

Run `mise run lint` and `mise run test` and say so in the pull request. CI's runs and logs are in the repository's
*Actions* tab ([CONTRIBUTING.md](../../../CONTRIBUTING.md#ci)).
