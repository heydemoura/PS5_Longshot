// (longshot) HMMA.16816.F16 in software: D(16x8) = A(16x16) * B(16x8) + C on the subgroup's lanes, for GPUs without
// cooperative matrices (AMD before RDNA 3, the Steam Deck among them; GTX 10 and 16; Intel's integrated GPUs). The 32 lanes'
// fragments (PTX mma.m16n8k16's layout: lane t, g = t/4, i = t%4) are exchanged through subgroup operations; the products are
// accumulated with FMA in f32 and rounded to f16 after every 8 columns of K, as HMMA.F16 does. One function, called for each
// HMMA_nx2 (emit_spirv_warp.cpp EmitMatrixMulAddF16), and built the same way by the device's self-test (vk_coopmat.cpp), which
// is why it takes a plain Sirit module and the types it needs rather than the translator's EmitContext.
//
// The forms (the first three compute the same words):
// - Loop: Lichii's (zortos293/Lichii, emit_spirv_tensor.cpp, GPL-3.0). The 8 steps (4 K-pairs in each half of K) in a loop the
//   driver is asked not to unroll; A and B from other lanes through shuffles, the f16 rounding in integer operations.
// - Unrolled: the steps written out (in a loop that runs once: see below). A lane's rows of A are in its own quad (lanes 4g..4g+3), so A comes through quad broadcasts
//   (DPP on AMD, not the LDS crossbar a shuffle takes), and A's lane indices are constants (B's are still the lane's
//   warp_base + col_base + c). The rounding to f16 is either the same
//   integer operations or the GPU's own f32 -> f16 -> f32 conversion (round to nearest even, f16 subnormals kept). A conversion
//   applied to an FMA's result may be folded into it by the driver (RADV: v_fma_mixlo_f16), which rounds the exact sum once
//   instead of rounding it to f32 first: a tie in f32 then goes the other way (1 word in about 17,000 of random data). The value
//   is passed through a shuffle with lane xor 0 (the same lane) before the conversion, which keeps them apart; RADV folds that
//   shuffle into the conversion as a DPP identity, so it costs nothing. vk_coopmat.cpp runs both forms on the device and uses the
//   unrolled one only where its words are the loop's. The shuffle keeps them apart only while the driver keeps a shuffle by
//   xor 0: one that folds it away rounds once again, and the tie inputs then make the self-test keep the loop (slower, said
//   only in the log).
// - Dot2: UnrolledF16 with A's and B's words left packed, two f16 a word, and each K pair one OpFDot2MixAcc32VALVE
//   (VK_VALVE_shader_mixed_float_dot_product) into the f32 sum: on RADV v_dot2c_f32_f16 with A's quad broadcast folded into it as
//   DPP, 32 a multiply where UnrolledF16 has 64 v_fma_mix_f32 and 16 DPP moves (ACO's listing on the Steam Deck: the self-test's
//   bench body 172 lines -> 133). Not always the loop's words: v_dot2 rounds a pair's two products and their sum once, the loop
//   after each product, so a pair's partial sum differs from the loop's by at most one f32 rounding (of that sum). That is not a
//   bound in f16 ulps of the result: where the K group's later terms cancel most of the sum, the difference can be many f16 ulps,
//   and where the sum lands beside an f16 tie it rounds the other way (the self-test's double-rounding warps find that every
//   time). vk_coopmat.cpp uses it where, on the self-test's fragments, every word is within 2 f16 ulps of the loop's, same sign,
//   NaN and infinity where the loop's are: a lane or layout mistake is off by whole values, not ulps. That the game's pictures do
//   not change is for an image comparison against the loop, not for this check. The loop is not the console's rounding either:
//   a tensor core adds a group's products before rounding, as v_dot2 does.
//
// And B either way through the subgroup (16 shuffles a multiply: on AMD 16 ds_bpermute_b32, the largest cost left once Dot2
// has halved the multiply-adds) or, with staged (SoftwareMmaStagedB), through a 256-byte tile of workgroup memory per subgroup:
// each lane writes its two words (8 adjacent bytes) and reads the 16 its columns need, which are four aligned 16-byte runs
// (lanes 8i..8i+7 hold columns 2i and 2i + 1), so 2 stores and 4 loads instead of 16 shuffles, with the same words: only where
// the bytes move changes, not the arithmetic. A subgroup barrier (no workgroup barrier: the tile is the subgroup's own, and
// HMMA runs in any control flow the warp is converged in) orders the writes before the reads, and the previous multiply's reads
// before them. A subgroup barrier needs the whole subgroup there, converged: with a lane missing (a partial subgroup) or one
// that has exited or taken another branch it is undefined, as the WMMA16 tiles' barriers are (mma_layout.h
// mma_wmma16_full_subgroups). So only for compute programs whose subgroups are 32 lanes and full (computeFullSubgroups,
// mma_software_full_subgroups; emit_spirv_warp.cpp), called where the guest's warp is converged as HMMA requires, and only where
// the device's self-test gives the unstaged form's words (vk_coopmat.cpp).
//
// On the Steam Deck (RADV), one multiply a lane over a 1280x800 dispatch: 0.469 ms looped, 0.363 ms unrolled with the integer
// rounding, 0.177 ms unrolled with the conversion (a fully fused conversion, not the same words, 0.153 ms). OoT's gameplay network
// at Resolution 100% (9dc98871, Kokiri Forest): 8.61 ms a frame looped, 3.82 ms unrolled with the conversion.
#pragma once

#include <array>
#include <cstdint>

#include <sirit/sirit.h>

namespace Shader::Backend::SPIRV {

enum class SoftwareMmaForm : uint32_t {
    Loop = 0,          // (Lichii's: the steps in a loop, shuffles, integer rounding)
    Unrolled = 1,      // (written out, A through quad broadcasts, integer rounding)
    UnrolledF16 = 2,   // (written out, A through quad broadcasts, the GPU's f16 conversion rounds)
    Dot2 = 3,          // (UnrolledF16 with each K pair one f16 pair dot product into f32, OpFDot2MixAcc32VALVE)
};

/// (longshot) The Dot2 form's OpFDot2MixAcc32VALVE needs these (VK_VALVE_shader_mixed_float_dot_product's
/// shaderMixedFloatDotProductFloat16AccFloat32). As numbers: the SPIR-V headers in use do not have them yet
inline constexpr spv::Capability kDotProductFloat16AccFloat32VALVE = static_cast<spv::Capability>(6912);
inline constexpr const char* kMixedFloatDotProductExtension = "SPV_VALVE_mixed_float_dot_product";

struct SoftwareMmaTypes {
    Sirit::Id u32, u32x2, u32x4, f32, f32x2, f16, boolean;
    Sirit::Id f16x2{};   // (the Dot2 form's: a word's two f16 as the dot product takes them)
};

/// (longshot) B through workgroup memory (see above): tile is a Workgroup array variable holding 16 u32x4 a subgroup, subgroup_id
/// the SubgroupId input. The subgroups must be 32 lanes and full; the caller adds the variables to the entry point
struct SoftwareMmaStagedB {
    Sirit::Id tile, subgroup_id;
    Sirit::Id u32_pointer, u32x4_pointer;   // (Workgroup pointers)
};
/// (longshot) A subgroup's tile: its 32 lanes' two B words each, as u32x4
inline constexpr uint32_t kSoftwareMmaTileVectors = 16;
inline constexpr uint32_t kSoftwareMmaTileBytes = kSoftwareMmaTileVectors * 16;
/// (longshot) B through workgroup memory's indices, written once for the emitter (Ops over SPIR-V ids, DefineSoftwareMma) and for
/// tests/test_mma_software.py (Ops over plain numbers: every lane must read the words the shuffles hand it). The subgroup's tile
/// starts at u32x4 SubgroupId * kSoftwareMmaTileVectors; lane l stores its B word w at u32x4 tile + l / 2, component 2 (l % 2) + w;
/// it loads its four runs j at u32x4 tile + 4 (l % 4) + j; and B's word from lane 8 (l % 4) + from (from 0-7), half upper, is
/// component software_tile_component(from, upper) of run software_tile_run(from). Ops: imm, add, and_, shl, shr
template <class Ops, class V>
V software_tile_base(Ops& o, V subgroup_id) { return o.shl(subgroup_id, 4); }
template <class Ops, class V>
V software_tile_own(Ops& o, V tile, V lane) { return o.add(tile, o.shr(lane, 1)); }
template <class Ops, class V>
V software_tile_half(Ops& o, V lane) { return o.shl(o.and_(lane, 1), 1); }
template <class Ops, class V>
V software_tile_columns(Ops& o, V tile, V lane) { return o.add(tile, o.shl(o.and_(lane, 3), 2)); }
constexpr uint32_t software_tile_run(uint32_t from) { return from / 2; }
constexpr uint32_t software_tile_component(uint32_t from, uint32_t upper) { return from % 2 * 2 + upper; }

/// The function (A: 4 words, B: 2, C: 2) -> D: 2 words, as the console's fragments hold them, two f16 a word. lane_variable is the
/// SubgroupLocalInvocationId input; quad: OpGroupNonUniformQuadBroadcast may be used (the caller adds GroupNonUniformQuad), else
/// A's lanes are shuffled to. The caller adds GroupNonUniform and GroupNonUniformShuffle.
inline Sirit::Id DefineSoftwareMma(Sirit::Module& m, const SoftwareMmaTypes& t, Sirit::Id lane_variable, SoftwareMmaForm form,
                                   bool quad, const SoftwareMmaStagedB* staged = nullptr) {
    using Sirit::Id;
    const Id u32ty = t.u32, f32ty = t.f32;
    const auto imm = [&](uint32_t v) { return m.Constant(u32ty, v); };
    const auto add = [&](Id a, Id b) { return m.OpIAdd(u32ty, a, b); };
    const auto and_ = [&](Id a, uint32_t b) { return m.OpBitwiseAnd(u32ty, a, imm(b)); };
    const auto unpack = [&](Id v) { return m.OpUnpackHalf2x16(t.f32x2, v); };
    const auto round_half = [&](Id value) {
        // Do not express the intermediate rounding as unpack(pack(value)): drivers may
        // eliminate that round trip. Round normal significands explicitly, ties to even;
        // half subnormals lie on the exactly representable 2^-24 grid.
        const Id bits = m.OpBitcast(u32ty, value);
        const Id magnitude = and_(bits, 0x7fffffffU);
        const Id tie = and_(m.OpShiftRightLogical(u32ty, magnitude, imm(13)), 1);
        const Id rounded = and_(add(add(magnitude, imm(0xfff)), tie), ~0x1fffU);
        const Id normal = m.OpSelect(u32ty, m.OpUGreaterThanEqual(t.boolean, rounded, imm(0x47800000)), imm(0x7f800000), rounded);
        const Id scaled = m.OpFMul(f32ty, m.OpBitcast(f32ty, magnitude), m.Constant(f32ty, 16777216.0f));
        const Id subnormal = m.OpBitcast(u32ty, m.OpFMul(f32ty, m.OpRoundEven(f32ty, scaled), m.Constant(f32ty, 0x1p-24f)));
        const Id finite = m.OpSelect(u32ty, m.OpUGreaterThanEqual(t.boolean, magnitude, imm(0x38800000)), normal, subnormal);
        const Id result = m.OpSelect(u32ty, m.OpUGreaterThanEqual(t.boolean, magnitude, imm(0x7f800000)), magnitude, finite);
        return m.OpBitcast(f32ty, m.OpBitwiseOr(u32ty, result, and_(bits, 0x80000000)));
    };
    const auto round_half_f16 = [&](Id value) {
        // (through the same lane first: the conversion is then not folded into the FMA that made the value, see above)
        const Id same = m.OpBitcast(f32ty, m.OpGroupNonUniformShuffleXor(u32ty, imm(3), m.OpBitcast(u32ty, value), imm(0)));
        const Id half = m.OpFConvert(t.f16, same);
        m.Decorate(half, spv::Decoration::NoContraction);
        const Id back = m.OpFConvert(f32ty, half);
        m.Decorate(back, spv::Decoration::NoContraction);
        return back;
    };

    const Id type = m.TypeFunction(t.u32x2, t.u32x4, t.u32x2, t.u32x2);
    const Id fn = m.OpFunction(t.u32x2, spv::FunctionControlMask::DontInline, type);
    const Id matrix_a = m.OpFunctionParameter(t.u32x4);
    const Id matrix_b = m.OpFunctionParameter(t.u32x2);
    const Id matrix_c = m.OpFunctionParameter(t.u32x2);
    m.AddLabel();
    const bool loop = form == SoftwareMmaForm::Loop;
    const bool dot2 = form == SoftwareMmaForm::Dot2;
    if (loop) staged = nullptr;   // (the loop keeps Lichii's shuffles)
    // (a function's variables come first in its first block; the counter first in either form, as in Lichii's)
    const Id counter = m.AddLocalVariable(m.TypePointer(spv::StorageClass::Function, u32ty), spv::StorageClass::Function);
    Id result{};
    std::array<Id, 4> accumulators{};
    if (!loop)
        result = m.AddLocalVariable(m.TypePointer(spv::StorageClass::Function, t.u32x2), spv::StorageClass::Function);
    else
        for (auto& acc : accumulators)
            acc = m.AddLocalVariable(m.TypePointer(spv::StorageClass::Function, f32ty), spv::StorageClass::Function);
    std::array<Id, 8> inputs;
    for (uint32_t i = 0; i < 4; ++i) inputs[i] = m.OpCompositeExtract(u32ty, matrix_a, i);
    for (uint32_t i = 0; i < 2; ++i) {
        inputs[4 + i] = m.OpCompositeExtract(u32ty, matrix_b, i);
        inputs[6 + i] = m.OpCompositeExtract(u32ty, matrix_c, i);
    }
    // (the lane's warp, its quad and its B lanes' base; read where Lichii's function reads them, so that the loop's SPIR-V is
    // the same as before word for word)
    Id warp_base{}, row_base{}, col_base{};
    const auto read_lane = [&] {
        const Id lane = m.OpLoad(u32ty, lane_variable);
        warp_base = and_(lane, ~31U);
        row_base = and_(lane, 28);
        col_base = m.OpShiftLeftLogical(u32ty, and_(lane, 3), imm(3));
    };
    const auto subgroup = [&] { return imm(3); };   // (spv::Scope::Subgroup)
    // acc[row * 2 + col] += A[row's register].half * B[col's lane].half, each half of the pair in turn (NoContraction: each FMA
    // rounds to f32 as the CPU's would)
    const auto multiply_add = [&](Id value, Id as, Id bs) {
        for (uint32_t half = 0; half < 2; ++half) {
            value = m.OpFma(f32ty, m.OpCompositeExtract(f32ty, as, half), m.OpCompositeExtract(f32ty, bs, half), value);
            m.Decorate(value, spv::Decoration::NoContraction);
        }
        return value;
    };
    // (staged: the subgroup's tile, word 2 l + w lane l's B word w, so lanes 8i..8i+7, the ones this lane's columns 2i and 2i + 1
    // come from, are its u32x4 4i..4i+3: those four, in order. The barrier before the stores keeps the previous multiply's loads
    // before them)
    struct TileOps {   // (software_tile_*'s arithmetic as SPIR-V)
        Sirit::Module& m;
        Id u32;
        Id imm(uint32_t v) { return m.Constant(u32, v); }
        Id add(Id a, Id b) { return m.OpIAdd(u32, a, b); }
        Id and_(Id a, uint32_t b) { return m.OpBitwiseAnd(u32, a, imm(b)); }
        Id shl(Id a, uint32_t b) { return m.OpShiftLeftLogical(u32, a, imm(b)); }
        Id shr(Id a, uint32_t b) { return m.OpShiftRightLogical(u32, a, imm(b)); }
    };
    const auto load_b_from_tile = [&] {
        TileOps o{m, u32ty};
        const Id semantics = imm(0x108);   // (AcquireRelease | WorkgroupMemory)
        const auto barrier = [&] { m.OpControlBarrier(subgroup(), subgroup(), semantics); };
        const auto element = [&](Id pointer, Id index, auto... rest) {
            return m.OpAccessChain(pointer, staged->tile, index, rest...);
        };
        const Id lane = and_(m.OpLoad(u32ty, lane_variable), 31);
        const Id tile = software_tile_base(o, m.OpLoad(u32ty, staged->subgroup_id));
        const Id own = software_tile_own(o, tile, lane);
        const Id half = software_tile_half(o, lane);
        barrier();
        for (uint32_t w = 0; w < 2; ++w) m.OpStore(element(staged->u32_pointer, own, add(half, imm(w))), inputs[4 + w]);
        barrier();
        const Id columns = software_tile_columns(o, tile, lane);
        std::array<Id, 4> runs;
        for (uint32_t j = 0; j < 4; ++j) runs[j] = m.OpLoad(t.u32x4, element(staged->u32x4_pointer, add(columns, imm(j))));
        return runs;
    };

    if (!loop) {
        read_lane();
        std::array<Id, 4> acc;
        for (uint32_t row = 0; row < 2; ++row) {
            const Id c = unpack(inputs[6 + row]);
            for (uint32_t col = 0; col < 2; ++col) acc[row * 2 + col] = m.OpCompositeExtract(f32ty, c, col);
        }
        // (the body in a loop that runs once, which the driver is asked not to unroll. Mesa inlines every call, and with the bodies
        // side by side its CSE shares the shuffles of an operand that several multiplies take: fewer of them, but each held until
        // its last use. OoT's gameplay network then took 256 VGPRs (4 waves a SIMD on the Steam Deck, not 8) and the 3/4 one spilled
        // 181 to scratch; in a loop of their own they keep the loop form's 128. On the Deck, at 1280x800: 3.82 ms a frame in a loop,
        // 4.60 ms without)
        m.OpStore(counter, imm(0));
        const Id header = m.OpLabel(), body = m.OpLabel(), next = m.OpLabel(), end = m.OpLabel();
        m.OpBranch(header);
        m.AddLabel(header);
        const Id trip = m.OpLoad(u32ty, counter);
        const Id more = m.OpULessThan(t.boolean, trip, imm(1));
        m.OpLoopMerge(end, next, spv::LoopControlMask::DontUnroll);
        m.OpBranchConditional(more, body, end);
        m.AddLabel(body);
        const std::array<Id, 4> b_runs = staged ? load_b_from_tile() : std::array<Id, 4>{};
        // (Dot2: the words stay packed, A's and B's K pair low half first in both, so one dot product is the pair's two
        // multiply-adds; the others unpack them to f32 and add each product in turn)
        const auto pair = [&](Id word) { return dot2 ? m.OpBitcast(t.f16x2, word) : unpack(word); };
        for (uint32_t upper = 0; upper < 2; ++upper) {   // (K 0-7, then 8-15: B's word and A's two of that half)
            for (uint32_t component = 0; component < 4; ++component) {   // (the K pair 2 * component of that half)
                std::array<Id, 2> bs;   // (B's column 2i + col, from lane 8i + component + 4 col)
                for (uint32_t col = 0; col < 2; ++col) {
                    const uint32_t from = component + col * 4;
                    bs[col] = pair(staged ? m.OpCompositeExtract(u32ty, b_runs[software_tile_run(from)], software_tile_component(from, upper))
                                          : m.OpGroupNonUniformShuffle(u32ty, subgroup(), inputs[4 + upper],
                                                                       add(warp_base, add(col_base, imm(from)))));
                }
                for (uint32_t row = 0; row < 2; ++row) {   // (A's row g + 8 row, from lane 4g + component: this lane's quad)
                    const Id a = inputs[upper * 2 + row];
                    const Id as = pair(quad ? m.OpGroupNonUniformQuadBroadcast(u32ty, subgroup(), a, imm(component))
                                            : m.OpGroupNonUniformShuffle(u32ty, subgroup(), a, add(warp_base, add(row_base, imm(component)))));
                    for (uint32_t col = 0; col < 2; ++col)
                        acc[row * 2 + col] = dot2 ? m.OpFDot2MixAcc32VALVE(f32ty, as, bs[col], acc[row * 2 + col])
                                                  : multiply_add(acc[row * 2 + col], as, bs[col]);
                }
            }
            // HMMA.F16 rounds after each K=8 group, including the midpoint of K=16 (Dot2 with the conversion, as UnrolledF16).
            for (auto& v : acc) v = form == SoftwareMmaForm::Unrolled ? round_half(v) : round_half_f16(v);
        }
        std::array<Id, 2> packed;
        for (uint32_t row = 0; row < 2; ++row)
            packed[row] = m.OpPackHalf2x16(u32ty, m.OpCompositeConstruct(t.f32x2, acc[row * 2], acc[row * 2 + 1]));
        m.OpStore(result, m.OpCompositeConstruct(t.u32x2, packed[0], packed[1]));
        m.OpBranch(next);
        m.AddLabel(next);
        m.OpStore(counter, add(trip, imm(1)));
        m.OpBranch(header);
        m.AddLabel(end);
        m.OpReturnValue(m.OpLoad(t.u32x2, result));
        m.OpFunctionEnd();
        return fn;
    }

    for (uint32_t row = 0; row < 2; ++row) {
        const Id c = unpack(inputs[6 + row]);
        for (uint32_t col = 0; col < 2; ++col) m.OpStore(accumulators[row * 2 + col], m.OpCompositeExtract(f32ty, c, col));
    }
    m.OpStore(counter, imm(0));
    read_lane();
    const Id header = m.OpLabel(), body = m.OpLabel(), next = m.OpLabel(), end = m.OpLabel();
    m.OpBranch(header);
    m.AddLabel(header);
    const Id pair = m.OpLoad(u32ty, counter);
    const Id more = m.OpULessThan(t.boolean, pair, imm(8));
    m.OpLoopMerge(end, next, spv::LoopControlMask::DontUnroll);
    m.OpBranchConditional(more, body, end);
    m.AddLabel(body);
    const Id upper = m.OpUGreaterThanEqual(t.boolean, pair, imm(4));
    const Id component = and_(pair, 3);
    const Id a_lane = add(warp_base, add(row_base, component));
    const Id b_lane = add(warp_base, add(col_base, component));
    const Id b = m.OpSelect(u32ty, upper, inputs[5], inputs[4]);
    std::array<Id, 2> bs;
    for (uint32_t col = 0; col < 2; ++col)
        bs[col] = unpack(m.OpGroupNonUniformShuffle(u32ty, subgroup(), b, add(b_lane, imm(col * 4))));
    const Id round_now = m.OpIEqual(t.boolean, component, imm(3));
    for (uint32_t row = 0; row < 2; ++row) {
        const Id a = m.OpSelect(u32ty, upper, inputs[row + 2], inputs[row]);
        const Id as = unpack(m.OpGroupNonUniformShuffle(u32ty, subgroup(), a, a_lane));
        for (uint32_t col = 0; col < 2; ++col)
            m.OpStore(accumulators[row * 2 + col], multiply_add(m.OpLoad(f32ty, accumulators[row * 2 + col]), as, bs[col]));
    }
    // HMMA.F16 rounds after each K=8 group, including the midpoint of K=16.
    // OpSelect evaluates both operands: keep the expensive half conversion in
    // a branch so the other three pairs do no rounding work. The counter is
    // identical in every participating lane and all shuffles precede this branch.
    const Id round_block = m.OpLabel(), round_merge = m.OpLabel();
    m.OpSelectionMerge(round_merge, spv::SelectionControlMask::DontFlatten);
    m.OpBranchConditional(round_now, round_block, round_merge);
    m.AddLabel(round_block);
    for (const Id accumulator : accumulators) m.OpStore(accumulator, round_half(m.OpLoad(f32ty, accumulator)));
    m.OpBranch(round_merge);
    m.AddLabel(round_merge);
    m.OpBranch(next);
    m.AddLabel(next);
    m.OpStore(counter, add(pair, imm(1)));
    m.OpBranch(header);
    m.AddLabel(end);
    std::array<Id, 2> packed;
    for (uint32_t row = 0; row < 2; ++row)
        packed[row] = m.OpPackHalf2x16(u32ty, m.OpCompositeConstruct(t.f32x2, m.OpLoad(f32ty, accumulators[row * 2]),
                                                                     m.OpLoad(f32ty, accumulators[row * 2 + 1])));
    m.OpReturnValue(m.OpCompositeConstruct(t.u32x2, packed[0], packed[1]));
    m.OpFunctionEnd();
    return fn;
}

} // namespace Shader::Backend::SPIRV
