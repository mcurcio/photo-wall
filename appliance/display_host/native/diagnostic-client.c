/* Private base diagnostic painter. MIT. No GTK, Player or app-root imports. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <cairo.h>
#include <jansson.h>
#include <wayland-client.h>
#include "photo-wall-frame-client.h"
#include "presentation-time-client.h"

#define MAX_OUTPUTS 16
#define MAX_PIXELS (4096 * 2160)
#define MAX_MEMORY (128 * 1024 * 1024)
struct client;
struct output {
    struct client *client;
    struct wl_surface *surface;
    char name[97], reason[97];
    int width, height;
    uint32_t serial, testing;
    bool dirty, overlay;
    double points[4][2];
    unsigned buffers;
};
struct client {
    struct wl_display *display;
    struct wl_compositor *compositor;
    struct wl_shm *shm;
    struct wp_presentation *presentation;
    struct pw_diagnostic_manager_v1 *manager;
    struct output outputs[MAX_OUTPUTS];
    size_t memory;
};
struct buffer {
    struct wl_buffer *proxy;
    struct output *output;
    void *pixels;
    size_t size;
};
static void draw(struct output *o);
static void buffer_release(void *data, struct wl_buffer *proxy) {
    struct buffer *b = data;
    struct output *o = b->output;
    wl_buffer_destroy(proxy);
    munmap(b->pixels, b->size);
    o->client->memory -= b->size;
    o->buffers--;
    free(b);
    if (o->dirty) draw(o);
}
static const struct wl_buffer_listener buffer_listener = {.release = buffer_release};
static void sync_output(void *data, struct wp_presentation_feedback *f, struct wl_output *o) {
    (void)data; (void)f; (void)o;
}
static void presented(void *data, struct wp_presentation_feedback *f,
                      uint32_t hi, uint32_t lo, uint32_t ns, uint32_t refresh,
                      uint32_t seq_hi, uint32_t seq_lo, uint32_t flags) {
    (void)data; (void)hi; (void)lo; (void)ns; (void)refresh;
    (void)seq_hi; (void)seq_lo; (void)flags;
    wp_presentation_feedback_destroy(f);
}
static void discarded(void *data, struct wp_presentation_feedback *f) {
    (void)data; wp_presentation_feedback_destroy(f);
}
static const struct wp_presentation_feedback_listener feedback_listener = {
    .sync_output = sync_output, .presented = presented, .discarded = discarded
};
static const char *reason_text(const char *reason) {
    if (!strcmp(reason, "starting_new")) return "Starting Player - waiting for authorized handoff";
    if (!strcmp(reason, "output_mode_changed")) return "Display mode changed - checking Player output";
    if (!strcmp(reason, "surface_lease_or_process_lost")) return "Player output unavailable - management remains separate";
    if (!strcmp(reason, "authorized_withdrawal")) return "Player restarting - waiting for new output";
    return "Player output unavailable";
}
static void line(cairo_t *cr, double x, double y, double size, const char *text) {
    cairo_set_font_size(cr, size); cairo_move_to(cr, x, y); cairo_show_text(cr, text);
}
static void draw(struct output *o) {
    struct client *c = o->client;
    if (!o->dirty || !c->compositor || !c->shm || !c->presentation || !o->surface ||
        o->width <= 0 || o->height <= 0 || o->width > 8192 || o->height > 8192 ||
        (int64_t)o->width * o->height > MAX_PIXELS || o->buffers >= 2) return;
    size_t size = (size_t)o->width * (size_t)o->height * 4;
    if (c->memory + size > MAX_MEMORY) return;
    int fd = memfd_create("photo-wall-diagnostic", MFD_CLOEXEC);
    if (fd < 0 || ftruncate(fd, (off_t)size) < 0) { if (fd >= 0) close(fd); return; }
    void *pixels = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (pixels == MAP_FAILED) { close(fd); return; }
    struct wl_shm_pool *pool = wl_shm_create_pool(c->shm, fd, (int32_t)size);
    struct buffer *b = calloc(1, sizeof *b);
    if (!pool || !b) {
        if (pool) wl_shm_pool_destroy(pool);
        free(b); munmap(pixels, size); close(fd); return;
    }
    b->proxy = wl_shm_pool_create_buffer(pool, 0, o->width, o->height, o->width * 4, WL_SHM_FORMAT_ARGB8888);
    wl_shm_pool_destroy(pool); close(fd);
    if (!b->proxy) { free(b); munmap(pixels, size); return; }
    b->output = o; b->pixels = pixels; b->size = size;
    wl_buffer_add_listener(b->proxy, &buffer_listener, b);
    o->buffers++; c->memory += size;
    cairo_surface_t *image = cairo_image_surface_create_for_data(pixels, CAIRO_FORMAT_ARGB32,
        o->width, o->height, o->width * 4);
    cairo_t *cr = cairo_create(image);
    cairo_set_operator(cr, CAIRO_OPERATOR_SOURCE);
    cairo_set_source_rgba(cr, 0.04, 0.06, 0.09, o->overlay ? 0.0 : (o->testing ? 0.96 : 1.0));
    cairo_paint(cr);
    cairo_set_operator(cr, CAIRO_OPERATOR_OVER);
    if (o->overlay) {
        cairo_set_line_width(cr, 3.0);
        cairo_set_source_rgb(cr, 1.0, 1.0, 0.0);
        for (int i = 0; i < 4; i++) {
            double x = o->points[i][0]*o->width, y = o->points[i][1]*o->height;
            if (i == 0) cairo_move_to(cr, x, y); else cairo_line_to(cr, x, y);
        }
        cairo_close_path(cr); cairo_stroke(cr);
        for (int i = 0; i < 4; i++) {
            double x = o->points[i][0]*o->width, y = o->points[i][1]*o->height;
            cairo_arc(cr, x, y, 9, 0, 6.283185307); cairo_stroke(cr);
            char label[8]; snprintf(label, sizeof label, "%d", i+1);
            line(cr, x+12, y+18, 20, label);
        }
        line(cr, 24, 32, 20, "Live calibration - output-space overlay");
    } else {
    double scale = o->width / 1280.0;
    if (scale < 0.35) scale = 0.35;
    cairo_scale(cr, scale, scale);
    cairo_select_font_face(cr, "sans", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_NORMAL);
    cairo_set_source_rgb(cr, 0.94, 0.96, 1.0);
    line(cr, 48, 90, 36, "Photo Wall");
    line(cr, 48, 150, 24, reason_text(o->reason));
    char detail[192];
    snprintf(detail, sizeof detail, "Output %s  |  %d x %d pixels", o->name, o->width, o->height);
    line(cr, 48, 215, 20, detail);
    line(cr, 48, 260, 20, "Frame binding unconfirmed");
    line(cr, 48, 305, 18, "Base display service - app content is independently supervised");
    line(cr, 48, 345, 18, "Panel brightness unavailable");
    }
    cairo_destroy(cr); cairo_surface_flush(image); cairo_surface_destroy(image);
    struct wp_presentation_feedback *feedback = wp_presentation_feedback(c->presentation, o->surface);
    wp_presentation_feedback_add_listener(feedback, &feedback_listener, o);
    pw_diagnostic_manager_v1_ack(c->manager, o->surface, o->serial);
    wl_surface_attach(o->surface, b->proxy, 0, 0);
    wl_surface_damage(o->surface, 0, 0, o->width, o->height);
    wl_surface_commit(o->surface);
    o->dirty = false;
}
static void output_configure(void *data, struct pw_diagnostic_manager_v1 *manager,
    const char *name, int32_t width, int32_t height, uint32_t serial, const char *reason, uint32_t testing) {
    struct client *c = data;
    struct output *o = NULL;
    for (int i = 0; i < MAX_OUTPUTS; i++) if (!strcmp(c->outputs[i].name, name)) { o = &c->outputs[i]; break; }
    if (!o) for (int i = 0; i < MAX_OUTPUTS; i++) if (!c->outputs[i].name[0]) { o = &c->outputs[i]; break; }
    if (!o || strlen(name) > 96 || strlen(reason) > 96) return;
    o->client = c;
    snprintf(o->name, sizeof o->name, "%s", name);
    snprintf(o->reason, sizeof o->reason, "%s", reason);
    o->width = width; o->height = height; o->serial = serial; o->testing = testing;
    o->dirty = true; o->overlay = false;
    if (!o->surface && c->compositor) {
        o->surface = wl_compositor_create_surface(c->compositor);
        pw_diagnostic_manager_v1_surface(manager, o->surface, name);
    }
    draw(o);
}
static void overlay_configure(void *data, struct pw_diagnostic_manager_v1 *manager,
    const char *name, uint32_t serial, const char *primitives, uint32_t visible) {
    (void)manager;
    struct client *c = data;
    struct output *o = NULL;
    for (int i = 0; i < MAX_OUTPUTS; i++) if (!strcmp(c->outputs[i].name, name)) { o = &c->outputs[i]; break; }
    if (!o || !visible || strlen(primitives) > 512) return;
    json_error_t error; json_t *points = json_loads(primitives, JSON_REJECT_DUPLICATES, &error);
    if (!json_is_array(points) || json_array_size(points) != 4) { json_decref(points); return; }
    for (int i = 0; i < 4; i++) for (int j = 0; j < 2; j++)
        o->points[i][j] = json_number_value(json_array_get(json_array_get(points, i), j));
    json_decref(points);
    o->serial = serial; o->overlay = true; o->dirty = true; draw(o);
}
static const struct pw_diagnostic_manager_v1_listener diagnostic_listener = {
    .output = output_configure, .overlay = overlay_configure};
static void global(void *data, struct wl_registry *registry, uint32_t name, const char *interface, uint32_t version) {
    (void)version;
    struct client *c = data;
    if (!strcmp(interface, "wl_compositor")) c->compositor = wl_registry_bind(registry, name, &wl_compositor_interface, 1);
    else if (!strcmp(interface, "wl_shm")) c->shm = wl_registry_bind(registry, name, &wl_shm_interface, 1);
    else if (!strcmp(interface, "wp_presentation")) c->presentation = wl_registry_bind(registry, name, &wp_presentation_interface, 1);
    else if (!strcmp(interface, "pw_diagnostic_manager_v1")) {
        c->manager = wl_registry_bind(registry, name, &pw_diagnostic_manager_v1_interface, 2);
        pw_diagnostic_manager_v1_add_listener(c->manager, &diagnostic_listener, c);
    }
}
static void global_remove(void *data, struct wl_registry *registry, uint32_t name) {
    (void)data; (void)registry; (void)name;
}
static const struct wl_registry_listener registry_listener = {.global = global, .global_remove = global_remove};
int main(void) {
    if (!getenv("WAYLAND_SOCKET")) return 2; /* never discover the app's public socket */
    struct client c = {0};
    c.display = wl_display_connect(NULL);
    if (!c.display) return 1;
    struct wl_registry *registry = wl_display_get_registry(c.display);
    wl_registry_add_listener(registry, &registry_listener, &c);
    if (wl_display_roundtrip(c.display) < 0 || wl_display_roundtrip(c.display) < 0 ||
        !c.compositor || !c.shm || !c.presentation || !c.manager) return 1;
    while (wl_display_dispatch(c.display) >= 0) {}
    wl_display_disconnect(c.display);
    return 1;
}
