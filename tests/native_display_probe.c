#define _GNU_SOURCE
#include <stdlib.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <poll.h>
#include <jansson.h>
#include <unistd.h>
#include <sys/mman.h>
#include <wayland-client.h>
#include "xdg-shell-client.h"
#include "photo-wall-frame-client.h"
#include "frame-client.h"
static struct wl_compositor *comp;
static struct wl_shm *shm;
static struct xdg_wm_base *wm;
static int configured, private_probe;
static void ping(void*d,struct xdg_wm_base*w,uint32_t serial){(void)d;xdg_wm_base_pong(w,serial);}
static const struct xdg_wm_base_listener wm_listener={.ping=ping};
static void global(void*d,struct wl_registry*r,uint32_t n,const char*i,uint32_t v){
(void)d;(void)v;
if(!strcmp(i,"wl_compositor"))comp=wl_registry_bind(r,n,&wl_compositor_interface,1);
if(!strcmp(i,"wl_shm"))shm=wl_registry_bind(r,n,&wl_shm_interface,1);
if(!strcmp(i,"xdg_wm_base")){wm=wl_registry_bind(r,n,&xdg_wm_base_interface,1);xdg_wm_base_add_listener(wm,&wm_listener,NULL);}
if(private_probe&&!strcmp(i,"pw_diagnostic_manager_v1"))wl_registry_bind(r,n,&pw_diagnostic_manager_v1_interface,1);
}
static void removed(void*d,struct wl_registry*r,uint32_t n){(void)d;(void)r;(void)n;}
static const struct wl_registry_listener registry_listener={global,removed};
static void configure(void*d,struct xdg_surface*s,uint32_t serial){(void)d;xdg_surface_ack_configure(s,serial);configured=1;}
static const struct xdg_surface_listener surface_listener={configure};
static void top_config(void*d,struct xdg_toplevel*t,int32_t w,int32_t h,struct wl_array*a){(void)d;(void)t;(void)w;(void)h;(void)a;}
static void top_close(void*d,struct xdg_toplevel*t){(void)d;(void)t;exit(0);}
static const struct xdg_toplevel_listener top_listener={.configure=top_config,.close=top_close};
int main(int argc,char**argv){private_probe=argc>1&&!strcmp(argv[1],"private");
struct wl_display*d=wl_display_connect(NULL);if(!d)return 2;
struct wl_registry*r=wl_display_get_registry(d);wl_registry_add_listener(r,&registry_listener,NULL);
if(wl_display_roundtrip(d)<0)return private_probe?0:3;
if(wl_display_roundtrip(d)<0)return private_probe?0:3;
if(private_probe)return 4;
struct pw_frame_client*c=pw_frame_client_create(d);if(!c)return 5;
struct wl_surface*s=wl_compositor_create_surface(comp);struct xdg_surface*xs=xdg_wm_base_get_xdg_surface(wm,s);
xdg_surface_add_listener(xs,&surface_listener,NULL);struct xdg_toplevel*top=xdg_surface_get_toplevel(xs);
xdg_toplevel_add_listener(top,&top_listener,NULL);xdg_toplevel_set_app_id(top,"photo-wall-headless");wl_surface_commit(s);
int fd=memfd_create("probe",MFD_CLOEXEC);int size=640*480*4;ftruncate(fd,size);
uint32_t*p=mmap(NULL,size,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);for(int i=0;i<640*480;i++)p[i]=0xff223344;
struct wl_shm_pool*pool=wl_shm_create_pool(shm,fd,size);struct wl_buffer*b=wl_shm_pool_create_buffer(pool,0,640,480,640*4,WL_SHM_FORMAT_XRGB8888);close(fd);
for(int n=0;n<3000;n++){
usleep(30000);
wl_display_dispatch_pending(d);struct pollfd pollfd={.fd=wl_display_get_fd(d),.events=POLLIN};wl_display_flush(d);
if(poll(&pollfd,1,100)>0&&wl_display_dispatch(d)<0)return 6;
char grant[37],frame[97]="";uint64_t gen,rev;uint32_t state;int current=pw_frame_client_grant(c,"headless",grant,frame,&gen,&rev,&state);
if(current<0){fprintf(stderr,"probe grant lost\n");return 7;}
/* Fixture adopts revision2 when authorized, deliberately ignores revision3 to test expiry. */
uint32_t pending_state; char pending_id[37];
if(pw_frame_client_grant_revision(c,"headless",frame,1,2,pending_id,&pending_state)==1){
strcpy(grant,pending_id);state=pending_state;current=1;}

if(configured){
char trial[4097], trial_tag[97]; const char *tag=state?"authored-probe":"synthetic-probe";
if(current==1&&pw_frame_client_trial(c,"headless",grant,trial,sizeof trial)==1){
json_error_t error; json_t *candidate=json_loads(trial,0,&error);
const char *hash=json_string_value(json_object_get(candidate,"candidate_sha256"));
if(hash){snprintf(trial_tag,sizeof trial_tag,"trial-%s",hash);
if(json_integer_value(json_object_get(candidate,"sequence"))==1)memset(trial_tag+6,'0',64);
tag=trial_tag;pw_frame_client_overlay(c,s,grant,tag,"[[0.4,0.1],[0.9,0.3],[0.8,0.9],[0.1,0.8]]");
if(json_integer_value(json_object_get(candidate,"sequence"))==3)wl_surface_commit(s);
}
json_decref(candidate);}
if(current==1&&pw_frame_client_tag_next_commit(c,s,grant,tag)<0){fprintf(stderr,"probe tag failed\n");return 8;}
wl_surface_attach(s,b,0,0);wl_surface_damage(s,0,0,640,480);wl_surface_commit(s);}
}
return 0;}
