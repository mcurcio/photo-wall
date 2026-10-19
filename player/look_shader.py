"""The look in GLSL: the one shader copy of player/geometry.py's final steps, shared by the fit pass
(player/fit_pass.py) and the pattern pass (player/pattern_pass.py) (render redesign, run ledgers
.claude/runs/render-planes/s1-page.md and s3-page.md):
the Frame's paper under the room's light and the mat shadow (`irradiance`, `paper_reflectance`,
`paper_signal`), the paper profile's LUT, the fixed highlight rolloff, white balance, sRGB encode,
contrast, gamma, clamp and dither, each step as player/geometry.py states it.

Rules this module owns (one home each): the look's GLSL functions (`LOOK_GLSL`) and their uniforms
(`look_uniforms`), the LUT's texture unit and its upload (`LUT_UNIT`, `upload_lut`). Rules it does
NOT own: the formulas (player/geometry.py); where a pixel sits in its opening, and which pixel the
dither reads (each pass's own); the step sets (each pass compiles its programs, the LUT step a
constant `lut_on` of its program, so a step switched off costs a frame nothing).
"""
from __future__ import annotations

from typing import Any, Final

from contracts.frame_lighting import FRAME_LIGHTS_MAX, SHEET_WHITENESS
from contracts.lut import LUT_GRID
from contracts.node_frame import PictureOperations
from player.geometry import BAYER_4, HIGHLIGHT_KNEE, LIGHT_KINDS, UNSIZED_MAT, FrameLights

LUT_UNIT: Final = 1     # the LUT's texture unit; unit 0 is the picture being sampled

# player/geometry.py's and the contracts' constants, written in so each has one home.
_LOOK_CONSTANTS = (
    f"#define FRAME_LIGHTS_MAX {FRAME_LIGHTS_MAX}\n"
    + "".join(f"#define LIGHT_{kind.upper()} {code}\n" for code, kind in enumerate(LIGHT_KINDS))
    + f"const float LUT_GRID = {float(LUT_GRID)!r};\n"
    f"const float SHEET_WHITENESS = {SHEET_WHITENESS!r};\n"
    f"const float HIGHLIGHT_KNEE = {HIGHLIGHT_KNEE!r};\n"
    f"const float BAYER_4[16] = float[16]({', '.join(f'{float(rank)!r}' for rank in BAYER_4)});\n")

LOOK_GLSL: Final[str] = """
precision highp sampler3D;
""" + _LOOK_CONSTANTS + """
uniform vec3 rgb_multipliers;
uniform float contrast_slope;
uniform float gamma_exponent;
uniform float matched_lux;
uniform float panel_black;
uniform int output_height;
uniform sampler3D lut;
uniform vec2 mat_size;
uniform float mat_depth;
uniform float paper_white;
uniform float paper_black;
uniform float peak;
uniform int light_count;
uniform int light_kind[FRAME_LIGHTS_MAX];
uniform vec3 light_rgb[FRAME_LIGHTS_MAX];
uniform vec2 light_toward[FRAME_LIGHTS_MAX];
uniform vec2 light_band[FRAME_LIGHTS_MAX];
uniform vec3 light_position[FRAME_LIGHTS_MAX];
uniform vec2 light_beam[FRAME_LIGHTS_MAX];
vec3 srgb_encoded(vec3 value) {
    value = clamp(value, 0.0, 1.0);
    return mix(1.055*pow(value, vec3(1.0/2.4))-0.055, 12.92*value, lessThanEqual(value, vec3(0.0031308)));
}
vec3 srgb_decoded(vec3 value) {
    value = clamp(value, 0.0, 1.0);
    return mix(pow((value+0.055)/1.055, vec3(2.4)), value/12.92, lessThanEqual(value, vec3(0.04045)));
}
float smoothstep01(float t) {
    return t*t*(3.0-2.0*t);
}
// geometry.py `lut_applied`: the paper profile's LUT, fully; the grid's points sit at texel centres.
vec3 lut_applied(vec3 value) {
    vec3 encoded = srgb_encoded(value);
    return srgb_decoded(texture(lut, encoded*((LUT_GRID-1.0)/LUT_GRID)+0.5/LUT_GRID).rgb);
}
// geometry.py `_edge_distance`: from m (mat space, mm) to the opening's edge along `toward`.
float edge_distance(vec2 m, vec2 toward) {
    float reach = 3.0e38;
    if (toward.x > 0.0) reach = min(reach, (mat_size.x-m.x)/toward.x);
    else if (toward.x < 0.0) reach = min(reach, m.x/-toward.x);
    if (toward.y > 0.0) reach = min(reach, (mat_size.y-m.y)/toward.y);
    else if (toward.y < 0.0) reach = min(reach, m.y/-toward.y);
    return max(reach, 0.0);
}
// geometry.py `_close_shape`: the inverse square with the angle onto the wall, times the cone.
float close_shape(int j, vec2 m) {
    vec3 place = light_position[j];
    vec3 aim = vec3(mat_size*0.5-place.xy, -place.z);
    vec3 ray = vec3(m-place.xy, -place.z);
    float d0 = length(aim);
    float d = length(ray);
    float phi = acos(clamp(dot(aim, ray)/(d0*d), -1.0, 1.0));
    vec2 beam = light_beam[j];          // half the inner and outer beam, radians
    float cone;
    if (phi <= beam.x) cone = 1.0;
    else if (phi >= beam.y) cone = 0.0;
    else cone = smoothstep01((beam.y-phi)/(beam.y-beam.x));
    float r = d0/d;
    return r*r*r*cone;
}
// geometry.py `_reached`: the share of light j the mat lets reach m.
float reached(int j, vec2 m) {
    int kind = light_kind[j];
    if (kind == LIGHT_WASH) return 1.0;
    if (kind == LIGHT_FAR) {
        float s = edge_distance(m, light_toward[j]);
        vec2 band = light_band[j];      // (dark until, lit from), mm
        if (s >= band.y) return 1.0;
        if (s < band.x) return 0.0;
        return smoothstep01((s-band.x)/(band.y-band.x));
    }
    vec3 place = light_position[j];
    vec2 foot = place.xy-m;
    float h = length(foot);
    if (h == 0.0) return 1.0;
    return edge_distance(m, foot/h) >= mat_depth*h/place.z ? 1.0 : 0.0;
}
// geometry.py `irradiance`: E at a point of the opening's unit square, rgb lux, summed over the
// Frame's lights.
vec3 irradiance(vec2 opening_uv) {
    vec2 m = opening_uv*mat_size;
    vec3 total = vec3(0.0);
    for (int j = 0; j < FRAME_LIGHTS_MAX; j++) {
        if (j >= light_count) break;
        float shape = light_kind[j] == LIGHT_CLOSE ? close_shape(j, m) : 1.0;
        if (shape > 0.0) total += light_rgb[j]*(shape*reached(j, m));
    }
    return total;
}
// geometry.py `rolled_off` on a pixel's largest channel, through the shortfall, so the peak is
// exactly white.
float rolled_off(float value) {
    if (value <= HIGHLIGHT_KNEE) return value;
    float t = (min(value, peak)-HIGHLIGHT_KNEE)/(1.0-HIGHLIGHT_KNEE);
    float w = (peak-HIGHLIGHT_KNEE)/(1.0-HIGHLIGHT_KNEE);
    return 1.0-(1.0-HIGHLIGHT_KNEE)*(w-t)*(w+t)/(w*w*(1.0+t));
}
// geometry.py `highlight_rolled`: the whole pixel scaled by its largest channel's rolloff, so its
// hue stays.
vec3 highlight_rolled(vec3 signal) {
    float largest = max(signal.r, max(signal.g, signal.b));
    if (peak <= 1.0 || largest <= HIGHLIGHT_KNEE) return signal;
    return signal*(rolled_off(largest)/largest);
}
// geometry.py `final_pixel`'s photo steps: the paper profile's LUT (the program's `lut_on` step),
// then the photo on paper under the room's light (`paper_reflectance`, `paper_signal`,
// `highlight_rolled`), as a signal.
vec3 lit_signal(vec3 photo, vec2 opening_uv) {
    if (lut_on == 1) photo = lut_applied(photo);
    vec3 reflectance = paper_white*(paper_black+(1.0-paper_black)*photo);
    vec3 lit = reflectance*irradiance(opening_uv)/(SHEET_WHITENESS*matched_lux);
    return highlight_rolled(max((lit-panel_black)/(1.0-panel_black), 0.0));
}
// geometry.py `display_steps`, then `dithered` at `dither_at` (a pixel counted from the image's top).
vec3 displayed(vec3 signal, ivec2 dither_at) {
    vec3 contrasted = (srgb_encoded(signal*rgb_multipliers)-0.5)*contrast_slope+0.5;
    vec3 adjusted = clamp(pow(max(contrasted, 0.0), vec3(gamma_exponent)), 0.0, 1.0);
    ivec2 at = dither_at & 3;
    return clamp(adjusted+((BAYER_4[at.y*4+at.x]+0.5)/16.0-0.5)/255.0, 0.0, 1.0);
}
// A window's pixel counted from its top: GL draws a window bottom-up, `output_height` rows.
ivec2 window_pixel() {
    return ivec2(int(gl_FragCoord.x), output_height-1-int(gl_FragCoord.y));
}
// The look (geometry.py `final_pixel` inside the opening): a linear photo at `opening_uv` of the
// opening's unit square, as the encoded value to store, dithered at `dither_at`.
vec3 look(vec3 linear, vec2 opening_uv, ivec2 dither_at) {
    return displayed(lit_signal(linear, opening_uv), dither_at);
}
"""
"""GLSL ES 3.00 functions, no `main` and no `#version`, for a fragment shader that declares
`const int lut_on` (its step set) before them: `vec3 look(vec3 linear, vec2 opening_uv, ivec2
dither_at)`, its two halves `lit_signal` (the paper under the light) and `displayed` (the Display's
steps and the dither, which the frame pass's test patterns share), `window_pixel`, `srgb_decoded`,
and the uniforms `look_uniforms` sets."""


def look_uniforms(gl: Any, program: int, operations: PictureOperations, lights: FrameLights | None, *,
                  dither_height: int, lut_bound: bool) -> None:
    """Set `LOOK_GLSL`'s uniforms in `program` (current) for `operations` and the Frame's `lights`
    (None: no light, every pixel black as player/geometry.py `final_pixel` has it), the dither
    counting rows from the top of a `dither_height`-row image, the LUT step on when `lut_bound` (its
    texture bound on LUT_UNIT).

    `dither_height` is `window_pixel`'s; a pass whose rows run top-down passes its own pixel to
    `look`. `lut_bound` points the `lut` sampler at LUT_UNIT (never unit 0, where the picture's
    sampler of another type sits); the step itself is the program's `lut_on`, compiled true exactly
    when the LUT is bound, so with it false the sampler is unread and left unset."""
    def uniform(name: str) -> int:
        return gl.glGetUniformLocation(program, name)

    gl.glUniform3f(uniform("rgb_multipliers"), *operations.rgb_multipliers)
    gl.glUniform1f(uniform("contrast_slope"), operations.contrast_slope)
    gl.glUniform1f(uniform("gamma_exponent"), operations.gamma_exponent)
    gl.glUniform1f(uniform("matched_lux"), operations.light.matched_lux)
    gl.glUniform1f(uniform("panel_black"), operations.light.black)
    gl.glUniform1i(uniform("output_height"), dither_height)
    if lut_bound:
        gl.glUniform1i(uniform("lut"), LUT_UNIT)
    present = lights.lights if lights is not None else ()
    padded = present + (None,) * (FRAME_LIGHTS_MAX - len(present))
    gl.glUniform2f(uniform("mat_size"), *(lights.mat_size if lights is not None else UNSIZED_MAT))
    gl.glUniform1f(uniform("mat_depth"), lights.depth_mm if lights is not None else 0.0)
    gl.glUniform1f(uniform("paper_white"), lights.paper.whiteness if lights is not None else 1.0)
    gl.glUniform1f(uniform("paper_black"), 10 ** -lights.paper.ink_density if lights is not None else 0.0)
    gl.glUniform1f(uniform("peak"), lights.peak if lights is not None else 1.0)
    gl.glUniform1i(uniform("light_count"), len(present))
    gl.glUniform1iv(uniform("light_kind"), FRAME_LIGHTS_MAX,
                    [LIGHT_KINDS.index(light.kind) if light else 0 for light in padded])
    gl.glUniform3fv(uniform("light_rgb"), FRAME_LIGHTS_MAX,
                    [v for light in padded for v in (light.rgb if light else (0.0, 0.0, 0.0))])
    gl.glUniform2fv(uniform("light_toward"), FRAME_LIGHTS_MAX,
                    [v for light in padded for v in (light.toward if light else (0.0, 0.0))])
    gl.glUniform2fv(uniform("light_band"), FRAME_LIGHTS_MAX,
                    [v for light in padded for v in (light.band_mm if light else (0.0, 0.0))])
    gl.glUniform3fv(uniform("light_position"), FRAME_LIGHTS_MAX,
                    [v for light in padded for v in (light.position_mm if light else (0.0, 0.0, 1.0))])
    gl.glUniform2fv(uniform("light_beam"), FRAME_LIGHTS_MAX,
                    [v for light in padded for v in (light.beam if light else (0.0, 0.0))])


def upload_lut(gl: Any, texture: int, table: bytes) -> None:
    """Upload the paper profile's LUT `table` (contracts/lut.py's layout as served: red fastest,
    then green, then blue) into the GL_TEXTURE_3D `texture`, bound on LUT_UNIT, filtered as
    player/geometry.py `lut_sampled` reads it. Texture unit 0 is the active unit after."""
    gl.glActiveTexture(gl.GL_TEXTURE0 + LUT_UNIT)
    try:
        gl.glBindTexture(gl.GL_TEXTURE_3D, texture)
        for parameter, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR),
                                 (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                                 (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                                 (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE),
                                 (gl.GL_TEXTURE_WRAP_R, gl.GL_CLAMP_TO_EDGE)):
            gl.glTexParameteri(gl.GL_TEXTURE_3D, parameter, value)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)      # a row is 33 * 3 bytes
        gl.glTexImage3D(gl.GL_TEXTURE_3D, 0, gl.GL_RGB8, LUT_GRID, LUT_GRID, LUT_GRID, 0,
                        gl.GL_RGB, gl.GL_UNSIGNED_BYTE, table)
    finally:
        gl.glActiveTexture(gl.GL_TEXTURE0)


__all__ = ["LOOK_GLSL", "LUT_UNIT", "look_uniforms", "upload_lut"]
