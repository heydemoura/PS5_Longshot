/* The launcher opts into the game startup stream through the builder. The game
   inherits the builder's stdout, so startup and build events have one reader. */
#pragma once
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static inline int launcher_startup(void) {
    const char *enabled = getenv("LONGSHOT_LAUNCHER_STARTUP");
    return enabled && !strcmp(enabled, "1");
}

static inline void launcher_game_ready(void) {
    fputs("{\"v\":1,\"type\":\"game_ready\"}\n", stdout);
    fflush(stdout);
}
