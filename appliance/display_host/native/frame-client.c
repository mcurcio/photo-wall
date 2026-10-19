/* App-private bridge; output facts still come from the base shell. MIT. */
#define _POSIX_C_SOURCE 200809L  /* CLOCK_MONOTONIC under c11 */
#include <errno.h>
#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>
#include "photo-wall-frame-client.h"
#include "presentation-time-client.h"
#include "xdg-shell-client-protocol.h"
#include "linux-dmabuf-v1-client-protocol.h"
#include "viewporter-client-protocol.h"
#include "single-pixel-buffer-v1-client-protocol.h"
#include "alpha-modifier-v1-client-protocol.h"
#include "frame-client.h"
#define MAX_FEEDBACK 128
/* Answers one surface's commits may wait on at once. Weston 14 answers a feedback only when the
 * surface it rides on is seen, or when that surface gets a new buffer or is destroyed (discarded):
 * a surface nothing shows (one another surface covers) holds its feedback unanswered. Past this
 * many a burst drops its oldest answer, and a covered surface's commits go untracked (no feedback
 * asked) until one is answered (FEEDBACK_STALE_NS), so the table never fills, nor Weston's list
 * for that surface: MAX_OUTPUTS * 2 words' surfaces x 4 = MAX_FEEDBACK. */
#define FEEDBACK_PER_SURFACE 4
/* How long a surface's oldest unanswered commit may wait before the surface counts as covered.
 * Weston answers a seen surface's commits at its next repaint (a refresh or two), so a full table
 * younger than this is a burst within one repaint: the oldest answer is dropped and the newest
 * commit tracked, so a burst's last commit (a trial's, say) is never the untracked one. Older, the
 * surface is covered and its commits go untracked, so Weston's list stays bounded. */
#define FEEDBACK_STALE_NS 250000000ll
#define MAX_OUTPUTS 16
#define MAX_OPERATIONS 1024  /* contracts.node_frame.PICTURE_OPERATIONS_BYTES (picture operations v5) */
struct grant { char output[97], id[37], frame[97], trial[4097]; uint64_t generation, revision; uint32_t state; };
/* The newest `picture` operations per Output (frame protocol v4), kept verbatim. */
struct picture { char output[97], operations[MAX_OPERATIONS + 1]; };
/* The compositor's newest word per surface (pacing, player/pacing.py): the tag sequence it answers,
 * 1 presented or 2 discarded, and for a presented one its time and the Output's refresh. */
struct word { struct wl_surface *surface; uint64_t sequence; int64_t at_ns; uint32_t refresh_ns, kind; };
/* An Output as Weston announces it to a connected client (wl_output v4): its name, current mode
 * and transform, known from its first `done` on. */
struct output { struct wl_output *proxy; uint32_t global; char id[97]; int32_t width, height, transform; bool done; };
/* A compositor buffer and how many showings of it the compositor has not released yet: a showing
 * begins with a present that shows it after one that did not, and wl_buffer.release ends one. */
struct pw_picture {
    struct pw_frame_client *client;
    struct wl_buffer *buffer;
    uint32_t held;
    struct pw_picture *next;
};
/* One Output's app surface and its sub-surfaces, one per plane, made as presents need them. */
struct pw_screen {
    struct pw_frame_client *client;
    char output[97];
    struct wl_surface *surface;
    struct xdg_surface *xdg;
    struct xdg_toplevel *toplevel;
    struct wp_viewport *viewport;
    struct wl_buffer *black;               /* by the first configure, and a present with no ground */
    bool black_attached, configured, closed;
    int32_t pending_width, pending_height, width, height;
    uint32_t planes;
    struct wl_surface *plane[PW_SCREEN_PLANES];
    struct wl_subsurface *sub[PW_SCREEN_PLANES];
    struct wp_alpha_modifier_surface_v1 *alpha[PW_SCREEN_PLANES];
    struct wp_viewport *view[PW_SCREEN_PLANES];  /* each sub-surface's destination: its plane's place */
    struct pw_picture *shown[PW_SCREEN_PLANES];   /* each sub-surface's picture in the last present */
    /* What each sub-surface was last sent, so a present sends only what changed (a stacking or
     * position request dirties the parent's sub-surface order, which Weston answers by damaging and
     * repainting below the planes): its position, buffer transform (-1: none yet) and multiplier
     * (0: none yet, never a valid strength), and its viewport's destination (0: none yet). Reset
     * when the sub-surface is made. */
    int32_t at_x[PW_SCREEN_PLANES], at_y[PW_SCREEN_PLANES], transform[PW_SCREEN_PLANES];
    int32_t to_width[PW_SCREEN_PLANES], to_height[PW_SCREEN_PLANES];
    uint32_t multiplier[PW_SCREEN_PLANES];
    /* The screen's ground: the picture its own surface shows (NULL: the single-pixel black) and the
     * buffer transform it was attached with. */
    struct pw_picture *ground;
    int32_t ground_transform;
    struct pw_screen *next;
};
struct pw_frame_client {
    struct wl_display *display;
    bool connected;                        /* pw_frame_client_connect: the connection is this client's */
    struct xdg_wm_base *wm;
    struct wl_compositor *compositor;
    struct wl_subcompositor *subcompositor;
    struct zwp_linux_dmabuf_v1 *dmabuf;
    struct wp_viewporter *viewporter;
    struct wp_single_pixel_buffer_manager_v1 *single_pixel;
    struct wp_alpha_modifier_v1 *alpha;
    struct output outputs[MAX_OUTPUTS];
    struct pw_screen *screens;
    struct pw_picture *imported;           /* every imported picture, linked */
    struct wl_event_queue *queue;
    struct wl_registry *registry;
    struct pw_frame_manager_v1 *manager;
    struct wp_presentation *presentation;
    struct wp_presentation_feedback *feedback[MAX_FEEDBACK];
    struct wl_surface *feedback_surface[MAX_FEEDBACK];
    uint64_t feedback_sequence[MAX_FEEDBACK];
    int64_t feedback_since[MAX_FEEDBACK];  /* CLOCK_MONOTONIC: since when the surface waits unanswered */
    uint64_t tagged;                       /* the newest tag's sequence; 0: none */
    uint32_t clock;                        /* wp_presentation's clock_id */
    bool clock_known;
    struct word words[MAX_OUTPUTS * 2];
    uint32_t manager_name, presentation_name;
    bool lost;
    uint32_t version;
    struct grant grants[MAX_OUTPUTS * 2];
    struct picture pictures[MAX_OUTPUTS];
};
static void clock_id(void *data, struct wp_presentation *presentation, uint32_t id) {
    (void)presentation;
    struct pw_frame_client *c = data;
    c->clock = id; c->clock_known = true;
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
static void picture_event(void *data, struct pw_frame_manager_v1 *manager,
                          const char *output, const char *operations) {
    (void)manager;
    struct pw_frame_client *c = data;
    if (!*output || strlen(output) > 96 || strlen(operations) > MAX_OPERATIONS) { c->lost = true; return; }
    struct picture *p = NULL;
    for (int i = 0; i < MAX_OUTPUTS && !p; i++) if (!strcmp(c->pictures[i].output, output)) p = &c->pictures[i];
    for (int i = 0; i < MAX_OUTPUTS && !p; i++) if (!c->pictures[i].output[0]) p = &c->pictures[i];
    if (!p) { c->lost = true; return; }
    strcpy(p->output, output); strcpy(p->operations, operations);
}
static const struct pw_frame_manager_v1_listener manager_listener = {
    .grant_frame = grant_event, .trial = trial_event, .picture = picture_event
};
static void output_geometry(void *data, struct wl_output *proxy, int32_t x, int32_t y, int32_t mm_width,
    int32_t mm_height, int32_t subpixel, const char *make, const char *model, int32_t transform) {
    (void)proxy; (void)x; (void)y; (void)mm_width; (void)mm_height; (void)subpixel; (void)make; (void)model;
    ((struct output *)data)->transform = transform;
}
static void output_mode(void *data, struct wl_output *proxy, uint32_t flags, int32_t width, int32_t height,
    int32_t refresh) {
    (void)proxy; (void)refresh;
    struct output *o = data;
    if (flags & WL_OUTPUT_MODE_CURRENT) { o->width = width; o->height = height; }
}
static void output_done(void *data, struct wl_output *proxy) {
    (void)proxy;
    struct output *o = data;
    o->done = o->id[0] != 0;
}
static void output_scale(void *data, struct wl_output *proxy, int32_t factor) {
    (void)data; (void)proxy; (void)factor;
}
static void output_name(void *data, struct wl_output *proxy, const char *name) {
    (void)proxy;
    struct output *o = data;
    if (strlen(name) <= 96) strcpy(o->id, name);
}
static void output_description(void *data, struct wl_output *proxy, const char *description) {
    (void)data; (void)proxy; (void)description;
}
static const struct wl_output_listener output_listener = {
    .geometry = output_geometry, .mode = output_mode, .done = output_done, .scale = output_scale,
    .name = output_name, .description = output_description
};
static void wm_ping(void *data, struct xdg_wm_base *wm, uint32_t serial) {
    (void)data;
    xdg_wm_base_pong(wm, serial);
}
static const struct xdg_wm_base_listener wm_listener = {.ping = wm_ping};
/* The standard globals a connected client's screens speak. */
static void screen_global(struct pw_frame_client *c, struct wl_registry *registry, uint32_t name,
                          const char *interface, uint32_t version) {
    if (!strcmp(interface, "xdg_wm_base") && !c->wm) {
        c->wm = wl_registry_bind(registry, name, &xdg_wm_base_interface, 1);
        xdg_wm_base_add_listener(c->wm, &wm_listener, c);
    } else if (!strcmp(interface, "wl_compositor") && !c->compositor && version >= 4) {
        c->compositor = wl_registry_bind(registry, name, &wl_compositor_interface, 4);   /* damage_buffer */
    } else if (!strcmp(interface, "wl_subcompositor") && !c->subcompositor) {
        c->subcompositor = wl_registry_bind(registry, name, &wl_subcompositor_interface, 1);
    } else if (!strcmp(interface, "zwp_linux_dmabuf_v1") && !c->dmabuf && version >= 3) {
        /* No listener: its format and modifier events are dropped. */
        c->dmabuf = wl_registry_bind(registry, name, &zwp_linux_dmabuf_v1_interface, 3);
    } else if (!strcmp(interface, "wp_viewporter") && !c->viewporter) {
        c->viewporter = wl_registry_bind(registry, name, &wp_viewporter_interface, 1);
    } else if (!strcmp(interface, "wp_single_pixel_buffer_manager_v1") && !c->single_pixel) {
        c->single_pixel = wl_registry_bind(registry, name, &wp_single_pixel_buffer_manager_v1_interface, 1);
    } else if (!strcmp(interface, "wp_alpha_modifier_v1") && !c->alpha) {
        c->alpha = wl_registry_bind(registry, name, &wp_alpha_modifier_v1_interface, 1);
    } else if (!strcmp(interface, "wl_output") && version >= 4) {
        for (int i = 0; i < MAX_OUTPUTS; i++) if (!c->outputs[i].proxy) {
            struct output *o = &c->outputs[i];
            *o = (struct output){.global = name};
            o->proxy = wl_registry_bind(registry, name, &wl_output_interface, 4);   /* its name */
            wl_output_add_listener(o->proxy, &output_listener, o);
            return;
        }
    }
}
static void global(void *data, struct wl_registry *registry, uint32_t name,
                   const char *interface, uint32_t version) {
    struct pw_frame_client *c = data;
    if (c->connected) screen_global(c, registry, name, interface, version);
    if (!strcmp(interface, "pw_frame_manager_v1") && !c->manager && version >= 3) {
        c->version = version >= 4 ? 4 : version;
        c->manager = wl_registry_bind(registry, name, &pw_frame_manager_v1_interface, c->version);
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
    for (int i = 0; i < MAX_OUTPUTS; i++) if (c->outputs[i].proxy && c->outputs[i].global == name) {
        wl_output_release(c->outputs[i].proxy);
        c->outputs[i] = (struct output){0};
    }
}
static const struct wl_registry_listener registry_listener = {.global = global, .global_remove = global_remove};
/* Keep the word for the feedback's surface when it answers a newer tag than the word held. A
 * surface new to a full table takes the oldest word's entry (the lowest sequence: tag sequences
 * rise across surfaces): a remap gives the Output a new wl_surface and the old one answers no more,
 * so the table never fills for good. */
static void retire(struct pw_frame_client *c, struct wp_presentation_feedback *f, uint32_t kind,
                   int64_t at_ns, uint32_t refresh_ns) {
    for (int i = 0; i < MAX_FEEDBACK; i++) if (c->feedback[i] == f) {
        struct wl_surface *surface = c->feedback_surface[i];
        uint64_t sequence = c->feedback_sequence[i];
        struct word *w = NULL;
        for (int j = 0; j < MAX_OUTPUTS * 2 && !w; j++) if (c->words[j].surface == surface) w = &c->words[j];
        for (int j = 0; j < MAX_OUTPUTS * 2 && !w; j++) if (!c->words[j].surface) w = &c->words[j];
        if (!w) {
            w = &c->words[0];
            for (int j = 1; j < MAX_OUTPUTS * 2; j++) if (c->words[j].sequence < w->sequence) w = &c->words[j];
            *w = (struct word){0};
        }
        if (w && sequence > w->sequence)
            *w = (struct word){.surface = surface, .sequence = sequence, .at_ns = at_ns,
                               .refresh_ns = refresh_ns, .kind = kind};
        c->feedback[i] = NULL; c->feedback_surface[i] = NULL; c->feedback_sequence[i] = 0;
        c->feedback_since[i] = 0;
    }
    wp_presentation_feedback_destroy(f);
}
static void sync_output(void *data, struct wp_presentation_feedback *f, struct wl_output *o) {
    (void)data; (void)f; (void)o;
}
static void presented(void *data, struct wp_presentation_feedback *f,
    uint32_t hi, uint32_t lo, uint32_t ns, uint32_t refresh,
    uint32_t seq_hi, uint32_t seq_lo, uint32_t flags) {
    (void)seq_hi; (void)seq_lo; (void)flags;
    struct pw_frame_client *c = data;
    /* A time on the Player's own clock only: any other clock is not comparable (-1). */
    int64_t at = c->clock_known && c->clock == CLOCK_MONOTONIC ?
        (int64_t)((((uint64_t)hi << 32) | lo) * 1000000000ull + ns) : -1;
    retire(c, f, 1, at, refresh); /* the Player's pacing only; never app-to-Central evidence */
}
static void discarded(void *data, struct wp_presentation_feedback *f) { retire(data, f, 2, -1, 0); }
static const struct wp_presentation_feedback_listener feedback_listener = {
    .sync_output = sync_output, .presented = presented, .discarded = discarded
};
void pw_frame_client_destroy(struct pw_frame_client *c) {
    if (!c) return;
    while (c->screens) pw_screen_destroy(c->screens);
    while (c->imported) pw_picture_destroy(c->imported);
    for (int i = 0; i < MAX_FEEDBACK; i++) if (c->feedback[i]) wp_presentation_feedback_destroy(c->feedback[i]);
    if (c->manager) pw_frame_manager_v1_destroy(c->manager);
    if (c->presentation) wp_presentation_destroy(c->presentation);
    if (c->wm) xdg_wm_base_destroy(c->wm);
    if (c->compositor) wl_compositor_destroy(c->compositor);
    if (c->subcompositor) wl_subcompositor_destroy(c->subcompositor);
    if (c->dmabuf) zwp_linux_dmabuf_v1_destroy(c->dmabuf);
    if (c->viewporter) wp_viewporter_destroy(c->viewporter);
    if (c->single_pixel) wp_single_pixel_buffer_manager_v1_destroy(c->single_pixel);
    if (c->alpha) wp_alpha_modifier_v1_destroy(c->alpha);
    for (int i = 0; i < MAX_OUTPUTS; i++) if (c->outputs[i].proxy) wl_output_release(c->outputs[i].proxy);
    if (c->registry) wl_registry_destroy(c->registry);
    if (c->queue) wl_event_queue_destroy(c->queue);
    if (c->connected && c->display) { wl_display_flush(c->display); wl_display_disconnect(c->display); }
    free(c);
}
/* The client's own queue and registry on `c->display`, and two roundtrips: the globals, then the
 * first events of what was bound (an Output's name and mode). */
static bool start(struct pw_frame_client *c) {
    c->queue = wl_display_create_queue(c->display);
    if (!c->queue) return false;
    struct wl_display *wrapper = wl_proxy_create_wrapper(c->display);
    if (!wrapper) return false;
    wl_proxy_set_queue((struct wl_proxy *)wrapper, c->queue);
    c->registry = wl_display_get_registry(wrapper);
    wl_proxy_wrapper_destroy(wrapper);
    wl_registry_add_listener(c->registry, &registry_listener, c);
    return wl_display_roundtrip_queue(c->display, c->queue) >= 0 &&
        wl_display_roundtrip_queue(c->display, c->queue) >= 0 && c->manager && c->presentation;
}
struct pw_frame_client *pw_frame_client_create(struct wl_display *display) {
    if (!display) return NULL;
    struct pw_frame_client *c = calloc(1, sizeof *c);
    if (!c) return NULL;
    c->display = display;
    /* Initialization happens before rendering begins, on GDK's owning thread. */
    if (!start(c)) { pw_frame_client_destroy(c); return NULL; }
    return c;
}
struct pw_frame_client *pw_frame_client_connect(const char *socket) {
    struct wl_display *display = wl_display_connect(socket);
    if (!display) return NULL;
    struct pw_frame_client *c = calloc(1, sizeof *c);
    if (!c) { wl_display_disconnect(display); return NULL; }
    c->display = display;
    c->connected = true;
    if (!start(c) || c->version < 4 || !c->wm || !c->compositor || !c->subcompositor || !c->dmabuf ||
        !c->viewporter || !c->single_pixel || !c->alpha) {
        pw_frame_client_destroy(c); return NULL;
    }
    return c;
}
int pw_frame_client_fd(struct pw_frame_client *c) {
    return c && c->connected ? wl_display_get_fd(c->display) : -1;
}
static bool flushed(struct pw_frame_client *c) {
    return wl_display_flush(c->display) >= 0 || errno == EAGAIN;
}
/* A connected client reads its own socket: whatever arrived, without blocking, then dispatched
 * (pings answered by wm_ping) and the answers flushed. */
static int pump(struct pw_frame_client *c) {
    if (!flushed(c)) return -1;
    while (wl_display_prepare_read_queue(c->display, c->queue) != 0)
        if (wl_display_dispatch_queue_pending(c->display, c->queue) < 0) return -1;
    struct pollfd fd = {.fd = wl_display_get_fd(c->display), .events = POLLIN};
    int ready = poll(&fd, 1, 0);
    if (ready > 0 && (fd.revents & (POLLIN | POLLHUP | POLLERR))) {
        if (wl_display_read_events(c->display) < 0) return -1;
    } else {
        wl_display_cancel_read(c->display);
        if (ready < 0 && errno != EINTR) return -1;
    }
    int dispatched = wl_display_dispatch_queue_pending(c->display, c->queue);
    return dispatched < 0 || !flushed(c) ? -1 : dispatched;
}
int pw_frame_client_dispatch(struct pw_frame_client *c) {
    if (!c || c->lost) return -1;
    if (c->connected) return pump(c);
    return wl_display_dispatch_queue_pending(c->display, c->queue);
}
static bool tag_valid(struct pw_frame_client *c, const char *grant_id, const char *frame_tag,
                      const char *picture_change) {
    if (!grant_id || strlen(grant_id) != 36 || !frame_tag || !*frame_tag || strlen(frame_tag) > 96) return false;
    if (picture_change) {
        size_t length = strlen(picture_change);
        if (c->version < 4 || !length || length > 20 || strspn(picture_change, "0123456789") != length) return false;
    }
    return true;
}
static int64_t monotonic_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (int64_t)t.tv_sec * 1000000000ll + t.tv_nsec;
}
/* A slot for one more answer to `key`'s commits, and in `since` since when `key` waits unanswered
 * (now, or the dropped answer's). When `key` already waits on FEEDBACK_PER_SURFACE answers: within
 * FEEDBACK_STALE_NS of its oldest, that answer is dropped (its proxy destroyed: libwayland discards
 * Weston's later word for it) and its slot taken, so the newest commit is always tracked; past it,
 * -1 (the commit goes untracked: the surface is covered). -1 too when the table is full. Every
 * slot is freed by its answer (presented, or discarded: a new buffer on its surface, or the surface
 * destroyed), by such a drop, or with its screen (pw_screen_destroy). */
static int feedback_slot(struct pw_frame_client *c, struct wl_surface *key, int64_t *since) {
    int open_slot = -1, oldest = -1, waiting = 0;
    int64_t now = monotonic_ns();
    for (int i = 0; i < MAX_FEEDBACK; i++) {
        if (!c->feedback[i]) { if (open_slot < 0) open_slot = i; }
        else if (c->feedback_surface[i] == key) {
            waiting++;
            if (oldest < 0 || c->feedback_since[i] < c->feedback_since[oldest]) oldest = i;
        }
    }
    *since = now;
    if (waiting < FEEDBACK_PER_SURFACE) return open_slot;
    if (now - c->feedback_since[oldest] >= FEEDBACK_STALE_NS) return -1;
    *since = c->feedback_since[oldest];     /* the surface still waits since then */
    wp_presentation_feedback_destroy(c->feedback[oldest]);
    c->feedback[oldest] = NULL; c->feedback_surface[oldest] = NULL; c->feedback_sequence[oldest] = 0;
    c->feedback_since[oldest] = 0;
    return oldest;
}
/* Ask for the presentation feedback of `on`'s next applied commit into `slot` (nothing when
 * slot < 0), its answer kept as `key`'s word for the tag `sequence`, `key` waiting since `since`
 * (feedback_slot's). False when the request could not be made. */
static bool feedback_request(struct pw_frame_client *c, int slot, struct wl_surface *key,
    struct wl_surface *on, uint64_t sequence, int64_t since) {
    if (slot < 0) return true;
    struct wp_presentation_feedback *f = wp_presentation_feedback(c->presentation, on);
    if (!f) return false;
    c->feedback[slot] = f;
    c->feedback_surface[slot] = key;
    c->feedback_sequence[slot] = sequence;
    c->feedback_since[slot] = since;
    wp_presentation_feedback_add_listener(f, &feedback_listener, c);
    return true;
}
/* The tag (v3 `tag`, or v4 `tag_picture` with `picture_change`) for `surface`'s next commit. */
static void tag_send(struct pw_frame_client *c, struct wl_surface *surface,
    const char *grant_id, const char *frame_tag, const char *picture_change) {
    if (picture_change) pw_frame_manager_v1_tag_picture(c->manager, surface, grant_id, frame_tag, picture_change);
    else pw_frame_manager_v1_tag(c->manager, surface, grant_id, frame_tag);
}
/* The tag and its presentation feedback, both on the surface GDK commits. */
static int tag_commit(struct pw_frame_client *c, struct wl_surface *surface,
    const char *grant_id, const char *frame_tag, const char *picture_change) {
    if (!c || !surface || !tag_valid(c, grant_id, frame_tag, picture_change) ||
        pw_frame_client_dispatch(c) < 0) return -1;
    uint64_t sequence = c->tagged + 1;
    tag_send(c, surface, grant_id, frame_tag, picture_change);
    int64_t since;
    int slot = feedback_slot(c, surface, &since);
    if (!feedback_request(c, slot, surface, surface, sequence, since)) return -1;
    c->tagged = sequence;
    return 0;
}
int pw_frame_client_tag_next_commit(struct pw_frame_client *c, struct wl_surface *surface,
    const char *grant_id, const char *frame_tag) {
    return tag_commit(c, surface, grant_id, frame_tag, NULL);
}
int pw_frame_client_tag_picture_next_commit(struct pw_frame_client *c, struct wl_surface *surface,
    const char *grant_id, const char *frame_tag, const char *picture_change) {
    if (!picture_change) return -1;
    return tag_commit(c, surface, grant_id, frame_tag, picture_change);
}
int pw_frame_client_picture(struct pw_frame_client *c, const char *output, char *operations, uint32_t capacity) {
    if (!c || !output || !operations || c->version < 4 || pw_frame_client_dispatch(c) < 0) return -1;
    for (int i = 0; i < MAX_OUTPUTS; i++) if (c->pictures[i].output[0] && !strcmp(c->pictures[i].output, output)) {
        if (strlen(c->pictures[i].operations) >= capacity) return -1;
        strcpy(operations, c->pictures[i].operations);
        return 1;
    }
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
    /* The tag's own check first: the shell answers a bad grant or tag with a protocol error. */
    if (!c || !surface || !tag_valid(c, grant, tag, NULL) || !primitives || strlen(primitives) > 512 ||
        c->lost) return -1;
    pw_frame_manager_v1_overlay(c->manager, surface, grant, tag, primitives);
    return 0;
}

uint64_t pw_frame_client_tagged(struct pw_frame_client *c) { return c ? c->tagged : 0; }
int pw_frame_client_presentation(struct pw_frame_client *c, struct wl_surface *surface,
    uint64_t *sequence, int64_t *at_ns, uint32_t *refresh_ns) {
    if (!c || !surface || !sequence || !at_ns || !refresh_ns || pw_frame_client_dispatch(c) < 0) return -1;
    for (int i = 0; i < MAX_OUTPUTS * 2; i++) if (c->words[i].surface == surface) {
        *sequence = c->words[i].sequence; *at_ns = c->words[i].at_ns; *refresh_ns = c->words[i].refresh_ns;
        return (int)c->words[i].kind;
    }
    return 0;
}

/* The Player's screens (frame-client.h; render redesign, run ledger .claude/runs/render-planes/
 * s1-page.md, bead B3). */
#define FOURCC_XR24 0x34325258u   /* DRM_FORMAT_XRGB8888 */
#define FOURCC_AR24 0x34325241u   /* DRM_FORMAT_ARGB8888 */
int pw_frame_client_output(struct pw_frame_client *c, const char *output_id,
    int32_t *width, int32_t *height, int32_t *transform) {
    if (!c || !output_id || !width || !height || !transform || pw_frame_client_dispatch(c) < 0) return -1;
    for (int i = 0; i < MAX_OUTPUTS; i++) {
        struct output *o = &c->outputs[i];
        if (o->proxy && o->done && !strcmp(o->id, output_id)) {
            *width = o->width; *height = o->height; *transform = o->transform;
            return 1;
        }
    }
    return 0;
}
static void toplevel_configure(void *data, struct xdg_toplevel *toplevel, int32_t width, int32_t height,
                               struct wl_array *states) {
    (void)toplevel; (void)states;
    struct pw_screen *s = data;
    s->pending_width = width; s->pending_height = height;
}
static void toplevel_close(void *data, struct xdg_toplevel *toplevel) {
    (void)toplevel;
    struct pw_screen *s = data;
    s->closed = true; s->configured = false;
}
static void toplevel_bounds(void *data, struct xdg_toplevel *toplevel, int32_t width, int32_t height) {
    (void)data; (void)toplevel; (void)width; (void)height;
}
static void toplevel_capabilities(void *data, struct xdg_toplevel *toplevel, struct wl_array *capabilities) {
    (void)data; (void)toplevel; (void)capabilities;
}
static const struct xdg_toplevel_listener toplevel_listener = {
    .configure = toplevel_configure, .close = toplevel_close, .configure_bounds = toplevel_bounds,
    .wm_capabilities = toplevel_capabilities
};
/* Each configure with a size: the black stretched over it (its buffer attached once only). This
 * commit applies nothing of the planes: every present ends with the screen's own commit. */
static void surface_configure(void *data, struct xdg_surface *xdg, uint32_t serial) {
    struct pw_screen *s = data;
    xdg_surface_ack_configure(xdg, serial);
    if (s->closed) return;
    if (s->pending_width > 0 && s->pending_height > 0) {
        s->width = s->pending_width; s->height = s->pending_height; s->configured = true;
    }
    if (s->configured) {
        wp_viewport_set_destination(s->viewport, s->width, s->height);
        if (!s->black_attached) { wl_surface_attach(s->surface, s->black, 0, 0); s->black_attached = true; }
        wl_surface_damage_buffer(s->surface, 0, 0, INT32_MAX, INT32_MAX);
    }
    wl_surface_commit(s->surface);
}
static const struct xdg_surface_listener surface_listener = {.configure = surface_configure};
struct pw_screen *pw_screen_create(struct pw_frame_client *c, const char *output_id) {
    if (!c || !c->connected || c->lost || !output_id || !*output_id || strlen(output_id) > 96) return NULL;
    struct pw_screen *s = calloc(1, sizeof *s);
    if (!s) return NULL;
    s->client = c;
    strcpy(s->output, output_id);
    s->next = c->screens; c->screens = s;
    s->surface = wl_compositor_create_surface(c->compositor);
    s->viewport = s->surface ? wp_viewporter_get_viewport(c->viewporter, s->surface) : NULL;
    s->black = wp_single_pixel_buffer_manager_v1_create_u32_rgba_buffer(c->single_pixel, 0, 0, 0, UINT32_MAX);
    s->xdg = s->surface ? xdg_wm_base_get_xdg_surface(c->wm, s->surface) : NULL;
    s->toplevel = s->xdg ? xdg_surface_get_toplevel(s->xdg) : NULL;
    if (!s->viewport || !s->black || !s->toplevel) { pw_screen_destroy(s); return NULL; }
    xdg_surface_add_listener(s->xdg, &surface_listener, s);
    xdg_toplevel_add_listener(s->toplevel, &toplevel_listener, s);
    char app_id[112];
    snprintf(app_id, sizeof app_id, "photo-wall-%s", output_id);   /* player/output_discovery.py */
    xdg_toplevel_set_app_id(s->toplevel, app_id);
    wl_surface_commit(s->surface);
    if (!flushed(c)) { pw_screen_destroy(s); return NULL; }
    return s;
}
int pw_screen_size(struct pw_screen *s, int32_t *width, int32_t *height) {
    if (!s || !width || !height || pw_frame_client_dispatch(s->client) < 0) return -1;
    if (!s->configured) return 0;
    *width = s->width; *height = s->height;
    return 1;
}
void pw_screen_destroy(struct pw_screen *s) {
    if (!s) return;
    struct pw_frame_client *c = s->client;
    for (struct pw_screen **at = &c->screens; *at; at = &(*at)->next) if (*at == s) { *at = s->next; break; }
    for (uint32_t i = 0; i < s->planes; i++) {
        wp_viewport_destroy(s->view[i]);
        wp_alpha_modifier_surface_v1_destroy(s->alpha[i]);
        wl_subsurface_destroy(s->sub[i]);
        wl_surface_destroy(s->plane[i]);
    }
    /* Its answers and words go with it (those asked on its planes too: they are kept under the
     * screen's surface): a later surface may reuse the address. */
    for (int i = 0; i < MAX_FEEDBACK; i++) if (c->feedback[i] && c->feedback_surface[i] == s->surface) {
        wp_presentation_feedback_destroy(c->feedback[i]);
        c->feedback[i] = NULL; c->feedback_surface[i] = NULL; c->feedback_sequence[i] = 0;
        c->feedback_since[i] = 0;
    }
    for (int i = 0; i < MAX_OUTPUTS * 2; i++) if (c->words[i].surface == s->surface) c->words[i] = (struct word){0};
    if (s->toplevel) xdg_toplevel_destroy(s->toplevel);
    if (s->xdg) xdg_surface_destroy(s->xdg);
    if (s->viewport) wp_viewport_destroy(s->viewport);
    if (s->surface) wl_surface_destroy(s->surface);
    if (s->black) wl_buffer_destroy(s->black);
    flushed(c);
    free(s);
}
static void picture_release(void *data, struct wl_buffer *buffer) {
    (void)buffer;
    struct pw_picture *p = data;
    if (p->held) p->held--;
}
static const struct wl_buffer_listener picture_listener = {.release = picture_release};
struct import { struct wl_buffer *buffer; bool failed; };
static void import_created(void *data, struct zwp_linux_buffer_params_v1 *params, struct wl_buffer *buffer) {
    (void)params;
    ((struct import *)data)->buffer = buffer;
}
static void import_failed(void *data, struct zwp_linux_buffer_params_v1 *params) {
    (void)params;
    ((struct import *)data)->failed = true;
}
static const struct zwp_linux_buffer_params_v1_listener import_listener = {
    .created = import_created, .failed = import_failed
};
struct pw_picture *pw_picture_import(struct pw_frame_client *c, int fd, uint32_t width,
    uint32_t height, uint32_t stride, uint32_t fourcc, uint64_t modifier) {
    if (!c || !c->connected || c->lost || fd < 0 || !width || !height || width > INT32_MAX ||
        height > INT32_MAX || !stride) return NULL;
    /* A layout that cannot hold the picture is refused here: Weston answers a buffer shorter than
     * stride x height with a protocol error, which would end the connection. */
    if ((fourcc == FOURCC_XR24 || fourcc == FOURCC_AR24) && (uint64_t)stride < (uint64_t)width * 4) return NULL;
    off_t at = lseek(fd, 0, SEEK_CUR), size = lseek(fd, 0, SEEK_END);
    if (at >= 0) lseek(fd, at, SEEK_SET);
    if (size >= 0 && (uint64_t)stride * height > (uint64_t)size) return NULL;
    struct pw_picture *p = calloc(1, sizeof *p);
    if (!p) return NULL;
    struct zwp_linux_buffer_params_v1 *params = zwp_linux_dmabuf_v1_create_params(c->dmabuf);
    if (!params) { free(p); return NULL; }
    struct import result = {0};
    zwp_linux_buffer_params_v1_add_listener(params, &import_listener, &result);
    /* libwayland sends a duplicate of `fd`: the caller keeps its own. */
    zwp_linux_buffer_params_v1_add(params, fd, 0, 0, stride, (uint32_t)(modifier >> 32),
                                   (uint32_t)(modifier & 0xffffffffu));
    zwp_linux_buffer_params_v1_create(params, (int32_t)width, (int32_t)height, fourcc, 0);
    int answered = wl_display_roundtrip_queue(c->display, c->queue);
    zwp_linux_buffer_params_v1_destroy(params);
    if (answered < 0 || !result.buffer) {
        if (result.buffer) wl_buffer_destroy(result.buffer);
        free(p);
        return NULL;
    }
    p->client = c;
    p->buffer = result.buffer;
    wl_buffer_add_listener(p->buffer, &picture_listener, p);
    p->next = c->imported; c->imported = p;
    return p;
}
int pw_picture_busy(struct pw_picture *p) { return p && p->held ? 1 : 0; }
void pw_picture_destroy(struct pw_picture *p) {
    if (!p) return;
    struct pw_frame_client *c = p->client;
    for (struct pw_picture **at = &c->imported; *at; at = &(*at)->next) if (*at == p) { *at = p->next; break; }
    for (struct pw_screen *s = c->screens; s; s = s->next) {
        for (uint32_t i = 0; i < PW_SCREEN_PLANES; i++) if (s->shown[i] == p) s->shown[i] = NULL;
        if (s->ground == p) { s->ground = NULL; s->ground_transform = -1; }   /* attached again next */
    }
    wl_buffer_destroy(p->buffer);
    flushed(c);
    free(p);
}
static bool owned(struct pw_frame_client *c, const struct pw_picture *p) {
    for (struct pw_picture *q = c->imported; q; q = q->next) if (q == p) return true;
    return false;
}
static bool showing(struct pw_picture *const *pictures, uint32_t count, const struct pw_picture *p) {
    for (uint32_t i = 0; i < count; i++) if (pictures[i] == p) return true;
    return false;
}
/* Sub-surface i made: stacked above the one below it (the screen's surface for 0) once, as nothing
 * restacks the planes later (a present adds or removes them only at the top), and nothing sent yet. */
static void plane_make(struct pw_screen *s, uint32_t i) {
    struct pw_frame_client *c = s->client;
    s->plane[i] = wl_compositor_create_surface(c->compositor);
    s->sub[i] = wl_subcompositor_get_subsurface(c->subcompositor, s->plane[i], s->surface);
    wl_subsurface_set_sync(s->sub[i]);
    wl_subsurface_place_above(s->sub[i], i ? s->plane[i - 1] : s->surface);
    s->alpha[i] = wp_alpha_modifier_v1_get_surface(c->alpha, s->plane[i]);
    s->view[i] = wp_viewporter_get_viewport(c->viewporter, s->plane[i]);
    s->shown[i] = NULL;
    s->at_x[i] = s->at_y[i] = INT32_MIN;
    s->to_width[i] = s->to_height[i] = 0;
    s->transform[i] = -1;
    s->multiplier[i] = 0;
}
/* Whether `p` is one of `now`'s first `count` pictures or the ground `ground`. */
static bool among(struct pw_picture *const *now, uint32_t count, const struct pw_picture *ground,
                  const struct pw_picture *p) {
    return p == ground || showing(now, count, p);
}
int64_t pw_screen_present(struct pw_screen *s, const struct pw_plane_state *planes, uint32_t count,
    struct pw_picture *ground, int32_t buffer_transform, const char *grant_id, const char *frame_tag,
    const char *picture_change) {
    if (!s || (count && !planes) || count > PW_SCREEN_PLANES || buffer_transform < 0 || buffer_transform > 3)
        return -2;
    struct pw_frame_client *c = s->client;
    for (uint32_t i = 0; i < count; i++)
        if (!planes[i].alpha || planes[i].width < 1 || planes[i].height < 1 || !planes[i].picture ||
            !owned(c, planes[i].picture)) return -2;
    if (ground && !owned(c, ground)) return -2;
    if (!tag_valid(c, grant_id, frame_tag, picture_change)) return -2;
    /* Read first: a configure dispatched after the planes' requests would commit them untagged. */
    if (pw_frame_client_dispatch(c) < 0) return -1;
    if (!s->configured) return -3;
    int64_t since;
    int slot = feedback_slot(c, s->surface, &since);
    uint64_t sequence = c->tagged + 1;
    for (; s->planes < count; s->planes++) plane_make(s, s->planes);
    struct pw_picture *now[PW_SCREEN_PLANES] = {0};
    for (uint32_t i = 0; i < count; i++) {
        const struct pw_plane_state *plane = &planes[i];
        /* Only what changed is sent: a fade's present is each plane's multiplier and commit. */
        if (s->at_x[i] != plane->x || s->at_y[i] != plane->y) {
            wl_subsurface_set_position(s->sub[i], plane->x, plane->y);
            s->at_x[i] = plane->x; s->at_y[i] = plane->y;
        }
        /* The picture, finished at its own size, enlarged to its place (a picture as large as its
         * place is shown 1:1): double-buffered, applied by this commit. */
        if (s->to_width[i] != plane->width || s->to_height[i] != plane->height) {
            wp_viewport_set_destination(s->view[i], plane->width, plane->height);
            s->to_width[i] = plane->width; s->to_height[i] = plane->height;
        }
        if (s->shown[i] != plane->picture || s->transform[i] != buffer_transform) {
            wl_surface_attach(s->plane[i], plane->picture->buffer, 0, 0);
            if (s->transform[i] != buffer_transform) wl_surface_set_buffer_transform(s->plane[i], buffer_transform);
            wl_surface_damage_buffer(s->plane[i], 0, 0, INT32_MAX, INT32_MAX);
            s->transform[i] = buffer_transform;
        }
        if (s->multiplier[i] != plane->alpha) {
            wp_alpha_modifier_surface_v1_set_multiplier(s->alpha[i], plane->alpha);
            s->multiplier[i] = plane->alpha;
        }
        /* The answer rides on what is presented: the topmost plane, never covered by the planes
         * below it (Weston 14 answers no feedback of a surface the planes cover, such as the
         * screen's ground under a full-screen opaque Frame, and keeps it). Asked before this
         * sub-surface's commit, which caches it for the screen's commit, as it caches the rest. */
        if (i + 1 == count && !feedback_request(c, slot, s->surface, s->plane[i], sequence, since)) return -1;
        wl_surface_commit(s->plane[i]);
        now[i] = plane->picture;
    }
    for (uint32_t i = count; i < s->planes; i++) {     /* the rest unmapped */
        wl_surface_attach(s->plane[i], NULL, 0, 0);
        wl_surface_commit(s->plane[i]);
    }
    /* The ground: a picture the display controller can scan out under the planes, so Weston never
     * needs its renderer for what the planes leave uncovered; NULL, the single-pixel black. */
    int32_t ground_transform = ground ? buffer_transform : 0;
    if (ground != s->ground || ground_transform != s->ground_transform) {
        wl_surface_attach(s->surface, ground ? ground->buffer : s->black, 0, 0);
        if (ground_transform != s->ground_transform) wl_surface_set_buffer_transform(s->surface, ground_transform);
        wl_surface_damage_buffer(s->surface, 0, 0, INT32_MAX, INT32_MAX);
    }
    /* A showing begins when the last present did not show the picture (one release ends it). */
    for (uint32_t i = 0; i <= count; i++) {
        struct pw_picture *p = i < count ? now[i] : ground;
        if (!p || (i < count ? among(now, i, NULL, p) : showing(now, count, p))) continue;   /* counted */
        if (!among(s->shown, PW_SCREEN_PLANES, s->ground, p)) p->held++;
    }
    memcpy(s->shown, now, sizeof now);
    s->ground = ground; s->ground_transform = ground_transform;
    tag_send(c, s->surface, grant_id, frame_tag, picture_change);
    /* No plane: the ground is what is presented. */
    if (!count && !feedback_request(c, slot, s->surface, s->surface, sequence, since)) return -1;
    c->tagged = sequence;
    wl_surface_commit(s->surface);
    /* That commit unmapped the rest; destroying them now (no visible change) drops the compositor's
     * last hold on their pictures: Weston 14's GL renderer keeps an unmapped surface's buffer until
     * the surface gets another or is destroyed, so their pictures would never be released. */
    for (uint32_t i = count; i < s->planes; i++) {
        wp_viewport_destroy(s->view[i]);
        wp_alpha_modifier_surface_v1_destroy(s->alpha[i]);
        wl_subsurface_destroy(s->sub[i]);
        wl_surface_destroy(s->plane[i]);
        s->shown[i] = NULL;
    }
    s->planes = count;
    if (!flushed(c)) return -1;
    return (int64_t)c->tagged;
}
int pw_screen_presentation(struct pw_screen *s, uint64_t *sequence, int64_t *at_ns, uint32_t *refresh_ns) {
    if (!s) return -1;
    return pw_frame_client_presentation(s->client, s->surface, sequence, at_ns, refresh_ns);
}

/* Render redesign S3 (run ledger .claude/runs/render-planes/s3-page.md, bead B3): the trial overlay on
 * the screen's surface, pw_frame_client_overlay's own checks and request. Read first, as
 * pw_screen_present does, so the configure check is current; the shell keeps the overlay for the
 * surface's next commit, which the present that follows makes. */
int pw_screen_overlay(struct pw_screen *s, const char *grant_id, const char *frame_tag, const char *primitives) {
    if (!s || pw_frame_client_dispatch(s->client) < 0 || !s->configured) return -1;
    return pw_frame_client_overlay(s->client, s->surface, grant_id, frame_tag, primitives);
}
