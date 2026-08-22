(function initializeContentScript() {
  "use strict";

  if (globalThis.__jobRadarContentLoaded) return;
  globalThis.__jobRadarContentLoaded = true;

  async function extractWhenReady(timeoutMs = 2500) {
    try {
      return JobRadarParser.extract(document, location.href);
    } catch (initialError) {
      await new Promise((resolve) => {
        const observer = new MutationObserver(() => {
          try {
            JobRadarParser.extract(document, location.href);
            observer.disconnect();
            resolve();
          } catch (_error) {
            // Keep waiting for a dynamic Workday or client-rendered page.
          }
        });
        observer.observe(document.documentElement, { childList: true, subtree: true });
        setTimeout(() => {
          observer.disconnect();
          resolve();
        }, timeoutMs);
      });
      try {
        return JobRadarParser.extract(document, location.href);
      } catch (_error) {
        throw initialError;
      }
    }
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "JOBRADAR_EXTRACT") return false;
    extractWhenReady()
      .then((data) => sendResponse({ ok: true, data }))
      .catch((error) => sendResponse({
        ok: false,
        error: error.message || "Job extraction failed.",
      }));
    return true;
  });
})();
