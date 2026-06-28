//  Shaders.metal — M6 motion render path.
//
//  Fullscreen-triangle vertex stage + a YCbCr->RGB fragment stage for the
//  decoded NV12 frames coming out of VideoToolbox.
//
//  COLOR CONTRACT (see CLAUDE.md / mirror-milestone-plan §6, §9):
//  The decoded planes are NV12, full-range, BT.709 matrix, BT.709 primaries,
//  sRGB transfer. We convert YCbCr->R'G'B' with the matrix supplied by the host
//  side (driven from the CVPixelBuffer color attachments — see DisplayRenderer),
//  and we DO NOT linearize: the output stays sRGB-encoded R'G'B'. The renderer
//  writes those values verbatim into a .bgra8Unorm drawable (NOT _srgb) whose
//  CAMetalLayer.colorspace is tagged sRGB, so Core Animation color-manages
//  sRGB -> Display-P3 for the panel. Tagging the layer Display-P3 here while
//  writing sRGB values would double-saturate — do not do it.

#include <metal_stdlib>
using namespace metal;

// Matches `ColorConversion` in DisplayRenderer.swift (simd_float3x3 + simd_float3).
//   rgb = matrix * (ycbcr - offset)
// `matrix` already folds in the range scaling (full vs video) and the BT.709
// (or 601 fallback) coefficients; `offset` is the black-level / chroma-centre
// subtraction (e.g. (0, 0.5, 0.5) for full range).
struct ColorConversion {
    float3x3 matrix;
    float3   offset;
};

// Matches `CleanAperture` in DisplayRenderer.swift (two float2 => 16 B). Maps the
// displayed [0,1] UV onto the coded texture's clean sub-rect so coded padding
// (e.g. 1088 coded rows vs 1080 displayed) is never sampled: uv = uv*scale+offset.
struct CleanAperture {
    float2 scale;
    float2 offset;
};

struct VertexOut {
    float4 position [[position]];
    float2 texCoord;
};

// Single oversized triangle covering the whole screen: vertex ids 0,1,2 ->
// clip (-1,-1), (3,-1), (-1,3) (the on-screen portion is the full [-1,1]^2).
//
// `scale` (buffer 0) is the per-axis fraction of the screen the source occupies
// (1.0 on the un-letterboxed axis, <1.0 on the barred axis). We bake the
// letterbox into texCoord rather than the position, mapping the centred image
// rect to [0,1] and pushing the bars to texCoord <0 or >1 (drawn black by the
// fragment). texCoord uses the Metal top-left origin (v flipped so row 0 is on
// top).
vertex VertexOut fullscreenVertex(uint vid [[vertex_id]],
                                  constant float2 &scale [[buffer(0)]]) {
    float2 base = float2((vid << 1) & 2, vid & 2);   // (0,0) (2,0) (0,2)
    float2 clip = base * 2.0 - 1.0;                   // (-1,-1) (3,-1) (-1,3)

    VertexOut out;
    out.position = float4(clip, 0.0, 1.0);
    // ndc/scale recentres so the image fills [-scale, scale] and bars fall outside.
    out.texCoord = float2((clip.x / scale.x + 1.0) * 0.5,
                          (1.0 - clip.y / scale.y) * 0.5);
    return out;
}

fragment float4 yuvFragment(VertexOut in [[stage_in]],
                            texture2d<float> lumaTex   [[texture(0)]],
                            texture2d<float> chromaTex [[texture(1)]],
                            constant ColorConversion &cc [[buffer(0)]],
                            constant CleanAperture &ca [[buffer(1)]]) {
    // Letterbox bars: anything outside the image rect is black.
    float2 uv = in.texCoord;
    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) {
        return float4(0.0, 0.0, 0.0, 1.0);
    }

    // Honor the clean aperture: remap the displayed [0,1] range onto the coded
    // texture's clean sub-rect so the coded padding rows/cols are never sampled.
    uv = uv * ca.scale + ca.offset;

    constexpr sampler s(filter::linear, mag_filter::linear, address::clamp_to_edge);
    float  y    = lumaTex.sample(s, uv).r;     // .r8Unorm  -> Y'
    float2 cbcr = chromaTex.sample(s, uv).rg;  // .rg8Unorm -> Cb,Cr

    float3 ycbcr = float3(y, cbcr.x, cbcr.y);
    float3 rgb   = cc.matrix * (ycbcr - cc.offset);     // sRGB-encoded R'G'B'
    return float4(clamp(rgb, 0.0, 1.0), 1.0);
}
