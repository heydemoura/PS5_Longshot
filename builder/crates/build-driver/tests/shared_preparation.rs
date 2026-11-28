//! A second installation can reuse completed preparation only while its inputs and shader cache match.
use build_core::report::{Event, Format, Reporter};
use build_driver::{BuildOptions, CompileShaders, GameSource, build, folder, layout::Work, shaders};
use std::{
    fs,
    path::Path,
    process::Command,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};

fn instance(dir: &Path) {
    fs::create_dir_all(dir).unwrap();
    let mut record = folder::Record::new("builder", &GameSource::Nup(dir.join("example.nup")), &[]);
    record.stamp = Some(folder::Stamp {
        builder: "builder".into(),
        profile: "profile".into(),
        game: "game".into(),
        mods: None,
        patches: String::new(),
    });
    record.write(dir).unwrap();
    fs::write(dir.join("longshot_version.txt"), "translator_stamp=abcd1234\n").unwrap();
    for name in shaders::key_files(cfg!(windows)) {
        fs::write(dir.join(name), name.as_bytes()).unwrap();
    }
}

#[test]
fn a_second_instance_reuses_only_matching_successful_shader_preparation() {
    if !cfg!(any(windows, target_os = "linux")) {
        return;
    }
    let flag = "LONGSHOT_TEST_SHARED_PREPARATION";
    let Some(root) = std::env::var_os(flag).map(std::path::PathBuf::from) else {
        let root = std::env::temp_dir().join(format!("longshot-instance-preparation-{}", std::process::id()));
        fs::create_dir(&root).unwrap();
        let status = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "a_second_instance_reuses_only_matching_successful_shader_preparation", "--nocapture"])
            .env(flag, &root)
            .env("LONGSHOT_CACHE", root.join("cache"))
            .env("LONGSHOT_SHADER_STORE", root.join("store"))
            .env_remove("LONGSHOT_NO_PREPARE")
            .status()
            .unwrap();
        fs::remove_dir_all(root).unwrap();
        assert!(status.success());
        return;
    };
    let (first, second) = (root.join("first"), root.join("second"));
    instance(&first);
    instance(&second);
    let get = |k: &str| std::env::var(k).ok();
    let cache = shaders::shared_dir(&first, get).unwrap();
    assert_eq!(shaders::shared_dir(&second, get), Some(cache.clone()));
    fs::create_dir_all(&cache).unwrap();
    fs::create_dir_all(root.join("store")).unwrap();
    let header = |magic: &[u8; 4]| {
        let mut h = magic.to_vec();
        h.extend(1u32.to_le_bytes());
        h.extend(7u32.to_le_bytes());
        h.extend([0; 4]);
        h
    };
    let mut pack = header(b"LSPK");
    let mut index = header(b"LSPI");
    for id in [1u64, 2u64] {
        let offset = pack.len() as u64 + 24;
        for field in [id, id, 1] {
            pack.extend(field.to_le_bytes());
        }
        pack.extend(1u32.to_le_bytes());
        for field in [id, id, offset, 1] {
            index.extend(field.to_le_bytes());
        }
    }
    fs::write(root.join("store/spirv.pack"), &pack).unwrap();
    fs::write(root.join("store/spirv.idx"), &index).unwrap();
    let mut programs = [0u8; 17];
    programs[..4].copy_from_slice(b"HSP1");
    programs[4..8].copy_from_slice(&7u32.to_le_bytes());
    fs::write(cache.join("longshot_programs.bin"), programs).unwrap();
    fs::write(cache.join("longshot_prepared.bin"), [1; 48]).unwrap();
    fs::write(cache.join("longshot_vk_pipelines.bin"), b"pipelines").unwrap();
    let stale = |dir: &Path| shaders::would_prepare(dir, cfg!(windows), CompileShaders::Most, get);
    assert!(stale(&second));
    let driver = shaders::driver_id(get).unwrap();
    let old = shaders::Record::of(&first, cfg!(windows), CompileShaders::Most, &driver, &Default::default()).unwrap();
    old.write(&first);
    assert!(!stale(&first), "the original installation can still use its matching key");
    let reporter = build_core::report::Reporter::to(build_core::report::Format::Text, Box::new(std::io::sink()));
    assert!(shaders::prepare_unless_kept(
        &first.join("no executable"),
        &first,
        cfg!(windows),
        CompileShaders::Most,
        1,
        get,
        &reporter
    ));
    assert!(stale(&second), "an old key cannot certify the shared cache for another installation");
    shaders::remember(&first, cfg!(windows), CompileShaders::Most, get);
    assert!(
        !stale(&second),
        "a fresh folder with identical inputs and completed shared caches should skip preparation"
    );
    assert!(shaders::would_prepare(&second, cfg!(windows), CompileShaders::All, get));
    let plan_reuses_shaders = || {
        let cached = Arc::new(AtomicBool::new(false));
        let observed = cached.clone();
        let report = Reporter::to(Format::Text, Box::new(std::io::sink())).observed(Some(Arc::new(move |event| {
            if let Event::BuildPlan { shaders_cached, .. } = event {
                observed.store(*shaders_cached, Ordering::Relaxed);
            }
        })));
        let mut options = BuildOptions::new(GameSource::Nup(root.join("example.nup")), &second);
        options.root = Some(root.clone());
        build::report_plan(&options, &Work::at(root.join("work")), 24, true, &report);
        cached.load(Ordering::Relaxed)
    };
    assert!(plan_reuses_shaders(), "the build plan should account for the matching cache");
    let renderer = second.join(shaders::key_files(cfg!(windows))[0]);
    fs::write(&renderer, b"a different renderer").unwrap();
    assert!(stale(&second));
    assert!(!plan_reuses_shaders(), "a renderer change should restore the cold shader budget");
    fs::copy(first.join(shaders::key_files(cfg!(windows))[0]), &renderer).unwrap();
    assert!(!stale(&second), "restoring a previously prepared renderer reuses its receipt");
    fs::write(root.join("store/spirv.idx"), b"incomplete index").unwrap();
    assert!(stale(&first), "a folder-local record cannot override a missing SPIR-V generation");
    fs::write(root.join("store/spirv.idx"), &index[..48]).unwrap();
    fs::write(root.join("store/spirv.pack"), &pack[..44]).unwrap();
    assert!(stale(&first), "a valid, nonempty but truncated store needs preparation");
    fs::write(root.join("store/spirv.idx"), &index).unwrap();
    fs::write(root.join("store/spirv.pack"), &pack).unwrap();
    assert!(!stale(&first));
    fs::write(cache.join("longshot_vk_pipelines.bin"), b"partial").unwrap();
    assert!(stale(&first), "a stale local record cannot bless a replaced pipeline cache");
    assert!(stale(&second));
    fs::remove_file(cache.join("longshot_vk_pipelines.bin")).unwrap();
    assert!(stale(&second));
}
