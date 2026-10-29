"use strict";
(async function boot() {
  try { cfg = await api("/config"); } catch (e) { toast("Cannot reach the AutoCut server", true); }
  const chk = $("#claudeAuto");
  chk.checked = !!cfg.settings.claude_auto_apply;
  chk.onchange = guard(async () => {
    await api("/settings", "PUT", { claude_auto_apply: chk.checked });
    toast(chk.checked ? "Claude's cuts now apply instantly" : "Claude's cuts now wait for your approval");
  });
  route();
})();
