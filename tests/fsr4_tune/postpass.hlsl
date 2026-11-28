// AMD FSR 4's postpass in miniature, for tests/test_fsr4_tune.py: a constant weight table copied into a Function array,
// an outer counted loop with `!=` and an out-of-bounds early continue, and an inner counted loop carrying live-out
// accumulators in registers (multi-OpPhi header). Reads from a separate input buffer so invocations never race on
// overlapping words. Built in this folder with:
//   dxc -spirv -T cs_6_0 -E fsr4_model_test_postpass -fspv-target-env=vulkan1.3 -Fo postpass.spv postpass.hlsl
RWByteAddressBuffer ScratchBuffer : register(u0);
ByteAddressBuffer InBuffer : register(t1);
cbuffer Params : register(b2) { uint width; };
static const uint W[8] = { 3u, 5u, 7u, 11u, 13u, 17u, 19u, 23u };
[numthreads(8, 8, 1)]
void fsr4_model_test_postpass(uint3 id : SV_DispatchThreadID) {
    [loop] for (uint y = 0u; y != 2u; y++) {
        uint x = id.x + y;
        if (x >= width) continue;
        uint sum0 = 0u;
        uint sum1 = 1u;
        [loop] for (uint i = 0u; i != 4u; i++) {
            uint v = InBuffer.Load((x + i) * 4u);
            sum0 += v * W[y * 4u + i];
            sum1 ^= v + W[i];
        }
        ScratchBuffer.Store((id.x * 2u + y) * 4u, sum0 + sum1);
    }
}
