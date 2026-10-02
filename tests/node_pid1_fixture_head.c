/* Disposable fixture only: named headless output for production DRM inventory matching. */
#include <libweston/libweston.h>
#include <libweston/windowed-output-api.h>
WL_EXPORT int wet_module_init(struct weston_compositor *compositor, int *argc, char *argv[])
{
    (void)argc; (void)argv;
    const struct weston_windowed_output_api *api =
        weston_windowed_output_get_api(compositor, WESTON_WINDOWED_OUTPUT_HEADLESS);
    if (!api) return -1;
    return api->create_head(compositor->primary_backend, "Virtual-1");
}
