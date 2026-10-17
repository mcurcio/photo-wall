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
