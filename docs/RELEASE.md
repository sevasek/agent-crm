# Cutting a release

The first published image should be `v0.1.0`. Do this on a clean `main`
after reviewing `[Unreleased]` in [`CHANGELOG.md`](../CHANGELOG.md).
Do not retag; a tag push is what starts the publish workflow.

## 1. Move the changelog

Cut the current Added/Changed/Fixed bullets out of `## [Unreleased]`
and put them under a dated version heading:

```markdown
## [Unreleased]

## [0.1.0] - YYYY-MM-DD

### Added
…
```

Leave the `## [Unreleased]` heading in place (empty is fine) so later
PRs have somewhere to land. At the bottom, point the compare links at
the new tag:

```markdown
[Unreleased]: https://github.com/sevasek/agent-crm/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sevasek/agent-crm/releases/tag/v0.1.0
```

Commit that changelog edit on `main` before tagging. Later versions
do the same: move Unreleased under `v0.2.0` (and so on) and add a
compare link from the previous tag.

## 2. Tag

```bash
git checkout main
git pull origin main
git tag v0.1.0
git push origin v0.1.0
```

Prerelease tags use a hyphen (`v0.1.0-rc.1`). Those still build an
image, but they do not move `:latest`.

## 3. What the workflow publishes

[`.github/workflows/release.yml`](../.github/workflows/release.yml)
runs on `v*` tags. It builds `linux/amd64` and `linux/arm64` and
pushes to GHCR:

- `ghcr.io/sevasek/agent-crm:v0.1.0` (the tag name)
- `ghcr.io/sevasek/agent-crm:latest` — only when the tag has no `-`

The default compose path remains `build: .`. To run the published
image instead of building locally, see
[Production](../README.md#production).
