// (longshot) Native matrix multiplies: OoT's networks (the movie upscaler, the gameplay TAAU) are HMMA.16816.F16
// programs, which the shuffle expansion in lower_to_maxwell.h runs at a fraction of the speed. With
// VK_KHR_cooperative_matrix offering 16x8x16 f16 on the subgroup, HMMA lowers to HMMA_nx2 and the SPIR-V back end
// emits one OpCooperativeMatrixMulAddKHR, moving each lane's fragment registers in and out of its elements.
//
// Which matrix positions a lane's elements are is the implementation's choice, so at start this measures it: a probe
// loads row-major matrices whose values are their positions and writes out every lane's elements in order. When each
// lane holds the same positions as the console's fragments do (PTX mma.m16n8k16's layout), only the order within the
// lane differs, and that order (mma_layout.h) is all the emitter needs. A multiply in the console's layout is then
// checked against the CPU's (exact: small integers). Anything else, or RX_VK_NATIVE_MMA=0, keeps the expansion.
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <string>
#include <vector>

#include "vk_internal.h"
#include "vk_draw.h"   // (extern_guarded_shader_module, extern_guarded_compute)
#include "shader_recompiler/backend/spirv/mma_layout.h"
#include "shader_recompiler/backend/spirv/mma_software.h"
#include <sirit/sirit.h>
#ifndef LONGSHOT_ENV_H   // (env_on: the switches' one rule)
#include "longshot_env.h"
#endif

namespace mkwvk {
namespace {

uint16_t to_half(float f) {   // (small integers only: exact)
    if (f == 0.0f) return 0;
    uint32_t x;
    std::memcpy(&x, &f, 4);
    const uint32_t sign = (x >> 16) & 0x8000;
    const int exp = (int)((x >> 23) & 0xff) - 127 + 15;
    return (uint16_t)(sign | (uint32_t)(exp << 10) | ((x >> 13) & 0x3ff));
}
float from_half(uint16_t h) {
    const uint32_t sign = (uint32_t)(h & 0x8000) << 16, exp = (h >> 10) & 0x1f, man = h & 0x3ff;
    uint32_t x;
    if (!exp && !man) x = sign;
    else if (!exp) { float f = std::ldexp((float)man, -24); return sign ? -f : f; }
    else if (exp == 31) x = sign | 0x7f800000 | (man << 13);
    else x = sign | ((exp - 15 + 127) << 23) | (man << 13);
    float f;
    std::memcpy(&f, &x, 4);
    return f;
}

bool wmma16_result_matches(float got, float want) {
    return std::isfinite(got) && std::fabs(got - want) <= 1e-4f;
}

// the console's fragment positions: lane t (g = t/4, i = t%4), element e (two a register, low half first)
void console_a(int t, int e, int& r, int& k) { const int g = t / 4, i = t % 4; r = g + ((e >> 1) & 1) * 8; k = 2 * i + (e & 1) + (e >> 2) * 8; }
void console_b(int t, int e, int& k, int& n) { const int g = t / 4, i = t % 4; k = 2 * i + (e & 1) + (e >> 1) * 8; n = g; }
void console_c(int t, int e, int& r, int& n) { const int g = t / 4, i = t % 4; r = g + (e >> 1) * 8; n = 2 * i + (e & 1); }

// a 32-lane compute shader over one storage buffer of u32 words, built by body(module, ...)
struct TestShader {
    Sirit::Module m{0x00010600};
    Sirit::Id u32, f16, f16x2, buf, u32_ptr, lane, ta, tb, tc, zero;
    std::vector<Sirit::Id> extra_interfaces;   // (variables the body adds: test_multiply_wmma16's workgroup tiles)
    Sirit::Id c32(uint32_t v) { return m.Constant(u32, v); }
    Sirit::Id word_ptr(Sirit::Id index) { return m.OpAccessChain(u32_ptr, buf, c32(0), index); }
    Sirit::Id word(Sirit::Id index) { return m.OpLoad(u32, word_ptr(index)); }
    void store(Sirit::Id index, Sirit::Id value) { m.OpStore(word_ptr(index), value); }
    Sirit::Id at(Sirit::Id base, uint32_t add) { return m.OpIAdd(u32, base, c32(add)); }
    Sirit::Id pack(Sirit::Id lo, Sirit::Id hi) { return m.OpBitcast(u32, m.OpCompositeConstruct(f16x2, lo, hi)); }
    Sirit::Id element(Sirit::Id matrix, uint32_t j) { return m.OpCompositeExtract(f16, matrix, j); }
};

std::vector<uint32_t> build_shader(const std::function<void(TestShader&)>& body) {
    using namespace Sirit;
    TestShader s;
    Module& m = s.m;
    m.AddCapability(spv::Capability::Shader);
    m.AddCapability(spv::Capability::Float16);
    m.AddCapability(spv::Capability::CooperativeMatrixKHR);
    m.AddExtension("SPV_KHR_cooperative_matrix");
    m.AddCapability(spv::Capability::VulkanMemoryModel);
    m.AddExtension("SPV_KHR_vulkan_memory_model");
    m.SetMemoryModel(spv::AddressingModel::Logical, spv::MemoryModel::VulkanKHR);
    const Id void_t = m.TypeVoid();
    s.u32 = m.TypeUInt(32);
    const Id uvec3 = m.TypeVector(s.u32, 3);
    s.f16 = m.TypeFloat(16);
    s.f16x2 = m.TypeVector(s.f16, 2);
    const Id runtime = m.TypeRuntimeArray(s.u32);
    m.Decorate(runtime, spv::Decoration::ArrayStride, 4U);
    const Id block = m.TypeStruct(runtime);
    m.Decorate(block, spv::Decoration::Block);
    m.MemberDecorate(block, 0U, spv::Decoration::Offset, 0U);
    s.buf = m.AddGlobalVariable(m.TypePointer(spv::StorageClass::StorageBuffer, block), spv::StorageClass::StorageBuffer);
    m.Decorate(s.buf, spv::Decoration::DescriptorSet, 0U);
    m.Decorate(s.buf, spv::Decoration::Binding, 0U);
    const Id lid = m.AddGlobalVariable(m.TypePointer(spv::StorageClass::Input, uvec3), spv::StorageClass::Input);
    m.Decorate(lid, spv::Decoration::BuiltIn, spv::BuiltIn::LocalInvocationId);
    s.u32_ptr = m.TypePointer(spv::StorageClass::StorageBuffer, s.u32);
    const Id scope = s.c32((uint32_t)spv::Scope::Subgroup);
    const auto mat = [&](uint32_t r, uint32_t c, spv::CooperativeMatrixUse use) {
        return m.TypeCooperativeMatrixKHR(s.f16, scope, s.c32(r), s.c32(c), s.c32((uint32_t)use));
    };
    s.ta = mat(16, 16, spv::CooperativeMatrixUse::MatrixAKHR);
    s.tb = mat(16, 8, spv::CooperativeMatrixUse::MatrixBKHR);
    s.tc = mat(16, 8, spv::CooperativeMatrixUse::MatrixAccumulatorKHR);
    s.zero = m.Constant(s.f16, 0U);
    const Id main = m.OpFunction(void_t, spv::FunctionControlMask::MaskNone, m.TypeFunction(void_t));
    m.AddLabel();
    s.lane = m.OpCompositeExtract(s.u32, m.OpLoad(uvec3, lid), 0U);
    body(s);
    m.OpReturn();
    m.OpFunctionEnd();
    std::vector<Id> interfaces{s.buf, lid};
    interfaces.insert(interfaces.end(), s.extra_interfaces.begin(), s.extra_interfaces.end());
    m.AddEntryPoint(spv::ExecutionModel::GLCompute, main, "main", interfaces);
    m.AddExecutionMode(main, spv::ExecutionMode::LocalSize, 32U, 1U, 1U);
    return m.Assemble();
}

// one dispatch of a 32-lane shader over words (in and out), groups of 32 lanes
bool run_shader(const std::vector<uint32_t>& code, std::vector<uint32_t>& data, uint32_t groups = 1,
                const char* what = "cooperative matrix test", bool full_subgroups = false) {
    const VkDeviceSize size = data.size() * 4;
    VkBufferCreateInfo bi{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    bi.size = size;
    bi.usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
    VkBuffer buffer{};
    if (vkCreateBuffer(dev.device, &bi, nullptr, &buffer) != VK_SUCCESS) return false;
    VkMemoryRequirements req;
    vkGetBufferMemoryRequirements(dev.device, buffer, &req);
    VkPhysicalDeviceMemoryProperties mp;
    vkGetPhysicalDeviceMemoryProperties(dev.physical, &mp);
    uint32_t type = UINT32_MAX;
    const VkMemoryPropertyFlags want = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
    for (uint32_t i = 0; i < mp.memoryTypeCount && type == UINT32_MAX; i++)
        if ((req.memoryTypeBits & (1u << i)) && (mp.memoryTypes[i].propertyFlags & want) == want) type = i;
    VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    ai.allocationSize = req.size;
    ai.memoryTypeIndex = type;
    VkDeviceMemory memory{};
    if (type == UINT32_MAX || vkAllocateMemory(dev.device, &ai, nullptr, &memory) != VK_SUCCESS) {
        vkDestroyBuffer(dev.device, buffer, nullptr);
        return false;
    }
    vkBindBufferMemory(dev.device, buffer, memory, 0);
    uint32_t* words = nullptr;
    if (vkMapMemory(dev.device, memory, 0, size, 0, (void**)&words) != VK_SUCCESS || !words) {
        logf("vk: cooperative matrix test: the buffer could not be mapped");
        vkDestroyBuffer(dev.device, buffer, nullptr);
        vkFreeMemory(dev.device, memory, nullptr);
        return false;
    }
    std::memcpy(words, data.data(), size);

    VkDescriptorSetLayoutBinding b{0, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1, VK_SHADER_STAGE_COMPUTE_BIT, nullptr};
    VkDescriptorSetLayoutCreateInfo dli{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO};
    dli.bindingCount = 1, dli.pBindings = &b;
    VkDescriptorSetLayout dsl{};
    vkCreateDescriptorSetLayout(dev.device, &dli, nullptr, &dsl);
    VkPipelineLayoutCreateInfo pli{VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO};
    pli.setLayoutCount = 1, pli.pSetLayouts = &dsl;
    VkPipelineLayout layout{};
    vkCreatePipelineLayout(dev.device, &pli, nullptr, &layout);
    VkShaderModuleCreateInfo smi{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};
    smi.codeSize = code.size() * 4, smi.pCode = code.data();
    VkShaderModule module{};
    VkComputePipelineCreateInfo cpi{VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO};
    VkPipelineShaderStageRequiredSubgroupSizeCreateInfo lanes{VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_REQUIRED_SUBGROUP_SIZE_CREATE_INFO};
    lanes.requiredSubgroupSize = 32;   // (as the programs it stands for will run: vk_main.cpp)
    VkPipeline pipeline{};
    VkResult pr;
    {
        IntelDriverGate gate;
        if (extern_guarded_shader_module(&smi, &module) != VK_SUCCESS) module = VK_NULL_HANDLE;
        cpi.stage = {VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, nullptr, 0, VK_SHADER_STAGE_COMPUTE_BIT, module, "main", nullptr};
        if (dev.sg32_stages & VK_SHADER_STAGE_COMPUTE_BIT) cpi.stage.pNext = &lanes;
        // (full_subgroups: as vk_draw.cpp pin_subgroup gives a pinned program whose workgroup is whole subgroups, the one a
        // test with workgroup memory tiles a subgroup stands for)
        if (full_subgroups && (dev.sg32_stages & VK_SHADER_STAGE_COMPUTE_BIT) && dev.vk13.computeFullSubgroups)
            cpi.stage.flags |= VK_PIPELINE_SHADER_STAGE_CREATE_REQUIRE_FULL_SUBGROUPS_BIT;
        cpi.layout = layout;
        pr = module ? extern_guarded_compute(VK_NULL_HANDLE, &cpi, &pipeline) : VK_ERROR_INITIALIZATION_FAILED;
    }
    bool ok = false;
    if (pr == VK_SUCCESS) {
        VkDescriptorPoolSize ps{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1};
        VkDescriptorPoolCreateInfo dpi{VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO};
        dpi.maxSets = 1, dpi.poolSizeCount = 1, dpi.pPoolSizes = &ps;
        VkDescriptorPool pool{};
        vkCreateDescriptorPool(dev.device, &dpi, nullptr, &pool);
        VkDescriptorSetAllocateInfo dsa{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
        dsa.descriptorPool = pool, dsa.descriptorSetCount = 1, dsa.pSetLayouts = &dsl;
        VkDescriptorSet set{};
        vkAllocateDescriptorSets(dev.device, &dsa, &set);
        VkDescriptorBufferInfo dbi{buffer, 0, size};
        VkWriteDescriptorSet wr{VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET};
        wr.dstSet = set, wr.descriptorCount = 1, wr.descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, wr.pBufferInfo = &dbi;
        vkUpdateDescriptorSets(dev.device, 1, &wr, 0, nullptr);
        VkCommandPoolCreateInfo cpci{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
        cpci.queueFamilyIndex = dev.queue_family;
        VkCommandPool cmd_pool{};
        vkCreateCommandPool(dev.device, &cpci, nullptr, &cmd_pool);
        VkCommandBufferAllocateInfo cba{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
        cba.commandPool = cmd_pool, cba.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, cba.commandBufferCount = 1;
        VkCommandBuffer cmd{};
        vkAllocateCommandBuffers(dev.device, &cba, &cmd);
        VkCommandBufferBeginInfo cbb{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
        cbb.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
        vkBeginCommandBuffer(cmd, &cbb);
        vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, pipeline);
        vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, layout, 0, 1, &set, 0, nullptr);
        vkCmdDispatch(cmd, groups, 1, 1);
        VkMemoryBarrier2 mb{VK_STRUCTURE_TYPE_MEMORY_BARRIER_2};
        mb.srcStageMask = VK_PIPELINE_STAGE_2_COMPUTE_SHADER_BIT, mb.srcAccessMask = VK_ACCESS_2_SHADER_WRITE_BIT;
        mb.dstStageMask = VK_PIPELINE_STAGE_2_HOST_BIT, mb.dstAccessMask = VK_ACCESS_2_HOST_READ_BIT;
        VkDependencyInfo di{VK_STRUCTURE_TYPE_DEPENDENCY_INFO};
        di.memoryBarrierCount = 1, di.pMemoryBarriers = &mb;
        vkCmdPipelineBarrier2(cmd, &di);
        vkEndCommandBuffer(cmd);
        VkFenceCreateInfo fci{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
        VkFence fence{};
        if (vkCreateFence(dev.device, &fci, nullptr, &fence) != VK_SUCCESS) fence = VK_NULL_HANDLE;   // (no fence: not submitted, so all is freed below)
        VkCommandBufferSubmitInfo csi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_SUBMIT_INFO};
        csi.commandBuffer = cmd;
        VkSubmitInfo2 si{VK_STRUCTURE_TYPE_SUBMIT_INFO_2};
        si.commandBufferInfoCount = 1, si.pCommandBufferInfos = &csi;
        const bool submitted = fence && vkQueueSubmit2(dev.queue, 1, &si, fence) == VK_SUCCESS;
        ok = submitted && vkWaitForFences(dev.device, 1, &fence, VK_TRUE, 5000000000ull) == VK_SUCCESS;
        if (ok) std::memcpy(data.data(), words, size);
        else logf("vk: %s: the dispatch did not complete", what);
        if (submitted && !ok) {
            if (module) vkDestroyShaderModule(dev.device, module, nullptr);
            return false;
        }
        vkDestroyFence(dev.device, fence, nullptr);
        vkDestroyCommandPool(dev.device, cmd_pool, nullptr);
        vkDestroyDescriptorPool(dev.device, pool, nullptr);
        vkDestroyPipeline(dev.device, pipeline, nullptr);
    } else {
        logf("vk: %s: pipeline creation failed (%d)", what, (int)pr);
    }
    if (module) vkDestroyShaderModule(dev.device, module, nullptr);
    vkDestroyPipelineLayout(dev.device, layout, nullptr);
    vkDestroyDescriptorSetLayout(dev.device, dsl, nullptr);
    vkDestroyBuffer(dev.device, buffer, nullptr);
    vkFreeMemory(dev.device, memory, nullptr);
    return ok;
}

// The probe: A (16x16), B (16x8) and C (16x8) loaded row major from words 0, 128 and 192, each element's value its
// position (row * columns + column); lane t writes its 8 A elements at 256 + 4t, its 4 B at 384 + 2t, its 4 C at
// 448 + 2t. Then each console element is found among the device's of the same lane: one order for all lanes or none.
bool measure_order() {
    std::vector<uint32_t> data(512, 0);
    const auto put = [&](uint32_t base, int rows, int cols) {
        for (int r = 0; r < rows; r++)
            for (int c = 0; c < cols; c += 2)
                data[base + (r * cols + c) / 2] = (uint32_t)to_half((float)(r * cols + c)) |
                                                  ((uint32_t)to_half((float)(r * cols + c + 1)) << 16);
    };
    put(0, 16, 16), put(128, 16, 8), put(192, 16, 8);
    const auto code = build_shader([](TestShader& s) {
        auto& m = s.m;
        const auto load = [&](Sirit::Id type, uint32_t base, uint32_t stride_words) {
            return m.OpCooperativeMatrixLoadKHR(type, s.word_ptr(s.c32(base)), s.c32(0) /* RowMajorKHR */,
                                                s.c32(stride_words));
        };
        const Sirit::Id a = load(s.ta, 0, 8), b = load(s.tb, 128, 4), c = load(s.tc, 192, 4);
        const auto out = [&](Sirit::Id matrix, uint32_t base, uint32_t words) {
            const Sirit::Id at = m.OpIAdd(s.u32, s.c32(base), m.OpIMul(s.u32, s.lane, s.c32(words)));
            for (uint32_t w = 0; w < words; w++) s.store(s.at(at, w), s.pack(s.element(matrix, 2 * w), s.element(matrix, 2 * w + 1)));
        };
        out(a, 256, 4), out(b, 384, 2), out(c, 448, 2);
    });
    if (!run_shader(code, data)) return false;
    const auto dev_value = [&](uint32_t base, uint32_t words, int t, int j) {
        const uint32_t w = data[base + t * words + j / 2];
        return (int)from_half((uint16_t)(j & 1 ? w >> 16 : w & 0xffff));
    };
    const auto find = [&](const char* name, uint32_t base, int count, int cols, auto console, auto& order) {
        for (int e = 0; e < count; e++) {
            int found = -1;
            for (int t = 0; t < 32; t++) {
                int r, c;
                console(t, e, r, c);
                int j = 0;
                while (j < count && dev_value(base, count / 2, t, j) != r * cols + c) j++;
                if (j == count || (found >= 0 && j != found)) {
                    std::string row;
                    for (int k = 0; k < count; k++) row += " " + std::to_string(dev_value(base, count / 2, t, k));
                    logf("vk: cooperative matrix %s: lane %d holds%s, the console's element %d is (%d, %d): not the same "
                         "positions in every lane", name, t, row.c_str(), e, r, c);
                    return false;
                }
                found = j;
            }
            order[e] = (uint32_t)found;
        }
        std::string s;
        for (int e = 0; e < count; e++) s += " " + std::to_string(order[e]);
        logf("vk: cooperative matrix %s: the console's elements are the device's%s", name, s.c_str());
        return true;
    };
    using namespace Shader::Backend::SPIRV;
    return find("A", 256, 8, 16, console_a, mma_order_a) && find("B", 384, 4, 8, console_b, mma_order_b) &&
           find("C", 448, 4, 8, console_c, mma_order_c);
}

// test values (small integers, every product and sum exact in f16)
float a_val(int r, int k) { return (float)(((r * 7 + k * 3 + 1) % 5) - 2); }
float b_val(int k, int n) { return (float)(((k * 5 + n * 11 + 2) % 4) - 1); }
float c_val(int r, int n) { return (float)(((r * 3 + n * 13) % 9) - 4); }

// The multiply as HMMA_nx2 emits it: lane t's fragment words at 8t (A 4, B 2, C 2), D's 2 out at 256 + 2t
bool test_multiply() {
    using namespace Shader::Backend::SPIRV;
    std::vector<uint32_t> data(320, 0xffffffffu);
    const auto pack = [](float lo, float hi) { return (uint32_t)to_half(lo) | ((uint32_t)to_half(hi) << 16); };
    for (int t = 0; t < 32; t++) {
        int r0, c0, r1, c1;
        for (int e = 0; e < 8; e += 2) console_a(t, e, r0, c0), console_a(t, e + 1, r1, c1), data[t * 8 + e / 2] = pack(a_val(r0, c0), a_val(r1, c1));
        for (int e = 0; e < 4; e += 2) console_b(t, e, r0, c0), console_b(t, e + 1, r1, c1), data[t * 8 + 4 + e / 2] = pack(b_val(r0, c0), b_val(r1, c1));
        for (int e = 0; e < 4; e += 2) console_c(t, e, r0, c0), console_c(t, e + 1, r1, c1), data[t * 8 + 6 + e / 2] = pack(c_val(r0, c0), c_val(r1, c1));
    }
    const auto code = build_shader([](TestShader& s) {
        auto& m = s.m;
        const Sirit::Id base = m.OpIMul(s.u32, s.lane, s.c32(8));
        const auto load = [&](Sirit::Id type, uint32_t first, uint32_t count, const auto& order) {
            Sirit::Id mx = m.OpCompositeConstruct(type, s.zero);
            for (uint32_t w = 0; w < count; w++) {
                const Sirit::Id pair = m.OpBitcast(s.f16x2, s.word(s.at(base, first + w)));
                for (uint32_t h = 0; h < 2; h++) mx = m.OpCompositeInsert(type, m.OpCompositeExtract(s.f16, pair, h), mx, order[w * 2 + h]);
            }
            return mx;
        };
        const Sirit::Id d = m.OpCooperativeMatrixMulAddKHR(s.tc, load(s.ta, 0, 4, mma_order_a), load(s.tb, 4, 2, mma_order_b),
                                                            load(s.tc, 6, 2, mma_order_c));
        const Sirit::Id out = m.OpIAdd(s.u32, s.c32(256), m.OpIMul(s.u32, s.lane, s.c32(2)));
        for (uint32_t w = 0; w < 2; w++)
            s.store(s.at(out, w), s.pack(s.element(d, mma_order_c[w * 2]), s.element(d, mma_order_c[w * 2 + 1])));
    });
    if (!run_shader(code, data)) return false;
    int bad = 0;
    for (int t = 0; t < 32; t++)
        for (int e = 0; e < 4; e++) {
            int r, n;
            console_c(t, e, r, n);
            float want_d = c_val(r, n);
            for (int k = 0; k < 16; k++) want_d += a_val(r, k) * b_val(k, n);
            const uint32_t w = data[256 + t * 2 + e / 2];
            const float got = from_half((uint16_t)(e & 1 ? w >> 16 : w & 0xffff));
            if (got != want_d && bad++ < 4)
                logf("vk: cooperative matrix test: lane %d element %d (D[%d][%d]) %g, the CPU %g", t, e, r, n, got, want_d);
        }
    if (bad) logf("vk: cooperative matrix test: %d of 128 elements differ", bad);
    return bad == 0;
}

// (AMD RDNA 3/3.5/4) full 16x16 N-paired multiply as DefineWmma16PairFunction (emit_spirv_warp.cpp) emits it, for one
// subgroup: lane t's fragment words at 12t (A 4, B0 2, B1 2, C0 2, C1 2), D0/D1 (4 words) out at 384 + 4t, verifying all 256 elements
bool test_multiply_wmma16() {
    using namespace Shader::Backend::SPIRV;
    std::vector<uint32_t> data(512, 0xffffffffu);
    const auto pack = [](float lo, float hi) { return (uint32_t)to_half(lo) | ((uint32_t)to_half(hi) << 16); };
    for (int t = 0; t < 32; t++) {
        int r0, c0, r1, c1;
        for (int e = 0; e < 8; e += 2) console_a(t, e, r0, c0), console_a(t, e + 1, r1, c1), data[t * 12 + e / 2] = pack(a_val(r0, c0), a_val(r1, c1));
        for (int e = 0; e < 4; e += 2) console_b(t, e, r0, c0), console_b(t, e + 1, r1, c1), data[t * 12 + 4 + e / 2] = pack(b_val(r0, c0), b_val(r1, c1));
        for (int e = 0; e < 4; e += 2) console_b(t, e, r0, c0), console_b(t, e + 1, r1, c1), data[t * 12 + 6 + e / 2] = pack(b_val(r0, c0 + 8), b_val(r1, c1 + 8));
        for (int e = 0; e < 4; e += 2) console_c(t, e, r0, c0), console_c(t, e + 1, r1, c1), data[t * 12 + 8 + e / 2] = pack(c_val(r0, c0), c_val(r1, c1));
        for (int e = 0; e < 4; e += 2) console_c(t, e, r0, c0), console_c(t, e + 1, r1, c1), data[t * 12 + 10 + e / 2] = pack(c_val(r0, c0 + 8), c_val(r1, c1 + 8));
    }
    const auto code = build_shader([](TestShader& s) {
        auto& m = s.m;
        const auto c = [&](uint32_t v) { return s.c32(v); };
        const Sirit::Id scope = c((uint32_t)spv::Scope::Subgroup);
        const auto mat = [&](spv::CooperativeMatrixUse use) {
            return m.TypeCooperativeMatrixKHR(s.f16, scope, c(16), c(16), c((uint32_t)use));
        };
        const Sirit::Id ta = mat(spv::CooperativeMatrixUse::MatrixAKHR), tb = mat(spv::CooperativeMatrixUse::MatrixBKHR),
                        tc = mat(spv::CooperativeMatrixUse::MatrixAccumulatorKHR);
        const Sirit::Id wptr = m.TypePointer(spv::StorageClass::Workgroup, s.u32);
        const Sirit::Id tiles = m.AddGlobalVariable(
            m.TypePointer(spv::StorageClass::Workgroup, m.TypeArray(s.u32, c(mma_wmma16_tile_words))),
            spv::StorageClass::Workgroup);
        s.extra_interfaces.push_back(tiles);
        const auto at = [&](Sirit::Id index) { return m.OpAccessChain(wptr, tiles, index); };
        const auto put = [&](Sirit::Id index, Sirit::Id value) { m.OpStore(at(index), value, spv::MemoryAccessMask::NonPrivatePointer); };
        const auto get = [&](Sirit::Id index) { return m.OpLoad(s.u32, at(index), spv::MemoryAccessMask::NonPrivatePointer); };
        const auto barrier = [&] {
            m.OpControlBarrier(scope, scope, c((uint32_t)(spv::MemorySemanticsMask::AcquireRelease | spv::MemorySemanticsMask::WorkgroupMemory |
                                                          spv::MemorySemanticsMask::MakeAvailable | spv::MemorySemanticsMask::MakeVisible)));
        };
        const Sirit::Id in = m.OpIMul(s.u32, s.lane, c(12));
        const Sirit::Id g = m.OpShiftRightLogical(s.u32, s.lane, c(2)), i = m.OpBitwiseAnd(s.u32, s.lane, c(3));
        const Sirit::Id gi = m.OpIAdd(s.u32, m.OpShiftLeftLogical(s.u32, g, c(3)), i);
        const auto word = [&](uint32_t region, uint32_t rows, uint32_t words) { return s.at(gi, region * 128 + rows * 8 + words); };
        const Sirit::Id zero = c(0);
        const bool fast = mma_wmma16_fast;
        put(word(0, 0, 0), s.word(s.at(in, 0))), put(word(0, 8, 0), s.word(s.at(in, 1)));
        put(word(0, 0, 4), s.word(s.at(in, 2))), put(word(0, 8, 4), s.word(s.at(in, 3)));
        put(word(1, 0, 0), s.word(s.at(in, 4))), put(word(1, 0, 4), fast ? s.word(s.at(in, 5)) : zero);
        put(word(1, 8, 0), s.word(s.at(in, 6))), put(word(1, 8, 4), fast ? s.word(s.at(in, 7)) : zero);
        if (!fast) {
            put(word(2, 0, 0), zero), put(word(2, 0, 4), s.word(s.at(in, 5)));
            put(word(2, 8, 0), zero), put(word(2, 8, 4), s.word(s.at(in, 7)));
        }
        put(word(3, 0, 0), s.word(s.at(in, 8))), put(word(3, 8, 0), s.word(s.at(in, 9)));
        put(word(3, 0, 4), s.word(s.at(in, 10))), put(word(3, 8, 4), s.word(s.at(in, 11)));
        barrier();
        const auto tile = [&](Sirit::Id type, uint32_t region, uint32_t layout) {
            return m.OpCooperativeMatrixLoadKHR(type, at(c(region * 128)), c(layout), c(8), spv::MemoryAccessMask::NonPrivatePointer);
        };
        const Sirit::Id a = tile(ta, 0, 0);
        Sirit::Id d = m.OpCooperativeMatrixMulAddKHR(tc, a, tile(tb, 1, 1), tile(tc, 3, 0));
        if (!fast) d = m.OpCooperativeMatrixMulAddKHR(tc, a, tile(tb, 2, 1), d);
        barrier();
        m.OpCooperativeMatrixStoreKHR(at(c(3 * 128)), d, c(0), c(8), spv::MemoryAccessMask::NonPrivatePointer);
        barrier();
        const Sirit::Id out = m.OpIAdd(s.u32, c(384), m.OpIMul(s.u32, s.lane, c(4)));
        s.store(s.at(out, 0), get(word(3, 0, 0)));
        s.store(s.at(out, 1), get(word(3, 8, 0)));
        s.store(s.at(out, 2), get(word(3, 0, 4)));
        s.store(s.at(out, 3), get(word(3, 8, 4)));
    });
    if (!run_shader(code, data)) return false;
    int bad = 0;
    for (int t = 0; t < 32; t++)
        for (int half_idx = 0; half_idx < 2; half_idx++)
            for (int e = 0; e < 4; e++) {
                int r, n;
                console_c(t, e, r, n);
                n += half_idx * 8;
                float want_d = c_val(r, n);
                for (int k = 0; k < 16; k++) want_d += a_val(r, k) * b_val(k, n);
                const uint32_t w = data[384 + t * 4 + half_idx * 2 + e / 2];
                const float got = from_half((uint16_t)(e & 1 ? w >> 16 : w & 0xffff));
                // (RDNA 3/3.5 gfx11 v_wmma_f16 can leave a <=2^-22 f16 subnormal residual on exact cancellation to 0;
                // any layout or tile bug is off by >= 1.0 on these integer inputs, so 1e-4 catches every wrong lane)
                if (!wmma16_result_matches(got, want_d) && bad++ < 4)
                    logf("vk: WMMA 16x16x16 test: lane %d element %d (D[%d][%d]) %g, the CPU %g", t, e, r, n, got, want_d);
            }
    if (bad) logf("vk: WMMA 16x16x16 test: %d of 256 elements differ", bad);
    return bad == 0;
}

} // namespace

// (vk_main.cpp, once the device is made: dev.coop_matrix says the extension and its feature are on)
namespace {
// ---- (longshot) The software multiplies' form (mma_software.h). The unrolled form is used only where, on this device, it gives every
// word the loop gives: a 32-lane shader multiplies kSoftwareTestLanes lanes' fragments once in each form and the words are compared.
// The fragments are f16 values of every kind (normal at several scales, subnormal, zero, sums that overflow), and every eighth warp
// is built so that a K group's exact sum lies just off an f16 tie that its f32 rounding lands on, the remainder coming in the
// group's last FMA (K 7 and 15: the one a conversion would be folded into): 3 * 683 + 2^-8 * (+-2^-8) = 2049 +- 2^-16 (f32: 2049,
// a tie between 2048 and 2050), 48 * 1365 + 2^-8 * (+-2^-8) = 65520 +- 2^-16 (a tie between 65504 and infinity). A driver that
// rounds such a sum to f16 once, folding the conversion into the FMA, gives the other neighbour for 2049 + 2^-16 and 65520 -
// 2^-16, and the loop is kept. (The test module has none of the game programs' float-control execution modes: it stands for
// them while those leave f16 denormals alone, profile support_fp16_denorm_* unset, as now.) Every eighth warp from the fourth is
// built for the Dot2 form, whose pair dot product rounds two products and their sum once where the loop rounds twice (see
// software_test_input). And B through workgroup memory is tried in 128-lane workgroups, four subgroups a tile each, as the
// game's networks have them, against the same form's words through shuffles.
constexpr uint32_t kSoftwareTestLanes = 32 * 256;
constexpr uint32_t kSoftwareStagedLanes = 128;   // (a staged test workgroup: four 32-lane subgroups)

/// lane gid (workgroup * 32 + lane): D of words [8 gid, 8 gid + 8) (A 4, B 2, C 2) through the form's function, to words
/// 8 * kSoftwareTestLanes + 2 gid. staged: B through a workgroup memory tile per subgroup (mma_software.h SoftwareMmaStagedB), in
/// workgroups of kSoftwareStagedLanes, laid out as emit_spirv_warp.cpp DefineSoftwareMatrixFunction lays them (a plain array)
std::vector<uint32_t> software_test_shader(Shader::Backend::SPIRV::SoftwareMmaForm form, bool quad, bool staged = false) {
    using namespace Sirit;
    using Shader::Backend::SPIRV::DefineSoftwareMma;
    Module m{0x00010600};
    // (f16 only in the forms that convert to it: the others run where the device has no f16 shaders)
    const bool dot2 = form == Shader::Backend::SPIRV::SoftwareMmaForm::Dot2;
    const bool f16 = form == Shader::Backend::SPIRV::SoftwareMmaForm::UnrolledF16 || dot2;
    m.AddCapability(spv::Capability::Shader);
    if (f16) m.AddCapability(spv::Capability::Float16);
    if (dot2) {
        m.AddExtension(Shader::Backend::SPIRV::kMixedFloatDotProductExtension);
        m.AddCapability(Shader::Backend::SPIRV::kDotProductFloat16AccFloat32VALVE);
    }
    m.AddCapability(spv::Capability::GroupNonUniform);
    m.AddCapability(spv::Capability::GroupNonUniformShuffle);
    if (quad) m.AddCapability(spv::Capability::GroupNonUniformQuad);
    m.SetMemoryModel(spv::AddressingModel::Logical, spv::MemoryModel::GLSL450);
    const Id void_t = m.TypeVoid(), u32 = m.TypeUInt(32), f32 = m.TypeFloat(32);
    const Id uvec3 = m.TypeVector(u32, 3);
    const Id f16_t = f16 ? m.TypeFloat(16) : Id{};
    const Shader::Backend::SPIRV::SoftwareMmaTypes types{u32, m.TypeVector(u32, 2), m.TypeVector(u32, 4), f32, m.TypeVector(f32, 2),
                                                         f16_t, m.TypeBool(), f16 ? m.TypeVector(f16_t, 2) : Id{}};
    const Id runtime = m.TypeRuntimeArray(u32);
    m.Decorate(runtime, spv::Decoration::ArrayStride, 4U);
    const Id block = m.TypeStruct(runtime);
    m.Decorate(block, spv::Decoration::Block);
    m.MemberDecorate(block, 0U, spv::Decoration::Offset, 0U);
    const Id buf = m.AddGlobalVariable(m.TypePointer(spv::StorageClass::StorageBuffer, block), spv::StorageClass::StorageBuffer);
    m.Decorate(buf, spv::Decoration::DescriptorSet, 0U);
    m.Decorate(buf, spv::Decoration::Binding, 0U);
    const Id gid_var = m.AddGlobalVariable(m.TypePointer(spv::StorageClass::Input, uvec3), spv::StorageClass::Input);
    m.Decorate(gid_var, spv::Decoration::BuiltIn, spv::BuiltIn::GlobalInvocationId);
    const Id lane_var = m.AddGlobalVariable(m.TypePointer(spv::StorageClass::Input, u32), spv::StorageClass::Input);
    m.Decorate(lane_var, spv::Decoration::BuiltIn, spv::BuiltIn::SubgroupLocalInvocationId);
    const Id u32_ptr = m.TypePointer(spv::StorageClass::StorageBuffer, u32);
    Shader::Backend::SPIRV::SoftwareMmaStagedB tiles{};
    if (staged) {   // (no stride, as DefineSoftwareMatrixFunction's)
        const Id array =
            m.TypeArray(types.u32x4, m.Constant(u32, kSoftwareStagedLanes / 32 * Shader::Backend::SPIRV::kSoftwareMmaTileVectors));
        tiles.tile = m.AddGlobalVariable(m.TypePointer(spv::StorageClass::Workgroup, array), spv::StorageClass::Workgroup);
        tiles.subgroup_id = m.AddGlobalVariable(m.TypePointer(spv::StorageClass::Input, u32), spv::StorageClass::Input);
        m.Decorate(tiles.subgroup_id, spv::Decoration::BuiltIn, spv::BuiltIn::SubgroupId);
        tiles.u32_pointer = m.TypePointer(spv::StorageClass::Workgroup, u32);
        tiles.u32x4_pointer = m.TypePointer(spv::StorageClass::Workgroup, types.u32x4);
    }
    const Id fn = DefineSoftwareMma(m, types, lane_var, form, quad, staged ? &tiles : nullptr);
    const Id main = m.OpFunction(void_t, spv::FunctionControlMask::MaskNone, m.TypeFunction(void_t));
    m.AddLabel();
    const auto c32 = [&](uint32_t v) { return m.Constant(u32, v); };
    const Id gid = m.OpCompositeExtract(u32, m.OpLoad(uvec3, gid_var), 0U);
    const auto word = [&](Id base, uint32_t n) { return m.OpAccessChain(u32_ptr, buf, c32(0), m.OpIAdd(u32, base, c32(n))); };
    const Id in = m.OpIMul(u32, gid, c32(8));
    std::array<Id, 8> w;
    for (uint32_t k = 0; k < 8; k++) w[k] = m.OpLoad(u32, word(in, k));
    const Id d = m.OpFunctionCall(types.u32x2, fn, m.OpCompositeConstruct(types.u32x4, w[0], w[1], w[2], w[3]),
                            m.OpCompositeConstruct(types.u32x2, w[4], w[5]), m.OpCompositeConstruct(types.u32x2, w[6], w[7]));
    const Id out = m.OpIAdd(u32, c32(8 * kSoftwareTestLanes), m.OpIMul(u32, gid, c32(2)));
    for (uint32_t k = 0; k < 2; k++) m.OpStore(word(out, k), m.OpCompositeExtract(u32, d, k));
    m.OpReturn();
    m.OpFunctionEnd();
    std::vector<Id> interfaces{buf, gid_var, lane_var};
    if (staged) interfaces.insert(interfaces.end(), {tiles.tile, tiles.subgroup_id});
    m.AddEntryPoint(spv::ExecutionModel::GLCompute, main, "main", interfaces);
    m.AddExecutionMode(main, spv::ExecutionMode::LocalSize, staged ? kSoftwareStagedLanes : 32U, 1U, 1U);
    return m.Assemble();
}

/// The fragments the forms are compared on (fixed: the same every start)
std::vector<uint32_t> software_test_input() {
    std::vector<uint32_t> data(10 * kSoftwareTestLanes, 0);
    uint32_t x = 0x9e3779b9u;
    const auto next = [&x] { x ^= x << 13, x ^= x >> 17, x ^= x << 5; return x; };
    const auto half = [&]() -> uint32_t {
        const uint32_t k = next() % 16, sign = next() & 0x8000, man = next() & 0x3ff;
        if (k < 9) return sign | ((10 + next() % 10) << 10) | man;   // (normal, 2^-5 to 2^4: activations and weights)
        if (k < 11) return sign | man;                                // (subnormal or zero)
        if (k < 13) return sign | ((25 + next() % 6) << 10) | man;    // (large: sums overflow)
        if (k < 14) return sign | ((1 + next() % 4) << 10) | man;     // (tiny normal)
        return sign | ((next() % 31) << 10) | man;                    // (any finite)
    };
    for (uint32_t i = 0; i < 8 * kSoftwareTestLanes; i++) data[i] = half() | half() << 16;
    for (uint32_t warp = 0; warp < kSoftwareTestLanes / 32; warp += 8) {
        // (A[r][6] = a, A[r][7] = 2^-8; B[6][n] = b, B[7][n] = +-2^-8; all else 0: lanes 4g + 3 hold k 6 and 7 of rows g, g + 8 and
        // of column g)
        const uint32_t kind = warp / 8 % 4, a = kind < 2 ? 0x4200 /* 3 */ : 0x5200 /* 48 */,
                       b = kind < 2 ? 0x6156 /* 683 */ : 0x6555 /* 1365 */, small = kind % 2 ? 0x9c00 : 0x1c00;   // (+-2^-8)
        for (uint32_t t = 0; t < 32; t++) {
            uint32_t* lane = &data[(warp * 32 + t) * 8];
            for (uint32_t k = 0; k < 8; k++) lane[k] = 0;
            if (t % 4 != 3) continue;
            lane[0] = lane[1] = a | 0x1c00u << 16;   // (rows g and g + 8: k 6 and 7)
            lane[4] = b | small << 16;               // (column g: k 6 and 7)
        }
    }
    for (uint32_t warp = 4; warp < kSoftwareTestLanes / 32; warp += 8) {
        // (for the Dot2 form: k 4 gives x = a4 * b4, k 6 and 7 the products p and q, x + p an f32 tie that q, far smaller, decides
        // when the pair is rounded once. 2049 + 2^-13 + 2^-20 is f16 2050 rounded once, 2048 rounded twice (2049 + 2^-13 -> 2049,
        // then an f16 tie to even). Kind 1 has p and q the other way round; kind 2 is 2051 - 2^-13 - 2^-20 (once 2050, twice 2052))
        const uint32_t kind = warp / 8 % 3;
        const uint32_t a4 = kind < 2 ? 0x4200 /* 3 */ : 0x4700 /* 7 */, b4 = kind < 2 ? 0x6156 /* 683 */ : 0x5c94 /* 293 */;
        // (A's k 6 and 7, B's k 6 and 7: 2^-7 * 2^-6 = 2^-13, 2^-10 * 2^-10 = 2^-20)
        const uint32_t a67 = kind == 0 ? 0x2000 | 0x1400u << 16 : kind == 1 ? 0x1400 | 0x2000u << 16 : 0xa000 | 0x9400u << 16;
        const uint32_t b67 = kind == 0 ? 0x2400 | 0x1400u << 16 : kind == 1 ? 0x1400 | 0x2400u << 16 : 0x2400 | 0x1400u << 16;
        for (uint32_t t = 0; t < 32; t++) {
            uint32_t* lane = &data[(warp * 32 + t) * 8];
            for (uint32_t k = 0; k < 8; k++) lane[k] = 0;
            if (t % 4 == 2) lane[0] = lane[1] = a4, lane[4] = b4;     // (k 4 of rows g and g + 8, and of column g)
            if (t % 4 == 3) lane[0] = lane[1] = a67, lane[4] = b67;   // (k 6 and 7)
        }
    }
    return data;
}

constexpr size_t kSoftwareFormDidNotRun = ~size_t(0);

/// How a form's words differ from the ones it is compared with: differ, the words that are not the same (any NaN is a NaN); off,
/// the words with a half more than 2 f16 ulps off, of the other sign, a NaN against a number or an infinity against a finite
/// value (0x7bff and 0x7c00 are one apart in bits, not in value): what a lane or layout mistake gives, not a rounding. Both
/// kSoftwareFormDidNotRun when the device could not run the form
struct SoftwareWords {
    size_t differ = kSoftwareFormDidNotRun, off = kSoftwareFormDidNotRun;
    bool ran() const { return differ != kSoftwareFormDidNotRun; }
};

/// The form's words for the test's fragments (in data, past the fragments: software_test_shader), or false when the device could
/// not run it
bool software_form_words(Shader::Backend::SPIRV::SoftwareMmaForm form, bool quad, bool staged, std::vector<uint32_t>& data) {
    data = software_test_input();
    return run_shader(software_test_shader(form, quad, staged), data,
                      kSoftwareTestLanes / (staged ? kSoftwareStagedLanes : 32), "software matrix test", staged);
}

SoftwareWords software_words_compare(const std::vector<uint32_t>& got, const std::vector<uint32_t>& want) {
    const auto nan_as_one = [](uint32_t h) { return (h & 0x7c00) == 0x7c00 && (h & 0x3ff) ? 0x7e00u : h; };
    // (finite f16 bits of one sign are in the order of their values: ulps apart is the bits' difference; +0 and -0 count as one.
    // An infinity or a NaN is near only its own kind: 65504 is one bit from infinity)
    const auto within = [&](uint32_t a, uint32_t b) {
        a = nan_as_one(a), b = nan_as_one(b);
        if ((a & 0x7fff) == 0 && (b & 0x7fff) == 0) return true;
        if ((a ^ b) & 0x8000 || ((a & 0x7c00) == 0x7c00) != ((b & 0x7c00) == 0x7c00)) return false;
        if ((a & 0x7c00) == 0x7c00) return a == b;
        return (a > b ? a - b : b - a) <= 2;
    };
    SoftwareWords r;
    r.differ = r.off = 0;
    for (uint32_t i = 0; i < 2 * kSoftwareTestLanes; i++) {
        const uint32_t g = got[8 * kSoftwareTestLanes + i], w = want[8 * kSoftwareTestLanes + i];
        r.differ += nan_as_one(g & 0xffff) != nan_as_one(w & 0xffff) || nan_as_one(g >> 16) != nan_as_one(w >> 16);
        r.off += !within(g & 0xffff, w & 0xffff) || !within(g >> 16, w >> 16);
    }
    return r;
}

/// RX_VK_MMA_DOT2 (the extension is asked for in vk_main.cpp the same way, where the unrolled forms are tried): unset or 1, the
/// Dot2 form is tried where the device has the f16 pair dot product and used where no word of the self-test is far from the
/// loop's (SoftwareWords::off 0: each within 2 f16 ulps); 0, not tried
bool software_dot2_wanted(bool mixed_dot_f16, const char* env) {
    return mixed_dot_f16 && (!env || !env_value_off(env));
}

/// RX_VK_MMA_STAGE_B: 0, B through shuffles; 1, through workgroup memory on any GPU whose compute subgroups are pinned to 32 lanes;
/// unset, on RADV, where the shuffles are ds_bpermute_b32 through the LDS anyway (the Steam Deck). Used only where the self-test
/// gives the same words as through shuffles
bool software_stage_b_wanted(VkDriverId driver, bool compute_pinned_32, const char* env) {
    return compute_pinned_32 && (env ? !env_value_off(env) : driver == VK_DRIVER_ID_MESA_RADV);
}

/// RX_VK_MMA_UNROLL=0: the loop; 1: the unrolled forms tried on any GPU; unset: on RADV and on AMD RDNA 4 (is_rdna4)
/// with AMD's proprietary driver, where they were measured (the Steam Deck and an RX 9060 XT)
bool software_unroll_wanted(VkDriverId driver, bool rdna4, const char* env) {
    const bool def = driver == VK_DRIVER_ID_MESA_RADV || (rdna4 && driver == VK_DRIVER_ID_AMD_PROPRIETARY);
    return env ? !env_value_off(env) : def;
}

/// (mma_software.h SoftwareMmaStagedB) B through workgroup memory for the chosen form, where it gives that form's own words
/// (words, through shuffles) in 128-lane workgroups; else the shuffles stay. Only with full compute subgroups
/// (computeFullSubgroups, as wmma16_init): a subgroup with lanes missing would have a SubgroupId past the workgroup's tiles and
/// lanes missing at its barriers. Sets mma_software_staged_b and mma_software_full_subgroups, which choose_software_form has
/// turned off
void choose_staged_b(Shader::Backend::SPIRV::SoftwareMmaForm form, bool quad, const std::vector<uint32_t>& words) {
    namespace spirv = Shader::Backend::SPIRV;
    const char* env = std::getenv("RX_VK_MMA_STAGE_B");
    const bool pinned = dev.sg32_stages & VK_SHADER_STAGE_COMPUTE_BIT;
    if (!software_stage_b_wanted(dev.driver_id, pinned, env)) {
        logf("vk: software matrix multiplies: B through shuffles (%s)", env && env_value_off(env) ? "RX_VK_MMA_STAGE_B=0"
                                                                       : !pinned ? "compute subgroups not pinned to 32 lanes"
                                                                                 : "not RADV, RX_VK_MMA_STAGE_B=1 tries workgroup memory");
        return;
    }
    if (!dev.vk13.computeFullSubgroups) {   // (vk_draw.cpp pin_subgroup asks REQUIRE_FULL_SUBGROUPS only with it)
        logf("vk: software matrix multiplies: B through shuffles (no full compute subgroups)");
        return;
    }
    std::vector<uint32_t> data;
    if (!software_form_words(form, quad, true, data)) {
        logf("vk: software matrix multiplies: B through workgroup memory: its test did not run: B through shuffles");
        return;
    }
    const SoftwareWords r = software_words_compare(data, words);
    if (r.differ) {
        logf("vk: software matrix multiplies: B through workgroup memory gave %zu of %u words other than through shuffles: "
             "B through shuffles", r.differ, 2 * kSoftwareTestLanes);
        return;
    }
    spirv::mma_software_staged_b = dev.props.limits.maxComputeSharedMemorySize;
    spirv::mma_software_full_subgroups = true;
    logf("vk: software matrix multiplies: B through workgroup memory, 256 bytes a subgroup: the shuffles' words for %u lanes "
         "(RX_VK_MMA_STAGE_B=0: through shuffles)", kSoftwareTestLanes);
}

void choose_software_form() {
    using Shader::Backend::SPIRV::SoftwareMmaForm;
    namespace spirv = Shader::Backend::SPIRV;
    spirv::mma_software_form = (uint32_t)SoftwareMmaForm::Loop;
    spirv::mma_software_staged_b = 0;
    spirv::mma_software_full_subgroups = false;
    const char* env = std::getenv("RX_VK_MMA_UNROLL");
    if (!software_unroll_wanted(dev.driver_id, is_rdna4(dev.props.vendorID, dev.props.deviceID, dev.props.deviceName), env)) {
        logf("vk: software matrix multiplies looped (%s)",
             env ? "RX_VK_MMA_UNROLL=0" : "not RADV or RDNA 4 on AMD's proprietary driver, RX_VK_MMA_UNROLL=1 tries the unrolled form");
        return;
    }
    // (A through quad broadcasts where every stage has quad operations, as the translator may emit the function in any stage that
    // has subgroup operations; shuffles to the same lanes otherwise)
    VkPhysicalDeviceSubgroupProperties sg{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SUBGROUP_PROPERTIES};
    VkPhysicalDeviceProperties2 p2{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2};
    p2.pNext = &sg;
    vkGetPhysicalDeviceProperties2(dev.physical, &p2);
    const bool quad = (sg.supportedOperations & VK_SUBGROUP_FEATURE_QUAD_BIT) && (sg.supportedStages & VK_SHADER_STAGE_COMPUTE_BIT) &&
                      sg.quadOperationsInAllStages;
    std::vector<uint32_t> loop;
    if (!software_form_words(SoftwareMmaForm::Loop, false, false, loop)) {
        logf("vk: software matrix multiplies looped (the loop's test did not run)");
        return;
    }
    const char* dot2_env = std::getenv("RX_VK_MMA_DOT2");
    for (const SoftwareMmaForm form : {SoftwareMmaForm::Dot2, SoftwareMmaForm::UnrolledF16, SoftwareMmaForm::Unrolled}) {
        const char* name = form == SoftwareMmaForm::Dot2          ? "unrolled, f16 pair dot products, f16 rounding by conversion"
                           : form == SoftwareMmaForm::UnrolledF16 ? "unrolled, f16 rounding by conversion"
                                                                  : "unrolled, f16 rounding in integers";
        // (the f16 forms' conversions need f16 shaders: RADV gives Polaris, RX 400/500, none)
        if (form != SoftwareMmaForm::Unrolled && !dev.vk12.shaderFloat16) {
            logf("vk: software matrix multiplies: %s not tried (the GPU has no f16 shaders)", name);
            continue;
        }
        if (form == SoftwareMmaForm::Dot2 && !software_dot2_wanted(dev.mixed_dot_f16, dot2_env)) {
            // (vk_main.cpp asks for the extension only with Vulkan headers that have it, 1.4.345 on)
#ifdef VK_VALVE_shader_mixed_float_dot_product
            const char* missing = "no f16 pair dot product into f32 on this GPU";
#else
            const char* missing = "this build's Vulkan headers have no f16 pair dot product";
#endif
            logf("vk: software matrix multiplies: %s not tried (%s)", name,
                 dot2_env && env_value_off(dot2_env) ? "RX_VK_MMA_DOT2=0" : missing);
            continue;
        }
        std::vector<uint32_t> words;
        const SoftwareWords r = software_form_words(form, quad, false, words) ? software_words_compare(words, loop) : SoftwareWords{};
        // (Dot2 rounds a K pair once, the loop twice: on these fragments its words are within 2 f16 ulps of the loop's, where a
        // lane or layout mistake is off by whole values)
        const bool within_rounding = form == SoftwareMmaForm::Dot2 && r.off == 0;
        if (r.ran() && (r.differ == 0 || within_rounding)) {
            spirv::mma_software_form = (uint32_t)form;
            spirv::mma_software_quad = quad;
            char how[160];
            if (r.differ == 0)
                snprintf(how, sizeof how, "the loop's words for %u lanes", kSoftwareTestLanes);
            else
                snprintf(how, sizeof how, "%zu of %u words other than the loop's, all within 2 f16 ulps (RX_VK_MMA_DOT2=0: not tried)",
                         r.differ, 2 * kSoftwareTestLanes);
            logf("vk: software matrix multiplies %s%s: %s (RX_VK_MMA_UNROLL=0: looped)", name,
                 quad ? ", A through quad broadcasts" : "", how);
            choose_staged_b(form, quad, words);
            return;
        }
        if (!r.ran())
            logf("vk: software matrix multiplies: %s: its test did not run: not used", name);
        else
            logf("vk: software matrix multiplies: %s gave %zu of %u words other than the loop's (%zu more than 2 f16 ulps off): not used",
                 name, r.differ, 2 * kSoftwareTestLanes, r.off);
    }
    logf("vk: software matrix multiplies looped");
}

/// Without cooperative matrices (a GTX 1650, an RTX 2060 on some drivers, AMD and Intel cards): the same multiplies in a shader function on
/// subgroup shuffles. The lowering refuses a matrix multiply it has no way to run, and the game's upscaling networks were then left out:
/// the 3D scene came out black or as coloured noise, with the HUD over it (Longshot issues 10 and 14)
void software_multiplies(const char* why) {
    Shader::Backend::SPIRV::mma_software = true;
    dev.native_mma = true;
    logf("vk: matrix multiplies in software (%s)", why);
    choose_software_form();
}
/// (longshot, mma_layout.h global_table_loop) Intel's Mesa driver only for now (Mesa inlines DontInline; Windows not tried); env is
/// RX_VK_GM_LOOP, 0 or 1 over the default. A table indexed by the loop's counter needs dynamic indexing of storage buffer arrays
/// (tests/test_global_table_loop.py)
bool table_loop_wanted(VkDriverId driver, bool dynamic_indexing, const char* env) {
    return (env ? !env_value_off(env) : driver == VK_DRIVER_ID_INTEL_OPEN_SOURCE_MESA) && dynamic_indexing;
}
bool mesh_pages_wanted(VkDriverId driver, const char* env) {
    return env ? !env_value_off(env) : driver == VK_DRIVER_ID_INTEL_OPEN_SOURCE_MESA;
}
/// (longshot, AMD RDNA 3/4) cooperative matrices of 16x16x16 only: HMMA_nx2 through workgroup tiles (emit_spirv_warp.cpp
/// DefineWmma16MatrixFunction).
void wmma16_init() {
    using namespace Shader::Backend::SPIRV;
    if (const char* e = std::getenv("RX_VK_MMA_WMMA16"); e && env_value_off(e)) {
        software_multiplies("RX_VK_MMA_WMMA16=0");
        return;
    }
    if (dev.props.vendorID != 0x1002 || dev.policy.nan_bits || !(dev.facts.rdna3() || dev.facts.rdna4())) {
        software_multiplies("WMMA 16x16x16 requires an AMD RDNA 3/4 GPU");
        return;
    }
    if (!dev.vk12.shaderFloat16) {
        software_multiplies("no f16 shaders");
        return;
    }
    if (!(dev.sg32_stages & VK_SHADER_STAGE_COMPUTE_BIT)) {   // (the tiles are per 32-lane subgroup)
        software_multiplies("cooperative matrices of 16x16x16 only, and no 32-lane compute subgroups");
        return;
    }
    if (!dev.vk13.computeFullSubgroups) {   // (a subgroup with lanes missing would run the tiles: vk_draw.cpp pin_subgroup)
        software_multiplies("cooperative matrices of 16x16x16 only, and no full compute subgroups");
        return;
    }
    mma_wmma16_full_subgroups = true;
    mma_wmma16_max_subgroups = dev.max_workgroup_subgroups;
    mma_wmma16_fast = env_on("RX_VK_MMA_WMMA16_FAST");
    const char* fuse_env = std::getenv("RX_VK_MMA_WMMA16_FUSE");
    mma_wmma16_fused = !fuse_env || !env_value_off(fuse_env);
    mma_wmma16_shared_limit = dev.props.limits.maxComputeSharedMemorySize;
    if (!test_multiply_wmma16()) {
        mma_wmma16_fast = false;
        mma_wmma16_fused = false;
        software_multiplies("the 16x16x16 cooperative matrix test failed");
        return;
    }
    mma_wmma16 = true;
    dev.native_mma = true;
    logf("vk: native matrix multiplies on (WMMA 16x16x16 through workgroup tiles, %s, %s)",
         mma_wmma16_fused ? "fused" : "unfused", mma_wmma16_fast ? "fast K=16" : "exact K=8");
}
/// (longshot, mma_layout.h graphics_table_loads) 1 by default, RX_VK_GP_TABLE's 0, 1 or 2 over it (anything else 0); 0 where the
/// table cannot hold the pools: the guest's memory not imported, or no device addresses (tests/test_mesh_fallback_pages.py)
int table_loads_wanted(const char* env, bool host_import, bool device_addresses) {
    int v = env ? std::atoi(env) : 1;
    if (v != 1 && v != 2) v = 0;
    return host_import && device_addresses ? v : 0;
}
/// (longshot, mma_layout.h mesh_fallback_shared_warp) the policy's default (device_policy.h mesh_shared_warp: Mesa's Intel
/// driver, and AMD's Windows driver on GPUs whose compute stages run only wider than 32 lanes, GCN); env is RX_VK_MESH_SHARED_WARP, unset or empty the
/// default, an off word off, anything else on (tests/test_mesh_fallback_shared_warp.py, tests/test_gcn_mesh_shared_warp.py)
bool shared_warp_wanted(bool by_policy, const char* env) {
    return env && *env ? !env_value_off(env) : by_policy;
}
} // namespace

bool software_dot2_extension_wanted(VkDriverId driver, const VkPhysicalDeviceProperties& props) {
    return software_unroll_wanted(driver, is_rdna4(props.vendorID, props.deviceID, props.deviceName), std::getenv("RX_VK_MMA_UNROLL")) &&
           software_dot2_wanted(true, std::getenv("RX_VK_MMA_DOT2"));
}

void coopmat_init() {
    // (amd-fix) NaN guard on float colour outputs (mma_layout.h frag_nan_guard), on every card since v2.0.6 as NX2navi's patch 0027
    // has it: the neon plants were reported on NVIDIA cards too, and one NaN texel spreads through bloom and blur. RX_VK_NAN_GUARD=0:
    // off, with vk_shader.cpp's GuardFragmentOutputs (one switch for both guards; it replaces RX_VK_NO_NAN_GUARD)
    {
        const char* e = std::getenv("RX_VK_NAN_GUARD");
        Shader::Backend::SPIRV::frag_nan_guard = !e || std::atoi(e) != 0;
    }
    if (Shader::Backend::SPIRV::frag_nan_guard) logf("vk: colour outputs NaN-guarded");
    // (device_policy.h position_invariant: every vendor but NVIDIA, unless RX_VK_NO_INVARIANT is on)
    Shader::Backend::SPIRV::position_invariant = dev.policy.position_invariant;
    if (Shader::Backend::SPIRV::position_invariant) logf("vk: positions invariant (not an NVIDIA card)");
    Shader::Backend::SPIRV::global_table_loop = table_loop_wanted(dev.driver_id, dev.features.shaderStorageBufferArrayDynamicIndexing,
                                                                  std::getenv("RX_VK_GM_LOOP"));
    if (Shader::Backend::SPIRV::global_table_loop) logf("vk: storage buffer tables searched in loops (RX_VK_GM_LOOP=0: written out)");
    {   // (mma_layout.h graphics_table_loads: on where the table can hold the pools, the guest's memory imported with device addresses;
        // elsewhere it stays empty and every load through it would read 0)
        const char* e = std::getenv("RX_VK_GP_TABLE");
        const int v = table_loads_wanted(e, dev.external_memory_host, dev.facts.buffer_device_address);
        if (!v && table_loads_wanted(e, true, true))
            logf("vk: global loads search the storage buffers: the guest page table needs host memory import and buffer device "
                 "addresses (RX_VK_GP_TABLE not taken)");
        Shader::Backend::SPIRV::graphics_table_loads = v;
    }
    if (Shader::Backend::SPIRV::graphics_table_loads == 1)
        logf("vk: vertex, fragment, compute, task and mesh stages' global loads read the guest page table alone (RX_VK_GP_TABLE=0: "
             "they search the storage buffers)");
    else if (Shader::Backend::SPIRV::graphics_table_loads == 2)
        logf("vk: vertex, fragment, compute, task and mesh stages' global loads checked against the guest page table (RX_VK_GP_TABLE=2)");
    Shader::Backend::SPIRV::mesh_fallback_pages = mesh_pages_wanted(dev.driver_id, std::getenv("RX_VK_MESH_PAGES"));
    if (Shader::Backend::SPIRV::mesh_fallback_pages)
        logf("vk: the mesh fallback's global loads read the guest page table (RX_VK_MESH_PAGES=0: they search the storage buffers)");
    Shader::Backend::SPIRV::mesh_fallback_global_attributes = mesh_pages_wanted(dev.driver_id, std::getenv("RX_VK_MESH_GLOBAL_ATTRS"));
    if (Shader::Backend::SPIRV::mesh_fallback_global_attributes)
        logf("vk: the mesh fallback's attribute regions in a pool of the records buffer (RX_VK_MESH_GLOBAL_ATTRS=0: in workgroup memory)");
    Shader::Backend::SPIRV::mesh_fallback_shared_warp = shared_warp_wanted(dev.policy.mesh_shared_warp, std::getenv("RX_VK_MESH_SHARED_WARP"));
    logf("vk: mesh_shared_warp=%d (effective, RX_VK_MESH_SHARED_WARP over the policy default)",
         Shader::Backend::SPIRV::mesh_fallback_shared_warp ? 1 : 0);
    Shader::Backend::SPIRV::mesh_fallback_shared_warp_check =
        Shader::Backend::SPIRV::mesh_fallback_shared_warp && env_on("RX_VK_MESH_SHARED_WARP_CHECK");
    if (Shader::Backend::SPIRV::mesh_fallback_shared_warp)
        logf("vk: the mesh fallback's warps in workgroup memory, its stages at any lane count (RX_VK_MESH_SHARED_WARP=0: the subgroup's)%s",
             Shader::Backend::SPIRV::mesh_fallback_shared_warp_check ? "; checked against the subgroup's (RX_VK_MESH_SHARED_WARP_CHECK)" : "");
    // (longshot) the translation's vendor fixes (device_policy.h fetch_bounds, nan_bits): out-of-bounds texel fetches answer 0,
    // NaN tests on bits
    Shader::Backend::SPIRV::fetch_bounds = dev.policy.fetch_bounds;
    // (dev.features.robustBufferAccess: on exactly when robustBufferAccess2 is, vk_main.cpp create_device)
    Shader::Backend::SPIRV::fetch_bounds_buffers = dev.policy.fetch_bounds && !dev.features.robustBufferAccess;
    Shader::Backend::SPIRV::nan_bits = dev.policy.nan_bits;
    Shader::Backend::SPIRV::mesh_fallback_strict_fmul = dev.policy.mesh_fallback_strict_fmul;
    // (longshot) RX_VK_MMA_SOFTWARE=1: HMMA_nx2 in a shader function on subgroup shuffles, no cooperative matrices
    // (emit_spirv_warp.cpp, from Lichii): the lowering still gives HMMA_nx2 (dev.native_mma)
    if (const char* sw = std::getenv("RX_VK_MMA_SOFTWARE"); sw && std::atoi(sw)) {
        software_multiplies("subgroup shuffles, RX_VK_MMA_SOFTWARE");
        return;
    }
    const char* env = std::getenv("RX_VK_NATIVE_MMA");
    if (env && !std::atoi(env)) {
        logf("vk: native matrix multiplies off (RX_VK_NATIVE_MMA=0)");
        return;
    }
    if (!dev.coop_matrix || !vkGetPhysicalDeviceCooperativeMatrixPropertiesKHR || env_on("RX_VK_MMA_NO_COOP")) {
        software_multiplies("no cooperative matrices");
        return;
    }
    uint32_t n = 0;
    vkGetPhysicalDeviceCooperativeMatrixPropertiesKHR(dev.physical, &n, nullptr);
    std::vector<VkCooperativeMatrixPropertiesKHR> props(n, VkCooperativeMatrixPropertiesKHR{VK_STRUCTURE_TYPE_COOPERATIVE_MATRIX_PROPERTIES_KHR});
    vkGetPhysicalDeviceCooperativeMatrixPropertiesKHR(dev.physical, &n, props.data());
    bool shape = false, shape16 = false;
    for (const auto& p : props) {
        const bool f16 = p.AType == VK_COMPONENT_TYPE_FLOAT16_KHR && p.BType == VK_COMPONENT_TYPE_FLOAT16_KHR &&
                         p.CType == VK_COMPONENT_TYPE_FLOAT16_KHR && p.ResultType == VK_COMPONENT_TYPE_FLOAT16_KHR &&
                         p.scope == VK_SCOPE_SUBGROUP_KHR;
        shape |= f16 && p.MSize == 16 && p.NSize == 8 && p.KSize == 16;
        shape16 |= f16 && p.MSize == 16 && p.NSize == 16 && p.KSize == 16;
    }
    if (!shape && shape16) {   // (AMD RDNA 3/4: WMMA 16x16x16 only)
        wmma16_init();
        return;
    }
    if (!shape) {
        software_multiplies("the cooperative matrices lack 16x8x16 f16");
        return;
    }
    if (!dev.vk12.shaderFloat16) {
        software_multiplies("no f16 shaders");
        return;
    }
    bool ok = measure_order() && test_multiply();
    // (longshot, diagnosis) RX_VK_MMA_A_ORDER / _B_ORDER / _C_ORDER=<n,n,...>: the element orders given instead of the
    // measured ones (as if the console's fragments were laid out otherwise)
    const auto override_order = [&](const char* name, auto& order) {
        const char* e = std::getenv(name);
        if (!e) return;
        for (size_t k = 0; k < order.size() && *e; ++k) {
            order[k] = (uint32_t)std::strtoul(e, nullptr, 10);
            while (*e && *e != ',') e++;
            if (*e) e++;
        }
        ok = true;
        logf("vk: %s given: the cooperative matrix test is skipped", name);
    };
    override_order("RX_VK_MMA_A_ORDER", Shader::Backend::SPIRV::mma_order_a);
    override_order("RX_VK_MMA_B_ORDER", Shader::Backend::SPIRV::mma_order_b);
    override_order("RX_VK_MMA_C_ORDER", Shader::Backend::SPIRV::mma_order_c);
    if (!ok) {
        software_multiplies("the cooperative matrix layout is not the console's");
        return;
    }
    dev.native_mma = true;
    logf("vk: native matrix multiplies on (cooperative matrices, the console's fragment layout checked)");
}

} // namespace mkwvk
