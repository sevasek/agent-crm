// Applies to every file in docs/ without editing the files themselves —
// these .md files are also read raw on GitHub, so no front matter in them.
module.exports = {
  layout: "base.njk",
  eleventyComputed: {
    title: (data) => data.titles[data.page.fileSlug] || data.page.fileSlug,
    // Filenames are SCREAMING_CASE for grep-ability in the repo; URLs
    // shouldn't be. index.md (fileSlug "") stays the site root.
    permalink: (data) => {
      const slug = data.page.fileSlug;
      return slug === "" ? "index.html" : `${slug.toLowerCase()}/index.html`;
    },
  },
};
