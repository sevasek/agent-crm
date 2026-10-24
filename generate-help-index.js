#!/usr/bin/env node
// Doc-build-time only, like eleventy.config.js and check-docs.js. Keeps
// WELCOME.md's table of contents in sync with its own `##` sections so the
// index can't go stale when a section is added, renamed, or removed — run
// on every `docs:build`, not as a one-off.
const fs = require("fs");
const path = require("path");

const WELCOME = path.join(__dirname, "docs", "WELCOME.md");
const START = "<!-- GENERATED:HELP-INDEX:START -->";
const END = "<!-- GENERATED:HELP-INDEX:END -->";

// Same slugging GitHub (and markdown-it, which Eleventy uses here) give
// heading anchors: lowercase, drop anything but letters/digits/space/hyphen,
// spaces to hyphens.
function slugify(heading) {
  return heading
    .toLowerCase()
    .replace(/[^\w\s-]/g, "")
    .trim()
    .replace(/\s+/g, "-");
}

function headingsOf(markdown) {
  return [...markdown.matchAll(/^## (.+)$/gm)].map((m) => m[1].trim());
}

function buildIndex(markdown) {
  const headings = headingsOf(markdown);
  if (headings.length === 0) throw new Error("no ## sections found in WELCOME.md");
  return headings.map((h) => `- [${h}](#${slugify(h)})`).join("\n");
}

function run() {
  const markdown = fs.readFileSync(WELCOME, "utf8");
  const startIdx = markdown.indexOf(START);
  const endIdx = markdown.indexOf(END);
  if (startIdx === -1 || endIdx === -1 || endIdx < startIdx) {
    throw new Error(`WELCOME.md is missing the ${START} / ${END} markers`);
  }
  const index = buildIndex(markdown);
  const updated =
    markdown.slice(0, startIdx + START.length) + "\n" + index + "\n" + markdown.slice(endIdx);
  fs.writeFileSync(WELCOME, updated);
  return { count: headingsOf(markdown).length, index };
}

if (require.main === module) {
  const { count } = run();
  console.log(`docs/WELCOME.md: indexed ${count} sections`);

  // ponytail: the one check this parser needs — a known fixture round-trips.
  const got = buildIndex("## Apps\n\n## Lost reasons\n\n## Want more detail?\n");
  const want = [
    "- [Apps](#apps)",
    "- [Lost reasons](#lost-reasons)",
    "- [Want more detail?](#want-more-detail)",
  ].join("\n");
  if (got !== want) {
    console.error("FAIL: slugify/index self-check\n got:\n" + got + "\n want:\n" + want);
    process.exit(1);
  }
}

module.exports = { slugify, buildIndex };
