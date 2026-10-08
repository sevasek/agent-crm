#!/usr/bin/env node
// Doc-build-time only, like eleventy.config.js. Smoke-checks the manual
// build: run `npm run docs:test`. Not part of the app, not in the image.
const fs = require("fs");
const path = require("path");

const SITE = path.join(__dirname, "_site");
const assert = (cond, msg) => {
  if (!cond) {
    console.error("FAIL:", msg);
    process.exitCode = 1;
  } else {
    console.log("ok:", msg);
  }
};

const read = (p) => fs.readFileSync(path.join(SITE, p), "utf8");

// Lowercased permalinks, not the SCREAMING_CASE source filenames.
for (const slug of ["mcp", "scope", "data_model", "deploy", "release", "client_deploy", "cutover-from-sevasek-crm"]) {
  assert(fs.existsSync(path.join(SITE, slug, "index.html")), `${slug}/index.html exists`);
}
assert(fs.existsSync(path.join(SITE, "index.html")), "site root index.html exists (from docs/index.md)");
assert(!fs.existsSync(path.join(SITE, "UI_UPGRADE_PLAN")), "UI_UPGRADE_PLAN.md was not built (internal, not user-facing)");
assert(!fs.existsSync(path.join(SITE, "adr")), "docs/adr/ was not built (internal design records)");

// The manual home page's own nav must not list itself, and must list the rest.
const home = read("index.html");
assert((home.match(/class="topnav"/g) || []).length === 1, "exactly one nav block on the home page");
assert(!/href="\/">Manual<\/a>\s*<a href="\/">/.test(home), "no duplicate/empty root nav entry");
for (const label of ["Scope", "Data model", "MCP: the agent interface", "Deploy", "Cutting a release", "New client instance", "Cutover from sevasek CRM"]) {
  assert(home.includes(`>${label}<`), `nav includes "${label}"`);
}

// Same CSS the app itself serves, not a copy with its own drift.
assert(fs.existsSync(path.join(SITE, "assets", "pico.min.css")), "pico.min.css copied through");
assert(fs.existsSync(path.join(SITE, "assets", "style.css")), "app style.css copied through");

const mcp = read(path.join("mcp", "index.html"));
assert(mcp.includes("<title>MCP: the agent interface"), "MCP page has its mapped title");
assert(mcp.includes("<table>"), "MCP page's tool table rendered as a real <table> (styled by style.css)");

if (process.exitCode) {
  console.error("\ndocs build check failed");
  process.exit(1);
}
console.log("\ndocs build check passed");
