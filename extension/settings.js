"use strict";

(function exposeSettings(root) {
  function normalizeApiBaseUrl(value) {
    let parsed;
    try {
      parsed = new URL(String(value || "").trim());
    } catch (_error) {
      throw new Error("Enter a valid JobRadar API URL.");
    }
    const hostname = parsed.hostname.toLowerCase();
    const loopback = parsed.protocol === "http:"
      && (hostname === "127.0.0.1" || hostname === "localhost");
    const privateTailscale = parsed.protocol === "https:"
      && hostname.endsWith(".ts.net");
    if (!loopback && !privateTailscale) {
      throw new Error("Use loopback HTTP or your private Tailscale HTTPS URL.");
    }
    if (parsed.username || parsed.password || parsed.search || parsed.hash) {
      throw new Error("The API URL cannot contain credentials, a query, or a fragment.");
    }
    if (parsed.pathname !== "/" && parsed.pathname !== "") {
      throw new Error("Use the API origin without /dashboard or another path.");
    }
    return parsed.origin;
  }

  function optionalOrigin(apiBaseUrl) {
    const parsed = new URL(normalizeApiBaseUrl(apiBaseUrl));
    return parsed.hostname.endsWith(".ts.net") ? `${parsed.origin}/*` : null;
  }

  const exported = { normalizeApiBaseUrl, optionalOrigin };
  root.JobRadarSettings = exported;
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
})(typeof globalThis === "undefined" ? this : globalThis);
