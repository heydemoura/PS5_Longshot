//! A module made for hot-cold splitting ([`Module::split_cold`]): its weights and attributes.

use super::*;
use mir::{Edge, Function};

/// b0 branches on Z to b1 or b2; both return.
fn branchy() -> Function {
    let mut f = Function::new(0x8000_1000);
    let (b0, b1, b2) = (f.start, f.add_block(0x8000_1010), f.add_block(0x8000_1020));
    let z = f.push(b0, Op::Get(Loc::Flag(Flag::Z)));
    f.terminate(
        b0,
        Term::Branch { cond: z, then: Edge::new(Dest::Block(b1), false), otherwise: Edge::new(Dest::Block(b2), false) },
    );
    let x0 = f.push(b1, Op::Get(Loc::X(0)));
    f.push(b1, Op::Set(Loc::X(1), x0));
    f.terminate(b1, Term::Return);
    let x0 = f.push(b2, Op::Get(Loc::X(0)));
    f.push(b2, Op::Set(Loc::X(2), x0));
    f.terminate(b2, Term::Return);
    f.verify().unwrap();
    f
}

fn counts(blocks: &[(u64, u64)], taken: &[(u64, u64)]) -> profile::Function {
    profile::Function {
        hash: 1,
        entries: 1000,
        blocks: blocks.iter().copied().collect(),
        taken: taken.iter().copied().collect(),
    }
}

fn written(f: &Function, c: &profile::Function, split: Option<BTreeSet<u64>>) -> String {
    let mut m = Module::new(Options::default());
    if let Some(skip) = split {
        m.split_cold(skip);
    }
    m.add(f, Runs { hash: 1, counts: Some(c), cold: false });
    m.finish()
}

#[test]
fn split_a_branch_never_taken_is_weighted_zero_and_nothing_else_changes() {
    // (the branch's block ran 1000 times, never taken: no taken count, as a profile file has it)
    let c = counts(&[(0x8000_1000, 1000), (0x8000_1020, 1000)], &[]);
    let off = written(&branchy(), &c, None);
    assert!(!off.contains("i32 0, i32 1000"), "off: no weights for it, as without the switch\n{off}");
    let on = written(&branchy(), &c, Some(BTreeSet::new()));
    assert!(on.contains("!{!\"branch_weights\", i32 0, i32 1000}"), "the branch never taken:\n{on}");
    // (only that: the same text with the weights taken out)
    let node = on.lines().find(|l| l.contains("i32 0, i32 1000")).unwrap().split(" = ").next().unwrap().to_string();
    let stripped: String = on
        .lines()
        .filter(|l| !l.starts_with(&format!("{node} = ")))
        .map(|l| l.replace(&format!(", !prof {node}"), "") + "\n")
        .collect();
    assert_eq!(stripped, off);
    crate::tests::compiles(&on);
    // (left out: as off, no new zero weight)
    assert_eq!(written(&branchy(), &c, Some([0x8000_1000].into())), off);
    // (left out, but its weights without the switch are (n, 0) into a join: kept from 0 there too)
    let joined = counts(&[(0x8000_1000, 1000), (0x8000_1010, 1000), (0x8000_1020, 1000)], &[(0x8000_1000, 1000)]);
    assert!(written(&branchy(), &joined, None).contains("i32 1000, i32 0"), "off as before");
    assert!(
        written(&branchy(), &joined, Some([0x8000_1000].into())).contains("!{!\"branch_weights\", i32 1000, i32 1}")
    );
    // (taken once in 2^31 - 2: unscaled 1 to 2147483645, which LLVM rounds to its cutoff; kept to 1 to 2^30. And the
    // other way round: not taken once, kept to 2^30 to 1)
    let once = counts(&[(0x8000_1000, (1 << 31) - 2), (0x8000_1010, 1)], &[(0x8000_1000, 1)]);
    let on = written(&branchy(), &once, Some(BTreeSet::new()));
    assert!(on.contains(&format!("!{{!\"branch_weights\", i32 1, i32 {}}}", 1u64 << 30)), "{on}");
    let not_once = counts(&[(0x8000_1000, (1 << 31) - 2), (0x8000_1020, 1)], &[(0x8000_1000, (1 << 31) - 3)]);
    let on = written(&branchy(), &not_once, Some(BTreeSet::new()));
    assert!(on.contains(&format!("!{{!\"branch_weights\", i32 {}, i32 1}}", 1u64 << 30)), "{on}");
    // (a branch taken keeps its weights; a block that never ran has none, on or off)
    let taken = counts(&[(0x8000_1000, 1000)], &[(0x8000_1000, 10)]);
    assert_eq!(written(&branchy(), &taken, Some(BTreeSet::new())), written(&branchy(), &taken, None));
    let never = counts(&[(0x8000_1020, 1000)], &[]);
    assert!(!written(&branchy(), &never, Some(BTreeSet::new())).contains("i32 0, i32"));
}

#[test]
fn split_a_function_that_calls_setjmp_is_noinline() {
    let mut f = branchy();
    f.push(BlockId(1), Op::SetjmpCall { target: 0x8000_5000, rets: [0x8000_1018, 0, 0, 0] });
    f.verify().unwrap();
    let c = counts(&[(0x8000_1000, 1000), (0x8000_1020, 1000)], &[]);
    let on = written(&f, &c, Some(BTreeSet::new()));
    assert!(on.contains("(ptr noalias nocapture %c) #0 #4 !prof"), "noinline:\n{on}");
    crate::tests::compiles(&on);
    let off = written(&f, &c, None);
    assert!(off.contains("(ptr noalias nocapture %c) #0 !prof"), "off: as without the switch\n{off}");
}

#[test]
fn split_a_later_piece_s_first_block_is_written_as_off_and_a_rare_branch_keeps_a_weight_of_one() {
    // (passes::split makes a piece's first block at the function's entry address: the counts there are the entry
    // block's, so, split, its branch gets no weights, neither (0, n) from no taken count nor (n, 0) from the entry's)
    let c = counts(&[(0x8000_1000, 1000), (0x8000_1020, 1000)], &[]);
    let taken = counts(&[(0x8000_1000, 1000), (0x8000_1020, 1000)], &[(0x8000_1000, 1000)]);
    let mut piece = branchy();
    piece.pieces = Some(mir::Pieces { base: "f_80001000".into(), index: 2 });
    for p in [&c, &taken] {
        let on = written(&piece, p, Some(BTreeSet::new()));
        let branch = on.lines().find(|l| l.contains("br i1")).expect("the branch");
        assert!(!branch.contains("!prof"), "no weights: {branch}\n{on}");
    }
    // (the function left out too: its piece's first branch has no weights either)
    let on = written(&piece, &taken, Some([0x8000_1000].into()));
    let branch = on.lines().find(|l| l.contains("br i1")).expect("the branch");
    assert!(!branch.contains("!prof"), "left out, no weights: {branch}\n{on}");
    assert!(written(&piece, &c, None).lines().all(|l| !l.contains("i32 0, i32 1000")), "off as before");
    assert!(written(&piece, &taken, None).contains("i32 1000, i32 0"), "off as before: the entry's counts");
    // (a branch into a piece's first block: its counts are not its own, so it is taken to have run: weighted 1)
    let mut looped = Function::new(0x8000_1000);
    let (b0, b1, b2) = (looped.start, looped.add_block(0x8000_1010), looped.add_block(0x8000_1020));
    looped.terminate(b0, Term::Jump(Edge::new(Dest::Block(b1), false)));
    let z = looped.push(b1, Op::Get(Loc::Flag(Flag::Z)));
    looped.terminate(
        b1,
        Term::Branch { cond: z, then: Edge::new(Dest::Block(b0), false), otherwise: Edge::new(Dest::Block(b2), false) },
    );
    looped.terminate(b2, Term::Return);
    looped.pieces = Some(mir::Pieces { base: "f_80001000".into(), index: 2 });
    looped.verify().unwrap();
    let ran = counts(&[(0x8000_1010, 1000), (0x8000_1020, 1000)], &[]);
    assert!(written(&looped, &ran, Some(BTreeSet::new())).contains("!{!\"branch_weights\", i32 1, i32 1000}"));
    // (not a piece: weighted as any function)
    assert!(written(&branchy(), &c, Some(BTreeSet::new())).contains("i32 0, i32 1000"));
    // (taken once in 2^33 runs: scaled, 1 / 5 is 0; split it stays 1, the other side kept to 2^30)
    let rare = counts(&[(0x8000_1000, 1 << 33), (0x8000_1010, 1), (0x8000_1020, 1 << 33)], &[(0x8000_1000, 1)]);
    let on = written(&branchy(), &rare, Some(BTreeSet::new()));
    assert!(on.contains(&format!("!{{!\"branch_weights\", i32 1, i32 {}}}", 1 << 30)), "{on}");
    let off = written(&branchy(), &rare, None);
    assert!(off.contains("!{!\"branch_weights\", i32 0, i32"), "off as before: {off}");
}
