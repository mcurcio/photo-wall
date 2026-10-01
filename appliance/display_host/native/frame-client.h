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
