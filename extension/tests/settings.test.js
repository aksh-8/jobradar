"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { normalizeApiBaseUrl, optionalOrigin } = require("../settings.js");

test("accepts and normalizes loopback API origins", () => {
  assert.equal(normalizeApiBaseUrl("http://127.0.0.1:8000/"), "http://127.0.0.1:8000");
  assert.equal(optionalOrigin("http://localhost:8000"), null);
});

test("accepts a private Tailscale HTTPS origin and requests only that host", () => {
  const api = normalizeApiBaseUrl("https://akash-pc.example.ts.net/");
  assert.equal(api, "https://akash-pc.example.ts.net");
  assert.equal(optionalOrigin(api), "https://akash-pc.example.ts.net/*");
});

test("rejects public, insecure, and dashboard-path API URLs", () => {
  assert.throws(() => normalizeApiBaseUrl("https://example.com"), /loopback HTTP/);
  assert.throws(() => normalizeApiBaseUrl("http://host.example.ts.net"), /loopback HTTP/);
  assert.throws(
    () => normalizeApiBaseUrl("https://host.example.ts.net/dashboard/"),
    /without \/dashboard/,
  );
});
