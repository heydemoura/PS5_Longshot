/* PS5: the link's stand-in for the console's libSceAudioIn: the five calls host/ps5/hos_sdl_ps5.c makes for player 0's
   DualSense microphone (host/ps5/ps5_mic.h). The payload SDK has no libSceAudioIn stub, so ps5/tools/link.sh and
   ps5/builder/make_kit.py build this one as they build PS5_Vulkan's AGC stubs: a shared object named libSceAudioIn.prx that
   the link and ps5-native-tool read to write the imports into the eboot, so that the console's loader loads the system's
   libSceAudioIn with the title's other system modules. These bodies are never packaged or run on the console.
   From Grapnel (github.com/morpheus302/PS5_Grapnel, ps5/stubs/libSceAudioIn_link_stub.c), after mihawk-99's PS5_RetroArch,
   tooling/ps5-stubs/audio_in_link_stub.c (Mihawk). SPDX-License-Identifier: GPL-3.0-or-later */
#include <stdint.h>

int sceAudioInInit(void) { return -1; }
int sceAudioInOpen(int32_t user, int32_t type, int32_t index, uint32_t grain, uint32_t rate, uint32_t param) {
    (void)user, (void)type, (void)index, (void)grain, (void)rate, (void)param;
    return -1;
}
int sceAudioInInput(int32_t port, void *pcm) {
    (void)port, (void)pcm;
    return -1;
}
int sceAudioInGetSilentState(int32_t port) {
    (void)port;
    return -1;
}
int sceAudioInClose(int32_t port) {
    (void)port;
    return -1;
}
