"""The queues' global priority switches, RX_VK_QUEUE_PRIORITY and RX_VK_PRESENT_QUEUE_PRIORITY: their level's read and the
family's gate (gpu/vk/device_policy.h's policy_rule), and where create_device (vk_main.cpp) uses them.

The read (parse_queue_priority) is compiled with the Vulkan headers and run on made-up values: a level's name asks that
level (low, medium, high, realtime: VkQueueGlobalPriorityKHR's 128 to 1024) in any case, spaces around it not counted; an
off word asks none (0); anything else, empty and unset included, is refused, and the caller says so. Each level has its
name for the log (queue_priority_name: none for 0, ? for a value no level has).

The family's gate (queue_priority_listed) is whether a level may be asked of a queue's family: it is in the family's list
(VkQueueFamilyGlobalPriorityPropertiesKHR's, its count of levels read and no further than the list's room), or the driver
gives no list (VK_EXT_global_priority without VK_EXT_global_priority_query: the level is then asked unlisted, and a device
refused with it is made again without it). A family that lists low and medium lets low and medium be asked, not high nor
realtime; a list read short of a level does not have it.

A device not made with a level asked is made again without it, whatever the failure; the log says the level refused
(queue_priority_refused) only for what the spec gives a refused level: VK_ERROR_NOT_PERMITTED_KHR (no privilege for it)
and VK_ERROR_INITIALIZATION_FAILED (no resources for it, or a level the family does not list). Any other failure is
said as such, its cause maybe another.

Then create_device itself, cut from vk_main.cpp and run with a made-up driver:
- refused_not_permitted, refused_initialization: the device refused with the level asked: the log says refused, and the
  device is made again without the level nor its extension.
- failed_other: a device failing otherwise (out of host memory) with the level asked: made again without it, the log
  saying it failed with the level asked, the cause maybe another, not that the level was refused.
- unread_no_call, unread_family: the driver says it lists the families' levels (VK_KHR_global_priority) but no list can
  be read (no vkGetPhysicalDeviceQueueFamilyProperties2, or the queue's family not among those it gives): the log says
  no list could be read and the level is asked unlisted, not that the driver has neither extension.
- unlisted_ext: VK_EXT_global_priority alone: no list given, the level asked unlisted, the log saying so.
- made_no_own_queue: the presents with no queue of their own (RX_VK_PRESENT_QUEUE off): the device's line says the
  presents are on the replay's queue, not that their own queue has a level; made_own_queue: with their own queue, its
  level is said.

And the source: create_device reads both switches through parse_queue_priority, asks nothing for an off word, reads the
family's list and lets queue_priority_listed decide, and chains the level onto the queue's create info only where it
allows it, before the device is made; a device not made with it says refused by queue_priority_refused, and is made
again without it.

    python -I -B tests/test_queue_priority.py --headers <Vulkan-Headers/include> [--cxx g++]
"""
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))   # (python -I leaves the check's own folder off the path)
from srccut import between, block   # noqa: E402

RUN_ALL_ARGS = ["--cxx", "{cxx}", "--headers", "{vulkan_headers}"]

ROOT = Path(__file__).resolve().parents[1]
VK = ROOT / "gpu" / "vk"

LOW, MEDIUM, HIGH, REALTIME = 128, 256, 512, 1024

# (a switch's value, nullptr for unset; whether it is taken; the level it asks, -1 where it is refused)
PARSE = [
    ('"low"', 1, LOW), ('"medium"', 1, MEDIUM), ('"high"', 1, HIGH), ('"realtime"', 1, REALTIME),
    ('"LOW"', 1, LOW), ('"High"', 1, HIGH), ('"RealTime"', 1, REALTIME),          # (any case)
    ('" medium "', 1, MEDIUM), ('"\\tlow\\r\\n"', 1, LOW),                          # (spaces around it not counted)
    ('"0"', 1, 0), ('"off"', 1, 0), ('"OFF"', 1, 0), ('"false"', 1, 0), ('"no"', 1, 0),   # (an off word: none asked)
    ('""', 0, -1), ("nullptr", 0, -1),                                              # (unset or empty: nothing read)
    ('"lo"', 0, -1), ('"lowest"', 0, -1), ('"real time"', 0, -1), ('"1"', 0, -1), ('"128"', 0, -1),
    ('"low medium"', 0, -1), ('"yes"', 0, -1),
]
NAMES = [(0, "none"), (LOW, "low"), (MEDIUM, "medium"), (HIGH, "high"), (REALTIME, "realtime"), (99, "?")]
# (the level asked; the family's list, None where the driver gives none; the count of levels it says it has; whether the
# level may be asked)
LISTED = [
    (LOW, None, 0, 1), (REALTIME, None, 0, 1),                    # (no list: asked unlisted)
    (LOW, [LOW, MEDIUM], 2, 1), (MEDIUM, [LOW, MEDIUM], 2, 1),     # (an RTX 4090's families, 621.24)
    (HIGH, [LOW, MEDIUM], 2, 0), (REALTIME, [LOW, MEDIUM], 2, 0),
    (MEDIUM, [LOW, MEDIUM], 1, 0),                                 # (the count read, no further)
    (LOW, [], 0, 0),                                               # (a family that lists none)
    (REALTIME, [LOW, MEDIUM, HIGH, REALTIME], 4, 1),
    (HIGH, [MEDIUM] * 16 + [HIGH], 17, 0),                         # (no further than the list's 16 places)
]
# (vkCreateDevice's result with a level asked; whether it is the level refused, as the spec gives it)
REFUSED = [
    ("VK_ERROR_NOT_PERMITTED_KHR", 1), ("VK_ERROR_INITIALIZATION_FAILED", 1),
    ("VK_ERROR_OUT_OF_HOST_MEMORY", 0), ("VK_ERROR_OUT_OF_DEVICE_MEMORY", 0), ("VK_ERROR_DEVICE_LOST", 0),
    ("VK_ERROR_EXTENSION_NOT_PRESENT", 0), ("VK_ERROR_FEATURE_NOT_PRESENT", 0), ("VK_ERROR_TOO_MANY_OBJECTS", 0),
    ("VK_ERROR_UNKNOWN", 0),
]

FIXTURE = r"""
#include <cstdio>
#include "device_policy.h"
using namespace mkwvk;
static void parse(int i, const char* text) {
    uint32_t level = 77;
    const bool ok = policy_rule::parse_queue_priority(text, level);
    std::printf("PARSE %d %d %lld\n", i, (int)ok, ok ? (long long)level : -1LL);
}
static void listed(int i, uint32_t level, const VkQueueGlobalPriorityKHR* levels, uint32_t count) {
    std::printf("LISTED %d %d\n", i, (int)policy_rule::queue_priority_listed(level, levels, count));
}
int main() {
@@CASES@@
    return 0;
}
"""


# (create_device's global priority, cut from vk_main.cpp: its switches read and chained, then the device made, with a
# made-up driver. Each case prints its log lines (LOG), each vkCreateDevice (CREATE: whether a level is chained onto a
# queue and its extension asked) and the result (MADE))
CREATE_FIXTURE = r"""
#define VK_NO_PROTOTYPES
#include <algorithm>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include "device_policy.h"
using namespace mkwvk;
void logf(const char* fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    std::printf("LOG ");
    std::vprintf(fmt, ap);
    std::printf("\n");
    va_end(ap);
}
struct Dev {
    VkInstance instance = nullptr;
    VkPhysicalDevice physical = nullptr;
    VkDevice device = nullptr;
    uint32_t queue_family = 0;
} dev;
bool has_extension(const std::vector<VkExtensionProperties>& all, const char* name) {
    for (const auto& e : all)
        if (!std::strcmp(e.extensionName, name)) return true;
    return false;
}
// (the made-up driver: its families, each listing low and medium; vkGetPhysicalDeviceQueueFamilyProperties2 given or not;
// vkCreateDevice's results in turn)
uint32_t families = 3;
bool props2_given = true;
std::vector<VkResult> results;
size_t creates = 0;
void VKAPI_CALL props2(VkPhysicalDevice, uint32_t* count, VkQueueFamilyProperties2* p) {
    if (!p) {
        *count = families;
        return;
    }
    for (uint32_t i = 0; i < *count; i++) {
        auto* g = (VkQueueFamilyGlobalPriorityPropertiesKHR*)p[i].pNext;
        g->priorityCount = 2;
        g->priorities[0] = VK_QUEUE_GLOBAL_PRIORITY_LOW_KHR;
        g->priorities[1] = VK_QUEUE_GLOBAL_PRIORITY_MEDIUM_KHR;
    }
}
PFN_vkVoidFunction VKAPI_CALL get_proc(VkInstance, const char* name) {
    return props2_given && !std::strcmp(name, "vkGetPhysicalDeviceQueueFamilyProperties2") ? (PFN_vkVoidFunction)props2 : nullptr;
}
PFN_vkGetInstanceProcAddr instance_proc() { return get_proc; }
VkResult vkCreateDevice(VkPhysicalDevice, const VkDeviceCreateInfo* ci, const VkAllocationCallbacks*, VkDevice*) {
    int chained = 0, ext = 0;
    for (uint32_t i = 0; i < ci->queueCreateInfoCount; i++) chained += ci->pQueueCreateInfos[i].pNext != nullptr;
    for (uint32_t i = 0; i < ci->enabledExtensionCount; i++)
        ext += !std::strcmp(ci->ppEnabledExtensionNames[i], VK_KHR_GLOBAL_PRIORITY_EXTENSION_NAME) ||
               !std::strcmp(ci->ppEnabledExtensionNames[i], VK_EXT_GLOBAL_PRIORITY_EXTENSION_NAME);
    std::printf("CREATE chained %d ext %d\n", chained, ext);
    return creates < results.size() ? results[creates++] : VK_SUCCESS;
}
void put(const char* name, const char* value) {
#ifdef _WIN32
    _putenv_s(name, value ? value : "");
#else
    value ? setenv(name, value, 1) : unsetenv(name);
#endif
}
void make(const std::vector<const char*>& names, uint32_t compute_family) {
    std::vector<VkExtensionProperties> exts;
    for (const char* n : names) {
        VkExtensionProperties e{};
        std::snprintf(e.extensionName, sizeof e.extensionName, "%s", n);
        exts.push_back(e);
    }
    std::vector<const char*> want = {VK_KHR_SWAPCHAIN_EXTENSION_NAME};
    static const float priorities[2] = {1.0f, 1.0f};
    std::vector<VkDeviceQueueCreateInfo> qs;
    for (const uint32_t family : {dev.queue_family, compute_family})
        if (family != UINT32_MAX) {
            VkDeviceQueueCreateInfo x{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
            x.queueFamilyIndex = family, x.queueCount = 1, x.pQueuePriorities = priorities;
            qs.push_back(x);
        }
@@PRIORITY@@
    VkDeviceCreateInfo ci{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
    ci.queueCreateInfoCount = (uint32_t)qs.size();
    ci.pQueueCreateInfos = qs.data();
    ci.enabledExtensionCount = (uint32_t)want.size();
    ci.ppEnabledExtensionNames = want.data();
@@MADE@@
    std::printf("MADE %d\n", (int)made);
}
int main(int argc, char** argv) {
    const std::string c = argv[1];
    const char* const KHR = VK_KHR_GLOBAL_PRIORITY_EXTENSION_NAME;
    const char* const EXT = VK_EXT_GLOBAL_PRIORITY_EXTENSION_NAME;
    put("RX_VK_QUEUE_PRIORITY", "low");
    put("RX_VK_PRESENT_QUEUE_PRIORITY", nullptr);
    if (c == "refused_not_permitted") results = {VK_ERROR_NOT_PERMITTED_KHR}, make({KHR}, 1);
    else if (c == "refused_initialization") results = {VK_ERROR_INITIALIZATION_FAILED}, make({KHR}, 1);
    else if (c == "failed_other") results = {VK_ERROR_OUT_OF_HOST_MEMORY}, make({KHR}, 1);
    else if (c == "unread_no_call") props2_given = false, make({KHR}, 1);
    else if (c == "unread_family") dev.queue_family = 5, make({KHR}, UINT32_MAX);   // (the driver gives 3 families)
    else if (c == "unlisted_ext") make({EXT}, 1);
    else if (c == "made_no_own_queue") make({KHR}, UINT32_MAX);
    else if (c == "made_own_queue") put("RX_VK_PRESENT_QUEUE_PRIORITY", "medium"), make({KHR}, 1);
    return 0;
}
"""
CREATE_CASES = ("refused_not_permitted", "refused_initialization", "failed_other", "unread_no_call", "unread_family",
                "unlisted_ext", "made_no_own_queue", "made_own_queue")


def check_create(cxx, headers, tmp):
    """create_device's global priority, cut from vk_main.cpp, run with the made-up driver (CREATE_FIXTURE): the failures"""
    main = (VK / "vk_main.cpp").read_text(encoding="utf-8")
    create = block(main, "bool create_device() {", "vk_main.cpp")
    priority = between(create, "    VkDeviceQueueGlobalPriorityCreateInfoKHR queue_priority[2]", "    // (the presenter thread's waits",
                       "vk_main.cpp create_device")
    made = between(create, "    VkResult made = vkCreateDevice(", "    if (made != VK_SUCCESS) {", "vk_main.cpp create_device")
    source = CREATE_FIXTURE.replace("@@PRIORITY@@", priority).replace("@@MADE@@", made)
    cpp, exe = tmp / "create.cpp", tmp / "create"
    cpp.write_text(source, encoding="utf-8")
    r = subprocess.run([cxx, "-std=c++20", "-O1", "-Wall", "-I" + str(VK), "-I" + str(headers), str(cpp), "-o", str(exe)],
                       capture_output=True, text=True, timeout=300)
    if r.returncode:
        print(r.stdout + r.stderr)
        raise SystemExit("create_device's global priority, cut from vk_main.cpp, did not build in its fixture")
    runs = {}
    for case in CREATE_CASES:
        out = subprocess.run([str(exe), case], capture_output=True, text=True, timeout=30, check=True).stdout.splitlines()
        runs[case] = ([l[4:] for l in out if l.startswith("LOG ")], [l for l in out if l.startswith("CREATE ")],
                      [l for l in out if l.startswith("MADE ")])

    failures = []
    for case, result in (("refused_not_permitted", -1000174001), ("refused_initialization", -3)):
        logs, creates, made_ = runs[case]
        said = [l for l in logs if "vkCreateDevice" in l]
        if said != ["vk: vkCreateDevice refused the queues' global priority (VkResult %d): made again without it" % result] \
                or creates != ["CREATE chained 1 ext 1", "CREATE chained 0 ext 0"] or made_ != ["MADE 0"]:
            failures.append("%s: the device refused with the level asked (VkResult %d): the log %s, %s, %s; expected it "
                            "said refused, and the device made again without the level nor its extension"
                            % (case, result, said, creates, made_))
    logs, creates, made_ = runs["failed_other"]
    said = [l for l in logs if "vkCreateDevice" in l or "global priority asked" in l]
    if said != ["vk: vkCreateDevice failed (VkResult -1) with the global priority asked: made again without it, the cause "
                "may be another"] or creates != ["CREATE chained 1 ext 1", "CREATE chained 0 ext 0"] or made_ != ["MADE 0"]:
        failures.append("failed_other: a device failing out of host memory with the level asked: the log %s, %s, %s; "
                        "expected it made again without the level, the log saying it failed (VkResult -1) with the level "
                        "asked, the cause maybe another, not that the level was refused" % (said, creates, made_))
    for case, family in (("unread_no_call", 0), ("unread_family", 5)):
        logs, creates, made_ = runs[case]
        said = [l for l in logs if l.startswith("vk: RX_VK_QUEUE_PRIORITY=low")]
        if said != ["vk: RX_VK_QUEUE_PRIORITY=low for the replay's queue (family %d): no list of the family's levels could "
                    "be read; asked unlisted" % family] or creates[:1] != ["CREATE chained 1 ext 1"]:
            failures.append("%s: the driver lists the families' levels (VK_KHR_global_priority) but none could be read: "
                            "the log %s, %s; expected it said no list could be read and the level asked unlisted (not that "
                            "the driver has neither extension), the level chained" % (case, said, creates[:1]))
    logs, creates, made_ = runs["unlisted_ext"]
    said = [l for l in logs if l.startswith("vk: RX_VK_QUEUE_PRIORITY=low")]
    if said != ["vk: RX_VK_QUEUE_PRIORITY=low for the replay's queue (family 0): the driver gives no list of the family's "
                "levels (no VK_KHR_global_priority nor VK_EXT_global_priority_query); asked unlisted"] \
            or creates[:1] != ["CREATE chained 1 ext 1"]:
        failures.append("unlisted_ext: VK_EXT_global_priority alone: the log %s, %s; expected it said the driver gives no "
                        "list (neither extension that lists) and the level asked unlisted, the level chained"
                        % (said, creates[:1]))
    for case, want in (("made_no_own_queue", "vk: the device was made with the queues' global priority asked "
                                             "(VK_KHR_global_priority; the replay's low, the presents on the replay's queue "
                                             "(none of their own))"),
                       ("made_own_queue", "vk: the device was made with the queues' global priority asked "
                                          "(VK_KHR_global_priority; the replay's low, the presents' own medium)")):
        logs, creates, made_ = runs[case]
        said = [l for l in logs if l.startswith("vk: the device was made")]
        if said != [want] or made_ != ["MADE 0"]:
            failures.append("%s: the device's line %s, %s; expected %r" % (case, said, made_, want))
    return failures


def check_source():
    """create_device reads both switches by the rules and asks a level only where the family's gate lets it"""
    main = (VK / "vk_main.cpp").read_text(encoding="utf-8")
    create = block(main, "bool create_device() {", "vk_main.cpp")
    steps = ('const char* const names[2] = {"RX_VK_QUEUE_PRIORITY", "RX_VK_PRESENT_QUEUE_PRIORITY"};',
             "const char* const e = getenv(names[k]);",
             "if (!policy_rule::parse_queue_priority(e, level)) {",
             "if (!level) continue;",
             "if (family_of[k] == UINT32_MAX) {",
             "gp.assign(count, ",
             "props2(dev.physical, &count, fp.data());",
             "const VkQueueFamilyGlobalPriorityPropertiesKHR* const g = gp.empty() ? nullptr : &gp[family_of[k]];",
             "const bool in_list = policy_rule::queue_priority_listed(level, g ? g->priorities : nullptr, g ? g->priorityCount : 0);",
             "if (!in_list) continue;",
             "queue_priority[k].globalPriority = (VkQueueGlobalPriorityKHR)level;",
             "x.pNext = &queue_priority[k];",
             "vkCreateDevice(dev.physical, &ci",
             "policy_rule::queue_priority_refused(made)",
             "for (auto& x : qs) x.pNext = nullptr;",
             "            made = vkCreateDevice(dev.physical, &ci")   # (the retry's, its own line)
    at = [create.find(s) for s in steps]
    if -1 in at:
        raise SystemExit("FAIL: create_device: the queues' global priority is not read by device_policy.h's "
                         "parse_queue_priority, asked where its queue_priority_listed lets it, said refused by its "
                         "queue_priority_refused and made again without it: no %s" % steps[at.index(-1)])
    if at != sorted(at):
        raise SystemExit("FAIL: create_device: the queues' global priority's steps are out of order: %s" % ", ".join(steps))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headers", type=Path, required=True)
    parser.add_argument("--cxx", default="g++")
    args = parser.parse_args()
    header = (VK / "device_policy.h").read_text(encoding="utf-8")
    rules = block(header, "namespace policy_rule {", "device_policy.h")
    for name in ("parse_queue_priority(", "queue_priority_name(", "queue_priority_listed("):
        if name not in rules:
            raise SystemExit("FAIL: device_policy.h's policy_rule has no %s(): RX_VK_QUEUE_PRIORITY and "
                             "RX_VK_PRESENT_QUEUE_PRIORITY's level and the family's gate are decided inline in create_device, "
                             "untested" % name.rstrip("("))
    failures = []
    has_refused = "queue_priority_refused(" in rules
    if not has_refused:
        failures.append("device_policy.h's policy_rule has no queue_priority_refused(): which of vkCreateDevice's results "
                        "is the global priority refused is decided inline in create_device, untested")

    cases = ["    parse(%d, %s);" % (i, text) for i, (text, _, _) in enumerate(PARSE)]
    cases += ['    std::printf("NAME %d %%s\\n", policy_rule::queue_priority_name(%du));' % (i, level)
              for i, (level, _) in enumerate(NAMES)]
    for i, (level, levels, count, _) in enumerate(LISTED):
        if levels is None:
            cases.append("    listed(%d, %du, nullptr, %d);" % (i, level, count))
        else:
            room = ", ".join("(VkQueueGlobalPriorityKHR)%d" % v for v in levels + [0] * (17 - len(levels)))
            cases.append("    { const VkQueueGlobalPriorityKHR l[17] = {%s}; listed(%d, %du, l, %d); }" % (room, i, level, count))
    if has_refused:
        cases += ["    std::printf(\"REFUSED %d %%d\\n\", (int)policy_rule::queue_priority_refused(%s));" % (i, result)
                  for i, (result, _) in enumerate(REFUSED)]
    source = FIXTURE.replace("@@CASES@@", "\n".join(cases))
    with tempfile.TemporaryDirectory(prefix="queue-priority-") as tmp:
        tmp = Path(tmp)
        cpp, exe = tmp / "fixture.cpp", tmp / "fixture"
        cpp.write_text(source, encoding="utf-8")
        r = subprocess.run([args.cxx, "-std=c++20", "-O1", "-Wall", "-I" + str(VK), "-I" + str(args.headers), str(cpp), "-o", str(exe)],
                           capture_output=True, text=True, timeout=300)
        if r.returncode:
            print(r.stdout + r.stderr)
            raise SystemExit("the fixture did not build")
        out = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, check=True).stdout.splitlines()
        failures += check_create(args.cxx, args.headers, tmp)

    got = {}
    for line in out:
        word, i, rest = line.split(" ", 2)
        got[(word, int(i))] = rest
    for i, (text, ok, level) in enumerate(PARSE):
        if got.get(("PARSE", i)) != "%d %d" % (ok, level):
            failures.append("parse_queue_priority(%s): %s, expected %d %d (taken, level)" % (text, got.get(("PARSE", i)), ok, level))
    for i, (level, name) in enumerate(NAMES):
        if got.get(("NAME", i)) != name:
            failures.append("queue_priority_name(%d): %s, expected %s" % (level, got.get(("NAME", i)), name))
    for i, (level, levels, count, ok) in enumerate(LISTED):
        if got.get(("LISTED", i)) != str(ok):
            failures.append("queue_priority_listed(%d, %s, %d): %s, expected %d" % (level, levels, count, got.get(("LISTED", i)), ok))
    for i, (result, ok) in enumerate(REFUSED if has_refused else []):
        if got.get(("REFUSED", i)) != str(ok):
            failures.append("queue_priority_refused(%s): %s, expected %d" % (result, got.get(("REFUSED", i)), ok))
    if failures:
        raise SystemExit("FAIL:\n  " + "\n  ".join(failures))
    check_source()
    print("ok: %d values read, %d names, %d family lists, %d results, and create_device asks and says by them in %d cases"
          % (len(PARSE), len(NAMES), len(LISTED), len(REFUSED), len(CREATE_CASES)))


if __name__ == "__main__":
    main()
