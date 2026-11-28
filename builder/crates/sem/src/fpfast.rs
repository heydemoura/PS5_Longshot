//! The float operations' fast paths: the host's IEEE operation inline, and the exact semantics (fp.rs) out of line.
//!
//! **Why it is exact.** The lifted code runs on x86-64 under the host's default MXCSR: round to nearest, DAZ and FTZ
//! clear, every exception masked (nothing in the host changes it; host/hos_setjmp.c saves and restores it as it is).
//! There, an IEEE operation's result is the Arm ARM's for the same operands under this model (fp.rs) except
//! - where it is a NaN: Arm's NaN is chosen from the operands (or is the default NaN, under DN or for an invalid
//!   operation), the host's is not; every NaN operand makes a NaN result, so testing the result covers them;
//! - under FPCR.FZ, where an operand or the result is subnormal (FZ flushes single and double precision only).
//!
//! So each operation is an [`Op::Checked`]: its fast value the host's operation, its slow condition a NaN result or a
//! subnormal operand or result (in any lane of a vector), and its helper the exact semantics, written once per module
//! and called with the operands and FPCR. FPCR.DN changes only NaN results: the helper reads it.
//!
//! The check does not read FZ: a subnormal takes the slow path with FZ clear too, where the fast value would have
//! been right. That is rare (and slow on the host's own float unit anyway), and it keeps the check short: one
//! `is.fpclass` per operand, one for the result (NaN or subnormal), ORed, and for a vector one reduction.
//!
//! **The middle path** ([`nan_mid`], in a lift's hot units by default). A vector with a NaN in one lane takes the
//! slow path at every operation, the helper's whole computation for every lane. For FPAdd, FPSub, FPMul, FPDiv,
//! FPMulX and FPMulAdd the helper is: flush(operands), the host's operation, Arm's NaN, flush(result) (fp.rs). Where
//! no operand lane and no lane of the fast value is subnormal, every flush is the identity whatever FZ says: an
//! operand is not changed, the operation sees the fast path's own operands and makes the fast value, and the result
//! the NaN choice makes is a NaN or the fast value, neither of them subnormal. So there the helper's value is the
//! same computation with FZ read as clear: the NaN choice (and FPMulX's infinity times zero, FPMulAdd's quiet NaN
//! addend with infinity times zero) applied to the host's operation. The pass gives those helpers a middle path
//! ([`mir::Mid`]): on the slow path, a test of the operands and the fast value for a subnormal lane (the fast path's
//! own tests, of the values themselves, so it holds whatever another pass made of the fast path's condition); none:
//! the exact semantics with FZ clear ([`Sem::exact`]), inline; one: the helper, as without it.

use crate::Sem;
use crate::fp::Env;
use mir::{BinOp, FBinOp, FClass, FPred, FUnOp, Function, Helper, HelperId, Loc, Op, Reduce, Term, Type, Value};

/// A float operation with a fast path: what [`Sem::fp`] computes.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub(crate) enum Fast {
    /// FPAdd, FPSub, FPMul, FPDiv of `[a, b]` ([`Sem::farith`]).
    Arith(FBinOp),
    /// FPMulX of `[a, b]`.
    MulX,
    /// FPMulAdd of `[addend, a, b]`.
    MulAdd,
    /// FPMax, FPMin, FPMaxNum, FPMinNum of `[a, b]`.
    MinMax {
        /// The larger (else the smaller).
        max: bool,
        /// The Num forms.
        nm: bool,
    },
    /// FPSqrt of `[x]`.
    Sqrt,
    /// FPRoundInt of `[x]` with this rounding.
    Rint(FUnOp),
    /// FPConvert of `[x]` to this float type.
    Convert(Type),
    /// FPToFixed of `[x]` ([`Sem::ftoint`]'s operands).
    ToInt {
        /// The fraction bits.
        fbits: u32,
        /// The rounding (`None`: towards zero).
        round: Option<FUnOp>,
        /// The integer type.
        to: Type,
        /// Unsigned (else signed).
        unsigned: bool,
    },
    /// FRECPS or (`rsqrt`) FRSQRTS of `[n, m]`.
    Step {
        /// FRSQRTS.
        rsqrt: bool,
    },
}

/// A type as a helper's name spells it: f32, v4f32, i64.
fn spell(ty: Type) -> String {
    let scalar = |t: Type| if t.is_float() { format!("f{}", t.bits()) } else { format!("i{}", t.bits()) };
    match ty {
        Type::Vec(n, e) => format!("v{n}{}", scalar(e.scalar())),
        t => scalar(t),
    }
}

fn unop_name(op: FUnOp) -> &'static str {
    match op {
        FUnOp::Sqrt => "sqrt",
        FUnOp::RoundEven => "roundeven",
        FUnOp::Round => "round",
        FUnOp::Floor => "floor",
        FUnOp::Ceil => "ceil",
        FUnOp::Trunc => "trunc",
    }
}

impl Fast {
    /// Its helper's name for operands of `params` (the operands', then FPCR's): every field of it is in the name, so
    /// that two helpers of one name compute the same.
    pub(crate) fn helper_name(self, params: &[Type]) -> String {
        let what = match self {
            Fast::Arith(op) => format!("{op:?}").to_lowercase(),
            Fast::MulX => "mulx".into(),
            Fast::MulAdd => "muladd".into(),
            Fast::MinMax { max, nm } => format!("{}{}", if max { "max" } else { "min" }, if nm { "nm" } else { "" }),
            Fast::Sqrt => "sqrt".into(),
            Fast::Rint(op) => format!("rint.{}", unop_name(op)),
            Fast::Convert(to) => format!("cvt.{}", spell(to)),
            Fast::ToInt { fbits, round, to, unsigned } => format!(
                "toint.{}.{}.{fbits}.{}",
                spell(to),
                if unsigned { "u" } else { "s" },
                round.map_or("zero", unop_name)
            ),
            Fast::Step { rsqrt } => if rsqrt { "rsqrts" } else { "recps" }.into(),
        };
        let params: Vec<String> = params.iter().map(|&t| spell(t)).collect();
        format!("lifter.fp.{what}.{}", params.join("."))
    }
}

impl Sem<'_> {
    /// `kind` of `ops`: the host's operation unless its result is a NaN or an operand or the result is subnormal,
    /// else the exact semantics' (an out-of-line helper's).
    pub(crate) fn fp(&mut self, kind: Fast, ops: &[Value]) -> Value {
        let fpcr = self.get(Loc::Fpcr);
        let fast = self.quick(kind, ops);
        // (the values FZ would flush: single and double operands, and the result where it can be subnormal)
        let flushable = |s: &Self, x: Value| matches!(s.ty(x).elem(), Type::F32 | Type::F64);
        let result_flushed = flushable(self, fast)
            && match kind {
                Fast::Arith(_) | Fast::MulX | Fast::MulAdd | Fast::Step { .. } => true,
                Fast::Convert(to) => to.elem().bits() < self.ty(ops[0]).elem().bits(),
                Fast::MinMax { .. } | Fast::Sqrt | Fast::Rint(_) | Fast::ToInt { .. } => false,
            };
        let mut tests = Vec::new();
        for &x in ops {
            if flushable(self, x) {
                tests.push(self.op(Op::FClass(FClass::Subnormal, x)));
            }
        }
        if result_flushed {
            tests.push(self.op(Op::FClass(FClass::NanOrSubnormal, fast)));
        } else if self.ty(fast).is_float() {
            tests.push(self.op(Op::FCmp(FPred::Uno, fast, fast)));
        }
        let Some((&first, rest)) = tests.split_first() else { return fast };
        let mut any = first;
        for &t in rest {
            any = self.bin(BinOp::Or, any, t);
        }
        let slow = self.any_lane(any);
        let helper = self.fp_helper(kind, ops);
        let mut args = ops.to_vec();
        args.push(fpcr);
        self.op(Op::Checked { fast, slow, helper, args })
    }

    /// Whether any lane of the lane mask `m` is set (`m` itself, of a scalar).
    fn any_lane(&mut self, m: Value) -> Value {
        if self.ty(m).is_vector() { self.op(Op::Reduce(Reduce::Or, m)) } else { m }
    }

    /// `kind`'s fast value: the host's operation, the exact one's wherever [`Sem::fp`]'s check passes.
    fn quick(&mut self, kind: Fast, ops: &[Value]) -> Value {
        match kind {
            Fast::Arith(op) => self.host_bin(op, ops[0], ops[1]),
            Fast::MulX => self.host_bin(FBinOp::Mul, ops[0], ops[1]),
            Fast::MulAdd => self.op(Op::Fma(ops[1], ops[2], ops[0])),
            Fast::MinMax { max, .. } => {
                // (without NaNs FPMaxNum is FPMax, and FPMax is IEEE's maximum: −0 below +0)
                let op = if max { FBinOp::Maximum } else { FBinOp::Minimum };
                self.host_bin(op, ops[0], ops[1])
            }
            Fast::Sqrt => self.host_un(FUnOp::Sqrt, ops[0]),
            Fast::Rint(op) => self.host_un(op, ops[0]),
            Fast::Convert(to) => self.fcast_float(ops[0], to),
            Fast::ToInt { fbits, round, to, unsigned } => self.ftoint_core(ops[0], fbits, round, to, unsigned),
            Fast::Step { rsqrt } => {
                let neg = self.fneg(ops[0]);
                self.step_core(neg, ops[1], rsqrt)
            }
        }
    }

    /// `kind`'s exact semantics (fp.rs, simd.rs), under `env`.
    fn exact(&mut self, env: Env, kind: Fast, ops: &[Value]) -> Value {
        match kind {
            Fast::Arith(op) => self.farith(env, op, ops[0], ops[1]),
            Fast::MulX => self.fmulx(env, ops[0], ops[1]),
            Fast::MulAdd => self.fmuladd(env, ops[0], ops[1], ops[2]),
            Fast::MinMax { max, nm } => self.fminmax(env, ops[0], ops[1], max, nm),
            Fast::Sqrt => self.fsqrt(env, ops[0]),
            Fast::Rint(op) => self.frint(env, op, ops[0]),
            Fast::Convert(to) => self.fconvert(env, ops[0], to),
            Fast::ToInt { fbits, round, to, unsigned } => self.ftoint(env, ops[0], fbits, round, to, unsigned),
            Fast::Step { rsqrt } => self.step(env, ops[0], ops[1], rsqrt),
        }
    }

    /// The helper computing `kind` of operands like `ops` exactly, from them and FPCR: the function's own, made once.
    fn fp_helper(&mut self, kind: Fast, ops: &[Value]) -> HelperId {
        let mut params: Vec<Type> = ops.iter().map(|&x| self.ty(x)).collect();
        params.push(Type::I64);
        let name = kind.helper_name(&params);
        if let Some(id) = self.f.helper_named(&name) {
            return id;
        }
        let mut body = Function::new(0);
        let b = body.start;
        let values: Vec<Value> =
            params.iter().enumerate().map(|(i, &ty)| body.push(b, Op::Param { index: i as u8, ty })).collect();
        let (args, fpcr) = values.split_at(ops.len());
        let mut s = Sem::new(&mut body, b, self.pc);
        let env = s.env_of(fpcr[0]);
        let result = s.exact(env, kind, args);
        body.terminate(b, Term::Return);
        self.f.add_helper(Helper { name, params, body, result, mid: None })
    }
}

/// The kinds a middle path is made for (see the module's comment): the operations whose exact semantics are flush,
/// the host's operation, the NaN choice, flush.
const MID_KINDS: [Fast; 6] = [
    Fast::Arith(FBinOp::Add),
    Fast::Arith(FBinOp::Sub),
    Fast::Arith(FBinOp::Mul),
    Fast::Arith(FBinOp::Div),
    Fast::MulX,
    Fast::MulAdd,
];

/// What [`nan_mid`] did to a function: its fast paths, and those whose helper has a middle path now.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct NanMidStats {
    /// The function's fast paths ([`Op::Checked`]).
    pub checks: usize,
    /// Those of them whose slow path has a middle path.
    pub mid: usize,
}

impl std::ops::AddAssign for NanMidStats {
    fn add_assign(&mut self, o: NanMidStats) {
        self.checks += o.checks;
        self.mid += o.mid;
    }
}

/// The middle path given to `f`'s helpers of FPAdd, FPSub, FPMul, FPDiv, FPMulX and FPMulAdd (the module's comment):
/// on the slow path, where no operand lane and no lane of the fast value is subnormal, the exact semantics with FZ
/// read as clear, inline; else the helper. Nothing else of `f` changes.
pub fn nan_mid(f: &mut Function) -> NanMidStats {
    give_mid(f, false)
}

/// [`nan_mid`] with a wrong test, for tests/test_lifter_semantics.py's negative control only: the operands' lanes are
/// not tested, the fast value's alone, so a subnormal operand beside a NaN lane takes the middle path and is not
/// flushed under FZ.
#[doc(hidden)]
pub fn nan_mid_broken(f: &mut Function) -> NanMidStats {
    give_mid(f, true)
}

fn give_mid(f: &mut Function, broken: bool) -> NanMidStats {
    let found: Vec<(HelperId, Fast)> = f
        .helpers()
        .filter(|(_, h)| h.mid.is_none())
        .filter_map(|(id, h)| MID_KINDS.iter().find(|k| k.helper_name(&h.params) == h.name).map(|&k| (id, k)))
        .collect();
    for (id, kind) in found {
        let h = f.helper(id);
        let (params, ret) = (h.params.clone(), h.ret());
        let what = h.name.strip_prefix("lifter.fp.").expect("a float helper's name");
        let mid = mir::Mid { test: subnormal_test(&params, ret, broken), value: mid_value(kind, what, &params) };
        f.helper_mut(id).mid = Some(Box::new(mid));
    }
    let mut stats = NanMidStats::default();
    for (_, b) in f.blocks() {
        for &v in &b.insts {
            if let Op::Checked { helper, .. } = f.inst(v).op {
                stats.checks += 1;
                stats.mid += usize::from(f.helper(helper).mid.is_some());
            }
        }
    }
    stats
}

/// The middle path's test: whether a lane of an operand (`params` but the last, FPCR) or of the fast value (of `ret`)
/// is subnormal, of the single and double ones (FZ flushes no half). `broken`: the fast value's lanes alone.
fn subnormal_test(params: &[Type], ret: Type, broken: bool) -> Helper {
    let tested: Vec<Type> = params[..params.len() - 1].iter().copied().chain([ret]).collect();
    let spelled: Vec<String> = tested.iter().map(|&t| spell(t)).collect();
    let name = format!("lifter.fp.mid.{}.{}", if broken { "subnormal-broken" } else { "subnormal" }, spelled.join("."));
    let mut body = Function::new(0);
    let b = body.start;
    let values: Vec<Value> =
        tested.iter().enumerate().map(|(i, &ty)| body.push(b, Op::Param { index: i as u8, ty })).collect();
    let mut s = Sem::new(&mut body, b, 0);
    let mut any = None;
    for (i, &x) in values.iter().enumerate() {
        let flushable = matches!(tested[i].elem(), Type::F32 | Type::F64);
        if !flushable || (broken && i + 1 < values.len()) {
            continue;
        }
        let t = s.op(Op::FClass(FClass::Subnormal, x));
        let t = s.any_lane(t);
        any = Some(match any {
            None => t,
            Some(a) => s.bin(BinOp::Or, a, t),
        });
    }
    let result = match any {
        Some(a) => a,
        None => s.konst(Type::I1, 0),
    };
    body.terminate(b, Term::Return);
    Helper { name, params: tested, body, result, mid: None }
}

/// The middle path's value: `kind`'s exact semantics of `params` (the operands, then FPCR) with FZ read as clear, DN
/// as FPCR has it. `what`: the helper's name past `lifter.fp.`.
fn mid_value(kind: Fast, what: &str, params: &[Type]) -> Helper {
    let mut body = Function::new(0);
    let b = body.start;
    let values: Vec<Value> =
        params.iter().enumerate().map(|(i, &ty)| body.push(b, Op::Param { index: i as u8, ty })).collect();
    let (args, fpcr) = values.split_at(params.len() - 1);
    let mut s = Sem::new(&mut body, b, 0);
    let (fz, dn) = (s.konst(Type::I1, 0), s.bit(fpcr[0], 25));
    let result = s.exact(Env { fz, dn }, kind, args);
    body.terminate(b, Term::Return);
    Helper { name: format!("lifter.fp.mid.{what}"), params: params.to_vec(), body, result, mid: None }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_differ_where_the_computations_do() {
        let p = [Type::F32, Type::F32, Type::I64];
        let kinds = [
            Fast::Arith(FBinOp::Add),
            Fast::Arith(FBinOp::Sub),
            Fast::Arith(FBinOp::Mul),
            Fast::Arith(FBinOp::Div),
            Fast::MulX,
            Fast::MulAdd,
            Fast::MinMax { max: true, nm: false },
            Fast::MinMax { max: false, nm: false },
            Fast::MinMax { max: true, nm: true },
            Fast::MinMax { max: false, nm: true },
            Fast::Sqrt,
            Fast::Rint(FUnOp::Floor),
            Fast::Rint(FUnOp::Ceil),
            Fast::Convert(Type::F64),
            Fast::Convert(Type::F16),
            Fast::ToInt { fbits: 0, round: None, to: Type::I32, unsigned: false },
            Fast::ToInt { fbits: 0, round: None, to: Type::I32, unsigned: true },
            Fast::ToInt { fbits: 3, round: None, to: Type::I32, unsigned: true },
            Fast::ToInt { fbits: 3, round: Some(FUnOp::Floor), to: Type::I32, unsigned: true },
            Fast::ToInt { fbits: 3, round: Some(FUnOp::Floor), to: Type::I64, unsigned: true },
            Fast::Step { rsqrt: false },
            Fast::Step { rsqrt: true },
        ];
        let names: std::collections::BTreeSet<String> = kinds.iter().map(|k| k.helper_name(&p)).collect();
        assert_eq!(names.len(), kinds.len());
        let v = [Type::Vec(4, mir::Elem::F32), Type::Vec(4, mir::Elem::F32), Type::I64];
        assert_ne!(Fast::Sqrt.helper_name(&p), Fast::Sqrt.helper_name(&v));
        assert_eq!(Fast::Arith(FBinOp::Add).helper_name(&v), "lifter.fp.add.v4f32.v4f32.i64");
    }

    /// A function of these words, one after another, lowered as the lift does (no passes).
    fn lowered(words: &[u32]) -> Function {
        let mut f = Function::new(0x8000_0000);
        let b = f.start;
        for (i, &w) in words.iter().enumerate() {
            let insn = a64::decode(w).unwrap_or_else(|| panic!("{w:08x} decodes"));
            crate::lower(&mut f, b, 0x8000_0000 + 4 * i as u64, &insn).unwrap_or_else(|e| panic!("{w:08x}: {e:?}"));
        }
        f.terminate(b, Term::Return);
        f.verify().unwrap();
        f
    }

    /// The names of `f`'s helpers with a middle path, and those without.
    fn with_and_without(f: &Function) -> (Vec<String>, Vec<String>) {
        let (mut with, mut without) = (Vec::new(), Vec::new());
        for (_, h) in f.helpers() {
            if h.mid.is_some() { &mut with } else { &mut without }.push(h.name.clone());
        }
        (with, without)
    }

    #[test]
    fn the_middle_path_is_given_to_add_sub_mul_div_mulx_and_muladd_alone() {
        let words = [
            0x4e22_d420, // fadd v0.4s, v1.4s, v2.4s
            0x4ea2_d420, // fsub v0.4s, v1.4s, v2.4s
            0x1e22_0820, // fmul s0, s1, s2
            0x1e62_1820, // fdiv d0, d1, d2
            0x4e22_dc20, // fmulx v0.4s, v1.4s, v2.4s
            0x4e62_cc20, // fmla v0.2d, v1.2d, v2.2d
            0x1ee2_2820, // fadd h0, h1, h2
            0x1e21_c020, // fsqrt s0, s1
            0x1e22_4820, // fmax s0, s1, s2
            0x4e22_fc20, // frecps v0.4s, v1.4s, v2.4s
            0x1e22_c020, // fcvt d0, s1
        ];
        let mut f = lowered(&words);
        let before = f.to_string();
        let stats = nan_mid(&mut f);
        f.verify().unwrap();
        assert_eq!(stats, NanMidStats { checks: 11, mid: 7 });
        let (with, without) = with_and_without(&f);
        assert_eq!(
            with,
            [
                "lifter.fp.add.v4f32.v4f32.i64",
                "lifter.fp.sub.v4f32.v4f32.i64",
                "lifter.fp.mul.f32.f32.i64",
                "lifter.fp.div.f64.f64.i64",
                "lifter.fp.mulx.v4f32.v4f32.i64",
                "lifter.fp.muladd.v2f64.v2f64.v2f64.i64",
                "lifter.fp.add.f16.f16.i64",
            ]
        );
        assert_eq!(
            without,
            [
                "lifter.fp.sqrt.f32.i64",
                "lifter.fp.max.f32.f32.i64",
                "lifter.fp.recps.v4f32.v4f32.i64",
                "lifter.fp.cvt.f64.f32.i64"
            ]
        );
        // (nothing but the helpers changes: the function's blocks are the same text)
        let blocks = |t: &str| t.split("\nh0 ").next().unwrap().to_string();
        assert_eq!(blocks(&f.to_string()), blocks(&before));
        let mid = |name: &str| f.helper(f.helper_named(name).unwrap()).mid.clone().unwrap();
        let m = mid("lifter.fp.muladd.v2f64.v2f64.v2f64.i64");
        assert_eq!(m.test.name, "lifter.fp.mid.subnormal.v2f64.v2f64.v2f64.v2f64");
        assert_eq!(m.value.name, "lifter.fp.mid.muladd.v2f64.v2f64.v2f64.i64");
        // (the test classifies each single or double it is given, the operands and the fast value; FZ flushes no
        // half, so a half's test is false)
        let classes = |h: &Helper| {
            h.body
                .blocks()
                .flat_map(|(_, b)| b.insts.clone())
                .filter(|&v| matches!(h.body.inst(v).op, Op::FClass(..)))
                .count()
        };
        assert_eq!(classes(&m.test), 4);
        assert_eq!(classes(&mid("lifter.fp.mul.f32.f32.i64").test), 3);
        let half = mid("lifter.fp.add.f16.f16.i64").test;
        assert_eq!(classes(&half), 0);
        assert!(matches!(half.body.inst(half.result).op, Op::Const { ty: Type::I1, value: 0 }));
        // (the value reads FPCR's DN and not its FZ: the same computation of the same operands with FZ clear; FPCR is
        // its last parameter, a bit of it read as a shift right by the bit's number)
        let value = &m.value;
        let body = &value.body;
        let fpcr: Vec<Value> = body
            .blocks()
            .flat_map(|(_, b)| b.insts.clone())
            .filter(|&v| matches!(body.inst(v).op, Op::Param { index: 3, .. }))
            .collect();
        assert!(!fpcr.is_empty(), "FPCR, the fourth parameter");
        // (every use of FPCR, of any node of it, is that shift, by 25: no other read of it, a mask or a compare, can
        // carry FZ)
        let uses: Vec<Option<u128>> = body
            .blocks()
            .flat_map(|(_, b)| b.insts.clone())
            .filter(|&v| body.inst(v).op.operands().iter().any(|x| fpcr.contains(x)))
            .map(|v| match body.inst(v).op {
                Op::Bin(BinOp::LShr, x, k) if fpcr.contains(&x) => match body.inst(k).op {
                    Op::Const { value, .. } => Some(value),
                    _ => None,
                },
                _ => None,
            })
            .collect();
        assert_eq!(uses, [Some(25)], "FPCR read for DN (bit 25) alone, not FZ (bit 24)");
        // (again: nothing more)
        let again = f.clone();
        assert_eq!(nan_mid(&mut f), NanMidStats { checks: 11, mid: 7 });
        assert_eq!(f, again);
    }

    #[test]
    fn the_broken_middle_path_tests_the_fast_value_alone() {
        let mut f = lowered(&[0x4e22_cc20]); // fmla v0.4s, v1.4s, v2.4s
        assert_eq!(nan_mid_broken(&mut f), NanMidStats { checks: 1, mid: 1 });
        f.verify().unwrap();
        let (_, h) = f.helpers().next().unwrap();
        let test = &h.mid.as_ref().unwrap().test;
        assert_eq!(test.name, "lifter.fp.mid.subnormal-broken.v4f32.v4f32.v4f32.v4f32");
        let classes = test.body.blocks().flat_map(|(_, b)| b.insts.clone());
        assert_eq!(classes.filter(|&v| matches!(test.body.inst(v).op, Op::FClass(..))).count(), 1);
    }
}
