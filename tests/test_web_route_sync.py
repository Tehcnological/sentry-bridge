"""The EFB setup on the shared screen: This device plus a list of EFB addresses, and a browser left untouched
following a route another browser (or the follower) saved."""
from pathlib import Path
import json
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent
NAMES = ("updateDestination", "routeState", "pullForm", "pullSettings", "pullValue", "pullCheck", "pullAddresses",
         "snapshotForm", "efbPort", "thisDevice", "shared", "invalidate", "restoreRoutes", "noThisDevice",
         "discoveryJob", "renderDiscovered", "renderEfbList", "addEfb", "removeEfb", "routeRequest", "efbSummary",
         "showAddressError")

# Runs app.js's own functions against a small DOM. Prints the state after each step.
HARNESS = r"""
class El { constructor(id) { this.id = id; this.value = ""; this.checked = false; this.hidden = false;
  this.disabled = false; this.textContent = ""; this.children = []; }
  replaceChildren(...children) { this.children = children; }
  closest() { return $(this.id + "-label"); } }
const els = {};
const $ = id => els[id] || (els[id] = new El(id));
const document = { activeElement: null, querySelectorAll: () => [], body: { classList: { toggle() {} } } };
$("host").value = "windows";
let live = true, checked = false, revision = 0, shownRoute = "", status = null, pending = false, discoveryRoute;
const MAX_EFBS = 16;
let efbAddresses = [], routeLocked = false, lastFound = [], shownEfbs = "";
function element(tag, text, className) { return {tag, text, className, children: [], dataset: {}, disabled: false,
  append(...c) { this.children.push(...c); }, addEventListener(type, fn) { this["on" + type] = fn; },
  setAttribute(name, value) { this[name] = value; }}; }
const LIMITS_DOC = "", LIMITS_SECTION = {};
function appearance() {} function sampleState() {}
const settingsFields = SETTINGS_FIELDS;
const synced = {};
FUNCTIONS
const settings = {sentryIp: "192.168.4.1", listenPort: 4000, efbPort: 4000, httpPort: 80, announcePort: 63093,
  ownshipHex: "", staleSeconds: 15, intervalSeconds: 1, statsSeconds: 10, reference: null};
const rows = () => $("efb-list").children.map(row => row.children[0].text);
function poll(s) { status = s; pullForm(s); return {addresses: rows(), thisDevice: $("same").checked,
  help: $("topology-help").textContent, summary: $("route-summary").textContent, validationHidden: $("validation").hidden,
  checked, results: $("discovered").children.length}; }
const out = {};
// Browser A: This device, validated, with a Find EFB result shown.
let s = {settings, routesRequest: {host: "windows", thisDevice: true, addresses: []}};
status = s; restoreRoutes(s.routesRequest);
checked = true; $("validation").hidden = false; $("discovered").children = ["Device · 192.168.4.15"];
out.same = poll(s);
// Browser B saved two other devices.
s = {settings, routesRequest: {host: "windows", thisDevice: false, addresses: ["192.168.4.20", "192.168.4.21"]}};
out.network = poll(s);
// This browser adds one; a poll of the unchanged saved route keeps the local edit.
checked = true;
$("address").value = " 192.168.4.30 "; addEfb($("address").value);
out.added = {addresses: rows(), field: $("address").value, checked};
out.kept = poll(s);
addEfb("192.168.4.30"); out.duplicate = {error: $("address-error").textContent, addresses: rows()};
addEfb(""); out.empty = $("address-error").textContent;
removeEfb("192.168.4.30"); out.removed = rows();
// The follower replaced .20 (saved elsewhere): an untouched browser shows it and drops Find EFB results.
$("discovered").children = ["Device · 192.168.4.15"];
s = {settings, routesRequest: {host: "windows", thisDevice: false, addresses: ["192.168.4.6", "192.168.4.21"]}};
out.followed = poll(s);
// Find EFB results: a listed device says Added; Add appends another.
renderDiscovered([{app: "Device", address: "192.168.4.6", port: 4000}, {app: "Avare", address: "192.168.4.50", port: 4000}]);
const found = $("discovered").children.map(row => ({text: row.children[1].text, disabled: row.children[1].disabled}));
$("discovered").children[1].children[1].onclick();
out.find = {found, addresses: rows()};
// Sixteen at most.
for (let n = 100; rows().length < 16; n++) addEfb("192.168.4." + n);
$("address-error").textContent = ""; addEfb("192.168.4.250");
out.limit = {count: rows().length, error: $("address-error").textContent, addDisabled: $("add-efb").disabled};
// What Start sends, and the summary with This device on.
efbAddresses = ["192.168.4.20", "192.168.4.21"]; $("same").checked = true; updateDestination();
out.request = routeRequest();
out.summary = $("route-summary").textContent;
// Row states while running.
status = {...s, running: true, efbs: [{address: "192.168.4.20", state: "answering"}, {address: "192.168.4.21", state: "not seen yet"}]};
renderEfbList();
out.states = $("efb-list").children.map(row => row.children[1].text);
// An unchanged list keeps its rows between polls, so a tap on Remove is not lost.
const firstRow = $("efb-list").children[0];
renderEfbList(); poll(status);
out.stable = {same: firstRow === $("efb-list").children[0], label: firstRow.children[2]["aria-label"]};
// Check setup errors that name an address go under the field.
showAddressError("192.168.4.21: listed twice."); out.checkError = $("address-error").textContent;
// Reload while forwarding to a saved route: the first poll is not a change.
for (const key of Object.keys(synced)) delete synced[key];
const saved = {settings, routesRequest: {host: "windows", thisDevice: false, addresses: ["192.168.4.30"]}, running: true};
status = saved; restoreRoutes(saved.routesRequest);
checked = true; $("validation").hidden = false;
out.reload = poll(saved);
// Find EFB results against route changes, in renderLive's order: pullForm, then the job result.
const job = {action: "Find EFB", state: "done", result: {efbs: [{app: "Device", address: "192.168.4.31", port: 4000}]}};
const running = {action: "Find EFB", state: "running"};
const moved = n => ({...saved, job, routesRequest: {host: "windows", thisDevice: false, addresses: ["192.168.4.4" + n]}});
const same = {...saved, job};
const results = {};
const count = () => $("discovered").children.length;
function render(s, job) { const before = shownRoute; poll(s); if (job) discoveryJob(job, shownRoute !== before); }
status = same; discoveryRoute = undefined; render(same, job); results.reload = count();
render(same, running); render(same, job); results.unchanged = count();
render(same, running); render(moved(1), job); results.samePoll = count();
render(same, running); render(moved(2)); render(moved(2), job); results.laterPoll = count();
render(moved(3)); discoveryRoute = undefined; render(moved(4), running); render(moved(4), job);
results.firstSeenRunning = count();
render(moved(4), running); render(moved(4), job); results.nextFind = count();
out.results = results;
// This device is hidden where EFBs do not share the receiver port, unless the simulator runs.
const hidden = {};
for (const host of ["windows", "macos", "linux", "android", "esp32"]) {
  $("host").value = host; $("same").checked = true; status = {settings, simulator: false}; updateDestination();
  hidden[host] = {hidden: $("same-label").hidden, on: thisDevice()};
}
$("host").value = "android"; status = {settings, simulator: true}; $("same").checked = true; updateDestination();
hidden.androidSimulator = {hidden: $("same-label").hidden, on: thisDevice()};
out.hidden = hidden;
console.log(JSON.stringify(out));
"""


def node():
    return shutil.which("node") or next((p for p in (r"C:\Program Files\nodejs\node.exe",) if Path(p).is_file()), None)


def function_source(script, name):
    start = script.index(f"function {name}(")
    depth = 0
    for i in range(script.index("{", start), len(script)):
        depth += {"{": 1, "}": -1}.get(script[i], 0)
        if depth == 0:
            return script[start:i + 1]
    raise ValueError(name)


class TestWebRouteSync(unittest.TestCase):
    def test_efb_list_on_the_shared_screen(self):
        script = (ROOT / "web/app.js").read_text(encoding="utf-8")
        self.assertIn("if (routeState() !== shownRoute) { $(\"discovered\").replaceChildren(); lastFound = []; discoveryRoute = null; updateDestination(); }", script)
        if node() is None:
            return  # shortcut: this host has no Node; the line above still pins the refresh
        fields = re.search(r"const settingsFields = (\{[^}]*\});", script).group(1)
        harness = HARNESS.replace("SETTINGS_FIELDS", fields).replace(
            "FUNCTIONS", "\n".join(function_source(script, name) for name in NAMES))
        result = subprocess.run([node(), "-"], input=harness, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["same"]["addresses"], [])
        self.assertTrue(out["same"]["thisDevice"])
        self.assertTrue(out["same"]["checked"])  # an unchanged poll keeps the validation
        self.assertEqual(out["same"]["results"], 1)  # and the Find EFB results
        network = out["network"]
        self.assertEqual(network["addresses"], ["192.168.4.20", "192.168.4.21"])
        self.assertFalse(network["thisDevice"])
        self.assertNotIn("127.0.0.1", network["help"])
        self.assertEqual(network["summary"], "UDP 4000 · 2 others")
        self.assertTrue(network["validationHidden"])
        self.assertFalse(network["checked"])
        self.assertEqual(network["results"], 0)  # Find EFB results may list the old address
        self.assertEqual(out["added"], {"addresses": ["192.168.4.20", "192.168.4.21", "192.168.4.30"], "field": "", "checked": False})
        self.assertEqual(out["kept"]["addresses"], ["192.168.4.20", "192.168.4.21", "192.168.4.30"])
        self.assertEqual(out["duplicate"], {"error": "192.168.4.30: listed twice.",
                                            "addresses": ["192.168.4.20", "192.168.4.21", "192.168.4.30"]})
        self.assertEqual(out["empty"], "Enter the EFB device's IPv4 address.")
        self.assertEqual(out["removed"], ["192.168.4.20", "192.168.4.21"])
        self.assertEqual(out["followed"]["addresses"], ["192.168.4.6", "192.168.4.21"])
        self.assertEqual(out["followed"]["results"], 0)
        self.assertEqual(out["find"]["found"], [{"text": "Added", "disabled": True}, {"text": "Add", "disabled": False}])
        self.assertEqual(out["find"]["addresses"], ["192.168.4.6", "192.168.4.21", "192.168.4.50"])
        self.assertEqual(out["limit"], {"count": 16, "error": "Up to 16 EFB addresses.", "addDisabled": True})
        self.assertEqual(out["request"], {"host": "windows", "thisDevice": True, "addresses": ["192.168.4.20", "192.168.4.21"]})
        self.assertEqual(out["summary"], "UDP 4000 · This device + 2 others · shared")
        self.assertEqual(out["states"], ["answering", "not seen yet"])
        self.assertEqual(out["stable"], {"same": True, "label": "Remove 192.168.4.20"})
        self.assertEqual(out["checkError"], "192.168.4.21: listed twice.")
        self.assertTrue(out["reload"]["checked"], out["reload"])  # restored route, unchanged poll: nothing invalidated
        self.assertEqual(out["reload"]["addresses"], ["192.168.4.30"])
        # A Find EFB result from before a route change never comes back after the clear.
        self.assertEqual(out["results"], {"reload": 1, "unchanged": 1, "samePoll": 0, "laterPoll": 0,
                                          "firstSeenRunning": 0, "nextFind": 1})
        hidden = out["hidden"]
        for host in ("windows", "macos"):
            self.assertEqual(hidden[host], {"hidden": False, "on": True}, host)
        for host in ("linux", "android", "esp32"):
            self.assertEqual(hidden[host], {"hidden": True, "on": False}, host)
        self.assertEqual(hidden["androidSimulator"], {"hidden": False, "on": True})
        # renderLive pulls the form before it writes the running output and the route locks.
        render = function_source(script, "renderLive")
        self.assertLess(render.index("pullForm(s)"), render.index('$("output-summary")'))
        self.assertLess(render.index("pullForm(s)"), render.index('$("same").disabled'))
        self.assertLess(render.index("pullForm(s)"), render.index("discoveryJob(s.job, shownRoute !== routeBefore)"))


if __name__ == "__main__":
    unittest.main()
