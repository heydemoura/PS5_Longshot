// SPDX-License-Identifier: GPL-3.0-or-later
// (longshot) The Lens of Truth's eye at the picture's own shape. With the Lens on, the effect's fragment program draws the purple
// eye over the whole picture from each pixel's place on the screen, x and y from -1 to 1: inside the eye where
// |y| < 0.9 (1 - (1.26984131 x)^2). The 1.26984131 is 16:9 over the eye's half width, 1.4 half heights
// (GlassTruthControllerParam's RadiusX): the eye 1.4 half heights wide on a 16:9 picture, as the reveal is, which the game
// sizes in half heights at every shape. The same program takes x into half heights for the eye's ring, its bands and its
// distance to the edge as x * 16:9 (3.55555558 u - 1.77777779 from the 0..1 place, and -1.77777779 again on the edge's
// slope). On a wider picture the drawn eye stretched with it and the reveal did not: its sides showed nothing hidden inside
// the drawn eye. The pass gives the program the picture's shape in place of 16:9, from the projection the effect's camera
// bank (13) holds at 0x80 (x) and 0x94 (y): the eye's constant that shape over 1.4, its 16:9s that shape. 16:9 gives the
// game's constants again, 2.37 (5120x2160) an eye 1.4 half heights wide that meets the reveal. A projection that gives no
// sane shape keeps the game's constants.
//
// Outside the eye its swirl glows by how far from the eye's edge it is, from the same x: the edge's 0.9 (1 - (x / 1.4)^2) and its
// square, unclamped. 16:9 never takes x past 16:9; 32:9 takes it to twice that, the glow nine times what the game ever drew there,
// burnt out white streaks at the sides that shimmered under DLSS while turning. The eye's x is held at 16:9's side for it: past
// there the swirl keeps the glow of 16:9's side, out to the picture's own.
//
// The eye is drawn on a ring of the picture: its vertex program places each vertex at its UV, x and y from -1 to 1, its inner
// edge a hole inside the eye that the fragment program then has nothing to draw in. A weight in the vertex (attribute 0's x:
// 0 on the hole's edge, 1 at the picture's) has y take the eye's opening, c16[2], on the hole and 1 at the picture's edge. The
// hole's x stayed 16:9's on a wider picture: wider than the eye at its own shape, it left the eye's sides undrawn, a sliver
// neither the eye's tint nor the reveal. LensEyeVertexPass, for the eye's own vertex program, has x take 16:9 over the
// picture's shape the same way: the hole narrows with the eye, the picture's edge stays where it is.
//
// The same program hands the fragment program a noise texture's place from the UV's x as well (u * 1.995 + its drift): across
// the picture's width, so on a wider picture the eye's green swirl, which that noise moves, was stretched sideways (its
// sparkles dashes, 1.75 times as wide as high at 32:9). Its u is given the vertex's place on the picture in half heights in
// 16:9's terms, 0.5 + x (the ring's, narrowed) times the shape over 16:9 / 2: the noise as big on every shape, the game's at 16:9.

#pragma once

#include <algorithm>
#include <bit>
#include <iterator>
#include <string>
#include <unordered_map>
#include <vector>

#include "shader_recompiler/frontend/ir/ir_emitter.h"
#include "shader_recompiler/frontend/ir/program.h"

namespace Shader::Optimization {

namespace lens_eye_detail {

constexpr float kEyeScale = 1.26984131f;   // (16:9 / 1.4)
constexpr float kWide = 1.77777779f;       // (16:9, and twice it: the program's x into half heights)
constexpr float kTwiceWide = 3.55555558f;
constexpr float kRadiusX = 1.4f;           // (half heights)
constexpr float kRadiusY = 0.9f;
constexpr u32 kCameraBank = 13;
constexpr u32 kProjectionX = 0x80, kProjectionY = 0x94;

inline bool IsImm(const IR::Value& v, float f) {
    if (!v.IsImmediate()) return false;
    const IR::Value r{v.Resolve()};
    return r.Type() == IR::Type::F32 && std::bit_cast<u32>(r.F32()) == std::bit_cast<u32>(f);
}

} // namespace lens_eye_detail

/// The eye's x scale made the picture's shape over 1.4; nullptr when made, else why this program is not the eye's
inline const char* LensEyePass(IR::Program& program) {
    using namespace lens_eye_detail;
    if (program.stage != Stage::Fragment) return "not a fragment program";
    if (!((program.info.constant_buffer_mask >> kCameraBank) & 1)) return "no camera bank";
    std::unordered_map<const IR::Inst*, std::vector<IR::Inst*>> users;
    std::vector<IR::Inst*> scales;
    for (IR::Block* block : program.blocks) {
        for (IR::Inst& inst : block->Instructions()) {
            for (size_t i = 0; i < inst.NumArgs(); i++)
                if (!inst.Arg(i).IsImmediate()) users[inst.Arg(i).InstRecursive()].push_back(&inst);
            if (inst.GetOpcode() == IR::Opcode::FPMul32 && (IsImm(inst.Arg(0), kEyeScale) != IsImm(inst.Arg(1), kEyeScale)))
                scales.push_back(&inst);
        }
    }
    // (the eye's: squared, then 0.9 - 0.9 x^2)
    IR::Inst* eye = nullptr;
    for (IR::Inst* scale : scales) {
        for (IR::Inst* square : users[scale]) {
            if (square->GetOpcode() != IR::Opcode::FPMul32 || square->Arg(0).IsImmediate() || square->Arg(1).IsImmediate() ||
                square->Arg(0).InstRecursive() != scale || square->Arg(1).InstRecursive() != scale)
                continue;
            for (IR::Inst* edge : users[square]) {
                if (edge->GetOpcode() == IR::Opcode::FPFma32 && !edge->Arg(0).IsImmediate() &&
                    edge->Arg(0).InstRecursive() == square && IsImm(edge->Arg(1), -kRadiusY) && IsImm(edge->Arg(2), kRadiusY)) {
                    if (eye && eye != scale) return "two eyes";
                    eye = scale;
                }
            }
        }
    }
    if (!eye) return "no eye";
    // (the picture's shape, once at the program's start, where every use of it is reached from)
    IR::Block* const entry{program.post_order_blocks.empty() ? program.blocks.front() : program.post_order_blocks.back()};
    auto& list{entry->Instructions()};
    auto at_start{list.begin()};
    for (auto it = list.begin(); it != list.end(); ++it)
        if (it->GetOpcode() == IR::Opcode::Prologue) at_start = std::next(it);
    IR::IREmitter ir{*entry, at_start};
    const IR::F32 px{ir.GetFloatCbuf(ir.Imm32(kCameraBank), ir.Imm32(kProjectionX))};
    const IR::F32 py{ir.GetFloatCbuf(ir.Imm32(kCameraBank), ir.Imm32(kProjectionY))};
    const IR::F32 measured{ir.FPMul(py, IR::F32{ir.FPRecip(px)})};
    // (a sane shape, 1:2 to 10:1: else, NaN included, the game's 16:9)
    const IR::U1 sane{ir.LogicalAnd(ir.FPGreaterThan(measured, ir.Imm32(0.5f)), ir.FPLessThan(measured, ir.Imm32(10.0f)))};
    const IR::F32 shape{ir.Select(sane, measured, ir.Imm32(kWide))};
    const IR::F32 scale{ir.Select(sane, ir.FPMul(measured, ir.Imm32(1.0f / kRadiusX)), ir.Imm32(kEyeScale))};
    const IR::F32 twice{ir.FPAdd(shape, shape)};
    const IR::F32 minus{ir.FPNeg(shape)};
    eye->SetArg(IsImm(eye->Arg(0), kEyeScale) ? 0 : 1, scale);
    // (the eye's x, x / 1.4 in half heights, held at 16:9's side, 16:9 / 1.4: the swirl's glow outside the eye grows as the
    // square of it, unclamped, and past 16:9's side, where the game never drew it, burnt out white at 32:9 and shimmered under
    // DLSS while turning. Past there it keeps 16:9's side's, as wide as the picture is; up to there, at 16:9 everywhere, as before)
    {
        std::vector<std::pair<IR::Inst*, size_t>> uses;
        for (IR::Block* b : program.blocks)
            for (IR::Inst& inst : b->Instructions())
                for (size_t i = 0; i < inst.NumArgs(); i++)
                    if (!inst.Arg(i).IsImmediate() && inst.Arg(i).InstRecursive() == eye && inst.GetOpcode() != IR::Opcode::Identity)
                        uses.emplace_back(&inst, i);
        IR::Block* eb{};
        for (IR::Block* b : program.blocks)
            for (IR::Inst& inst : b->Instructions())
                if (&inst == eye) eb = b;
        IR::IREmitter at_eye{*eb, std::next(eb->Instructions().iterator_to(*eye))};
        const IR::F32 held{at_eye.FPClamp(IR::F32{IR::Value{eye}}, ir.Imm32(-kEyeScale), ir.Imm32(kEyeScale))};
        for (const auto& [inst, i] : uses) inst->SetArg(i, held);
    }
    // (the program's 16:9s: each immediate +-16:9 and 2 x 16:9 of its float arithmetic)
    for (IR::Block* b : program.blocks) {
        for (IR::Inst& inst : b->Instructions()) {
            const IR::Opcode op{inst.GetOpcode()};
            if (op != IR::Opcode::FPMul32 && op != IR::Opcode::FPFma32 && op != IR::Opcode::FPAdd32) continue;
            for (size_t i = 0; i < inst.NumArgs(); i++) {
                if (IsImm(inst.Arg(i), kWide)) inst.SetArg(i, shape);
                else if (IsImm(inst.Arg(i), -kWide)) inst.SetArg(i, minus);
                else if (IsImm(inst.Arg(i), kTwiceWide)) inst.SetArg(i, twice);
                else if (IsImm(inst.Arg(i), -kTwiceWide)) inst.SetArg(i, IR::F32{ir.FPNeg(twice)});
            }
        }
    }
    // (the bank's binding covers what it reads: only up to the words the game's program read before)
    u32& used{program.info.constant_buffer_used_sizes[kCameraBank]};
    used = std::max<u32>(used, (kProjectionY + 4 + 15) & ~15u);
    program.info.used_constant_buffer_types |= IR::Type::F32;
    return nullptr;
}

/// For the eye's vertex program (its fragment program's LensEyePass made): the ring's hole narrowed with the eye, x times
/// 16:9 over the picture's shape on the hole's edge, 1 at the picture's; and the UV's other uses (the swirl's noise) its place
/// in 16:9's terms. nullptr when made, else why it was left; *swirl nullptr when the noise was moved too, else why not
inline const char* LensEyeVertexPass(IR::Program& program, const char** swirl = nullptr) {
    using namespace lens_eye_detail;
    if (program.stage != Stage::VertexB) return "not a vertex program";
    if (!((program.info.constant_buffer_mask >> kCameraBank) & 1)) return "no camera bank";
    const auto attribute = [](const IR::Value& v, IR::Attribute& out) {
        if (v.IsImmediate()) return false;
        const IR::Inst* i{v.InstRecursive()};
        if (i->GetOpcode() != IR::Opcode::GetAttribute || !i->Arg(0).IsImmediate()) return false;
        out = i->Arg(0).Attribute();
        return true;
    };
    const auto to_unit = [&](const IR::Inst& inst, IR::Attribute& from) {   // (u * 2 - 1)
        return inst.GetOpcode() == IR::Opcode::FPFma32 && attribute(inst.Arg(0), from) && IsImm(inst.Arg(1), 2.0f) &&
               IsImm(inst.Arg(2), -1.0f);
    };
    std::unordered_map<const IR::Inst*, std::vector<IR::Inst*>> users;
    std::vector<IR::Inst*> units;
    std::unordered_map<const IR::Inst*, IR::Block*> block_of;
    for (IR::Block* block : program.blocks) {
        for (IR::Inst& inst : block->Instructions()) {
            block_of[&inst] = block;
            for (size_t i = 0; i < inst.NumArgs(); i++)
                if (!inst.Arg(i).IsImmediate()) users[inst.Arg(i).InstRecursive()].push_back(&inst);
            IR::Attribute a;
            if (to_unit(inst, a)) units.push_back(&inst);
        }
    }
    // (y: its unit times the opening's weight, (1 - w) c16[2] + w; x: the same UV's other unit)
    IR::Inst *across = nullptr, *weight = nullptr;
    for (IR::Inst* y : units) {
        IR::Attribute ya;
        to_unit(*y, ya);
        const int yi{int(ya) - int(IR::Attribute::Generic0X)};
        if (yi < 0 || yi >= 32 * 4 || yi % 4 != 1) continue;
        for (IR::Inst* mul : users[y]) {
            if (mul->GetOpcode() != IR::Opcode::FPMul32) continue;
            const IR::Value other{mul->Arg(0).IsImmediate() || mul->Arg(0).InstRecursive() != y ? mul->Arg(0) : mul->Arg(1)};
            if (other.IsImmediate()) continue;
            IR::Inst* lerp{other.InstRecursive()};
            IR::Attribute wa;
            if (lerp->GetOpcode() != IR::Opcode::FPFma32 || lerp->Arg(1).IsImmediate() || !attribute(lerp->Arg(2), wa)) continue;
            const IR::Inst* open{lerp->Arg(1).InstRecursive()};
            if (open->GetOpcode() != IR::Opcode::GetCbufF32 || !open->Arg(0).IsImmediate() || open->Arg(0).U32() != 16 ||
                !open->Arg(1).IsImmediate() || open->Arg(1).U32() != 8)
                continue;
            for (IR::Inst* x : units) {
                IR::Attribute xa;
                to_unit(*x, xa);
                if (int(xa) != int(ya) - 1) continue;
                if ((across && across != x) || (weight && weight != lerp)) return "two rings";
                across = x, weight = lerp;
            }
        }
    }
    if (!across) return "no ring";
    IR::Block* const block{block_of[across]};
    if (block_of[weight] != block) return "the ring's x and its weight apart";
    // (after both, the weight's parts and the x made by then; every use of the x after it, in its block)
    auto& list{block->Instructions()};
    std::unordered_map<const IR::Inst*, size_t> order;
    size_t n{};
    for (IR::Inst& inst : list) order[&inst] = n++;
    IR::Inst* const later{order[across] > order[weight] ? across : weight};
    for (IR::Inst* user : users[across])
        if (block_of[user] != block || order[user] <= order[later]) return "the ring's x used before its weight";
    // (the UV's x read elsewhere: the swirl's noise, u * 1.995 + its drift. Each in float arithmetic after the narrowed x)
    IR::Attribute xa;
    attribute(across->Arg(0), xa);
    std::vector<std::pair<IR::Inst*, size_t>> noise;
    const char* noise_why = nullptr;
    for (IR::Block* b : program.blocks) {
        for (IR::Inst& inst : b->Instructions()) {
            IR::Attribute a;
            if (inst.GetOpcode() != IR::Opcode::GetAttribute || !attribute(IR::Value{&inst}, a) || a != xa) continue;
            for (IR::Inst* user : users[&inst]) {
                const IR::Opcode op{user->GetOpcode()};
                if (user == across || op == IR::Opcode::Identity) continue;   // (an Identity's own users are the value's)
                if (op != IR::Opcode::FPFma32 && op != IR::Opcode::FPMul32 && op != IR::Opcode::FPAdd32) {
                    thread_local std::string why;
                    why = "the UV's x read by " + std::string{IR::NameOf(op)};
                    noise_why = why.c_str();
                } else if (block_of[user] != block || order[user] <= order[later]) noise_why = "the UV's x read before the ring's";
                for (size_t i = 0; i < user->NumArgs(); i++)
                    if (!user->Arg(i).IsImmediate() && user->Arg(i).InstRecursive() == &inst) noise.emplace_back(user, i);
            }
        }
    }
    if (noise.empty() && !noise_why) noise_why = "no other read of the UV's x";
    if (noise_why) noise.clear();
    if (swirl) *swirl = noise_why;
    IR::IREmitter ir{*block, std::next(list.iterator_to(*later))};
    const IR::F32 px{ir.GetFloatCbuf(ir.Imm32(kCameraBank), ir.Imm32(kProjectionX))};
    const IR::F32 py{ir.GetFloatCbuf(ir.Imm32(kCameraBank), ir.Imm32(kProjectionY))};
    const IR::F32 measured{ir.FPMul(py, IR::F32{ir.FPRecip(px)})};
    const IR::U1 sane{ir.LogicalAnd(ir.FPGreaterThan(measured, ir.Imm32(0.5f)), ir.FPLessThan(measured, ir.Imm32(10.0f)))};
    const IR::F32 narrow{ir.Select(sane, ir.FPMul(ir.Imm32(kWide), IR::F32{ir.FPRecip(measured)}), ir.Imm32(1.0f))};
    // ((1 - w) narrow + w, as the opening's weight is (1 - w) c16[2] + w)
    const IR::F32 w{weight->Arg(2)};
    const IR::F32 scale{ir.FPFma(IR::F32{ir.FPAdd(IR::F32{ir.FPNeg(w)}, ir.Imm32(1.0f))}, narrow, w)};
    const IR::F32 narrowed{ir.FPMul(IR::F32{IR::Value{across}}, scale)};
    IR::Inst* const made{narrowed.InstRecursive()};
    for (IR::Inst* user : users[across]) {
        if (user == made) continue;
        for (size_t i = 0; i < user->NumArgs(); i++)
            if (!user->Arg(i).IsImmediate() && user->Arg(i).InstRecursive() == across) user->SetArg(i, narrowed);
    }
    if (!noise.empty()) {
        // (0.5 + x (shape / 16:9) / 2: u again at 16:9)
        const IR::F32 half_shape{ir.Select(sane, ir.FPMul(measured, ir.Imm32(0.5f / kWide)), ir.Imm32(0.5f))};
        const IR::F32 place{ir.FPFma(narrowed, half_shape, ir.Imm32(0.5f))};
        for (const auto& [user, i] : noise) user->SetArg(i, place);
    }
    u32& used{program.info.constant_buffer_used_sizes[kCameraBank]};
    used = std::max<u32>(used, (kProjectionY + 4 + 15) & ~15u);
    program.info.used_constant_buffer_types |= IR::Type::F32;
    return nullptr;
}

} // namespace Shader::Optimization
