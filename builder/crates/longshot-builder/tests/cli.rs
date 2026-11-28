//! The command line's own answers: `--help` (and `-h`, `help`) is the usage and status 0; `--version` names what the
//! program carries (a developer's build, here: none).

fn run(args: &[&str]) -> (Option<i32>, String) {
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_longshot-builder")).args(args).output().unwrap();
    (out.status.code(), String::from_utf8(out.stdout).unwrap())
}

#[test]
fn help_is_the_usage_and_version_says_what_is_carried() {
    for h in ["--help", "-h", "help"] {
        let (code, out) = run(&[h]);
        assert_eq!(code, Some(0), "{h}");
        assert!(out.starts_with("usage: longshot-builder build"), "{h}: {out}");
    }
    let (code, out) = run(&["--version"]);
    assert_eq!(code, Some(0));
    assert!(out.starts_with("longshot-builder ") && out.contains("a developer's build"), "{out}");
}

#[test]
fn json_output_formats_early_errors_and_help_on_stdout() {
    for flag in [vec!["--output=json"], vec!["--output", "json"]] {
        for command in ["build", "diagnostics", "--help", "--version"] {
            let output = std::process::Command::new(env!("CARGO_BIN_EXE_longshot-builder"))
                .arg(command)
                .args(&flag)
                .output()
                .unwrap();
            assert!(output.stderr.is_empty(), "{}", String::from_utf8_lossy(&output.stderr));
            let event: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
            assert_eq!(event["v"], 1);
            assert_eq!(event["type"], if output.status.success() { "log" } else { "error" });
        }
    }
}

#[test]
fn json_output_captures_developer_results_and_diagnostics() {
    for (word, success) in [("d503201f", true), ("invalid", false)] {
        let output = std::process::Command::new(env!("CARGO_BIN_EXE_longshot-builder"))
            .args(["dev", "decode", word, "--output", "json"])
            .output()
            .unwrap();
        assert!(output.stderr.is_empty());
        assert_eq!(output.status.success(), success);
        let event: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(event["v"], 1);
        assert_eq!(event["type"], "log");
        assert!(event["text"].as_str().unwrap().contains(word));
        assert_eq!(event["level"], if success { "INFO" } else { "ERROR" });
    }
}

#[test]
fn build_announces_its_plan_before_resolving_the_tools() {
    use std::fs;
    let fixture = std::env::temp_dir().join(format!("longshot-build-plan-cli-{}", std::process::id()));
    fs::create_dir(&fixture).unwrap();
    let game = fixture.join("game.nup");
    let out = fixture.join("game");
    let tools = fixture.join("missing-tools");
    let work = fixture.join("work");
    fs::write(&game, b"the builder never opens this fixture").unwrap();
    let (status, stdout) = run(&[
        "build",
        "--game",
        game.to_str().unwrap(),
        "--out",
        out.to_str().unwrap(),
        "--root",
        tools.to_str().unwrap(),
        "--work",
        work.to_str().unwrap(),
        "--no-prepare-shaders",
        "--output",
        "json",
    ]);
    assert_eq!(status, Some(1), "{stdout}");
    let events: Vec<serde_json::Value> = stdout.lines().map(|line| serde_json::from_str(line).unwrap()).collect();
    assert_eq!(
        events[0],
        serde_json::json!({"v":1,"type":"build_plan","stages":["read","lift","compile","finish"],"eta_deferred":true,"shaders_cached":false})
    );
    assert_eq!(events[1]["type"], "build_progress");
    assert_eq!(events[1]["fraction"], 0.0);
    assert_eq!(events[1]["seconds"], 0);
    let error = events.iter().find(|event| event["type"] == "error").unwrap();
    assert_eq!(error["code"], "tools_incomplete");
    assert!(!work.exists());
    fs::remove_dir_all(fixture).unwrap();
}

#[test]
fn extracts_only_romfs_and_preserves_an_existing_destination() {
    use std::fs;
    let fixture = std::env::temp_dir().join(format!("longshot-romfs-cli-{}", std::process::id()));
    fs::create_dir_all(&fixture).unwrap();
    let game = fixture.join("original");
    let out = fixture.join("export");
    fs::create_dir_all(game.join("romfs/nested")).unwrap();
    fs::create_dir_all(game.join("exefs")).unwrap();
    fs::create_dir(&out).unwrap();
    fs::write(game.join("romfs/nested/example.bin"), b"fixture").unwrap();
    fs::write(game.join("exefs/main"), b"executable fixture").unwrap();

    let args = ["extract-romfs", "--game", game.to_str().unwrap(), "--out", out.to_str().unwrap(), "--output", "json"];
    let (status, stdout) = run(&args);
    assert_eq!(status, Some(0), "{stdout}");
    assert!(stdout.contains("\"type\":\"progress\"") && stdout.contains("\"type\":\"done\""), "{stdout}");
    assert_eq!(fs::read(out.join("romfs/nested/example.bin")).unwrap(), b"fixture");
    assert!(!out.join("exefs").exists());
    fs::write(out.join("romfs/nested/example.bin"), b"keep").unwrap();
    assert_ne!(run(&args).0, Some(0));
    assert_eq!(fs::read(out.join("romfs/nested/example.bin")).unwrap(), b"keep");
    assert_eq!(fs::read(game.join("romfs/nested/example.bin")).unwrap(), b"fixture");
    fs::remove_dir_all(fixture).unwrap();
}

#[test]
fn exports_the_newest_titles_romfs_from_a_package() {
    use game_files::testkit::NupBuilder;
    use std::fs;
    let fixture = std::env::temp_dir().join(format!("longshot-romfs-package-{}", std::process::id()));
    fs::create_dir(&fixture).unwrap();
    let game = fixture.join("original.nup");
    let out = fixture.join("export");
    fs::create_dir(&out).unwrap();
    let package = NupBuilder {
        titles: vec![
            (1, 1, vec![("main".into(), b"old exefs".to_vec(), 0)], vec![("example.bin".into(), b"old".to_vec(), 0)]),
            (1, 2, vec![("main".into(), b"new exefs".to_vec(), 0)], vec![("example.bin".into(), b"new".to_vec(), 1)]),
        ],
    };
    fs::write(&game, package.build()).unwrap();
    let args = ["extract-romfs", "--game", game.to_str().unwrap(), "--out", out.to_str().unwrap(), "--output", "json"];
    let (status, stdout) = run(&args);
    assert_eq!(status, Some(0), "{stdout}");
    assert_eq!(fs::read(out.join("romfs/example.bin")).unwrap(), b"new");
    assert!(!out.join("exefs").exists());
    fs::remove_dir_all(fixture).unwrap();
}
