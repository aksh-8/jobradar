(function initializeContentScript() {
  "use strict";

  if (globalThis.__jobRadarContentLoaded) return;
  globalThis.__jobRadarContentLoaded = true;

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "JOBRADAR_EXTRACT") return false;
    try {
      sendResponse({ ok: true, data: JobRadarParser.extract(document, location.href) });
    } catch (error) {
      sendResponse({ ok: false, error: error.message || "Job extraction failed." });
    }
    return false;
  });
})();
