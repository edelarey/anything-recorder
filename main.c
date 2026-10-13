#define _GNU_SOURCE
#include <gtk/gtk.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>

#define APP_NAME "Anything Recorder"
#define APP_ID   "za.delarey.anythingrecorder"

typedef struct {
    char id[128];
    char label[256];
} VideoSource;

typedef struct {
    char name[512];
    char state[32];
} AudioSource;

typedef struct {
    GtkWidget *window;
    GtkWidget *video_combo;
    GtkWidget *audio_combo;
    GtkWidget *folder_btn;
    GtkWidget *name_entry;
    GtkWidget *record_btn;
    GtkWidget *status;
    GtkWidget *preview;
    GtkWidget *preview_frame;

    atomic_int running;
    pthread_t thread;
    pid_t ff_pid;
    int ff_in;
    int ff_err;
    char video_id[128];
    char audio_name[512];
    char path[1024];
    double seconds;
    guint preview_timeout;
    gboolean use_gsr;
} App;

static char *config_path(void);
static char *default_save_folder(void);
static void load_config(App *app);
static void save_config(App *app);
static void update_preview(App *app);

static gboolean is_wayland(void)
{
    const char *s = getenv("WAYLAND_DISPLAY");
    return s && *s;
}

static gboolean have_cmd(const char *cmd)
{
    char buf[256];
    g_snprintf(buf, sizeof buf, "command -v %s >/dev/null 2>&1", cmd);
    return system(buf) == 0;
}

static GList *list_video_sources(App *app)
{
    GList *out = NULL;

    VideoSource *portal = g_new0(VideoSource, 1);
    g_strlcpy(portal->id, "portal", sizeof portal->id);
    g_strlcpy(portal->label, "Portal (pick screen/window — works on Wayland)", sizeof portal->label);
    out = g_list_append(out, portal);

    VideoSource *screen = g_new0(VideoSource, 1);
    g_strlcpy(screen->id, "screen", sizeof screen->id);
    g_strlcpy(screen->label, "Entire screen", sizeof screen->label);
    out = g_list_append(out, screen);

    if (app->use_gsr) {
        FILE *p = popen("gpu-screen-recorder --list-monitors 2>/dev/null", "r");
        if (p) {
            char line[256];
            while (fgets(line, sizeof line, p)) {
                g_strstrip(line);
                if (!*line || line[0] == '#')
                    continue;
                VideoSource *v = g_new0(VideoSource, 1);
                g_strlcpy(v->id, line, sizeof v->id);
                g_snprintf(v->label, sizeof v->label, "%s", line);
                out = g_list_append(out, v);
            }
            pclose(p);
        }
    }

    if (!is_wayland()) {
        FILE *p = popen("xrandr --listmonitors 2>/dev/null", "r");
        if (p) {
            char line[512];
            while (fgets(line, sizeof line, p)) {
                char *name = strrchr(line, ' ');
                if (!name) continue;
                name++;
                g_strstrip(name);
                if (!*name || !g_ascii_isalnum(name[0])) continue;
                gboolean dup = FALSE;
                for (GList *l = out; l; l = l->next) {
                    if (strcmp(((VideoSource*)l->data)->id, name) == 0) {
                        dup = TRUE; break;
                    }
                }
                if (dup) continue;
                VideoSource *v = g_new0(VideoSource, 1);
                g_strlcpy(v->id, name, sizeof v->id);
                g_snprintf(v->label, sizeof v->label, "%s (xrandr)", name);
                out = g_list_append(out, v);
            }
            pclose(p);
        }
    }

    return out;
}

static GList *list_audio_sources(void)
{
    FILE *p = popen("pactl list short sources 2>/dev/null", "r");
    if (!p)
        return NULL;

    GList *out = NULL;
    char line[1024];
    while (fgets(line, sizeof line, p)) {
        char idx[32], name[512], driver[128], fmt[128], state[32];
        if (sscanf(line, "%31s %511s %127s %127s %31s",
                   idx, name, driver, fmt, state) < 5)
            continue;
        if (!strstr(name, ".monitor"))
            continue;
        AudioSource *s = g_new0(AudioSource, 1);
        g_strlcpy(s->name, name, sizeof s->name);
        g_strlcpy(s->state, state, sizeof s->state);
        out = g_list_append(out, s);
    }
    pclose(p);
    return out;
}

static void free_video(GList *list) { g_list_free_full(list, g_free); }
static void free_audio(GList *list) { g_list_free_full(list, g_free); }

static gboolean do_preview(gpointer data)
{
    App *app = data;
    if (atomic_load(&app->running) || is_wayland())
        return G_SOURCE_CONTINUE;

    const char *vid = gtk_combo_box_get_active_id(GTK_COMBO_BOX(app->video_combo));
    if (!vid || !vid[0] || strcmp(vid, "portal") == 0 || strcmp(vid, "screen") == 0)
        return G_SOURCE_CONTINUE;

    char cmd[1024];
    char tmp[] = "/tmp/anything-preview-XXXXXX.png";
    int fd = mkstemps(tmp, 4);
    if (fd < 0)
        return G_SOURCE_CONTINUE;
    close(fd);

    g_snprintf(cmd, sizeof cmd,
               "ffmpeg -hide_banner -loglevel error -y "
               "-f x11grab -video_size 1280x720 -i %s "
               "-frames:v 1 -vf scale=320:-1 %s",
               vid, tmp);
    if (system(cmd) == 0) {
        GdkPixbuf *pb = gdk_pixbuf_new_from_file(tmp, NULL);
        if (pb) {
            gtk_image_set_from_pixbuf(GTK_IMAGE(app->preview), pb);
            g_object_unref(pb);
        }
    }
    unlink(tmp);
    return G_SOURCE_CONTINUE;
}

static void update_preview(App *app)
{
    if (app->preview_timeout)
        g_source_remove(app->preview_timeout);
    app->preview_timeout = g_timeout_add_seconds(2, do_preview, app);
    do_preview(app);
}

static void close_fd(int *fd)
{
    if (*fd >= 0) {
        close(*fd);
        *fd = -1;
    }
}

static void reap_child(App *app)
{
    if (app->ff_pid <= 0)
        return;
    kill(app->ff_pid, SIGINT);
    int status = 0;
    for (int i = 0; i < 50; i++) {
        if (waitpid(app->ff_pid, &status, WNOHANG) == app->ff_pid) {
            app->ff_pid = -1;
            close_fd(&app->ff_err);
            return;
        }
        usleep(40000);
    }
    kill(app->ff_pid, SIGKILL);
    waitpid(app->ff_pid, &status, 0);
    app->ff_pid = -1;
    close_fd(&app->ff_err);
}

static int spawn_recorder(App *app)
{
    int errpipe[2] = {-1, -1};
    if (pipe(errpipe) != 0)
        return -1;

    pid_t pid = fork();
    if (pid < 0)
        return -1;

    if (pid == 0) {
        dup2(errpipe[1], STDERR_FILENO);
        int dn = open("/dev/null", O_WRONLY);
        if (dn >= 0) { dup2(dn, STDOUT_FILENO); close(dn); }
        close(errpipe[0]);
        close(errpipe[1]);

        if (app->use_gsr) {
            char audarg[600];
            g_snprintf(audarg, sizeof audarg, "device:%s", app->audio_name);

            execlp("gpu-screen-recorder", "gpu-screen-recorder",
                   "-w", app->video_id,
                   "-f", "30",
                   "-a", audarg,
                   "-o", app->path,
                   (char *)NULL);
            fprintf(stderr, "gpu-screen-recorder failed to start\n");
            _exit(127);
        }

        if (is_wayland() || strcmp(app->video_id, "portal") == 0) {
            fprintf(stderr, "Wayland/portal requires gpu-screen-recorder\n");
            _exit(1);
        }

        execlp("ffmpeg", "ffmpeg",
               "-hide_banner", "-loglevel", "info", "-y",
               "-f", "x11grab", "-framerate", "30", "-i", app->video_id,
               "-f", "pulse", "-i", app->audio_name,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-c:a", "aac", "-b:a", "192k",
               "-shortest",
               app->path,
               (char *)NULL);
        _exit(127);
    }

    close(errpipe[1]);
    app->ff_pid = pid;
    app->ff_err = errpipe[0];
    fcntl(app->ff_err, F_SETFL, O_NONBLOCK);
    return 0;
}

typedef struct {
    App *app;
    char msg[512];
    gboolean done;
} UiMsg;

static gboolean apply_ui(gpointer data)
{
    UiMsg *u = data;
    App *app = u->app;
    if (u->msg[0])
        gtk_label_set_text(GTK_LABEL(app->status), u->msg);
    if (u->done) {
        gtk_button_set_label(GTK_BUTTON(app->record_btn), "Record");
        gtk_widget_set_sensitive(app->video_combo, TRUE);
        gtk_widget_set_sensitive(app->audio_combo, TRUE);
        gtk_widget_set_sensitive(app->folder_btn, TRUE);
        gtk_widget_set_sensitive(app->name_entry, TRUE);
        gtk_style_context_remove_class(gtk_widget_get_style_context(app->record_btn), "destructive-action");
        gtk_style_context_add_class(gtk_widget_get_style_context(app->record_btn), "suggested-action");
    }
    g_free(u);
    return G_SOURCE_REMOVE;
}

static void push_ui(App *app, const char *msg, gboolean done)
{
    UiMsg *u = g_new0(UiMsg, 1);
    u->app = app;
    u->done = done;
    if (msg)
        g_strlcpy(u->msg, msg, sizeof u->msg);
    g_idle_add(apply_ui, u);
}

static void *record_thread(void *arg)
{
    App *app = arg;
    if (spawn_recorder(app) != 0) {
        push_ui(app, "Failed to start recorder.", TRUE);
        atomic_store(&app->running, 0);
        return NULL;
    }

    char buf[1024];
    double seconds = 0.0;
    time_t last = 0;
    time_t started = time(NULL);
    while (atomic_load(&app->running) || app->ff_pid > 0) {
        ssize_t n = read(app->ff_err, buf, sizeof buf - 1);
        if (n > 0) {
            buf[n] = 0;
            seconds = difftime(time(NULL), started);
        }
        time_t now = time(NULL);
        if (now != last && atomic_load(&app->running)) {
            last = now;
            seconds = difftime(now, started);
            char m[128];
            int sec = (int)seconds;
            g_snprintf(m, sizeof m, "Recording  %02d:%02d  (%s)",
                       sec / 60, sec % 60,
                       app->use_gsr ? "gpu-screen-recorder" : "ffmpeg");
            push_ui(app, m, FALSE);
        }
        if (app->ff_pid > 0) {
            int st;
            if (waitpid(app->ff_pid, &st, WNOHANG) == app->ff_pid)
                app->ff_pid = -1;
        }
        usleep(40000);
    }
    reap_child(app);
    atomic_store(&app->running, 0);
    push_ui(app, "Saved.", TRUE);
    return NULL;
}

static void on_record(GtkButton *btn, App *app)
{
    (void)btn;
    if (atomic_load(&app->running)) {
        atomic_store(&app->running, 0);
        return;
    }

    const char *vid = gtk_combo_box_get_active_id(GTK_COMBO_BOX(app->video_combo));
    const char *aud = gtk_combo_box_get_active_id(GTK_COMBO_BOX(app->audio_combo));
    if (!vid || !aud) {
        gtk_label_set_text(GTK_LABEL(app->status), "Select video and audio sources.");
        return;
    }

    if ((strcmp(vid, "portal") == 0 || is_wayland()) && !app->use_gsr) {
        gtk_label_set_text(GTK_LABEL(app->status),
            "Install gpu-screen-recorder for Wayland/portal capture.");
        return;
    }

    char *folder = gtk_file_chooser_get_filename(GTK_FILE_CHOOSER(app->folder_btn));
    const char *name = gtk_entry_get_text(GTK_ENTRY(app->name_entry));
    if (!folder || !name || !name[0]) {
        gtk_label_set_text(GTK_LABEL(app->status), "Choose folder and name.");
        g_free(folder);
        return;
    }

    char path[1024];
    g_snprintf(path, sizeof path, "%s/%s.mp4", folder, name);
    if (g_file_test(path, G_FILE_TEST_EXISTS)) {
        GtkWidget *dlg = gtk_message_dialog_new(GTK_WINDOW(app->window),
            GTK_DIALOG_MODAL, GTK_MESSAGE_QUESTION, GTK_BUTTONS_YES_NO,
            "Overwrite existing file?");
        int r = gtk_dialog_run(GTK_DIALOG(dlg));
        gtk_widget_destroy(dlg);
        if (r != GTK_RESPONSE_YES) {
            g_free(folder);
            return;
        }
    }

    g_strlcpy(app->video_id, vid, sizeof app->video_id);
    g_strlcpy(app->audio_name, aud, sizeof app->audio_name);
    g_strlcpy(app->path, path, sizeof app->path);
    g_free(folder);

    atomic_store(&app->running, 1);
    gtk_button_set_label(GTK_BUTTON(app->record_btn), "Stop");
    gtk_widget_set_sensitive(app->video_combo, FALSE);
    gtk_widget_set_sensitive(app->audio_combo, FALSE);
    gtk_widget_set_sensitive(app->folder_btn, FALSE);
    gtk_widget_set_sensitive(app->name_entry, FALSE);
    gtk_style_context_remove_class(gtk_widget_get_style_context(app->record_btn), "suggested-action");
    gtk_style_context_add_class(gtk_widget_get_style_context(app->record_btn), "destructive-action");
    gtk_label_set_text(GTK_LABEL(app->status), "Starting…");

    pthread_create(&app->thread, NULL, record_thread, app);
    pthread_detach(app->thread);
    save_config(app);
}

static void reload_video(App *app)
{
    char *prev = NULL;
    const char *cur = gtk_combo_box_get_active_id(GTK_COMBO_BOX(app->video_combo));
    if (cur)
        prev = g_strdup(cur);
    gtk_combo_box_text_remove_all(GTK_COMBO_BOX_TEXT(app->video_combo));
    GList *list = list_video_sources(app);
    for (GList *l = list; l; l = l->next) {
        VideoSource *v = l->data;
        gtk_combo_box_text_append(GTK_COMBO_BOX_TEXT(app->video_combo), v->id, v->label);
    }
    if (prev && !gtk_combo_box_set_active_id(GTK_COMBO_BOX(app->video_combo), prev))
        gtk_combo_box_set_active(GTK_COMBO_BOX(app->video_combo), 0);
    else if (!prev)
        gtk_combo_box_set_active(GTK_COMBO_BOX(app->video_combo), 0);
    g_free(prev);
    free_video(list);
    update_preview(app);
}

static void reload_audio(App *app)
{
    char *prev = NULL;
    const char *cur = gtk_combo_box_get_active_id(GTK_COMBO_BOX(app->audio_combo));
    if (cur)
        prev = g_strdup(cur);
    gtk_combo_box_text_remove_all(GTK_COMBO_BOX_TEXT(app->audio_combo));
    GList *list = list_audio_sources();
    for (GList *l = list; l; l = l->next) {
        AudioSource *s = l->data;
        char label[640];
        g_snprintf(label, sizeof label, "%s  [%s]", s->name, s->state);
        gtk_combo_box_text_append(GTK_COMBO_BOX_TEXT(app->audio_combo), s->name, label);
    }
    if (prev && !gtk_combo_box_set_active_id(GTK_COMBO_BOX(app->audio_combo), prev))
        gtk_combo_box_set_active(GTK_COMBO_BOX(app->audio_combo), 0);
    else if (!prev)
        gtk_combo_box_set_active(GTK_COMBO_BOX(app->audio_combo), 0);
    g_free(prev);
    free_audio(list);
}

static void on_video_changed(GtkComboBox *combo, App *app)
{
    (void)combo;
    update_preview(app);
}

static char *config_path(void)
{
    return g_build_filename(g_get_user_config_dir(), "anything-recorder", "config.ini", NULL);
}

static char *default_save_folder(void)
{
    const char *home = g_get_home_dir();
    char *vids = g_build_filename(home, "Videos", NULL);
    if (g_file_test(vids, G_FILE_TEST_IS_DIR))
        return vids;
    g_free(vids);
    return g_strdup(home);
}

static void load_config(App *app)
{
    char *path = config_path();
    GKeyFile *kf = g_key_file_new();
    if (g_key_file_load_from_file(kf, path, G_KEY_FILE_NONE, NULL)) {
        char *folder = g_key_file_get_string(kf, "main", "folder", NULL);
        if (folder)
            gtk_file_chooser_set_filename(GTK_FILE_CHOOSER(app->folder_btn), folder);
        g_free(folder);
    }
    g_key_file_free(kf);
    g_free(path);
}

static void save_config(App *app)
{
    char *folder = gtk_file_chooser_get_filename(GTK_FILE_CHOOSER(app->folder_btn));
    if (!folder)
        return;
    char *path = config_path();
    char *dir = g_path_get_dirname(path);
    g_mkdir_with_parents(dir, 0755);
    GKeyFile *kf = g_key_file_new();
    g_key_file_set_string(kf, "main", "folder", folder);
    g_key_file_save_to_file(kf, path, NULL);
    g_key_file_free(kf);
    g_free(dir);
    g_free(path);
    g_free(folder);
}

static void activate(GtkApplication *ga, gpointer data)
{
    (void)data;
    App *app = g_new0(App, 1);
    app->ff_pid = -1;
    app->ff_in = -1;
    app->ff_err = -1;
    app->use_gsr = have_cmd("gpu-screen-recorder");

    app->window = gtk_application_window_new(ga);
    gtk_window_set_title(GTK_WINDOW(app->window), APP_NAME);
    gtk_window_set_default_size(GTK_WINDOW(app->window), 720, 520);

    GtkWidget *box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 8);
    gtk_container_set_border_width(GTK_CONTAINER(box), 12);
    gtk_container_add(GTK_CONTAINER(app->window), box);

    char hint[512];
    g_snprintf(hint, sizeof hint,
               "Capture a monitor (HDMI / cast) + audio monitor to MP4.\n"
               "Backend: %s%s",
               app->use_gsr ? "gpu-screen-recorder (X11 + Wayland)" : "ffmpeg (X11 only)",
               app->use_gsr ? "" : " — install gpu-screen-recorder for Wayland.");
    GtkWidget *hint_lbl = gtk_label_new(hint);
    gtk_label_set_xalign(GTK_LABEL(hint_lbl), 0.0);
    gtk_label_set_line_wrap(GTK_LABEL(hint_lbl), TRUE);
    gtk_box_pack_start(GTK_BOX(box), hint_lbl, FALSE, FALSE, 0);

    GtkWidget *vh = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 6);
    app->video_combo = gtk_combo_box_text_new();
    gtk_widget_set_hexpand(app->video_combo, TRUE);
    g_signal_connect(app->video_combo, "changed", G_CALLBACK(on_video_changed), app);
    GtkWidget *vref = gtk_button_new_with_label("Refresh");
    g_signal_connect_swapped(vref, "clicked", G_CALLBACK(reload_video), app);
    gtk_box_pack_start(GTK_BOX(vh), gtk_label_new("Video:"), FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(vh), app->video_combo, TRUE, TRUE, 0);
    gtk_box_pack_start(GTK_BOX(vh), vref, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(box), vh, FALSE, FALSE, 0);

    GtkWidget *ah = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 6);
    app->audio_combo = gtk_combo_box_text_new();
    gtk_widget_set_hexpand(app->audio_combo, TRUE);
    GtkWidget *aref = gtk_button_new_with_label("Refresh");
    g_signal_connect_swapped(aref, "clicked", G_CALLBACK(reload_audio), app);
    gtk_box_pack_start(GTK_BOX(ah), gtk_label_new("Audio:"), FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(ah), app->audio_combo, TRUE, TRUE, 0);
    gtk_box_pack_start(GTK_BOX(ah), aref, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(box), ah, FALSE, FALSE, 0);

    GtkWidget *frame = gtk_frame_new("Settings");
    GtkWidget *grid = gtk_grid_new();
    gtk_grid_set_row_spacing(GTK_GRID(grid), 6);
    gtk_grid_set_column_spacing(GTK_GRID(grid), 8);
    gtk_container_set_border_width(GTK_CONTAINER(grid), 8);
    gtk_container_add(GTK_CONTAINER(frame), grid);

    app->folder_btn = gtk_file_chooser_button_new("Save folder", GTK_FILE_CHOOSER_ACTION_SELECT_FOLDER);
    gtk_grid_attach(GTK_GRID(grid), gtk_label_new("Folder:"), 0, 0, 1, 1);
    gtk_grid_attach(GTK_GRID(grid), app->folder_btn, 1, 0, 1, 1);

    app->name_entry = gtk_entry_new();
    gtk_entry_set_placeholder_text(GTK_ENTRY(app->name_entry), "Name this take");
    gtk_widget_set_hexpand(app->name_entry, TRUE);
    GtkWidget *ext = gtk_label_new(".mp4");
    gtk_grid_attach(GTK_GRID(grid), gtk_label_new("Name:"), 0, 1, 1, 1);
    gtk_grid_attach(GTK_GRID(grid), app->name_entry, 1, 1, 1, 1);
    gtk_grid_attach(GTK_GRID(grid), ext, 2, 1, 1, 1);

    gtk_box_pack_start(GTK_BOX(box), frame, FALSE, FALSE, 0);

    app->preview_frame = gtk_frame_new("Preview (X11 monitors only)");
    app->preview = gtk_image_new();
    gtk_widget_set_size_request(app->preview, 320, 180);
    gtk_container_add(GTK_CONTAINER(app->preview_frame), app->preview);
    gtk_box_pack_start(GTK_BOX(box), app->preview_frame, TRUE, TRUE, 0);

    app->record_btn = gtk_button_new_with_label("Record");
    gtk_style_context_add_class(gtk_widget_get_style_context(app->record_btn), "suggested-action");
    gtk_widget_set_size_request(app->record_btn, -1, 42);
    g_signal_connect(app->record_btn, "clicked", G_CALLBACK(on_record), app);
    gtk_box_pack_start(GTK_BOX(box), app->record_btn, FALSE, FALSE, 0);

    app->status = gtk_label_new(app->use_gsr ? "Ready (gpu-screen-recorder)." : "Ready (ffmpeg X11).");
    gtk_label_set_xalign(GTK_LABEL(app->status), 0.0);
    gtk_box_pack_start(GTK_BOX(box), app->status, FALSE, FALSE, 0);

    load_config(app);
    if (!gtk_file_chooser_get_filename(GTK_FILE_CHOOSER(app->folder_btn))) {
        char *def = default_save_folder();
        gtk_file_chooser_set_filename(GTK_FILE_CHOOSER(app->folder_btn), def);
        g_free(def);
    }

    reload_video(app);
    reload_audio(app);
    update_preview(app);

    gtk_widget_show_all(app->window);
}

int main(int argc, char **argv)
{
    GtkApplication *ga = gtk_application_new(APP_ID, G_APPLICATION_DEFAULT_FLAGS);
    g_signal_connect(ga, "activate", G_CALLBACK(activate), NULL);
    int st = g_application_run(G_APPLICATION(ga), argc, argv);
    g_object_unref(ga);
    return st;
}
