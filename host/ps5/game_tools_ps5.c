/* (PS5) The control bridge's game commands (game/game.h: the state, the map, the destinations, a teleport or an age asked
   for) are game/oot_tools.c's debug tools, built for Windows and Linux only. The console has no bridge client: these say
   "not here", so host/control.c, the Discord status and the input movie link and take the answer they take before a
   game is loaded. */
#include <stddef.h>
int game_current_map(char *out, size_t cap) { if (out && cap) out[0] = 0; return 0; }
int game_state(int *event, int *loading, int *child) { (void)event, (void)loading, (void)child; return 0; }
int game_destination(int index, const char **child, const char **adult) { (void)index, (void)child, (void)adult; return 0; }
int game_teleport_ask(int index) { (void)index; return 0; }
int game_teleport_result(int ticket, char *why, size_t why_cap, char *map, size_t map_cap, int *accepted) {
    (void)ticket, (void)map, (void)map_cap;
    if (why && why_cap) why[0] = 0;
    if (accepted) *accepted = 0;
    return 0;
}
void game_teleport_cancel(int ticket) { (void)ticket; }
int game_age_ask(int adult) { (void)adult; return 0; }
