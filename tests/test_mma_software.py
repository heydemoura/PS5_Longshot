"""The software matrix multiplies' forms (mma_software.h) and the self-test that chooses between them (vk_coopmat.cpp).

On a GPU without cooperative matrices (the Steam Deck, AMD before RDNA 3, GTX 10 and 16, Intel's integrated GPUs), the game's
upscale networks run HMMA.16816.F16 as a shader function. Its loop form (Lichii's) is what shipped; the unrolled forms take A
through quad broadcasts and round with the GPU's own f16 conversion, and are used only where the device's self-test finds the
loop's words in theirs. Without a GPU this checks what can be checked:

- every form, with quad broadcasts and without, assembles to SPIR-V that spirv-val accepts, from the self-test's own shader
  (software_test_shader cut from vk_coopmat.cpp), and has the shape its form promises (one loop the driver is asked not to
  unroll: the 8 steps, or the written-out body run once; shuffles for A or quad broadcasts; the shuffle with lane xor 0 before
  each f16 conversion); B through workgroup memory as a plain array;
- the emitter's function (DefineSoftwareMatrixFunction cut from emit_spirv_warp.cpp) as programs get it, through spirv-val, and
  B through shuffles where a program cannot stage it (no full subgroups, a workgroup not whole subgroups, the tiles past the
  shared memory, an explicit workgroup layout with workgroup memory of its own, which the self-test does not run);
- the self-test's inputs catch a driver that folds the f16 conversion into the FMA before it: on the CPU, the loop's
  arithmetic (each FMA rounded to f32, the sum to f16 after every 8 columns of K) and the folded one (the last FMA of a group
  rounded to f16 once) give other words on the warps built for it, so such a driver's unrolled form is refused;
- the self-test's comparison (software_words_compare): 65504 against infinity, a NaN or an infinity against a number, the
  other sign are far, a rounding of 1 or 2 f16 ulps near;
- the Dot2 form (each K pair one f16 pair dot product into f32, OpFDot2MixAcc32VALVE) rounds a pair once where the loop rounds
  twice: on the CPU, with that arithmetic, the self-test's double-rounding warps give other words than the loop's, and the
  random fragments none the comparison calls far, which is what the self-test lets through. A spirv-val older than the opcode
  (SPIRV-Tools 2025.4) validates those modules with the dot products written as FMA pairs, and says SKIP for the dot product;
- B through workgroup memory (SoftwareMmaStagedB): no B shuffles left, two word stores, four 16-byte loads, two subgroup barriers, and
  the same arithmetic as the form through shuffles; and its indices (software_tile_*) on plain numbers: every lane of 4 subgroups
  reads the words the shuffles would hand it, none stored outside its subgroup's tile or over another;
- the switches (RX_VK_MMA_UNROLL, RADV and AMD RDNA 4 on AMD's proprietary driver by default; RX_VK_MMA_DOT2; RX_VK_MMA_STAGE_B,
  RADV by default, with full compute subgroups only), the emitter's use of the chosen form and of the tiles, and the program
  cache's stamp.
"""
import argparse
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))   # (python -I leaves the check's own folder off the path)
from srccut import block, between

RUN_ALL_ARGS = ["--cxx", "{cxx}", "--headers", "{spirv_headers}", "--spirv-val", "{spirv_val}"]
# (spirv-val: SPIRV-Tools comes with the Linux runner's packages, not on Windows)
RUN_ALL_SKIP = {"windows": "needs spirv-val: checked on Linux"}

ROOT = Path(__file__).resolve().parents[1]
VK = ROOT / "gpu/vk"
SPIRV_DIR = VK / "eden/shader_recompiler/backend/spirv"
DRIVER_IDS = {"VK_DRIVER_ID_AMD_PROPRIETARY": 1, "VK_DRIVER_ID_AMD_OPEN_SOURCE": 2, "VK_DRIVER_ID_MESA_RADV": 3,
              "VK_DRIVER_ID_NVIDIA_PROPRIETARY": 4, "VK_DRIVER_ID_INTEL_OPEN_SOURCE_MESA": 6}   # (as the Vulkan headers number them)


def compile_fixture(cxx, source, extra, folder, name):
    cpp = folder / f"{name}.cpp"
    cpp.write_text(source, encoding="utf-8")
    exe = folder / f"{name}.exe"
    subprocess.run([cxx, "-std=c++20", "-O1", str(cpp), *extra, "-o", str(exe)], check=True)
    return exe


def compile_and_run(cxx, source, extra, folder, name, *args):
    exe = compile_fixture(cxx, source, extra, folder, name)
    run = subprocess.run([str(exe), *map(str, args)], capture_output=True, text=True, timeout=120)
    if run.returncode:
        raise SystemExit(f"{name}: {run.stdout.strip()} {run.stderr.strip()}".strip())
    return run.stdout


def sirit_flags(headers):
    """what a fixture that builds SPIR-V with Sirit and mma_software.h compiles with"""
    sirit = VK / "sirit"
    return ["-I" + str(sirit / "include"), "-I" + str(sirit / "src"), "-I" + str(VK / "eden"), "-I" + str(headers),
            *map(str, sorted((sirit / "src").rglob("*.cpp")))]


def spirv(cxx, headers, spirv_val, folder):
    """The self-test's shader in every form, through spirv-val, and each form's shape"""
    coopmat = (VK / "vk_coopmat.cpp").read_text(encoding="utf-8")
    shaders = between(coopmat, "constexpr uint32_t kSoftwareTestLanes", "/// The fragments the forms are compared on")
    fixture = """#include <array>
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>
#include <sirit/sirit.h>
#include "shader_recompiler/backend/spirv/mma_software.h"
""" + shaders + """
int main(int argc, char** argv) {
    using Shader::Backend::SPIRV::SoftwareMmaForm;
    if (argc != 2) return 2;
    for (const SoftwareMmaForm form : {SoftwareMmaForm::Loop, SoftwareMmaForm::Unrolled, SoftwareMmaForm::UnrolledF16,
                                       SoftwareMmaForm::Dot2})
        for (const bool quad : {false, true})
            for (const bool staged : {false, true}) {   // (B through shuffles; through tiles)
                const auto code = software_test_shader(form, quad, staged);
                const std::string name = std::string(argv[1]) + "/form" + std::to_string((unsigned)form) + (quad ? "q" : "") +
                                         (staged ? "s" : "") + ".spv";
                FILE* f = std::fopen(name.c_str(), "wb");
                if (!f || std::fwrite(code.data(), 4, code.size(), f) != code.size()) return 3;
                std::fclose(f);
            }
    return 0;
}
"""
    compile_and_run(cxx, fixture, sirit_flags(headers), folder, "forms", folder)
    for form in (0, 1, 2, 3):
        for quad in ("", "q"):
            shuffled = check_form(spirv_val, folder / f"form{form}{quad}.spv", form, bool(quad), False)
            check_form(spirv_val, folder / f"form{form}{quad}s.spv", form, bool(quad), True, shuffled)


SKIPS = []   # (what an old spirv-val could not check: named at the end, never counted as passed)


def validate(spirv_val, path, where):
    """spirv-val on the module. One whose spirv-val is older than OpFDot2MixAcc32VALVE (SPIRV-Tools 2025.4; opcode 6916,
    capability 6912) stops at the opcode: the module is validated again with each dot product written as its two products and
    adds in f32 (dot2_as_fma), so all the rest is still checked, and the dot product itself is a SKIP, said aloud. A module that
    fails either way fails. Returns whether the dot products were checked"""
    val = subprocess.run([spirv_val, "--target-env", "vulkan1.3", str(path)], capture_output=True, text=True)
    if not val.returncode:
        return True
    error = val.stderr.strip() or val.stdout.strip()
    code = path.read_bytes()
    if 6916 not in ops_of(code) or not re.search(r"6916|6912|Invalid opcode|DotProductFloat16AccFloat32VALVE|mixed_float_dot", error):
        raise SystemExit(f"{where}: spirv-val: {error}")
    swapped = path.with_suffix(".fma.spv")
    swapped.write_bytes(dot2_as_fma(code))
    again = subprocess.run([spirv_val, "--target-env", "vulkan1.3", str(swapped)], capture_output=True, text=True)
    if again.returncode:
        raise SystemExit(f"{where}: spirv-val, the dot products written as FMA pairs: {again.stderr.strip() or again.stdout.strip()}")
    note = f"SKIP: {where}: this spirv-val ({spirv_val}) does not know OpFDot2MixAcc32VALVE ({error.splitlines()[0]}): the module " \
           "passed with the dot products as FMA pairs, the dot products themselves NOT validated (SPIRV-Tools 2025.4 or later checks them)"
    print(note, file=sys.stderr)
    print(note)
    SKIPS.append(where)
    return False


def dot2_as_fma(code):
    """The module with every OpFDot2MixAcc32VALVE (result f32 = acc + a.b, a and b two f16) written as f32 conversions of the
    halves, two OpFMul and two OpFAdd into the same result, and the extension and capability dropped: valid for an old spirv-val
    exactly where the dot product's module is but for that instruction"""
    words = struct.unpack(f"<{len(code) // 4}I", code)
    out, bound = list(words[:5]), words[3]
    # (the f16 and f32 type ids, from OpTypeFloat)
    types = {inst[2]: inst[1] for inst in instructions(code) if inst[0] & 0xffff == 22}
    f16, f32 = types[16], types[32]
    for inst in instructions(code):
        n, op = len(inst), inst[0] & 0xffff
        if op == 17 and inst[1] == 6912:   # (OpCapability DotProductFloat16AccFloat32VALVE)
            continue
        if op == 10 and struct.pack(f"<{n - 1}I", *inst[1:]).split(b"\0")[0] == b"SPV_VALVE_mixed_float_dot_product":
            continue
        if op != 6916:
            out += inst
            continue
        _, result_type, result, a, b, acc = inst
        if result_type != f32:
            raise SystemExit("an OpFDot2MixAcc32VALVE whose result is not f32")
        ids = list(range(bound, bound + 11))   # (4 halves, their 4 conversions, a product, a sum, the other product)
        bound += 11
        halves = []
        for k, (vector, half) in enumerate(((a, 0), (a, 1), (b, 0), (b, 1))):
            out += [5 << 16 | 81, f16, ids[k], vector, half]          # (OpCompositeExtract)
            out += [4 << 16 | 115, f32, ids[4 + k], ids[k]]           # (OpFConvert)
            halves.append(ids[4 + k])
        out += [5 << 16 | 133, f32, ids[8], halves[0], halves[2]]     # (OpFMul a0 b0)
        out += [5 << 16 | 129, f32, ids[9], ids[8], acc]              # (OpFAdd)
        out += [5 << 16 | 133, f32, ids[10], halves[1], halves[3]]    # (OpFMul a1 b1)
        out += [5 << 16 | 129, f32, result, ids[10], ids[9]]          # (OpFAdd, the dot product's own result)
    out[3] = bound
    return struct.pack(f"<{len(out)}I", *out)


EMITTER = r"""
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <stdexcept>
#include <vector>
#include <sirit/sirit.h>
#include "shader_recompiler/backend/spirv/mma_layout.h"
#include "shader_recompiler/backend/spirv/mma_software.h"

namespace Shader::Backend::SPIRV {
using Sirit::Id;
using u32 = std::uint32_t;
using s32 = std::int32_t;
enum class Stage { Compute, Fragment };
struct NotImplementedException : std::runtime_error { using std::runtime_error::runtime_error; };
namespace IR {
struct Program {   // (what DefineSoftwareMatrixFunction reads)
    std::array<u32, 3> workgroup_size{};
    u32 shared_memory_size{};
};
}
struct Profile {
    bool has_broken_spirv_subgroup_shuffle{}, warp_size_potentially_larger_than_guest{};
    u32 max_subgroup_size = 32;
    bool SupportsSubgroupStage(Stage) const { return true; }
};
struct EmitContext : Sirit::Module {
    using Sirit::Module::Module;
    std::array<Id, 5> U32{}, F32{}, F16{};
    Id U1{};
    Id subgroup_local_invocation_id{};
    Id software_matrix_function{};
    bool uses_explicit_workgroup_layout{};
    Stage stage = Stage::Compute;
    Profile profile;
    std::vector<Id> interfaces;
    Id SConst(s32 value) { return Constant(TypeInt(32, true), value); }
};
@@FUNCTION@@
}

using namespace Shader::Backend::SPIRV;

int main(int argc, char** argv) {
    if (argc != 9) return 2;
    const u32 form = std::atoi(argv[2]), width = std::atoi(argv[4]), own = std::atoi(argv[5]);
    const bool quad = argv[3][0] == '1', explicit_layout = argv[6][0] == '1';
    mma_software = true;
    mma_software_form = form;
    mma_software_quad = quad;
    mma_software_full_subgroups = argv[7][0] == '1';
    mma_software_staged_b = std::atoi(argv[8]);
    EmitContext ctx{0x00010600};
    ctx.AddCapability(spv::Capability::Shader);
    ctx.AddCapability(spv::Capability::Float16);
    if (form == static_cast<u32>(SoftwareMmaForm::Dot2)) {   // (as SetupCapabilities)
        ctx.AddExtension(kMixedFloatDotProductExtension);
        ctx.AddCapability(kDotProductFloat16AccFloat32VALVE);
    }
    ctx.SetMemoryModel(spv::AddressingModel::Logical, spv::MemoryModel::GLSL450);
    const Id void_t{ctx.TypeVoid()};
    ctx.U32[1] = ctx.TypeInt(32, false);
    ctx.F32[1] = ctx.TypeFloat(32);
    ctx.F16[1] = ctx.TypeFloat(16);
    for (u32 n = 2; n <= 4; ++n) {
        ctx.U32[n] = ctx.TypeVector(ctx.U32[1], n);
        ctx.F32[n] = ctx.TypeVector(ctx.F32[1], n);
        ctx.F16[n] = ctx.TypeVector(ctx.F16[1], n);
    }
    ctx.U1 = ctx.TypeBool();
    ctx.subgroup_local_invocation_id = ctx.AddGlobalVariable(ctx.TypePointer(spv::StorageClass::Input, ctx.U32[1]),
                                                             spv::StorageClass::Input);
    ctx.Decorate(ctx.subgroup_local_invocation_id, spv::Decoration::BuiltIn, spv::BuiltIn::SubgroupLocalInvocationId);
    ctx.interfaces.push_back(ctx.subgroup_local_invocation_id);
    const Id runtime{ctx.TypeRuntimeArray(ctx.U32[1])};
    ctx.Decorate(runtime, spv::Decoration::ArrayStride, 4U);
    const Id ssbo_block{ctx.TypeStruct(runtime)};
    ctx.Decorate(ssbo_block, spv::Decoration::Block);
    ctx.MemberDecorate(ssbo_block, 0U, spv::Decoration::Offset, 0U);
    const Id ssbo{ctx.AddGlobalVariable(ctx.TypePointer(spv::StorageClass::StorageBuffer, ssbo_block), spv::StorageClass::StorageBuffer)};
    ctx.Decorate(ssbo, spv::Decoration::DescriptorSet, 0U);
    ctx.Decorate(ssbo, spv::Decoration::Binding, 0U);
    ctx.interfaces.push_back(ssbo);
    IR::Program program;
    program.workgroup_size = {width, 1, 1};
    program.shared_memory_size = own;
    Id own_shared{};
    const Id own_word{ctx.TypePointer(spv::StorageClass::Workgroup, ctx.U32[1])};
    ctx.uses_explicit_workgroup_layout = explicit_layout;
    if (own) {   // (as EmitContext::DefineSharedMemory, with an explicit layout and without)
        const Id array{ctx.TypeArray(ctx.U32[1], ctx.Constant(ctx.U32[1], (own + 3) / 4))};
        Id type{array};
        if (explicit_layout) {
            ctx.AddExtension("SPV_KHR_workgroup_memory_explicit_layout");
            ctx.AddCapability(spv::Capability::WorkgroupMemoryExplicitLayoutKHR);
            ctx.Decorate(array, spv::Decoration::ArrayStride, 4U);
            type = ctx.TypeStruct(array);
            ctx.MemberDecorate(type, 0U, spv::Decoration::Offset, 0U);
            ctx.Decorate(type, spv::Decoration::Block);
        }
        own_shared = ctx.AddGlobalVariable(ctx.TypePointer(spv::StorageClass::Workgroup, type), spv::StorageClass::Workgroup);
        if (explicit_layout) ctx.Decorate(own_shared, spv::Decoration::Aliased);
        ctx.interfaces.push_back(own_shared);
    }
    ctx.AddCapability(spv::Capability::GroupNonUniform);   // (as DefineInputs for a program with a warp lane)
    DefineSoftwareMatrixFunction(ctx, program);
    const Id main_fn{ctx.OpFunction(void_t, spv::FunctionControlMask::MaskNone, ctx.TypeFunction(void_t))};
    ctx.AddLabel();
    const Id one{ctx.Constant(ctx.U32[1], 1u)}, zero{ctx.Constant(ctx.U32[1], 0u)};
    const Id d{ctx.OpFunctionCall(ctx.U32[2], ctx.software_matrix_function, ctx.OpCompositeConstruct(ctx.U32[4], one, one, one, one),
                                  ctx.OpCompositeConstruct(ctx.U32[2], one, one), ctx.OpCompositeConstruct(ctx.U32[2], one, one))};
    const Id lane{ctx.OpLoad(ctx.U32[1], ctx.subgroup_local_invocation_id)};
    const Id word_ptr{ctx.TypePointer(spv::StorageClass::StorageBuffer, ctx.U32[1])};
    ctx.OpStore(ctx.OpAccessChain(word_ptr, ssbo, zero, lane), ctx.OpCompositeExtract(ctx.U32[1], d, 0U));
    if (own) {   // (the program's own words are used too)
        const Id at{explicit_layout ? ctx.OpAccessChain(own_word, own_shared, zero, zero) : ctx.OpAccessChain(own_word, own_shared, zero)};
        ctx.OpStore(at, lane);
    }
    ctx.OpReturn();
    ctx.OpFunctionEnd();
    ctx.AddEntryPoint(spv::ExecutionModel::GLCompute, main_fn, "main", ctx.interfaces);
    ctx.AddExecutionMode(main_fn, spv::ExecutionMode::LocalSize, width, 1U, 1U);
    const auto code{ctx.Assemble()};
    std::ofstream(argv[1], std::ios::binary).write(reinterpret_cast<const char*>(code.data()),
                                                   static_cast<std::streamsize>(code.size() * 4));
    return 0;
}
"""

# name, form, quad, workgroup X, own shared bytes, explicit layout, full subgroups, the shared memory limit, wanted: array (the
# tiles as a plain array), shuffles (B not staged)
EMITTER_CASES = (
    ("unrolled, quad, 128 lanes", 1, 1, 128, 0, 0, 1, 65536, "array"),
    ("f16 conversion, quad, 128 lanes", 2, 1, 128, 0, 0, 1, 65536, "array"),
    ("Dot2, quad, 128 lanes", 3, 1, 128, 0, 0, 1, 65536, "array"),
    ("Dot2, shuffles for A, 64 lanes", 3, 0, 64, 0, 0, 1, 65536, "array"),
    ("Dot2, own shared memory without an explicit layout", 3, 1, 128, 100, 0, 1, 65536, "array"),
    ("f16 conversion, own shared memory, explicit layout", 2, 1, 128, 100, 1, 1, 65536, "shuffles"),
    ("Dot2, own shared memory, explicit layout", 3, 1, 128, 100, 1, 1, 65536, "shuffles"),
    ("Dot2, explicit layout, no own shared memory", 3, 1, 128, 0, 1, 1, 65536, "array"),
    ("Dot2, no full subgroups", 3, 1, 128, 0, 0, 0, 65536, "shuffles"),
    ("Dot2, 48 lanes wide", 3, 1, 48, 0, 0, 1, 65536, "shuffles"),
    ("Dot2, tiles fill the shared memory", 3, 1, 128, 0, 0, 1, 4 * 256, "array"),
    ("Dot2, own and tiles past the shared memory", 3, 1, 128, 16, 0, 1, 4 * 256, "shuffles"),
    ("the loop", 0, 0, 128, 0, 0, 1, 65536, "shuffles"),
)


def emitter(cxx, headers, spirv_val, folder):
    """DefineSoftwareMatrixFunction (emit_spirv_warp.cpp) as a program gets it, through spirv-val: the tiles as a plain array,
    and B through shuffles where a program cannot stage it (an explicit workgroup layout with workgroup memory of its own among
    them, where every workgroup variable is a Block)"""
    warp = (SPIRV_DIR / "emit_spirv_warp.cpp").read_text(encoding="utf-8")
    fixture = EMITTER.replace("@@FUNCTION@@", block(warp, "void DefineSoftwareMatrixFunction("))
    exe = compile_fixture(cxx, fixture, sirit_flags(headers), folder, "emitter")
    for n, (name, form, quad, width, own, explicit, full, limit, want) in enumerate(EMITTER_CASES):
        spv = folder / f"emitter{n}.spv"
        run = subprocess.run([str(exe), str(spv), str(form), str(quad), str(width), str(own), str(explicit), str(full),
                              str(limit)], capture_output=True, text=True, timeout=60)
        if run.returncode:
            raise SystemExit(f"emitter: {name}: the fixture stopped: {run.stdout.strip()} {run.stderr.strip()}")
        code = spv.read_bytes()
        ops = ops_of(code)
        barriers, shuffles, blocks = ops.count(224), ops.count(345), decorations(code).count(2)
        got = "shuffles" if not barriers else "array"
        # (Block structs: the storage buffer's, and the program's own workgroup memory's with an explicit layout; never the tiles)
        if blocks != 1 + (own and explicit) or barriers not in (0, 2) or got != want or (got != "shuffles" and shuffles != (0 if quad else 16)):
            raise SystemExit(f"emitter: {name}: {barriers} barriers, {shuffles} shuffles, {blocks} Block structs: {got}, not {want}")
        checked = validate(spirv_val, spv, f"emitter: {name}")
        how = "through shuffles" if want == "shuffles" else "as tiles in an array"
        print(f"PASS: emitter: {name}: B {how}, spirv-val"
              f"{'' if checked else ' (with FMA pairs, see SKIP)'}")


TILE_MAPPING = r"""
#include <cstdio>
#include <sirit/sirit.h>
#include "shader_recompiler/backend/spirv/mma_software.h"
using namespace Shader::Backend::SPIRV;
struct NumOps {   // (software_tile_*'s arithmetic on plain numbers)
    uint32_t imm(uint32_t v) { return v; }
    uint32_t add(uint32_t a, uint32_t b) { return a + b; }
    uint32_t and_(uint32_t a, uint32_t b) { return a & b; }
    uint32_t shl(uint32_t a, uint32_t b) { return a << b; }
    uint32_t shr(uint32_t a, uint32_t b) { return a >> b; }
};
int main() {
    constexpr uint32_t subgroups = 4, unset = ~0u;
    uint32_t tiles[subgroups * kSoftwareMmaTileVectors][4];
    for (auto& v : tiles) for (auto& c : v) c = unset;
    NumOps o;
    // (every lane of every subgroup stores its two B words, named (subgroup * 32 + lane) * 2 + w, where the emitter stores them)
    for (uint32_t sg = 0; sg < subgroups; ++sg)
        for (uint32_t lane = 0; lane < 32; ++lane) {
            const uint32_t tile = software_tile_base(o, sg), own = software_tile_own(o, tile, lane), half = software_tile_half(o, lane);
            for (uint32_t w = 0; w < 2; ++w) {
                const uint32_t c = o.add(half, o.imm(w));
                if (own < sg * kSoftwareMmaTileVectors || own >= (sg + 1) * kSoftwareMmaTileVectors || c > 3) {
                    std::printf("FAIL: subgroup %u lane %u word %u stored at u32x4 %u component %u, outside its tile\n", sg, lane, w, own, c);
                    return 1;
                }
                if (tiles[own][c] != unset) {
                    std::printf("FAIL: subgroup %u lane %u word %u stored over another word (u32x4 %u component %u)\n", sg, lane, w, own, c);
                    return 1;
                }
                tiles[own][c] = (sg * 32 + lane) * 2 + w;
            }
        }
    // (and every lane reads, for each K pair and column, the word the shuffles hand it: half upper of B from lane
    // 8 (lane % 4) + component + 4 col, as DefineSoftwareMma's shuffle path takes it)
    uint32_t checked = 0;
    for (uint32_t sg = 0; sg < subgroups; ++sg)
        for (uint32_t lane = 0; lane < 32; ++lane) {
            const uint32_t tile = software_tile_base(o, sg), columns = software_tile_columns(o, tile, lane);
            for (uint32_t upper = 0; upper < 2; ++upper)
                for (uint32_t component = 0; component < 4; ++component)
                    for (uint32_t col = 0; col < 2; ++col) {
                        const uint32_t from = component + col * 4, run = software_tile_run(from);
                        const uint32_t at = o.add(columns, o.imm(run)), c = software_tile_component(from, upper);
                        const uint32_t want = (sg * 32 + ((lane & 3) << 3) + from) * 2 + upper;
                        if (run > 3 || at < sg * kSoftwareMmaTileVectors || at >= (sg + 1) * kSoftwareMmaTileVectors || c > 3 ||
                            tiles[at][c] != want) {
                            std::printf("FAIL: subgroup %u lane %u: half %u of B from lane %u reads u32x4 %u component %u (word %u), not "
                                        "word %u\n", sg, lane, upper, ((lane & 3) << 3) + from, at, c, c > 3 ? unset : tiles[at][c], want);
                            return 1;
                        }
                        ++checked;
                    }
        }
    std::printf("%u\n", checked);
    return 0;
}
"""


def tile_mapping(cxx, headers, folder):
    """B through workgroup memory hands each lane the words the shuffles would (mma_software.h software_tile_*, which
    DefineSoftwareMma emits): with those indices on plain numbers, 4 subgroups of 32 lanes store their B words, none outside its
    subgroup's tile or over another, and every lane's 16 reads (2 halves x 4 K pairs x 2 columns) are the shuffles' words"""
    header = (SPIRV_DIR / "mma_software.h").read_text(encoding="utf-8")
    load = header[header.index("const auto load_b_from_tile = [&]"):header.index("return runs;")]
    for need in ("software_tile_base(o, ", "software_tile_own(o, tile, lane)", "software_tile_half(o, lane)",
                 "software_tile_columns(o, tile, lane)"):
        if need not in load:
            raise SystemExit(f"load_b_from_tile does not take its indices from {need.split('(')[0]}: the check below would not see them")
    if "b_runs[software_tile_run(from)], software_tile_component(from, upper)" not in header:
        raise SystemExit("DefineSoftwareMma does not take B's word from the runs by software_tile_run and software_tile_component")
    out = compile_and_run(cxx, TILE_MAPPING, sirit_flags(headers), folder, "tile_mapping")
    print(f"PASS: B through workgroup memory: every lane of 4 subgroups reads the shuffles' words ({out.split()[-1]} reads), "
          "none stored outside its tile or over another")


def check_form(spirv_val, path, form, quad, staged, shuffled=(0, 0)):
    """One form's shape: its loop, its shuffles for A and B, its rounding, its dot products and its tiles. shuffled: the same
    form's (loads, stores) through shuffles, which the staged module's are counted against; returns its own"""
    where = f"form {form}{' with quad broadcasts' if quad else ''}{', B through workgroup memory' if staged else ''}"
    dots_checked = validate(spirv_val, path, where)
    code = path.read_bytes()
    ops = ops_of(code)
    loops = ops.count(246)          # (OpLoopMerge)
    shuffles = ops.count(345)       # (OpGroupNonUniformShuffle)
    quads = ops.count(365)          # (OpGroupNonUniformQuadBroadcast)
    xors = ops.count(346)           # (OpGroupNonUniformShuffleXor)
    converts = ops.count(115)       # (OpFConvert)
    fmas = ext_insts(code).count(50)   # (GLSL.std.450 Fma)
    dots = ops.count(6916)          # (OpFDot2MixAcc32VALVE)
    barriers = ops.count(224)       # (OpControlBarrier)
    loads = ops.count(61)           # (OpLoad)
    stores = ops.count(62)          # (OpStore)
    blocks = decorations(code).count(2)   # (Decoration Block: the storage buffer's only)
    staged = staged and form != 0   # (the loop keeps its shuffles)
    if loops != 1:
        raise SystemExit(f"{where}: {loops} loops, not one (the steps' loop, or the written-out body's loop that runs once)")
    if form == 0 and (shuffles != 4 or quads or xors or converts or barriers):
        raise SystemExit(f"{where}: not the 8 steps' loop on 4 shuffles with integer rounding ({shuffles} shuffles, {quads} "
                         f"quad, {xors} xor, {converts} conversions, {barriers} barriers)")
    b_shuffles = 0 if staged else 16
    if form and (quads, shuffles) != ((16, b_shuffles) if quad else (0, 16 + b_shuffles)):
        raise SystemExit(f"{where}: {quads} quad broadcasts and {shuffles} shuffles, not "
                         f"{(16, b_shuffles) if quad else (0, 16 + b_shuffles)} (B's {b_shuffles}, and A's 16 one way or the other)")
    if form == 1 and (xors or converts):
        raise SystemExit(f"{where}: the integer rounding has {xors} xor shuffles and {converts} conversions")
    if form in (2, 3) and (xors != 8 or converts != 16):
        raise SystemExit(f"{where}: {xors} xor shuffles and {converts} conversions, not 8 and 16 (each rounding: the "
                         "same lane, then f32 -> f16 -> f32)")
    if form == 3 and (dots != 32 or fmas):
        raise SystemExit(f"{where}: {dots} pair dot products and {fmas} FMAs, not 32 and none (one a K pair of each of the 4 sums)")
    if form in (1, 2) and fmas != 64:
        raise SystemExit(f"{where}: {fmas} FMAs, not 64 (each K of each of the 4 sums)")
    if form != 3 and dots:
        raise SystemExit(f"{where}: {dots} pair dot products in a form without them")
    want_barriers = 2 if staged else 0   # (the subgroup's two)
    if barriers != want_barriers:
        raise SystemExit(f"{where}: {barriers} barriers, not {want_barriers}")
    # (the staged shader's loads past the one through shuffles': the lane and the subgroup id, then the tile's 4; its stores: the
    # lane's two B words)
    if staged and loads - shuffled[0] != 6:
        raise SystemExit(f"{where}: {loads - shuffled[0] - 2} loads of the tile, not 4 (lanes 8i..8i+7's words, 16 bytes each)")
    if staged and stores - shuffled[1] != 2:
        raise SystemExit(f"{where}: {stores - shuffled[1]} stores to the tile, not 2 (the lane's two B words)")
    if blocks != 1:
        raise SystemExit(f"{where}: {blocks} Block structs, not 1")
    print(f"PASS: {where}: spirv-val{'' if dots_checked else ' (too old for OpFDot2MixAcc32VALVE: with FMA pairs, see SKIP)'}, {loops} loop, "
          f"{shuffles} shuffles, {quads} quad broadcasts, {xors} xor shuffles, {converts} conversions, {dots} dot products, "
          f"{barriers} barriers")
    return loads, stores


def instructions(code):
    """each instruction's words, in order, past the module's header"""
    words = struct.unpack(f"<{len(code) // 4}I", code)
    at = 5
    while at < len(words):
        n = max(words[at] >> 16, 1)
        yield words[at:at + n]
        at += n


def decorations(code):
    """each OpDecorate's decoration, in order"""
    return [inst[2] for inst in instructions(code) if inst[0] & 0xffff == 71]


def ext_insts(code):
    """each OpExtInst's instruction number, in order"""
    return [inst[4] for inst in instructions(code) if inst[0] & 0xffff == 12]


def ops_of(code):
    """each instruction's opcode, in order"""
    return [inst[0] & 0xffff for inst in instructions(code)]


MODEL = r"""
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>
// (the loop's arithmetic and the folded one, lane by lane: PTX mma.m16n8k16's fragments, lane t, g = t/4, i = t%4)
float h2f(uint32_t h) {
    const uint32_t sign = (h & 0x8000) << 16, exp = (h >> 10) & 0x1f, man = h & 0x3ff;
    if (!exp) { const float f = std::ldexp((float)man, -24); return sign ? -f : f; }
    uint32_t x = exp == 31 ? sign | 0x7f800000 | man << 13 : sign | (exp - 15 + 127) << 23 | man << 13;
    float f; std::memcpy(&f, &x, 4); return f;
}
uint32_t f2h(double v) {   // (round to nearest even, f16 subnormals kept, overflow to infinity)
    if (std::isnan(v)) return 0x7e00;
    const uint32_t sign = std::signbit(v) ? 0x8000 : 0;
    double a = std::fabs(v);
    if (a >= 65520.0) return sign | 0x7c00;
    if (a < 0x1p-14) return sign | (uint32_t)std::nearbyint(a * 0x1p24);
    int e; std::frexp(a, &e);   // (a = m * 2^e, m in [0.5, 1))
    const double q = std::nearbyint(std::ldexp(a, 11 - e));   // (11 significant bits)
    if (q >= 2048.0) { e++; }
    const uint32_t bits = (uint32_t)(e - 1 + 15) << 10 | ((uint32_t)(q >= 2048.0 ? q / 2 : q) & 0x3ff);
    return sign | bits;
}
// one lane's D: folded = the last FMA of each group of 8 K rounded to f16 once (a driver folding the conversion into it)
void lane_d(const uint32_t* warp, int t, bool folded, uint32_t out[2]) {
    const int i = t % 4;
    float acc[4];
    for (int row = 0; row < 2; row++)
        for (int col = 0; col < 2; col++) acc[row * 2 + col] = h2f(warp[t * 8 + 6 + row] >> 16 * col & 0xffff);
    for (int upper = 0; upper < 2; upper++) {
        double last[4];
        for (int component = 0; component < 4; component++)
            for (int row = 0; row < 2; row++)
                for (int col = 0; col < 2; col++) {
                    const uint32_t a = warp[((t & ~3) + component) * 8 + upper * 2 + row];
                    const uint32_t b = warp[(i * 8 + component + col * 4) * 8 + 4 + upper];
                    for (int half = 0; half < 2; half++) {
                        const float x = h2f(a >> 16 * half & 0xffff), y = h2f(b >> 16 * half & 0xffff);
                        float& s = acc[row * 2 + col];
                        if (component == 3 && half == 1) last[row * 2 + col] = (double)x * y + s;   // (exact for these inputs)
                        s = std::fma(x, y, s);
                    }
                }
        for (int k = 0; k < 4; k++) acc[k] = h2f(f2h(folded ? last[k] : (double)acc[k]));
    }
    for (int row = 0; row < 2; row++) out[row] = f2h(acc[row * 2]) | f2h(acc[row * 2 + 1]) << 16;
}
// the Dot2 form's: each K pair's two products and the sum rounded to f32 once (v_dot2_f32_f16), the sum to f16 after every 8 K
void lane_dot2(const uint32_t* warp, int t, uint32_t out[2]) {
    const int i = t % 4;
    float acc[4];
    for (int row = 0; row < 2; row++)
        for (int col = 0; col < 2; col++) acc[row * 2 + col] = h2f(warp[t * 8 + 6 + row] >> 16 * col & 0xffff);
    for (int upper = 0; upper < 2; upper++) {
        for (int component = 0; component < 4; component++)
            for (int row = 0; row < 2; row++)
                for (int col = 0; col < 2; col++) {
                    const uint32_t a = warp[((t & ~3) + component) * 8 + upper * 2 + row];
                    const uint32_t b = warp[(i * 8 + component + col * 4) * 8 + 4 + upper];
                    // (long double: f16 products are exact in it, and the three-term sum rounded once but for far-apart terms)
                    long double sum = acc[row * 2 + col];
                    for (int half = 0; half < 2; half++)
                        sum += (long double)h2f(a >> 16 * half & 0xffff) * (long double)h2f(b >> 16 * half & 0xffff);
                    acc[row * 2 + col] = (float)sum;
                }
        for (int k = 0; k < 4; k++) acc[k] = h2f(f2h((double)acc[k]));
    }
    for (int row = 0; row < 2; row++) out[row] = f2h(acc[row * 2]) | f2h(acc[row * 2 + 1]) << 16;
}
"""


def inputs(cxx, folder):
    """The self-test's inputs tell the loop's words from a folding driver's"""
    coopmat = (VK / "vk_coopmat.cpp").read_text(encoding="utf-8")
    lanes = re.search(r"constexpr uint32_t kSoftwareTestLanes = [^;]+;", coopmat).group(0)
    make = block(coopmat, "std::vector<uint32_t> software_test_input()")
    fixture = MODEL + lanes + "\n" + make + r"""
int main() {
    const std::vector<uint32_t> data = software_test_input();
    // (the warps built for it: every eighth, kinds 3*683 + 2^-16, 3*683 - 2^-16, 48*1365 + 2^-16, 48*1365 - 2^-16)
    const uint32_t want_loop[4] = {0x6800, 0x6800, 0x7c00, 0x7c00}, want_folded[4] = {0x6801, 0x6800, 0x7c00, 0x7bff};
    int folded_differ = 0;
    for (uint32_t warp = 0; warp < kSoftwareTestLanes / 32; warp += 8) {
        const uint32_t kind = warp / 8 % 4;
        for (int t = 0; t < 32; t++) {
            uint32_t loop[2], folded[2];
            lane_d(&data[warp * 32 * 8], t, false, loop);
            lane_d(&data[warp * 32 * 8], t, true, folded);
            // (every lane's D: rows g, g + 8 of columns 2i, 2i + 1, each the same sum)
            for (int w = 0; w < 2; w++) {
                const uint32_t lw = want_loop[kind] | want_loop[kind] << 16, fw = want_folded[kind] | want_folded[kind] << 16;
                if (loop[w] != lw || folded[w] != fw) {
                    std::printf("warp %u lane %d word %d: loop %08x (want %08x), folded %08x (want %08x)\n", warp, t, w, loop[w], lw,
                                folded[w], fw);
                    return 1;
                }
                folded_differ += loop[w] != folded[w];
            }
        }
    }
    std::printf("%d\n", folded_differ);
    return folded_differ ? 0 : 1;
}
"""
    out = compile_and_run(cxx, fixture, [], folder, "inputs")
    count = int(out.split()[-1])
    if count < 512:
        raise SystemExit(f"only {count} of the built warps' words tell a folding driver from the loop")
    print(f"PASS: the self-test's inputs: {count} words of the built warps differ where the conversion is folded "
          "(2049 + 2^-16: 2048 looped, 2050 folded; 65520 - 2^-16: infinity looped, 65504 folded)")


def compare_source():
    """vk_coopmat.cpp's word comparison (SoftwareWords, software_words_compare), cut out, with kSoftwareTestLanes"""
    coopmat = (VK / "vk_coopmat.cpp").read_text(encoding="utf-8")
    lanes = re.search(r"constexpr uint32_t kSoftwareTestLanes = [^;]+;", coopmat).group(0)
    words = between(coopmat, "constexpr size_t kSoftwareFormDidNotRun", "/// The form's words for the test's fragments")
    return lanes + "\n" + words + "\n" + block(coopmat, "SoftwareWords software_words_compare(") + "\n"


def compare(cxx, folder):
    """software_words_compare: what is a rounding (near) and what a mistake (far). 65504 (0x7bff) and infinity (0x7c00) are one
    apart in bits and must be far, as must a NaN against a number or an infinity, and the other sign"""
    # (got, want, differ, far): one half of one word set, the rest the same
    cases = [(0x3c00, 0x3c00, 0, 0), (0x3c00, 0x3c01, 1, 0), (0x3c00, 0x3c02, 1, 0), (0x3c00, 0x3c03, 1, 1),
             (0x7bff, 0x7c00, 1, 1), (0x7c00, 0x7bff, 1, 1), (0x7bfe, 0x7c00, 1, 1), (0xfbff, 0xfc00, 1, 1),
             (0x7c00, 0x7c00, 0, 0), (0xfc00, 0xfc00, 0, 0), (0x7c00, 0xfc00, 1, 1), (0x7e00, 0x7c01, 0, 0),
             (0x7e00, 0x7c00, 1, 1), (0x7e00, 0x7bff, 1, 1), (0x0000, 0x8000, 1, 0), (0x0001, 0x8001, 1, 1),
             (0x0001, 0x0000, 1, 0), (0x3c00, 0xbc00, 1, 1)]
    checks = "\n".join(
        f"    if (!one(0x{g:04x}u, 0x{w:04x}u, {d}, {f})) {{ std::printf(\"FAIL: %04x against %04x\\n\", 0x{g:04x}u, 0x{w:04x}u); return 1; }}"
        for g, w, d, f in cases)
    fixture = "#include <cstddef>\n#include <cstdint>\n#include <cstdio>\n#include <vector>\n" + compare_source() + r"""
bool one(uint32_t got_half, uint32_t want_half, size_t differ, size_t off) {
    for (const int high : {0, 1}) {   // (either half of a word, in the middle of the results)
        std::vector<uint32_t> got(10 * kSoftwareTestLanes, 0x3c003c00u), want = got;
        const uint32_t at = 9 * kSoftwareTestLanes + 7, keep = high ? 0x3c00u : 0x3c00u << 16;
        got[at] = high ? got_half << 16 | keep : got_half | keep;
        want[at] = high ? want_half << 16 | keep : want_half | keep;
        const SoftwareWords r = software_words_compare(got, want);
        if (r.differ != differ || r.off != off) {
            std::printf("half %d: differ %zu far %zu, not %zu %zu\n", high, r.differ, r.off, differ, off);
            return false;
        }
    }
    return true;
}
int main() {
""" + checks + "\n    return 0;\n}\n"
    compile_and_run(cxx, fixture, [], folder, "compare")
    print(f"PASS: the self-test's comparison on {len(cases)} pairs of halves: within 2 f16 ulps near, 65504 against infinity "
          "far (one bit apart), a NaN or an infinity near only its own kind, the other sign far")


def dot2_inputs(cxx, folder):
    """The Dot2 form against the loop on the self-test's inputs: other words on the double-rounding warps, none far off (by
    vk_coopmat.cpp's own comparison)"""
    coopmat = (VK / "vk_coopmat.cpp").read_text(encoding="utf-8")
    make = block(coopmat, "std::vector<uint32_t> software_test_input()")
    fixture = MODEL + compare_source() + make + r"""
int main() {
    const std::vector<uint32_t> data = software_test_input();
    // (the warps built for it: every eighth from the fourth, kinds 2049 + 2^-13 + 2^-20 (two orders) and 2051 - 2^-13 - 2^-20)
    const uint32_t want_loop[3] = {0x6800, 0x6800, 0x6802}, want_dot2[3] = {0x6801, 0x6801, 0x6801};
    std::vector<uint32_t> loops(data.size() + 2 * kSoftwareTestLanes), dot2s(loops.size());
    int built = 0;
    for (uint32_t warp = 0; warp < kSoftwareTestLanes / 32; warp++)
        for (int t = 0; t < 32; t++) {
            uint32_t loop[2], dot2[2];
            lane_d(&data[warp * 32 * 8], t, false, loop);
            lane_dot2(&data[warp * 32 * 8], t, dot2);
            for (int w = 0; w < 2; w++) {
                if (warp % 8 == 4) {
                    const uint32_t kind = warp / 8 % 3, lw = want_loop[kind] * 0x10001u, dw = want_dot2[kind] * 0x10001u;
                    if (loop[w] != lw || dot2[w] != dw) {
                        std::printf("warp %u lane %d word %d: loop %08x (want %08x), dot2 %08x (want %08x)\n", warp, t, w, loop[w],
                                    lw, dot2[w], dw);
                        return 1;
                    }
                    built++;
                }
                loops[8 * kSoftwareTestLanes + 2 * (warp * 32 + t) + w] = loop[w];
                dot2s[8 * kSoftwareTestLanes + 2 * (warp * 32 + t) + w] = dot2[w];
            }
        }
    const SoftwareWords r = software_words_compare(dot2s, loops);
    std::printf("%d %zu %zu %u\n", built, r.differ, r.off, 2 * kSoftwareTestLanes);
    return 0;
}
"""
    built, differ, far, words = map(int, compile_and_run(cxx, fixture, [], folder, "dot2_inputs").split()[-4:])
    if far:
        raise SystemExit(f"the Dot2 form's arithmetic gives {far} words far from the loop's (more than 2 f16 ulps, another sign, or "
                         "an infinity or NaN against a number) on the self-test's inputs: the self-test would refuse a correct Dot2 form")
    if differ < built:
        raise SystemExit(f"only {differ} words differ between the loop and the Dot2 form, fewer than the {built} words built for it")
    print(f"PASS: the Dot2 form's arithmetic on the self-test's inputs: {differ} of {words} words other than the loop's "
          f"(the {built} of the double-rounding warps among them), none far off (vk_coopmat.cpp software_words_compare)")


def switch(cxx, folder):
    """software_unroll_wanted: RADV and AMD RDNA 4 on AMD's proprietary driver by default, RX_VK_MMA_UNROLL over it"""
    arch_h = (VK / "gpu_arch.h").read_text(encoding="utf-8")
    rdna4_fn = block(arch_h, "inline bool is_rdna4(")
    auto_profile = block((VK / "vk_main.cpp").read_text(encoding="utf-8"), "void auto_profile(")
    auto_profile = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", auto_profile, flags=re.S))   # (code only)
    if not re.search(r"^\s*hw\.rdna4 = is_rdna4\(p\.vendorID, p\.deviceID, p\.deviceName\);", auto_profile, re.M):
        raise SystemExit("auto_profile does not set hw.rdna4 from is_rdna4(p.vendorID, p.deviceID, p.deviceName)")
    coopmat = (VK / "vk_coopmat.cpp").read_text(encoding="utf-8")
    fn = block(coopmat, "bool software_unroll_wanted(")
    choose = block(coopmat, "void choose_software_form(")
    if ('software_unroll_wanted(dev.driver_id, is_rdna4(dev.props.vendorID, dev.props.deviceID, dev.props.deviceName), env)' not in choose
            or 'std::getenv("RX_VK_MMA_UNROLL")' not in choose):
        raise SystemExit("choose_software_form does not ask software_unroll_wanted with is_rdna4 and RX_VK_MMA_UNROLL")
    order = [choose.index(x) for x in ("software_form_words(SoftwareMmaForm::Loop", "software_words_compare(words, loop)",
                                         "if (r.ran() && (r.differ == 0 || within_rounding))",
                                         "mma_software_form = (uint32_t)form", "choose_staged_b(form, quad, words)")]
    if order != sorted(order) or choose.count("mma_software_form = (uint32_t)form") != 1:
        raise SystemExit("choose_software_form does not run the loop, compare a form and only then use it")
    if "const bool within_rounding = form == SoftwareMmaForm::Dot2 && r.off == 0;" not in choose:
        raise SystemExit("choose_software_form lets the Dot2 form through other than where every word is within 2 f16 ulps")
    staged = block(coopmat, "void choose_staged_b(")
    order = [staged.index(x) for x in ("software_stage_b_wanted(", "if (!dev.vk13.computeFullSubgroups)",
                                         "software_form_words(form, quad, true", "if (r.differ)",
                                         "mma_software_staged_b = dev.props.limits.maxComputeSharedMemorySize",
                                         "mma_software_full_subgroups = true")]
    if order != sorted(order):
        raise SystemExit("choose_staged_b does not ask for full subgroups and compare the tiles' words with the shuffles' before "
                         "using them")
    if "software_multiplies(\"subgroup shuffles, RX_VK_MMA_SOFTWARE\")" not in coopmat:
        raise SystemExit("RX_VK_MMA_SOFTWARE=1 does not go through software_multiplies (and its form)")
    radv, amd, amd_open, nvidia, intel = (
        "VK_DRIVER_ID_MESA_RADV", "VK_DRIVER_ID_AMD_PROPRIETARY", "VK_DRIVER_ID_AMD_OPEN_SOURCE",
        "VK_DRIVER_ID_NVIDIA_PROPRIETARY", "VK_DRIVER_ID_INTEL_OPEN_SOURCE_MESA")
    cases = [
        (radv, False, None, True), (radv, True, None, True), (radv, False, "0", False), (radv, False, "1", True),
        (amd, False, None, False), (amd, False, "1", True),
        (amd, True, None, True), (amd, True, "0", False), (amd, True, "1", True),
        (amd_open, True, None, False), (amd_open, True, "1", True),
        (nvidia, False, None, False), (intel, False, None, False),
        (intel, False, "true", True), (nvidia, False, "off", False),
    ]
    rdna4_checks = "\n".join([
        '    if (!is_rdna4(0x1002, 0x7551, "AMD Radeon AI PRO R9700")) { std::puts("FAIL: is_rdna4 R9700"); return 1; }',
        '    if (!is_rdna4(0x1002, 0x7550, "AMD Device 7550")) { std::puts("FAIL: is_rdna4 0x7550"); return 1; }',
        '    if (!is_rdna4(0x1002, 0x7590, "AMD Device 7590")) { std::puts("FAIL: is_rdna4 0x7590"); return 1; }',
        '    if (!is_rdna4(0x1002, 0x0000, "AMD Radeon RX 9060 XT")) { std::puts("FAIL: is_rdna4 RX 9060 XT by name"); return 1; }',
        '    if (!is_rdna4(0x1002, 0x7551, nullptr)) { std::puts("FAIL: is_rdna4 0x7551 nullptr"); return 1; }',
        '    if (!is_rdna4(0x1002, 0x0000, "AMD Radeon RX 9080")) { std::puts("FAIL: is_rdna4 RX 9080"); return 1; }',
        '    if (!is_rdna4(0x1002, 0x0000, "AMD Radeon GFX1201")) { std::puts("FAIL: is_rdna4 GFX1201"); return 1; }',
        '    if (is_rdna4(0x1002, 0x73DF, "AMD Radeon RX 6700 XT")) { std::puts("FAIL: is_rdna4 RX 6700 XT"); return 1; }',
        '    if (is_rdna4(0x1002, 0x744C, "AMD Radeon RX 7900 XTX")) { std::puts("FAIL: is_rdna4 RX 7900 XTX"); return 1; }',
        '    if (is_rdna4(0x1002, 0x0000, nullptr)) { std::puts("FAIL: is_rdna4 nullptr"); return 1; }',
        '    if (is_rdna4(0x10DE, 0x7551, "NVIDIA GeForce RTX 4090")) { std::puts("FAIL: is_rdna4 NVIDIA id"); return 1; }',
        '    if (is_rdna4(0x10DE, 0x0000, "NVIDIA RX 9000")) { std::puts("FAIL: is_rdna4 NVIDIA name"); return 1; }',
    ])
    dot2_fn, stage_fn = block(coopmat, "bool software_dot2_wanted("), block(coopmat, "bool software_stage_b_wanted(")
    dot2_cases = [(True, None, True), (True, "1", True), (True, "0", False), (False, None, False), (False, "1", False)]
    stage_cases = [(radv, True, None, True), (radv, False, None, False), (radv, True, "0", False), (radv, False, "1", False),
                   (amd, True, None, False), (amd, True, "1", True), (intel, True, None, False), (nvidia, True, "true", True)]
    q = lambda e: "nullptr" if e is None else chr(34) + e + chr(34)
    more = "\n".join(
        [f'    if (software_dot2_wanted({str(m).lower()}, {q(e)}) != {str(w).lower()}) '
         f'{{ std::puts("FAIL: dot2 {m} {e}"); return 1; }}' for m, e, w in dot2_cases] +
        [f'    if (software_stage_b_wanted({d}, {str(pin).lower()}, {q(e)}) != {str(w).lower()}) '
         f'{{ std::puts("FAIL: stage_b {d} {pin} {e}"); return 1; }}' for d, pin, e, w in stage_cases])
    checks = more + "\n" + "\n".join(
        f'    if (software_unroll_wanted({d}, {str(r).lower()}, {"nullptr" if e is None else chr(34) + e + chr(34)}) != {str(w).lower()}) '
        f'{{ std::puts("FAIL: {d} rdna4={r} {e}"); return 1; }}' for d, r, e, w in cases)
    env_h = (VK / "compat/longshot_env.h").read_text(encoding="utf-8")
    enum = "enum VkDriverId { " + ", ".join(f"{k} = {n}" for k, n in DRIVER_IDS.items()) + " };\n"
    source = (env_h + "\n#include <cstdint>\n#include <cstdio>\n#include <cstdlib>\n#include <cstring>\n" + enum +
              rdna4_fn + "\n" + fn + "\n" + dot2_fn + "\n" + stage_fn + "\nint main() {\n" + rdna4_checks + "\n" + checks + "\n    return 0;\n}\n")
    compile_and_run(cxx, source, [], folder, "switch")
    print("PASS: RX_VK_MMA_UNROLL: unrolled on RADV and AMD RDNA 4 on AMD's proprietary driver by default, looped elsewhere, the switch over both")
    print("PASS: RX_VK_MMA_DOT2: tried where the GPU has the f16 pair dot product unless 0; RX_VK_MMA_STAGE_B: RADV by default where compute "
          "is pinned to 32 lanes, the switch over it, never unpinned")


def wiring():
    warp = (SPIRV_DIR / "emit_spirv_warp.cpp").read_text(encoding="utf-8")
    define = block(warp, "void DefineSoftwareMatrixFunction(")
    if "DefineSoftwareMma(ctx, " not in define or "mma_software_form" not in define:
        raise SystemExit("the emitter does not define the software function in the chosen form")
    if "GroupNonUniformQuad" not in define:
        raise SystemExit("the emitter does not declare GroupNonUniformQuad for the quad broadcasts")
    stage = define[define.index("const bool full_subgroups"):define.index("if (stage_b)")]
    for need in ("mma_software_staged_b", "ctx.stage == Stage::Compute", "ctx.profile.max_subgroup_size == 32",
                 "!ctx.profile.warp_size_potentially_larger_than_guest", "invocations && wg[0] % 32 == 0",
                 "own + tiles * kSoftwareMmaTileBytes <= mma_software_staged_b", "mma_software_full_subgroups",
                 "!explicit_layout", "full_subgroups &&"):
        if need not in stage:
            raise SystemExit(f"the emitter stages B without checking {need}")
    if define.count("ctx.interfaces.push_back(") != 2 or "BuiltIn::SubgroupId" not in define:
        raise SystemExit("the tiles and the SubgroupId input are not in the entry point's interface")
    caps = block((SPIRV_DIR / "emit_spirv.cpp").read_text(encoding="utf-8"), "void SetupCapabilities(")
    if "kDotProductFloat16AccFloat32VALVE" not in caps or "kMixedFloatDotProductExtension" not in caps:
        raise SystemExit("a Dot2 program does not declare the mixed float dot product's extension and capability")
    shader = (VK / "vk_shader.cpp").read_text(encoding="utf-8")
    stamp = block(shader, "uint64_t translator_stamp()")
    if ("mma_software_form << 8" not in stamp or "mma_software_quad" not in stamp or "mma_software_staged_b" not in stamp or
            "mma_software_full_subgroups" not in stamp):
        raise SystemExit("the program cache's stamp does not hold the software multiplies' form and where B goes")
    print("PASS: the emitter takes the chosen form, and the program cache's stamp holds it")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cxx", default="c++")
    parser.add_argument("--headers", type=Path, required=True)
    parser.add_argument("--spirv-val", default="spirv-val")
    args = parser.parse_args()
    wiring()
    with tempfile.TemporaryDirectory(prefix="mma-software-") as folder:
        folder = Path(folder)
        switch(args.cxx, folder)
        inputs(args.cxx, folder)
        compare(args.cxx, folder)
        dot2_inputs(args.cxx, folder)
        spirv(args.cxx, args.headers, args.spirv_val, folder)
        emitter(args.cxx, args.headers, args.spirv_val, folder)
        tile_mapping(args.cxx, args.headers, folder)
    if SKIPS:
        print(f"SKIP: {len(SKIPS)} modules' OpFDot2MixAcc32VALVE not validated (spirv-val too old), the rest of them was", file=sys.stderr)


if __name__ == "__main__":
    main()
