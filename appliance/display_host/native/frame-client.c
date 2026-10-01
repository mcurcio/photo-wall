/* App-private bridge; output facts still come from the base shell. MIT. */
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <wayland-client.h>
#include "photo-wall-frame-client.h"
#include "presentation-time-client.h"
#include "frame-client.h"
#define MAX_FEEDBACK 128
#define MAX_OUTPUTS 16
struct grant { char output[97], id[37], frame[97], trial[4097]; uint64_t generation, revision; uint32_t state; };
struct pw_frame_client {
    struct wl_display *display;
    struct wl_event_queue *queue;
    struct wl_registry *registry;
    struct pw_frame_manager_v1 *manager;
    struct wp_presentation *presentation;
    struct wp_presentation_feedback *feedback[MAX_FEEDBACK];
    uint32_t manager_name, presentation_name;
    bool lost;
    struct grant grants[MAX_OUTPUTS * 2];
};
static void clock_id(void *data, struct wp_presentation *presentation, uint32_t id) {
    (void)data; (void)presentation; (void)id;
}
static const struct wp_presentation_listener presentation_listener = {.clock_id = clock_id};
static void grant_event(void *data, struct pw_frame_manager_v1 *manager,
    const char *output, const char *id, const char *frame, const char *generation, const char *revision, uint32_t state) {
    (void)manager;
    struct pw_frame_client *c = data;
    if (strlen(output) > 96 || strlen(id) != 36 || !*frame || strlen(frame) > 96 || state > 3) { c->lost = true; return; }
    struct grant *g = NULL;
    for (int i = 0; i < MAX_OUTPUTS * 2; i++) if (!strcmp(c->grants[i].output, output) && !strcmp(c->grants[i].id, id)) { g = &c->grants[i]; break; }
    if (!g && state == 2) return;
    if (!g) for (int i = 0; i < MAX_OUTPUTS * 2; i++) if (!c->grants[i].output[0] || c->grants[i].state == 2) { g = &c->grants[i]; break; }
    if (!g) { c->lost = true; return; }
    if (state == 2 && strcmp(g->id, id)) return;
    if (strcmp(g->id, id)) g->trial[0] = 0;
    strcpy(g->output, output); strcpy(g->id, id); strcpy(g->frame, frame);
    char *end = NULL;
    g->generation = strtoull(generation, &end, 10);
    if (!*generation || *end || !g->generation) { c->lost = true; return; }
    g->revision = strtoull(revision, &end, 10);
    if (!*revision || *end) { c->lost = true; return; }
    g->state = state;
}
static void trial_event(void *data, struct pw_frame_manager_v1 *manager,
                        const char *output, const char *grant, const char *candidate) {
    (void)manager;
    struct pw_frame_client *c = data;
    if (strlen(candidate) > 4096) { c->lost = true; return; }
    for (int i = 0; i < MAX_OUTPUTS * 2; i++) if (!strcmp(c->grants[i].output, output) &&
        !strcmp(c->grants[i].id, grant)) {
        strcpy(c->grants[i].trial, candidate); return;
    }
}
static const struct pw_frame_manager_v1_listener manager_listener = {.grant_frame = grant_event, .trial = trial_event};
static void global(void *data, struct wl_registry *registry, uint32_t name,
                   const char *interface, uint32_t version) {
    struct pw_frame_client *c = data;
    if (!strcmp(interface, "pw_frame_manager_v1") && !c->manager && version >= 3) {
        c->manager = wl_registry_bind(registry, name, &pw_frame_manager_v1_interface, 3);
        c->manager_name = name;
        pw_frame_manager_v1_add_listener(c->manager, &manager_listener, c);
    } else if (!strcmp(interface, "wp_presentation") && !c->presentation) {
        c->presentation = wl_registry_bind(registry, name, &wp_presentation_interface, 1);
        c->presentation_name = name;
        wp_presentation_add_listener(c->presentation, &presentation_listener, c);
    }
}
static void global_remove(void *data, struct wl_registry *registry, uint32_t name) {
    (void)registry;
    struct pw_frame_client *c = data;
    if (name == c->manager_name || name == c->presentation_name) c->lost = true;
}
static const struct wl_registry_listener registry_listener = {.global = global, .global_remove = global_remove};
static void retire(struct pw_frame_client *c, struct wp_presentation_feedback *f) {
    for (int i = 0; i < MAX_FEEDBACK; i++) if (c->feedback[i] == f) c->feedback[i] = NULL;
    wp_presentation_feedback_destroy(f);
}
static void sync_output(void *data, struct wp_presentation_feedback *f, struct wl_output *o) {
    (void)data; (void)f; (void)o;
}
static void presented(void *data, struct wp_presentation_feedback *f,
    uint32_t hi, uint32_t lo, uint32_t ns, uint32_t refresh,
    uint32_t seq_hi, uint32_t seq_lo, uint32_t flags) {
    (void)hi; (void)lo; (void)ns; (void)refresh; (void)seq_hi; (void)seq_lo; (void)flags;
    retire(data, f); /* deliberately no app-to-Central evidence callback */
}
static void discarded(void *data, struct wp_presentation_feedback *f) { retire(data, f); }
static const struct wp_presentation_feedback_listener feedback_listener = {
    .sync_output = sync_output, .presented = presented, .discarded = discarded
};
void pw_frame_client_destroy(struct pw_frame_client *c) {
    if (!c) return;
    for (int i = 0; i < MAX_FEEDBACK; i++) if (c->feedback[i]) wp_presentation_feedback_destroy(c->feedback[i]);
    if (c->manager) pw_frame_manager_v1_destroy(c->manager);
    if (c->presentation) wp_presentation_destroy(c->presentation);
    if (c->registry) wl_registry_destroy(c->registry);
    if (c->queue) wl_event_queue_destroy(c->queue);
    free(c);
}
struct pw_frame_client *pw_frame_client_create(struct wl_display *display) {
    if (!display) return NULL;
    struct pw_frame_client *c = calloc(1, sizeof *c);
    if (!c) return NULL;
    c->display = display;
    c->queue = wl_display_create_queue(display);
    if (!c->queue) { pw_frame_client_destroy(c); return NULL; }
    struct wl_display *wrapper = wl_proxy_create_wrapper(display);
    if (!wrapper) { pw_frame_client_destroy(c); return NULL; }
    wl_proxy_set_queue((struct wl_proxy *)wrapper, c->queue);
    c->registry = wl_display_get_registry(wrapper);
    wl_proxy_wrapper_destroy(wrapper);
    wl_registry_add_listener(c->registry, &registry_listener, c);
    /* Initialization happens before rendering begins, on GDK's owning thread. */
    if (wl_display_roundtrip_queue(display, c->queue) < 0 ||
        wl_display_roundtrip_queue(display, c->queue) < 0 || !c->manager || !c->presentation) {
        pw_frame_client_destroy(c); return NULL;
    }
    return c;
}
int pw_frame_client_dispatch(struct pw_frame_client *c) {
    if (!c || c->lost) return -1;
    return wl_display_dispatch_queue_pending(c->display, c->queue);
}
int pw_frame_client_tag_next_commit(struct pw_frame_client *c, struct wl_surface *surface,
    const char *grant_id, const char *frame_tag) {
    if (!c || !surface || !grant_id || strlen(grant_id) != 36 || !frame_tag ||
        !*frame_tag || strlen(frame_tag) > 96 || pw_frame_client_dispatch(c) < 0) return -1;
    int slot = -1;
    for (int i = 0; i < MAX_FEEDBACK; i++) if (!c->feedback[i]) { slot = i; break; }
    if (slot < 0) return -1;
    pw_frame_manager_v1_tag(c->manager, surface, grant_id, frame_tag);
    struct wp_presentation_feedback *f = wp_presentation_feedback(c->presentation, surface);
    if (!f) return -1;
    c->feedback[slot] = f;
    wp_presentation_feedback_add_listener(f, &feedback_listener, c);
    return 0;
}

int pw_frame_client_grant(struct pw_frame_client *c, const char *output,
    char *id, char *frame, uint64_t *generation, uint64_t *revision, uint32_t *state) {
    if (!c || !output || !id || !frame || !generation || !revision || !state || pw_frame_client_dispatch(c) < 0) return -1;
    for (int i = 0; i < MAX_OUTPUTS * 2; i++) {
        struct grant *g = &c->grants[i];
        if (!strcmp(g->output, output) && g->state < 2 && g->id[0]) {
            memcpy(id, g->id, sizeof g->id); memcpy(frame, g->frame, sizeof g->frame); *generation = g->generation;
            *revision = g->revision; *state = g->state; return 1;
        }
    }
    return 0;
}

int pw_frame_client_grant_revision(struct pw_frame_client *c, const char *output, const char *frame,
    uint64_t generation, uint64_t revision, char *id, uint32_t *state) {
    if (!c || !output || !frame || !id || !state || pw_frame_client_dispatch(c) < 0) return -1;
    for (int i = 0; i < MAX_OUTPUTS * 2; i++) {
        struct grant *g = &c->grants[i];
        if (!strcmp(g->output, output) && !strcmp(g->frame, frame) && g->state != 2 && g->id[0] &&
            g->generation == generation && g->revision == revision) {
            memcpy(id, g->id, sizeof g->id); *state = g->state; return 1;
        }
    }
    return 0;
}

int pw_frame_client_trial(struct pw_frame_client *c, const char *output, const char *grant,
                         char *candidate, uint32_t capacity) {
    if (!c || !output || !grant || !candidate || capacity < 4097 || pw_frame_client_dispatch(c) < 0) return -1;
    for (int i = 0; i < MAX_OUTPUTS * 2; i++) if (!strcmp(c->grants[i].output, output) &&
        !strcmp(c->grants[i].id, grant) && c->grants[i].state != 2) {
        strcpy(candidate, c->grants[i].trial); return c->grants[i].trial[0] ? 1 : 0;
    }
    return 0;
}
int pw_frame_client_overlay(struct pw_frame_client *c, struct wl_surface *surface,
    const char *grant, const char *tag, const char *primitives) {
    if (!c || !surface || !grant || !tag || !primitives || strlen(primitives) > 512 || c->lost) return -1;
    pw_frame_manager_v1_overlay(c->manager, surface, grant, tag, primitives);
    return 0;
}
