// SPDX-License-Identifier: GPL-3.0-or-later
// The Lens of Truth eye pass (ir_opt/lens_eye_pass.h) on synthetic fragment programs: the game's eye edge, 0.9 - 0.9 (1.26984131 x)^2,
// takes the picture's shape over 1.4 from the camera bank's projection, and its place in half heights (3.55555558 u - 1.77777779,
// -1.77777779 on the edge's slope) that shape (16:9 the game's constants again, 2.37 wider, a projection with no sane shape the
// game's constants); the shapes it must not take are left as they are. The pass's IR is interpreted here.
// tests/test_lens_eye_pass.py builds and runs it.
#include <cmath>
#include <cstdio>
#include <stdexcept>
#include <unordered_map>
#include "shader_recompiler/frontend/ir/ir_emitter.h"
#include "shader_recompiler/frontend/ir/program.h"
#include "shader_recompiler/object_pool.h"
#include "shader_recompiler/ir_opt/lens_eye_pass.h"

using namespace Shader;

#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "FAIL:%d %s\n", __LINE__, #x); std::exit(1); } } while (0)

enum class Shape { Game, NoEdge, OtherEdge, TwoEyes, Vertex, NoCameraBank };

struct Built {
    ObjectPool<IR::Inst> insts{256};
    ObjectPool<IR::Block> blocks{4};
    IR::Program program;
    IR::Inst* scale{};
    IR::Inst* edge{};
    IR::Inst* across{};   // (the place in half heights: 3.55555558 u - 1.77777779)
    IR::Inst* slope{};    // (and moved along the edge's slope: -1.77777779 again)
};

void build(Built& b, Shape shape) {
    IR::Block* block = b.blocks.Create(b.insts);
    b.program.blocks.push_back(block);
    b.program.post_order_blocks.push_back(block);
    b.program.stage = shape == Shape::Vertex ? Stage::VertexB : Stage::Fragment;
    // (as the game's program has it: the camera bank read already, its near and far)
    if (shape != Shape::NoCameraBank) {
        b.program.info.constant_buffer_mask |= 1u << 13;
        b.program.info.constant_buffer_used_sizes[13] = 0x30;
    }
    IR::IREmitter ir{*block};
    const IR::F32 x = ir.GetAttribute(IR::Attribute::Generic0X);
    const IR::F32 y = ir.GetAttribute(IR::Attribute::Generic0Y);
    const auto eye = [&](IR::F32 at) {
        const IR::F32 scaled = ir.FPMul(at, ir.Imm32(1.26984131f));
        b.scale = scaled.InstRecursive();
        const IR::F32 square = ir.FPMul(scaled, scaled);
        const float k = shape == Shape::OtherEdge ? 0.8f : 0.9f;
        const IR::F32 edge = shape == Shape::NoEdge ? IR::F32{ir.FPAdd(square, ir.Imm32(0.9f))}
                                                    : IR::F32{ir.FPFma(square, ir.Imm32(-k), ir.Imm32(k))};
        b.edge = edge.InstRecursive();
        return edge;
    };
    IR::F32 inside = ir.FPFma(IR::F32{ir.FPAbs(y)}, ir.Imm32(-1.0f), eye(x));
    if (shape == Shape::TwoEyes) inside = ir.FPAdd(inside, eye(y));
    ir.SetFragColor(0, 0, inside);
    const IR::F32 u = ir.GetAttribute(IR::Attribute::Generic1X);
    const IR::F32 across = ir.FPFma(u, ir.Imm32(3.55555558f), ir.Imm32(-1.77777779f));
    const IR::F32 slope = ir.FPFma(y, ir.Imm32(-1.77777779f), across);
    b.across = across.InstRecursive(), b.slope = slope.InstRecursive();
    ir.SetFragColor(0, 1, across);
    ir.SetFragColor(0, 2, slope);
}

struct Eval {
    float x, y, px, py, u = 0.0f, open = 1.0f;
    std::unordered_map<const IR::Inst*, double> memo;
    double operator()(const IR::Value& v) {
        if (v.IsImmediate()) {
            const IR::Value r = v.Resolve();
            if (r.Type() == IR::Type::U1) return r.U1() ? 1.0 : 0.0;
            if (r.Type() == IR::Type::F32) return r.F32();
            return r.U32();
        }
        const IR::Inst* n = v.InstRecursive();
        if (const auto it = memo.find(n); it != memo.end()) return it->second;
        auto a = [&](size_t i) { return float((*this)(n->Arg(i))); };
        float r;
        switch (n->GetOpcode()) {
        case IR::Opcode::GetAttribute: {
            const IR::Attribute at = n->Arg(0).Attribute();
            r = at == IR::Attribute::Generic0X ? x : at == IR::Attribute::Generic1X ? u : y;
            break;
        }
        case IR::Opcode::GetCbufF32: {
            const u32 at = n->Arg(1).U32();
            if (n->Arg(0).U32() == 16 && at == 8) { r = open; break; }
            if (n->Arg(0).U32() != 13) throw std::runtime_error("bank");
            r = at == 0x80 ? px : at == 0x94 ? py : throw std::runtime_error("cbuf");
            break;
        }
        case IR::Opcode::FPAdd32: r = a(0) + a(1); break;
        case IR::Opcode::FPMul32: r = a(0) * a(1); break;
        case IR::Opcode::FPFma32: r = std::fma(a(0), a(1), a(2)); break;
        case IR::Opcode::FPAbs32: r = std::fabs(a(0)); break;
        case IR::Opcode::FPNeg32: r = -a(0); break;
        case IR::Opcode::FPClamp32: r = std::fmin(std::fmax(a(0), a(1)), a(2)); break;
        case IR::Opcode::FPRecip32: r = 1.0f / a(0); break;
        case IR::Opcode::SelectF32: r = a(0) != 0.0f ? a(1) : a(2); break;
        case IR::Opcode::FPOrdGreaterThan32: r = a(0) > a(1); break;
        case IR::Opcode::FPOrdLessThan32: r = a(0) < a(1); break;
        case IR::Opcode::LogicalAnd: r = a(0) != 0.0f && a(1) != 0.0f; break;
        default: throw std::runtime_error("an opcode the fixture does not evaluate");
        }
        return memo[n] = r;
    }
};

/// Whether every instruction's operands are made before it in the block (the pass's insertion point dominates its uses)
bool defined_before_use(const IR::Block& block) {
    std::unordered_map<const IR::Inst*, size_t> at;
    size_t i = 0;
    for (const IR::Inst& inst : block.Instructions()) at[&inst] = i++;
    for (const IR::Inst& inst : block.Instructions())
        for (size_t a = 0; a < inst.NumArgs(); a++)
            if (!inst.Arg(a).IsImmediate()) {
                const auto it = at.find(inst.Arg(a).InstRecursive());
                if (it == at.end() || it->second >= at[&inst]) return false;
            }
    return true;
}

/// The eye's half width on the picture, in half heights, for a projection: where its edge meets y = 0
float half_width(Built& b, float px, float py) {
    float lo = 0.0f, hi = 4.0f;   // (past the picture's side: 4:3 has the eye wider than the picture)
    for (int i = 0; i < 60; i++) {
        const float mid = 0.5f * (lo + hi);
        Eval e{mid, 0.0f, px, py, 0.0f, {}};
        (e(IR::Value{b.edge}) > 0.0 ? lo : hi) = mid;
    }
    return lo * (py / px);
}

/// The eye's vertex program as the game's has it: a ring at its UV (x and y from -1 to 1), y times the opening's weight
/// (1 - w) c16[2] + w, w attribute 1's x here; the x used once after
struct Ring {
    ObjectPool<IR::Inst> insts{64};
    ObjectPool<IR::Block> blocks{2};
    IR::Program program;
    IR::Inst* use{};
    IR::Inst* across{};
    IR::Inst* noise{};   // (the swirl's noise: u * 1.995 + 0.3)
};
void build_ring(Ring& b, bool camera_bank, Stage stage = Stage::VertexB, bool noise_first = false) {
    IR::Block* block = b.blocks.Create(b.insts);
    b.program.blocks.push_back(block);
    b.program.post_order_blocks.push_back(block);
    b.program.stage = stage;
    if (camera_bank) b.program.info.constant_buffer_mask |= 1u << 13, b.program.info.constant_buffer_used_sizes[13] = 0x1c0;
    IR::IREmitter ir{*block};
    if (noise_first)
        b.noise = ir.FPFma(ir.GetAttribute(IR::Attribute::Generic0X), ir.Imm32(1.99500012f), ir.Imm32(0.3f)).InstRecursive();
    const IR::F32 w = ir.GetAttribute(IR::Attribute::Generic1X);
    const IR::F32 open = ir.GetFloatCbuf(ir.Imm32(16u), ir.Imm32(8u));
    const IR::F32 weight = ir.FPFma(IR::F32{ir.FPAdd(IR::F32{ir.FPNeg(w)}, ir.Imm32(1.0f))}, open, w);
    const IR::F32 y = ir.FPMul(IR::F32{ir.FPFma(ir.GetAttribute(IR::Attribute::Generic0Y), ir.Imm32(2.0f), ir.Imm32(-1.0f))}, weight);
    const IR::F32 x = ir.FPFma(ir.GetAttribute(IR::Attribute::Generic0X), ir.Imm32(2.0f), ir.Imm32(-1.0f));
    const IR::F32 use = ir.FPAdd(x, y);
    b.use = use.InstRecursive(), b.across = x.InstRecursive();
    if (!noise_first)
        b.noise = ir.FPFma(ir.GetAttribute(IR::Attribute::Generic0X), ir.Imm32(1.99500012f), ir.Imm32(0.3f)).InstRecursive();
    ir.SetFragColor(0, 0, IR::F32{IR::Value{b.noise}});
}

int main() {
    const float py = 2.14450693f;   // (50 degrees high, as the camera bank had it)
    {
        Built b;
        build(b, Shape::Game);
        const char* why = Optimization::LensEyePass(b.program);
        if (why) std::fprintf(stderr, "the game's shape refused: %s\n", why);
        CHECK(why == nullptr);
        CHECK(defined_before_use(*b.program.blocks.front()));
        CHECK(b.program.info.constant_buffer_used_sizes[13] >= 0x98);
        // (16:9: the game's constant, to the bit)
        Eval e169{1.0f, 0.0f, py / (16.0f / 9.0f), py, 0.0f, {}};
        const float s169 = float(e169(b.scale->Arg(1)));
        CHECK(std::fabs(s169 - 1.26984131f) <= 2e-7f);
        // (each shape: the eye 1.4 half heights wide, as the reveal)
        for (const float shape : {16.0f / 9.0f, 2.37037037f, 2.4f, 32.0f / 9.0f, 1.6f, 4.0f / 3.0f}) {
            const float w = half_width(b, py / shape, py);
            if (std::fabs(w - 1.4f) > 1e-4f) std::fprintf(stderr, "shape %g: half width %g\n", shape, w);
            CHECK(std::fabs(w - 1.4f) <= 1e-4f);
        }
        // (the glow's x held at 16:9's side: the edge's value at the picture's side 16:9's at every wider shape, the game's to the
        // bit at 16:9; inside 16:9's side the same at every shape)
        const float side169 = std::fma(1.26984131f * 1.26984131f, -0.9f, 0.9f);
        for (const float shape : {16.0f / 9.0f, 2.37037037f, 32.0f / 9.0f}) {
            Eval side{1.0f, 0.0f, py / shape, py, 0.0f, {}};
            const float v = float(side(IR::Value{b.edge}));
            if (std::fabs(v - side169) > 1e-5f) std::fprintf(stderr, "shape %g: the side's edge value %g, not %g\n", shape, v, side169);
            CHECK(std::fabs(v - side169) <= 1e-5f);
            Eval inner{1.5f / shape, 0.0f, py / shape, py, 0.0f, {}};   // (1.5 half heights out)
            CHECK(std::fabs(float(inner(IR::Value{b.edge})) - 0.9f * (1.0f - (1.5f / 1.4f) * (1.5f / 1.4f))) <= 1e-5f);
        }
        // (the place in half heights and its slope term: the picture's shape for 16:9, the game's at 16:9 to the bit)
        for (const float shape : {16.0f / 9.0f, 2.37037037f}) {
            for (const float u : {0.0f, 0.25f, 1.0f}) {
                Eval e{0.0f, 0.5f, py / shape, py, u, {}};
                const float want = (2.0f * u - 1.0f) * shape;
                const float across = float(e(IR::Value{b.across})), slope = float(e(IR::Value{b.slope}));
                CHECK(std::fabs(across - want) <= 1e-5f);
                CHECK(std::fabs(slope - (want - 0.5f * shape)) <= 1e-5f);
                if (shape == 16.0f / 9.0f) CHECK(across == std::fma(u, 3.55555558f, -1.77777779f));
            }
        }
        // (no sane projection: an unbound bank's zeros, NaN: the game's constant)
        for (const auto& [px, pyy] : {std::pair{0.0f, py}, std::pair{0.0f, 0.0f}, std::pair{std::nanf(""), py}, std::pair{py, 0.0f}}) {
            Eval e{1.0f, 0.0f, px, pyy, 1.0f, {}};
            CHECK(float(e(b.scale->Arg(1))) == 1.26984131f);
            CHECK(float(e(IR::Value{b.across})) == std::fma(1.0f, 3.55555558f, -1.77777779f));
        }
    }
    for (const Shape shape : {Shape::NoEdge, Shape::OtherEdge, Shape::TwoEyes, Shape::Vertex, Shape::NoCameraBank}) {
        Built b;
        build(b, shape);
        const size_t before = b.program.blocks.front()->Instructions().size();
        const char* why = Optimization::LensEyePass(b.program);
        CHECK(why != nullptr);
        CHECK(b.program.blocks.front()->Instructions().size() == before);
        CHECK(b.scale->Arg(1).IsImmediate());
        CHECK(b.across->Arg(1).IsImmediate() && b.across->Arg(2).IsImmediate());
    }
    {   // (the ring: its hole narrowed with the eye, x times 16:9 over the shape on the hole's edge, 1 at the picture's)
        Ring b;
        build_ring(b, true);
        const char* swirl = "unset";
        const char* why = Optimization::LensEyeVertexPass(b.program, &swirl);
        if (why) std::fprintf(stderr, "the ring refused: %s\n", why);
        if (swirl) std::fprintf(stderr, "the swirl refused: %s\n", swirl);
        CHECK(why == nullptr);
        CHECK(swirl == nullptr);
        CHECK(defined_before_use(*b.program.blocks.front()));
        CHECK(b.program.info.constant_buffer_used_sizes[13] >= 0x98);
        for (const float shape : {16.0f / 9.0f, 2.37037037f, 32.0f / 9.0f, 1.6f, 4.0f / 3.0f}) {
            for (const float w : {0.0f, 0.5f, 1.0f}) {
                // (x 0.8 at the UV, y 0: the use reads the x alone)
                Eval e{0.9f, 0.5f, py / shape, py, w, 1.0f};
                const float x = float(e(b.use->Arg(0)));
                const float want = 0.8f * ((1.0f - w) * (16.0f / 9.0f) / shape + w);
                if (std::fabs(x - want) > 1e-5f) std::fprintf(stderr, "shape %g weight %g: x %g, not %g\n", shape, w, x, want);
                CHECK(std::fabs(x - want) <= 1e-5f);
                // (the noise: u at its place in 16:9's terms, 0.5 + x shape / (16:9) / 2; u itself at 16:9)
                const float noise = float(e(IR::Value{b.noise}));
                const float noise_want = (0.5f + 0.5f * want * shape / (16.0f / 9.0f)) * 1.99500012f + 0.3f;
                if (std::fabs(noise - noise_want) > 1e-5f) std::fprintf(stderr, "shape %g weight %g: noise %g, not %g\n", shape, w, noise, noise_want);
                CHECK(std::fabs(noise - noise_want) <= 1e-5f);
                if (shape == 16.0f / 9.0f) CHECK(std::fabs(noise - (0.9f * 1.99500012f + 0.3f)) <= 1e-5f);
            }
        }
        Eval none{0.9f, 0.5f, 0.0f, py, 0.0f, 1.0f};   // (no sane projection: the game's x and noise)
        CHECK(std::fabs(float(none(b.use->Arg(0))) - 0.8f) <= 1e-6f);
        CHECK(std::fabs(float(none(IR::Value{b.noise})) - (0.9f * 1.99500012f + 0.3f)) <= 1e-5f);
    }
    {   // (the noise read before the ring's x is made: the ring narrowed, the noise left as the game's)
        Ring b;
        build_ring(b, true, Stage::VertexB, true);
        const char* swirl = nullptr;
        CHECK(Optimization::LensEyeVertexPass(b.program, &swirl) == nullptr);
        CHECK(swirl != nullptr);
        CHECK(b.noise->Arg(0).InstRecursive()->GetOpcode() == IR::Opcode::GetAttribute);
        CHECK(defined_before_use(*b.program.blocks.front()));
    }
    for (const bool camera_bank : {false, true}) {   // (the shapes it must not take: no camera bank; a fragment program)
        Ring b;
        build_ring(b, camera_bank, camera_bank ? Stage::Fragment : Stage::VertexB);
        CHECK(Optimization::LensEyeVertexPass(b.program) != nullptr);
        CHECK(b.use->Arg(0).InstRecursive() == b.across);
    }
    std::printf("PASS: the Lens of Truth eye pass\n");
    return 0;
}
