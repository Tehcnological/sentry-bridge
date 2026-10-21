"use strict";
const $ = id => document.getElementById(id);
let ready = false, checked = false, checking = false, revision = 0, shownRoute = "";
let discoveryRoute;  // the route shown when Find EFB started; null after a route change
let live = false, status = null, pending = false, uiError = "", filesRevision = "", jobRevision = "";
const topology = () => document.querySelector('input[name="topology"]:checked').value;
function element(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}
function preferences() {
  try { return JSON.parse(localStorage.getItem("sentrybridge-preview-appearance")) || {}; }
  catch { return {}; }
}
const saved = preferences();
$("theme").value = ["system","light","dark"].includes(saved.theme) ? saved.theme : "dark";
$("motion").checked = saved.motion === true;
$("expressive").checked = typeof saved.expressive === "boolean" ? saved.expressive : true;
const systemTheme = matchMedia("(prefers-color-scheme: dark)");
function appearance() {
  document.body.classList.toggle("dark", $("theme").value === "dark" || ($("theme").value === "system" && systemTheme.matches));
  document.body.classList.toggle("reduced-motion", $("motion").checked);
  document.body.classList.toggle("expressive", $("host").value === "android" && $("expressive").checked);
  try { localStorage.setItem("sentrybridge-preview-appearance", JSON.stringify({theme:$("theme").value,motion:$("motion").checked,expressive:$("expressive").checked})); } catch { /* Storage is optional. */ }
}
systemTheme.addEventListener("change", appearance);
for (const id of ["theme","motion","expressive"]) $(id).addEventListener("change", appearance);
const tabs = ["bridge","settings","diagnostics"];
function selectTab(name, focus = false) {
  for (const tab of tabs) {
    const selected = tab === name;
    $(tab+"-tab").setAttribute("aria-selected", String(selected));
    $(tab+"-tab").tabIndex = selected ? 0 : -1;
    $(tab+"-panel").hidden = !selected;
  }
  if (focus) $(name+"-tab").focus();
}
tabs.forEach((name,index) => {
  $(name+"-tab").addEventListener("click", () => selectTab(name));
  $(name+"-tab").addEventListener("keydown", event => {
    let next;
    if (event.key === "ArrowRight") next = (index+1)%tabs.length;
    if (event.key === "ArrowLeft") next = (index+tabs.length-1)%tabs.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = tabs.length-1;
    if (next !== undefined) { event.preventDefault(); selectTab(tabs[next],true); }
  });
});
function sampleState() {
  const state = $("scenario").value;
  $("receiver-heading").textContent = {stopped:"Stopped",waiting:"Waiting for Sentry",running:"Receiving",lost:"Disconnected",mode:"Mode off"}[state];
  $("gps").textContent = state === "running" ? "3D fix" : "-";
  $("traffic").textContent = state === "running" ? "12" : "-";
  $("weather").textContent = state === "running" ? "47" : "-";
  $("age").textContent = state === "running" ? "< 1s" : state === "lost" ? "> 15s" : "-";
  $("start").textContent = state === "stopped" ? "Start" : "Stop"; $("start").classList.toggle("stop",state !== "stopped");
  $("warning").hidden = !["lost","mode"].includes(state);
  $("warning").textContent = state === "lost" ? "No receiver data. Check Sentry power and WiFi." : "Compatibility Mode is off. Enable it on the receiver.";
}
$("scenario").addEventListener("change",sampleState);
$("start").addEventListener("click",async () => {
  const wantStart = live ? !status?.running : $("scenario").value === "stopped";
  if (wantStart && !(await confirmLimits())) return;
  if (live) {
    if (!wantStart) { await act("stop"); return; }
    if (!checked && !(await checkSetup())) { selectTab("bridge"); return; }
    if (status?.running) return;
    await act("start",routeRequest());
    return;
  }
  if (!wantStart) { $("scenario").value = "stopped"; sampleState(); return; }
  if (!checked && !(await checkSetup())) { selectTab("bridge"); return; }
  $("scenario").value = "waiting";
  sampleState();
});
function invalidate() {
  revision++;
  checked = false;
  $("validation").hidden = true;
  $("output-summary").textContent = "Not configured";
  if (!live && $("scenario").value !== "stopped") { $("scenario").value = "stopped"; sampleState(); }
}
// One GDL90 route: This device (127.0.0.1) or Other device (its address), on the EFB port from Settings.
function efbPort() { return status?.settings?.efbPort ?? 4000; }
function shared() { return topology() === "same" && efbPort() === (status?.settings?.listenPort ?? 4000); }
// Compatibility and limitations: one section per platform in docs/Compatibility.md, linked from the bottom of
// Settings and from a dialog before each Start until "Don't show again" is saved with Start.
const LIMITS_DOC = "https://github.com/Tehcnological/sentry-bridge/blob/master/docs/Compatibility.md";
const LIMITS_SECTION = {android:"android",windows:"windows",macos:"macos-and-linux",linux:"macos-and-linux",esp32:"small-boards-esp32-c3-pico-w-pico-2-w"};
const LIMITS_KEY = "sentrybridge-hide-limits";
function confirmLimits() {
  try { if (localStorage.getItem(LIMITS_KEY) === "1") return Promise.resolve(true); } catch { /* Storage is optional. */ }
  const dialog = $("limits-dialog");
  $("limits-hide").checked = false;
  dialog.showModal();
  // Button handlers, not the dialog's close event: the decision is known at the click on every WebView.
  return new Promise(resolve => {
    const finish = start => {
      if (start && $("limits-hide").checked) { try { localStorage.setItem(LIMITS_KEY,"1"); } catch { /* Storage is optional. */ } }
      if (dialog.open) dialog.close();
      resolve(start);
    };
    $("limits-start").onclick = () => finish(true);
    $("limits-cancel").onclick = () => finish(false);
    dialog.oncancel = event => { event.preventDefault(); finish(false); };  // Escape
  });
}
// No EFB on ESP32; Android EFBs hold UDP 4000 without sharing, so only the simulator uses This device there.
function noThisDevice() {
  const host = $("host").value;
  return host === "esp32" || host === "android" && !status?.simulator;
}
function updateDestination() {
  const host = $("host").value;
  document.querySelectorAll(".limits-link").forEach(link => link.href = LIMITS_DOC+"#"+(LIMITS_SECTION[host] || "all-platforms"));
  $("same").disabled = noThisDevice();
  $("same").closest("label").hidden = noThisDevice();
  if ($("same").disabled) $("network").checked = true;
  const local = topology() === "same";
  $("address-field").hidden = local;
  if (live) $("discovery").hidden = local || !status?.settings?.announcePort;
  $("topology-help").textContent = local ? (shared() ? "EFB output: 127.0.0.1 on UDP "+efbPort()+", shared with the EFB. One EFB at a time receives it." : "EFB output: 127.0.0.1 on UDP "+efbPort()+".") : "Use the EFB device's IPv4 address on the receiver WiFi, UDP "+efbPort()+".";
  $("route-summary").textContent = "UDP "+efbPort()+(shared() ? " · shared" : "");
  $("expressive-row").hidden = host !== "android";
  document.body.classList.toggle("mobile",host === "android");
  shownRoute = routeState();
  invalidate();
  appearance();
}
for (const id of ["host","same","network"]) $(id).addEventListener("change",updateDestination);
$("address").addEventListener("input",invalidate);
$("find-efb").addEventListener("click",() => { $("discovered").replaceChildren(); discoveryRoute = shownRoute; act("discover",{seconds:6}); });
function renderDiscovered(efbs) {
  const rows = efbs.map(efb => {
    const use = element("button","Use");
    use.disabled = pending || !!status?.monitoring || status?.job.state === "running";
    use.addEventListener("click",() => { $("address").value = efb.address; invalidate(); });
    const row = element("li");
    row.append(element("span",`${efb.app} · ${efb.address} · port ${efb.port}`),use);
    return row;
  });
  if (!rows.length) rows.push(element("li","No EFB or other device found on the receiver's WiFi. Enter the address from the EFB device's WiFi details.","note"));
  $("discovered").replaceChildren(...rows);
}
// Results found before a route change may list the old address: show only those for the current route.
// A job first seen running in the poll that changed the route may have started on the old one.
function discoveryJob(job, routeChanged) {
  if (job.action !== "Find EFB") return;
  if (job.state === "running") discoveryRoute = routeChanged ? null : shownRoute;
  if (job.state === "done" && (discoveryRoute === undefined || discoveryRoute === shownRoute)) renderDiscovered(job.result.efbs || []);
}
async function checkSetup() {
  if (checking || !ready) return false;
  const before = revision;
  checking = true;
  $("check").disabled = true; $("start").disabled = true;
  const view = $("validation");
  view.replaceChildren(); view.hidden = false; view.classList.remove("error"); view.textContent = "Checking…";
  try {
    const response = await fetch("/api/validate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(routeRequest())});
    const result = await response.json();
    if (before !== revision) return false;
    if (!response.ok) throw new Error(result.error || "Setup check failed.");
    view.replaceChildren(element("strong","Route valid"));
    const list = element("ul"); result.routes.forEach(route => list.append(element("li",route.address+":"+route.port+(route.shared ? " · shared" : "")))); view.append(list);
    const details = element("details"); details.append(element("summary","Validation details"));
    result.warnings.forEach(warning => details.append(element("p",warning)));
    details.append(element("p",live ? "Applies when you press Start. EFB display unverified." : "Preview only. Nothing applied; EFB display unverified.")); view.append(details);
    $("output-summary").textContent = result.routes.map(route => route.address+":"+route.port).join(", ");
    checked = true;
    return true;
  } catch (error) {
    if (before === revision) { view.hidden = false; view.classList.add("error"); view.textContent = error.message; }
    return false;
  } finally {
    checking = false; $("check").disabled = !ready; $("start").disabled = !ready;
  }
}
$("check").addEventListener("click",checkSetup);
function routeRequest() {
  return {host:$("host").value,topology:topology(),address:topology() === "same" ? "127.0.0.1" : $("address").value.trim()};
}
const settingsFields = {sentryIp:"sentry-ip",listenPort:"listen-port",efbPort:"efb-port",httpPort:"http-port",announcePort:"announce-port",ownshipHex:"ownship",staleSeconds:"stale",intervalSeconds:"interval",statsSeconds:"stats-interval"};
const synced = {};
function snapshotForm() {
  for (const id of [...Object.values(settingsFields), "ref-lat", "ref-lon", "address"]) synced[id] = $(id).value;
  for (const id of ["auto-restore", "relay", "update-check"]) synced[id] = $(id).checked;
  synced.topology = topology();
}
function pullValue(id, value) {
  const el = $(id);
  const next = value == null ? "" : String(value);
  if (el === document.activeElement) return;
  if (synced[id] !== undefined && el.value !== synced[id] && el.value !== next) return;
  el.value = next;
  synced[id] = next;
}
function pullCheck(id, value) {
  const el = $(id);
  const next = !!value;
  if (el === document.activeElement) return;
  if (synced[id] !== undefined && el.checked !== synced[id] && el.checked !== next) return;
  el.checked = next;
  synced[id] = next;
}
function pullForm(s) {
  const settings = s.settings;
  if (!settings) return;
  pullSettings(s, settings);
  // Another browser, or the bridge following a moved EFB, changed the route: show it, and drop stale
  // validation and Find EFB results (they may list the old address).
  if (routeState() !== shownRoute) { $("discovered").replaceChildren(); discoveryRoute = null; updateDestination(); }
}
function routeState() {
  return [$("host").value, topology(), synced.address ?? "", efbPort(), status?.settings?.listenPort, status?.settings?.announcePort].join("|");
}
function pullSettings(s, settings) {
  for (const [key, id] of Object.entries(settingsFields)) pullValue(id, settings[key]);
  pullCheck("auto-restore", settings.autoRestore);
  pullCheck("relay", settings.relay);
  pullCheck("update-check", !!settings.updateCheck);
  pullValue("ref-lat", settings.reference?.[0] ?? "");
  pullValue("ref-lon", settings.reference?.[1] ?? "");
  const request = s.routesRequest;
  if (!request) return;
  const nextTopo = request.topology === "same" ? "same" : "network";
  const address = nextTopo === "same" ? "" : (request.address || "");
  const routeFocus = document.activeElement === $("address") || document.activeElement === $("same") || document.activeElement === $("network");
  if (!routeFocus && (synced.topology === undefined || topology() === synced.topology || topology() === nextTopo)) {
    $(nextTopo === "same" ? "same" : "network").checked = true;
    synced.topology = nextTopo;
    pullValue("address", address);
  }
}
function fillSettings(s) {
  for (const [key,id] of Object.entries(settingsFields)) $(id).value = s[key];
  $("auto-restore").checked = s.autoRestore;
  $("relay").checked = s.relay;
  $("update-check").checked = !!s.updateCheck;
  $("ref-lat").value = s.reference?.[0] ?? "";
  $("ref-lon").value = s.reference?.[1] ?? "";
  snapshotForm();
}
function settingsRequest() {
  const s = {};
  for (const [key,id] of Object.entries(settingsFields)) s[key] = ["sentryIp","ownshipHex"].includes(key) ? $(id).value.trim() : Number($(id).value);
  s.autoRestore = $("auto-restore").checked; s.relay = $("relay").checked; s.updateCheck = $("update-check").checked;
  const lat = $("ref-lat").value, lon = $("ref-lon").value;
  if ((lat === "") !== (lon === "")) throw new Error("Enter both reference coordinates, or leave both blank.");
  s.reference = lat === "" ? null : [Number(lat),Number(lon)];
  return s;
}
async function api(action, body = {}) {
  const response = await fetch("/api/actions/"+action,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Operation failed.");
  return data;
}
function showError(error) {
  uiError = error.message;
  $("warning").hidden = false; $("warning").textContent = uiError;
  $("operation").textContent = uiError; $("operation").classList.add("error");
}
async function act(action, body = {}) {
  if (pending) return null;
  pending = true;
  uiError = "";
  if (status) renderLive(status);
  try {
    const result = await api(action,body);
    await refreshStatus();
    return result;
  } catch (error) { showError(error); return null; }
  finally { pending = false; if (status) renderLive(status); }
}
function updateFiles(files) {
  const version = JSON.stringify([files,status?.job.state,status?.recording,pending]);
  if (version === filesRevision) return;
  filesRevision = version;
  $("files").replaceChildren();
  for (const file of files) {
    const row = element("div");
    if (file.ready) {
      const link = element("a",file.name); link.href = "/api/files/"+file.id; link.download = file.name; row.append(link);
    } else row.append(element("span",file.name+(file.kind === "capture" && status?.recording ? " · recording" : status?.job.state === "running" ? " · preparing" : " · not created")));
    const remove = element("button","Delete"); remove.setAttribute("aria-label","Delete "+file.name);
    remove.disabled = pending || status?.job.state === "running" || !file.ready && file.kind === "capture" && status?.recording;
    remove.addEventListener("click",() => act("delete-file",{id:file.id})); row.append(remove);
    $("files").append(row);
  }
  for (const id of ["analysis-capture","weather-source","weather-output"]) {
    const selected = $(id).value;
    $(id).replaceChildren();
    files.filter(file => file.kind === "capture" && file.ready).forEach(file => {
      const option = element("option",file.name+" · "+file.id.slice(0,6)); option.value = file.id; $(id).append(option);
    });
    if ([...$(id).options].some(option => option.value === selected)) $(id).value = selected;
  }
}
function renderLive(s) {
  status = s;
  // Android shows the bar only for the simulator, so simulated traffic is never mistaken for real.
  const adapterLabel = s.simulator ? "Simulator · local traffic only" : s.host === "android" ? "" : "Local bridge";
  $("adapter-label").textContent = adapterLabel;
  $("adapter-label").parentElement.hidden = !adapterLabel;
  $("receiver-heading").textContent = s.recording && !s.running ? "Recording" : {stopped:"Stopped",waiting:"Waiting for Sentry",running:"Receiving",lost:"Disconnected",mode:"Mode off"}[s.state];
  $("start").textContent = s.running ? "Stop" : "Start"; $("start").classList.toggle("stop",!!s.running);
  $("start").disabled = pending || s.job.state === "running" && !s.running || s.monitoring && !s.running;
  // GPS status comes only from heartbeats; weather or traffic alone leave it unknown.
  const heartbeat = s.monitoring && s.mode !== "ENCRYPTED" && s.heartbeatSeconds != null && s.heartbeatSeconds <= 3;
  $("gps").textContent = heartbeat ? s.gps ? "Fix" : "No fix" : "-";
  $("traffic").textContent = s.running ? String(s.traffic) : "-";
  $("weather").textContent = s.monitoring ? String(s.weather) : "-";
  $("age").textContent = s.lastPacketSeconds === null ? "-" : Math.round(s.lastPacketSeconds*10)/10+"s";  // Android sends the raw float
  // Read from the receiver's statistics WebSocket while forwarding; null when stale or not sent.
  const sensors = s.sensors || {};
  $("battery").textContent = sensors.batteryPercent == null ? "-" : sensors.batteryPercent+"%";
  $("co").textContent = sensors.coPpm == null ? "-" : sensors.coPpm+" ppm";
  $("temperature").textContent = sensors.temperatureC == null ? "-" : sensors.temperatureC.toFixed(1)+"°C";
  // The receiver's own alarm level when it reported one; otherwise 50 ppm, not called the receiver's.
  const reported = s.device?.settings?.coAlarmLevel > 0, coLevel = reported ? s.device.settings.coAlarmLevel : 50;
  const co = sensors.coPpm >= coLevel ? "Carbon monoxide "+sensors.coPpm+" ppm, at or above "+(reported ? "the receiver's alarm level ("+coLevel+" ppm)." : "50 ppm.") : "";
  $("co").classList.toggle("alarm",!!co);
  const power = s.device?.settings?.Power?.powerSavings;
  const problem = co || uiError || s.error || (s.job.state === "failed" ? s.job.action+": "+s.job.error : "") || (s.state === "lost" ? "No receiver data. Check Sentry power and WiFi." : s.state === "mode" ? "Compatibility Mode is off. Enable it in Settings." : power === true ? "Power Savings is on. Disable it in Sentry's web settings before flight." : "");
  $("warning").textContent = problem; $("warning").hidden = !problem;
  // The backend says whether this install may check (an F-Droid install updates through F-Droid).
  $("update-row").hidden = !s.updateSupported;
  $("update-note").hidden = !s.update;
  if (s.update) { $("update-note").textContent = "Update available: v"+s.update.version; $("update-note").href = s.update.url; }
  $("diagnostic-mode").textContent = s.mode;
  $("diagnostic-input").textContent = "UDP "+s.settings.listenPort;
  $("engine").textContent = s.engine;
  // Before the running output and the route locks below: a refreshed route resets both.
  const routeBefore = shownRoute;
  if (!pending) pullForm(s);
  if (s.running && s.routesRequest) $("output-summary").textContent = s.routesRequest.address+":"+s.settings.efbPort;
  const currentMode = s.monitoring && s.lastPacketSeconds !== null && s.lastPacketSeconds < 10 ? s.mode : null;
  // Running but the receiver is silent: an earlier result says nothing about it now (it resets to ForeFlight mode at power-up).
  const silent = s.monitoring && !currentMode;
  const knownMode = currentMode || (silent ? "" : s.modeObservation?.mode || "");
  const on = knownMode.startsWith("PUBLIC"), off = knownMode.startsWith("ENCRYPTED");
  const modeLabel = on || off ? (on ? "On" : "Off")+(currentMode ? "" : " · last checked") : silent ? "No receiver data" : s.modeRequested === true ? "On requested · unverified" : s.modeRequested === false ? "Off requested · unverified" : "Not checked";
  $("mode-value").textContent = pending || s.job.state === "running" && ["Compatibility Mode","Automatic restoration"].includes(s.job.action) ? "Updating…" : modeLabel;
  if (!pending && s.job.state !== "running") $("compatibility").checked = on || !off && !silent && s.modeRequested === true;
  $("compatibility").disabled = pending || s.job.state === "running";
  $("power-value").textContent = "Power Savings: "+(power === true ? "on" : power === false ? "off" : "not checked");
  const receiverWeb = $("receiver-web");
  receiverWeb.hidden = s.simulator || !s.settings.sentryIp;
  receiverWeb.href = "http://"+s.settings.sentryIp+":"+s.settings.httpPort+"/settings/";
  const locked = pending || s.monitoring || s.job.state === "running";
  $("discovery").hidden = topology() === "same" || !s.settings.announcePort;
  // Find EFB only reads announcements, so it works while forwarding; Use edits the route and stays locked.
  $("find-efb").disabled = pending || s.job.state === "running";
  $("discovered").querySelectorAll("button").forEach(button => button.disabled = locked);
  for (const id of [...Object.values(settingsFields),"auto-restore","relay","update-check","ref-lat","ref-lon","save-settings","simulator","sim-gps","sim-uat","sim-weather"]) $(id).disabled = locked;
  if (!pending) $("simulator").checked = s.simulator;
  // Routing is immutable while the UDP worker is running.
  for (const input of $("bridge-panel").querySelectorAll("input,select")) {
    input.disabled = locked || input.dataset.unavailable === "true";
  }
  $("same").disabled = locked || noThisDevice();
  $("same").closest("label").hidden = noThisDevice();
  if (!locked && noThisDevice() && $("same").checked) { $("network").checked = true; updateDestination(); }
  $("check").disabled = locked;
  $("capture-start").disabled = pending || s.recording || s.job.state === "running";
  $("capture-stop").disabled = pending || !s.recording || s.job.state === "running";
  document.querySelectorAll("[data-action],#check-mode,#get-logs,#analyze,#compare-weather,#probe-ws,#capture-upload").forEach(button => button.disabled = pending || s.job.state === "running");
  // Exit stays available while a job runs: the adapter cancels the job.
  $("quit").disabled = pending;
  $("device-status").hidden = !s.device;
  if (s.device) $("device-status").textContent = JSON.stringify(s.device,null,2);
  const knownLogs = ["all",...s.deviceLogs];
  if (JSON.stringify([...$("receiver-logs").options].map(o => o.value)) !== JSON.stringify(knownLogs)) {
    $("receiver-logs").replaceChildren(...knownLogs.map(name => {const option = element("option",name === "all" ? "All logs" : name); option.value = name; return option;}));
  }
  if ($("bridge-log").parentElement.open && $("bridge-log").textContent !== s.logs.join("\n")) $("bridge-log").textContent = s.logs.join("\n");
  updateFiles(s.artifacts);
  const jobKey = JSON.stringify(s.job);
  if (jobKey !== jobRevision) {
    jobRevision = jobKey;
    const toolFailed = s.job.result?.exitCode !== undefined && s.job.result.exitCode !== 0;
    $("operation").classList.toggle("error",s.job.state === "failed" || toolFailed);
    $("operation").replaceChildren(element("span",s.job.state === "running" ? s.job.action+"…" : s.job.state === "failed" ? s.job.error : s.job.state === "done" ? s.job.action+(toolFailed ? ": check report" : " complete") : ""));
    if (s.job.state === "running") $("tool-result").textContent = "";
    discoveryJob(s.job, shownRoute !== routeBefore);
    if (s.job.result) {
      $("tool-result").textContent = s.job.result.text || JSON.stringify(s.job.result,null,2);
      if (s.job.result.warning) $("operation").append(element("p",s.job.result.warning));
      for (const key of ["backup","download","report"]) {
        const id = s.job.result[key];
        const file = s.artifacts.find(file => file.id === id && file.ready);
        if (file) { const link = element("a","Download "+file.name); link.href = "/api/files/"+id; link.download = file.name; $("operation").append(element("br"),link); }
      }
    }
  }
}
async function refreshStatus() {
  try {
    const response = await fetch("/api/status");
    if (!response.ok) throw new Error("Local GUI adapter unavailable.");
    renderLive(await response.json());
  } catch (error) { showError(error); }
}
async function poll() {
  if (live && !document.hidden) await refreshStatus();
  setTimeout(poll,document.hidden ? 5000 : 1000);
}
for (const button of document.querySelectorAll("[data-action]")) button.addEventListener("click",() => act(button.dataset.action));
$("save-settings").addEventListener("click",async () => {
  // The EFB port belongs to the route: show it on the Bridge tab and check the route again.
  try { if (await act("settings",settingsRequest())) { $("settings-result").textContent = "Saved"; updateDestination(); } } catch(error) { showError(error); }
});
$("auto-restore").addEventListener("change",async () => {
  try { if (await act("settings",settingsRequest())) $("settings-result").textContent = "Saved"; } catch(error) { showError(error); }
});
$("compatibility").addEventListener("change",() => act("compatibility",{enabled:$("compatibility").checked,backup:$("mode-backup").checked,verify:$("mode-verify").checked,seconds:Number($("verify-seconds").value)}));
$("check-mode").addEventListener("click",() => act("check",{seconds:Number($("check-seconds").value)}));
$("get-logs").addEventListener("click",() => act("download-logs",{name:$("receiver-logs").value,maxMb:Number($("log-limit").value)}));
$("capture-start").addEventListener("click",() => act("capture-start",{seconds:Number($("capture-seconds").value)}));
$("capture-stop").addEventListener("click",() => act("capture-stop"));
$("analyze").addEventListener("click",() => act("analyze",{capture:$("analysis-capture").value,port:Number($("analysis-port").value),samples:Number($("hex-samples").value)}));
$("compare-weather").addEventListener("click",() => act("weather",{source:$("weather-source").value,output:$("weather-output").value,inputPort:Number($("weather-input-port").value),outputPort:Number($("weather-output-port").value)}));
$("probe-ws").addEventListener("click",() => act("websocket",{path:$("ws-path").value,seconds:Number($("ws-seconds").value)}));
$("simulator").addEventListener("change",async () => {
  const result = await act("simulator",{enabled:$("simulator").checked,gps:$("sim-gps").checked,uat:$("sim-uat").checked,weather:$("sim-weather").checked});
  if (result) {
    fillSettings(status.settings);
    $("analysis-port").value = status.settings.listenPort;
    if (status.simulator) { $("same").checked = true; updateDestination(); }
    else { updateDestination(); restoreRoutes(status.routesRequest); }
  }
});
$("capture-upload").addEventListener("change",async () => {
  const file = $("capture-upload").files[0]; if (!file) return;
  const limit = status?.host === "android" ? 8 : 32;
  if (file.size > limit*1024*1024) { showError(new Error("Capture exceeds "+limit+" MiB.")); return; }
  try {
    const bytes = new Uint8Array(await file.arrayBuffer()); let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset,offset+8192));
    await act("upload",{name:file.name,data:btoa(binary)});
  } catch(error) { showError(error); }
  $("capture-upload").value = "";
});
$("quit").addEventListener("click",async () => {
  try {
    await api("quit"); live = false;
    document.querySelectorAll("button,input,select").forEach(control => control.disabled = true);
    $("receiver-heading").textContent = "Closed";
    $("adapter-label").textContent = "Sentry Bridge closed";
    $("warning").hidden = true;
    for (const id of ["gps","traffic","weather","age"]) $(id).textContent = "-";
  } catch(error) { showError(error); }
});
function restoreRoutes(request) {
  if (!request) return;
  $(request.topology === "same" ? "same" : "network").checked = true;
  $("address").value = request.topology === "same" ? "" : request.address || "";
  snapshotForm();  // before updateDestination, which records the route it shows from the snapshot
  updateDestination();
}
appearance(); sampleState();
$("check").disabled = true; $("start").disabled = true;
async function initialize() {
  try {
    const adapter = await fetch("/api/status");
    if (adapter.ok) {
      status = await adapter.json(); live = status.adapter === "local";
      if (live) {
        $("host").value = status.host;
        document.querySelector(".preview-options").hidden = true;
        document.querySelector(".stats").setAttribute("aria-label","Receiver status");
        document.querySelectorAll(".live-controls").forEach(section => section.hidden = false);
        fillSettings(status.settings);
        $("analysis-port").value = status.settings.listenPort;
        if (status.simulator && !status.routesRequest) $("same").checked = true;
      }
    }
    updateDestination(); restoreRoutes(status?.routesRequest);
    ready = true; $("check").disabled = false; $("start").disabled = false;
    if (live) { renderLive(status); poll(); }
    const initialTab = location.hash.slice(1); if (tabs.includes(initialTab)) selectTab(initialTab);
  } catch(error) { $("validation").hidden = false; $("validation").textContent = error.message; }
}
initialize();
