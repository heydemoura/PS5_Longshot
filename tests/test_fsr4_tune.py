"""AMD FSR 4's shader rewrites (gpu/vk/fsr4_tune.h, gpu/vk/spirv_unroll.h) on shaders built as FSR 4's are (DXC, the
modules in tests/fsr4_tune with their HLSL): a network layer in miniature, a postpass in miniature (with OpINotEqual,
multi-OpPhi live-out accumulators, and an out-of-bounds early continue), and a table the shader writes into, plus
synthetic SPIR-V edge cases for mixed vector constructors, decorations, OpName forwarding, type ordering, qualified
stores, nested continues, OpLine before merge-block OpPhi, and malformed operands. Built as C++ and run, each result
checked with spirv-val and an exact SPIR-V interpreter:
- the layer's and postpass's tables move to the tail of the writable buffer, after what the tail held, each read where
  it was put, the Function arrays gone; the table that is written stays
- every loop of the layer and postpass unrolls deterministically, folds away Function arrays and unused weight tables,
  and produces bit-exact identical output buffer words across invocations (including both continue outcomes)
- a size limit just above the layer's own keeps loops rolled within it
- the network's layers alone (..._passN) get 64 lanes; the prepass, the postpass and a pass's _post do not
- a binding takes the tail in only at offset 0 with the whole buffer (or a range past the tail), and one of exactly the
  library's size is widened to it
And in gpu/vk/vk_fsr4.cpp's hooks (read as text, complementing the compiled behavioral tests in
tests/test_fsr4_bind_dispatch.py): the tail is kept per buffer and forgotten when the library destroys it, its buffer is
made a transfer destination, its bindings are checked where descriptors are written, a pipeline whose tail cannot be
read gets AMD's own in its place, and the background builds are counted and stopped when the window closes, before the
pipeline cache's save (gpu/vk/vk_main.cpp).
No game or GPU required.

python -I -B tests/test_fsr4_tune.py [--cxx g++] [--spirv-val spirv-val]
"""
import argparse
import os
import struct
import subprocess
import tempfile
from pathlib import Path

RUN_ALL_ARGS = ["--cxx", "{cxx}", "--spirv-val", "{spirv_val}"]

ROOT = Path(__file__).resolve().parents[1]
VK = ROOT / "gpu" / "vk"
FIXTURES = ROOT / "tests" / "fsr4_tune"

W = [0x0A0B0C0D, 3, 0xFFFFFFF0, 77, 5, 0x12345678, 9, 1]   # (layer.hlsl's two tables)
V = [11, 22, 33, 44]
POST_W = [3, 5, 7, 11, 13, 17, 19, 23]                    # (postpass.hlsl's table)
BEFORE = [111, 222]   # (what the tail held before: the tables go after it)
BASE = 1000           # (the word where the tail starts in the buffer)

DRIVER = r"""
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif
#include "fsr4_tune.h"
#include "spirv_unroll.h"

static void set_binary_stdio() {
#ifdef _WIN32
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
#endif
}
static std::vector<uint32_t> read_stdin_spv() {
    std::vector<uint8_t> bytes;
    uint8_t buf[16384];
    size_t n;
    while ((n = std::fread(buf, 1, sizeof buf, stdin)) > 0) bytes.insert(bytes.end(), buf, buf + n);
    std::vector<uint32_t> w(bytes.size() / 4);
    if (!w.empty()) std::memcpy(w.data(), bytes.data(), w.size() * 4);
    return w;
}
static void write_u32(uint32_t v) { std::fwrite(&v, 4, 1, stdout); }
static void write_blob(const void* data, size_t bytes) {
    write_u32((uint32_t)bytes);
    if (bytes) std::fwrite(data, 1, bytes, stdout);
}
static int loops(const std::vector<uint32_t>& w) {
    int n = 0;
    for (size_t i = 5; i < w.size();) {
        const uint32_t len = w[i] >> 16;
        if (!len || i + len > w.size()) break;
        n += (w[i] & 0xffff) == 246;   // OpLoopMerge
        i += len;
    }
    return n;
}
// stdin_tune <base word> <tail words...>: reads SPIR-V from stdin, writes [text][tail_spv][unroll_spv] blobs to stdout
// stdin_unroll <max words>: reads SPIR-V from stdin, writes [ok][left][det][out_spv] to stdout
// names <entry>...: which get 64 lanes, which are the postpass
// covers <size> <tail> <offset> <range>...: covers_tail and widened_range for each offset/range pair
int main(int argc, char** argv) {
    if (argc >= 4 && std::string(argv[1]) == "covers") {
        const uint64_t size = strtoull(argv[2], nullptr, 10), tail = strtoull(argv[3], nullptr, 10);
        for (int i = 4; i + 1 < argc; i += 2) {
            const uint64_t o = strtoull(argv[i], nullptr, 10), r = strtoull(argv[i + 1], nullptr, 10);
            std::printf("%llu %llu %d %llu\n", (unsigned long long)o, (unsigned long long)r, fsr4_tune::covers_tail(o, r, size, tail),
                        (unsigned long long)fsr4_tune::widened_range(o, r, size, tail));
        }
        return 0;
    }
    if (argc >= 2 && std::string(argv[1]) == "names") {
        for (int i = 2; i < argc; i++) std::printf("%s %d %d\n", argv[i], fsr4_tune::wants_wave64(argv[i]), fsr4_tune::is_postpass(argv[i]));
        return 0;
    }
    if (argc >= 3 && std::string(argv[1]) == "stdin_unroll") {
        set_binary_stdio();
        const std::vector<uint32_t> code = read_stdin_spv();
        const size_t max_w = (size_t)strtoull(argv[2], nullptr, 10);
        std::vector<uint32_t> un, un2;
        int left = -1, left2 = -1;
        const bool ok = spirv_unroll::unroll_all(code.data(), code.size(), un, max_w, 64, &left);
        const bool ok2 = spirv_unroll::unroll_all(code.data(), code.size(), un2, max_w, 64, &left2);
        const uint32_t det = (ok == ok2 && left == left2 && un == un2) ? 1u : 0u;
        write_u32(ok ? 1u : 0u);
        write_u32((uint32_t)left);
        write_u32(det);
        write_blob(ok ? un.data() : nullptr, ok ? un.size() * 4 : 0);
        return 0;
    }
    if (argc < 3 || std::string(argv[1]) != "stdin_tune") return 2;
    set_binary_stdio();
    const std::vector<uint32_t> code = read_stdin_spv();
    std::string text;
    char line[512];
    std::snprintf(line, sizeof line, "entry %s lane_dependent %d\n", fsr4_tune::entry_name(code.data(), code.size()).c_str(),
                  fsr4_tune::lane_dependent(code.data(), code.size()));
    text += line;
    std::vector<uint32_t> out, tail;
    for (int i = 3; i < argc; i++) tail.push_back((uint32_t)strtoul(argv[i], nullptr, 10));
    const uint32_t base = (uint32_t)strtoul(argv[2], nullptr, 10) + (uint32_t)tail.size();
    const bool moved = fsr4_tune::arrays_to_buffer(code.data(), code.size(), base, out, tail);
    text += "moved " + std::to_string(moved ? 1 : 0);
    for (uint32_t t : tail) text += " " + std::to_string(t);
    text += "\n";
    std::vector<uint32_t> un, un2;
    int left = -1, left2 = -1;
    const bool unrolled = spirv_unroll::unroll_all(code.data(), code.size(), un, code.size() * 20, 64, &left);
    const bool unrolled2 = spirv_unroll::unroll_all(code.data(), code.size(), un2, code.size() * 20, 64, &left2);
    const int det = (unrolled == unrolled2 && left == left2 && un == un2) ? 1 : 0;
    std::snprintf(line, sizeof line, "unrolled %d %d %d %zu %d\n", unrolled ? 1 : 0, loops(code), left, un.size(), det);
    text += line;
    std::vector<uint32_t> small;
    int left_small = -1;
    const size_t limit = code.size() + 64;
    const bool some = spirv_unroll::unroll_all(code.data(), code.size(), small, limit, 64, &left_small);
    std::snprintf(line, sizeof line, "capped %d %d %zu %zu\n", some ? 1 : 0, left_small, some ? small.size() : code.size(), limit);
    text += line;
    write_blob(text.data(), text.size());
    write_blob(moved ? out.data() : nullptr, moved ? out.size() * 4 : 0);
    write_blob(unrolled ? un.data() : nullptr, unrolled ? un.size() * 4 : 0);
    return 0;
}
"""


def words_from_bytes(data):
    return list(struct.unpack("<%dI" % (len(data) // 4), data))


def bytes_from_words(w):
    return struct.pack("<%dI" % len(w), *w)


def words(path):
    return words_from_bytes(Path(path).read_bytes())


def instructions(w):
    i = 5
    while i < len(w):
        n = w[i] >> 16
        yield w[i] & 0xFFFF, w[i:i + n]
        i += n


def names_of(w):
    out = {}
    for op, ins in instructions(w):
        if op == 5:   # OpName
            out[ins[1]] = struct.pack("<%dI" % (len(ins) - 2), *ins[2:]).split(b"\0")[0].decode()
    return out


def uint_constants(w):
    """id -> value of every 32-bit unsigned OpConstant"""
    t_uint = {ins[1] for op, ins in instructions(w) if op == 21 and ins[2:4] == [32, 0]}
    return {ins[2]: ins[3] for op, ins in instructions(w) if op == 43 and len(ins) == 4 and ins[1] in t_uint}


def chains_into(w, var):
    return sum(1 for op, ins in instructions(w) if op == 65 and ins[3] == var)


def function_vars(w):
    return sum(1 for op, ins in instructions(w) if op == 59 and ins[3] == 7)


def constant_composites(w):
    return sum(1 for op, _ in instructions(w) if op == 44)


def facts(out):
    return {line.split()[0]: line.split()[1:] for line in out.splitlines()}


def run_spv_vm(w, buffers, uniform_words=None, gid=(0, 0, 0)):
    """Compact SPIR-V compute-shader interpreter for game-free semantic equivalence checks.
    Executes one invocation with the given buffers (dict name -> list of u32, mutated in place), uniform words, and
    gl_GlobalInvocationID. Covers the integer, bit, composite, memory, phi, and control-flow instructions emitted by
    the DXC fixtures and synthetic regression modules."""
    uniform_words = uniform_words or []
    names = names_of(w)
    types = {}
    consts = {}
    global_vars = {}   # id -> (storage_class, pointee_ty)
    builtins = {}
    bindings = {}
    blocks = {}
    entry_label = None
    in_fn = False
    cur_label = None

    for op, ins in instructions(w):
        if op == 71:  # OpDecorate
            if len(ins) >= 4 and ins[2] == 11:   # BuiltIn
                builtins[ins[1]] = ins[3]
            elif len(ins) >= 4 and ins[2] == 33:  # Binding
                bindings[ins[1]] = ins[3]
        elif op == 20:
            types[ins[1]] = ("bool",)
        elif op == 21:
            types[ins[1]] = ("int", ins[2], ins[3])
        elif op == 22:
            types[ins[1]] = ("float", ins[2])
        elif op == 23:
            types[ins[1]] = ("vec", ins[2], ins[3])
        elif op == 28:
            types[ins[1]] = ("arr", ins[2], consts[ins[3]])
        elif op == 29:
            types[ins[1]] = ("rta", ins[2])
        elif op == 30:
            types[ins[1]] = ("struct", tuple(ins[2:]))
        elif op == 32:
            types[ins[1]] = ("ptr", ins[2], ins[3])
        elif op == 41:
            consts[ins[2]] = True
        elif op == 42:
            consts[ins[2]] = False
        elif op == 43:
            consts[ins[2]] = ins[3] & 0xFFFFFFFF
        elif op == 44:
            consts[ins[2]] = [consts[x] for x in ins[3:]]
        elif op == 54:
            in_fn = True
        elif op == 56:
            in_fn = False
        elif op == 59 and not in_fn:
            sc = ins[3]
            global_vars[ins[2]] = sc
        elif op == 248 and in_fn:
            cur_label = ins[1]
            if entry_label is None:
                entry_label = cur_label
            blocks[cur_label] = []
        elif in_fn and cur_label is not None:
            blocks[cur_label].append((op, ins))

    # Map StorageBuffer variables to buffer names (by OpName, falling back to binding index for synthetic shaders)
    var_buf = {}
    for vid, sc in global_vars.items():
        if sc == 12:   # StorageBuffer
            name = names.get(vid)
            if name in buffers:
                var_buf[vid] = name
            elif bindings.get(vid) in buffers:
                var_buf[vid] = bindings[vid]
            elif "buf" in buffers:
                var_buf[vid] = "buf"

    regs = dict(consts)
    fn_mem = {}

    def s32(v):
        v &= 0xFFFFFFFF
        return v - 0x100000000 if v & 0x80000000 else v

    def clone_val(v):
        return [clone_val(x) for x in v] if isinstance(v, list) else v

    def default_val(ty_id):
        t = types[ty_id]
        if t[0] in ("int", "float"):
            return 0
        if t[0] == "bool":
            return False
        if t[0] in ("vec", "arr"):
            return [default_val(t[1]) for _ in range(t[2])]
        if t[0] == "struct":
            return [default_val(m) for m in t[1]]
        return 0

    def read_ptr(p):
        kind = p[0]
        if kind == "input":
            return list(gid)
        if kind == "buf":
            buf = buffers[p[1]]
            idx = p[2]
            return buf[idx] if 0 <= idx < len(buf) else 0
        if kind == "uni":
            idx = p[1]
            return uniform_words[idx] if 0 <= idx < len(uniform_words) else 0
        if kind == "fn":
            cur = fn_mem[p[1]]
            for idx in p[2]:
                cur = cur[idx]
            return clone_val(cur)
        raise AssertionError("unsupported ptr read: %r" % (p,))

    def write_ptr(p, val):
        kind = p[0]
        if kind == "buf":
            buf = buffers[p[1]]
            idx = p[2]
            if idx >= len(buf):
                buf.extend([0] * (idx - len(buf) + 1))
            buf[idx] = val & 0xFFFFFFFF
        elif kind == "fn":
            if not p[2]:
                fn_mem[p[1]] = clone_val(val)
            else:
                cur = fn_mem[p[1]]
                for idx in p[2][:-1]:
                    cur = cur[idx]
                cur[p[2][-1]] = clone_val(val)
        else:
            raise AssertionError("unsupported ptr write: %r" % (p,))

    for vid, sc in global_vars.items():
        if sc == 1 and builtins.get(vid) == 28:   # Input GlobalInvocationId
            regs[vid] = ("input",)
        elif sc == 2:   # Uniform
            regs[vid] = ("uni_base",)
        elif sc == 12 and vid in var_buf:
            regs[vid] = ("buf_base", var_buf[vid])

    label = entry_label
    prev_label = None
    steps = 0
    while True:
        insns = blocks[label]
        # Evaluate all leading OpPhis atomically from the predecessor block
        phi_updates = {}
        i = 0
        while i < len(insns):
            op, ins = insns[i]
            if op in (8, 317):   # OpLine / OpNoLine
                i += 1
                continue
            if op != 245:      # OpPhi
                break
            matched = False
            for k in range(3, len(ins), 2):
                if ins[k + 1] == prev_label:
                    phi_updates[ins[2]] = clone_val(regs[ins[k]])
                    matched = True
                    break
            assert matched, "OpPhi %d had no edge from block %s" % (ins[2], prev_label)
            i += 1
        regs.update(phi_updates)

        next_label = None
        for op, ins in insns:
            steps += 1
            assert steps < 200000, "SPIR-V VM exceeded step limit (possible infinite loop)"
            if op in (8, 317, 245, 246, 247):   # OpLine, OpNoLine, OpPhi, OpLoopMerge, OpSelectionMerge
                continue
            if op == 59:   # Function OpVariable
                pt = types[ins[1]]
                fn_mem[ins[2]] = default_val(pt[2])
                regs[ins[2]] = ("fn", ins[2], ())
            elif op == 65:   # OpAccessChain
                base = regs[ins[3]]
                idxs = [regs[x] for x in ins[4:]]
                if base[0] == "buf_base":
                    # struct member 0 -> runtime array index
                    regs[ins[2]] = ("buf", base[1], idxs[-1])
                elif base[0] == "uni_base":
                    regs[ins[2]] = ("uni", idxs[-1])
                elif base[0] == "fn":
                    regs[ins[2]] = ("fn", base[1], base[2] + tuple(idxs))
                else:
                    raise AssertionError("unsupported AccessChain base: %r" % (base,))
            elif op == 61:   # OpLoad
                regs[ins[2]] = read_ptr(regs[ins[3]])
            elif op == 62:   # OpStore
                write_ptr(regs[ins[1]], regs[ins[2]])
            elif op == 81:   # OpCompositeExtract
                v = regs[ins[3]]
                for idx in ins[4:]:
                    v = v[idx]
                regs[ins[2]] = clone_val(v)
            elif op == 80:   # OpCompositeConstruct
                ty = types[ins[1]]
                if ty[0] == "vec":
                    flat = []
                    for opnd in ins[3:]:
                        v = regs[opnd]
                        if isinstance(v, list):
                            flat.extend(v)
                        else:
                            flat.append(v)
                    regs[ins[2]] = flat
                else:
                    regs[ins[2]] = [clone_val(regs[opnd]) for opnd in ins[3:]]
            elif op == 124:  # OpBitcast
                regs[ins[2]] = clone_val(regs[ins[3]])
            elif op == 128:  # OpIAdd
                regs[ins[2]] = (regs[ins[3]] + regs[ins[4]]) & 0xFFFFFFFF
            elif op == 130:  # OpISub
                regs[ins[2]] = (regs[ins[3]] - regs[ins[4]]) & 0xFFFFFFFF
            elif op == 132:  # OpIMul
                regs[ins[2]] = (regs[ins[3]] * regs[ins[4]]) & 0xFFFFFFFF
            elif op == 134:  # OpUDiv
                regs[ins[2]] = (regs[ins[3]] // regs[ins[4]]) & 0xFFFFFFFF
            elif op == 137:  # OpUMod
                regs[ins[2]] = (regs[ins[3]] % regs[ins[4]]) & 0xFFFFFFFF
            elif op == 194:  # OpShiftRightLogical
                regs[ins[2]] = (regs[ins[3]] & 0xFFFFFFFF) >> (regs[ins[4]] & 31)
            elif op == 195:  # OpShiftRightArithmetic
                regs[ins[2]] = (s32(regs[ins[3]]) >> (regs[ins[4]] & 31)) & 0xFFFFFFFF
            elif op == 196:  # OpShiftLeftLogical
                regs[ins[2]] = ((regs[ins[3]] & 0xFFFFFFFF) << (regs[ins[4]] & 31)) & 0xFFFFFFFF
            elif op == 197:  # OpBitwiseOr
                regs[ins[2]] = (regs[ins[3]] | regs[ins[4]]) & 0xFFFFFFFF
            elif op == 198:  # OpBitwiseXor
                regs[ins[2]] = (regs[ins[3]] ^ regs[ins[4]]) & 0xFFFFFFFF
            elif op == 199:  # OpBitwiseAnd
                regs[ins[2]] = (regs[ins[3]] & regs[ins[4]]) & 0xFFFFFFFF
            elif op == 170:  # OpIEqual
                regs[ins[2]] = (regs[ins[3]] & 0xFFFFFFFF) == (regs[ins[4]] & 0xFFFFFFFF)
            elif op == 171:  # OpINotEqual
                regs[ins[2]] = (regs[ins[3]] & 0xFFFFFFFF) != (regs[ins[4]] & 0xFFFFFFFF)
            elif op == 172:  # OpUGreaterThan
                regs[ins[2]] = (regs[ins[3]] & 0xFFFFFFFF) > (regs[ins[4]] & 0xFFFFFFFF)
            elif op == 174:  # OpUGreaterThanEqual
                regs[ins[2]] = (regs[ins[3]] & 0xFFFFFFFF) >= (regs[ins[4]] & 0xFFFFFFFF)
            elif op == 176:  # OpULessThan
                regs[ins[2]] = (regs[ins[3]] & 0xFFFFFFFF) < (regs[ins[4]] & 0xFFFFFFFF)
            elif op == 177:  # OpSLessThan
                regs[ins[2]] = s32(regs[ins[3]]) < s32(regs[ins[4]])
            elif op == 249:  # OpBranch
                next_label = ins[1]
                break
            elif op == 250:  # OpBranchConditional
                next_label = ins[2] if regs[ins[1]] else ins[3]
                break
            elif op == 253:  # OpReturn
                return
            else:
                raise AssertionError("unsupported SPIR-V opcode %d in VM" % op)
        prev_label = label
        label = next_label


def spv_inst(op, *operands):
    return [((len(operands) + 1) << 16) | op, *operands]


def spv_str(text):
    b = text.encode("utf-8") + b"\0"
    while len(b) % 4:
        b += b"\0"
    return list(struct.unpack("<%dI" % (len(b) // 4), b))


def spv_module(bound, body_words):
    return bytes_from_words([0x07230203, 0x00010300, 0, bound, 0] + body_words)


def build_synthetic_regressions():
    """Minimal valid Vulkan 1.3 SPIR-V modules exercising review findings L1-L5, L8-L10."""
    cases = {}

    def preamble(fn_id, buf_id, rta_id, sb_struct_id, extra_debug=None, extra_decor=None):
        w = []
        w += spv_inst(17, 1)                                     # OpCapability Shader
        w += spv_inst(14, 0, 1)                                  # OpMemoryModel Logical GLSL450
        w += spv_inst(15, 5, fn_id, *spv_str("main"))            # OpEntryPoint GLCompute
        w += spv_inst(16, fn_id, 17, 1, 1, 1)                    # OpExecutionMode LocalSize 1 1 1
        if extra_debug:
            w += extra_debug
        w += spv_inst(71, rta_id, 6, 4)                          # ArrayStride 4
        w += spv_inst(72, sb_struct_id, 0, 35, 0)                # Member Offset 0
        w += spv_inst(71, sb_struct_id, 2)                       # Block
        w += spv_inst(71, buf_id, 34, 0)                         # DescriptorSet 0
        w += spv_inst(71, buf_id, 33, 0)                         # Binding 0
        if extra_decor:
            w += extra_decor
        return w

    def base_types():
        # Fixed IDs 1..16:
        # 1:void, 2:fn_ty, 3:bool, 4:uint, 5:rta, 6:sb_struct, 7:ptr_sb_struct, 8:ptr_sb_uint,
        # 9:ptr_fn_uint, 10:buf, 11:u0, 12:u1, 13:u2, 14:u3, 15:u4, 16:fn
        w = []
        w += spv_inst(19, 1)
        w += spv_inst(33, 2, 1)
        w += spv_inst(20, 3)
        w += spv_inst(21, 4, 32, 0)
        w += spv_inst(29, 5, 4)
        w += spv_inst(30, 6, 5)
        w += spv_inst(32, 7, 12, 6)
        w += spv_inst(32, 8, 12, 4)
        w += spv_inst(32, 9, 7, 4)
        w += spv_inst(43, 4, 11, 0)
        w += spv_inst(43, 4, 12, 1)
        w += spv_inst(43, 4, 13, 2)
        w += spv_inst(43, 4, 14, 3)
        w += spv_inst(43, 4, 15, 4)
        w += spv_inst(59, 7, 10, 12)
        return w

    # L1: mixed vector+scalar OpCompositeConstruct (v4uint from v2uint + uint + uint) inside a 2-iter loop
    w = preamble(16, 10, 5, 6) + base_types()
    w += spv_inst(23, 20, 4, 2)            # %20 = v2uint
    w += spv_inst(23, 21, 4, 4)            # %21 = v4uint
    w += spv_inst(32, 22, 7, 21)           # %22 = ptr Function v4uint
    w += spv_inst(43, 4, 23, 20)           # %23 = 20u
    w += spv_inst(43, 4, 24, 30)           # %24 = 30u
    w += spv_inst(43, 4, 25, 40)           # %25 = 40u
    w += spv_inst(54, 1, 16, 0, 2)         # OpFunction
    w += spv_inst(248, 30)                 # entry
    w += spv_inst(59, 22, 31, 7)           # %31 = Function v4uint
    w += spv_inst(65, 8, 32, 10, 11, 11)   # &buf[0]
    w += spv_inst(61, 4, 33, 32)           # %33 = buf[0]
    w += spv_inst(249, 34)
    w += spv_inst(248, 34)                 # header
    w += spv_inst(245, 4, 35, 11, 30, 36, 37)
    w += spv_inst(176, 3, 38, 35, 13)      # i < 2
    w += spv_inst(246, 39, 37, 0)
    w += spv_inst(250, 38, 37, 39)
    w += spv_inst(248, 37)                 # body/cont
    w += spv_inst(80, 20, 40, 33, 23)      # v2uint(buf[0], 20)
    w += spv_inst(80, 21, 41, 40, 24, 25)  # v4uint(v2, 30, 40) -> MIXED vector + scalars!
    w += spv_inst(62, 31, 41)              # store to Function v4uint
    w += spv_inst(61, 21, 42, 31)          # load Function v4uint
    w += spv_inst(81, 4, 43, 42, 0)
    w += spv_inst(81, 4, 44, 42, 1)
    w += spv_inst(81, 4, 45, 42, 2)
    w += spv_inst(81, 4, 46, 42, 3)
    w += spv_inst(132, 4, 47, 35, 15)      # base = i * 4
    w += spv_inst(128, 4, 48, 47, 12)      # +1
    w += spv_inst(128, 4, 49, 47, 13)      # +2
    w += spv_inst(128, 4, 50, 47, 14)      # +3
    w += spv_inst(65, 8, 51, 10, 11, 47)
    w += spv_inst(62, 51, 43)
    w += spv_inst(65, 8, 52, 10, 11, 48)
    w += spv_inst(62, 52, 44)
    w += spv_inst(65, 8, 53, 10, 11, 49)
    w += spv_inst(62, 53, 45)
    w += spv_inst(65, 8, 54, 10, 11, 50)
    w += spv_inst(62, 54, 46)
    w += spv_inst(128, 4, 36, 35, 12)
    w += spv_inst(249, 34)
    w += spv_inst(248, 39)
    w += spv_inst(253)
    w += spv_inst(56)
    cases["L1_mixed_vec_construct"] = spv_module(60, w)

    # L2a: OpDecorate RelaxedPrecision on an instruction inside a counted loop -> must refuse unrolling (left == 1)
    w = preamble(16, 10, 5, 6, extra_decor=spv_inst(71, 40, 0)) + base_types()
    w += spv_inst(54, 1, 16, 0, 2)
    w += spv_inst(248, 30)
    w += spv_inst(249, 31)
    w += spv_inst(248, 31)
    w += spv_inst(245, 4, 32, 11, 30, 33, 34)
    w += spv_inst(176, 3, 35, 32, 13)
    w += spv_inst(246, 36, 34, 0)
    w += spv_inst(250, 35, 34, 36)
    w += spv_inst(248, 34)
    w += spv_inst(128, 4, 40, 32, 12)      # decorated %40
    w += spv_inst(65, 8, 41, 10, 11, 32)
    w += spv_inst(62, 41, 40)
    w += spv_inst(128, 4, 33, 32, 12)
    w += spv_inst(249, 31)
    w += spv_inst(248, 36)
    w += spv_inst(253)
    w += spv_inst(56)
    cases["L2a_decorated_loop"] = spv_module(50, w)

    # L2b: OpDecorate RelaxedPrecision on a Function-array load outside loop -> load must not be forwarded/deleted
    w = preamble(16, 10, 5, 6, extra_decor=spv_inst(71, 34, 0)) + base_types()
    w += spv_inst(28, 20, 4, 13)           # uint[2]
    w += spv_inst(32, 21, 7, 20)           # ptr Function uint[2]
    w += spv_inst(43, 4, 22, 111)
    w += spv_inst(43, 4, 23, 222)
    w += spv_inst(44, 20, 24, 22, 23)
    w += spv_inst(54, 1, 16, 0, 2)
    w += spv_inst(248, 30)
    w += spv_inst(59, 21, 31, 7)
    w += spv_inst(62, 31, 24)
    w += spv_inst(65, 9, 33, 31, 11)
    w += spv_inst(61, 4, 34, 33)           # decorated load %34
    w += spv_inst(249, 35)
    w += spv_inst(248, 35)
    w += spv_inst(245, 4, 36, 11, 30, 37, 38)
    w += spv_inst(176, 3, 39, 36, 13)
    w += spv_inst(246, 40, 38, 0)
    w += spv_inst(250, 39, 38, 40)
    w += spv_inst(248, 38)
    w += spv_inst(128, 4, 41, 34, 36)
    w += spv_inst(65, 8, 42, 10, 11, 36)
    w += spv_inst(62, 42, 41)
    w += spv_inst(128, 4, 37, 36, 12)
    w += spv_inst(249, 35)
    w += spv_inst(248, 40)
    w += spv_inst(253)
    w += spv_inst(56)
    cases["L2b_decorated_load"] = spv_module(50, w)

    # L3: OpName on a forwarded Function-array load outside loop -> OpName stripped cleanly so spirv-val passes
    w = preamble(16, 10, 5, 6, extra_debug=spv_inst(5, 34, *spv_str("named_load"))) + base_types()
    w += spv_inst(28, 20, 4, 13)
    w += spv_inst(32, 21, 7, 20)
    w += spv_inst(43, 4, 22, 111)
    w += spv_inst(43, 4, 23, 222)
    w += spv_inst(44, 20, 24, 22, 23)
    w += spv_inst(54, 1, 16, 0, 2)
    w += spv_inst(248, 30)
    w += spv_inst(59, 21, 31, 7)
    w += spv_inst(62, 31, 24)
    w += spv_inst(65, 9, 33, 31, 12)
    w += spv_inst(61, 4, 34, 33)           # named load %34 (forwarded to 222 and removed)
    w += spv_inst(249, 35)
    w += spv_inst(248, 35)
    w += spv_inst(245, 4, 36, 11, 30, 37, 38)
    w += spv_inst(176, 3, 39, 36, 13)
    w += spv_inst(246, 40, 38, 0)
    w += spv_inst(250, 39, 38, 40)
    w += spv_inst(248, 38)
    w += spv_inst(128, 4, 41, 34, 36)
    w += spv_inst(65, 8, 42, 10, 11, 36)
    w += spv_inst(62, 42, 41)
    w += spv_inst(128, 4, 37, 36, 12)
    w += spv_inst(249, 35)
    w += spv_inst(248, 40)
    w += spv_inst(253)
    w += spv_inst(56)
    cases["L3_named_forwarded_load"] = spv_module(50, w)

    # L4: OpTypeInt 32 1 (%20 = signed int) declared AFTER the last OpConstant, used by OpBitcast inside loop
    w = preamble(16, 10, 5, 6) + base_types()
    w += spv_inst(21, 20, 32, 1)           # %20 = int (AFTER all OpConstants 11..15!)
    w += spv_inst(54, 1, 16, 0, 2)
    w += spv_inst(248, 30)
    w += spv_inst(249, 31)
    w += spv_inst(248, 31)
    w += spv_inst(245, 4, 32, 11, 30, 33, 34)
    w += spv_inst(176, 3, 35, 32, 13)
    w += spv_inst(246, 36, 34, 0)
    w += spv_inst(250, 35, 34, 36)
    w += spv_inst(248, 34)
    w += spv_inst(124, 20, 40, 32)         # OpBitcast %int %i -> folds to OpConstant %20!
    w += spv_inst(124, 4, 41, 40)          # OpBitcast %uint %40
    w += spv_inst(65, 8, 42, 10, 11, 32)
    w += spv_inst(62, 42, 41)
    w += spv_inst(128, 4, 33, 32, 12)
    w += spv_inst(249, 31)
    w += spv_inst(248, 36)
    w += spv_inst(253)
    w += spv_inst(56)
    cases["L4_type_after_last_const"] = spv_module(50, w)

    # L5: qualified OpStore (Aligned 4) to a write-only Function variable -> must NOT delete variable leaving dangling store
    w = preamble(16, 10, 5, 6) + base_types()
    w += spv_inst(54, 1, 16, 0, 2)
    w += spv_inst(248, 30)
    w += spv_inst(59, 9, 31, 7)            # Function uint %31
    w += spv_inst(249, 32)
    w += spv_inst(248, 32)
    w += spv_inst(245, 4, 33, 11, 30, 34, 35)
    w += spv_inst(176, 3, 36, 33, 13)
    w += spv_inst(246, 37, 35, 0)
    w += spv_inst(250, 36, 35, 37)
    w += spv_inst(248, 35)
    w += spv_inst(62, 31, 33, 2, 4)        # OpStore %31 %33 Aligned 4
    w += spv_inst(65, 8, 38, 10, 11, 33)
    w += spv_inst(62, 38, 33)
    w += spv_inst(128, 4, 34, 33, 12)
    w += spv_inst(249, 32)
    w += spv_inst(248, 37)
    w += spv_inst(253)
    w += spv_inst(56)
    cases["L5_aligned_store"] = spv_module(50, w)

    # L8: nested continue inside a selection arm that does work before continuing -> must refuse unrolling (left == 1)
    w = preamble(16, 10, 5, 6) + base_types()
    w += spv_inst(54, 1, 16, 0, 2)
    w += spv_inst(248, 30)
    w += spv_inst(65, 8, 31, 10, 11, 11)
    w += spv_inst(61, 4, 32, 31)           # %32 = buf[0]
    w += spv_inst(249, 33)
    w += spv_inst(248, 33)                 # header
    w += spv_inst(245, 4, 34, 11, 30, 35, 36)
    w += spv_inst(176, 3, 37, 34, 13)
    w += spv_inst(246, 38, 36, 0)
    w += spv_inst(250, 37, 39, 38)
    w += spv_inst(248, 39)                 # body
    w += spv_inst(171, 3, 40, 32, 11)      # buf[0] != 0
    w += spv_inst(247, 41, 0)              # outer OpSelectionMerge %41
    w += spv_inst(250, 40, 42, 41)
    w += spv_inst(248, 42)                 # selection arm: does a store AND branches directly to %cont (%36)!
    w += spv_inst(65, 8, 43, 10, 11, 12)
    w += spv_inst(62, 43, 34)
    w += spv_inst(249, 36)                 # non-trivial continue block inside selection
    w += spv_inst(248, 41)                 # selection merge
    w += spv_inst(249, 36)
    w += spv_inst(248, 36)                 # continue block
    w += spv_inst(128, 4, 35, 34, 12)
    w += spv_inst(249, 33)
    w += spv_inst(248, 38)                 # loop merge
    w += spv_inst(253)
    w += spv_inst(56)
    cases["L8_non_trivial_continue"] = spv_module(55, w)

    # L9: OpLine immediately before an OpPhi in the loop merge block -> must refuse unrolling (left == 1)
    w = preamble(16, 10, 5, 6, extra_debug=spv_inst(7, 25, *spv_str("test.hlsl"))) + base_types()
    w += spv_inst(54, 1, 16, 0, 2)
    w += spv_inst(248, 30)
    w += spv_inst(249, 31)
    w += spv_inst(248, 31)
    w += spv_inst(245, 4, 32, 11, 30, 33, 34)
    w += spv_inst(176, 3, 35, 32, 13)
    w += spv_inst(246, 36, 34, 0)
    w += spv_inst(250, 35, 34, 36)
    w += spv_inst(248, 34)
    w += spv_inst(128, 4, 33, 32, 12)
    w += spv_inst(249, 31)
    w += spv_inst(248, 36)                 # merge block with OpLine before OpPhi!
    w += spv_inst(8, 25, 10, 1)            # OpLine
    w += spv_inst(245, 4, 37, 32, 31)      # OpPhi %37 (%32 from %31)
    w += spv_inst(65, 8, 38, 10, 11, 11)
    w += spv_inst(62, 38, 37)
    w += spv_inst(253)
    w += spv_inst(56)
    cases["L9_opline_before_merge_phi"] = spv_module(50, w)

    # L10: malformed 1-word OpConstantTrue -> must fail safely without out-of-bounds read
    cases["L10_malformed_truncated"] = spv_module(10, spv_inst(41))

    # R1 (Review 2085): store asuint(float2(a, b)) to Function v2uint p, load p[0], then overwrite p in the same block.
    # Dead-store elimination must remove the first OpStore, NEVER the scalar OpBitcast emitted before it.
    w = preamble(16, 10, 5, 6) + base_types()
    w += spv_inst(22, 20, 32)              # %20 = float
    w += spv_inst(23, 21, 20, 2)           # %21 = v2float
    w += spv_inst(23, 22, 4, 2)            # %22 = v2uint
    w += spv_inst(32, 23, 7, 22)           # %23 = ptr Function v2uint
    w += spv_inst(43, 4, 24, 5)            # %24 = 5u
    w += spv_inst(43, 4, 25, 6)            # %25 = 6u
    w += spv_inst(54, 1, 16, 0, 2)
    w += spv_inst(248, 30)
    w += spv_inst(59, 23, 31, 7)           # %31 = Function v2uint p
    w += spv_inst(65, 8, 32, 10, 11, 11)   # &buf[0]
    w += spv_inst(61, 4, 33, 32)           # %33 = buf[0]
    w += spv_inst(65, 8, 34, 10, 11, 12)   # &buf[1]
    w += spv_inst(61, 4, 35, 34)           # %35 = buf[1]
    w += spv_inst(65, 8, 36, 10, 11, 13)   # &buf[2]
    w += spv_inst(61, 4, 37, 36)           # %37 = buf[2]
    w += spv_inst(65, 8, 38, 10, 11, 14)   # &buf[3]
    w += spv_inst(61, 4, 39, 38)           # %39 = buf[3]
    w += spv_inst(249, 40)
    w += spv_inst(248, 40)                 # header
    w += spv_inst(245, 4, 41, 11, 30, 42, 43)
    w += spv_inst(176, 3, 44, 41, 13)      # i < 2
    w += spv_inst(246, 45, 43, 0)
    w += spv_inst(250, 44, 43, 45)
    w += spv_inst(248, 43)                 # body/cont
    w += spv_inst(124, 20, 46, 33)         # %46 = bitcast float buf[0]
    w += spv_inst(124, 20, 47, 35)         # %47 = bitcast float buf[1]
    w += spv_inst(80, 21, 48, 46, 47)      # %48 = v2float(%46, %47)
    w += spv_inst(124, 22, 49, 48)         # %49 = bitcast v2uint %48
    w += spv_inst(62, 31, 49)              # 1st store to p (triggers scalar OpBitcast unpacking!)
    w += spv_inst(65, 9, 50, 31, 11)       # &p[0]
    w += spv_inst(61, 4, 51, 50)           # %51 = p[0] (forwarded to unpacked OpBitcast!)
    w += spv_inst(80, 22, 52, 37, 39)      # %52 = v2uint(buf[2], buf[3])
    w += spv_inst(62, 31, 52)              # 2nd store to p (overwrites p -> marks 1st store dead!)
    w += spv_inst(65, 9, 53, 31, 12)       # &p[1]
    w += spv_inst(61, 4, 54, 53)           # %54 = p[1]
    w += spv_inst(128, 4, 55, 15, 41)      # 4 + i
    w += spv_inst(128, 4, 56, 25, 41)      # 6 + i
    w += spv_inst(65, 8, 57, 10, 11, 55)
    w += spv_inst(62, 57, 51)              # buf[4 + i] = p_old[0]
    w += spv_inst(65, 8, 58, 10, 11, 56)
    w += spv_inst(62, 58, 54)              # buf[6 + i] = p_new[1]
    w += spv_inst(128, 4, 42, 41, 12)
    w += spv_inst(249, 40)
    w += spv_inst(248, 45)
    w += spv_inst(253)
    w += spv_inst(56)
    cases["R1_bitcast_overwrite_dead_store"] = spv_module(65, w)

    # R2 (Review 2085): OpISub (opcode 130) constant folding inside a 2-iter counted loop
    w = preamble(16, 10, 5, 6) + base_types()
    w += spv_inst(54, 1, 16, 0, 2)
    w += spv_inst(248, 30)
    w += spv_inst(249, 31)
    w += spv_inst(248, 31)
    w += spv_inst(245, 4, 32, 11, 30, 33, 34)
    w += spv_inst(176, 3, 35, 32, 13)
    w += spv_inst(246, 36, 34, 0)
    w += spv_inst(250, 35, 34, 36)
    w += spv_inst(248, 34)
    w += spv_inst(130, 4, 40, 15, 32)      # OpISub: 4 - i -> folds to 4, 3
    w += spv_inst(65, 8, 41, 10, 11, 32)
    w += spv_inst(62, 41, 40)
    w += spv_inst(128, 4, 33, 32, 12)
    w += spv_inst(249, 31)
    w += spv_inst(248, 36)
    w += spv_inst(253)
    w += spv_inst(56)
    cases["R2_isub_const_fold"] = spv_module(50, w)

    # R3: dead-at-runtime branch computes OpISub 0 - 1 = -1 and uses it in OpAccessChain into a fixed-size array.
    # Folding must NOT substitute negative constant -1 into OpAccessChain (spirv-val forbids negative array indices).
    w = preamble(16, 10, 5, 6) + base_types()
    w += spv_inst(21, 20, 32, 1)           # %20 = signed int
    w += spv_inst(43, 20, 21, 0)           # %21 = int 0
    w += spv_inst(43, 20, 22, 1)           # %22 = int 1
    w += spv_inst(43, 20, 23, 2)           # %23 = int 2
    w += spv_inst(28, 24, 4, 13)           # %24 = uint[2]
    w += spv_inst(32, 25, 7, 24)           # %25 = ptr Function uint[2]
    w += spv_inst(43, 4, 26, 101)
    w += spv_inst(43, 4, 27, 202)
    w += spv_inst(44, 24, 28, 26, 27)
    w += spv_inst(54, 1, 16, 0, 2)
    w += spv_inst(248, 30)
    w += spv_inst(59, 25, 31, 7)
    w += spv_inst(62, 31, 28)
    w += spv_inst(249, 32)
    w += spv_inst(248, 32)                 # header: for (int i = 0; i < 2; i++)
    w += spv_inst(245, 20, 33, 21, 30, 34, 35)
    w += spv_inst(177, 3, 36, 33, 23)      # OpSLessThan i < 2
    w += spv_inst(246, 37, 35, 0)
    w += spv_inst(250, 36, 38, 37)
    w += spv_inst(248, 38)                 # body: if (i != 0) buf[0] = arr[i - 1]
    w += spv_inst(171, 3, 39, 33, 21)      # i != 0
    w += spv_inst(247, 40, 0)
    w += spv_inst(250, 39, 41, 40)
    w += spv_inst(248, 41)
    w += spv_inst(130, 20, 42, 33, 22)     # OpISub: i - 1 (becomes -1 on untaken iter 0!)
    w += spv_inst(65, 9, 43, 31, 42)       # OpAccessChain arr[i - 1]
    w += spv_inst(61, 4, 44, 43)
    w += spv_inst(65, 8, 45, 10, 11, 11)
    w += spv_inst(62, 45, 44)
    w += spv_inst(249, 40)
    w += spv_inst(248, 40)
    w += spv_inst(249, 35)
    w += spv_inst(248, 35)
    w += spv_inst(128, 20, 34, 33, 22)
    w += spv_inst(249, 32)
    w += spv_inst(248, 37)
    w += spv_inst(253)
    w += spv_inst(56)
    cases["R3_oob_access_chain_index"] = spv_module(55, w)

    # R4: OpLine between two Function OpVariables in entry block + constant-array store inside loop -> hoist must place
    # the hoisted OpStore AFTER all Function OpVariables (spirv-val forbids OpStore before OpVariable).
    w = preamble(16, 10, 5, 6, extra_debug=spv_inst(7, 25, *spv_str("test.hlsl"))) + base_types()
    w += spv_inst(28, 20, 4, 13)           # %20 = uint[2]
    w += spv_inst(32, 21, 7, 20)           # %21 = ptr Function uint[2]
    w += spv_inst(43, 4, 22, 303)
    w += spv_inst(43, 4, 23, 404)
    w += spv_inst(44, 20, 24, 22, 23)      # %24 = {303, 404}
    w += spv_inst(54, 1, 16, 0, 2)
    w += spv_inst(248, 30)
    w += spv_inst(59, 21, 31, 7)           # %31 = Function uint[2]
    w += spv_inst(8, 25, 12, 1)            # OpLine between OpVariables!
    w += spv_inst(59, 9, 32, 7)            # %32 = Function uint (after OpLine!)
    w += spv_inst(62, 32, 11)              # store 0 to %32
    w += spv_inst(249, 33)
    w += spv_inst(248, 33)                 # header: for (uint i = 0; i < 2; i++)
    w += spv_inst(245, 4, 34, 11, 30, 35, 36)
    w += spv_inst(176, 3, 37, 34, 13)
    w += spv_inst(246, 38, 36, 0)
    w += spv_inst(250, 37, 36, 38)
    w += spv_inst(248, 36)
    w += spv_inst(62, 31, 24)              # constant-array store inside loop (triggers hoisting!)
    w += spv_inst(65, 9, 39, 31, 34)
    w += spv_inst(61, 4, 40, 39)
    w += spv_inst(61, 4, 41, 32)
    w += spv_inst(128, 4, 42, 40, 41)
    w += spv_inst(65, 8, 43, 10, 11, 34)
    w += spv_inst(62, 43, 42)
    w += spv_inst(128, 4, 35, 34, 12)
    w += spv_inst(249, 33)
    w += spv_inst(248, 38)
    w += spv_inst(253)
    w += spv_inst(56)
    cases["R4_hoist_var_with_opline"] = spv_module(50, w)

    # R5: store -1 into a local Function int variable, load it, and use it in OpAccessChain in a dead-at-runtime branch ->
    # forward_function_allocas must NOT forward-substitute -1 into OpAccessChain (spirv-val rejects negative indices).
    w = preamble(16, 10, 5, 6) + base_types()
    w += spv_inst(21, 20, 32, 1)           # %20 = signed int
    w += spv_inst(32, 21, 7, 20)           # %21 = ptr Function int
    w += spv_inst(43, 20, 22, 0xFFFFFFFF)  # %22 = int -1
    w += spv_inst(43, 20, 23, 0)           # %23 = int 0
    w += spv_inst(28, 24, 4, 13)           # %24 = uint[2]
    w += spv_inst(32, 25, 7, 24)           # %25 = ptr Function uint[2]
    w += spv_inst(43, 4, 26, 505)
    w += spv_inst(43, 4, 27, 606)
    w += spv_inst(44, 24, 28, 26, 27)
    w += spv_inst(54, 1, 16, 0, 2)
    w += spv_inst(248, 30)
    w += spv_inst(59, 25, 31, 7)           # %31 = Function uint[2]
    w += spv_inst(59, 21, 32, 7)           # %32 = Function int idx_var
    w += spv_inst(62, 31, 28)
    w += spv_inst(249, 33)
    w += spv_inst(248, 33)                 # header: for (uint i = 0; i < 2; i++)
    w += spv_inst(245, 4, 34, 11, 30, 35, 36)
    w += spv_inst(176, 3, 37, 34, 13)
    w += spv_inst(246, 38, 36, 0)
    w += spv_inst(250, 37, 39, 38)
    w += spv_inst(248, 39)                 # body: if (i != 0) { idx_var = 0; } else { idx_var = -1; }
    w += spv_inst(171, 3, 40, 34, 11)      # i != 0
    w += spv_inst(247, 41, 0)
    w += spv_inst(250, 40, 42, 41)
    w += spv_inst(248, 42)                 # taken only on i==1; inside, store -1 then load in dead sub-branch
    w += spv_inst(62, 32, 22)              # idx_var = -1
    w += spv_inst(61, 20, 43, 32)          # %43 = load idx_var (would forward to -1!)
    w += spv_inst(170, 3, 44, 34, 11)      # i == 0 (always false inside i != 0!)
    w += spv_inst(247, 45, 0)
    w += spv_inst(250, 44, 46, 45)
    w += spv_inst(248, 46)
    w += spv_inst(65, 9, 47, 31, 43)       # OpAccessChain arr[%43] -> must NOT become arr[-1]!
    w += spv_inst(61, 4, 48, 47)
    w += spv_inst(65, 8, 49, 10, 11, 11)
    w += spv_inst(62, 49, 48)
    w += spv_inst(249, 45)
    w += spv_inst(248, 45)
    w += spv_inst(65, 9, 50, 31, 23)       # arr[0] = 505
    w += spv_inst(61, 4, 51, 50)
    w += spv_inst(65, 8, 52, 10, 11, 11)
    w += spv_inst(62, 52, 51)
    w += spv_inst(249, 41)
    w += spv_inst(248, 41)
    w += spv_inst(249, 36)
    w += spv_inst(248, 36)
    w += spv_inst(128, 4, 35, 34, 12)
    w += spv_inst(249, 33)
    w += spv_inst(248, 38)
    w += spv_inst(253)
    w += spv_inst(56)
    cases["R5_forward_neg_access_chain"] = spv_module(60, w)

    # R6: 4-word 0-index OpAccessChain aliasing a Function variable -> find_non_escaping_fvars must treat it as escaping
    # so DCE/forwarding does not delete stores or the variable.
    w = preamble(16, 10, 5, 6) + base_types()
    w += spv_inst(54, 1, 16, 0, 2)
    w += spv_inst(248, 30)
    w += spv_inst(59, 9, 31, 7)            # %31 = Function uint
    w += spv_inst(65, 9, 32, 31)           # 4-word 0-index OpAccessChain %32 = %31!
    w += spv_inst(62, 31, 15)              # store 4 into %31
    w += spv_inst(249, 33)
    w += spv_inst(248, 33)
    w += spv_inst(245, 4, 34, 11, 30, 35, 36)
    w += spv_inst(176, 3, 37, 34, 13)
    w += spv_inst(246, 38, 36, 0)
    w += spv_inst(250, 37, 36, 38)
    w += spv_inst(248, 36)
    w += spv_inst(61, 4, 39, 32)           # load through 0-index alias %32
    w += spv_inst(128, 4, 40, 39, 34)
    w += spv_inst(65, 8, 41, 10, 11, 34)
    w += spv_inst(62, 41, 40)
    w += spv_inst(128, 4, 35, 34, 12)
    w += spv_inst(249, 33)
    w += spv_inst(248, 38)
    w += spv_inst(253)
    w += spv_inst(56)
    cases["R6_zero_index_access_chain"] = spv_module(50, w)

    # R7: a header phi fed by the loop's own test (flag = phi(false, i < 2)), read in the body. The test is not cloned, so
    # a copy would branch on an id nothing defines -> must refuse unrolling (left == 1)
    w = preamble(16, 10, 5, 6) + base_types()
    w += spv_inst(42, 3, 20)               # %20 = false
    w += spv_inst(54, 1, 16, 0, 2)
    w += spv_inst(248, 30)
    w += spv_inst(249, 31)
    w += spv_inst(248, 31)                 # header
    w += spv_inst(245, 4, 32, 11, 30, 33, 34)   # i
    w += spv_inst(245, 3, 35, 20, 30, 36, 34)   # flag = phi(false from entry, %36 the test from the continue block)
    w += spv_inst(176, 3, 36, 32, 13)      # i < 2
    w += spv_inst(246, 37, 34, 0)
    w += spv_inst(250, 36, 38, 37)
    w += spv_inst(248, 38)                 # body: if (flag) buf[i] = 4
    w += spv_inst(247, 39, 0)
    w += spv_inst(250, 35, 40, 39)
    w += spv_inst(248, 40)
    w += spv_inst(65, 8, 41, 10, 11, 32)
    w += spv_inst(62, 41, 15)
    w += spv_inst(249, 39)
    w += spv_inst(248, 39)
    w += spv_inst(249, 34)
    w += spv_inst(248, 34)                 # continue block
    w += spv_inst(128, 4, 33, 32, 12)
    w += spv_inst(249, 31)
    w += spv_inst(248, 37)
    w += spv_inst(253)
    w += spv_inst(56)
    cases["R7_phi_fed_by_loop_test"] = spv_module(50, w)

    # R8: for (uint i = 0; i < 4; i++) if (i < 2) buf[i] = arr[i]; over uint arr[2]. Unrolled, copies 2 and 3 would hold
    # arr[2] and arr[3] as constant indices in a branch never taken -> must refuse unrolling (left == 1)
    w = preamble(16, 10, 5, 6) + base_types()
    w += spv_inst(28, 24, 4, 13)           # %24 = uint[2]
    w += spv_inst(32, 25, 7, 24)           # %25 = ptr Function uint[2]
    w += spv_inst(43, 4, 26, 101)
    w += spv_inst(43, 4, 27, 202)
    w += spv_inst(44, 24, 28, 26, 27)
    w += spv_inst(54, 1, 16, 0, 2)
    w += spv_inst(248, 30)
    w += spv_inst(59, 25, 31, 7)
    w += spv_inst(62, 31, 28)
    w += spv_inst(249, 32)
    w += spv_inst(248, 32)                 # header
    w += spv_inst(245, 4, 33, 11, 30, 34, 35)
    w += spv_inst(176, 3, 36, 33, 15)      # i < 4
    w += spv_inst(246, 37, 35, 0)
    w += spv_inst(250, 36, 38, 37)
    w += spv_inst(248, 38)
    w += spv_inst(176, 3, 39, 33, 13)      # i < 2
    w += spv_inst(247, 40, 0)
    w += spv_inst(250, 39, 41, 40)
    w += spv_inst(248, 41)
    w += spv_inst(65, 9, 43, 31, 33)       # arr[i]
    w += spv_inst(61, 4, 44, 43)
    w += spv_inst(65, 8, 45, 10, 11, 33)
    w += spv_inst(62, 45, 44)
    w += spv_inst(249, 40)
    w += spv_inst(248, 40)
    w += spv_inst(249, 35)
    w += spv_inst(248, 35)
    w += spv_inst(128, 4, 34, 33, 12)
    w += spv_inst(249, 32)
    w += spv_inst(248, 37)
    w += spv_inst(253)
    w += spv_inst(56)
    cases["R8_counter_index_past_array"] = spv_module(50, w)

    # R9: a constant composite no instruction reads, decorated BuiltIn WorkgroupSize -> DCE must keep it and its decoration
    w = preamble(16, 10, 5, 6, extra_decor=spv_inst(71, 21, 11, 25)) + base_types()
    w += spv_inst(23, 20, 4, 3)            # %20 = uvec3
    w += spv_inst(44, 20, 21, 12, 12, 12)  # %21 = (1, 1, 1)
    w += spv_inst(54, 1, 16, 0, 2)
    w += spv_inst(248, 30)
    w += spv_inst(249, 33)
    w += spv_inst(248, 33)
    w += spv_inst(245, 4, 34, 11, 30, 35, 36)
    w += spv_inst(176, 3, 37, 34, 13)
    w += spv_inst(246, 38, 36, 0)
    w += spv_inst(250, 37, 36, 38)
    w += spv_inst(248, 36)
    w += spv_inst(128, 4, 40, 34, 15)      # i + 4
    w += spv_inst(65, 8, 41, 10, 11, 34)
    w += spv_inst(62, 41, 40)              # buf[i] = i + 4
    w += spv_inst(128, 4, 35, 34, 12)
    w += spv_inst(249, 33)
    w += spv_inst(248, 38)
    w += spv_inst(253)
    w += spv_inst(56)
    cases["R9_decorated_unused_composite"] = spv_module(50, w)

    # R10: loop A (i = 5, 6) is refused late, after the constant 6 was made for its copies: past it, a branch never taken
    # reads arr[i] over uint arr[2], and i is 7 there. Loop B needs the same 6: it must get a constant the module defines,
    # not the id A's refused attempt gave back -> A stays rolled (left == 1), B unrolls, the output is valid
    w = preamble(16, 10, 5, 6) + base_types()
    w += spv_inst(43, 4, 20, 5)
    w += spv_inst(43, 4, 21, 7)
    w += spv_inst(43, 4, 22, 12345)
    w += spv_inst(28, 24, 4, 13)           # %24 = uint[2]
    w += spv_inst(32, 25, 7, 24)
    w += spv_inst(43, 4, 26, 101)
    w += spv_inst(43, 4, 27, 202)
    w += spv_inst(44, 24, 28, 26, 27)
    w += spv_inst(54, 1, 16, 0, 2)
    w += spv_inst(248, 30)
    w += spv_inst(59, 25, 31, 7)
    w += spv_inst(62, 31, 28)
    w += spv_inst(65, 8, 29, 10, 11, 11)
    w += spv_inst(61, 4, 46, 29)           # %46 = buf[0]
    w += spv_inst(249, 32)
    w += spv_inst(248, 32)                 # loop A: for (i = 5; i < 7; i++) buf[1] = i
    w += spv_inst(245, 4, 33, 20, 30, 34, 35)
    w += spv_inst(176, 3, 36, 33, 21)
    w += spv_inst(246, 37, 35, 0)
    w += spv_inst(250, 36, 35, 37)
    w += spv_inst(248, 35)
    w += spv_inst(65, 8, 38, 10, 11, 12)
    w += spv_inst(62, 38, 33)
    w += spv_inst(128, 4, 34, 33, 12)
    w += spv_inst(249, 32)
    w += spv_inst(248, 37)                 # past A: if (buf[0] == 12345) buf[2] = arr[i]
    w += spv_inst(170, 3, 39, 46, 22)
    w += spv_inst(247, 40, 0)
    w += spv_inst(250, 39, 41, 40)
    w += spv_inst(248, 41)
    w += spv_inst(65, 9, 42, 31, 33)       # arr[i]: i is 7 here
    w += spv_inst(61, 4, 43, 42)
    w += spv_inst(65, 8, 44, 10, 11, 13)
    w += spv_inst(62, 44, 43)
    w += spv_inst(249, 40)
    w += spv_inst(248, 40)
    w += spv_inst(249, 50)
    w += spv_inst(248, 50)                 # loop B: for (j = 5; j < 7; j++) buf[3] = j
    w += spv_inst(245, 4, 51, 20, 40, 52, 53)
    w += spv_inst(176, 3, 54, 51, 21)
    w += spv_inst(246, 55, 53, 0)
    w += spv_inst(250, 54, 53, 55)
    w += spv_inst(248, 53)
    w += spv_inst(65, 8, 56, 10, 11, 14)
    w += spv_inst(62, 56, 51)
    w += spv_inst(128, 4, 52, 51, 12)
    w += spv_inst(249, 50)
    w += spv_inst(248, 55)
    w += spv_inst(253)
    w += spv_inst(56)
    cases["R10_refused_loop_constants"] = spv_module(60, w)

    return cases


def function_body(text, signature):
    """The body of the function whose definition starts with `signature`, by its braces"""
    at = text.find(signature)
    if at < 0:
        return ""
    i = text.index("{", at)
    depth = 0
    for j in range(i, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[i:j + 1]
    return ""


def source_problems():
    """The hooks' safety in vk_fsr4.cpp and the close in vk_main.cpp, read as text"""
    problems = []
    src = (VK / "vk_fsr4.cpp").read_text(encoding="utf-8")
    proc = function_body(src, "VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL device_proc(")
    for name, hook in [("vkDestroyBuffer", "destroy_buffer"), ("vkUpdateDescriptorSets", "update_sets"),
                       ("vkCmdPushDescriptorSetKHR", "push_set"), ("vkGetDescriptorEXT", "get_descriptor"),
                       ("vkGetBufferDeviceAddress", "buffer_address"), ("vkDestroyShaderModule", "destroy_shader_module")]:
        if '"%s"' % name not in proc or "&%s;" % hook not in proc:
            problems.append("device_proc does not hand the library %s for %s" % (hook, name))
    create = function_body(src, "VKAPI_ATTR VkResult VKAPI_CALL create_buffer(")
    if "VK_BUFFER_USAGE_TRANSFER_DST_BIT" not in create:
        problems.append("create_buffer does not make the grown buffer a transfer destination (vkCmdUpdateBuffer writes its tail)")
    if "forget_locked(*b)" not in create or "forget_locked(b)" not in function_body(src, "VKAPI_ATTR void VKAPI_CALL destroy_buffer("):
        problems.append("a destroyed buffer (or its handle given out again) is not forgotten with its tail")
    forget = function_body(src, "void forget_locked(")
    if "cur = 0" not in forget or "bigs.erase" not in forget:
        problems.append("forget_locked does not drop the buffer and stop new modules being rewritten against it")
    module = function_body(src, "VKAPI_ATTR VkResult VKAPI_CALL create_shader_module(")
    if "bigs.find(cur)" not in module or "mod.gen = cur" not in module:
        problems.append("create_shader_module does not rewrite against the newest live buffer and remember which")
    binding = function_body(src, "void note_binding_locked(")
    if "fsr4_tune::covers_tail" not in binding or "g.bound = -1" not in binding:
        problems.append("note_binding_locked does not check a binding with covers_tail and mark one without the tail")
    dispatch = function_body(src, "VKAPI_ATTR void VKAPI_CALL cmd_dispatch(")
    if "bound == 1" not in dispatch or "make_plain(" not in dispatch or "real_bind(cb, VK_PIPELINE_BIND_POINT_COMPUTE, use)" not in dispatch:
        problems.append("cmd_dispatch does not write the tail only for a live, checked buffer, with AMD's own pipeline otherwise")
    if "real_update(cb, big.buffer" not in dispatch:
        problems.append("cmd_dispatch does not write the tail into the pipeline's own buffer")
    pipes = function_body(src, "VKAPI_ATTR VkResult VKAPI_CALL create_compute_pipelines(")
    detach = pipes.find(".detach()")
    if detach >= 0 and pipes.rfind("jobs().running++", 0, detach) < 0:
        problems.append("an unroll thread is started without being counted (fsr4_close waits for the count)")
    if "closing" not in pipes:
        problems.append("create_compute_pipelines starts unroll jobs after the window closed")
    job = function_body(src, "void build_unrolled(")
    if "j.running--" not in job or "if (closing) return;" not in job:
        problems.append("build_unrolled does not end its count or leave when the window closed")
    close = function_body(src, "void close()")
    if "closing.exchange(true)" not in close or "wait_for(" not in close:
        problems.append("tune::close does not stop the jobs and wait for the one building")
    if "void fsr4_close() { tune::close(); }" not in src or src.count("void fsr4_close()") != 2:
        problems.append("fsr4_close is not defined in both builds (with and without FSR)")
    for fn, real in [("create_buffer", "real_cb"), ("create_shader_module", "real_csm"),
                     ("create_compute_pipelines", "real_ccp")]:
        body = function_body(src, "VKAPI_ATTR VkResult VKAPI_CALL " + fn + "(")
        if "suppressed" not in body or "return " + real not in body:
            problems.append(fn + " does not pass through while the new context is untuned")
    if "suppressed" in function_body(src, "VKAPI_ATTR void VKAPI_CALL cmd_dispatch("):
        problems.append("cmd_dispatch is suppressed too: the old Auto context's tail would not be served")
    make = function_body(src, "std::shared_ptr<Context> make_context(")
    if "tune::suppress" not in make or make.find("tune::suppress") > make.find("l.create("):
        problems.append("make_context does not set tune::suppress before l.create")
    prep = function_body(src, "int prepare(")
    if "s.context->model != want" not in prep or "make_context(ow, oh, hdr, want)" not in prep:
        problems.append("prepare does not rebuild when the FSR 4 model changes")
    load = function_body(src, "bool load(")
    gate = load.find("tune::expanded_unroll = best[0] == 4 && best[1] == 1 && best[2] == 2;")
    if gate < 0 or load.count("tune::expanded_unroll =") != 1:
        problems.append("load does not give the expanded unroller to FSR 4.1.2 alone (the shader set it was checked against)")
    elif not (load.rfind("pick_newest();") < gate < load.find("l.ok = true;")):
        problems.append("load sets expanded_unroll before the version is final (pick_newest) or after l.ok lets contexts start")
    main_cpp = (VK / "vk_main.cpp").read_text(encoding="utf-8")
    closer = function_body(main_cpp, "void mkwvk_close_asked(void)")
    if "fsr4_close();" not in closer or closer.find("fsr4_close();") > closer.find("pipeline_cache_close();"):
        problems.append("mkwvk_close_asked does not stop FSR 4's builds before the pipeline cache's save")
    return problems


def layer_reference(scratch, init, gid_x):
    """Exact reference execution of tests/fsr4_tune/layer.hlsl for one invocation."""
    scratch = list(scratch)
    o = gid_x
    for i in range(4):
        for j in range(2):
            for k in range(3):
                val = (scratch[o] + W[i * 2 + j] * scratch[gid_x + k + 64] + init[k] + V[(k + i) & 3]) & 0xFFFFFFFF
                scratch[o] = val
    return scratch


def postpass_reference(in_buf, scratch, width, gid_x):
    """Exact reference execution of tests/fsr4_tune/postpass.hlsl for one invocation."""
    scratch = list(scratch)
    for y in range(2):
        x = gid_x + y
        if x >= width:
            continue
        sum0 = 0
        sum1 = 1
        for i in range(4):
            v = in_buf[x + i]
            sum0 = (sum0 + v * POST_W[y * 4 + i]) & 0xFFFFFFFF
            sum1 = (sum1 ^ ((v + POST_W[i]) & 0xFFFFFFFF)) & 0xFFFFFFFF
        scratch[gid_x * 2 + y] = (sum0 + sum1) & 0xFFFFFFFF
    return scratch


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cxx", default=os.environ.get("CXX") or "g++")
    p.add_argument("--spirv-val", default="spirv-val")
    a = p.parse_args()
    problems = source_problems()
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        exe = t / "tune.exe"
        (t / "tune.cpp").write_text(DRIVER, encoding="utf-8")
        # (static on Windows: a MinGW compiler's C++ runtime is not on the PATH where the test runs)
        subprocess.run([a.cxx, "-std=c++17", "-O1", "-I", str(VK), str(t / "tune.cpp"), "-o", str(exe)] +
                       (["-static"] if os.name == "nt" else []), check=True)

        def run_text(*args):
            return subprocess.run([str(exe)] + [str(x) for x in args], capture_output=True, text=True, check=True).stdout

        def unpack_blobs(raw, count):
            off = 0
            out = []
            for _ in range(count):
                (n,) = struct.unpack_from("<I", raw, off)
                off += 4
                out.append(raw[off:off + n])
                off += n
            return out

        def run_stdin_tune(spv_bytes, base_word, *tail_words):
            raw = subprocess.run([str(exe), "stdin_tune", str(base_word)] + [str(x) for x in tail_words],
                                 input=spv_bytes, capture_output=True, check=True).stdout
            text_b, tail_b, unroll_b = unpack_blobs(raw, 3)
            return facts(text_b.decode("utf-8")), tail_b, unroll_b

        def run_stdin_unroll(spv_bytes, max_words):
            raw = subprocess.run([str(exe), "stdin_unroll", str(max_words)],
                                 input=spv_bytes, capture_output=True, check=True).stdout
            ok, left, det, n = struct.unpack_from("<IiII", raw, 0)
            return bool(ok), left, det, raw[16:16 + n]

        def valid_bytes(spv_bytes, what):
            r = subprocess.run([a.spirv_val, "--target-env", "vulkan1.3", "-"], input=spv_bytes, capture_output=True)
            if r.returncode:
                msg = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
                problems.append("%s is not valid SPIR-V: %s" % (what, msg))

        layer_bytes = (FIXTURES / "layer.spv").read_bytes()
        valid_bytes(layer_bytes, "tests/fsr4_tune/layer.spv")
        base = words_from_bytes(layer_bytes)
        f, layer_tail_bytes, layer_un_bytes = run_stdin_tune(layer_bytes, BASE, *BEFORE)
        if f["entry"] != ["fsr4_model_test_pass7", "lane_dependent", "0"]:
            problems.append("layer.spv's entry point or lane use read wrong: %s" % f["entry"])

        # --- the weights to the tail
        if f["moved"][0] != "1":
            problems.append("the layer's weight tables were not moved to the tail")
        else:
            tail = [int(x) for x in f["moved"][1:]]
            new = tail[len(BEFORE):]
            if tail[:len(BEFORE)] != BEFORE or sorted([new[:len(W)], new[len(W):]]) != sorted([W, V]) and \
                    sorted([new[:len(V)], new[len(V):]]) != sorted([V, W]):
                problems.append("the tail holds %s: not what it held, then the two tables whole" % tail)
            moved = words_from_bytes(layer_tail_bytes)
            valid_bytes(layer_tail_bytes, "the layer with its weights in the tail")
            added = set(uint_constants(moved).values()) - set(uint_constants(base).values())
            where = {BASE + len(BEFORE) + i for i in range(len(new)) if new[i:i + len(W)] == W or new[i:i + len(V)] == V}
            if not where or not where <= added:
                problems.append("the tables' places in the buffer %s are not among the constants the rewrite added %s" %
                                (sorted(where), sorted(added)))
            if function_vars(moved) != function_vars(base) - 2:
                problems.append("the two tables' Function arrays are not gone (%d Function variables, %d before)" %
                                (function_vars(moved), function_vars(base)))
            names = names_of(base)
            rw = [v for v, n in names.items() if n == "ScratchBuffer"]
            ro = [v for v, n in names.items() if n == "InitializerBuffer"]
            if not rw or not ro:
                problems.append("layer.spv's buffers are not named as expected: %s" % names)
            elif chains_into(moved, rw[0]) <= chains_into(base, rw[0]) or chains_into(moved, ro[0]) != chains_into(base, ro[0]):
                problems.append("the weights are not read from the writable buffer alone (reads of it %d -> %d, of the "
                                "read-only one %d -> %d)" % (chains_into(base, rw[0]), chains_into(moved, rw[0]),
                                                            chains_into(base, ro[0]), chains_into(moved, ro[0])))

        # --- the loops + semantic equivalence for layer.spv
        done, before, left, size, det = f["unrolled"]
        if done != "1" or before != "3" or left != "0" or det != "1":
            problems.append("the layer's 3 loops were not all unrolled deterministically: done %s, loops before %s, left %s, det %s" %
                            (done, before, left, det))
        else:
            valid_bytes(layer_un_bytes, "the unrolled layer")
            un_words = words_from_bytes(layer_un_bytes)
            if int(size) <= len(base):
                problems.append("the unrolled layer (%s words) is no larger than the original (%d)" % (size, len(base)))
            if function_vars(un_words) != 0:
                problems.append("the unrolled layer still has %d Function arrays (expected 0 after folding)" % function_vars(un_words))
            if constant_composites(un_words) != 0:
                problems.append("the unrolled layer still has %d unused OpConstantComposite tables (expected 0)" %
                                constant_composites(un_words))
            # Semantic equivalence across invocations (rolled vs unrolled+folded vs tail-rewritten vs Python reference)
            init_buf = [0x10203040, 0x55AA33CC, 0xDEAD0007]
            scratch_init = [((k * 2654435761) ^ 0xA5A55A5A) & 0xFFFFFFFF for k in range(128)]
            for gid_x in (0, 1, 2, 3):
                b_orig = {"ScratchBuffer": list(scratch_init), "InitializerBuffer": list(init_buf)}
                b_un = {"ScratchBuffer": list(scratch_init), "InitializerBuffer": list(init_buf)}
                run_spv_vm(base, b_orig, gid=(gid_x, 0, 0))
                run_spv_vm(un_words, b_un, gid=(gid_x, 0, 0))
                expected = layer_reference(scratch_init, init_buf, gid_x)
                if b_orig["ScratchBuffer"] != expected or b_un["ScratchBuffer"] != expected:
                    problems.append("layer.spv semantic mismatch at gid_x=%d: orig=%s unrolled=%s expected=%s" %
                                    (gid_x, b_orig["ScratchBuffer"][gid_x], b_un["ScratchBuffer"][gid_x], expected[gid_x]))
                if layer_tail_bytes:
                    tail_vals = [int(x) for x in f["moved"][1:]]
                    sc_tail = list(scratch_init) + [0] * (BASE - len(scratch_init)) + tail_vals
                    b_tail = {"ScratchBuffer": sc_tail, "InitializerBuffer": list(init_buf)}
                    run_spv_vm(words_from_bytes(layer_tail_bytes), b_tail, gid=(gid_x, 0, 0))
                    if b_tail["ScratchBuffer"][:len(expected)] != expected:
                        problems.append("layer_tail.spv semantic mismatch at gid_x=%d" % gid_x)

        some, left_small, size_small, limit = f["capped"]
        if left_small == "0" or int(size_small) > int(limit):
            problems.append("a size limit of %s words: %s loops left, %s words (some loops must stay rolled, within the "
                            "limit)" % (limit, left_small, size_small))

        # --- the postpass (OpINotEqual, multi-OpPhi live-out accumulators, if (oob) continue;, constant table)
        post_bytes = (FIXTURES / "postpass.spv").read_bytes()
        valid_bytes(post_bytes, "tests/fsr4_tune/postpass.spv")
        post_base = words_from_bytes(post_bytes)
        pf, post_tail_bytes, post_un_bytes = run_stdin_tune(post_bytes, BASE)
        if pf["entry"] != ["fsr4_model_test_postpass", "lane_dependent", "0"]:
            problems.append("postpass.spv's entry point or lane use read wrong: %s" % pf["entry"])
        if pf["moved"][0] != "1":
            problems.append("postpass.spv's weight table was not moved to the tail")
        else:
            valid_bytes(post_tail_bytes, "the postpass with its weights in the tail")
        pdone, pbefore, pleft, _, pdet = pf["unrolled"]
        if pdone != "1" or pbefore != "2" or pleft != "0" or pdet != "1":
            problems.append("postpass.spv's 2 loops (OpINotEqual, multi-phi, continue) were not all unrolled deterministically: "
                            "done %s, before %s, left %s, det %s" % (pdone, pbefore, pleft, pdet))
        else:
            valid_bytes(post_un_bytes, "the unrolled postpass")
            pun = words_from_bytes(post_un_bytes)
            if function_vars(pun) != 0:
                problems.append("the unrolled postpass still has %d Function arrays (expected 0 after folding)" % function_vars(pun))
            if constant_composites(pun) != 0:
                problems.append("the unrolled postpass still has %d unused OpConstantComposite tables (expected 0)" %
                                constant_composites(pun))
            # Semantic equivalence across invocations covering both continue outcomes (x < width and x >= width)
            in_buf = [0x11223344, 7, 0x80000019, 99, 0x00FF00AA, 13, 42, 0xDEADBEEF, 5, 19]
            width = 3
            sentinels = [0xC0FFEE00 + k for k in range(16)]
            for gid_x in (0, 1, 2, 3, 4):
                p_orig = {"InBuffer": list(in_buf), "ScratchBuffer": list(sentinels)}
                p_un = {"InBuffer": list(in_buf), "ScratchBuffer": list(sentinels)}
                run_spv_vm(post_base, p_orig, uniform_words=[width], gid=(gid_x, 0, 0))
                run_spv_vm(pun, p_un, uniform_words=[width], gid=(gid_x, 0, 0))
                expected = postpass_reference(in_buf, sentinels, width, gid_x)
                if p_orig["ScratchBuffer"] != expected or p_un["ScratchBuffer"] != expected:
                    problems.append("postpass.spv semantic mismatch at gid_x=%d (width=%d): orig=%s unrolled=%s expected=%s" %
                                    (gid_x, width, p_orig["ScratchBuffer"][:10], p_un["ScratchBuffer"][:10], expected[:10]))
                if post_tail_bytes:
                    ptail = [int(x) for x in pf["moved"][1:]]
                    sc_t = list(sentinels) + [0] * (BASE - len(sentinels)) + ptail
                    p_tail = {"InBuffer": list(in_buf), "ScratchBuffer": sc_t}
                    run_spv_vm(words_from_bytes(post_tail_bytes), p_tail, uniform_words=[width], gid=(gid_x, 0, 0))
                    if p_tail["ScratchBuffer"][:len(expected)] != expected:
                        problems.append("postpass_tail.spv semantic mismatch at gid_x=%d" % gid_x)

        # --- synthetic regression cases (L1-L5, L8-L10)
        synth = build_synthetic_regressions()
        for name, spv_b in synth.items():
            if name != "L10_malformed_truncated":
                valid_bytes(spv_b, "synthetic input %s" % name)
            ok, sleft, sdet, out_b = run_stdin_unroll(spv_b, 20000)
            if not sdet:
                problems.append("synthetic %s: non-deterministic unroll output" % name)
            if name == "L10_malformed_truncated":
                if ok:
                    problems.append("synthetic %s: malformed truncated SPIR-V should fail cleanly" % name)
                continue
            if name in ("L2a_decorated_loop", "L8_non_trivial_continue", "L9_opline_before_merge_phi",
                        "R7_phi_fed_by_loop_test", "R8_counter_index_past_array"):
                if sleft != 1:
                    problems.append("synthetic %s: expected loop to stay rolled (left=1), got ok=%s left=%d" % (name, ok, sleft))
                if ok:
                    valid_bytes(out_b, "synthetic output %s" % name)
                continue
            want_left = 1 if name == "R10_refused_loop_constants" else 0   # (its first loop stays rolled, its second unrolls)
            if not ok or sleft != want_left:
                problems.append("synthetic %s: expected an unroll (ok=1, left=%d), got ok=%s left=%d" % (name, want_left, ok, sleft))
                continue
            valid_bytes(out_b, "synthetic output %s" % name)
            # Semantic check for L1, L2b, L3, L4, L5, R1, R2
            orig_w, out_w = words_from_bytes(spv_b), words_from_bytes(out_b)
            b0 = {"buf": [777, 888, 999, 1111, 0, 0, 0, 0]}
            b1 = {"buf": [777, 888, 999, 1111, 0, 0, 0, 0]}
            try:
                run_spv_vm(orig_w, b0)
                run_spv_vm(out_w, b1)
            except (KeyError, IndexError) as e:   # (an id nothing defines: reported as this case's, the rest still run)
                problems.append("synthetic %s: the interpreter stopped on %s %s" % (name, type(e).__name__, e))
                continue
            if b0["buf"] != b1["buf"]:
                problems.append("synthetic %s: semantic mismatch orig=%s out=%s" % (name, b0["buf"], b1["buf"]))
            if name == "L1_mixed_vec_construct" and b1["buf"] != [777, 20, 30, 40, 777, 20, 30, 40]:
                problems.append("synthetic %s: unexpected lanes %s" % (name, b1["buf"]))
            if name == "R1_bitcast_overwrite_dead_store" and b1["buf"][4:] != [777, 777, 1111, 1111]:
                problems.append("synthetic %s: unexpected bitcast overwrite result %s" % (name, b1["buf"]))
            if name == "R2_isub_const_fold":
                if b1["buf"][:2] != [4, 3]:
                    problems.append("synthetic %s: unexpected OpISub result %s" % (name, b1["buf"][:2]))
                if any(op == 130 for op, _ in instructions(out_w)):
                    problems.append("synthetic %s: OpISub was not constant-folded away" % name)
            if name == "R3_oob_access_chain_index" and b1["buf"][0] != 101:
                problems.append("synthetic %s: unexpected dead-branch OpISub result %s" % (name, b1["buf"][0]))
            if name == "R4_hoist_var_with_opline" and b1["buf"][:2] != [303, 404]:
                problems.append("synthetic %s: unexpected hoisted-var result %s" % (name, b1["buf"][:2]))
            if name == "R5_forward_neg_access_chain" and b1["buf"][0] != 505:
                problems.append("synthetic %s: unexpected forwarded-neg result %s" % (name, b1["buf"][0]))
            if name == "R6_zero_index_access_chain" and b1["buf"][:2] != [4, 5]:
                problems.append("synthetic %s: unexpected zero-index alias result %s" % (name, b1["buf"][:2]))
            if name == "R9_decorated_unused_composite":
                if b1["buf"][:2] != [4, 5]:
                    problems.append("synthetic %s: unexpected result %s" % (name, b1["buf"][:2]))
                sized = {ins[1] for op, ins in instructions(out_w) if op == 71 and ins[2:4] == [11, 25]}
                if not sized or not sized <= {ins[2] for op, ins in instructions(out_w) if op == 44}:
                    problems.append("synthetic %s: the WorkgroupSize constant or its decoration was removed" % name)
            if name == "R10_refused_loop_constants" and b1["buf"][:4] != [777, 6, 999, 6]:
                problems.append("synthetic %s: unexpected result %s" % (name, b1["buf"][:4]))

        # --- a table the shader writes into stays
        written_bytes = (FIXTURES / "written.spv").read_bytes()
        valid_bytes(written_bytes, "tests/fsr4_tune/written.spv")
        wf, _, _ = run_stdin_tune(written_bytes, BASE)
        if wf["moved"][0] != "0":
            problems.append("a table the shader writes into was moved to the tail (it must stay)")

        # --- which entry points get 64 lanes
        want = {"fsr4_model_v07_fp8_no_scale_pass12": (1, 0), "fsr4_model_v07_fp8_no_scale_pass1_post": (0, 0),
                "fsr4_model_v07_fp8_no_scale_postpass": (0, 1), "fsr4_model_v07_fp8_no_scale_prepass": (0, 0),
                "fsr4_model_v07_fp8_no_scale_pass": (0, 0), "main": (0, 0)}
        for line in run_text("names", *want).splitlines():
            entry, w64, post = line.split()
            if (int(w64), int(post)) != want[entry]:
                problems.append("%s: 64 lanes %s, postpass %s; wanted %d, %d" % (entry, w64, post, *want[entry]))

        # --- which bindings take the tail in: (offset, range) -> (covers, widened range)
        size, tail, whole = 100 << 20, 64 << 10, (1 << 64) - 1
        cases = {(0, whole): (1, whole), (0, size + tail): (1, size + tail), (0, size + 2 * tail): (1, size + 2 * tail),
                 (0, size): (0, size + tail), (0, size - 4): (0, size - 4), (256, whole): (0, whole),
                 (256, size): (0, size), (0, size + tail - 4): (0, size + tail - 4)}
        args = [x for pair in cases for x in pair]
        for line in run_text("covers", size, tail, *args).splitlines():
            o, r, covers, widened = (int(x) for x in line.split())
            if (covers, widened) != cases[(o, r)]:
                problems.append("a binding at offset %d, range %d: covers the tail %d, widened to %d; wanted %d, %d" %
                                (o, r, covers, widened, *cases[(o, r)]))
            elif covers == 0 and o == 0 and widened != r:
                # (what is widened must then cover it)
                if not run_text("covers", size, tail, o, widened).split()[2] == "1":
                    problems.append("the widened range %d at offset 0 does not cover the tail" % widened)
    if problems:
        raise SystemExit("FAIL:\n  " + "\n  ".join(problems))
    print("PASS: FSR 4's weight tables move to the writable buffer's tail, a written table stays, layer & postpass "
          "loops unroll deterministically with bit-exact SPIR-V VM parity and valid SPIR-V (plus L1-L10 edge cases), "
          "the layers alone get 64 lanes, bindings are checked, and background builds stop at close")


if __name__ == "__main__":
    main()
