# Claude Code plugin schema — the subset we use

Verified against the official Claude Code docs. **Build to exactly this.** If you think you
need a field that isn't here, you don't.

## `.claude-plugin/plugin.json`

```json
{
  "name": "crm-hygiene",
  "version": "1.0.0",
  "description": "One-sentence description shown in the marketplace and /plugin UI.",
  "author": { "name": "LeanScale", "email": "anthony@leanscale.team", "url": "https://leanscale.team" },
  "homepage": "https://leanscale.team",
  "license": "LicenseRef-LeanScale-Customer",
  "keywords": ["revops", "salesforce", "hubspot", "crm", "data-quality"]
}
```

- `name` — **kebab-case, no spaces, no path separators.** Required.
- Everything else above is optional but we ship all of it.

> **Do not add `displayName`.** The published docs list it, but the shipping CLI rejects it —
> `root: Unrecognized key: "displayName"` — and the whole manifest fails validation. Verified
> against `claude plugin validate` on CLI 2.1.128. Customers will be on a range of versions, so
> we ship the manifest that validates on both: name, version, description, author, homepage,
> license, keywords. Nothing else.
- Do **not** add `commands`, `skills`, `agents` path fields — the default locations are
  auto-discovered and overriding them only creates ways to be wrong.
- Only `plugin.json` lives inside `.claude-plugin/`. Every other directory (`skills/`,
  `scripts/`) sits at the **plugin root**.

## Skills — `skills/<name>/SKILL.md`

We use **skills only**. No `commands/` directory: the docs recommend skills for new plugins,
and flat command files can't carry supporting scripts.

Every plugin ships exactly two:

```
skills/run/SKILL.md      -> /<plugin-name>:run
skills/setup/SKILL.md    -> /<plugin-name>:setup
```

Frontmatter we use, and nothing else:

```yaml
---
name: run
description: >-
  What it does AND when to trigger it. This is how Claude decides to auto-invoke,
  so write real trigger phrases into it.
argument-hint: "[--window 90d] [--segment Enterprise]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, ToolSearch
---
```

- `name` is optional (defaults to the directory name) — we set it explicitly anyway.
- **Plugin skills are ALWAYS namespaced as `/<plugin-name>:<skill-name>`.** There is no way
  to get a bare `/crm-hygiene`. This is why every plugin uses the same two skill names:
  a customer with four of these installed types `/<thing>:run` every time.
- `allowed-tools` is a permission grant, not a restriction — list only what's needed.
- Do not set `model`, `effort`, `context: fork`, or `background`. Defaults are correct here.

## `.claude-plugin/marketplace.json` (repo root)

```json
{
  "name": "leanscale-gtm",
  "owner": { "name": "LeanScale", "email": "anthony@leanscale.team", "url": "https://leanscale.team" },
  "description": "GTM and RevOps agents for Salesforce and HubSpot.",
  "metadata": { "pluginRoot": "./plugins" },
  "plugins": [
    { "name": "crm-hygiene", "source": "./crm-hygiene", "description": "...", "version": "1.0.0", "category": "data-quality" }
  ]
}
```

- `metadata.pluginRoot` is prepended to every relative `source`, so entries are just
  `"./<plugin-name>"`.
- `name` and `owner.name` are required; `owner` must be an object, not a string.

## Install paths (all verified supported)

```bash
/plugin marketplace add leanscale-io/gtm-agents        # GitHub shorthand
/plugin marketplace add https://git.example.com/x.git  # any git URL
/plugin marketplace add ./leanscale-gtm-agents         # local directory
/plugin install crm-hygiene@leanscale-gtm
```

A bare HTTPS URL to a `marketplace.json` **is** accepted, but relative `source` paths won't
resolve from it — so the zip download on the catalog site is documented as a **local
directory** install (unzip, then `/plugin marketplace add <path>`), which always works.

## Path variables

| Variable | Resolves to | Use for |
|---|---|---|
| `${CLAUDE_PLUGIN_ROOT}` | the plugin's install dir — **changes on every update** | bundled scripts, fixtures, config examples |
| `${CLAUDE_PLUGIN_DATA}` | `~/.claude/plugins/data/<plugin-id>/`, survives updates | per-plugin state, if you ever need it |
| `${CLAUDE_PROJECT_DIR}` | project root | run output |

Valid inside SKILL.md body text, hook commands, and MCP/LSP configs.

**We do not use `${CLAUDE_PLUGIN_DATA}` for config** — it is per-plugin, and our profile is
shared across all nine. Config lives in `~/.leanscale-gtm/` (see SPEC §2).

## Hard constraints that shape the build

1. **Marketplace-installed plugins are read-only and cached** in `~/.claude/plugins/cache`.
   Never write into `${CLAUDE_PLUGIN_ROOT}`.
2. **A plugin cannot reference files outside its own directory.** No `../core/lib`. The
   shared library is **copied** into every plugin at `scripts/lib/`. Build it once in
   `core/lib/`, then vendor it.
3. Scripts must be invoked as `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/analyze.py" ...` —
   always quoted, since the cache path contains a version segment.

## Validation

```bash
claude plugin validate ./plugins/<name>
```

Checks manifest JSON + schema, SKILL.md frontmatter, and that declared component paths exist.
Every plugin must pass before it ships.

> **There is no `--strict` flag** on the shipping CLI — it errors with
> `unknown option '--strict'`. The docs mention it; the binary doesn't have it. Plain
> `validate` is the gate.
