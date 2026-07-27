# Agent instructions

Workspace conventions load globally via `~/.claude/CLAUDE.md`. This file covers only what is specific to this repo.

## Scope

A single tiny Python service: a FastMCP server (`src/node_stats_mcp/server.py`) wrapping `psutil` reads as MCP tools, served over streamable-HTTP.

## Project shape

No frontend, no database, no in-game mods. `src/node_stats_mcp/` holds the server and its entrypoint; `tests/` covers the tool logic and the file-read security envelope. One image, one process.

## Repo boundaries

The deploy surface lives in [coilyco-bridge/deploy](https://forgejo.coilysiren.me/coilyco-bridge/deploy) `services/node-stats-mcp`, not here (source -> deploy layer invariant). This repo builds and publishes the image; the deploy repo rolls it.

## Commands

Route every command through Ward, never bare `uv` / `pytest`. Verbs are declared in [`.ward/ward.yaml`](.ward/ward.yaml). Run them as `ward exec <verb>`.

## Validation

`ward lint` (ruff + ruff-format + mypy) and `ward test` (pytest). `ward precommit` runs the full pre-commit suite, including the agentic-os catalog hooks. Validate before pushing.

## Safety

- **Every tool is read-only.** Never add a tool that writes, restarts, or mutates the host. Mutation belongs to operator surfaces, not this MCP.
- **File reads stay prefix-allowlisted.** `stat_path` / `read_text_head` resolve the real path and refuse anything outside `NODE_STATS_READABLE_ROOTS`. Keep that check on any new file-touching tool - never accept a raw path and open it.
- **Node view depends on the deployment.** Process and network tools reflect the node only when the pod runs hostPID + hostNetwork; say so in the tool docstring rather than assuming it.

## Cross-repo contracts

The image is published privately to
`forgejo.coilysiren.me/coilyco-flight-deck/node-stats-mcp:<full-source-sha>` by
[`.forgejo/workflows/build-publish.yml`](.forgejo/workflows/build-publish.yml)
on every push to main. The trusted publisher uses a package-write credential.
The deploy repo receives only the package-read credential and rolls the same
immutable reference. Keep the dependency surface tiny (psutil + mcp). A new
dependency needs a reason.

## Release

Push to main. CI tests, publishes one source-SHA image to Forgejo OCI, and
proves the remote manifest exists. There is no version bump or moving tag.
Deferred cleanup gets a Forgejo issue, never a silent skip.

## Agent rules

Name the actor in action sentences.

## See also

- [README.md](README.md) - human-facing intro.
- [docs/FEATURES.md](docs/FEATURES.md) - inventory of what ships today.
- [.ward/ward.yaml](.ward/ward.yaml) - allowlisted commands + catalog block.

Cross-reference convention from [features-release-tooling.md](docs/features-release-tooling.md).
