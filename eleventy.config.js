// Doc-build-time only — turns docs/*.md into a static manual, reusing the
// app's own CSS so it looks like the app. Not loaded by the app, not run in
// the Docker image (node_modules/output are .dockerignore'd).

const TITLES = require("./docs/_data/titles.js");

module.exports = function (eleventyConfig) {
  // Same files the app itself serves from /static — not copies of a
  // separately maintained stylesheet.
  eleventyConfig.addPassthroughCopy({ "app/static/vendor/pico.min.css": "assets/pico.min.css" });
  eleventyConfig.addPassthroughCopy({ "app/static/style.css": "assets/style.css" });
  eleventyConfig.addPassthroughCopy({ "docs/images": "assets/images" });

  // Nav list: every docs/*.md page except the manual's own home page.
  eleventyConfig.addCollection("manual", (api) =>
    api.getFilteredByGlob("docs/*.md")
      // index.md's fileSlug is "" (Eleventy convention for index files), not "index".
      .filter((p) => p.fileSlug !== "")
      .map((p) => ({ url: p.url, title: TITLES[p.fileSlug] || p.fileSlug }))
      .sort((a, b) => a.title.localeCompare(b.title))
  );

  return {
    // The docs are full of code samples with `{{ }}` / `${...}` — don't run
    // a template engine over markdown *content*, only over the .njk layout.
    markdownTemplateEngine: false,
    dir: { input: "docs", output: "_site", includes: "_includes" },
  };
};
