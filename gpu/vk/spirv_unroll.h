// Full unrolling, constant folding, constant-array folding, intra-block Function-alloca forwarding and dead-code elimination
// for counted loops in a SPIR-V module, innermost first: for AMD FSR 4's eligible network layers and postpass (fsr4_tune.h),
// whose nested loops over a kernel's taps and channels the driver leaves rolled. Rolled, every index into the weights and
// the accumulators is a loop counter and every tap pays a counter, a compare and a branch; unrolled and folded, the weight
// tables become immediate constants in the code, the scratch Function arrays disappear, and the shader shrinks enough that
// AMD's compiler builds every weight-table pass (passes 1, 2, 4, 5, 10, 12 and postpass) in ~0.15-0.35 s standalone
// (~0.33-0.69 s in-game) and <60 MB instead of ~50 s / 3 GB (or 40-55 GB OOM on passes 4, 5, 10). Plain functions over the
// words, no Vulkan; operands are walked by spirv_operands.h, so only ids are renamed, never a literal that happens to equal
// one.
//
// A loop is taken only in this shape (DXC's for its counted loops):
//   %H = OpLabel ; one or more %p = OpPhi %ty %init %pre %next %cont (one is the induction variable %i, %init a constant) ;
//   %t = OpULessThan / OpSLessThan / OpINotEqual %bool %i %N (%N a constant) ; OpLoopMerge %M %cont ... ;
//   OpBranchConditional %t %B %M
//   ... its blocks laid out after %H and before %M (optionally with one top-level `if (cond) continue;` selection) ... ;
//   %cont = OpLabel ... %inext = OpIAdd %i %step (%step a constant) ... OpBranch %H
// and only when: nothing outside it names a value it makes (the header and live-out header phis excepted); no body result
// carries an OpDecorate; no body branch exits early to %M or jumps to %cont from an un-retargeted selection; no OpSwitch is
// inside it; its body does not name the header, the test or %inext; the merge block has no phi (even after OpLine/OpNoLine);
// every instruction in it is one the operand table describes. Anything else is left as it is.
#pragma once

#include <algorithm>
#include <climits>
#include <cstdint>
#include <cstring>
#include <map>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "spirv_operands.h"

namespace spirv_unroll {

using Ins = std::vector<uint32_t>;   // one instruction, its first word included

/// Calls f(word&, is_result) for each id in the instruction (result type, result, operands); false when its operands are
/// not all described (the instruction is then not to be renamed)
template <class F>
inline bool for_each_id(Ins& in, F&& f) {
    const char* k = spirv_operands::of(in[0] & 0xffff);
    if (!k) return false;
    size_t w = 1;
    const size_t n = in.size();
    for (const char* p = k; *p; p++) {
        const char c = *p;
        const bool many = p[1] == '*', opt = p[1] == '?';
        if (many || opt) p++;
        do {
            if (w >= n) {
                if (many || opt) break;
                return false;   // (a required operand missing)
            }
            switch (c) {
            case 'T': f(in[w++], false); break;
            case 'R': f(in[w++], true); break;
            case 'i': f(in[w++], false); break;
            case 'l': case 'e': w++; break;
            case 's': { while (w < n) { const uint32_t x = in[w++]; if (!(x & 0xff000000u) || !(x & 0xff0000u) || !(x & 0xff00u) || !(x & 0xffu)) break; } break; }
            case 'c': w = n; break;
            case 'P': if (w + 1 >= n) return false; f(in[w++], false); f(in[w++], false); break;
            case 'Q': if (w + 1 >= n) return false; w++; f(in[w++], false); break;
            case 'q': if (w + 1 >= n) return false; f(in[w++], false); w++; break;
            case 'M': {   // MemoryAccess: Aligned a literal; MakePointerAvailable / Visible an id each
                const uint32_t m = in[w++];
                if (m & ~0x3fu) return false;
                if (m & 0x2) { if (w >= n) return false; w++; }
                if (m & 0x8) { if (w >= n) return false; f(in[w++], false); }
                if (m & 0x10) { if (w >= n) return false; f(in[w++], false); }
                break;
            }
            case 'G': {   // ImageOperands: every parameter an id, two for Grad
                const uint32_t m = in[w++];
                if (m & ~0x17fffu) return false;
                for (uint32_t b = 0; b < 17; b++) {
                    if (!(m & (1u << b)) || (b >= 10 && b <= 14)) continue;
                    for (int r = 0; r < (b == 2 ? 2 : 1); r++) { if (w >= n) return false; f(in[w++], false); }
                }
                break;
            }
            case 'L': {   // LoopControl: its parameters are literals
                const uint32_t m = in[w++];
                if (m & ~0x1ffu) return false;
                for (uint32_t b = 3; b <= 8; b++) if (m & (1u << b)) { if (w >= n) return false; w++; }
                break;
            }
            default: return false;   // ('X')
            }
        } while (many && w < n);
    }
    return w == n;
}

inline uint32_t result_of(Ins& in) {
    uint32_t r = 0;
    for_each_id(in, [&](uint32_t& id, bool res) { if (res) r = id; });
    return r;
}

struct Module {
    uint32_t header[5];
    std::vector<Ins> ins;
};

inline bool parse(const uint32_t* code, size_t words, Module& m) {
    if (words < 5 || code[0] != 0x07230203u) return false;
    for (int i = 0; i < 5; i++) m.header[i] = code[i];
    m.ins.clear();
    for (size_t i = 5; i < words;) {
        const uint32_t n = code[i] >> 16;
        if (!n || i + n > words) return false;
        m.ins.emplace_back(code + i, code + i + n);
        i += n;
    }
    return true;
}
inline std::vector<uint32_t> emit(const Module& m) {
    std::vector<uint32_t> o(m.header, m.header + 5);
    for (const Ins& in : m.ins) o.insert(o.end(), in.begin(), in.end());
    return o;
}

enum : uint32_t {
    kName = 5, kLine = 8, kExtInstImport = 11, kExtInst = 12,
    kTypeInt = 21, kTypeFloat = 22, kTypeVector = 23, kTypeMatrix = 24, kTypeArray = 28, kTypeRuntimeArray = 29, kTypeStruct = 30, kTypePointer = 32,
    kConstantTrue = 41, kConstantFalse = 42, kConstant = 43, kConstantComposite = 44,
    kFunction = 54, kFunctionParameter = 55, kFunctionEnd = 56, kFunctionCall = 57,
    kVariable = 59, kLoad = 61, kStore = 62, kAccessChain = 65, kDecorate = 71, kGroupDecorate = 74,
    kVectorShuffle = 79, kCompositeConstruct = 80, kCompositeExtract = 81, kCompositeInsert = 82, kCopyObject = 83,
    kBitcast = 124, kIAdd = 128, kISub = 130, kIMul = 132, kUDiv = 134, kSDiv = 135, kUMod = 137,
    kIEqual = 170, kINotEqual = 171, kULess = 176, kSLess = 177,
    kShiftRightLogical = 194, kShiftRightArithmetic = 195, kShiftLeftLogical = 196,
    kBitwiseOr = 197, kBitwiseXor = 198, kBitwiseAnd = 199,
    kPhi = 245, kLoopMerge = 246, kSelectionMerge = 247, kLabel = 248, kBranch = 249, kBranchCond = 250, kSwitch = 251,
    kNoLine = 317, kDecorateId = 332, kDecorateString = 5632
};
inline bool is_terminator(uint32_t op) { return op == 249 || op == 250 || op == 251 || op == 252 || op == 253 || op == 254 || op == 255; }

namespace detail {

inline bool is_pure_op(uint32_t op, uint32_t glsl_ext, const Ins& in) {
    switch (op) {
    case kAccessChain:
    case kVectorShuffle:
    case kCompositeConstruct:
    case kCompositeExtract:
    case kCompositeInsert:
    case kCopyObject:
    case kBitcast:
    case kIAdd: case kISub: case kIMul: case kUDiv: case kSDiv: case kUMod:
    case kIEqual: case kINotEqual: case kULess: case kSLess:
    case 172: case 173: case 174: case 175: case 178: case 179:   // integer comparisons
    case 164: case 165: case 166: case 167: case 168: case 169:   // logical ops and OpSelect
    case kShiftRightLogical: case kShiftRightArithmetic: case kShiftLeftLogical:
    case kBitwiseOr: case kBitwiseXor: case kBitwiseAnd: case 200:
    case 109: case 110: case 111: case 112:                       // ConvertFToU/S, SToF, UToF
    case 126: case 127: case 129: case 131: case 133: case 136: case 142:   // float arithmetic
    case 4450: case 4451: case 4452:                              // OpSDot, OpUDot, OpSUDot
        return true;
    case kExtInst:
        // GLSL.std.450 is pure except Modf (35) and Frexp (51), which store through a pointer operand
        return glsl_ext && in.size() >= 5 && in[3] == glsl_ext && in[4] != 35 && in[4] != 51;
    default:
        return false;
    }
}

struct TypeInfo {
    std::unordered_map<uint32_t, bool> int32_signed;                      // ty -> signed?
    std::unordered_set<uint32_t> float32;                                 // 32-bit float types
    std::unordered_map<uint32_t, std::pair<uint32_t, uint32_t>> vec_ty;   // vty -> (elem_ty, count)
    std::unordered_map<uint32_t, std::pair<uint32_t, uint32_t>> mat_ty;   // mty -> (col_ty, cols)
    std::unordered_map<uint32_t, std::pair<uint32_t, uint32_t>> arr_ty;   // aty -> (elem_ty, len)
    std::unordered_map<uint32_t, uint32_t> rta_elem;                      // rta -> elem_ty
    std::unordered_map<uint32_t, std::vector<uint32_t>> struct_ty;        // sty -> member types
    std::unordered_map<uint32_t, std::pair<uint32_t, uint32_t>> ptr_ty;   // pty -> (sc, pointee)
    std::unordered_map<uint32_t, std::pair<uint32_t, uint32_t>> const32;  // id -> (ty, raw_u32)
    std::unordered_map<uint64_t, uint32_t> const32_by_key;                // (ty<<32)|raw -> first id (deterministic)
    std::unordered_map<uint32_t, std::vector<uint32_t>> composite;        // id -> element ids
    std::unordered_map<uint32_t, uint32_t> id_type;                       // result_id -> type_id
    std::unordered_set<uint32_t> decorated;                               // ids carrying any OpDecorate
    uint32_t glsl_ext = 0;
    size_t first_func = 0;                                                // index of first OpFunction (or m.ins.size())
    bool malformed = false;

    void scan(const Module& m) {
        int32_signed.clear(); float32.clear(); vec_ty.clear(); mat_ty.clear();
        arr_ty.clear(); rta_elem.clear(); struct_ty.clear(); ptr_ty.clear();
        const32.clear(); const32_by_key.clear(); composite.clear(); id_type.clear();
        decorated.clear();
        glsl_ext = 0;
        first_func = m.ins.size();
        malformed = false;
        for (size_t k = 0; k < m.ins.size(); k++) {
            const Ins& in = m.ins[k];
            const uint32_t op = in[0] & 0xffff;
            if (op == kFunction) { first_func = k; break; }
            if (op == kDecorate || op == kDecorateId || op == kDecorateString) {
                if (in.size() < 3) { malformed = true; return; }
                decorated.insert(in[1]);
            } else if (op == kGroupDecorate) {
                if (in.size() < 2) { malformed = true; return; }
                for (size_t j = 2; j < in.size(); j++) decorated.insert(in[j]);
            } else if (op == kExtInstImport) {
                if (in.size() < 3) { malformed = true; return; }
                const char* s = (const char*)&in[2];
                const size_t max_len = (in.size() - 2) * 4;
                if (strnlen(s, max_len) == 12 && memcmp(s, "GLSL.std.450", 12) == 0) glsl_ext = in[1];
            } else if (op == kTypeInt) {
                if (in.size() != 4) { malformed = true; return; }
                if (in[2] == 32) int32_signed[in[1]] = in[3] != 0;
            } else if (op == kTypeFloat) {
                if (in.size() != 3) { malformed = true; return; }
                if (in[2] == 32) float32.insert(in[1]);
            } else if (op == kTypeVector) {
                if (in.size() != 4) { malformed = true; return; }
                vec_ty[in[1]] = {in[2], in[3]};
            } else if (op == kTypeMatrix) {
                if (in.size() != 4) { malformed = true; return; }
                mat_ty[in[1]] = {in[2], in[3]};
            } else if (op == kTypeArray) {
                if (in.size() != 4) { malformed = true; return; }
                int64_t len = 0;
                bool s = false;
                if (get_int(in[3], len, s) && len > 0) arr_ty[in[1]] = {in[2], (uint32_t)len};
            } else if (op == kTypeRuntimeArray) {
                if (in.size() != 3) { malformed = true; return; }
                rta_elem[in[1]] = in[2];
            } else if (op == kTypeStruct) {
                if (in.size() < 2) { malformed = true; return; }
                struct_ty[in[1]].assign(in.begin() + 2, in.end());
            } else if (op == kTypePointer) {
                if (in.size() != 4) { malformed = true; return; }
                ptr_ty[in[1]] = {in[2], in[3]};
            } else if (op == kConstant) {
                if (in.size() < 4) { malformed = true; return; }
                if (in.size() == 4 && int32_signed.count(in[1])) {
                    const32[in[2]] = {in[1], in[3]};
                    const32_by_key.try_emplace(((uint64_t)in[1] << 32) | in[3], in[2]);
                }
                id_type[in[2]] = in[1];
            } else if (op == kConstantComposite) {
                if (in.size() < 4) { malformed = true; return; }
                composite[in[2]].assign(in.begin() + 3, in.end());
                id_type[in[2]] = in[1];
            } else if (op == kConstantTrue || op == kConstantFalse) {
                if (in.size() != 3) { malformed = true; return; }
                id_type[in[2]] = in[1];
            } else if (op == kVariable) {
                if (in.size() < 4) { malformed = true; return; }
                id_type[in[2]] = in[1];
            }
        }
    }

    bool get_raw(uint32_t id, uint32_t& raw) const {
        auto it = const32.find(id);
        if (it == const32.end()) return false;
        raw = it->second.second;
        return true;
    }

    bool get_int(uint32_t id, int64_t& val, bool& is_signed) const {
        auto it = const32.find(id);
        if (it == const32.end()) return false;
        is_signed = int32_signed.at(it->second.first);
        val = is_signed ? (int64_t)(int32_t)it->second.second : (int64_t)it->second.second;
        return true;
    }

    uint32_t get_or_make_const(Module& m, std::vector<Ins>& new_consts, uint32_t ty, uint32_t raw) {
        const uint64_t key = ((uint64_t)ty << 32) | raw;
        auto it = const32_by_key.find(key);
        if (it != const32_by_key.end()) return it->second;
        const uint32_t id = m.header[3]++;
        new_consts.push_back({(4u << 16) | kConstant, ty, id, raw});
        const32[id] = {ty, raw};
        const32_by_key[key] = id;
        id_type[id] = ty;
        return id;
    }
};

/// Insert new global OpConstant instructions right before the first OpFunction (after all OpType* and prior constants)
inline void insert_new_consts(Module& m, size_t before_func, std::vector<Ins>& new_consts) {
    if (new_consts.empty()) return;
    m.ins.insert(m.ins.begin() + (ptrdiff_t)before_func, new_consts.begin(), new_consts.end());
    new_consts.clear();
}

/// False when substituting `repl` as index operand `idx_pos` (>= 4) of an OpAccessChain would put a negative or
/// out-of-bounds OpConstant into a fixed-size composite (forbidden by SPIR-V validation even in untaken branches).
inline bool safe_access_chain_const(const Ins& in, size_t idx_pos, uint32_t repl, const TypeInfo& ti,
                                    const std::unordered_map<uint32_t, uint32_t>& id_ty) {
    int64_t val = 0;
    bool is_signed = false;
    if (!ti.get_int(repl, val, is_signed)) return true;
    if (is_signed && val < 0) return false;
    if (in.size() <= 4) return true;
    auto bt = id_ty.find(in[3]);
    if (bt == id_ty.end()) return true;
    auto pt = ti.ptr_ty.find(bt->second);
    if (pt == ti.ptr_ty.end()) return true;
    uint32_t cur_ty = pt->second.second;
    for (size_t j = 4; j < idx_pos; j++) {
        if (auto it = ti.arr_ty.find(cur_ty); it != ti.arr_ty.end()) cur_ty = it->second.first;
        else if (auto it = ti.rta_elem.find(cur_ty); it != ti.rta_elem.end()) cur_ty = it->second;
        else if (auto it = ti.vec_ty.find(cur_ty); it != ti.vec_ty.end()) cur_ty = it->second.first;
        else if (auto it = ti.mat_ty.find(cur_ty); it != ti.mat_ty.end()) cur_ty = it->second.first;
        else if (auto it = ti.struct_ty.find(cur_ty); it != ti.struct_ty.end()) {
            int64_t mi = 0;
            bool ms = false;
            if (!ti.get_int(in[j], mi, ms) || mi < 0 || (size_t)mi >= it->second.size()) return false;
            cur_ty = it->second[(size_t)mi];
        } else {
            return true;
        }
    }
    if (auto it = ti.arr_ty.find(cur_ty); it != ti.arr_ty.end()) return (uint64_t)val < it->second.second;
    if (auto it = ti.vec_ty.find(cur_ty); it != ti.vec_ty.end()) return (uint64_t)val < it->second.second;
    if (auto it = ti.mat_ty.find(cur_ty); it != ti.mat_ty.end()) return (uint64_t)val < it->second.second;
    if (auto it = ti.struct_ty.find(cur_ty); it != ti.struct_ty.end()) return (uint64_t)val < it->second.size();
    return true;
}

inline void apply_aliases(Module& m, const std::unordered_map<uint32_t, uint32_t>& alias,
                          const TypeInfo* ti = nullptr, const std::unordered_map<uint32_t, uint32_t>* id_ty = nullptr) {
    if (alias.empty()) return;
    auto resolve = [&](uint32_t id) {
        int guard = 0;
        while (guard++ < 64) {
            auto it = alias.find(id);
            if (it == alias.end() || it->second == id) break;
            id = it->second;
        }
        return id;
    };
    for (Ins& in : m.ins) {
        const uint32_t op = in[0] & 0xffff;
        if (op == kName || op == kDecorate || op == kGroupDecorate || op == kDecorateId || op == kDecorateString) continue;
        for_each_id(in, [&](uint32_t& id, bool is_res) {
            if (!is_res) {
                const uint32_t r = resolve(id);
                if (r != id) {
                    const size_t pos = (size_t)(&id - in.data());
                    if (op == kAccessChain && pos >= 4 && ti && id_ty && !safe_access_chain_const(in, pos, r, *ti, *id_ty))
                        return;
                    id = r;
                }
            }
        });
    }
}

inline void strip_annotations(Module& m, const std::unordered_set<uint32_t>& dead_ids, bool drop_decorates = true) {
    if (dead_ids.empty()) return;
    std::vector<Ins> out;
    out.reserve(m.ins.size());
    for (Ins& in : m.ins) {
        const uint32_t op = in[0] & 0xffff;
        if (in.size() >= 2 && dead_ids.count(in[1]) && (op == kName || (drop_decorates && op == kDecorate))) continue;
        out.push_back(std::move(in));
    }
    m.ins.swap(out);
}

/// True when OpCompositeConstruct's operands map 1-to-1 to the composite's elements (for vectors: all scalar lanes,
/// no mixed vector+scalar packing).
inline bool is_flat_construct(const Ins& in, const TypeInfo& ti, const std::unordered_map<uint32_t, uint32_t>& id_ty) {
    if (in.size() < 4) return false;
    auto vit = ti.vec_ty.find(in[1]);
    if (vit == ti.vec_ty.end()) return true;   // array / struct: each operand is one element/member
    if (in.size() - 3 != vit->second.second) return false;
    for (size_t j = 3; j < in.size(); j++) {
        auto ot = id_ty.find(in[j]);
        if (ot == id_ty.end() || ot->second != vit->second.first) return false;
    }
    return true;
}

/// Function variables (uninitialized) whose pointers and derived OpAccessChain pointers never escape to anything other
/// than OpAccessChain, plain 4-word OpLoad, plain 3-word OpStore, OpName and OpDecorate.
inline std::unordered_set<uint32_t> find_non_escaping_fvars(const Module& m,
                                                            std::unordered_map<uint32_t, uint32_t>& ptr_root,
                                                            std::vector<std::pair<size_t, uint32_t>>* fvar_order = nullptr) {
    std::unordered_set<uint32_t> fvars;
    ptr_root.clear();
    if (fvar_order) fvar_order->clear();
    size_t func_idx = 0;
    for (size_t k = 0; k < m.ins.size(); k++) {
        const Ins& in = m.ins[k];
        const uint32_t op = in[0] & 0xffff;
        if (op == kFunction) { func_idx = k; continue; }
        if (op == kVariable && in.size() == 4 && in[3] == 7) {
            fvars.insert(in[2]);
            if (fvar_order) fvar_order->push_back({func_idx, in[2]});
        } else if (op == kAccessChain && in.size() >= 5) {
            const uint32_t base = in[3];
            const uint32_t r = fvars.count(base) ? base : (ptr_root.count(base) ? ptr_root[base] : 0);
            if (r) ptr_root[in[2]] = r;
        }
    }
    if (fvars.empty()) return fvars;
    std::unordered_set<uint32_t> bad;
    std::vector<uint32_t> at;
    for (const Ins& raw : m.ins) {
        Ins one = raw;
        const uint32_t op = one[0] & 0xffff;
        at.clear();
        if (!for_each_id(one, [&](uint32_t& id, bool) { at.push_back((uint32_t)(&id - one.data())); })) {
            at.clear();
            for (uint32_t k = 1; k < one.size(); k++) at.push_back(k);
        }
        for (uint32_t idx : at) {
            const uint32_t id = one[idx];
            const bool is_var = fvars.count(id) != 0;
            const auto pr = ptr_root.find(id);
            const bool is_chain = pr != ptr_root.end();
            if (!is_var && !is_chain) continue;
            if (is_var && op == kVariable && idx == 2) continue;
            if ((op == kName || op == kDecorate) && idx == 1) continue;
            if (op == kAccessChain && one.size() >= 5 && (idx == 3 || (is_chain && idx == 2))) continue;
            if (op == kLoad && one.size() == 4 && idx == 3) continue;
            if (op == kStore && one.size() == 3 && idx == 1) continue;
            bad.insert(is_var ? id : pr->second);
        }
    }
    for (uint32_t v : bad) fvars.erase(v);
    for (auto it = ptr_root.begin(); it != ptr_root.end();) {
        if (bad.count(it->second)) it = ptr_root.erase(it);
        else ++it;
    }
    return fvars;
}

/// Non-escaping Function variables that are only ever stored whole with one OpConstantComposite (no memory operands)
/// and never written through derived pointers.
inline std::unordered_map<uint32_t, uint32_t> find_immutable_const_vars(
    const Module& m, const TypeInfo& ti, std::vector<std::tuple<size_t, uint32_t, uint32_t>>* ordered = nullptr) {
    std::unordered_map<uint32_t, uint32_t> ptr_root;
    std::vector<std::pair<size_t, uint32_t>> fvar_order;
    const auto fvars = find_non_escaping_fvars(m, ptr_root, &fvar_order);
    if (fvars.empty()) return {};

    std::unordered_map<uint32_t, uint32_t> cand;
    std::unordered_map<uint32_t, int> store_cnt;
    std::unordered_set<uint32_t> bad;
    for (const Ins& in : m.ins) {
        const uint32_t op = in[0] & 0xffff;
        if (op == kStore && in.size() >= 3) {
            const uint32_t dst = in[1], val = in[2];
            if (fvars.count(dst)) {
                const uint32_t c = (in.size() == 3 && ti.composite.count(val)) ? val : 0;
                if (store_cnt[dst]++ == 0) cand[dst] = c;
                else if (cand[dst] != c) cand[dst] = 0;
            } else if (ptr_root.count(dst)) {
                bad.insert(ptr_root[dst]);
            }
        }
    }
    std::unordered_map<uint32_t, uint32_t> out;
    if (ordered) ordered->clear();
    for (const auto& [fn, v] : fvar_order) {
        if (!fvars.count(v) || bad.count(v)) continue;
        auto it = cand.find(v);
        if (it != cand.end() && it->second) {
            out[v] = it->second;
            if (ordered) ordered->emplace_back(fn, v, it->second);
        }
    }
    return out;
}

/// Hoist OpStore of immutable constant-composite Function variables once to the entry block of each variable's own function,
/// so unrolling never clones 576-word constant array initializations.
inline bool hoist_const_var_stores(Module& m) {
    TypeInfo ti;
    ti.scan(m);
    if (ti.malformed || ti.first_func >= m.ins.size()) return false;
    std::vector<std::tuple<size_t, uint32_t, uint32_t>> ordered;
    const auto imm = find_immutable_const_vars(m, ti, &ordered);
    if (imm.empty()) return false;

    std::unordered_map<size_t, std::vector<std::pair<uint32_t, uint32_t>>> per_insert;
    std::unordered_map<size_t, size_t> fn_insert;
    for (const auto& [fn, v, c] : ordered) {
        auto it = fn_insert.find(fn);
        if (it == fn_insert.end()) {
            size_t at = fn + 1;
            while (at < m.ins.size() && (m.ins[at][0] & 0xffff) != kLabel && (m.ins[at][0] & 0xffff) != kFunctionEnd) at++;
            if (at >= m.ins.size() || (m.ins[at][0] & 0xffff) != kLabel) return false;
            at++;
            size_t insert_at = at;
            for (size_t j = at; j < m.ins.size(); j++) {
                const uint32_t jop = m.ins[j][0] & 0xffff;
                if (jop == kVariable) insert_at = j + 1;
                else if (jop == kLabel || jop == kFunctionEnd || is_terminator(jop)) break;
            }
            it = fn_insert.emplace(fn, insert_at).first;
        }
        per_insert[it->second].push_back({v, c});
    }

    bool changed = false;
    std::vector<Ins> out;
    out.reserve(m.ins.size());
    for (size_t k = 0; k < m.ins.size(); k++) {
        if (auto it = per_insert.find(k); it != per_insert.end()) {
            for (const auto& [v, c] : it->second)
                out.push_back({(3u << 16) | kStore, v, c});
        }
        const Ins& in = m.ins[k];
        if ((in[0] & 0xffff) == kStore && in.size() == 3 && imm.count(in[1])) {
            changed = true;
            continue;
        }
        out.push_back(in);
    }
    if (changed) m.ins.swap(out);
    return changed;
}

/// Constant folding, composite extract/shuffle/bitcast simplification, and immutable constant-array load folding
inline bool fold_constants(Module& m) {
    TypeInfo ti;
    ti.scan(m);
    if (ti.malformed || ti.first_func >= m.ins.size()) return false;
    const auto imm = find_immutable_const_vars(m, ti);

    std::unordered_map<uint32_t, std::vector<uint32_t>> comp_elems = ti.composite;
    std::unordered_map<uint32_t, uint32_t> id_ty = ti.id_type;
    std::unordered_map<uint32_t, uint32_t> bitcast_src;
    std::unordered_map<uint32_t, uint32_t> ptr_const_val;
    std::unordered_map<uint32_t, uint32_t> alias;
    std::vector<Ins> new_consts;

    auto res_id = [&](uint32_t id) {
        int g = 0;
        while (g++ < 64) {
            auto it = alias.find(id);
            if (it == alias.end() || it->second == id) break;
            id = it->second;
        }
        return id;
    };

    bool changed = false;
    for (size_t k = ti.first_func; k < m.ins.size(); k++) {
        Ins& in = m.ins[k];
        const uint32_t op = in[0] & 0xffff;
        if (op == kFunction) continue;

        if (op != kName && op != kDecorate && op != kGroupDecorate && op != kDecorateId && op != kDecorateString) {
            for_each_id(in, [&](uint32_t& id, bool is_res) {
                if (!is_res) {
                    const uint32_t r = res_id(id);
                    if (r != id) {
                        const size_t pos = (size_t)(&id - in.data());
                        if (op == kAccessChain && pos >= 4 && !safe_access_chain_const(in, pos, r, ti, id_ty))
                            return;
                        id = r;
                        changed = true;
                    }
                }
            });
        }

        // Record result type up front for T R instructions so downstream checks always see it
        uint32_t res_out = 0;
        if (in.size() >= 3) {
            const char* kspec = spirv_operands::of(op);
            if (kspec && kspec[0] == 'T' && kspec[1] == 'R' && in[2]) {
                res_out = in[2];
                id_ty[res_out] = in[1];
            }
        }
        const bool dec_res = res_out && ti.decorated.count(res_out) != 0;

        if (op == kCopyObject && in.size() == 4) {
            if (!dec_res) { alias[in[2]] = in[3]; changed = true; }
            continue;
        }
        if (op == kCompositeConstruct && in.size() >= 4) {
            if (is_flat_construct(in, ti, id_ty))
                comp_elems[in[2]].assign(in.begin() + 3, in.end());
            continue;
        }
        if (op == kCompositeExtract && in.size() == 5) {
            if (!dec_res) {
                auto it = comp_elems.find(in[3]);
                if (it != comp_elems.end() && in[4] < it->second.size()) {
                    const uint32_t elem = res_id(it->second[in[4]]);
                    auto et = id_ty.find(elem);
                    if (et != id_ty.end() && et->second == in[1]) {
                        alias[in[2]] = elem;
                        changed = true;
                        continue;
                    }
                }
            }
            continue;
        }
        if (op == kVectorShuffle && in.size() >= 6) {
            auto it1 = comp_elems.find(in[3]), it2 = comp_elems.find(in[4]);
            if (it1 != comp_elems.end() && it2 != comp_elems.end()) {
                const size_t n1 = it1->second.size(), n2 = it2->second.size();
                std::vector<uint32_t> elems;
                bool ok = true;
                for (size_t j = 5; j < in.size(); j++) {
                    const uint32_t idx = in[j];
                    if (idx < n1) elems.push_back(res_id(it1->second[idx]));
                    else if (idx < n1 + n2) elems.push_back(res_id(it2->second[idx - n1]));
                    else { ok = false; break; }
                }
                if (ok) comp_elems[in[2]] = std::move(elems);
            }
            continue;
        }
        if (op == kBitcast && in.size() == 4) {
            bitcast_src[in[2]] = in[3];
            if (!dec_res) {
                auto st = id_ty.find(in[3]);
                if (st != id_ty.end() && st->second == in[1]) {
                    alias[in[2]] = in[3];
                    changed = true;
                    continue;
                }
                if (auto bs = bitcast_src.find(in[3]); bs != bitcast_src.end()) {
                    const uint32_t orig = res_id(bs->second);
                    auto ot = id_ty.find(orig);
                    if (ot != id_ty.end() && ot->second == in[1]) {
                        alias[in[2]] = orig;
                        changed = true;
                        continue;
                    }
                }
                if (auto it = ti.const32.find(in[3]); it != ti.const32.end() && ti.int32_signed.count(in[1])) {
                    alias[in[2]] = ti.get_or_make_const(m, new_consts, in[1], it->second.second);
                    changed = true;
                    continue;
                }
            }
            auto vit = ti.vec_ty.find(in[1]);
            auto cit = comp_elems.find(in[3]);
            if (vit != ti.vec_ty.end() && cit != comp_elems.end() &&
                cit->second.size() == vit->second.second && ti.int32_signed.count(vit->second.first)) {
                std::vector<uint32_t> mapped;
                bool all_const = true;
                for (uint32_t e : cit->second) {
                    e = res_id(e);
                    auto ec = ti.const32.find(e);
                    if (ec != ti.const32.end())
                        mapped.push_back(ti.get_or_make_const(m, new_consts, vit->second.first, ec->second.second));
                    else { all_const = false; break; }
                }
                if (all_const) comp_elems[in[2]] = std::move(mapped);
            }
            continue;
        }

        // Scalar 32-bit integer constant folding and identities
        if (!dec_res && in.size() == 5 && ti.int32_signed.count(in[1])) {
            bool s1 = false, s2 = false;
            int64_t v1 = 0, v2 = 0;
            const bool c1 = ti.get_int(in[3], v1, s1), c2 = ti.get_int(in[4], v2, s2);
            if (c1 && c2) {
                const uint32_t u1 = (uint32_t)v1, u2 = (uint32_t)v2;
                const int32_t i1 = (int32_t)v1, i2 = (int32_t)v2;
                uint32_t res = 0;
                bool ok = true;
                switch (op) {
                case kIAdd: res = u1 + u2; break;
                case kISub: res = u1 - u2; break;
                case kIMul: res = u1 * u2; break;
                case kUDiv: if (u2) res = u1 / u2; else ok = false; break;
                case kSDiv: if (i2 && !(i1 == INT32_MIN && i2 == -1)) res = (uint32_t)(i1 / i2); else ok = false; break;
                case kUMod: if (u2) res = u1 % u2; else ok = false; break;
                case kShiftRightLogical: if (u2 < 32) res = u1 >> u2; else ok = false; break;
                case kShiftRightArithmetic: if (u2 < 32) res = (i1 < 0 && u2 > 0) ? ~(~u1 >> u2) : (u1 >> u2); else ok = false; break;
                case kShiftLeftLogical: if (u2 < 32) res = u1 << u2; else ok = false; break;
                case kBitwiseOr: res = u1 | u2; break;
                case kBitwiseXor: res = u1 ^ u2; break;
                case kBitwiseAnd: res = u1 & u2; break;
                default: ok = false; break;
                }
                if (ok) {
                    alias[in[2]] = ti.get_or_make_const(m, new_consts, in[1], res);
                    changed = true;
                    continue;
                }
            } else {
                uint32_t repl = 0;
                if (op == kIAdd) { if (c1 && v1 == 0) repl = in[4]; else if (c2 && v2 == 0) repl = in[3]; }
                else if (op == kISub) { if (c2 && v2 == 0) repl = in[3]; }
                else if (op == kIMul) {
                    if (c1 && v1 == 1) repl = in[4];
                    else if (c2 && v2 == 1) repl = in[3];
                    else if ((c1 && v1 == 0) || (c2 && v2 == 0)) repl = ti.get_or_make_const(m, new_consts, in[1], 0);
                } else if (op == kUDiv || op == kSDiv) { if (c2 && v2 == 1) repl = in[3]; }
                else if (op == kShiftRightLogical || op == kShiftRightArithmetic || op == kShiftLeftLogical ||
                         op == kBitwiseOr || op == kBitwiseXor) {
                    if (c2 && v2 == 0) repl = in[3];
                }
                if (repl) {
                    auto rt = id_ty.find(repl);
                    if (rt != id_ty.end() && rt->second == in[1]) {
                        alias[in[2]] = repl;
                        changed = true;
                        continue;
                    }
                }
            }
        }

        // Vector 32-bit integer folding (tracks per-lane values for OpCompositeExtract)
        if (!dec_res && in.size() == 5 && ti.vec_ty.count(in[1])) {
            const auto [ety, cnt] = ti.vec_ty[in[1]];
            if (ti.int32_signed.count(ety)) {
                auto c1 = comp_elems.find(in[3]), c2 = comp_elems.find(in[4]);
                if (c1 != comp_elems.end() && c2 != comp_elems.end() &&
                    c1->second.size() == cnt && c2->second.size() == cnt) {
                    std::vector<uint32_t> res_elems;
                    bool ok = true;
                    for (uint32_t j = 0; j < cnt; j++) {
                        const uint32_t e1 = res_id(c1->second[j]), e2 = res_id(c2->second[j]);
                        bool s1 = false, s2 = false;
                        int64_t v1 = 0, v2 = 0;
                        const bool k1 = ti.get_int(e1, v1, s1), k2 = ti.get_int(e2, v2, s2);
                        if (k1 && k2) {
                            const uint32_t u1 = (uint32_t)v1, u2 = (uint32_t)v2;
                            const int32_t i1 = (int32_t)v1, i2 = (int32_t)v2;
                            uint32_t r = 0;
                            switch (op) {
                            case kIAdd: r = u1 + u2; break;
                            case kISub: r = u1 - u2; break;
                            case kIMul: r = u1 * u2; break;
                            case kUDiv: if (u2) r = u1 / u2; else ok = false; break;
                            case kSDiv: if (i2 && !(i1 == INT32_MIN && i2 == -1)) r = (uint32_t)(i1 / i2); else ok = false; break;
                            default: ok = false; break;
                            }
                            if (!ok) break;
                            res_elems.push_back(ti.get_or_make_const(m, new_consts, ety, r));
                        } else if (op == kIAdd && k1 && v1 == 0 && id_ty.count(e2) && id_ty[e2] == ety) res_elems.push_back(e2);
                        else if (op == kIAdd && k2 && v2 == 0 && id_ty.count(e1) && id_ty[e1] == ety) res_elems.push_back(e1);
                        else if (op == kIMul && k2 && v2 == 1 && id_ty.count(e1) && id_ty[e1] == ety) res_elems.push_back(e1);
                        else if (op == kIMul && k1 && v1 == 1 && id_ty.count(e2) && id_ty[e2] == ety) res_elems.push_back(e2);
                        else if (op == kIMul && ((k1 && v1 == 0) || (k2 && v2 == 0)))
                            res_elems.push_back(ti.get_or_make_const(m, new_consts, ety, 0));
                        else if (op == kUDiv && k2 && v2 == 1 && id_ty.count(e1) && id_ty[e1] == ety) res_elems.push_back(e1);
                        else { ok = false; break; }
                    }
                    if (ok) comp_elems[in[2]] = std::move(res_elems);
                }
            }
        }

        // Immutable constant-array OpAccessChain + OpLoad -> constant element
        if (op == kAccessChain && in.size() >= 5 && imm.count(in[3])) {
            uint32_t cur = imm.at(in[3]);
            bool ok = true;
            for (size_t j = 4; j < in.size(); j++) {
                int64_t idx = 0;
                bool s = false;
                if (!ti.get_int(in[j], idx, s) || idx < 0) { ok = false; break; }
                auto cit = ti.composite.find(cur);
                if (cit == ti.composite.end() || (size_t)idx >= cit->second.size()) { ok = false; break; }
                cur = cit->second[(size_t)idx];
            }
            if (ok) ptr_const_val[in[2]] = cur;
        }
        if (!dec_res && op == kLoad && in.size() == 4) {
            if (auto it = ptr_const_val.find(in[3]); it != ptr_const_val.end()) {
                auto et = id_ty.find(it->second);
                if (et != id_ty.end() && et->second == in[1]) {
                    alias[in[2]] = it->second;
                    changed = true;
                    continue;
                }
            }
        }
    }

    if (!new_consts.empty()) insert_new_consts(m, ti.first_func, new_consts);
    if (!alias.empty()) apply_aliases(m, alias, &ti, &id_ty);
    return changed;
}

/// Intra-block store-to-load forwarding and dead-store elimination for non-escaping Function-scope OpVariables,
/// with multi-dimensional constant-indexed OpAccessChain, flat OpCompositeConstruct / bitcasted vector unpacking, and vector reassembly.
inline bool forward_function_allocas(Module& m) {
    TypeInfo ti;
    ti.scan(m);
    if (ti.malformed || ti.first_func >= m.ins.size()) return false;
    std::unordered_map<uint32_t, uint32_t> ptr_root;
    const auto fvars = find_non_escaping_fvars(m, ptr_root);
    if (fvars.empty()) return false;

    std::unordered_map<uint32_t, uint32_t> id_ty = ti.id_type;
    for (const Ins& in : m.ins) {
        const uint32_t op = in[0] & 0xffff;
        const char* kspec = spirv_operands::of(op);
        if (kspec && kspec[0] == 'T' && kspec[1] == 'R' && in.size() >= 3) id_ty[in[2]] = in[1];
    }

    struct PtrLoc {
        uint32_t var = 0;
        std::vector<uint32_t> path;
        bool dynamic = false;
    };
    std::unordered_map<uint32_t, PtrLoc> ptrs;
    for (uint32_t v : fvars) ptrs[v] = {v, {}, false};

    std::unordered_map<uint32_t, std::vector<uint32_t>> constructs;
    for (const Ins& in : m.ins) {
        const uint32_t op = in[0] & 0xffff;
        if (op == kCompositeConstruct && is_flat_construct(in, ti, id_ty)) {
            constructs[in[2]].assign(in.begin() + 3, in.end());
        } else if (op == kConstantComposite && in.size() >= 4) {
            constructs[in[2]].assign(in.begin() + 3, in.end());
        } else if (op == kAccessChain && in.size() >= 5) {
            if (auto it = ptrs.find(in[3]); it != ptrs.end()) {
                PtrLoc loc = it->second;
                for (size_t j = 4; j < in.size(); j++) {
                    int64_t idx = 0;
                    bool s = false;
                    if (!loc.dynamic && ti.get_int(in[j], idx, s) && idx >= 0) {
                        loc.path.push_back((uint32_t)idx);
                    } else {
                        loc.dynamic = true;
                        loc.path.clear();
                    }
                }
                ptrs[in[2]] = std::move(loc);
            }
        }
    }

    std::unordered_map<uint32_t, std::vector<std::pair<size_t, size_t>>> ac_index_uses;
    for (size_t k = 0; k < m.ins.size(); k++) {
        const Ins& in = m.ins[k];
        if ((in[0] & 0xffff) == kAccessChain && in.size() >= 5)
            for (size_t j = 4; j < in.size(); j++) ac_index_uses[in[j]].push_back({k, j});
    }

    auto overlaps = [](const std::vector<uint32_t>& a, const std::vector<uint32_t>& b) {
        const size_t n = std::min(a.size(), b.size());
        for (size_t i = 0; i < n; i++) if (a[i] != b[i]) return false;
        return true;
    };
    auto is_32bit_scalar = [&](uint32_t ty) {
        return ti.int32_signed.count(ty) || ti.float32.count(ty);
    };

    using Path = std::vector<uint32_t>;
    std::unordered_map<uint32_t, std::map<Path, uint32_t>> val_map;
    std::unordered_map<uint32_t, std::map<Path, size_t>> last_store_idx;
    std::unordered_map<uint32_t, uint32_t> alias;
    std::unordered_set<uint32_t> removed_loads;
    std::unordered_set<size_t> dead_stores;
    std::unordered_map<uint32_t, uint32_t> vec_bitcast_src;

    auto resolve = [&](uint32_t id) {
        int g = 0;
        while (g++ < 64) {
            auto it = alias.find(id);
            if (it == alias.end() || it->second == id) break;
            id = it->second;
        }
        return id;
    };

    std::vector<Ins> rebuilt;
    rebuilt.reserve(m.ins.size() + 256);
    bool changed = false, in_func = false;
    for (size_t k = 0; k < m.ins.size(); k++) {
        Ins in = m.ins[k];
        const uint32_t op = in[0] & 0xffff;
        if (op == kFunction) { in_func = true; val_map.clear(); last_store_idx.clear(); rebuilt.push_back(std::move(in)); continue; }
        if (op == kFunctionEnd) { in_func = false; val_map.clear(); last_store_idx.clear(); rebuilt.push_back(std::move(in)); continue; }
        if (!in_func) { rebuilt.push_back(std::move(in)); continue; }
        if (op == kLabel) { val_map.clear(); last_store_idx.clear(); rebuilt.push_back(std::move(in)); continue; }

        for_each_id(in, [&](uint32_t& id, bool is_res) {
            if (!is_res) {
                const uint32_t r = resolve(id);
                if (r != id) {
                    const size_t pos = (size_t)(&id - in.data());
                    if (op == kAccessChain && pos >= 4 && !safe_access_chain_const(in, pos, r, ti, id_ty)) return;
                    id = r;
                    changed = true;
                }
            }
        });

        if (op == kCompositeConstruct && in.size() >= 4) {
            id_ty[in[2]] = in[1];
            if (is_flat_construct(in, ti, id_ty))
                constructs[in[2]].assign(in.begin() + 3, in.end());
        } else if (op == kBitcast && in.size() == 4) {
            id_ty[in[2]] = in[1];
            if (ti.vec_ty.count(in[1])) vec_bitcast_src[in[2]] = in[3];
        }

        if (op == kStore && in.size() >= 3) {
            if (auto pit = ptrs.find(in[1]); pit != ptrs.end()) {
                const PtrLoc& loc = pit->second;
                if (loc.dynamic || in.size() != 3) {
                    val_map.erase(loc.var);
                    last_store_idx.erase(loc.var);
                } else {
                    auto& vm = val_map[loc.var];
                    for (auto it = vm.begin(); it != vm.end();) {
                        if (overlaps(it->first, loc.path)) it = vm.erase(it);
                        else ++it;
                    }
                    auto& sm = last_store_idx[loc.var];
                    if (auto sit = sm.find(loc.path); sit != sm.end()) dead_stores.insert(sit->second);
                    for (auto it = sm.begin(); it != sm.end();) {
                        if (it->first.size() > loc.path.size() && overlaps(it->first, loc.path)) {
                            dead_stores.insert(it->second);
                            it = sm.erase(it);
                        } else if (it->first.size() < loc.path.size() && overlaps(it->first, loc.path)) {
                            it = sm.erase(it);
                        } else {
                            ++it;
                        }
                    }

                    const uint32_t val = resolve(in[2]);
                    vm[loc.path] = val;
                    if (auto cit = constructs.find(val); cit != constructs.end()) {
                        for (uint32_t ei = 0; ei < cit->second.size(); ei++) {
                            Path sub = loc.path;
                            sub.push_back(ei);
                            vm[sub] = resolve(cit->second[ei]);
                        }
                    } else if (auto bit = vec_bitcast_src.find(val); bit != vec_bitcast_src.end()) {
                        auto vt = id_ty.find(val);
                        auto vit = vt != id_ty.end() ? ti.vec_ty.find(vt->second) : ti.vec_ty.end();
                        if (vit != ti.vec_ty.end() && is_32bit_scalar(vit->second.first)) {
                            auto scit = constructs.find(resolve(bit->second));
                            if (scit != constructs.end() && scit->second.size() == vit->second.second) {
                                bool all_32 = true;
                                for (uint32_t raw_se : scit->second) {
                                    auto set = id_ty.find(resolve(raw_se));
                                    if (set == id_ty.end() || !is_32bit_scalar(set->second)) { all_32 = false; break; }
                                }
                                if (all_32) {
                                    const uint32_t ety = vit->second.first;
                                    for (uint32_t ei = 0; ei < scit->second.size(); ei++) {
                                        const uint32_t se = resolve(scit->second[ei]);
                                        uint32_t elem_val = se;
                                        if (id_ty[se] != ety) {
                                            elem_val = m.header[3]++;
                                            rebuilt.push_back({(4u << 16) | kBitcast, ety, elem_val, se});
                                            id_ty[elem_val] = ety;
                                        }
                                        Path sub = loc.path;
                                        sub.push_back(ei);
                                        vm[sub] = elem_val;
                                    }
                                }
                            }
                        }
                    }
                    // Record store index after any unpacking OpBitcasts emitted above, right before pushing the OpStore
                    sm[loc.path] = rebuilt.size();
                }
            }
            rebuilt.push_back(std::move(in));
        } else if (op == kLoad && in.size() >= 4) {
            bool forwarded = false;
            if (auto pit = ptrs.find(in[3]); pit != ptrs.end()) {
                const PtrLoc& loc = pit->second;
                if (loc.dynamic || in.size() != 4) {
                    last_store_idx.erase(loc.var);
                } else {
                    const bool dec_load = ti.decorated.count(in[2]) != 0;
                    if (!dec_load) {
                        if (auto vmit = val_map.find(loc.var); vmit != val_map.end()) {
                            if (auto eit = vmit->second.find(loc.path); eit != vmit->second.end()) {
                                const uint32_t v = resolve(eit->second);
                                auto vt = id_ty.find(v);
                                if (vt != id_ty.end() && vt->second == in[1]) {
                                    bool ac_safe = true;
                                    if (ti.const32.count(v)) {
                                        if (auto uit = ac_index_uses.find(in[2]); uit != ac_index_uses.end()) {
                                            for (const auto& [ui, upos] : uit->second) {
                                                if (!safe_access_chain_const(m.ins[ui], upos, v, ti, id_ty)) {
                                                    ac_safe = false;
                                                    break;
                                                }
                                            }
                                        }
                                    }
                                    if (ac_safe) {
                                        alias[in[2]] = v;
                                        removed_loads.insert(in[2]);
                                        changed = true;
                                        forwarded = true;
                                    }
                                }
                            } else if (ti.vec_ty.count(in[1])) {
                                const auto [ety, cnt] = ti.vec_ty[in[1]];
                                Ins cc = {((3u + cnt) << 16) | kCompositeConstruct, in[1], in[2]};
                                bool all_have = true;
                                for (uint32_t ei = 0; ei < cnt; ei++) {
                                    Path sub = loc.path;
                                    sub.push_back(ei);
                                    auto seit = vmit->second.find(sub);
                                    if (seit == vmit->second.end()) { all_have = false; break; }
                                    const uint32_t ev = resolve(seit->second);
                                    auto evt = id_ty.find(ev);
                                    if (evt == id_ty.end() || evt->second != ety) { all_have = false; break; }
                                    cc.push_back(ev);
                                }
                                if (all_have) {
                                    constructs[in[2]].assign(cc.begin() + 3, cc.end());
                                    id_ty[in[2]] = in[1];
                                    rebuilt.push_back(std::move(cc));
                                    changed = true;
                                    forwarded = true;
                                }
                            }
                        }
                    }
                    if (!forwarded) {
                        if (auto smit = last_store_idx.find(loc.var); smit != last_store_idx.end()) {
                            for (auto it = smit->second.begin(); it != smit->second.end();) {
                                if (overlaps(it->first, loc.path)) it = smit->second.erase(it);
                                else ++it;
                            }
                        }
                    }
                }
            }
            if (!forwarded) rebuilt.push_back(std::move(in));
        } else {
            if (op == kFunctionCall) { val_map.clear(); last_store_idx.clear(); }
            rebuilt.push_back(std::move(in));
        }
    }

    if (!dead_stores.empty()) {
        std::vector<Ins> out;
        out.reserve(rebuilt.size() - dead_stores.size());
        for (size_t k = 0; k < rebuilt.size(); k++)
            if (!dead_stores.count(k)) out.push_back(std::move(rebuilt[k]));
        rebuilt.swap(out);
        changed = true;
    }
    if (changed) {
        m.ins.swap(rebuilt);
        if (!alias.empty()) apply_aliases(m, alias, &ti, &id_ty);
        // Only OpName is stripped here (loads carrying OpDecorate were not forwarded)
        if (!removed_loads.empty()) strip_annotations(m, removed_loads, false);
    }
    return changed;
}

/// Eliminate unused pure instructions, unused plain loads, unused OpConstantComposite tables,
/// and non-escaping Function OpVariables that have no remaining uses (to exact fixed point).
inline bool dce(Module& m) {
    TypeInfo ti;
    ti.scan(m);
    if (ti.malformed) return false;
    std::unordered_map<uint32_t, uint32_t> ptr_root;
    auto fvars = find_non_escaping_fvars(m, ptr_root);

    bool any_changed = false;
    std::unordered_set<uint32_t> removed_ids;
    while (true) {
        std::unordered_set<uint32_t> used;
        for (Ins& in : m.ins) {
            const uint32_t op = in[0] & 0xffff;
            if (op == kName || op == kDecorate) continue;
            if (!for_each_id(in, [&](uint32_t& id, bool is_res) { if (!is_res) used.insert(id); })) {
                for (size_t k = 1; k < in.size(); k++) used.insert(in[k]);
            }
        }
        std::unordered_set<uint32_t> live_fvars;
        for (const Ins& in : m.ins) {
            const uint32_t op = in[0] & 0xffff;
            if (op == kLoad && in.size() >= 4 && (in.size() > 4 || used.count(in[2]))) {
                const uint32_t p = in[3];
                if (fvars.count(p)) live_fvars.insert(p);
                else if (ptr_root.count(p)) live_fvars.insert(ptr_root[p]);
            }
        }
        std::unordered_set<uint32_t> dead_fvars;
        for (uint32_t v : fvars) if (!live_fvars.count(v)) dead_fvars.insert(v);

        std::vector<Ins> out;
        out.reserve(m.ins.size());
        bool changed = false;
        for (Ins& in : m.ins) {
            const uint32_t op = in[0] & 0xffff;
            // Only remove the Function OpVariable once all stores and access chains referencing it are gone
            if (op == kVariable && in.size() == 4 && in[3] == 7 && dead_fvars.count(in[2]) && !used.count(in[2])) {
                removed_ids.insert(in[2]);
                fvars.erase(in[2]);
                changed = true; continue;
            }
            if (op == kStore && in.size() == 3) {
                const uint32_t dst = in[1];
                const uint32_t r = fvars.count(dst) ? dst : (ptr_root.count(dst) ? ptr_root[dst] : 0);
                if (r && dead_fvars.count(r)) { changed = true; continue; }
            }
            // (a decorated one stays: BuiltIn WorkgroupSize is read by no instruction, and sets the local size)
            if (op == kConstantComposite && in.size() >= 4 && !used.count(in[2]) && !ti.decorated.count(in[2])) {
                removed_ids.insert(in[2]);
                changed = true; continue;
            }
            if (is_pure_op(op, ti.glsl_ext, in) || (op == kLoad && in.size() == 4)) {
                uint32_t res = 0;
                if (for_each_id(in, [&](uint32_t& id, bool is_res) { if (is_res) res = id; }) && res && !used.count(res)) {
                    removed_ids.insert(res);
                    changed = true; continue;
                }
            }
            out.push_back(std::move(in));
        }
        m.ins.swap(out);
        if (!changed) break;
        any_changed = true;
    }
    if (!removed_ids.empty()) strip_annotations(m, removed_ids, true);
    return any_changed;
}

/// Merge adjacent single-predecessor unconditional branches (A: OpBranch %B; %B = OpLabel ...)
/// when neither A nor B carries a structured merge and B has no OpPhis. Declines modules with non-32-bit OpSwitch.
inline bool merge_trivial_blocks(Module& m) {
    TypeInfo ti;
    ti.scan(m);
    if (ti.malformed) return false;
    std::unordered_map<uint32_t, uint32_t> id_ty = ti.id_type;
    for (const Ins& in : m.ins) {
        const uint32_t op = in[0] & 0xffff;
        const char* kspec = spirv_operands::of(op);
        if (kspec && kspec[0] == 'T' && kspec[1] == 'R' && in.size() >= 3) id_ty[in[2]] = in[1];
        if (op == kSwitch) {
            if (in.size() < 3) return false;
            auto st = id_ty.find(in[1]);
            if (st == id_ty.end() || !ti.int32_signed.count(st->second) || ((in.size() - 3) % 2) != 0)
                return false;   // refuse 64-bit or unknown-width OpSwitch
        }
    }

    bool any = false;
    for (int pass = 0; pass < 8; pass++) {
        std::unordered_set<uint32_t> pinned;
        std::unordered_map<uint32_t, int> pred_cnt;
        std::unordered_map<uint32_t, bool> has_merge_before_term;

        uint32_t cur_lbl = 0;
        bool cur_has_merge = false;
        for (size_t k = 0; k < m.ins.size(); k++) {
            const Ins& in = m.ins[k];
            const uint32_t op = in[0] & 0xffff;
            if (op == kLabel) {
                if (in.size() != 2) return any;
                cur_lbl = in[1];
                cur_has_merge = false;
                if (k > 0 && ((m.ins[k - 1][0] & 0xffff) == kFunction || (m.ins[k - 1][0] & 0xffff) == kFunctionParameter))
                    pinned.insert(cur_lbl);
            } else if (op == kLoopMerge) {
                if (in.size() < 4) return any;
                pinned.insert(in[1]);
                pinned.insert(in[2]);
                pinned.insert(cur_lbl);
                cur_has_merge = true;
            } else if (op == kSelectionMerge) {
                if (in.size() < 3) return any;
                pinned.insert(in[1]);
                pinned.insert(cur_lbl);
                cur_has_merge = true;
            } else if (op == kPhi) {
                pinned.insert(cur_lbl);
            } else if (op == kBranch) {
                if (in.size() != 2) return any;
                has_merge_before_term[cur_lbl] = cur_has_merge;
                pred_cnt[in[1]]++;
            } else if (op == kBranchCond) {
                if (in.size() < 4) return any;
                has_merge_before_term[cur_lbl] = cur_has_merge;
                pred_cnt[in[2]]++;
                pred_cnt[in[3]]++;
                if (in[2] == in[3]) pred_cnt[in[2]]++;
                pinned.insert(in[2]);
                pinned.insert(in[3]);
            } else if (op == kSwitch) {
                has_merge_before_term[cur_lbl] = cur_has_merge;
                pinned.insert(in[2]);
                for (size_t j = 4; j < in.size(); j += 2) pinned.insert(in[j]);
            }
        }

        std::unordered_map<uint32_t, uint32_t> merged_into;
        std::unordered_set<uint32_t> merged_labels;
        std::vector<Ins> out;
        out.reserve(m.ins.size());
        bool changed = false;
        cur_lbl = 0;
        for (size_t k = 0; k < m.ins.size(); k++) {
            const Ins& in = m.ins[k];
            const uint32_t op = in[0] & 0xffff;
            if (op == kLabel) cur_lbl = in[1];
            if (op == kBranch && in.size() == 2 && k + 1 < m.ins.size() &&
                (m.ins[k + 1][0] & 0xffff) == kLabel && m.ins[k + 1].size() == 2 && m.ins[k + 1][1] == in[1] &&
                !has_merge_before_term[cur_lbl] && pred_cnt[in[1]] == 1 && !pinned.count(in[1])) {
                uint32_t target_root = cur_lbl;
                while (merged_into.count(target_root)) target_root = merged_into[target_root];
                merged_into[in[1]] = target_root;
                merged_labels.insert(in[1]);
                k++;
                changed = true;
                continue;
            }
            out.push_back(in);
        }
        if (!changed) break;
        any = true;
        for (Ins& in : out) {
            if ((in[0] & 0xffff) == kPhi) {
                for (size_t j = 4; j < in.size(); j += 2)
                    while (merged_into.count(in[j])) in[j] = merged_into[in[j]];
            }
        }
        m.ins.swap(out);
        strip_annotations(m, merged_labels, true);
    }
    return any;
}

} // namespace detail

/// One innermost loop unrolled, if one can be: true when the module changed
inline bool unroll_one(Module& m, std::unordered_set<uint64_t>& refused, uint32_t max_trips, size_t max_words = ~size_t(0)) {
    auto& I = m.ins;
    detail::TypeInfo ti;
    ti.scan(m);
    if (ti.malformed || ti.first_func >= I.size()) return false;

    size_t cur_words = 5;
    for (const Ins& in : I) cur_words += in.size();

    for (size_t li = ti.first_func; li + 1 < I.size(); li++) {
        if ((I[li][0] & 0xffff) != kLoopMerge || I[li].size() < 4) continue;
        const uint32_t M = I[li][1], cont = I[li][2];
        const uint64_t key = ((uint64_t)M << 32) | cont;
        if (refused.count(key)) continue;

        size_t hi = li;
        while (hi > ti.first_func && (I[hi][0] & 0xffff) != kLabel) hi--;
        if ((I[hi][0] & 0xffff) != kLabel || I[hi].size() != 2) { refused.insert(key); continue; }
        const uint32_t H = I[hi][1];

        const size_t cmp_idx = li - 1;
        if (cmp_idx <= hi + 1) { refused.insert(key); continue; }
        bool all_phis = true;
        for (size_t k = hi + 1; k < cmp_idx; k++)
            if ((I[k][0] & 0xffff) != kPhi || I[k].size() != 7) { all_phis = false; break; }
        const Ins& tl = I[cmp_idx];
        const Ins& bl = I[li + 1];
        const uint32_t cmp_op = tl[0] & 0xffff;
        if (!all_phis || tl.size() != 5 ||
            (cmp_op != kULess && cmp_op != kSLess && cmp_op != kINotEqual) ||
            (bl[0] & 0xffff) != kBranchCond || bl.size() < 4 || bl[1] != tl[2] || bl[3] != M) {
            refused.insert(key); continue;
        }
        const uint32_t B = bl[2], test = tl[2];
        if (B == M) { refused.insert(key); continue; }

        size_t mi = li + 1;
        while (mi < I.size() && !((I[mi][0] & 0xffff) == kLabel && I[mi].size() == 2 && I[mi][1] == M)) mi++;
        if (mi >= I.size()) { refused.insert(key); continue; }

        bool inner = true;
        for (size_t k = li + 1; k < mi && inner; k++) inner = (I[k][0] & 0xffff) != kLoopMerge;
        if (!inner) continue;   // (it may be innermost later)

        size_t ci = li + 1;
        while (ci < mi && !((I[ci][0] & 0xffff) == kLabel && I[ci].size() == 2 && I[ci][1] == cont)) ci++;
        size_t ce = ci + 1;
        while (ce < mi && !is_terminator(I[ce][0] & 0xffff) && (I[ce][0] & 0xffff) != kLabel) ce++;
        if (ci >= mi || ce >= mi || (I[ce][0] & 0xffff) != kBranch || I[ce].size() != 2 || I[ce][1] != H) {
            refused.insert(key); continue;
        }

        struct PhiInfo { uint32_t ty, id, init, pre, back; };
        std::vector<PhiInfo> phis;
        int iv_idx = -1;
        uint32_t limit_id = 0;
        bool phis_ok = true;
        for (size_t k = hi + 1; k < cmp_idx; k++) {
            const Ins& pl = I[k];
            PhiInfo pi{};
            pi.ty = pl[1]; pi.id = pl[2];
            if (pl[6] == cont) { pi.init = pl[3]; pi.pre = pl[4]; pi.back = pl[5]; }
            else if (pl[4] == cont) { pi.init = pl[5]; pi.pre = pl[6]; pi.back = pl[3]; }
            else { phis_ok = false; break; }
            if (pi.id == tl[3]) { iv_idx = (int)phis.size(); limit_id = tl[4]; }
            else if (cmp_op == kINotEqual && pi.id == tl[4] && iv_idx < 0) { iv_idx = (int)phis.size(); limit_id = tl[3]; }
            phis.push_back(pi);
        }
        if (!phis_ok || iv_idx < 0) { refused.insert(key); continue; }

        const PhiInfo& iv = phis[(size_t)iv_idx];
        uint32_t raw_start = 0, raw_limit = 0, raw_step = 0;
        if (!ti.get_raw(iv.init, raw_start) || !ti.get_raw(limit_id, raw_limit)) {
            refused.insert(key); continue;
        }

        size_t sl = ci + 1;
        uint32_t step_id = 0;
        while (sl < ce) {
            if (I[sl].size() == 5 && (I[sl][0] & 0xffff) == kIAdd && I[sl][2] == iv.back) {
                if (I[sl][3] == iv.id) { step_id = I[sl][4]; break; }
                if (I[sl][4] == iv.id) { step_id = I[sl][3]; break; }
            }
            sl++;
        }
        if (sl >= ce || !step_id || !ti.get_raw(step_id, raw_step)) {
            refused.insert(key); continue;
        }

        // Trip count with exact signedness matching the comparison opcode
        int64_t start = 0, limit = 0, step = 0, trips = 0;
        if (cmp_op == kSLess) {
            start = (int32_t)raw_start;
            limit = (int32_t)raw_limit;
            step = (int32_t)raw_step;
            if (step > 0 && limit > start) {
                trips = (limit - start + step - 1) / step;
                if (start + trips * step > (int64_t)INT32_MAX) trips = 0;   // would wrap 32-bit signed
            }
        } else {
            start = (uint32_t)raw_start;
            limit = (uint32_t)raw_limit;
            step = (int32_t)raw_step;
            if (step > 0 && limit > start) {
                if (cmp_op == kINotEqual) {
                    if ((limit - start) % step == 0) trips = (limit - start) / step;
                } else {
                    trips = (limit - start + step - 1) / step;
                    if (start + trips * step > 0xffffffffLL) trips = 0;     // would wrap 32-bit unsigned
                }
            }
        }
        if (trips <= 0 || trips > (int64_t)max_trips) { refused.insert(key); continue; }

        size_t body_words = 0;
        for (size_t k = li + 2; k < mi; k++) body_words += I[k].size();
        if (max_words <= (~size_t(0) / 3) &&
            cur_words + body_words * (size_t)(trips > 1 ? trips - 1 : 0) > max_words * 3) {
            refused.insert(key); continue;
        }

        std::unordered_set<uint32_t> made;
        std::vector<uint32_t> made_order;   // deterministic id allocation order
        std::unordered_set<uint32_t> phi_ids;
        for (const auto& p : phis) phi_ids.insert(p.id);
        bool ok = true;
        // (a phi fed by the counter's step or by the loop's own test: neither is cloned, so a copy would name an id that
        // no instruction defines any more)
        for (const auto& p : phis)
            if (p.id != iv.id && (p.back == iv.back || p.init == iv.back || p.back == test)) ok = false;
        for (size_t k = hi; k < mi && ok; k++) {
            uint32_t r = 0;
            ok = for_each_id(I[k], [&](uint32_t& id, bool res) { if (res) r = id; });
            if (r) {
                if (ti.decorated.count(r)) ok = false;   // preserve semantic OpDecorate (e.g. NoContraction) by refusing
                if (made.insert(r).second) made_order.push_back(r);
            }
        }
        if (!ok) { refused.insert(key); continue; }

        // Check structured CFG safety: no early breaks to M, no OpSwitch, at most one top-level `if (cond) continue;`,
        // and no branch to `cont` from inside any non-retargeted selection.
        bool cont_has_phi = false;
        for (size_t k = ci + 1; k < ce; k++) cont_has_phi |= (I[k][0] & 0xffff) == kPhi;
        bool cont_targeted_by_sel = false;
        for (size_t k = li + 2; k < mi; k++)
            if ((I[k][0] & 0xffff) == kSelectionMerge && I[k].size() >= 3 && I[k][1] == cont) cont_targeted_by_sel = true;

        std::unordered_set<size_t> skip_blocks;
        std::unordered_set<uint32_t> skip_labels;
        std::unordered_map<size_t, uint32_t> sel_retarget;
        std::unordered_map<size_t, std::pair<uint32_t, uint32_t>> cond_retarget;

        std::unordered_set<uint32_t> open_sel_merges;
        for (size_t k = li + 2; k < mi && ok; k++) {
            const uint32_t op = I[k][0] & 0xffff;
            if (op == kSwitch) { ok = false; break; }
            if (op == kLabel) {
                if (I[k].size() != 2) { ok = false; break; }
                open_sel_merges.erase(I[k][1]);
            }
            if (op == kBranch) {
                if (I[k].size() != 2) { ok = false; break; }
                if (I[k][1] == M) { ok = false; break; }
                const bool is_skip_br = k > 0 && skip_blocks.count(k - 1);
                if (I[k][1] == cont && !is_skip_br && !open_sel_merges.empty()) { ok = false; break; }
            }
            if (op == kBranchCond) {
                if (I[k].size() < 4) { ok = false; break; }
                if (I[k][2] == M || I[k][3] == M) { ok = false; break; }
                const bool is_retargeted = cond_retarget.count(k) != 0;
                if ((I[k][2] == cont || I[k][3] == cont) && !is_retargeted && !open_sel_merges.empty()) { ok = false; break; }
            }
            if (op == kSelectionMerge) {
                if (I[k].size() < 3) { ok = false; break; }
                const uint32_t sm = I[k][1];
                const bool top_level = open_sel_merges.empty();
                if (k + 1 < mi && (I[k + 1][0] & 0xffff) == kBranchCond && I[k + 1].size() >= 4) {
                    const uint32_t t_lbl = I[k + 1][2], f_lbl = I[k + 1][3];
                    if (top_level && !cont_has_phi && !cont_targeted_by_sel && skip_blocks.empty() && t_lbl != f_lbl &&
                        (f_lbl == sm || t_lbl == sm)) {
                        const uint32_t cand_skip = (f_lbl == sm) ? t_lbl : f_lbl;
                        for (size_t j = k + 2; j + 1 < ci; j++) {
                            if ((I[j][0] & 0xffff) == kLabel && I[j].size() == 2 && I[j][1] == cand_skip &&
                                (I[j + 1][0] & 0xffff) == kBranch && I[j + 1].size() == 2 && I[j + 1][1] == cont) {
                                int refs = 0;
                                for (size_t u = hi; u < mi; u++) {
                                    if (u == j) continue;
                                    Ins tmp = I[u];
                                    for_each_id(tmp, [&](uint32_t& id, bool) { if (id == cand_skip) refs++; });
                                }
                                if (refs == 1) {
                                    skip_blocks.insert(j);
                                    skip_labels.insert(cand_skip);
                                    sel_retarget[k] = cont;
                                    cond_retarget[k + 1] = {cand_skip, cont};
                                    cont_targeted_by_sel = true;
                                }
                                break;
                            }
                        }
                    }
                }
                if (!sel_retarget.count(k)) open_sel_merges.insert(sm);
            }
        }
        // Order-independent check: no non-retargeted selection whose merge != cont may reach cont inside its construct
        if (ok) {
            std::unordered_map<uint32_t, std::vector<uint32_t>> succs;
            std::vector<std::tuple<uint32_t, uint32_t, uint32_t>> non_ret_sels;   // (t_lbl, f_lbl, sm)
            uint32_t cur_b = 0;
            for (size_t k = li + 2; k < mi; k++) {
                const uint32_t op = I[k][0] & 0xffff;
                if (op == kLabel) cur_b = I[k][1];
                else if (op == kBranch && cur_b) succs[cur_b].push_back(I[k][1]);
                else if (op == kBranchCond && cur_b) {
                    succs[cur_b].push_back(I[k][2]);
                    succs[cur_b].push_back(I[k][3]);
                } else if (op == kSelectionMerge && !sel_retarget.count(k) && I[k][1] != cont &&
                           k + 1 < mi && (I[k + 1][0] & 0xffff) == kBranchCond && I[k + 1].size() >= 4) {
                    non_ret_sels.emplace_back(I[k + 1][2], I[k + 1][3], I[k][1]);
                }
            }
            for (const auto& [tlb, flb, sm] : non_ret_sels) {
                std::unordered_set<uint32_t> vis;
                std::vector<uint32_t> work;
                if (tlb != sm) work.push_back(tlb);
                if (flb != sm) work.push_back(flb);
                while (!work.empty() && ok) {
                    const uint32_t u = work.back();
                    work.pop_back();
                    if (u == cont) { ok = false; break; }
                    if (u == sm || !vis.insert(u).second) continue;
                    if (auto sit = succs.find(u); sit != succs.end())
                        for (uint32_t nxt : sit->second) work.push_back(nxt);
                }
                if (!ok) break;
            }
        }
        if (!ok) { refused.insert(key); continue; }

        for (size_t k = 0; k < I.size() && ok; k++) {
            if (k >= hi && k < mi) continue;
            Ins tmp = I[k];
            const uint32_t op = tmp[0] & 0xffff;
            if (op == kName) continue;   // OpName on eliminated loop ids is stripped below
            bool named = false;
            if (!for_each_id(tmp, [&](uint32_t& id, bool) {
                    if (id != H && !phi_ids.count(id) && made.count(id)) named = true;
                })) {
                if (op != 15 && op != 16 && op != 5 && op != 6 && op != 71 && op != 72 && op != 7 && op != 3 && op != 8) ok = false;
            }
            if (named) ok = false;
        }
        if (!ok) { refused.insert(key); continue; }

        std::vector<size_t> body;
        for (size_t k = li + 2; k < mi; k++) {
            if (k == sl || k == ce) continue;
            if (skip_blocks.count(k) || (k > 0 && skip_blocks.count(k - 1))) continue;
            body.push_back(k);
        }
        for (size_t k : body) {
            Ins tmp = I[k];
            for_each_id(tmp, [&](uint32_t& id, bool) { ok &= id != H && id != test && id != iv.back; });
        }
        // Scan the entire merge block (skipping OpLine/OpNoLine) for OpPhi
        bool merge_has_phi = false;
        for (size_t k = mi + 1; k < I.size(); k++) {
            const uint32_t op = I[k][0] & 0xffff;
            if (op == kPhi) { merge_has_phi = true; break; }
            if (op == kLabel || op == kFunctionEnd || is_terminator(op)) break;
        }
        if (!ok || merge_has_phi) {
            refused.insert(key); continue;
        }

        const uint32_t saved_bound = m.header[3];
        std::vector<Ins> new_consts;
        std::vector<Ins> out;
        std::vector<uint32_t> entries((size_t)trips), conts((size_t)trips);
        std::vector<std::vector<Ins>> copies((size_t)trips);
        std::unordered_map<uint32_t, uint32_t> prev_ren;
        // (every result's type, for the access-chain bounds checks below)
        std::unordered_map<uint32_t, uint32_t> id_ty = ti.id_type;
        for (const Ins& in : I) {
            const char* kspec = spirv_operands::of(in[0] & 0xffff);
            if (kspec && kspec[0] == 'T' && kspec[1] == 'R' && in.size() >= 3) id_ty[in[2]] = in[1];
        }
        // A refusal after constants were made for this loop: their ids go back and ti forgets them, or a later loop asking
        // for the same constant would be handed an id that no instruction defines
        const auto refuse_made = [&] {
            for (const Ins& c : new_consts) {
                ti.const32.erase(c[2]);
                ti.id_type.erase(c[2]);
                ti.const32_by_key.erase(((uint64_t)c[1] << 32) | c[3]);
            }
            m.header[3] = saved_bound;
            refused.insert(key);
        };

        bool body_ac_ok = true;
        for (int64_t it = 0; it < trips && body_ac_ok; it++) {
            std::unordered_map<uint32_t, uint32_t> ren;
            for (uint32_t id : made_order) {
                if (id != H && id != test && id != iv.back && !phi_ids.count(id) && !skip_labels.count(id))
                    ren[id] = m.header[3]++;
            }
            for (const auto& p : phis) {
                if (p.id == iv.id) {
                    ren[p.id] = ti.get_or_make_const(m, new_consts, iv.ty, (uint32_t)(start + it * step));
                } else if (it == 0) {
                    ren[p.id] = p.init;
                } else {
                    auto pr = prev_ren.find(p.back);
                    ren[p.id] = pr != prev_ren.end() ? pr->second : p.back;
                }
            }
            entries[(size_t)it] = ren.count(B) ? ren[B] : B;
            conts[(size_t)it] = ren[cont];
            for (size_t k : body) {
                Ins c = I[k];
                if (sel_retarget.count(k)) c[1] = sel_retarget[k];
                if (cond_retarget.count(k)) {
                    const auto [old_l, new_l] = cond_retarget[k];
                    if (c[2] == old_l) c[2] = new_l;
                    if (c[3] == old_l) c[3] = new_l;
                }
                for_each_id(c, [&](uint32_t& id, bool) {
                    if (auto r = ren.find(id); r != ren.end()) id = r->second;
                });
                // (the counter, or a phi's start, is a constant in this copy: as an index it must be in bounds even in a
                // branch this iteration never takes, `if (i < 2) x = arr[i];` over a 4-trip loop, or the module is invalid)
                if ((c[0] & 0xffff) == kAccessChain)
                    for (size_t j = 4; j < c.size() && body_ac_ok; j++)
                        if (c[j] != I[k][j] && !detail::safe_access_chain_const(I[k], j, c[j], ti, id_ty)) body_ac_ok = false;
                copies[(size_t)it].push_back(std::move(c));
            }
            prev_ren = std::move(ren);
        }
        if (!body_ac_ok) { refuse_made(); continue; }

        std::unordered_map<uint32_t, uint32_t> exit_alias;
        for (const auto& p : phis) {
            if (p.id == iv.id) {
                exit_alias[p.id] = ti.get_or_make_const(m, new_consts, iv.ty, (uint32_t)(start + trips * step));
            } else {
                auto pr = prev_ren.find(p.back);
                exit_alias[p.id] = pr != prev_ren.end() ? pr->second : p.back;
            }
        }
        // Refuse if a live-out phi would substitute a negative or out-of-bounds constant into an outside OpAccessChain
        if (!exit_alias.empty()) {
            bool exit_ac_ok = true;
            for (size_t k = 0; k < I.size() && exit_ac_ok; k++) {
                if (k >= hi && k < mi) continue;
                const Ins& in = I[k];
                if ((in[0] & 0xffff) == kAccessChain && in.size() >= 5) {
                    for (size_t j = 4; j < in.size(); j++) {
                        auto ea = exit_alias.find(in[j]);
                        if (ea != exit_alias.end() && !detail::safe_access_chain_const(in, j, ea->second, ti, id_ty)) {
                            exit_ac_ok = false;
                            break;
                        }
                    }
                }
            }
            if (!exit_ac_ok) { refuse_made(); continue; }
        }

        out.push_back(I[hi]);
        out.push_back({(2u << 16) | kBranch, entries[0]});
        for (int64_t it = 0; it < trips; it++) {
            const uint32_t target = it + 1 < trips ? entries[(size_t)it + 1] : M;
            bool in_cont = false;
            for (Ins& c : copies[(size_t)it]) {
                const bool label = (c[0] & 0xffff) == kLabel;
                if (label && in_cont) out.push_back({(2u << 16) | kBranch, target}), in_cont = false;
                if (label && c[1] == conts[(size_t)it]) in_cont = true;
                out.push_back(std::move(c));
            }
            if (in_cont) out.push_back({(2u << 16) | kBranch, target});
        }

        std::unordered_set<uint32_t> removed_ids;
        for (uint32_t id : made_order)
            if (id != H) removed_ids.insert(id);

        std::vector<Ins> res;
        res.reserve(I.size() + out.size() + new_consts.size());
        for (size_t k = 0; k < hi; k++) {
            if (k == ti.first_func)
                for (Ins& c : new_consts) res.push_back(std::move(c));
            res.push_back(std::move(I[k]));
        }
        for (Ins& c : out) res.push_back(std::move(c));
        for (size_t k = mi; k < I.size(); k++) res.push_back(std::move(I[k]));
        I.swap(res);

        if (!exit_alias.empty()) detail::apply_aliases(m, exit_alias);
        detail::strip_annotations(m, removed_ids, false);
        return true;
    }
    return false;
}

/// Every loop that can be, innermost first, folded and cleaned after each unroll while the module stays under max_words:
/// false when none was. left: the loops still rolled
inline bool unroll_all(const uint32_t* code, size_t words, std::vector<uint32_t>& out, size_t max_words, uint32_t max_trips,
                       int* left = nullptr) {
    Module m;
    if (!parse(code, words, m)) return false;

    detail::hoist_const_var_stores(m);
    detail::fold_constants(m);
    detail::dce(m);

    std::unordered_set<uint64_t> refused;
    int count = 0;
    while (true) {
        Module keep = m;
        if (!unroll_one(m, refused, max_trips, max_words)) break;
        detail::merge_trivial_blocks(m);
        detail::fold_constants(m);
        detail::forward_function_allocas(m);
        detail::fold_constants(m);
        detail::dce(m);

        size_t size = 5;
        for (const Ins& in : m.ins) size += in.size();
        if (size > max_words) { m = std::move(keep); break; }
        count++;
    }
    if (count) {
        detail::merge_trivial_blocks(m);
        detail::fold_constants(m);
        detail::forward_function_allocas(m);
        detail::fold_constants(m);
        detail::dce(m);
    }
    if (left) {
        *left = 0;
        for (const Ins& in : m.ins) *left += (in[0] & 0xffff) == kLoopMerge;
    }
    if (!count) return false;
    out = emit(m);
    return true;
}

} // namespace spirv_unroll
