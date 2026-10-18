/* App-private client ABI. MIT. Call only from the GTK owning render thread. */
#pragma once
#include <wayland-client.h>
#include <stdint.h>
struct pw_frame_client;
struct pw_frame_client *pw_frame_client_create(struct wl_display *display);
/* Call inside the successful draw's GTK paint cycle before GDK commits that
 * very buffer. This queues metadata/feedback; it never commits a surface. */
int pw_frame_client_tag_next_commit(struct pw_frame_client *client,
    struct wl_surface *surface, const char *grant_id, const char *frame_tag);
int pw_frame_client_dispatch(struct pw_frame_client *client);
void pw_frame_client_destroy(struct pw_frame_client *client);
/* Returns 1 for current grant, 0 when absent/revoked, -1 for transport failure.
 * grant_id points to caller-owned storage of at least 37 bytes. */
int pw_frame_client_grant(struct pw_frame_client *client, const char *output_id,
    char *grant_id, char *frame_id, uint64_t *binding_generation, uint64_t *config_revision,
    uint32_t *state);

/* Select active or pending revision by the actual composition identity. */
int pw_frame_client_grant_revision(struct pw_frame_client *client, const char *output_id, const char *frame_id,
    uint64_t binding_generation, uint64_t config_revision, char *grant_id, uint32_t *state);

int pw_frame_client_trial(struct pw_frame_client *client, const char *output_id,
    const char *grant_id, char *candidate, uint32_t capacity);
int pw_frame_client_overlay(struct pw_frame_client *client, struct wl_surface *surface,
    const char *grant_id, const char *frame_tag, const char *primitives);

/* Frame protocol v4 (roadmap 1c; run ledger .claude/runs/display-1c.md, slices P1 and R2).
 * Copies the newest `picture` operations for output_id into caller-owned storage of at least
 * `capacity` bytes (513 holds the largest). Returns 1 when one was received, 0 when none yet,
 * -1 for transport failure or a client bound below version 4. */
int pw_frame_client_picture(struct pw_frame_client *client, const char *output_id,
    char *operations, uint32_t capacity);
/* tag_next_commit plus the picture change the buffer was drawn with ("0": none yet). */
int pw_frame_client_tag_picture_next_commit(struct pw_frame_client *client,
    struct wl_surface *surface, const char *grant_id, const char *frame_tag, const char *picture_change);
/* Pacing (player/pacing.py). The sequence of the newest tagged commit (0: none); each tag takes the
 * next. */
uint64_t pw_frame_client_tagged(struct pw_frame_client *client);
/* The compositor's newest word for `surface`'s tagged commits: returns 1 presented, 2 discarded,
 * 0 none yet, -1 transport failure. `sequence` names the tag it answers; `at_ns` is when it turned
 * to light on CLOCK_MONOTONIC (-1: discarded, or the compositor's clock is another); `refresh_ns`
 * the Output's refresh period (0: unknown). Dispatches the client's queue first. */
int pw_frame_client_presentation(struct pw_frame_client *client, struct wl_surface *surface,
    uint64_t *sequence, int64_t *at_ns, uint32_t *refresh_ns);

/* The Player's screens (render redesign, run ledger .claude/runs/render-planes/s1-page.md, bead B3).
 * The client on its own connection, no GDK: every function below and above works on a client
 * made by pw_frame_client_connect, from the one thread that owns it.
 *
 * A screen is one Output's app surface: an xdg toplevel whose app id is "photo-wall-<output_id>"
 * (the shell maps it full screen on that Output), showing opaque black (a single-pixel buffer the
 * viewport stretches over the configured size), parent of up to PW_SCREEN_PLANES synchronized
 * sub-surfaces, one per plane, each with a wp_alpha_modifier_v1 surface. A picture is a LINEAR
 * dmabuf imported through zwp_linux_dmabuf_v1. */
#define PW_SCREEN_PLANES 15     /* each screen's black takes the 16th plane (bench T2) */
struct pw_screen;
struct pw_picture;
/* One plane of a present: its picture, its top-left in the screen's surface coordinates (pixels as
 * the screen hangs) and its strength as the wp_alpha_modifier_v1 multiplier (1..UINT32_MAX). */
struct pw_plane_state {
    struct pw_picture *picture;
    int32_t x, y;
    uint32_t alpha;
};
/* Connect to `socket` (NULL: $WAYLAND_DISPLAY) and bind the frame manager (v4), xdg_wm_base,
 * wl_compositor, wl_subcompositor, zwp_linux_dmabuf_v1 (v3 or later), wp_viewporter,
 * wp_single_pixel_buffer_manager_v1, wp_alpha_modifier_v1, wp_presentation and every wl_output
 * (v4, for its name). NULL when the connection or any of them is missing. The client answers
 * xdg_wm_base pings itself while dispatching; pw_frame_client_destroy disconnects. */
struct pw_frame_client *pw_frame_client_connect(const char *socket);
/* The connection's fd (-1: a client made by pw_frame_client_create, whose fd is GDK's). */
int pw_frame_client_fd(struct pw_frame_client *client);
/* The Output named output_id as Weston reports it: its current mode's width and height (physical
 * pixels) and its wl_output.transform. Returns 1 known, 0 not announced (or gone), -1 transport
 * failure. Dispatches the client's queue first. */
int pw_frame_client_output(struct pw_frame_client *client, const char *output_id,
    int32_t *width, int32_t *height, int32_t *transform);
/* A new screen for output_id; NULL on failure. It shows nothing until its first configure. */
struct pw_screen *pw_screen_create(struct pw_frame_client *client, const char *output_id);
/* The size of the screen's last configure (pixels as it hangs): 1 configured, 0 not yet, -1
 * transport failure. */
int pw_screen_size(struct pw_screen *screen, int32_t *width, int32_t *height);
void pw_screen_destroy(struct pw_screen *screen);
/* Import a LINEAR (or other `modifier`) single-plane dmabuf: `fd` is duplicated, the caller keeps
 * its own. One roundtrip (zwp_linux_buffer_params_v1.create's created or failed). NULL when the
 * compositor refuses it, on transport failure, or without asking when the layout cannot hold the
 * picture (an XR24 or AR24 stride below width x 4, an fd shorter than stride x height: Weston would
 * answer that with a protocol error, ending the connection). */
struct pw_picture *pw_picture_import(struct pw_frame_client *client, int fd, uint32_t width,
    uint32_t height, uint32_t stride, uint32_t fourcc, uint64_t modifier);
/* 1 while a commit showed the picture and the compositor has not released it since, else 0 (a
 * present that shows it after one that did not begins a showing; each wl_buffer.release ends one). */
int pw_picture_busy(struct pw_picture *picture);
void pw_picture_destroy(struct pw_picture *picture);
/* One atomic commit of the screen: planes[0..count) bottom to top, each sub-surface placed above the
 * one before, at (x, y), showing its picture with buffer_transform (a wl_output.transform, the
 * screen's own) and its alpha; sub-surfaces past count attach no buffer (unmapped); then the frame
 * manager's tag_picture (grant_id, frame_tag, picture_change) on the screen's surface and the
 * screen's commit, which applies them all; the unmapped sub-surfaces are destroyed after it, so the
 * compositor releases their pictures. The commit's presentation feedback rides on what it
 * presents: the topmost plane (no plane: the screen's black), as Weston 14 answers no feedback of a
 * surface the planes cover; its answer is the screen's word (pw_screen_presentation). A screen
 * waits on at most 4 answers: past them a commit is made untracked (the pacer's overdue rule
 * covers it), so no surface the compositor does not show can exhaust the client. Returns the tag's
 * sequence (>= 1); -1 transport failure; -2 a bad argument (count above PW_SCREEN_PLANES, alpha 0, a NULL
 * or foreign picture, a transform above 3); -3 the screen has no configure yet. */
int64_t pw_screen_present(struct pw_screen *screen, const struct pw_plane_state *planes, uint32_t count,
    int32_t buffer_transform, const char *grant_id, const char *frame_tag, const char *picture_change);
/* pw_frame_client_presentation for the screen's surface. */
int pw_screen_presentation(struct pw_screen *screen, uint64_t *sequence, int64_t *at_ns, uint32_t *refresh_ns);
