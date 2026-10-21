"""A browser left untouched follows a route another browser saved: address field, helper and validation."""
from pathlib import Path
import json
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent
NAMES = ("updateDestination", "routeState", "pullForm", "pullSettings", "pullValue", "pullCheck",
         "snapshotForm", "efbPort", "shared", "invalidate", "restoreRoutes", "noThisDevice",
         "discoveryJob", "renderDiscovered")

# Runs app.js's own functions against a small DOM. Prints the state after each poll.
HARNESS = r"""
class El { constructor(id) { this.id = id; this.value = ""; this._checked = false; this.hidden = false;
  this.disabled = false; this.textContent = ""; this.children = []; }
  replaceChildren(...children) { this.children = children; }
  closest() { return $(this.id + "-label"); }
  get checked() { return this._checked; }
  set checked(on) {  // the two topology radios share a group, as in the page
    if (on && (this.id === "same" || this.id === "network")) $(this.id === "same" ? "network" : "same")._checked = false;
    this._checked = on; } }
const els = {};
const $ = id => els[id] || (els[id] = new El(id));
const document = { activeElement: null, querySelectorAll: () => [], body: { classList: { toggle() {} } },
  querySelector: () => [$("same"), $("network")].find(e => e.checked) };
const topology = () => document.querySelector().value;
$("same").value = "same"; $("network").value = "network"; $("host").value = "windows";
let live = true, checked = false, revision = 0, shownRoute = "", status = null, pending = false, discoveryRoute;
function element(tag, text) { return {tag, text, append() {}, addEventListener() {}}; }
const LIMITS_DOC = "", LIMITS_SECTION = {};
function appearance() {} function sampleState() {}
const settingsFields = SETTINGS_FIELDS;
const synced = {};
FUNCTIONS
const settings = {sentryIp: "192.168.4.1", listenPort: 4000, efbPort: 4000, httpPort: 80, announcePort: 63093,
  ownshipHex: "", staleSeconds: 15, intervalSeconds: 1, statsSeconds: 10, reference: null};
function poll(s) { status = s; pullForm(s); return {address: $("address").value, hidden: $("address-field").hidden,
  help: $("topology-help").textContent, validationHidden: $("validation").hidden, checked,
  results: $("discovered").children.length}; }
// Browser A: This device, validated.
status = {settings, routesRequest: {topology: "same"}};
$("same").checked = true; snapshotForm(); updateDestination();
checked = true; $("validation").hidden = false; $("discovered").children = ["Device · 192.168.4.15"];
const out = [poll({settings, routesRequest: {topology: "same"}})];
// Browser B saved Other device 192.168.4.20.
out.push(poll({settings, routesRequest: {topology: "network", address: "192.168.4.20"}}));
checked = true; $("validation").hidden = false;
// Browser B moved the EFB port only.
out.push(poll({settings: {...settings, efbPort: 4001}, routesRequest: {topology: "network", address: "192.168.4.20"}}));
// Reload while forwarding to a saved Other device route: the first poll is not a change.
for (const id of ["address", "same", "network"]) delete els[id];
for (const key of Object.keys(synced)) delete synced[key];
$("same").value = "same"; $("network").value = "network";
const saved = {settings, routesRequest: {topology: "network", address: "192.168.4.30"}, running: true};
status = saved; restoreRoutes(saved.routesRequest);
checked = true; $("validation").hidden = false; $("output-summary").textContent = "192.168.4.30:4000";
out.push(poll(saved));
out[out.length - 1].output = $("output-summary").textContent;
// Find EFB results against route changes, in renderLive's order: pullForm, then the job result.
const found = {action: "Find EFB", state: "done", result: {efbs: [{app: "Device", address: "192.168.4.30", port: 4000}]}};
const running = {action: "Find EFB", state: "running"};
const moved = n => ({...saved, job: found, routesRequest: {topology: "network", address: "192.168.4.4" + n}});
const same = {...saved, job: found};
const results = {};
status = same; discoveryRoute = undefined; discoveryJob(found); results.reload = $("discovered").children.length;  // a finished job on the first poll
discoveryJob(running); poll(same); discoveryJob(found); results.unchanged = $("discovered").children.length;
discoveryJob(running); poll(moved(1)); discoveryJob(found); results.samePoll = $("discovered").children.length;
// renderLive passes a job on only when it changes, so the running job is seen once.
discoveryJob(running); poll(moved(2)); poll(moved(2)); discoveryJob(found);
results.laterPoll = $("discovered").children.length;
out.push(results);
// Android: This device is hidden (EFBs there hold UDP 4000) unless the simulator runs.
$("host").value = "android"; $("same").checked = true; status = {settings, simulator: false};
updateDestination();
const android = {hidden: $("same-label").hidden, network: $("network").checked};
status = {settings, simulator: true}; updateDestination();
android.simulatorHidden = $("same-label").hidden;
out.push(android);
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
    def test_untouched_browser_shows_a_route_saved_elsewhere(self):
        script = (ROOT / "web/app.js").read_text(encoding="utf-8")
        self.assertIn("if (routeState() !== shownRoute) { $(\"discovered\").replaceChildren(); discoveryRoute = null; updateDestination(); }", script)
        if node() is None:
            return  # shortcut: this host has no Node; the line above still pins the refresh
        fields = re.search(r"const settingsFields = (\{[^}]*\});", script).group(1)
        harness = HARNESS.replace("SETTINGS_FIELDS", fields).replace(
            "FUNCTIONS", "\n".join(function_source(script, name) for name in NAMES))
        result = subprocess.run([node(), "-"], input=harness, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        same, network, port, reload, results, android = json.loads(result.stdout)
        # A Find EFB result from before a route change never comes back after the clear.
        self.assertEqual(results, {"reload": 1, "unchanged": 1, "samePoll": 0, "laterPoll": 0})
        self.assertEqual(android, {"hidden": True, "network": True, "simulatorHidden": False})
        self.assertTrue(same["hidden"])
        self.assertTrue(same["checked"])  # an unchanged poll keeps the validation
        self.assertEqual(same["results"], 1)  # and the Find EFB results
        self.assertEqual(network["address"], "192.168.4.20")
        self.assertFalse(network["hidden"])  # the address field appears
        self.assertNotIn("127.0.0.1", network["help"])
        self.assertTrue(network["validationHidden"])
        self.assertFalse(network["checked"])  # the This device validation no longer counts
        self.assertEqual(network["results"], 0)  # Find EFB results may list the old address
        self.assertIn("UDP 4001", port["help"])
        self.assertFalse(port["checked"])
        self.assertTrue(reload["checked"], reload)  # restored route, unchanged poll: nothing invalidated
        self.assertEqual(reload["output"], "192.168.4.30:4000")
        # renderLive pulls the form before it writes the running output and the route locks.
        render = function_source(script, "renderLive")
        self.assertLess(render.index("pullForm(s)"), render.index('$("output-summary")'))
        self.assertLess(render.index("pullForm(s)"), render.index('$("same").disabled'))
        self.assertLess(render.index("pullForm(s)"), render.index("discoveryJob(s.job)"))  # the order the fixture uses


if __name__ == "__main__":
    unittest.main()
