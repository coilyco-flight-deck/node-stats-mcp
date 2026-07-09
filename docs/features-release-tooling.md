# Features: cross-repo tooling and release

This repo uses `docs/features-release-tooling.md` as the durable citation for
the catalog trifecta footers in `README.md`, `AGENTS.md`, and
`docs/FEATURES.md`.

The release pinning and guard setup is intentionally split:

- The managed `agentic-os` hook block is pinned by release tag.
- The manual `issue-reference-guard` pin stays on a release tag, not a raw SHA.
- The trifecta footers cite this page instead of a live issue thread.
