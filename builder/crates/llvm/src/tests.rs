//! The writer's text, and (where clang is found) that LLVM accepts it.

use super::*;
use mir::{BlockId, Edge, Function};

/// A function with a loop, a call, a store, a BLR and a tail call: every path the writer has.
fn sample() -> Function {
    let mut f = Function::new(0x8000_1000);
    let b0 = f.start;
    let b1 = f.add_block(0x8000_1004);
    let x0 = f.push(b0, Op::Get(Loc::X(0)));
    let one = f.push(b0, Op::Const { ty: Type::I64, value: 1 });
    let sum = f.push(b0, Op::Bin(BinOp::Add, x0, one));
    f.push(b0, Op::Set(Loc::X(0), sum));
    f.push(b0, Op::Store { addr: sum, value: x0, space: Space::Guest, order: None });
    f.push(b0, Op::Mark { addr: sum, bytes: 8 });
    f.push(b0, Op::Call { target: 0x8000_2000 });
    f.terminate(b0, Term::Jump(Edge::new(Dest::Block(b1), false)));
    let x1 = f.push(b1, Op::Get(Loc::X(1)));
    f.push(b1, Op::CallIndirect { target: x1, usual: None });
    let z = f.push(b1, Op::Get(Loc::Flag(Flag::Z)));
    f.terminate(
        b1,
        Term::Branch {
            cond: z,
            then: Edge::new(Dest::Block(BlockId(0)), true),
            otherwise: Edge::new(Dest::Tail(0x8000_3000), false),
        },
    );
    f.verify().unwrap();
    f
}

#[test]
fn writes_the_abi_and_the_sync() {
    let mut m = Module::new(Options::default());
    m.add(&sample(), Runs::default());
    let text = m.finish();
    assert!(text.contains("define void @f_80001000(ptr noalias nocapture %c)"), "{text}");
    assert!(text.contains("declare void @f_80002000(ptr nocapture)"));
    assert!(text.contains("musttail call void @f_80003000(ptr %c)"));
    assert!(text.contains("call void @a64_icall_site"));
    assert!(text.contains("call void @a64_poll_cur()"));
    assert!(text.contains("@a64_gpu_pages"));
    // (sync.rs: x1 is read in b1 alone, after the call: its field is read once, after the call, and never written.
    // x0 is read at entry, written before the call (dirty) and read again after the BLR, which the loop back to b0
    // reads it after; the tail call spills nothing, as nothing is dirty after the BLR. Z is read after the BLR alone)
    let field = |offset: u64| text.matches(&format!("getelementptr inbounds i8, ptr %c, i64 {offset}\n")).count();
    assert_eq!(field(8), 1, "x1 read once, after the call:\n{text}");
    assert_eq!(field(0), 3, "x0 read at entry, spilled before the call, read after the BLR:\n{text}");
    assert_eq!(field(ctx::NZCV + 1), 1, "Z read after the BLR:\n{text}");
}

#[test]
fn a_function_without_calls_reads_what_it_reads_and_writes_back_what_it_writes() {
    let mut f = Function::new(0x8000_1000);
    let b = f.start;
    let x0 = f.push(b, Op::Get(Loc::X(0)));
    f.push(b, Op::Set(Loc::X(2), x0));
    f.terminate(b, Term::Return);
    let mut m = Module::new(Options::default());
    m.add(&f, Runs::default());
    let text = m.finish();
    let field = |offset: u64| text.matches(&format!("getelementptr inbounds i8, ptr %c, i64 {offset}\n")).count();
    assert_eq!((field(0), field(16)), (1, 1), "x0 read once, x2 written once:\n{text}");
    assert_eq!(text.matches("getelementptr inbounds i8, ptr %c").count(), 2, "nothing else synced:\n{text}");
}

#[test]
fn clang_accepts_it() {
    let Some(clang) = clang() else { return };
    let mut m = Module::new(Options::default());
    m.add(&sample(), Runs::default());
    m.add(&vector_sample(), Runs::default());
    let dir = std::env::temp_dir().join(format!("longshot-builder-llvm-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let ll = dir.join("t.ll");
    std::fs::write(&ll, m.finish()).unwrap();
    let out = std::process::Command::new(clang)
        .args(["-c", "-O1", "-Wno-override-module", "-mcx16"])
        .arg(&ll)
        .arg("-o")
        .arg(dir.join("t.o"))
        .output()
        .unwrap();
    let _ = std::fs::remove_dir_all(&dir);
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
}

#[test]
fn an_instrumented_module_counts_each_block_and_branch_and_registers_them() {
    let mut m = Module::instrumented(Options::default(), "lifted_007");
    m.add(&sample(), Runs { hash: 0xabc, ..Runs::default() });
    let counted = m.counted().to_vec();
    let text = m.finish();
    // (two blocks and b1's branch)
    assert_eq!(counted.len(), 1);
    let kinds: Vec<_> = counted[0].counters.iter().map(|&(c, i)| (c, i)).collect();
    assert_eq!(
        kinds,
        [
            (profile::Counter::Block(0x8000_1000), 0),
            (profile::Counter::Block(0x8000_1004), 1),
            (profile::Counter::Taken(0x8000_1004), 2)
        ]
    );
    assert_eq!(counted[0].hash, 0xabc);
    assert!(text.contains("@lsp.counts = internal global [3 x i64]"), "{text}");
    assert!(text.contains("call void @lsp_register(ptr @lsp.counts, i64 3, ptr @lsp.unit)"), "{text}");
    assert!(text.contains("c\"lifted_007\\00\""), "{text}");
    compiles(&text);
}

#[test]
fn a_profile_gives_the_branch_its_weights_and_the_function_its_entries() {
    let mut counts = profile::Function { hash: 1, entries: 40, ..profile::Function::default() };
    counts.blocks.insert(0x8000_1004, 100);
    counts.taken.insert(0x8000_1004, 90);
    let mut m = Module::new(Options::default());
    m.add(&sample(), Runs { hash: 1, counts: Some(&counts), cold: false });
    let text = m.finish();
    assert!(text.contains("!{!\"function_entry_count\", i64 40}"), "{text}");
    assert!(text.contains("!{!\"branch_weights\", i32 90, i32 10}"), "{text}");
    compiles(&text);
    let mut cold = Module::new(Options::default());
    cold.add(&sample(), Runs { hash: 1, counts: None, cold: true });
    assert!(cold.finish().contains("(ptr noalias nocapture %c) #1 {"));
}

#[test]
fn a_branch_taken_more_often_than_its_block_ran_keeps_its_weights_in_32_bits() {
    let weights = |n: u64, t: u64| {
        let mut counts = profile::Function { hash: 1, entries: 1, ..profile::Function::default() };
        counts.blocks.insert(0x8000_1004, n);
        counts.taken.insert(0x8000_1004, t);
        let mut m = Module::new(Options::default());
        m.add(&sample(), Runs { hash: 1, counts: Some(&counts), cold: false });
        let text = m.finish();
        // (the branch's: not the polls' and the fast paths' fixed `2000, 1` and `1, 2000`)
        let w = text
            .lines()
            .filter_map(|l| l.split_once("!\"branch_weights\", i32 "))
            .map(|(_, w)| w)
            .find(|w| !w.contains("2000"))
            .expect("the branch's weights");
        let (a, b) = w.trim_end_matches('}').split_once(", i32 ").unwrap();
        let (a, b): (u64, u64) = (a.parse().unwrap(), b.parse().unwrap());
        assert!(a + b <= i32::MAX as u64, "n {n} t {t}: {a} {b}");
        (a, b)
    };
    // (a taken count past its block's, as racing counters give: the branch always taken)
    assert_eq!(weights(100, 130), (100, 0));
    // (past 32 bits, with the taken count past the block's: scaled from the larger, fitting)
    let (a, b) = weights(3 << 31, 5 << 31);
    assert!(a > 0 && b == 0);
    let (a, b) = weights(3 << 31, 1 << 31);
    assert!(a > 0 && b > a);
}

/// Where clang is found, that it compiles `text` at -O1.
/// The clang the tests compile with; a test fails without one, unless `LONGSHOT_NO_CLANG=1` says to pass over what
/// needs it.
pub(crate) fn clang() -> Option<&'static str> {
    let found = ["clang", "clang-23"]
        .into_iter()
        .find(|c| std::process::Command::new(c).arg("--version").output().is_ok_and(|o| o.status.success()));
    if found.is_none() && std::env::var("LONGSHOT_NO_CLANG").as_deref() != Ok("1") {
        panic!("no clang to compile the lifted IR with (LONGSHOT_NO_CLANG=1 passes over these checks)");
    }
    found
}

pub(crate) fn compiles(text: &str) {
    let Some(clang) = clang() else { return };
    // (a folder of each call's own: tests run at once)
    static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("longshot-builder-llvm-p{}-{n}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let ll = dir.join("t.ll");
    std::fs::write(&ll, text).unwrap();
    let out = std::process::Command::new(clang)
        .args(["-c", "-O1", "-Wno-override-module", "-mcx16"])
        .arg(&ll)
        .arg("-o")
        .arg(dir.join("t.o"))
        .output()
        .unwrap();
    let _ = std::fs::remove_dir_all(&dir);
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
}

/// A function with every float and vector operation: V registers, casts, shuffles, intrinsics and a table.
fn vector_sample() -> Function {
    use mir::{Elem, FBinOp, FCast, FPred, FUnOp, Reduce, Table, V128};
    let mut f = Function::new(0x8000_4000);
    let b = f.start;
    let v0 = f.push(b, Op::Get(Loc::V(0)));
    let v1 = f.push(b, Op::Get(Loc::V(1)));
    let f4 = Type::Vec(4, Elem::F32);
    let a = f.push(b, Op::Bitcast(f4, v0));
    let c = f.push(b, Op::Bitcast(f4, v1));
    let sum = f.push(b, Op::FBin(FBinOp::Add, a, c));
    // (a constant splat as an intrinsic's operand: written as a literal)
    let two = f.push(b, Op::Const { ty: Type::I32, value: 0x4000_0000 });
    let two = f.push(b, Op::Bitcast(Type::F32, two));
    let two = f.push(b, Op::Splat(f4, two));
    let fma = f.push(b, Op::Fma(sum, a, two));
    let root = f.push(b, Op::FUn(FUnOp::Sqrt, fma));
    let rounded = f.push(b, Op::FUn(FUnOp::RoundEven, root));
    let nan = f.push(b, Op::FCmp(FPred::Uno, rounded, rounded));
    let picked = f.push(b, Op::Select(nan, a, rounded));
    let ints = f.push(b, Op::FCast(FCast::ToSigned, Type::Vec(4, Elem::I32), picked));
    let back = f.push(b, Op::FCast(FCast::FromUnsigned, f4, ints));
    let wide = f.push(b, Op::Shuffle(back, back, vec![0, 1]));
    let wide = f.push(b, Op::FCast(FCast::Ext, Type::Vec(2, Elem::F64), wide));
    let i = f.push(b, Op::Bitcast(Type::Vec(4, Elem::I32), v0));
    let sat = f.push(b, Op::Bin(BinOp::SAddSat, i, ints));
    let not = f.push(b, Op::Un(UnOp::Not, sat));
    let pop = f.push(b, Op::Un(UnOp::Ctpop, not));
    let lane = f.push(b, Op::Extract(pop, 3));
    let total = f.push(b, Op::Reduce(Reduce::Add, pop));
    let mixed = f.push(b, Op::Bin(BinOp::Add, lane, total));
    let byte = f.push(b, Op::Cast(mir::Cast::Trunc, Type::I8, mixed));
    let looked = f.push(b, Op::Lookup(Table::AesSbox, byte));
    let splat = f.push(b, Op::Splat(Type::Vec(16, Elem::I8), looked));
    let s = f.push(b, Op::Bitcast(V128, splat));
    let d = f.push(b, Op::Bitcast(V128, wide));
    let e = f.push(b, Op::Extract(d, 0));
    let s = f.push(b, Op::Insert(s, 1, e));
    f.push(b, Op::Set(Loc::V(2), s));
    f.terminate(b, Term::Return);
    f.verify().unwrap();
    f
}

#[test]
fn writes_vectors() {
    let mut m = Module::new(Options::default());
    m.add(&vector_sample(), Runs::default());
    let text = m.finish();
    assert!(text.contains("<float 0x4000000000000000, float 0x4000000000000000"), "{text}");
    assert!(text.contains("@llvm.fptosi.sat.v4i32.v4f32"), "{text}");
    assert!(text.contains("@lifter.aes_sbox = private unnamed_addr constant [256 x i8] c\"\\63\\7C"), "{text}");
    assert!(text.contains(&format!("getelementptr inbounds i8, ptr %c, i64 {}\n", ctx::VREG + 32)), "{text}");
}

#[test]
fn a_call_gives_back_the_callee_saved_registers() {
    // x19 and x0 read after a call, nothing before it: x0 is the callee's to change, x19 is not (sync.rs)
    let mut f = Function::new(0x8000_1000);
    let b = f.start;
    f.push(b, Op::Call { target: 0x8000_2000 });
    let x19 = f.push(b, Op::Get(Loc::X(19)));
    let x0 = f.push(b, Op::Get(Loc::X(0)));
    let s = f.push(b, Op::Bin(BinOp::Add, x19, x0));
    f.push(b, Op::Set(Loc::X(1), s));
    f.terminate(b, Term::Return);
    let field =
        |text: &str, offset: u64| text.matches(&format!("getelementptr inbounds i8, ptr %c, i64 {offset}\n")).count();
    let mut m = Module::new(Options::default());
    m.add(&f, Runs::default());
    let text = m.finish();
    let entry = text.split("call void @f_80002000").next().unwrap();
    assert_eq!(field(entry, 19 * 8), 1, "x19 read before the call, at entry:\n{text}");
    assert_eq!(field(&text, 19 * 8), 1, "and not again after it:\n{text}");
    assert_eq!(field(entry, 0), 0, "x0 not read before the call:\n{text}");
    assert_eq!(field(&text, 0), 1, "x0 read after it:\n{text}");
    let mut all = Module::new(Options { callee_saved: false, ..Options::default() });
    all.add(&f, Runs::default());
    let text = all.finish();
    assert_eq!(field(&text, 19 * 8), 1, "without the rule, x19 is read after the call alone:\n{text}");
    assert_eq!(field(text.split("call void @f_80002000").next().unwrap(), 19 * 8), 0);
}

/// A function of one fast path, and its helper: `a + b` of V0's and V1's low singles, the helper's `b` when NaN.
fn checked_sample(entry: u64) -> Function {
    use mir::{FBinOp, FPred, Helper};
    let mut body = Function::new(0);
    let hb = body.start;
    let p0 = body.push(hb, Op::Param { index: 0, ty: Type::F32 });
    let p1 = body.push(hb, Op::Param { index: 1, ty: Type::F32 });
    let sum = body.push(hb, Op::FBin(FBinOp::Add, p0, p1));
    let nan = body.push(hb, Op::FCmp(FPred::Uno, sum, sum));
    let r = body.push(hb, Op::Select(nan, p1, sum));
    body.terminate(hb, Term::Return);
    let helper =
        Helper { name: "lifter.test.add".into(), params: vec![Type::F32, Type::F32], body, result: r, mid: None };

    let mut f = Function::new(entry);
    let h = f.add_helper(helper);
    let b = f.start;
    let mut single = |n: u8| {
        let v = f.push(b, Op::Get(Loc::V(n)));
        let lo = f.push(b, Op::Extract(v, 0));
        let lo = f.push(b, Op::Cast(mir::Cast::Trunc, Type::I32, lo));
        f.push(b, Op::Bitcast(Type::F32, lo))
    };
    let (x, y) = (single(0), single(1));
    let fast = f.push(b, Op::FBin(FBinOp::Add, x, y));
    let tiny = f.push(b, Op::FClass(mir::FClass::NanOrSubnormal, fast));
    let nan = f.push(b, Op::FCmp(FPred::Uno, fast, fast));
    let slow = f.push(b, Op::Bin(BinOp::Or, tiny, nan));
    let r = f.push(b, Op::Checked { fast, slow, helper: h, args: vec![x, y] });
    let bits = f.push(b, Op::Bitcast(Type::I32, r));
    let bits = f.push(b, Op::Cast(mir::Cast::Zext, Type::I64, bits));
    let zero = f.push(b, Op::Const { ty: Type::I128, value: 0 });
    let zero = f.push(b, Op::Bitcast(mir::V128, zero));
    let v = f.push(b, Op::Insert(zero, 0, bits));
    f.push(b, Op::Set(Loc::V(2), v));
    f.terminate(b, Term::Return);
    f.verify().unwrap();
    f
}

/// A helper of `params`, its body `f(params' values)` (one block).
fn helper_of(
    name: &str,
    params: &[Type],
    body_of: impl FnOnce(&mut Function, mir::BlockId, &[mir::Value]) -> mir::Value,
) -> mir::Helper {
    let mut body = Function::new(0);
    let b = body.start;
    let ps: Vec<mir::Value> =
        params.iter().enumerate().map(|(i, &ty)| body.push(b, Op::Param { index: i as u8, ty })).collect();
    let result = body_of(&mut body, b, &ps);
    body.terminate(b, Term::Return);
    mir::Helper { name: name.into(), params: params.to_vec(), body, result, mid: None }
}

/// A function of one float add of `ty` (a single, or a vector of two: V0's and V1's low bits) with its slow path, the
/// helper's (`lifter.fp.add.*`: its second operand where the sum is a NaN), and with `mid` a middle path (its test: any
/// subnormal; its value: the sum's first operand where it is a NaN). What they compute does not matter here: how the
/// writer calls them does.
fn mid_sample(entry: u64, ty: Type, mid: bool) -> Function {
    use mir::{FBinOp, FClass, FPred};
    let spell = if ty == Type::F32 { "f32" } else { "v2f32" };
    let params = [ty, ty, Type::I64];
    let nan_or = |which: usize| {
        move |f: &mut Function, b: mir::BlockId, p: &[mir::Value]| {
            let sum = f.push(b, Op::FBin(FBinOp::Add, p[0], p[1]));
            let nan = f.push(b, Op::FCmp(FPred::Uno, sum, sum));
            f.push(b, Op::Select(nan, p[which], sum))
        }
    };
    let mut helper = helper_of(&format!("lifter.fp.add.{spell}.{spell}.i64"), &params, nan_or(1));
    if mid {
        let test = helper_of(&format!("lifter.fp.mid.subnormal.{spell}.{spell}.{spell}"), &[ty, ty, ty], |f, b, p| {
            let mut any = None;
            for &x in p {
                let t = f.push(b, Op::FClass(FClass::Subnormal, x));
                let t = if ty.is_vector() { f.push(b, Op::Reduce(mir::Reduce::Or, t)) } else { t };
                any = Some(any.map_or(t, |a| f.push(b, Op::Bin(BinOp::Or, a, t))));
            }
            any.unwrap()
        });
        let value = helper_of(&format!("lifter.fp.mid.add.{spell}.{spell}.i64"), &params, nan_or(0));
        helper.mid = Some(Box::new(mir::Mid { test, value }));
    }
    let mut f = Function::new(entry);
    let h = f.add_helper(helper);
    let b = f.start;
    let mut operand = |n: u8| {
        let v = f.push(b, Op::Get(Loc::V(n)));
        let lo = f.push(b, Op::Extract(v, 0));
        if ty == Type::F32 {
            let lo = f.push(b, Op::Cast(mir::Cast::Trunc, Type::I32, lo));
            f.push(b, Op::Bitcast(Type::F32, lo))
        } else {
            f.push(b, Op::Bitcast(ty, lo))
        }
    };
    let (x, y) = (operand(0), operand(1));
    let fpcr = f.push(b, Op::Get(Loc::Fpcr));
    let fast = f.push(b, Op::FBin(FBinOp::Add, x, y));
    let tiny = f.push(b, Op::FClass(FClass::NanOrSubnormal, fast));
    let slow = if ty.is_vector() { f.push(b, Op::Reduce(mir::Reduce::Or, tiny)) } else { tiny };
    let r = f.push(b, Op::Checked { fast, slow, helper: h, args: vec![x, y, fpcr] });
    let bits = f.push(b, Op::Bitcast(if ty == Type::F32 { Type::I32 } else { Type::I64 }, r));
    let bits = if ty == Type::F32 { f.push(b, Op::Cast(mir::Cast::Zext, Type::I64, bits)) } else { bits };
    let zero = f.push(b, Op::Const { ty: Type::I128, value: 0 });
    let zero = f.push(b, Op::Bitcast(mir::V128, zero));
    let v = f.push(b, Op::Insert(zero, 0, bits));
    f.push(b, Op::Set(Loc::V(2), v));
    f.terminate(b, Term::Return);
    f.verify().unwrap();
    f
}

#[test]
#[cfg(debug_assertions)]
#[should_panic(expected = "two helpers named lifter.test.k compute differently")]
fn two_helpers_of_one_name_computing_differently_stop_the_writer() {
    // (a name is written once: a second function's helper of that name and another body would be written as the first)
    let with = |entry: u64, value: u128| {
        let helper = helper_of("lifter.test.k", &[], |f, b, _| {
            let k = f.push(b, Op::Const { ty: Type::I32, value });
            f.push(b, Op::Bitcast(Type::F32, k))
        });
        let mut f = Function::new(entry);
        let h = f.add_helper(helper);
        let b = f.start;
        let x0 = f.push(b, Op::Get(Loc::X(0)));
        let lo = f.push(b, Op::Cast(mir::Cast::Trunc, Type::I32, x0));
        let fast = f.push(b, Op::Bitcast(Type::F32, lo));
        let slow = f.push(b, Op::FCmp(mir::FPred::Uno, fast, fast));
        let r = f.push(b, Op::Checked { fast, slow, helper: h, args: vec![] });
        let bits = f.push(b, Op::Bitcast(Type::I32, r));
        let bits = f.push(b, Op::Cast(mir::Cast::Zext, Type::I64, bits));
        f.push(b, Op::Set(Loc::X(1), bits));
        f.terminate(b, Term::Return);
        f.verify().unwrap();
        f
    };
    let mut m = Module::new(Options::default());
    m.add(&with(0x8000_9000, 0x3f80_0000), Runs::default());
    m.add(&with(0x8000_a000, 0x3f80_0000), Runs::default());
    m.add(&with(0x8000_b000, 0x4000_0000), Runs::default());
}

#[test]
fn a_fast_path_whose_helper_takes_no_arguments_is_written() {
    // (the operands a middle path tests are taken only for one: a helper of no parameters is no float operation's,
    // and the writer must not read past its arguments)
    let helper = helper_of("lifter.test.k", &[], |f, b, _| {
        let k = f.push(b, Op::Const { ty: Type::I32, value: 0x3f80_0000 });
        f.push(b, Op::Bitcast(Type::F32, k))
    });
    let mut f = Function::new(0x8000_9000);
    let h = f.add_helper(helper);
    let b = f.start;
    let x0 = f.push(b, Op::Get(Loc::X(0)));
    let lo = f.push(b, Op::Cast(mir::Cast::Trunc, Type::I32, x0));
    let fast = f.push(b, Op::Bitcast(Type::F32, lo));
    let slow = f.push(b, Op::FCmp(mir::FPred::Uno, fast, fast));
    let r = f.push(b, Op::Checked { fast, slow, helper: h, args: vec![] });
    let bits = f.push(b, Op::Bitcast(Type::I32, r));
    let bits = f.push(b, Op::Cast(mir::Cast::Zext, Type::I64, bits));
    f.push(b, Op::Set(Loc::X(1), bits));
    f.terminate(b, Term::Return);
    f.verify().unwrap();
    let mut m = Module::new(Options::default());
    m.add(&f, Runs::default());
    let text = m.finish();
    assert!(text.contains("call float @lifter.test.k()"), "{text}");
    compiles(&text);
}

#[test]
fn a_middle_path_is_tested_then_inline_and_the_helper_called_only_past_it() {
    for ty in [Type::F32, Type::Vec(2, mir::Elem::F32)] {
        let t = llvm_type(ty);
        let spell = if ty == Type::F32 { "f32" } else { "v2f32" };
        // (two functions with the middle path, one without it, in one module: each helper written once)
        let mut m = Module::new(Options::default());
        m.add(&mid_sample(0x8000_5000, ty, true), Runs::default());
        m.add(&mid_sample(0x8000_6000, ty, true), Runs::default());
        m.add(&mid_sample(0x8000_7000, ty, false), Runs::default());
        let text = m.finish();
        let test = format!("@lifter.fp.mid.subnormal.{spell}.{spell}.{spell}");
        let value = format!("@lifter.fp.mid.add.{spell}.{spell}.i64");
        let full = format!("@lifter.fp.add.{spell}.{spell}.i64");
        assert_eq!(text.matches(&format!("define internal i1 {test}({t} %p0, {t} %p1, {t} %p2) #5 {{")).count(), 1);
        assert_eq!(text.matches(&format!("define internal {t} {value}({t} %p0, {t} %p1, i64 %p2) #5 {{")).count(), 1);
        assert_eq!(text.matches(&format!("define internal {t} {full}({t} %p0, {t} %p1, i64 %p2) #2 {{")).count(), 1);
        assert_eq!(text.matches(&format!("= call i1 {test}(")).count(), 2, "{text}");
        assert_eq!(text.matches(&format!("= call {t} {value}(")).count(), 2, "{text}");
        assert_eq!(text.matches(&format!("= call {t} {full}(")).count(), 3, "{text}");
        assert!(text.contains("attributes #5 = { nounwind willreturn alwaysinline memory(none) }"), "{text}");
        assert!(text.contains("attributes #6 = { nounwind willreturn memory(none) }"), "{text}");
        // (the test reads the operands and the fast value through an empty asm, in a vector register: a single as it
        // is, a vector of two singles as a double)
        let carrier = if ty == Type::F32 { "float" } else { "double" };
        assert_eq!(text.matches(&format!("= call {carrier} asm \"\", \"=x,0\"({carrier} ")).count(), 6, "{text}");
        // (three ways into the join with the middle path, two without)
        // (in each function with it: the test, then the branch to the full path or the middle one; the middle one's
        // value and then the join, with no call of the helper on the way; the helper only on the full path)
        for entry in ["80005000", "80006000"] {
            let body = text.split(&format!("@f_{entry}(")).nth(1).unwrap().split("\n}").next().unwrap();
            let at = |what: &str| body.find(what).unwrap_or_else(|| panic!("{what} in f_{entry}:\n{body}"));
            let (test_at, value_at, full_at) =
                (at(&format!("call i1 {test}(")), at(&format!("call {t} {value}(")), at(&format!("call {t} {full}(")));
            assert!(test_at < value_at && value_at < full_at, "{body}");
            let mid_block = &body[test_at..full_at];
            let (branch, rest) = mid_block.split_once("\n  br i1 ").unwrap();
            assert!(!branch.contains(&full) && !rest.contains(&full), "{body}");
            // (`br i1 %test, label %<full>, label %<middle>`: the test true takes the helper)
            let labels: Vec<&str> =
                rest.lines().next().unwrap().split("label %").skip(1).map(|l| l.split(',').next().unwrap()).collect();
            let [full_label, mid_label] = labels[..] else { panic!("{body}") };
            // (a block: its label's line to the next label's; it ends in its branch)
            let block = |label: &str| -> Vec<&str> {
                let from = body.split(&format!("\n{label}:\n")).nth(1).unwrap_or_else(|| panic!("{label}:\n{body}"));
                from.lines().take_while(|l| !l.ends_with(':')).collect()
            };
            let (mid_block, full_block) = (block(mid_label), block(full_label));
            // (the middle path: its value, no helper's call, and straight to the join, where the phi takes it)
            assert!(mid_block.iter().any(|l| l.contains(&format!("call {t} {value}("))), "{mid_block:?}");
            assert!(!mid_block.iter().any(|l| l.contains(&full)), "{mid_block:?}");
            let join =
                mid_block.last().unwrap().strip_prefix("  br label %").unwrap_or_else(|| panic!("{mid_block:?}"));
            assert!(!join.contains(' '), "{mid_block:?}");
            assert!(full_block.iter().any(|l| l.contains(&format!("call {t} {full}("))), "{full_block:?}");
            assert_eq!(full_block.last().unwrap(), &format!("  br label %{join}"), "{full_block:?}");
            let phi = block(join).into_iter().find(|l| l.contains("= phi ")).unwrap();
            // (and the values: the branch on the test's result, the phi taking the middle value from the middle block
            // and the helper's from the full one)
            let result_of = |lines: &[&str], callee: &str| -> String {
                let l = lines.iter().find(|l| l.contains(&format!(" {callee}("))).unwrap();
                l.trim().split(" = ").next().unwrap().to_string()
            };
            let test_line = body.lines().find(|l| l.contains(&format!("call i1 {test}("))).unwrap();
            let test_result = test_line.trim().split(" = ").next().unwrap();
            assert!(rest.starts_with(&format!("{test_result}, label %")), "{rest}");
            let (m, r) = (result_of(&mid_block, &value), result_of(&full_block, &full));
            assert!(phi.contains(&format!("[ {m}, %{mid_label} ]")), "{phi}");
            assert!(phi.contains(&format!("[ {r}, %{full_label} ]")), "{phi}");
            // (what the test reads: through the empty asm, the helper's operands (its arguments but FPCR) and the fast
            // value (the phi's first), in that order; a vector of two singles through a double, there and back)
            let full_line = full_block.iter().find(|l| l.contains(&full)).unwrap();
            let mut expect: Vec<String> = full_line
                .split('(')
                .nth(1)
                .unwrap()
                .split(", ")
                .take(2)
                .map(|a| a.rsplit(' ').next().unwrap().to_string())
                .collect();
            expect.push(phi.split("[ ").nth(1).unwrap().split(',').next().unwrap().to_string());
            let slow: Vec<&str> = body[..test_at].lines().rev().take_while(|l| !l.ends_with(':')).collect();
            let slow: Vec<&str> = slow.into_iter().rev().collect();
            let mut read = Vec::new();
            for l in slow.iter().filter(|l| l.contains("asm \"\", \"=x,0\"")) {
                let input = l.split("=x,0\"(").nth(1).unwrap().split(')').next().unwrap();
                let input = input.rsplit(' ').next().unwrap().to_string();
                let out = l.trim().split(" = ").next().unwrap();
                // (through a bitcast: its source, and the cast back of the asm's result)
                let source = slow
                    .iter()
                    .find(|b| b.trim().starts_with(&format!("{input} = bitcast ")))
                    .map(|b| b.split(" to ").next().unwrap().rsplit(' ').next().unwrap().to_string());
                let given = slow
                    .iter()
                    .find(|b| b.contains(&format!(" bitcast {carrier} {out} to ")))
                    .map(|b| b.trim().split(" = ").next().unwrap().to_string());
                read.push((source.unwrap_or(input), given.unwrap_or(out.to_string())));
            }
            assert_eq!(read.iter().map(|r| r.0.clone()).collect::<Vec<_>>(), expect, "{body}");
            let test_args: Vec<String> = test_line
                .split('(')
                .nth(1)
                .unwrap()
                .trim_end_matches(')')
                .split(", ")
                .map(|a| a.rsplit(' ').next().unwrap().to_string())
                .collect();
            assert_eq!(test_args, read.iter().map(|r| r.1.clone()).collect::<Vec<_>>(), "{body}");
        }
        let phis: Vec<&str> = text.lines().filter(|l| l.contains(&format!("= phi {t} "))).collect();
        assert_eq!(phis.len(), 3, "{text}");
        assert_eq!(phis.iter().filter(|l| l.matches("], [").count() == 2).count(), 2, "{phis:?}");
        compiles(&text);
        // (a module without a middle path has none of its text)
        let mut plain = Module::new(Options { count_checks: true, ..Options::default() });
        plain.add(&mid_sample(0x8000_7000, ty, false), Runs::default());
        let plain = plain.finish();
        for absent in ["lifter.fp.mid", "#5", "#6", "asm", "lifter_mid_checks"] {
            assert!(!plain.contains(absent), "{absent}: {plain}");
        }
        // (counted: the middle path's runs too)
        let mut counted = Module::new(Options { count_checks: true, ..Options::default() });
        counted.add(&mid_sample(0x8000_5000, ty, true), Runs::default());
        let text = counted.finish();
        assert!(text.contains("@lifter_mid_checks = external global i64"), "{text}");
        assert_eq!(text.matches("load i64, ptr @lifter_mid_checks").count(), 1, "{text}");
        compiles(&text);
    }
}

#[test]
fn a_fast_path_branches_around_its_helper_written_once_per_module() {
    let mut m = Module::new(Options::default());
    m.add(&checked_sample(0x8000_5000), Runs::default());
    m.add(&checked_sample(0x8000_6000), Runs::default());
    let text = m.finish();
    assert_eq!(text.matches("define internal float @lifter.test.add(float %p0, float %p1) #2 {").count(), 1, "{text}");
    assert_eq!(text.matches("call float @lifter.test.add(float %v").count(), 2, "{text}");
    assert!(text.contains("@llvm.is.fpclass.f32(float %v"), "{text}");
    assert!(text.contains("phi float [ %v"), "{text}");
    assert!(text.contains("!{!\"branch_weights\", i32 1, i32 2000}"), "{text}");
    assert!(text.contains("attributes #2 = { nounwind willreturn noinline cold optsize memory(none) }"), "{text}");
    compiles(&text);
    let mut counted = Module::new(Options { count_checks: true, ..Options::default() });
    counted.add(&checked_sample(0x8000_5000), Runs::default());
    let text = counted.finish();
    assert!(text.contains("load i64, ptr @lifter_slow_checks"), "{text}");
    compiles(&text);
}

#[test]
fn a_piece_edge_that_polls_polls() {
    // (a piece whose back edge to its own loop head, in another piece, is a polled piece edge)
    for poll in [true, false] {
        let mut f = Function::new(0x8000_1000);
        f.pieces = Some(mir::Pieces { base: "f_80001000".into(), index: 1 });
        let b0 = f.start;
        let x0 = f.push(b0, Op::Get(Loc::X(0)));
        f.push(b0, Op::Set(Loc::X(1), x0));
        f.terminate(b0, Term::Jump(Edge::new(Dest::Piece { piece: 2, entry: 0 }, poll)));
        f.verify().unwrap();
        let mut m = Module::new(Options::default());
        m.add(&f, Runs::default());
        // (the other piece, in the same module in a lift)
        let text = m.finish() + "declare void @f_80001000.p2(ptr)\n";
        let polls = text.contains("call void @a64_poll_cur()");
        assert_eq!(polls, poll, "{text}");
        assert!(text.contains("musttail call void @f_80001000.p2(ptr %c)"), "{text}");
        compiles(&text);
    }
}

#[test]
fn a_mark_on_the_gpu_bitmap_asks_for_both_its_pages() {
    // (a shared mark spans up to 4 KB: it may start on a page the GPU does not map and end on one it does)
    let mut f = Function::new(0x8000_1000);
    let b0 = f.start;
    let a = f.push(b0, Op::Get(Loc::X(0)));
    f.push(b0, Op::Mark { addr: a, bytes: 4096 });
    f.terminate(b0, Term::Return);
    f.verify().unwrap();
    let mut m = Module::new(Options { mark_bitmap: true, ..Options::default() });
    m.add(&f, Runs::default());
    let text = m.finish();
    assert_eq!(text.matches("getelementptr inbounds i8, ptr @a64_gpu_pages").count(), 2, "{text}");
    assert_eq!(text.matches("ptr @a64_dirty, i64").count(), 2, "one mark of two pages: {text}");
    compiles(&text);
}

#[test]
fn a_thread_local_global_is_asked_for_at_each_access() {
    // (two reads with a call between, where the fiber may move to another host thread: two asks of the accessor,
    // which LLVM cannot see into)
    let mut f = Function::new(0x8000_1000);
    let b0 = f.start;
    let g = mir::Global::thread_local("hos_tls_thing");
    f.push(b0, Op::HostLoad { global: g.clone(), ty: Type::I32 });
    f.push(b0, Op::Call { target: 0x8000_2000 });
    f.push(b0, Op::HostLoad { global: g, ty: Type::I32 });
    f.terminate(b0, Term::Return);
    f.verify().unwrap();
    let mut m = Module::new(Options::default());
    m.add(&f, Runs::default());
    let text = m.finish();
    assert_eq!(text.matches("= call ptr @tls.hos_tls_thing()").count(), 2, "{text}");
    assert!(text.contains("define weak hidden ptr @tls.hos_tls_thing() noinline"), "{text}");
    let body = &text[text.find("define void @f_80001000").unwrap()..];
    assert!(!body[..body.find("\n}").unwrap()].contains("llvm.threadlocal.address"), "{text}");
    compiles(&text);
}

#[test]
fn a_splat_wider_than_128_bits_is_written_lane_by_lane() {
    // (SQRDMULH's form: the 4 x 32 products widened to 4 x i64, a <4 x i64> splat of its rounding 1 << 30 added and
    // shifted by a splat of 31. Every lane is the splat's value, the lanes past 128 bits too)
    use mir::{BinOp, Cast, Elem};
    let mut f = Function::new(0x8000_1000);
    let b0 = f.start;
    let (i4, l4) = (Type::Vec(4, Elem::I32), Type::Vec(4, Elem::I64));
    let v = f.push(b0, Op::Get(Loc::V(0)));
    let v = f.push(b0, Op::Bitcast(i4, v));
    let wide = f.push(b0, Op::Cast(Cast::Sext, l4, v));
    let product = f.push(b0, Op::Bin(BinOp::Mul, wide, wide));
    let round = f.push(b0, Op::Const { ty: Type::I64, value: 1 << 30 });
    let round = f.push(b0, Op::Splat(l4, round));
    let sum = f.push(b0, Op::Bin(BinOp::Add, product, round));
    let by = f.push(b0, Op::Const { ty: Type::I64, value: 31 });
    let by = f.push(b0, Op::Splat(l4, by));
    let high = f.push(b0, Op::Bin(BinOp::AShr, sum, by));
    let narrow = f.push(b0, Op::Cast(Cast::Trunc, i4, high));
    let back = f.push(b0, Op::Bitcast(mir::V128, narrow));
    f.push(b0, Op::Set(Loc::V(0), back));
    f.terminate(b0, Term::Return);
    f.verify().unwrap();
    let mut m = Module::new(Options::default());
    m.add(&f, Runs::default());
    let text = m.finish();
    assert!(text.contains("<i64 1073741824, i64 1073741824, i64 1073741824, i64 1073741824>"), "{text}");
    assert!(text.contains("<i64 31, i64 31, i64 31, i64 31>"), "{text}");
    compiles(&text);
}

#[test]
fn a_nan_constant_is_defined_with_its_payload() {
    // (a float NaN with a payload, a bitcast of an integer constant, and a splat of it: each defined, the payload kept)
    let mut f = Function::new(0x8000_1000);
    let b0 = f.start;
    let int = f.push(b0, Op::Const { ty: Type::I32, value: 0x7fc0_1234 });
    let nan = f.push(b0, Op::Bitcast(Type::F32, int));
    let lanes = f.push(b0, Op::Splat(Type::Vec(4, mir::Elem::F32), nan));
    let v = f.push(b0, Op::Bitcast(mir::V128, lanes));
    f.push(b0, Op::Set(Loc::V(0), v));
    let back = f.push(b0, Op::Bitcast(Type::I32, nan));
    let wide = f.push(b0, Op::Cast(mir::Cast::Zext, Type::I64, back));
    f.push(b0, Op::Set(Loc::X(0), wide));
    f.terminate(b0, Term::Return);
    f.verify().unwrap();
    let mut m = Module::new(Options::default());
    m.add(&f, Runs::default());
    let text = m.finish();
    for x in [nan, lanes] {
        assert!(text.contains(&format!("%v{} = ", x.0)), "%v{} defined: {text}", x.0);
    }
    assert!(text.contains(&format!("bitcast i32 {} to float", 0x7fc0_1234)), "{text}");
    compiles(&text);
}
