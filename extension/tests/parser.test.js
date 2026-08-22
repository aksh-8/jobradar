"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { salaryFacts, sponsorshipStatus } = require("../parser.js");

test("detects explicit no-sponsorship language", () => {
  assert.equal(sponsorshipStatus("Candidates must work without sponsorship"), "NO");
});

test("detects affirmative sponsorship language", () => {
  assert.equal(sponsorshipStatus("Visa sponsorship is available for this role"), "YES");
});

test("keeps unspecified sponsorship for manual review", () => {
  assert.equal(sponsorshipStatus("Build cloud software for customers"), "UNKNOWN");
});

test("extracts annual structured salary ranges", () => {
  assert.deepEqual(
    salaryFacts({ value: { minValue: 150000, maxValue: 190000, unitText: "YEAR" } }),
    { base_salary_min_usd: 150000, base_salary_max_usd: 190000 },
  );
});

test("does not mislabel hourly compensation as annual salary", () => {
  assert.deepEqual(
    salaryFacts({ value: { minValue: 70, maxValue: 90, unitText: "HOUR" } }),
    {},
  );
});
