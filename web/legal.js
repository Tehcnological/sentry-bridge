"use strict";
// Read the shared preference without changing it or starting the bridge adapter.
// Same key and default as preferences() in app.js; keep them in step.
let theme = "dark";
try {
  const saved = JSON.parse(localStorage.getItem("sentrybridge-preview-appearance")) || {};
  if (["dark", "light", "system"].includes(saved.theme)) theme = saved.theme;
} catch { /* Storage is optional. */ }
const system = matchMedia("(prefers-color-scheme: dark)");
function appearance() { document.body.classList.toggle("dark", theme === "dark" || theme === "system" && system.matches); }
appearance();
system.addEventListener("change", appearance);
