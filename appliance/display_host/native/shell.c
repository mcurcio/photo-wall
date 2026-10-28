/* Base-owned Weston 14 shell. MIT; no Player/app-root imports.
 * Server-side wp_presentation events are the sole app presentation evidence.
 * A surface commit, frame callback, repaint request or ping answer is never that evidence.
 * Liveness and hold are two questions, each answered once (issue #123). Liveness is the app's
 * lease (LEASE_MS), renewed by a presented commit or by its answer to Weston's desktop ping, sent
 * once a second (PING_MS): a still picture commits nothing and keeps its grant while its process
 * answers; a hung or stopped one loses it (the grant goes). Hold is asked every lease_tick on the
 * compositor's own scene (`hold_evidence`): a released Output stays the app's only while a picture
 * plane of the app's is mapped with a buffer and a window alpha above 0, or Display draws on it
 * (the controller's `picture` op with `drawing`, or a live trial). Without either the holding slate
 * returns over the app (the grant kept) until one does; the app's ground and its frame tag are
 * never evidence, so an app that answers pings and shows nothing loses the screen.
 * The shell also serves the standard window alpha (wp_alpha_modifier_v1, `struct alpha`) and turns
 * an Output's screen on the controller's `turn` op (render redesign, .claude/runs/render-planes).
 * Weston's `--debug` allows every Output capture; the shell refuses the ones past Weston's memory
 * line (`capture_ask`, docs/display-host-backend.md#seeing-what-the-compositor-shows).
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <signal.h>
#include <time.h>
#include <unistd.h>
#include <jansson.h>
#include <wayland-server-core.h>
#include <libweston/libweston.h>
#include <libweston/desktop.h>
#include <libweston/shell-utils.h>
#include "photo-wall-frame-server.h"
#include "alpha-modifier-v1-server-protocol.h"

#define MAX_OUTPUTS 16
#define MAX_SURFACES 32
#define MAX_FEEDBACK 128
/* Feedbacks one app surface may hold in the table: a newer one takes the place of its oldest, so
 * a surface Weston does not show (its feedback unanswered until it shows again) never fills the
 * table for the others: MAX_SURFACES x 4 = MAX_FEEDBACK. */
#define FEEDBACK_PER_SURFACE 4
#define MAX_PACKET 16384
#define LEASE_MS 5000
#define PING_MS 1000         /* how often an app holding an Output is pinged */
#define FRAME_VERSION 4      /* pw_frame_manager_v1: v4 carries the picture (1c) */
#define DIAGNOSTIC_VERSION 6 /* pw_diagnostic_manager_v1: v4 the brightness line, v5 the Frame region (#124),
                                v6 Central's card */
#define MAX_REGION 1024      /* contracts.node_output.REGION_DOCUMENT_BYTES */
#define MAX_CENTRAL_CARD 1024 /* contracts.node_output.CENTRAL_CARD_BYTES */
#define MAX_OPERATIONS 1024  /* contracts.node_frame.PICTURE_OPERATIONS_BYTES (picture operations v5) */
#define MAX_BRIGHTNESS 128
#define MAX_PICTURE_CHANGE 20
/* The Output pixels one capturing client may capture in its life: appliance.kernel.capacity
 * CAPTURE_PIXELS (4096 x 2160), the capture Weston's memory line holds. */
#define CAPTURE_PIXELS 8847360

struct shell;
struct app_surface;
struct health;
struct output {
    struct shell *shell;
    struct weston_output *weston;
    char name[97];
    uint64_t generation;
    int mode_width, mode_height, mode_scale;
    uint32_t mode_refresh, mode_transform;
    struct weston_curtain *curtain, *fallback; /* fallback: amber while released and no live private client */
    struct weston_curtain *black; /* the controller's `black` op: opaque black above every view */
    bool black_on;                /* wanted by the controller; kept across a hot-plug of the Output */
    bool turned;                  /* the controller's `turn` op set `transform`; kept across a hot-plug */
    uint32_t transform;           /* wl_output.transform 0..3 (contracts/display_turn.py) */
    struct health *health;
    struct wl_listener destroyed;
    json_t *identity, *revision_identity, *trial_baseline;
    char revision_grant[37], revision_decision[37];
    uint64_t revision_until;
    char trial_json[4097], trial_id[37], trial_tag[71];
    uint64_t trial_generation, trial_sequence, trial_until, trial_hard_until;
    bool trial_terminal, overlay_visible;
    char overlay_buffer[64], overlay_frame_tag[97];
    char grant[37], handoff[37];
    uid_t uid;
    pid_t pid;
    uint64_t start_ticks, lease_until, last_presented, handoff_after, pinged;
    /* The run whose lease expired here (bench P2): offered again while it stays frozen, its slate
     * keeps the lease-lost reason, never "starting_new". Cleared by its handoff, another run's
     * candidate or any other invalidation. */
    pid_t lost_pid;
    uint64_t lost_ticks;
    struct { uint64_t serial, at; } recent_presented[128];
    unsigned recent_next;
    uint32_t diagnostic_serial;
    char reason[97];
    bool testing;
    /* The controller's latest `picture` op, forwarded verbatim and never parsed (1c). */
    char picture_operations[MAX_OPERATIONS + 1], brightness[MAX_BRIGHTNESS + 1];
    /* The controller's latest `region` op: the Frame region's canonical JSON, or empty (none);
     * forwarded verbatim to the private client, never parsed (#124). Kept across a hot-plug. */
    char region[MAX_REGION + 1];
    /* The controller's latest `central_card` op: Central's card's canonical JSON, or empty (none);
     * forwarded verbatim to the private client, never parsed. Dropped with the app's grant
     * (invalidate): a card never outlives the run it was composed for. */
    char central_card[MAX_CENTRAL_CARD + 1];
    struct app_surface *diagnostic;
    bool released;
    bool holding;   /* released and held by hold evidence; false: the holding slate is up */
    bool drawing;   /* the controller's latest `picture` op: Display draws a pattern or target */
    struct app_surface *app;
};
struct app_surface {
    int configured_width, configured_height;
    struct shell *shell;
    struct weston_desktop_surface *desktop;
    struct weston_view *view;
    struct output *output;
    uint64_t commit;
    bool diagnostic;
    uint32_t diagnostic_ack;
    struct wl_listener committed, destroyed;
    char pending_grant[37], pending_tag[97], pending_picture[MAX_PICTURE_CHANGE + 1];
    char overlay_grant[37], overlay_tag[97], primitives[513];
};
/* The private client's per-Output health surface: above app and slate, never touched by
 * handoff, invalidate() or the slate path. */
struct health {
    struct output *output;
    struct weston_view *view;
    struct wl_resource *resource;
    struct wl_listener committed, destroyed;
};
struct feedback {
    struct wl_client *client;
    uint32_t id;
    struct app_surface *surface;
    uint64_t commit, output_generation;
    uint32_t diagnostic_ack;
    char grant[37], tag[97], primitives[513], picture_change[MAX_PICTURE_CHANGE + 1];
};
struct shell {
    struct weston_compositor *compositor;
    struct weston_desktop *desktop;
    struct weston_layer app_layer, diagnostic_layer, health_layer, black_layer;
    struct output outputs[MAX_OUTPUTS];
    struct app_surface *surfaces[MAX_SURFACES];
    struct feedback feedbacks[MAX_FEEDBACK];
    struct wl_listener output_created, output_resized, destroyed;
    struct wl_protocol_logger *logger;
    struct wl_global *frame_global, *diagnostic_global, *alpha_global;
    struct wl_resource *frame_resources[MAX_SURFACES];
    struct wl_client *diagnostic_client;
    struct wl_resource *diagnostic_resource;
    struct wl_listener diagnostic_client_destroyed;
    /* The one client allowed to capture, the Output pixels it has captured, and its end. */
    struct wl_client *capture_holder;
    uint64_t capture_spent;
    struct wl_listener capture_ask, capture_holder_destroyed;
    pid_t diagnostic_pid;
    uint64_t next_diagnostic_start;
    struct wl_event_source *listen_source, *control_source, *timer;
    int listen_fd, control_fd;
    uint64_t serial;
    uid_t controller_uid;
    bool shutting_down;
    char boot[37], incarnation[37];
};

static uint64_t now_ms(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) < 0) abort();
    return (uint64_t)ts.tv_sec * 1000 + (uint64_t)ts.tv_nsec / 1000000;
}
static uint64_t boot_ms(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_BOOTTIME, &ts) < 0) abort();
    return (uint64_t)ts.tv_sec * 1000 + (uint64_t)ts.tv_nsec / 1000000;
}
static bool token(const char *s, size_t max) {
    if (!s || !*s || strlen(s) > max) return false;
    for (; *s; s++) if (!((*s >= 'a' && *s <= 'z') || (*s >= 'A' && *s <= 'Z') ||
        (*s >= '0' && *s <= '9') || *s == '-' || *s == '_' || *s == '.')) return false;
    return true;
}
static bool uuid(const char *s) {
    if (!s || strlen(s) != 36) return false;
    for (int i = 0; i < 36; i++) {
        if (i == 8 || i == 13 || i == 18 || i == 23) { if (s[i] != '-') return false; }
        else if (!((s[i] >= '0' && s[i] <= '9') || (s[i] >= 'a' && s[i] <= 'f'))) return false;
    }
    return true;
}
static bool read_uuid(const char *path, char out[37]) {
    FILE *f = fopen(path, "re");
    if (!f) return false;
    bool ok = fgets(out, 37, f) != NULL;
    fclose(f);
    return ok && uuid(out);
}
static uint64_t process_start(pid_t pid) {
    char path[64], buf[4096];
    snprintf(path, sizeof path, "/proc/%d/stat", pid);
    FILE *f = fopen(path, "re");
    if (!f) return 0;
    bool ok = fgets(buf, sizeof buf, f) != NULL;
    fclose(f);
    char *rest = ok ? strrchr(buf, ')') : NULL;
    if (!rest || rest[1] != ' ') return 0;
    char *save = NULL, *field = strtok_r(rest + 2, " ", &save);
    for (int n = 3; field && n < 22; n++) field = strtok_r(NULL, " ", &save);
    if (!field) return 0;
    return strtoull(field, NULL, 10);
}
static void send_event(struct shell *s, json_t *event) {
    char *wire = json_dumps(event, JSON_COMPACT | JSON_SORT_KEYS);
    json_decref(event);
    if (!wire) return;
    if (s->control_fd >= 0) {
        ssize_t n = send(s->control_fd, wire, strlen(wire), MSG_NOSIGNAL | MSG_DONTWAIT);
        /* A dropped event cannot be silently treated as a continuous stream. */
        if (n < 0 || (size_t)n != strlen(wire)) {
            if (s->control_source) wl_event_source_remove(s->control_source);
            s->control_source = NULL;
            close(s->control_fd);
            s->control_fd = -1;
        }
    }
    free(wire);
}
static json_t *output_key(struct output *o) {
    return json_pack("{s:s,s:s,s:s,s:I,s:I}",
        "kernel_boot_id", o->shell->boot, "display_host_incarnation", o->shell->incarnation,
        "output_id", o->name, "connection_generation", (json_int_t)o->generation,
        "mode_generation", (json_int_t)o->generation);
}
/* The output's own power (DPMS), as the controller's `output_power` op left it. */
static const char *output_power_state(struct output *o) {
    return o->weston && o->weston->power_state == WESTON_OUTPUT_POWER_FORCED_OFF ? "off" : "on";
}
/* `mode`: the mode Weston runs on the Output as the display is built (unturned), or null. */
static void output_event(struct output *o) {
    struct weston_mode *mode = o->weston ? o->weston->current_mode : NULL;
    send_event(o->shell, json_pack("{s:s,s:o,s:b,s:i,s:i,s:s,s:b,s:o}", "event", "output",
        "output", output_key(o), "connected", o->weston != NULL,
        "width", o->weston ? o->weston->width : 0,
        "height", o->weston ? o->weston->height : 0,
        "power", output_power_state(o), "black", o->black != NULL,
        "mode", mode ? json_pack("[i,i]", mode->width, mode->height) : json_null()));
}
/* The Output's stored Frame region to the private client (manager v5), verbatim. */
static void region_to(struct output *o) {
    struct wl_resource *r = o->shell->diagnostic_resource;
    if (r && wl_resource_get_version(r) >= 5)
        pw_diagnostic_manager_v1_send_region(r, o->name, o->region);
}
/* The Output's stored Central card to the private client (manager v6), verbatim. */
static void central_card_to(struct output *o) {
    struct wl_resource *r = o->shell->diagnostic_resource;
    if (r && wl_resource_get_version(r) >= 6)
        pw_diagnostic_manager_v1_send_central_card(r, o->name, o->central_card);
}
/* Every configure is followed by its region and Central's card, so the client lays the page out in
 * the right area and knows the card (none after an invalidation). */
static void diagnostic_configure(struct output *o) {
    if (!o->shell->diagnostic_resource) return;
    pw_diagnostic_manager_v1_send_output(o->shell->diagnostic_resource, o->name,
        o->weston ? o->weston->width : 0, o->weston ? o->weston->height : 0,
        ++o->diagnostic_serial, o->reason[0] ? o->reason : "app_absent", o->testing);
    region_to(o);
    central_card_to(o);
}
static void curtain(struct output *o, float alpha) {
    if (o->curtain) weston_shell_utils_curtain_destroy(o->curtain);
    o->curtain = NULL;
    if (!o->weston || o->shell->shutting_down) return;
    struct weston_curtain_params p = {
        .r = 0.04f, .g = 0.06f, .b = 0.09f, .a = alpha,
        .pos = o->weston->pos, .width = o->weston->width,
        .height = o->weston->height, .capture_input = true
    };
    o->curtain = weston_shell_utils_curtain_create(o->shell->compositor, &p);
    if (!o->curtain) abort(); /* compositor failure is output_unknown */
    weston_view_move_to_layer(o->curtain->view, &o->shell->diagnostic_layer.view_list);
    weston_view_set_output(o->curtain->view, o->weston);
    weston_surface_damage(o->curtain->view->surface);
}
/* The private client is alive: connected and bound to the manager at version 3 or later (the
 * health layer). It keeps its own layer unmapped while it has nothing to show and draws its own
 * amber when a page cannot be drawn (overlay/client.py HealthLayer), so liveness is the rule. */
static bool private_client_alive(struct shell *s) {
    return s->diagnostic_client && s->diagnostic_resource &&
        wl_resource_get_version(s->diagnostic_resource) >= 3;
}
/* Raised exactly while the private client is not alive, released or not: no debug overlay could
 * show then, so the amber covers the whole Output (issue #124). Never keyed on a mapped health
 * surface (render redesign S1). */
static void fallback_sync(struct output *o) {
    struct shell *s = o->shell;
    bool wanted = o->weston && !private_client_alive(s) && !s->shutting_down;
    if (!wanted && o->fallback) { weston_shell_utils_curtain_destroy(o->fallback); o->fallback = NULL; }
    if (!wanted || o->fallback) return;
    struct weston_curtain_params p = { /* RGBA (0.55, 0.35, 0, 0.5); Weston wants premultiplied */
        .r = 0.55f * 0.5f, .g = 0.35f * 0.5f, .b = 0.0f, .a = 0.5f, .pos = o->weston->pos,
        .width = o->weston->width, .height = o->weston->height, .capture_input = false
    };
    o->fallback = weston_shell_utils_curtain_create(s->compositor, &p);
    if (!o->fallback) abort(); /* compositor failure is output_unknown */
    weston_view_move_to_layer(o->fallback->view, &s->health_layer.view_list);
    weston_view_set_output(o->fallback->view, o->weston);
    weston_surface_damage(o->fallback->view->surface);
}
/* Black picture (2a, #83): an opaque black solid-colour view (never a shm buffer) above every other
 * view on the Output while the controller wants it; rebuilt at the Output's current size. */
static void black_sync(struct output *o) {
    struct shell *s = o->shell;
    if (o->black) { weston_shell_utils_curtain_destroy(o->black); o->black = NULL; }
    if (!o->black_on || !o->weston || s->shutting_down) return;
    struct weston_curtain_params p = {
        .r = 0.0f, .g = 0.0f, .b = 0.0f, .a = 1.0f, .pos = o->weston->pos,
        .width = o->weston->width, .height = o->weston->height, .capture_input = false
    };
    o->black = weston_shell_utils_curtain_create(s->compositor, &p);
    if (!o->black) abort(); /* compositor failure is output_unknown */
    weston_view_move_to_layer(o->black->view, &s->black_layer.view_list);
    weston_view_set_output(o->black->view, o->weston);
    weston_surface_damage(o->black->view->surface);
}
static void fallback_all(struct shell *s) {
    for (int i = 0; i < MAX_OUTPUTS; i++) if (s->outputs[i].name[0]) fallback_sync(&s->outputs[i]);
}
static bool client_matches(struct output *o, struct wl_client *client) {
    pid_t pid; uid_t uid; gid_t gid;
    wl_client_get_credentials(client, &pid, &uid, &gid);
    return pid == o->pid && uid == o->uid && process_start(pid) == o->start_ticks;
}
static void grant_identity_to(struct output *o, struct wl_resource *resource, uint32_t state,
                              json_t *identity, const char *grant) {
    if (!identity || !client_matches(o, wl_resource_get_client(resource))) return;
    char generation[32], revision[32];
    snprintf(generation, sizeof generation, "%"JSON_INTEGER_FORMAT,
        json_integer_value(json_object_get(identity, "binding_generation")));
    snprintf(revision, sizeof revision, "%"JSON_INTEGER_FORMAT,
        json_integer_value(json_object_get(identity, "config_revision")));
    if (wl_resource_get_version(resource) >= 3)
        pw_frame_manager_v1_send_grant_frame(resource, o->name, grant,
            json_string_value(json_object_get(identity, "frame_id")), generation, revision, state);
}
/* The Output's stored picture operations to one frame manager resource: version 4, the Output's
 * app client only, verbatim. */
static void picture_to(struct output *o, struct wl_resource *resource) {
    if (!o->picture_operations[0] || wl_resource_get_version(resource) < 4 || !o->identity ||
        !client_matches(o, wl_resource_get_client(resource))) return;
    pw_frame_manager_v1_send_picture(resource, o->name, o->picture_operations);
}
/* The Output's stored base page brightness line to the private client (manager v4). */
static void brightness_to(struct output *o) {
    struct wl_resource *r = o->shell->diagnostic_resource;
    if (r && o->brightness[0] && wl_resource_get_version(r) >= 4)
        pw_diagnostic_manager_v1_send_brightness(r, o->name, o->brightness);
}
static void grant_to(struct output *o, struct wl_resource *resource, uint32_t state) {
    grant_identity_to(o, resource, state, o->identity, o->grant);
    if (state != 2) picture_to(o, resource);   /* a newly granted or bound app gets the stored picture */
    if (state != 2 && o->revision_identity)
        grant_identity_to(o, resource, 3, o->revision_identity, o->revision_grant);
}
static void revision_clear(struct output *o, bool expired) {
    if (!o->revision_identity) return;
    for (int i = 0; i < MAX_SURFACES; i++) if (o->shell->frame_resources[i])
        grant_identity_to(o, o->shell->frame_resources[i], 2, o->revision_identity, o->revision_grant);
    if (expired) send_event(o->shell, json_pack("{s:s,s:O,s:s}", "event", "revision_expired",
        "identity", o->revision_identity, "grant_id", o->revision_grant));
    json_decref(o->revision_identity); o->revision_identity = NULL;
    o->revision_grant[0] = o->revision_decision[0] = 0; o->revision_until = 0;
}
static void grant_notify(struct output *o, uint32_t state) {
    for (int i = 0; i < MAX_SURFACES; i++) if (o->shell->frame_resources[i])
        grant_to(o, o->shell->frame_resources[i], state);
}
static void trial_notify(struct output *o) {
    for (int i = 0; i < MAX_SURFACES; i++) {
        struct wl_resource *r = o->shell->frame_resources[i];
        if (r && wl_resource_get_version(r) >= 2 && o->identity && client_matches(o, wl_resource_get_client(r)))
            pw_frame_manager_v1_send_trial(r, o->name, o->grant, o->trial_json);
    }
}
static void overlay_clear(struct output *o) {
    if (!o->overlay_visible) return;
    o->overlay_visible = false;
    if (o->released && o->holding && o->diagnostic) weston_surface_unmap(o->diagnostic->view->surface);
    if (o->shell->diagnostic_resource)
        pw_diagnostic_manager_v1_send_overlay(o->shell->diagnostic_resource, o->name,
            ++o->diagnostic_serial, "", 0);
}
static void trial_end(struct output *o) {
    if (!o->trial_json[0]) return;
    o->trial_json[0] = o->trial_tag[0] = 0; o->trial_terminal = true;
    trial_notify(o); overlay_clear(o);
}
static const char LEASE_LOST[] = "surface_lease_or_process_lost";
static void invalidate(struct output *o, const char *reason) {
    bool lost = o->identity && !strcmp(reason, LEASE_LOST);
    o->lost_pid = lost ? o->pid : 0;
    o->lost_ticks = lost ? o->start_ticks : 0;
    trial_end(o);
    revision_clear(o, true);
    grant_notify(o, 2);
    if (o->identity) send_event(o->shell, json_pack("{s:s,s:O,s:s}",
        "event", "invalidated", "identity", o->identity, "reason", reason));
    if (o->app) {
        weston_surface_unmap(o->app->view->surface);
        o->app->output = NULL;
    }
    o->app = NULL;
    json_decref(o->identity);
    o->identity = NULL;
    o->grant[0] = o->handoff[0] = 0;
    o->central_card[0] = 0;   /* Central's card was the ended run's: sent as none below */
    o->released = o->holding = false;
    o->testing = false;
    snprintf(o->reason, sizeof o->reason, "%s", reason);
    if (o->diagnostic) weston_surface_unmap(o->diagnostic->view->surface);
    diagnostic_configure(o);
    o->lease_until = o->last_presented = 0;
    memset(o->recent_presented, 0, sizeof o->recent_presented); o->recent_next = 0;
    curtain(o, 1.0f);
    fallback_sync(o);
}
static struct app_surface *find_surface(struct shell *s, struct wl_resource *r) {
    for (int i = 0; i < MAX_SURFACES; i++)
        if (s->surfaces[i] && s->surfaces[i]->view->surface->resource == r) return s->surfaces[i];
    return NULL;
}
/* The app surface whose commit a feedback asked on `r` reports: `r`'s own, or the one its
 * sub-surface tree hangs from. The Player's screens ask it on their topmost plane, what Weston
 * presents when the planes cover the app surface (Weston 14 answers no feedback of a covered
 * surface): frame-client.h pw_screen_present. The feedback is asked before that sub-surface's
 * commit, so the app surface's next commit is the one that applies it. */
static struct app_surface *feedback_surface(struct shell *s, struct wl_resource *r) {
    struct app_surface *a = find_surface(s, r);
    if (a || !r) return a;
    struct weston_surface *root = weston_surface_get_main_surface(wl_resource_get_user_data(r));
    return root && root->resource ? find_surface(s, root->resource) : NULL;
}
static bool matches_process(struct output *o, struct app_surface *a) {
    return client_matches(o, wl_resource_get_client(a->view->surface->resource));
}
static struct output *find_output(struct shell *s, const char *name) {
    for (int i = 0; i < MAX_OUTPUTS; i++)
        if (s->outputs[i].name[0] && name && !strcmp(s->outputs[i].name, name)) return &s->outputs[i];
    return NULL;
}
static void tag_frame(struct wl_client *client, struct wl_resource *resource,
                      struct wl_resource *surface, const char *grant, const char *tag) {
    (void)client;
    struct shell *s = wl_resource_get_user_data(resource);
    struct app_surface *a = find_surface(s, surface);
    if (!a || !uuid(grant) || !token(tag, 96)) {
        wl_resource_post_error(resource, 0, "invalid_frame_tag"); return;
    }
    snprintf(a->pending_grant, sizeof a->pending_grant, "%s", grant);
    snprintf(a->pending_tag, sizeof a->pending_tag, "%s", tag);
    a->pending_picture[0] = 0;   /* a v3 tag names no picture change: `presented` echoes null */
}
/* contracts.node_frame.picture_change_text: "0" or a decimal, at most 20 digits. */
static bool picture_change_token(const char *s) {
    if (!s || !*s || strlen(s) > MAX_PICTURE_CHANGE) return false;
    for (const char *c = s; *c; c++) if (*c < '0' || *c > '9') return false;
    return true;
}
static void tag_picture_frame(struct wl_client *client, struct wl_resource *resource,
    struct wl_resource *surface, const char *grant, const char *tag, const char *picture_change) {
    if (!picture_change_token(picture_change)) {
        wl_resource_post_error(resource, 0, "invalid_picture_change"); return;
    }
    tag_frame(client, resource, surface, grant, tag);
    struct app_surface *a = find_surface(wl_resource_get_user_data(resource), surface);
    if (a) snprintf(a->pending_picture, sizeof a->pending_picture, "%s", picture_change);
}
static bool bounded_primitives(const char *text) {
    if (!text || strlen(text) > 512) return false;
    json_error_t error;
    json_t *points = json_loads(text, JSON_REJECT_DUPLICATES, &error);
    bool ok = json_is_array(points) && json_array_size(points) == 4;
    for (size_t i = 0; ok && i < 4; i++) {
        json_t *point = json_array_get(points, i);
        ok = json_is_array(point) && json_array_size(point) == 2;
        for (size_t j = 0; ok && j < 2; j++) {
            json_t *value = json_array_get(point, j);
            double n = json_number_value(value);
            ok = json_is_number(value) && isfinite(n) && n >= 0 && n <= 1;
        }
    }
    json_decref(points); return ok;
}
static void overlay_frame(struct wl_client *client, struct wl_resource *resource,
    struct wl_resource *surface, const char *grant, const char *tag, const char *primitives) {
    (void)client;
    struct app_surface *a = find_surface(wl_resource_get_user_data(resource), surface);
    if (!a || !uuid(grant) || !token(tag, 96) || !bounded_primitives(primitives)) {
        wl_resource_post_error(resource, 0, "invalid_overlay_primitives"); return;
    }
    snprintf(a->overlay_grant, sizeof a->overlay_grant, "%s", grant);
    snprintf(a->overlay_tag, sizeof a->overlay_tag, "%s", tag);
    snprintf(a->primitives, sizeof a->primitives, "%s", primitives);
}
static void destroy_resource(struct wl_client *client, struct wl_resource *resource) {
    (void)client; wl_resource_destroy(resource);
}
static const struct pw_frame_manager_v1_interface frame_impl = {
    .destroy = destroy_resource, .tag = tag_frame, .overlay = overlay_frame,
    .tag_picture = tag_picture_frame
};
static void frame_resource_gone(struct wl_resource *r) {
    struct shell *s = wl_resource_get_user_data(r);
    for (int i = 0; i < MAX_SURFACES; i++) if (s->frame_resources[i] == r) s->frame_resources[i] = NULL;
}
static void bind_frame(struct wl_client *client, void *data, uint32_t version, uint32_t id) {
    (void)version;
    struct wl_resource *r = wl_resource_create(client, &pw_frame_manager_v1_interface,
        version < FRAME_VERSION ? version : FRAME_VERSION, id);
    if (!r) { wl_client_post_no_memory(client); return; }
    struct shell *s = data;
    wl_resource_set_implementation(r, &frame_impl, data, frame_resource_gone);
    for (int i = 0; i < MAX_SURFACES; i++) if (!s->frame_resources[i]) {
        s->frame_resources[i] = r;
        for (int j = 0; j < MAX_OUTPUTS; j++) grant_to(&s->outputs[j], r,
            s->outputs[j].released && !s->outputs[j].handoff[0] ? 1 : 0);
        return;
    }
    wl_resource_post_error(r, 0, "frame_manager_capacity");
}
static void diagnostic_committed(struct wl_listener *l, void *data) {
    (void)data;
    struct app_surface *a = wl_container_of(l, a, committed);
    struct output *o = a->output;
    if (!o || !o->weston || (o->released && o->holding && !o->overlay_visible) ||
        a->view->surface->width != o->weston->width ||
        a->view->surface->height != o->weston->height) return;
    weston_view_set_position(a->view, o->weston->pos);
    weston_view_set_output(a->view, o->weston);
    weston_view_set_mask(a->view, 0, 0, o->weston->width, o->weston->height);
    weston_view_move_to_layer(a->view, &o->shell->diagnostic_layer.view_list);
    weston_surface_map(a->view->surface);
    weston_view_update_transform(a->view);
    weston_surface_damage(a->view->surface);
    /* The server-generated curtain remains behind this protected text page. */
    if (o->curtain) weston_shell_utils_curtain_destroy(o->curtain);
    o->curtain = NULL;
}
static void diagnostic_surface_gone(struct wl_listener *l, void *data) {
    (void)data;
    struct app_surface *a = wl_container_of(l, a, destroyed);
    struct shell *s = a->shell;
    if (a->output) {
        a->output->diagnostic = NULL;
        if (!a->output->released || !a->output->holding) curtain(a->output, 1.0f);
    }
    for (int i = 0; i < MAX_FEEDBACK; i++) if (s->feedbacks[i].surface == a)
        memset(&s->feedbacks[i], 0, sizeof s->feedbacks[i]);
    for (int i = 0; i < MAX_SURFACES; i++) if (s->surfaces[i] == a) s->surfaces[i] = NULL;
    wl_list_remove(&a->committed.link);
    wl_list_remove(&a->destroyed.link);
    weston_view_destroy(a->view);
    free(a);
}
static void diagnostic_surface(struct wl_client *client, struct wl_resource *r,
                               struct wl_resource *surface_resource, const char *name) {
    struct shell *s = wl_resource_get_user_data(r);
    struct output *o = find_output(s, name);
    struct weston_surface *surface = wl_resource_get_user_data(surface_resource);
    if (client != s->diagnostic_client || !o || o->diagnostic ||
        weston_surface_set_role(surface, "photo-wall-diagnostic", r, 0) < 0) {
        wl_resource_post_error(r, 0, "invalid_diagnostic_surface"); return;
    }
    for (int i = 0; i < MAX_SURFACES; i++) if (!s->surfaces[i]) {
        struct app_surface *a = calloc(1, sizeof *a);
        if (!a) return;
        a->view = weston_view_create(surface);
        if (!a->view) { free(a); return; }
        a->shell = s; a->output = o; a->diagnostic = true;
        a->committed.notify = diagnostic_committed;
        a->destroyed.notify = diagnostic_surface_gone;
        wl_signal_add(&surface->commit_signal, &a->committed);
        wl_signal_add(&surface->destroy_signal, &a->destroyed);
        s->surfaces[i] = a; o->diagnostic = a;
        return;
    }
    wl_resource_post_error(r, 0, "diagnostic_surface_capacity");
}
static void diagnostic_ack(struct wl_client *client, struct wl_resource *r,
                           struct wl_resource *surface, uint32_t serial) {
    struct shell *s = wl_resource_get_user_data(r);
    struct app_surface *a = find_surface(s, surface);
    if (client != s->diagnostic_client || !a || !a->diagnostic || !a->output ||
        serial != a->output->diagnostic_serial) return;
    a->diagnostic_ack = serial;
}
static void health_free(struct health *h) {
    struct output *o = h->output;
    wl_list_remove(&h->committed.link);
    wl_list_remove(&h->destroyed.link);
    weston_surface_unmap(h->view->surface);
    weston_view_destroy(h->view);
    o->health = NULL;
    wl_resource_set_user_data(h->resource, NULL);
    free(h);
}
static void health_resource_gone(struct wl_resource *r) {
    struct health *h = wl_resource_get_user_data(r);
    if (h) health_free(h);
}
static void health_surface_gone(struct wl_listener *l, void *data) {
    (void)data;
    struct health *h = wl_container_of(l, h, destroyed);
    health_free(h);
}
static void health_committed(struct wl_listener *l, void *data) {
    (void)data;
    struct health *h = wl_container_of(l, h, committed);
    struct output *o = h->output;
    struct weston_surface *surface = h->view->surface;
    if (!o->weston || !weston_surface_has_content(surface)) {   /* a NULL buffer: nothing to show */
        weston_surface_unmap(surface); return;
    }
    weston_view_set_position(h->view, o->weston->pos);
    weston_view_set_output(h->view, o->weston);
    weston_view_set_mask(h->view, 0, 0, o->weston->width, o->weston->height);
    weston_view_move_to_layer(h->view, &o->shell->health_layer.view_list);
    weston_surface_map(surface);
    weston_view_update_transform(h->view);
    weston_surface_damage(surface);
}
static const struct pw_health_layer_v1_interface health_impl = { .destroy = destroy_resource };
static void get_health_layer(struct wl_client *client, struct wl_resource *r, uint32_t id,
                             struct wl_resource *surface_resource, const char *name) {
    struct shell *s = wl_resource_get_user_data(r);
    struct output *o = find_output(s, name);
    struct weston_surface *surface = wl_resource_get_user_data(surface_resource);
    if (o && o->health) { wl_resource_post_error(r, 0, "health_layer_exists"); return; }
    if (client != s->diagnostic_client || !o ||
        weston_surface_set_role(surface, "photo-wall-health", r, 0) < 0) {
        wl_resource_post_error(r, 0, "invalid_health_layer"); return;
    }
    struct health *h = calloc(1, sizeof *h);
    if (h) h->view = weston_view_create(surface);
    if (h && h->view) h->resource = wl_resource_create(client, &pw_health_layer_v1_interface, 1, id);
    if (!h || !h->view || !h->resource) {
        if (h && h->view) weston_view_destroy(h->view);
        free(h); wl_client_post_no_memory(client); return;
    }
    wl_resource_set_implementation(h->resource, &health_impl, h, health_resource_gone);
    h->output = o; o->health = h;
    h->committed.notify = health_committed;
    h->destroyed.notify = health_surface_gone;
    wl_signal_add(&surface->commit_signal, &h->committed);
    wl_signal_add(&surface->destroy_signal, &h->destroyed);
}
static const struct pw_diagnostic_manager_v1_interface diagnostic_impl = {
    .destroy = destroy_resource, .surface = diagnostic_surface, .ack = diagnostic_ack,
    .get_health_layer = get_health_layer
};
static void diagnostic_resource_gone(struct wl_resource *r) {
    struct shell *s = wl_resource_get_user_data(r); s->diagnostic_resource = NULL;
    fallback_all(s);
}
static void bind_diagnostic(struct wl_client *client, void *data, uint32_t version, uint32_t id) {
    (void)version;
    struct shell *s = data;
    if (client != s->diagnostic_client || s->diagnostic_resource) {
        wl_client_post_implementation_error(client, "private_diagnostic_role"); return;
    }
    struct wl_resource *r = wl_resource_create(client, &pw_diagnostic_manager_v1_interface,
        version < DIAGNOSTIC_VERSION ? version : DIAGNOSTIC_VERSION, id);
    if (!r) { wl_client_post_no_memory(client); return; }
    s->diagnostic_resource = r;
    wl_resource_set_implementation(r, &diagnostic_impl, s, diagnostic_resource_gone);
    for (int i = 0; i < MAX_OUTPUTS; i++) if (s->outputs[i].name[0]) {
        diagnostic_configure(&s->outputs[i]);
        brightness_to(&s->outputs[i]);
    }
    fallback_all(s);   /* alive at v3 or later: the amber goes */
}
/* Window alpha (wp_alpha_modifier_v1 v1, wayland-protocols staging): Weston 14 lacks it, so the shell
 * serves it to any client until Weston 16 does (then this goes, never two globals). One record per
 * surface. The factor is double-buffered as the protocol states: `set_multiplier` writes `pending`,
 * the client's wl_surface.commit request moves it to `cached` (protocol_log, which sees the request
 * before Weston handles it), and the surface's state being applied (its commit signal; for a
 * synchronized sub-surface, its parent's commit) sets every view of the surface to
 * weston_view_set_alpha(factor / UINT32_MAX). Destroying the object resets the factor to 1 at the next
 * commit: the record outlives its object until that commit is applied. */
struct alpha {
    struct weston_surface *surface;
    struct wl_resource *resource;   /* NULL once destroyed: the reset to 1 is still to be applied */
    uint32_t pending, cached, current;
    bool staged;                    /* a commit moved `pending` to `cached`, not applied yet */
    struct wl_listener applied, destroyed;
};
static void alpha_surface_gone(struct wl_listener *l, void *data);
static struct alpha *alpha_of(struct weston_surface *surface) {
    struct wl_listener *l = wl_signal_get(&surface->destroy_signal, alpha_surface_gone);
    struct alpha *a = NULL;
    return l ? wl_container_of(l, a, destroyed) : NULL;
}
static void alpha_free(struct alpha *a) {
    wl_list_remove(&a->applied.link);
    wl_list_remove(&a->destroyed.link);
    if (a->resource) wl_resource_set_user_data(a->resource, NULL);   /* later requests: no_surface */
    free(a);
}
static void alpha_surface_gone(struct wl_listener *l, void *data) {
    (void)data;
    struct alpha *a = wl_container_of(l, a, destroyed);
    alpha_free(a);
}
static void alpha_applied(struct wl_listener *l, void *data) {
    (void)data;
    struct alpha *a = wl_container_of(l, a, applied);
    if (a->staged) { a->current = a->cached; a->staged = false; }
    float factor = (float)((double)a->current / UINT32_MAX);
    struct weston_view *view;
    wl_list_for_each(view, &a->surface->views, surface_link)
        if (view->alpha != factor) weston_view_set_alpha(view, factor);
    if (!a->resource && a->current == UINT32_MAX) alpha_free(a);   /* the reset is applied */
}
/* protocol_log: the client's wl_surface.commit request takes the pending factor with it. */
static void alpha_commit_request(struct wl_resource *surface_resource) {
    struct weston_surface *surface = wl_resource_get_user_data(surface_resource);
    struct alpha *a = surface ? alpha_of(surface) : NULL;
    if (a) { a->cached = a->pending; a->staged = true; }
}
static void alpha_set_multiplier(struct wl_client *client, struct wl_resource *r, uint32_t factor) {
    (void)client;
    struct alpha *a = wl_resource_get_user_data(r);
    if (!a) {
        wl_resource_post_error(r, WP_ALPHA_MODIFIER_SURFACE_V1_ERROR_NO_SURFACE, "surface destroyed");
        return;
    }
    a->pending = factor;
}
static const struct wp_alpha_modifier_surface_v1_interface alpha_surface_impl = {
    .destroy = destroy_resource, .set_multiplier = alpha_set_multiplier
};
static void alpha_resource_gone(struct wl_resource *r) {
    struct alpha *a = wl_resource_get_user_data(r);
    if (!a) return;
    a->resource = NULL;
    a->pending = UINT32_MAX;   /* applied with the surface's next commit */
}
static void alpha_get_surface(struct wl_client *client, struct wl_resource *r, uint32_t id,
                              struct wl_resource *surface_resource) {
    struct weston_surface *surface = wl_resource_get_user_data(surface_resource);
    struct alpha *a = alpha_of(surface);
    if (a && a->resource) {
        wl_resource_post_error(r, WP_ALPHA_MODIFIER_V1_ERROR_ALREADY_CONSTRUCTED,
            "the surface already has an alpha modifier");
        return;
    }
    struct wl_resource *resource = wl_resource_create(client, &wp_alpha_modifier_surface_v1_interface,
        wl_resource_get_version(r), id);
    if (!resource) { wl_client_post_no_memory(client); return; }
    if (!a) {   /* else: a destroyed object's reset is pending; the new object takes its record */
        a = calloc(1, sizeof *a);
        if (!a) { wl_resource_destroy(resource); wl_client_post_no_memory(client); return; }
        a->surface = surface;
        a->pending = a->cached = a->current = UINT32_MAX;
        a->applied.notify = alpha_applied;
        a->destroyed.notify = alpha_surface_gone;
        wl_signal_add(&surface->commit_signal, &a->applied);
        wl_signal_add(&surface->destroy_signal, &a->destroyed);
    }
    a->resource = resource;
    wl_resource_set_implementation(resource, &alpha_surface_impl, a, alpha_resource_gone);
}
static const struct wp_alpha_modifier_v1_interface alpha_impl = {
    .destroy = destroy_resource, .get_surface = alpha_get_surface
};
static void bind_alpha(struct wl_client *client, void *data, uint32_t version, uint32_t id) {
    (void)version;   /* the global is version 1 */
    struct wl_resource *r = wl_resource_create(client, &wp_alpha_modifier_v1_interface, 1, id);
    if (!r) { wl_client_post_no_memory(client); return; }
    wl_resource_set_implementation(r, &alpha_impl, data, NULL);
}
/* Hold evidence (issue #123): what the compositor itself shows of a released Output's app. */
enum hold_evidence { HOLD_NONE = 0, HOLD_PICTURE_PLANE = 1, HOLD_DISPLAY_DRAWING = 2 };
/* A sub-surface of `surface`'s tree (never `surface` itself) that is mapped, has a buffer and a
 * window alpha above 0: a picture plane. Authored black is a black plane, so it holds. */
static bool picture_plane_below(struct weston_surface *surface) {
    struct weston_subsurface *sub;
    wl_list_for_each(sub, &surface->subsurface_list, parent_link) {
        if (sub->surface == surface) continue;   /* the parent's own entry in its list */
        struct alpha *a = alpha_of(sub->surface);
        if (weston_surface_is_mapped(sub->surface) && weston_surface_has_content(sub->surface) &&
            (!a || a->current > 0)) return true;
        if (picture_plane_below(sub->surface)) return true;
    }
    return false;
}
static enum hold_evidence hold_evidence(struct output *o) {
    if (o->app && picture_plane_below(o->app->view->surface)) return HOLD_PICTURE_PLANE;
    if (o->drawing || (o->trial_json[0] && boot_ms() < o->trial_until)) return HOLD_DISPLAY_DRAWING;
    return HOLD_NONE;
}
/* A released Output whose handoff completed: on hold loss the holding slate rises over the app
 * (curtain, then the private client's page for "hold_lost"), identity and grant kept; on its
 * return the slate goes. Each change, and each `tell` (the handoff completing), is a `hold` event.
 * A candidate, or an Output whose handoff is still to be presented, is not judged: its empty
 * candidate commits bring the handoff, and the app draws only once granted. */
static void hold_sync(struct output *o, bool tell) {
    if (!o->identity || !o->released || o->handoff[0] || !o->weston) return;
    enum hold_evidence evidence = hold_evidence(o);
    bool held = evidence != HOLD_NONE;
    if (held != o->holding) {
        o->holding = held;
        tell = true;
        if (held) {
            if (o->curtain) weston_shell_utils_curtain_destroy(o->curtain);
            o->curtain = NULL;
            if (o->diagnostic && !o->overlay_visible) weston_surface_unmap(o->diagnostic->view->surface);
        } else {
            o->testing = false;
            snprintf(o->reason, sizeof o->reason, "hold_lost");
            curtain(o, 1.0f);
            diagnostic_configure(o);
        }
    }
    if (tell) send_event(o->shell, json_pack("{s:s,s:o,s:b,s:s?}", "event", "hold",
        "output", output_key(o), "holding", held,
        "evidence", evidence == HOLD_PICTURE_PLANE ? "picture_plane" :
                    evidence == HOLD_DISPLAY_DRAWING ? "display_drawing" : NULL));
}
static void protocol_log(void *data, enum wl_protocol_logger_type direction,
                         const struct wl_protocol_logger_message *m) {
    struct shell *s = data;
    const char *class = wl_resource_get_class(m->resource), *name = m->message->name;
    struct wl_client *client = wl_resource_get_client(m->resource);
    if (direction == WL_PROTOCOL_LOGGER_REQUEST && !strcmp(class, "wp_presentation") &&
        !strcmp(name, "feedback") && m->arguments_count == 2) {
        struct app_surface *a = feedback_surface(s, (struct wl_resource *)m->arguments[0].o);
        if (!a) return;
        int vacant = -1, oldest = -1, held = 0;
        for (int i = 0; i < MAX_FEEDBACK; i++) {
            struct feedback *f = &s->feedbacks[i];
            if (!f->client) { if (vacant < 0) vacant = i; continue; }
            if (f->surface != a) continue;
            held++;
            if (oldest < 0 || (f->commit && (!s->feedbacks[oldest].commit || f->commit < s->feedbacks[oldest].commit)))
                oldest = i;
        }
        int slot = held >= FEEDBACK_PER_SURFACE ? oldest : vacant;
        if (slot >= 0)
            s->feedbacks[slot] = (struct feedback){.client = client, .id = m->arguments[1].n, .surface = a};
        return;
    }
    if (direction == WL_PROTOCOL_LOGGER_REQUEST && !strcmp(class, "wl_surface") && !strcmp(name, "commit")) {
        alpha_commit_request(m->resource);
        struct app_surface *a = find_surface(s, m->resource);
        if (!a) return;
        a->commit = ++s->serial;
        for (int i = 0; i < MAX_FEEDBACK; i++) {
            struct feedback *f = &s->feedbacks[i];
            if (f->surface != a || f->commit) continue;
            f->commit = a->commit;
            f->output_generation = a->output ? a->output->generation : 0;
            f->diagnostic_ack = a->diagnostic_ack;
            memcpy(f->grant, a->pending_grant, sizeof f->grant);
            if (!strcmp(a->overlay_grant, a->pending_grant) && !strcmp(a->overlay_tag, a->pending_tag))
                memcpy(f->primitives, a->primitives, sizeof f->primitives);
            memcpy(f->tag, a->pending_tag, sizeof f->tag);
            memcpy(f->picture_change, a->pending_picture, sizeof f->picture_change);
        }
        a->pending_grant[0] = a->pending_tag[0] = a->pending_picture[0] = 0;
        a->overlay_grant[0] = a->overlay_tag[0] = a->primitives[0] = 0;
        a->diagnostic_ack = 0;
        return;
    }
    if (direction != WL_PROTOCOL_LOGGER_EVENT || strcmp(class, "wp_presentation_feedback")) return;
    if (strcmp(name, "presented") && strcmp(name, "discarded")) return;
    for (int i = 0; i < MAX_FEEDBACK; i++) {
        struct feedback *f = &s->feedbacks[i];
        if (f->client != client || f->id != wl_resource_get_id(m->resource)) continue;
        struct app_surface *a = f->surface;
        struct output *o = a ? a->output : NULL;
        if (!strcmp(name, "presented") && o && a->diagnostic && o->diagnostic == a &&
            f->commit == a->commit && f->output_generation == o->generation &&
            f->diagnostic_ack == o->diagnostic_serial && !o->released) {
            send_event(s, json_pack("{s:s,s:o,s:i}", "event", "diagnostic_presented",
                "output", output_key(o), "serial", (int)o->diagnostic_serial));
        }
        if (!strcmp(name, "presented") && o && a->diagnostic && o->diagnostic == a &&
            f->output_generation == o->generation && f->diagnostic_ack == o->diagnostic_serial &&
            o->released && o->overlay_visible) {
            send_event(s, json_pack("{s:s,s:o,s:i,s:s,s:s}", "event", "overlay_presented",
                "output", output_key(o), "serial", (int)o->diagnostic_serial,
                "buffer_id", o->overlay_buffer, "frame_tag", o->overlay_frame_tag));
        }
        if (!strcmp(name, "presented") && o && !a->diagnostic && o->identity && o->app == a &&
            f->commit && f->commit > o->last_presented && f->output_generation == o->generation &&
            (!strcmp(f->grant, o->grant) || (o->revision_identity &&
             now_ms() < o->revision_until && !strcmp(f->grant, o->revision_grant))) &&
            token(f->tag, 96) && matches_process(o, a)) {
            if (o->revision_identity && !strcmp(f->grant, o->revision_grant)) {
                trial_end(o);
                /* The explicit revision authorization adopts only on this real presentation. */
                grant_notify(o, 2);
                json_decref(o->identity); o->identity = o->revision_identity;
                o->revision_identity = NULL;
                memcpy(o->grant, o->revision_grant, sizeof o->grant);
                memset(o->recent_presented, 0, sizeof o->recent_presented); o->recent_next = 0;
                send_event(s, json_pack("{s:s,s:O,s:s,s:s}", "event", "revision_adopted",
                    "identity", o->identity, "grant_id", o->grant,
                    "decision_id", o->revision_decision));
                o->revision_grant[0] = o->revision_decision[0] = 0; o->revision_until = 0;
                grant_notify(o, 1);
            }
            o->lease_until = now_ms() + LEASE_MS;
            o->last_presented = f->commit;
            o->recent_presented[o->recent_next].serial = f->commit;
            o->recent_presented[o->recent_next].at = now_ms();
            o->recent_next = (o->recent_next + 1) % 128;
            char buffer[64]; snprintf(buffer, sizeof buffer, "weston-%"PRIu64, f->commit);
            send_event(s, json_pack("{s:s,s:O,s:s,s:s,s:s,s:I,s:s?}", "event", "presented",
                "identity", o->identity, "grant_id", o->grant, "buffer_id", buffer,
                "frame_tag", f->tag, "observed_monotonic_ms", (json_int_t)now_ms(),
                "picture_change", f->picture_change[0] ? f->picture_change : NULL));
            if (o->trial_json[0] && boot_ms() < o->trial_until &&
                !strcmp(f->tag, o->trial_tag) && f->primitives[0] && s->diagnostic_resource) {
                o->overlay_visible = true;
                snprintf(o->overlay_buffer, sizeof o->overlay_buffer, "%s", buffer);
                snprintf(o->overlay_frame_tag, sizeof o->overlay_frame_tag, "%s", f->tag);
                pw_diagnostic_manager_v1_send_overlay(s->diagnostic_resource, o->name,
                    ++o->diagnostic_serial, f->primitives, 1);
            } else overlay_clear(o);
            /* Only presentation after the protected layer removal completes handoff. */
            if (o->handoff[0] && o->released && f->commit > o->handoff_after) {
                send_event(s, json_pack("{s:s,s:O,s:s}", "event", "handoff_presented",
                    "identity", o->identity, "handoff_id", o->handoff));
                o->handoff[0] = 0;
                grant_notify(o, 1);
                hold_sync(o, true);   /* the empty candidate commit holds nothing: slate till a plane */
            }
        }
        memset(f, 0, sizeof *f);
        return;
    }
}
static void surface_added(struct weston_desktop_surface *d, void *data) {
    struct shell *s = data;
    for (int i = 0; i < MAX_SURFACES; i++) if (!s->surfaces[i]) {
        struct app_surface *a = calloc(1, sizeof *a);
        if (!a) return;
        a->shell = s; a->desktop = d;
        a->view = weston_desktop_surface_create_view(d);
        if (!a->view) { free(a); return; }
        s->surfaces[i] = a;
        weston_desktop_surface_set_user_data(d, a);
        /* Configure even an unauthorized toplevel, but never map it. Without
         * this initial configure a well-behaved xdg client cannot attach. */
        const char *id = weston_desktop_surface_get_app_id(d);
        struct output *o = id && !strncmp(id, "photo-wall-", 11) ? find_output(s, id + 11) : NULL;
        if (o && o->weston) {
            a->configured_width = o->weston->width; a->configured_height = o->weston->height;
            weston_desktop_surface_set_size(d, a->configured_width, a->configured_height);
            weston_desktop_surface_set_fullscreen(d, true);
        } else {
            weston_desktop_surface_set_size(d, 1, 1);
        }
        return;
    }
    weston_desktop_surface_close(d);
}
static void surface_removed(struct weston_desktop_surface *d, void *data) {
    struct shell *s = data;
    struct app_surface *a = weston_desktop_surface_get_user_data(d);
    if (!a) return;
    if (a->output) invalidate(a->output, "surface_destroyed");
    for (int i = 0; i < MAX_FEEDBACK; i++) if (s->feedbacks[i].surface == a)
        memset(&s->feedbacks[i], 0, sizeof s->feedbacks[i]);
    for (int i = 0; i < MAX_SURFACES; i++) if (s->surfaces[i] == a) s->surfaces[i] = NULL;
    weston_desktop_surface_unlink_view(a->view);
    weston_view_destroy(a->view);
    free(a);
}
static void committed(struct weston_desktop_surface *d, struct weston_coord_surface offset, void *data) {
    (void)offset;
    struct shell *s = data;
    struct app_surface *a = weston_desktop_surface_get_user_data(d);
    const char *id = weston_desktop_surface_get_app_id(d);
    if (!a || !id || strncmp(id, "photo-wall-", 11)) return;
    struct output *o = find_output(s, id + 11);
    if (!o || !o->weston || !o->identity || (o->app && o->app != a) || !matches_process(o, a)) return;
    /* GTK sets the per-Output app ID after its first map/configure. Configure
     * once on target resolution or dimensions changing, never on every commit. */
    if (a->configured_width != o->weston->width || a->configured_height != o->weston->height) {
        a->configured_width = o->weston->width; a->configured_height = o->weston->height;
        weston_desktop_surface_set_size(d, a->configured_width, a->configured_height);
        weston_desktop_surface_set_fullscreen(d, true);
    }
    struct weston_surface *surface = a->view->surface;
    if (surface->width != o->weston->width || surface->height != o->weston->height) return;
    a->output = o; o->app = a;
    weston_view_set_position(a->view, o->weston->pos);
    weston_view_set_output(a->view, o->weston);
    weston_view_set_mask(a->view, 0, 0, o->weston->width, o->weston->height);
    weston_view_move_to_layer(a->view, &s->app_layer.view_list);
    weston_surface_map(surface);
    weston_view_update_transform(a->view);
    weston_surface_damage(surface);
}
static void set_parent(struct weston_desktop_surface *d, struct weston_desktop_surface *parent, void *data) {
    (void)parent; (void)data;
    weston_desktop_surface_close(d); /* no popups/transients may bypass a slot */
}
/* The app answered Weston's ping: it is alive, so each Output whose app surface it owns, with a
 * matching process, keeps its lease. Liveness only: never presentation or hold evidence. */
static void pong(struct weston_desktop_client *client, void *data) {
    struct shell *s = data;
    for (int i = 0; i < MAX_OUTPUTS; i++) {
        struct output *o = &s->outputs[i];
        if (o->identity && o->app &&
            weston_desktop_surface_get_client(o->app->desktop) == client && matches_process(o, o->app))
            o->lease_until = now_ms() + LEASE_MS;
    }
}
static const struct weston_desktop_api desktop_api = {
    .struct_size = sizeof(struct weston_desktop_api), .surface_added = surface_added,
    .surface_removed = surface_removed, .committed = committed, .set_parent = set_parent,
    .pong = pong
};
static void record_mode(struct output *o);
/* The Output's mode (size, refresh, scale or transform) changed: the app's grant goes and the
 * controller hears of the Output under its new key. */
static void remode(struct output *o) {
    invalidate(o, "output_mode_changed"); o->generation++; record_mode(o); black_sync(o);
    diagnostic_configure(o); output_event(o);
}
static bool mode_changed(struct output *o);
/* The screen turn the controller wants, on the Output's current weston_output (a hot-plugged Output
 * gets it back, as black_on). */
static void turn_sync(struct output *o) {
    if (o->turned && o->weston && o->weston->transform != o->transform)
        weston_output_set_transform(o->weston, o->transform);
}
static bool same_key(struct output *o, json_t *key) {
    json_t *expected = output_key(o);
    bool equal = json_equal(expected, key);
    json_decref(expected);
    return equal;
}
static const char *string(json_t *j, const char *key) { return json_string_value(json_object_get(j, key)); }
static bool command(struct shell *s, json_t *j) {
    const char *op = string(j, "op");
    struct output *o = find_output(s, string(j, "output_id"));
    if (!op || !o || !o->weston || !same_key(o, json_object_get(j, "output"))) return false;
    if (!strcmp(op, "diagnostic")) { invalidate(o, "authorized_withdrawal"); return true; }
    /* Signal off (1b D2): DPMS off/on on the named output, never disabling it, so the output,
     * its views and the app's grant stay; base-only, like every op on this socket. */
    if (!strcmp(op, "output_power")) {
        const char *power = string(j, "power");
        if (!power || (strcmp(power, "on") && strcmp(power, "off"))) return false;
        if (!strcmp(power, "off")) weston_output_power_off(o->weston);
        else weston_output_power_on(o->weston);
        send_event(s, json_pack("{s:s,s:s,s:s}", "event", "output_power",
            "output_id", o->name, "power", output_power_state(o)));
        return true;
    }
    /* Black picture (2a): the curtain up or down; the `black` event says which, ahead of the response. */
    if (!strcmp(op, "black")) {
        json_t *on = json_object_get(j, "on");
        if (!json_is_boolean(on)) return false;
        o->black_on = json_is_true(on);
        black_sync(o);
        send_event(s, json_pack("{s:s,s:s,s:b}", "event", "black", "output_id", o->name, "on", o->black != NULL));
        return true;
    }
    /* The screen turn (render redesign S1): wl_output.transform 0..3 (contracts/display_turn.py
     * wayland_transform), every client told by Weston; a quarter turn swaps the Output's size, a mode
     * change like any other. The `turn` event says the transform Weston has, ahead of the response. */
    if (!strcmp(op, "turn")) {
        json_t *transform = json_object_get(j, "transform");
        if (!json_is_integer(transform) || json_integer_value(transform) < 0 ||
            json_integer_value(transform) > 3) return false;
        o->turned = true; o->transform = (uint32_t)json_integer_value(transform);
        turn_sync(o);
        if (mode_changed(o)) remode(o);
        send_event(s, json_pack("{s:s,s:s,s:i}", "event", "turn", "output_id", o->name,
            "transform", (int)o->weston->transform));
        return true;
    }
    /* Picture (1c P2): the controller's operations for the Output's app and its base page line for
     * the private client, stored and forwarded verbatim; the shell never parses `operations`.
     * `drawing` (required, issue #123): Display draws a pattern or target, which holds the Output. */
    if (!strcmp(op, "picture")) {
        const char *operations = string(j, "operations"), *brightness = string(j, "brightness");
        json_t *drawing = json_object_get(j, "drawing");
        if (!operations || !*operations || strlen(operations) > MAX_OPERATIONS ||
            !brightness || strlen(brightness) > MAX_BRIGHTNESS || !json_is_boolean(drawing)) return false;
        o->drawing = json_is_true(drawing);
        snprintf(o->picture_operations, sizeof o->picture_operations, "%s", operations);
        snprintf(o->brightness, sizeof o->brightness, "%s", brightness);
        for (int i = 0; i < MAX_SURFACES; i++) if (s->frame_resources[i]) picture_to(o, s->frame_resources[i]);
        brightness_to(o);
        return true;
    }
    /* The Frame region (#124): canonical JSON of contracts.node_output.FrameRegion, or "" (none),
     * stored and forwarded verbatim to the private client; the shell never interprets geometry. */
    if (!strcmp(op, "region")) {
        const char *region = string(j, "region");
        if (!region || strlen(region) > MAX_REGION) return false;
        snprintf(o->region, sizeof o->region, "%s", region);
        region_to(o);
        return true;
    }
    /* Central's card: canonical JSON of contracts.node_output.CentralCard, or "" (none), composed
     * by the controller, stored and forwarded verbatim; the shell never reads it (no verdict, no
     * code). */
    if (!strcmp(op, "central_card")) {
        const char *card = string(j, "card");
        if (!card || strlen(card) > MAX_CENTRAL_CARD) return false;
        snprintf(o->central_card, sizeof o->central_card, "%s", card);
        central_card_to(o);
        return true;
    }
    if (!strcmp(op, "withdraw")) {
        json_t *identity = json_object_get(j, "identity");
        const char *decision = string(j, "decision_id");
        if (!o->identity || !json_equal(identity, o->identity) || !uuid(decision)) return false;
        json_t *removed = json_deep_copy(o->identity);
        invalidate(o, "authorized_withdrawal");
        /* This confirms the old role was actually unmapped, not that a command
         * was received or the replacement diagnostic has physically scanned out. */
        send_event(s, json_pack("{s:s,s:o,s:s}", "event", "role_removed",
            "identity", removed, "decision_id", decision));
        return true;
    }
    if (!strcmp(op, "candidate")) {
        json_t *identity = json_object_get(j, "identity"), *process = json_object_get(identity, "process");
        const char *grant = string(j, "grant_id");
        json_int_t pid = json_integer_value(json_object_get(process, "pid"));
        json_int_t ticks = json_integer_value(json_object_get(process, "start_ticks"));
        json_int_t uid = json_integer_value(json_object_get(j, "uid"));
        if (!token(string(identity, "frame_id"), 96) || o->identity || !uuid(grant) || !uuid(string(process, "invocation_id")) ||
            !same_key(o, json_object_get(identity, "output")) || pid <= 0 || pid > INT32_MAX ||
            ticks <= 0 || uid <= 0 || uid > UINT32_MAX || process_start((pid_t)pid) != (uint64_t)ticks)
            return false;
        o->identity = json_deep_copy(identity);
        snprintf(o->grant, sizeof o->grant, "%s", grant);
        o->pid = (pid_t)pid; o->uid = (uid_t)uid; o->start_ticks = (uint64_t)ticks;
        o->lease_until = now_ms() + LEASE_MS;
        /* Synthetic test content only: starting_new authorization is required
         * upstream. The translucent cover permits genuine compositor feedback. A run whose lease
         * expired here and is offered again (Central sees no candidate and none admitted) is
         * still taken, so a run that resumes can hand off, but its slate keeps saying it
         * stopped: it is not starting. */
        o->testing = true;
        bool relost = o->lost_pid == (pid_t)pid && o->lost_ticks == (uint64_t)ticks;
        if (!relost) { o->lost_pid = 0; o->lost_ticks = 0; }
        snprintf(o->reason, sizeof o->reason, "%s", relost ? LEASE_LOST : "starting_new");
        if (o->diagnostic) weston_surface_unmap(o->diagnostic->view->surface);
        curtain(o, 0.96f);
        diagnostic_configure(o);
        grant_notify(o, 0);
        return true;
    }
    if (!strcmp(op, "trial_end")) { trial_end(o); return true; }
    if (!strcmp(op, "trial")) {
        const char *text = string(j, "candidate");
        if (!text || strlen(text) > 4096 || !o->identity || !o->released || o->handoff[0]) return false;
        json_error_t error;
        json_t *trial = json_loads(text, JSON_REJECT_DUPLICATES, &error);
        const char *id = string(trial, "trial_id"), *hash = string(trial, "candidate_sha256");
        json_int_t generation = json_integer_value(json_object_get(trial, "generation"));
        json_int_t sequence = json_integer_value(json_object_get(trial, "sequence"));
        json_int_t until = json_integer_value(json_object_get(trial, "expires_boottime_ms"));
        json_int_t hard = json_integer_value(json_object_get(trial, "hard_expires_boottime_ms"));
        bool same = id && !strcmp(id, o->trial_id) && generation == (json_int_t)o->trial_generation;
        bool ok = uuid(id) && hash && strlen(hash) == 64 && strspn(hash,"0123456789abcdef") == 64 &&
            generation > 0 && sequence > 0 && until > (json_int_t)boot_ms() && hard >= until &&
            hard <= (json_int_t)boot_ms() + 120000 &&
            json_equal(json_object_get(trial, "baseline"), o->identity) &&
            ((!json_equal(o->trial_baseline, o->identity)) || (same && !o->trial_terminal && sequence >= (json_int_t)o->trial_sequence &&
              hard <= (json_int_t)o->trial_hard_until) || generation > (json_int_t)o->trial_generation);
        if (ok && same && sequence == (json_int_t)o->trial_sequence && o->trial_tag[0])
            ok = !strcmp(hash, o->trial_tag + 6);
        if (ok) {
            if (!same || sequence != (json_int_t)o->trial_sequence) overlay_clear(o);
            snprintf(o->trial_json, sizeof o->trial_json, "%s", text);
            snprintf(o->trial_id, sizeof o->trial_id, "%s", id);
            snprintf(o->trial_tag, sizeof o->trial_tag, "trial-%s", hash);
            json_decref(o->trial_baseline); o->trial_baseline = json_deep_copy(o->identity);
            o->trial_generation = (uint64_t)generation; o->trial_sequence = (uint64_t)sequence;
            o->trial_until = (uint64_t)until; o->trial_hard_until = (uint64_t)hard; o->trial_terminal = false;
            trial_notify(o);
        }
        json_decref(trial); return ok;
    }
    if (!strcmp(op, "revision")) {
        json_t *identity = json_object_get(j, "identity");
        if (!token(string(identity, "frame_id"), 96)) return false;
        const char *grant = string(j, "grant_id"), *decision = string(j, "decision_id");
        json_int_t ttl = json_integer_value(json_object_get(j, "ttl_ms"));
        if (!o->identity || !o->released || o->handoff[0] || o->revision_identity ||
            !uuid(grant) || !uuid(decision) || ttl <= 0 || ttl > LEASE_MS ||
            !same_key(o, json_object_get(identity, "output")) ||
            !json_equal(json_object_get(identity, "process"), json_object_get(o->identity, "process")) ||
            !json_equal(json_object_get(identity, "app_epoch"), json_object_get(o->identity, "app_epoch")) ||
            !json_equal(json_object_get(identity, "frame_id"), json_object_get(o->identity, "frame_id")) ||
            !json_equal(json_object_get(identity, "binding_generation"), json_object_get(o->identity, "binding_generation")) ||
            !json_is_integer(json_object_get(identity, "config_revision")) ||
            json_integer_value(json_object_get(identity, "config_revision")) <=
                json_integer_value(json_object_get(o->identity, "config_revision")) ||
            !o->app || !matches_process(o, o->app)) return false;
        o->revision_identity = json_deep_copy(identity);
        snprintf(o->revision_grant, sizeof o->revision_grant, "%s", grant);
        snprintf(o->revision_decision, sizeof o->revision_decision, "%s", decision);
        o->revision_until = now_ms() + (uint64_t)ttl;
        grant_notify(o, 1);
        return true;
    }
    if (!strcmp(op, "handoff")) {
        const char *grant = string(j, "grant_id"), *handoff = string(j, "handoff_id");
        const char *buffer = string(j, "buffer_id");
        bool recent = false;
        for (unsigned i = 0; buffer && i < 128; i++) {
            char expected[64];
            snprintf(expected, sizeof expected, "weston-%"PRIu64, o->recent_presented[i].serial);
            if (o->recent_presented[i].serial && !strcmp(buffer, expected) &&
                now_ms() - o->recent_presented[i].at < LEASE_MS) { recent = true; break; }
        }
        if (!o->identity || !grant || strcmp(grant, o->grant) || !uuid(handoff) ||
            !recent || !o->last_presented || now_ms() >= o->lease_until ||
            !o->app || !matches_process(o, o->app)) return false;
        if (o->curtain) weston_shell_utils_curtain_destroy(o->curtain);
        o->curtain = NULL; o->released = o->holding = true;   /* judged once the handoff is presented */
        o->lost_pid = 0; o->lost_ticks = 0;                    /* the run answers again */
        o->handoff_after = s->serial;
        if (o->diagnostic) weston_surface_unmap(o->diagnostic->view->surface);
        snprintf(o->handoff, sizeof o->handoff, "%s", handoff);
        weston_surface_damage(o->app->view->surface);
        fallback_sync(o);
        return true;
    }
    return false;
}
static int control_read(int fd, uint32_t mask, void *data) {
    struct shell *s = data;
    if (mask & (WL_EVENT_HANGUP | WL_EVENT_ERROR)) goto closed;
    char buf[MAX_PACKET + 1];
    ssize_t n = recv(fd, buf, sizeof buf, MSG_DONTWAIT | MSG_TRUNC);
    if (n < 0 && (errno == EAGAIN || errno == EINTR)) return 0;
    if (n <= 0 || n > MAX_PACKET) goto closed;
    json_error_t error;
    json_t *j = json_loadb(buf, (size_t)n, JSON_REJECT_DUPLICATES, &error);
    const char *request = j ? string(j, "request_id") : NULL;
    bool accepted = request && uuid(request) && command(s, j);
    send_event(s, json_pack("{s:s,s:s,s:b}", "event", "response", "request_id",
        request ? request : "invalid", "accepted", accepted));
    json_decref(j);
    return 0;
closed:
    if (s->control_source) wl_event_source_remove(s->control_source);
    s->control_source = NULL;
    close(s->control_fd); s->control_fd = -1;
    return 0;
}
static int accept_control(int fd, uint32_t mask, void *data) {
    (void)mask;
    struct shell *s = data;
    int peer = accept4(fd, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
    if (peer < 0) return 0;
    struct ucred cred; socklen_t size = sizeof cred;
    if (getsockopt(peer, SOL_SOCKET, SO_PEERCRED, &cred, &size) < 0 ||
        cred.uid != s->controller_uid || s->control_fd >= 0) { close(peer); return 0; }
    s->control_fd = peer;
    s->control_source = wl_event_loop_add_fd(wl_display_get_event_loop(s->compositor->wl_display),
        peer, WL_EVENT_READABLE, control_read, s);
    send_event(s, json_pack("{s:s,s:i,s:s,s:s}", "event", "hello", "protocol", 1,
        "boot_id", s->boot, "incarnation_id", s->incarnation));
    for (int i = 0; i < MAX_OUTPUTS; i++) if (s->outputs[i].name[0]) {
        struct output *o = &s->outputs[i];
        invalidate(o, "controller_restarted");
        o->generation++;
        output_event(o);
        diagnostic_configure(o);
    }
    return 0;
}
static void output_destroyed(struct wl_listener *l, void *data) {
    (void)data;
    struct output *o = wl_container_of(l, o, destroyed);
    wl_list_remove(&o->destroyed.link);
    o->weston = NULL;
    if (o->health) weston_surface_unmap(o->health->view->surface); /* re-maps on its next commit */
    invalidate(o, "output_disconnected"); /* drops o->fallback: no Output */
    black_sync(o); /* drops o->black: no Output; black_on stays for its return */
    o->generation++;
    output_event(o);
}
static void record_mode(struct output *o) {
    struct weston_output *w = o->weston;
    o->mode_width = w->width; o->mode_height = w->height;
    o->mode_refresh = w->current_mode ? w->current_mode->refresh : 0;
    o->mode_scale = w->current_scale; o->mode_transform = w->transform;
}
static bool mode_changed(struct output *o) {
    struct weston_output *w = o->weston;
    return w && (o->mode_width != w->width || o->mode_height != w->height ||
        o->mode_refresh != (w->current_mode ? w->current_mode->refresh : 0) ||
        o->mode_scale != w->current_scale || o->mode_transform != w->transform);
}
static void add_output(struct shell *s, struct weston_output *w) {
    struct output *o = find_output(s, w->name);
    if (!token(w->name, 96)) return;
    if (!o) for (int i = 0; i < MAX_OUTPUTS; i++) if (!s->outputs[i].name[0]) { o = &s->outputs[i]; break; }
    if (!o) return;
    o->shell = s; o->weston = w; o->generation++;
    turn_sync(o);
    record_mode(o);
    snprintf(o->name, sizeof o->name, "%s", w->name);
    o->destroyed.notify = output_destroyed;
    wl_signal_add(&w->destroy_signal, &o->destroyed);
    curtain(o, 1.0f);
    black_sync(o);
    snprintf(o->reason, sizeof o->reason, "app_absent");
    diagnostic_configure(o);
    output_event(o);
}
static void output_created(struct wl_listener *l, void *data) {
    struct shell *s = wl_container_of(l, s, output_created); add_output(s, data);
}
static void output_resized(struct wl_listener *l, void *data) {
    struct shell *s = wl_container_of(l, s, output_resized);
    struct weston_output *w = data;
    struct output *o = find_output(s, w->name);
    if (!o) return;
    remode(o);
}
/* Every capture's memory is charged to Weston and Mesa keeps it (capacity.py CAPTURE_BYTES): one
 * client captures at a time, from its first allowed capture until it disconnects, and at most
 * CAPTURE_PIXELS of Output in its life, so two clients' captures, one client's captures of every
 * lit Output at once, or a second source on one Output never pass the line. Only refuses: the
 * `--debug` authority allows. A refused capture fails "unauthorized". */
static void capture_holder_gone(struct wl_listener *l, void *data) {
    (void)data;
    struct shell *s = wl_container_of(l, s, capture_holder_destroyed);
    wl_list_remove(&s->capture_holder_destroyed.link);
    s->capture_holder = NULL;
    s->capture_spent = 0;
}
static void capture_ask(struct wl_listener *l, struct weston_output_capture_attempt *att) {
    struct shell *s = wl_container_of(l, s, capture_ask);
    const struct weston_mode *mode = att->who->output->current_mode;
    uint64_t pixels = mode ? (uint64_t)mode->width * (uint64_t)mode->height : CAPTURE_PIXELS + 1;
    if ((s->capture_holder && s->capture_holder != att->who->client) ||
        s->capture_spent + pixels > CAPTURE_PIXELS) {
        att->denied = true;
        weston_log("photo-wall-shell: capture of %s refused: %s\n", att->who->output->name,
                   s->capture_holder && s->capture_holder != att->who->client
                       ? "another client is capturing" : "past the capture line");
        return;
    }
    if (!s->capture_holder) {
        s->capture_holder = att->who->client;
        s->capture_holder_destroyed.notify = capture_holder_gone;
        wl_client_add_destroy_listener(s->capture_holder, &s->capture_holder_destroyed);
    }
    s->capture_spent += pixels;
}
static void diagnostic_client_gone(struct wl_listener *l, void *data) {
    (void)data;
    struct shell *s = wl_container_of(l, s, diagnostic_client_destroyed);
    wl_list_remove(&s->diagnostic_client_destroyed.link);
    s->diagnostic_client = NULL;
    s->diagnostic_resource = NULL;
    s->next_diagnostic_start = now_ms() + 2000;
    fallback_all(s);
}
static void start_diagnostic(struct shell *s) {
    if (s->diagnostic_client || now_ms() < s->next_diagnostic_start) return;
    s->next_diagnostic_start = now_ms() + 2000;
    int pair[2];
    if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, pair) < 0) return;
    pid_t child = fork();
    if (child == 0) {
        close(pair[0]);
        if (fcntl(pair[1], F_SETFD, 0) < 0) _exit(126);
        char socket_fd[32]; snprintf(socket_fd, sizeof socket_fd, "%d", pair[1]);
        setenv("WAYLAND_SOCKET", socket_fd, 1);
        execl("/usr/lib/photo-wall/node-display/diagnostic-client", "diagnostic-client", (char *)NULL);
        _exit(127);
    }
    close(pair[1]);
    if (child < 0) { close(pair[0]); return; }
    s->diagnostic_pid = child;
    s->diagnostic_client = wl_client_create(s->compositor->wl_display, pair[0]);
    if (!s->diagnostic_client) { close(pair[0]); kill(child, SIGTERM); return; }
    s->diagnostic_client_destroyed.notify = diagnostic_client_gone;
    wl_client_add_destroy_listener(s->diagnostic_client, &s->diagnostic_client_destroyed);
}
static int lease_tick(void *data) {
    struct shell *s = data;
    if (s->diagnostic_pid > 0 && waitpid(s->diagnostic_pid, NULL, WNOHANG) == s->diagnostic_pid)
        s->diagnostic_pid = 0;
    start_diagnostic(s);
    for (int i = 0; i < MAX_OUTPUTS; i++) {
        struct output *o = &s->outputs[i];
        if (mode_changed(o)) remode(o);
        if (o->trial_json[0] && boot_ms() >= o->trial_until) trial_end(o);
        if (o->revision_identity && now_ms() >= o->revision_until) revision_clear(o, true);
        /* The app's liveness: one ping a second while it holds the Output (Weston keeps at most one
         * outstanding); its answer renews the lease (`pong`), so a still picture, an Output forced
         * off (`output_power`) or under the black curtain (`black`) needs no presentation to keep
         * it. The app's process is still checked. */
        if (o->identity && o->app && now_ms() >= o->pinged + PING_MS) {
            o->pinged = now_ms();
            weston_desktop_client_ping(weston_desktop_surface_get_client(o->app->desktop));
        }
        if (o->identity && (now_ms() >= o->lease_until || process_start(o->pid) != o->start_ticks))
            invalidate(o, LEASE_LOST);
        hold_sync(o, false);   /* the compositor's scene now: a picture plane or Display's drawing */
    }
    wl_event_source_timer_update(s->timer, 200);
    return 0;
}
static void shell_destroyed(struct wl_listener *l, void *data) {
    (void)data;
    struct shell *s = wl_container_of(l, s, destroyed);
    s->shutting_down = true;
    wl_list_remove(&s->destroyed.link);
    wl_list_remove(&s->output_created.link);
    wl_list_remove(&s->output_resized.link);
    wl_list_remove(&s->capture_ask.link);
    if (s->capture_holder) wl_list_remove(&s->capture_holder_destroyed.link);
    wl_protocol_logger_destroy(s->logger);
    wl_global_destroy(s->frame_global);
    wl_global_destroy(s->diagnostic_global);
    wl_global_destroy(s->alpha_global);
    if (s->diagnostic_pid > 0) kill(s->diagnostic_pid, SIGTERM);
    wl_event_source_remove(s->timer);
    if (s->control_source) wl_event_source_remove(s->control_source);
    wl_event_source_remove(s->listen_source);
    if (s->control_fd >= 0) close(s->control_fd);
    close(s->listen_fd);
    s->control_fd = -1;
    if (s->diagnostic_client) wl_client_destroy(s->diagnostic_client);
    for (int i = 0; i < MAX_SURFACES; i++) {
        if (s->frame_resources[i]) wl_resource_destroy(s->frame_resources[i]);
        struct app_surface *a = s->surfaces[i];
        if (!a) continue;
        if (a->desktop) {
            weston_desktop_surface_set_user_data(a->desktop, NULL);
            weston_desktop_surface_unlink_view(a->view);
        }
        weston_view_destroy(a->view);
        free(a);
        s->surfaces[i] = NULL;
    }
    for (int i = 0; i < MAX_OUTPUTS; i++) {
        struct output *o = &s->outputs[i];
        if (o->weston) wl_list_remove(&o->destroyed.link);
        if (o->curtain) weston_shell_utils_curtain_destroy(o->curtain);
        if (o->fallback) weston_shell_utils_curtain_destroy(o->fallback);
        if (o->black) weston_shell_utils_curtain_destroy(o->black);
        json_decref(o->identity);
        json_decref(o->revision_identity);
        json_decref(o->trial_baseline);
    }
    weston_layer_fini(&s->app_layer);
    weston_layer_fini(&s->diagnostic_layer);
    weston_layer_fini(&s->health_layer);
    weston_layer_fini(&s->black_layer);
    weston_desktop_destroy(s->desktop);
    free(s);
}
WL_EXPORT int wet_shell_init(struct weston_compositor *ec, int *argc, char *argv[]) {
    (void)argc; (void)argv;
    const char *runtime = getenv("XDG_RUNTIME_DIR"), *controller = getenv("PHOTO_WALL_CONTROLLER_UID");
    if (!runtime || !controller) return -1;
    struct shell *s = calloc(1, sizeof *s);
    if (!s) return -1;
    s->compositor = ec; s->control_fd = -1;
    char *end = NULL; unsigned long uid = strtoul(controller, &end, 10);
    if (!*controller || *end || uid > UINT32_MAX) return -1;
    s->controller_uid = (uid_t)uid;
    if (!read_uuid("/proc/sys/kernel/random/boot_id", s->boot) ||
        !read_uuid("/proc/sys/kernel/random/uuid", s->incarnation)) return -1;
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    if (snprintf(addr.sun_path, sizeof addr.sun_path, "%s/control.sock", runtime) >= (int)sizeof addr.sun_path) return -1;
    /* RuntimeDirectory is private and owned by this base service. */
    unlink(addr.sun_path);
    s->listen_fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (s->listen_fd < 0 || bind(s->listen_fd, (struct sockaddr *)&addr, sizeof addr) < 0 ||
        chmod(addr.sun_path, 0600) < 0 || listen(s->listen_fd, 1) < 0) return -1;
    weston_layer_init(&s->app_layer, ec);
    weston_layer_init(&s->diagnostic_layer, ec);
    weston_layer_init(&s->health_layer, ec);
    weston_layer_init(&s->black_layer, ec);
    weston_layer_set_position(&s->app_layer, WESTON_LAYER_POSITION_NORMAL);
    weston_layer_set_position(&s->diagnostic_layer, WESTON_LAYER_POSITION_LOCK);
    /* Above the protected slate, below Weston's own cursor and fade. */
    weston_layer_set_position(&s->health_layer, WESTON_LAYER_POSITION_LOCK + 1);
    /* Black picture above everything of ours, health included: a dark room shows nothing. */
    weston_layer_set_position(&s->black_layer, WESTON_LAYER_POSITION_LOCK + 2);
    s->desktop = weston_desktop_create(ec, &desktop_api, s);
    if (!s->desktop) return -1;
    s->frame_global = wl_global_create(ec->wl_display, &pw_frame_manager_v1_interface, FRAME_VERSION, s, bind_frame);
    s->diagnostic_global = wl_global_create(ec->wl_display, &pw_diagnostic_manager_v1_interface, DIAGNOSTIC_VERSION,
        s, bind_diagnostic);
    s->alpha_global = wl_global_create(ec->wl_display, &wp_alpha_modifier_v1_interface, 1, s, bind_alpha);
    s->logger = wl_display_add_protocol_logger(ec->wl_display, protocol_log, s);
    if (!s->frame_global || !s->diagnostic_global || !s->alpha_global || !s->logger) return -1;
    s->output_created.notify = output_created;
    wl_signal_add(&ec->output_created_signal, &s->output_created);
    s->output_resized.notify = output_resized;
    wl_signal_add(&ec->output_resized_signal, &s->output_resized);
    s->destroyed.notify = shell_destroyed;
    wl_signal_add(&ec->destroy_signal, &s->destroyed);
    weston_compositor_add_screenshot_authority(ec, &s->capture_ask, capture_ask);
    struct weston_output *w;
    wl_list_for_each(w, &ec->output_list, link) add_output(s, w);
    struct wl_event_loop *loop = wl_display_get_event_loop(ec->wl_display);
    s->listen_source = wl_event_loop_add_fd(loop, s->listen_fd, WL_EVENT_READABLE, accept_control, s);
    s->timer = wl_event_loop_add_timer(loop, lease_tick, s);
    wl_event_source_timer_update(s->timer, 200);
    return 0;
}
