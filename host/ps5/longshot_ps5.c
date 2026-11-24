/* PS5: what Longshot's runtime and back end call that the console has no answer for, since PS5_Hookshot's port was
   written. The libc names are kept inside the executable (ps5/tools/link.sh's local.map), as win32_ps5.c's are. */
#include <errno.h>
#include <fcntl.h>
#include <pwd.h>
#include <stddef.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

/* ---- the C library ---- */
/* (Linux system calls by number: host/hos_crash_dialog.c's pidfd_open and close_range, which fall back when refused) */
int syscall(int number, ...) { (void)number; errno = ENOSYS; return -1; }
/* (the crash window's clipboard tool: no program is started on the console) */
int pipe2(int fds[2], int flags) { (void)fds, (void)flags; errno = ENOSYS; return -1; }
/* (the control bridge's listener, host/control.c) */
int accept4(int s, struct sockaddr *addr, socklen_t *len, int flags) {
    const int fd = accept(s, addr, len);
    if (fd < 0) return fd;
    if (flags & SOCK_NONBLOCK) fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
    if (flags & SOCK_CLOEXEC) fcntl(fd, F_SETFD, FD_CLOEXEC);
    return fd;
}
/* (the program cache's read-ahead hints, gpu/vk/vk_progcache.cpp) */
int posix_fadvise(int fd, off_t offset, off_t len, int advice) { (void)fd, (void)offset, (void)len, (void)advice; return 0; }
/* (every folder the host and the renderer make, ps5/build.py MKDIR: for every user, whatever mode was asked. The console
   runs the title as a user of its own; the FTP server and a file manager are another, and could not delete what lay in a
   folder made 0755 or 0700. One that is there already, made by a build before this, is opened the same way: the title
   asks for its folders at every start) */
#undef mkdir
int mkdir(const char *path, mode_t mode);
int hos_ps5_mkdir(const char *path, mode_t mode) {
    (void)mode;
    const int r = mkdir(path, 0777);
    if (r != 0 && errno == EEXIST) {
        chmod(path, 0777);   /* (ours: opened; another user's, made by FTP: refused, and open already) */
        errno = EEXIST;
    }
    return r;
}
/* (files made by Longshot's writers: the console has no mask to change) */
mode_t umask(mode_t mask) { (void)mask; return 022; }
/* (the bug report's home folder, gpu/vk/vk_bug_report.cpp: it takes none) */
struct passwd *getpwuid(uid_t uid) { (void)uid; return NULL; }

/* ---- the control bridge's game state (host/control.c): host/hos_main.c has it with the debug tools, which are built on
   Windows and Linux only. Nothing is known and nothing is asked here ---- */
/* (the Discord status' figures, host/discord_rpc.c: not known here, as hos_oot_state) */
int hos_oot_stats(int *life, int *max_life, int *rupees, int *tokens, int *pieces) {
    if (life) *life = -1;
    if (max_life) *max_life = -1;
    if (rupees) *rupees = -1;
    if (tokens) *tokens = -1;
    if (pieces) *pieces = -1;
    return 0;
}
int hos_oot_state(int *event, int *loading, int *child) {
    if (event) *event = 0;
    if (loading) *loading = 0;
    if (child) *child = 0;
    return 0;
}
int hos_oot_current_map(char *out, size_t cap) { if (out && cap) *out = 0; return 0; }
int hos_oot_destination(int index, const char **child, const char **adult) { (void)index, (void)child, (void)adult; return 0; }
int hos_oot_teleport_ask(int index) { (void)index; return 0; }
int hos_oot_age_ask(int adult) { (void)adult; return 0; }
int hos_oot_teleport_result(int ticket, char *why, size_t why_cap, char *map, size_t map_cap, int *accepted) {
    (void)ticket;
    if (why && why_cap) *why = 0;
    if (map && map_cap) *map = 0;
    if (accepted) *accepted = 0;
    return 0;
}
void hos_oot_teleport_cancel(int ticket) { (void)ticket; }
