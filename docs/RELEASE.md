# Release

## Pre-release Checklist

1. Validate scripts:

```bash
npm run validate
```

2. Preview npm package contents:

```bash
npm pack --dry-run
```

3. Smoke-install locally:

```bash
bash scripts/install-codereview-skill.sh
```

4. Restart Claude/Codex.
5. Verify `/codereview` resolves.

## Versioning

Use semantic versioning:

- `v0.1.0`: initial pre-release
- `v0.1.1`: patch fix
- `v0.2.0`: new backward-compatible feature
- `v1.0.0`: first stable release (after battle-testing)

## Release Flow

1. Move "Unreleased" items in `CHANGELOG.md` under `## [x.y.z] - YYYY-MM-DD`.
2. Bump `version` in `package.json` to `x.y.z`.
3. Commit and push:

```bash
git add CHANGELOG.md package.json
git commit -m "release: vx.y.z"
git push origin main
```

4. Tag and push (triggers the GitHub Action):

```bash
git tag vx.y.z
git push origin vx.y.z
```

5. The GitHub Action automatically:
   - Validates scripts (`npm run validate`)
   - Publishes to GitHub Packages
   - Creates a GitHub Release with notes extracted from `CHANGELOG.md`

## Manual Fallback

If the GitHub Action fails or you need to publish manually:

```bash
npm publish
```

`prepublishOnly` runs `npm run validate` automatically before publishing.

## Post-release

1. Verify the GitHub Release appears with correct changelog notes.
2. Verify the package is listed in the repo's Packages tab.
3. Mention restart requirement for Claude/Codex in release notes.
