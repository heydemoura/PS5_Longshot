//! The stages the driver calls, each behind one seam, so a stage still being written is one change to bring in.
//!
//! - [`GameFiles`]: the read stage: the dump's checks, the exefs, the mods' ExeFS patches, the image, the romfs placed
//!   for the game, the disk ([`crate::read::ReadStage`], over the game-files and image crates).
//! - [`Assets`]: the finish's game-derived files (the texts, the button prompts, the shader list, the pipeline lists,
//!   the window's icon: [`crate::assets::FinishAssets`], over the build-assets crate). Each is optional in the game
//!   (it shows its own without), so a failure is a warning.
//! - [`lift`] and [`compile`]: the lift (`lift::whole::run`) and the compile (`build_compile::run`), each called from
//!   one function, as the [`Lift`] and [`Compile`] the driver is given ([`Stages::real`]; a test gives its own).

use crate::options::GameSource;
use build_core::report::Reporter;
use build_core::{Error, Result};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;

/// A game check's answer (`longshot-builder check`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Check {
    /// It is the build Longshot supports.
    pub ok: bool,
    /// main's build id, short.
    pub build: String,
    /// The sentence for the player.
    pub text: String,
}

/// What the read stage found of the game's code.
#[derive(Clone, Debug, Default)]
pub struct GameRead {
    /// main's build id.
    pub build_id: String,
    /// The game's own name, from its .nup (kept in the game folder for the window and the exe's name).
    pub title: Option<String>,
}

/// The mods' ExeFS patches this build takes, in order.
#[derive(Clone, Debug, Default)]
pub struct Patches {
    /// The patch files, as the image stage reads them back (game-files' `patches::to_json`).
    pub files: Vec<u8>,
    /// The list the game folder keeps (`exefs_patches.txt`: game/mods.c says which mods wait for a build).
    pub list: String,
    /// How many files apply (code or data); 0: the image is the game's own.
    pub applied: usize,
}

/// How the romfs was placed for the game.
#[derive(Clone, Debug, Default)]
pub struct RomfsPlaced {
    /// Its files.
    pub files: usize,
    /// Their bytes.
    pub bytes: u64,
    /// Damaged files replaced by stand-ins (an incomplete dump): their names.
    pub stood_in: Vec<String>,
    /// What the swap does with a `romfs` folder in the game folder.
    pub romfs_dir: RomfsDir,
    /// The builder's own files of the game folder (by name) this build has none of: removed at the swap (a damaged
    /// list, a dump's build's index).
    pub remove: Vec<String>,
}

/// A `romfs` folder in the game folder of a build from a .nup, which the game would read before the .nup's index
/// (host/hos_main.c: a stale dump's folder wins).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum RomfsDir {
    /// None there.
    #[default]
    None,
    /// The builder's (a build from a dump linked or copied it in): removed at the swap.
    Remove,
    /// Not the builder's: the player's own, left as it is and said.
    Theirs,
}

/// What the disk under a folder can tell.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Disk {
    /// The bytes free for this user.
    pub free: Option<u64>,
    /// On a hard drive (`None`: cannot be told).
    pub spinning: Option<bool>,
}

/// The read stage.
pub trait GameFiles: Sync {
    /// Whether the game is the supported build, from its main module's header alone (nothing unpacked).
    fn check(&self, game: &GameSource) -> Result<Check>;
    /// The disk under `folder` (or the nearest folder above it that exists).
    fn disk(&self, folder: &Path) -> Disk;
    /// The game folder `out` takes new files (made if missing), said in the player's words where it does not: a folder
    /// under Program Files does only for administrators, and the build would otherwise stop minutes later.
    fn check_writable(&self, out: &Path) -> Result<()>;
    /// The game's code modules into `exefs` (made again from nothing), checked: every module one, the supported build.
    /// The game's own name and icon kept in `staging` (the finish's folder, laid out as the game folder).
    fn read_exefs(&self, game: &GameSource, exefs: &Path, staging: &Path, r: &Reporter) -> Result<GameRead>;
    /// The mods' ExeFS patches for this build: `profile`'s manifest mods' (the Mod API's layer gives them), each file's
    /// kind, those that would stop a hook site (`tables`) refused; a mod in the emulators' layout in its `mods/` said
    /// (`mod_needs_import`), never read.
    fn patches(&self, exefs: &Path, profile: Option<&Path>, tables: &Path, r: &Reporter) -> Result<Patches>;
    /// The image written into `image` (`image.bin`, `image.json`, each only when it changed): the modules laid out,
    /// patched, relocated and bound; `hle`, the symbols the host implements.
    fn image(&self, exefs: &Path, patches: &Patches, hle: &BTreeSet<String>, image: &Path, r: &Reporter) -> Result<()>;
    /// The romfs placed for the game in game folder `out`: the .nup's index, its note and the damaged files' list
    /// written in `staging` (the finish's folder: they go in at the swap), or an unpacked dump's folder linked into
    /// `out` (26 GB is not staged). What the swap removes is said in the answer. Runs beside the lift: it logs, and
    /// sends no progress.
    fn romfs(&self, game: &GameSource, out: &Path, staging: &Path, r: &Reporter) -> Result<RomfsPlaced>;
}

/// Where the finish's assets go, and what they may use.
#[derive(Clone, Copy, Debug)]
pub struct AssetPlaces<'a> {
    /// The game folder: its romfs folder (a dump's, linked) and, before the first build that stages it, its title.
    pub out: &'a Path,
    /// Where the assets are written (the work folder's `finish/`), laid out as the game folder: the finish puts them
    /// in with the build's other files, all at once, so a build that fails before leaves the old build's whole.
    pub into: &'a Path,
    /// The work folder: the assets' cache (`assets/`).
    pub work: &'a Path,
    /// The tools' root: Kenney's fonts, `assets/`, the seed pipeline list.
    pub root: &'a Path,
    /// Workers a step may run.
    pub jobs: usize,
}

/// The finish's game-derived files. Each writes into the game folder, reading the game's files through the romfs as
/// the game reads them (its index, or its linked folder), and keeps what it made while its inputs stay the same.
pub trait Assets: Sync {
    /// The texts that name the console, reworded for a PC in every language (`out/texts`, game/texts.c).
    fn texts(&self, at: &AssetPlaces<'_>, r: &Reporter) -> Result<()>;
    /// The button prompts of other controllers, the game's icon font redrawn (`out/prompts`, host/prompts.c).
    fn prompts(&self, at: &AssetPlaces<'_>, r: &Reporter) -> Result<()>;
    /// Every shader of the game, for the shader preparation (`out/longshot_shaders.bin`, gpu/vk/vk_shader.cpp).
    fn shader_list(&self, at: &AssetPlaces<'_>, r: &Reporter) -> Result<()>;
    /// The pipeline lists shipped with the tools (`out/cache/longshot_pipelines.bin`,
    /// `out/longshot_pipeline_states_shipped.bin`): hashes and states, no game code.
    fn pipelines(&self, at: &AssetPlaces<'_>, r: &Reporter) -> Result<()>;
    /// The window's icon (`out/longshot.png`, Linux): the game's own (kept by the read stage), else Longshot's.
    fn window_icon(&self, at: &AssetPlaces<'_>, r: &Reporter) -> Result<()>;
}

fn pending(what: &str, missing: &str) -> Error {
    Error::player("stage_not_ready", format!("This builder cannot {what} yet: {missing} is not in it."))
}

/// Assets that are never made (a test's stand-in): each says so, as a warning of the build.
#[derive(Clone, Copy, Debug, Default)]
pub struct PendingAssets;

impl Assets for PendingAssets {
    fn texts(&self, _: &AssetPlaces<'_>, _: &Reporter) -> Result<()> {
        Err(pending("reword the game's texts", "the assets stage"))
    }
    fn prompts(&self, _: &AssetPlaces<'_>, _: &Reporter) -> Result<()> {
        Err(pending("draw the button prompts", "the assets stage"))
    }
    fn shader_list(&self, _: &AssetPlaces<'_>, _: &Reporter) -> Result<()> {
        Err(pending("list the game's shaders", "the assets stage"))
    }
    fn pipelines(&self, _: &AssetPlaces<'_>, _: &Reporter) -> Result<()> {
        Err(pending("ship the pipeline lists", "the assets stage"))
    }
    fn window_icon(&self, _: &AssetPlaces<'_>, _: &Reporter) -> Result<()> {
        Err(pending("make the window's icon", "the assets stage"))
    }
}

/// What a lift is asked: the driver's view of `lift::whole::LiftOptions`.
#[derive(Clone, Debug)]
pub struct LiftAsk {
    /// The image's folder.
    pub image: PathBuf,
    /// gen/.
    pub gen_dir: PathBuf,
    /// The tree.
    pub root: PathBuf,
    /// `main.npdm` (the boot constants).
    pub npdm: PathBuf,
    /// What the lift does with the guest profile.
    pub profiling: lift::whole::Profiling,
    /// Workers.
    pub jobs: usize,
    /// The lifts' store.
    pub store: Option<PathBuf>,
}

/// What a lift did, as the driver needs it.
#[derive(Clone, Debug, Default)]
pub struct Lifted {
    /// Functions built with their edits (hook sites, entry hooks, text patches).
    pub edited: usize,
    /// The lift's key.
    pub key: String,
    /// Units in all, and lifted now.
    pub units: (usize, usize),
    /// The lift was skipped or taken from the store.
    pub reused: bool,
    /// Where the compile gets the units' texts the lift did not write (none: it wrote them all).
    pub texts: Option<Arc<LiftTexts>>,
}

impl Lifted {
    /// The compile's source of the units' texts.
    pub fn ir_source(&self) -> Option<build_compile::IrSource> {
        self.texts.clone().map(|t| build_compile::IrSource(t as Arc<dyn build_compile::IrText>))
    }

    /// The units whose texts were made noted in gen/'s `units.txt` (after the compile, whatever came of it).
    pub fn note_texts(&self, r: &Reporter) {
        if let Some(t) = &self.texts {
            t.note(r);
        }
    }
}

/// The lift's units' texts, made for the compile ([`lift::whole::Texts`]).
pub struct LiftTexts(pub lift::whole::Texts);

impl LiftTexts {
    /// After the compile, whatever came of it (the build's and a compile alone's): what the lift's options did in the
    /// texts made (the log's line), and those units noted in gen/'s `units.txt`.
    pub fn note(&self, r: &Reporter) {
        if let Some(note) = self.0.fp_nan_mid_note() {
            r.log(note);
        }
        if let Err(e) = self.0.finish() {
            r.log(format!("lift: the units' list not updated ({e}): the next build makes those units' texts again"));
        }
    }
}

impl std::fmt::Debug for LiftTexts {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("LiftTexts")
    }
}

impl build_compile::IrText for LiftTexts {
    fn text(&self, name: &str) -> Result<String> {
        self.0.text(name)
    }
}

/// The lift stage.
pub trait Lift: Sync {
    /// The lift of `ask`.
    fn lift(&self, ask: &LiftAsk, r: &Reporter) -> Result<Lifted>;
}

/// The compile stage.
pub trait Compile: Sync {
    /// The compile of `o`.
    fn compile(&self, o: &build_compile::Options, r: &Reporter) -> Result<build_compile::Outcome>;
}

/// The guest profile the tree ships, if it does (assets/profiles/; addresses and counts, no game code).
pub const GUEST_PROFILE: &str = "assets/profiles/guest.profile.xz";

/// What the lift does with the guest profile `g` asks for: the tree's (when it ships one) or a file's counts used (its
/// hottest functions, 95 % of the counted runs and at most 4,000, in hot units), or counters written in.
pub fn profiling(root: &Path, g: &crate::GuestProfile) -> lift::whole::Profiling {
    let used = |path: PathBuf| lift::whole::Profiling::Use { path, share: 0.95, max: 4000 };
    match g {
        crate::GuestProfile::Shipped => {
            Some(root.join(GUEST_PROFILE)).filter(|f| f.is_file()).map_or(lift::whole::Profiling::Off, used)
        }
        crate::GuestProfile::File(f) => used(f.clone()),
        crate::GuestProfile::Generate => lift::whole::Profiling::Generate,
    }
}

/// The lift's options for `ask`, from the environment's switches: the one place both the lift ([`lift`]) and a
/// compile alone ([`lift_texts`], `longshot-builder compile`) take them from, so that both refuse what one refuses.
pub fn lift_options(ask: &LiftAsk) -> Result<lift::whole::LiftOptions> {
    unicode_switches(&crate::env_os)?;
    lift_options_from(ask, &crate::env)
}

/// The middle path's skip list read as text ([`crate::env`] reads one that is not Unicode as unset): with the switch
/// on (unset included: the default), a list that is set but not Unicode is refused, not taken for none (which would
/// give every function the option). With the switch off the list is not used (nor refused), as [`fp_nan_mid`] does not
/// use it. The switch itself is read as [`crate::env`] reads it (a value that is not Unicode as unset).
fn unicode_switches(env_os: &dyn Fn(&str) -> Option<std::ffi::OsString>) -> Result<()> {
    let on = fp_nan_mid_on(env_os("LONGSHOT_FP_NAN_MID").and_then(|v| v.into_string().ok()).as_deref());
    if on && env_os("LONGSHOT_FP_NAN_MID_SKIP").is_some_and(|v| v.to_str().is_none()) {
        return Err(Error::format("LONGSHOT_FP_NAN_MID_SKIP", "its value is not Unicode text"));
    }
    Ok(())
}

/// [`lift_options`] with the switches read from `env`.
fn lift_options_from(ask: &LiftAsk, env: &dyn Fn(&str) -> Option<String>) -> Result<lift::whole::LiftOptions> {
    let mut o = lift::whole::LiftOptions::new(&ask.image, &ask.gen_dir);
    o.host = Some(ask.root.join("host"));
    o.tables = Some(ask.root.join("game"));
    o.npdm = Some(ask.npdm.clone());
    o.jobs = ask.jobs.max(1);
    o.store = ask.store.clone();
    o.profiling = ask.profiling.clone();
    // (the units' texts, 5.7 GB on the game, are made as the compile needs them and not kept; LONGSHOT_KEEP_IR=1 keeps
    // them in gen/ to read or compare)
    o.keep_text = env("LONGSHOT_KEEP_IR").is_some_and(|v| v == "1");
    o.fp_nan_mid = fp_nan_mid(env("LONGSHOT_FP_NAN_MID").as_deref(), env("LONGSHOT_FP_NAN_MID_SKIP").as_deref())?;
    // (a bad skip list stops the lift first: lift())
    o.hot_split =
        hot_split(env("LONGSHOT_HOT_SPLIT").as_deref(), env("LONGSHOT_HOT_SPLIT_SKIP").as_deref()).ok().flatten();
    Ok(o)
}

/// Whether `LONGSHOT_FP_NAN_MID`'s value `v` turns the middle path on: unset or any value but an off word
/// ([`build_core::switch`]: `0`, `off`, `no`, `false`, empty) is on, the default.
fn fp_nan_mid_on(v: Option<&str>) -> bool {
    v.is_none_or(|v| build_core::switch(v) != Some(false))
}

/// The float slow paths' middle path (`lift::whole::LiftOptions::fp_nan_mid`), from `LONGSHOT_FP_NAN_MID` (on by
/// default: unset, `1` or any word but an off one; `0`, `off`, `no`, `false` or empty turn it off: the IR and every key
/// as without it) and `LONGSHOT_FP_NAN_MID_SKIP`, the functions left out (their entries in hex, `0x` or `f_` before
/// each or not, split by commas or spaces).
pub fn fp_nan_mid(on: Option<&str>, skip: Option<&str>) -> Result<Option<BTreeSet<u64>>> {
    if !fp_nan_mid_on(on) {
        return Ok(None);
    }
    let mut entries = BTreeSet::new();
    for word in skip.unwrap_or("").split(|c: char| c == ',' || c.is_whitespace()).filter(|w| !w.is_empty()) {
        let hex = word.trim_start_matches("f_").trim_start_matches("0x");
        let entry = u64::from_str_radix(hex, 16).map_err(|_| {
            Error::format("LONGSHOT_FP_NAN_MID_SKIP", format!("{word:?} is no function's entry (hex, as f_8000a000)"))
        })?;
        entries.insert(entry);
    }
    Ok(Some(entries))
}

/// The hot units made for LLVM's hot-cold splitting (lift::whole::LiftOptions::hot_split) as `LONGSHOT_HOT_SPLIT` (`on`)
/// and `LONGSHOT_HOT_SPLIT_SKIP` (`skip`) say: on by default, off when the switch is `0` (or another off word:
/// build_compile::hotsplit::on, which the compile's half, `-hot-cold-split` for the hot units, reads it with too); the
/// skip list, the entries of functions left out (hex, `f_` or `0x` allowed, separated by commas or spaces). Err: the
/// word of the list that is no entry.
pub fn hot_split(on: Option<&str>, skip: Option<&str>) -> std::result::Result<Option<BTreeSet<u64>>, String> {
    if !build_compile::hotsplit::on(on) {
        return Ok(None);
    }
    let mut out = BTreeSet::new();
    for word in skip.unwrap_or("").split([',', ' ']).map(str::trim).filter(|w| !w.is_empty()) {
        let hex = word.strip_prefix("f_").or_else(|| word.strip_prefix("0x")).unwrap_or(word);
        out.insert(u64::from_str_radix(hex, 16).map_err(|_| word.to_string())?);
    }
    Ok(Some(out))
}

/// A compile on its own (`longshot-builder compile`) of the lift in `gen_dir`: refused when `LONGSHOT_HOT_SPLIT` and
/// `LONGSHOT_HOT_SPLIT_SKIP` are not what that lift was made with, or the list is bad. Its units' texts on disk
/// (`LONGSHOT_KEEP_IR=1`) are not made again, so without this an off lift would compile with the hot flags (or the
/// other way round), and a setjmp owner the lift did not write noinline could have a region outlined.
pub fn check_hot_split(gen_dir: &Path) -> Result<()> {
    check_hot_split_as(
        gen_dir,
        crate::env("LONGSHOT_HOT_SPLIT").as_deref(),
        crate::env("LONGSHOT_HOT_SPLIT_SKIP").as_deref(),
    )
}

/// [`check_hot_split`] with the switch `on` and the list `skip` as given (unset: None).
pub fn check_hot_split_as(gen_dir: &Path, on: Option<&str>, skip: Option<&str>) -> Result<()> {
    let now = hot_split(on, skip)
        .map_err(|w| Error::format("LONGSHOT_HOT_SPLIT_SKIP", format!("{w:?} is not a function's entry (hex)")))?;
    match lift::whole::hot_split_of(gen_dir) {
        // (no complete lift: a lift that stopped, or failed at the link, deletes its key file and can leave its texts)
        None if now.is_some() => Err(Error::format(
            "LONGSHOT_HOT_SPLIT",
            "gen/ holds no complete lift (no lift.key) to compile with the switch on: lift again",
        )),
        Some(made) if made != now => {
            let say = |s: &Option<BTreeSet<u64>>| match s {
                None => "off".to_string(),
                Some(k) => format!("on, {} functions left out", k.len()),
            };
            Err(Error::format(
                "LONGSHOT_HOT_SPLIT",
                format!("the lift in gen/ was made with it {}, the compile asks {}: lift again", say(&made), say(&now)),
            ))
        }
        _ => Ok(()),
    }
}

/// The texts of the lift `ask` asks for, made when the compile needs one (`session`: the lift's, when it ran now).
pub fn lift_texts(ask: &LiftAsk, session: Option<Arc<lift::whole::Session>>) -> Result<Option<Arc<LiftTexts>>> {
    unicode_switches(&crate::env_os)?;
    lift_texts_from(ask, session, &crate::env)
}

/// [`lift_texts`] with the switches read from `env`.
fn lift_texts_from(
    ask: &LiftAsk,
    session: Option<Arc<lift::whole::Session>>,
    env: &dyn Fn(&str) -> Option<String>,
) -> Result<Option<Arc<LiftTexts>>> {
    let o = lift_options_from(ask, env)?;
    Ok((!o.keeps_text()).then(|| Arc::new(LiftTexts(lift::whole::Texts::new(o, session)))))
}

/// The lift (`lift::whole::run`): the one place the driver calls it.
pub fn lift(ask: &LiftAsk, r: &Reporter) -> Result<Lifted> {
    match hot_split(crate::env("LONGSHOT_HOT_SPLIT").as_deref(), crate::env("LONGSHOT_HOT_SPLIT_SKIP").as_deref()) {
        Err(word) => {
            return Err(Error::format("LONGSHOT_HOT_SPLIT_SKIP", format!("{word:?} is not a function's entry (hex)")));
        }
        Ok(Some(skip)) => r.log(format!(
            "LONGSHOT_HOT_SPLIT: the hot units are made for LLVM's hot-cold splitting ({} functions left out by \
             LONGSHOT_HOT_SPLIT_SKIP, and those the edits change)",
            skip.len()
        )),
        Ok(None) => {}
    }
    let (out, session) = lift::whole::run_with(&lift_options(ask)?, r)?;
    let reused = !matches!(out.how, lift::whole::How::Lifted);
    Ok(Lifted {
        units: (out.units.len(), out.units_lifted),
        key: out.key,
        reused,
        edited: out.edited,
        texts: lift_texts(ask, session)?,
    })
}

/// The compile (`build_compile::run`): the one place the driver calls it.
pub fn compile(o: &build_compile::Options, r: &Reporter) -> Result<build_compile::Outcome> {
    build_compile::run(o, r)
}

/// The builder's lift ([`lift`]).
#[derive(Clone, Copy, Debug, Default)]
pub struct LiftStage;
impl Lift for LiftStage {
    fn lift(&self, ask: &LiftAsk, r: &Reporter) -> Result<Lifted> {
        lift(ask, r)
    }
}

/// The builder's compile ([`compile`]).
#[derive(Clone, Copy, Debug, Default)]
pub struct CompileStage;
impl Compile for CompileStage {
    fn compile(&self, o: &build_compile::Options, r: &Reporter) -> Result<build_compile::Outcome> {
        compile(o, r)
    }
}

/// The stages a build runs.
pub struct Stages<'a> {
    /// The read stage.
    pub game: &'a dyn GameFiles,
    /// The lift.
    pub lift: &'a dyn Lift,
    /// The compile.
    pub compile: &'a dyn Compile,
    /// The finish's assets.
    pub assets: &'a dyn Assets,
}

impl Stages<'static> {
    /// The builder's own stages.
    pub fn real() -> Stages<'static> {
        Stages {
            game: &crate::read::ReadStage,
            lift: &LiftStage,
            compile: &CompileStage,
            assets: &crate::assets::FinishAssets,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{LiftAsk, fp_nan_mid, lift_options_from, lift_texts_from, unicode_switches};
    use std::collections::BTreeSet;

    #[test]
    fn the_float_middle_path_is_read_by_value_with_its_list_of_functions_left_out() {
        // (on by default: unset, 1, an on word or any other; off for an off word, any case, spaces around; the list is
        // read only when on)
        for off in [Some("0"), Some(""), Some("off"), Some("no"), Some("false"), Some(" OFF "), Some("False")] {
            assert_eq!(fp_nan_mid(off, Some("not hex")).unwrap(), None, "{off:?}");
        }
        for on in [None, Some("1"), Some("on"), Some("yes"), Some("true"), Some("2")] {
            assert_eq!(fp_nan_mid(on, None).unwrap(), Some(BTreeSet::new()), "{on:?}");
            assert!(fp_nan_mid(on, Some("not hex")).is_err(), "{on:?}: the list read");
        }
        assert_eq!(
            fp_nan_mid(Some("1"), Some("f_81c7f880, 0x820e2520 8173b340,,")).unwrap(),
            Some(BTreeSet::from([0x81c7_f880, 0x820e_2520, 0x8173_b340]))
        );
        let e = fp_nan_mid(Some("1"), Some("f_81c7f880,f_zz")).unwrap_err().to_string();
        assert!(e.contains("\"f_zz\""), "{e}");
    }

    #[test]
    fn the_lift_and_a_compile_alone_take_the_float_middle_path_from_one_place_which_refuses_a_bad_list() {
        // (lift_options is what both the lift and lift_texts (a compile alone: `longshot-builder compile`) read; a bad
        // list stops it, as the lift's own check did, instead of a compile with the option silently off)
        let ask = LiftAsk {
            image: "image".into(),
            gen_dir: "gen".into(),
            root: "root".into(),
            npdm: "main.npdm".into(),
            profiling: lift::whole::Profiling::Off,
            jobs: 1,
            store: None,
        };
        let with = |on: &'static str, skip: &'static str| {
            move |name: &str| match name {
                "LONGSHOT_FP_NAN_MID" => Some(on.to_string()),
                "LONGSHOT_FP_NAN_MID_SKIP" => Some(skip.to_string()),
                _ => None,
            }
        };
        let e = lift_options_from(&ask, &with("1", "f_81c7f880,f_zz")).unwrap_err().to_string();
        assert!(e.contains("LONGSHOT_FP_NAN_MID_SKIP") && e.contains("\"f_zz\""), "{e}");
        // (the texts a compile alone makes: refused the same, not made with the option off)
        let e = lift_texts_from(&ask, None, &with("1", "f_zz")).expect_err("a bad list refused").to_string();
        assert!(e.contains("LONGSHOT_FP_NAN_MID_SKIP"), "{e}");
        assert!(lift_texts_from(&ask, None, &with("1", "f_81c7f880")).unwrap().is_some());
        let o = lift_options_from(&ask, &with("1", "f_81c7f880")).unwrap();
        assert_eq!(o.fp_nan_mid, Some(BTreeSet::from([0x81c7_f880])));
        // (a list that is not Unicode: refused, not read as none)
        #[cfg(windows)]
        let bad: std::ffi::OsString = std::os::windows::ffi::OsStringExt::from_wide(&[0x66, 0xd800]);
        #[cfg(unix)]
        let bad: std::ffi::OsString = std::os::unix::ffi::OsStringExt::from_vec(vec![b'f', 0xff]);
        let os = |on: &'static str| {
            let bad = bad.clone();
            move |name: &str| match name {
                "LONGSHOT_FP_NAN_MID" => Some(on.into()),
                "LONGSHOT_FP_NAN_MID_SKIP" => Some(bad.clone()),
                _ => None,
            }
        };
        let e = unicode_switches(&os("1")).unwrap_err().to_string();
        assert!(e.contains("LONGSHOT_FP_NAN_MID_SKIP") && e.contains("not Unicode"), "{e}");
        // (off: the list is not used, whatever it holds, and nothing of it is refused)
        for off in ["0", "", "off", "no", "false"] {
            assert!(unicode_switches(&os(off)).is_ok(), "{off:?}");
        }
        // (unset is on: the list is read, and one that is not Unicode refused)
        let unset = |name: &str| (name == "LONGSHOT_FP_NAN_MID_SKIP").then(|| bad.clone());
        assert!(unicode_switches(&unset).unwrap_err().to_string().contains("not Unicode"));
        assert!(unicode_switches(&|_: &str| None).is_ok());
        // (off: the list is not used)
        assert_eq!(lift_options_from(&ask, &with("0", "f_zz")).unwrap().fp_nan_mid, None);
    }

    #[test]
    fn the_float_middle_path_is_on_when_its_switch_is_unset_and_off_when_it_is_0() {
        // (the lift's options a build takes from the environment: nothing set gives the middle path with nothing left
        // out, as LONGSHOT_FP_NAN_MID=1 does; 0 gives none, the lift as before the switch existed)
        let ask = LiftAsk {
            image: "image".into(),
            gen_dir: "gen".into(),
            root: "root".into(),
            npdm: "main.npdm".into(),
            profiling: lift::whole::Profiling::Off,
            jobs: 1,
            store: None,
        };
        let only = |on: Option<&'static str>, skip: Option<&'static str>| {
            move |name: &str| match name {
                "LONGSHOT_FP_NAN_MID" => on.map(str::to_string),
                "LONGSHOT_FP_NAN_MID_SKIP" => skip.map(str::to_string),
                _ => None,
            }
        };
        let unset = lift_options_from(&ask, &only(None, None)).unwrap();
        assert_eq!(unset.fp_nan_mid, Some(BTreeSet::new()), "unset: on");
        assert_eq!(unset.fp_nan_mid, lift_options_from(&ask, &only(Some("1"), None)).unwrap().fp_nan_mid);
        assert_eq!(
            lift_options_from(&ask, &only(None, Some("f_81c7f880"))).unwrap().fp_nan_mid,
            Some(BTreeSet::from([0x81c7_f880])),
            "unset: the skip list taken"
        );
        assert_eq!(lift_options_from(&ask, &only(Some("0"), Some("f_81c7f880"))).unwrap().fp_nan_mid, None, "0: off");
        // (a compile alone reads it the same way)
        assert!(lift_texts_from(&ask, None, &only(None, Some("f_zz"))).is_err(), "unset: the list read");
        assert!(lift_texts_from(&ask, None, &only(Some("0"), Some("f_zz"))).is_ok(), "0: the list not read");
    }
}
