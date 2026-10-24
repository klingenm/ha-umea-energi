---
name: mise
description: "Manage tool versions, environment variables and project tasks with mise. Use when installing, adding, pinning, upgrading or removing a dev tool (python, uv, ruff, etc.), when running project tasks (format, lint, typecheck, test, check, build), when a command is missing or the wrong version, or when editing mise.toml."
---

# mise

mise is the single source of truth for tool versions and project tasks in this repo.
Configuration lives in `mise.toml` at the repository root.

## Rules

- Never install tools with `brew`, `pipx`, `pip install --user`, `uv tool install`, or curl scripts.
  Add them to `mise.toml` with `mise use` instead.
- Python *libraries* (runtime and dev dependencies) belong in `pyproject.toml` via `uv add` /
  `uv add --dev`, not in mise. Standalone CLIs (ruff, uv itself, gh, …) belong in mise.
- uv is configured (via `[env]` in `mise.toml`) to use the mise-managed Python and never
  download its own. Don't override `UV_PYTHON*` variables or add a `.python-version` file.
- Prefer `mise run <task>` over retyping the underlying commands, so local runs match CI.
- Use `mise.local.toml` (gitignored) for personal overrides; don't put them in `mise.toml`.

## Running tasks

```sh
mise tasks                # list tasks with descriptions
mise run format           # ruff format + ruff check --fix
mise run lint             # ruff format --check + ruff check
mise run typecheck        # ty, via uv check
mise run test             # pytest
mise run test -- -k name  # pass extra args through to the task
mise run check            # lint + typecheck + test (run before committing)
mise run sync             # uv sync --all-groups
mise run hass             # local Home Assistant on http://localhost:8123 (config in ./config)
```

When adding a task, define it under `[tasks.<name>]` in `mise.toml` with a `description`
and a `run` string or list; use `depends` to compose tasks. Flags for mise itself (e.g. `--force`)
go *before* the task name; anything after it is passed to the task's command.

## Managing tools

```sh
mise ls --current            # tools and versions active in this directory
mise install                 # install everything pinned in mise.toml
mise use <tool>@<version>    # add or change a pin in mise.toml (e.g. mise use ruff@0.17)
mise unuse <tool>            # remove a tool from mise.toml
mise ls-remote <tool>        # list available versions
mise latest <tool>           # latest available version
mise outdated                # pinned tools with newer versions
mise upgrade --bump          # upgrade and rewrite pins in mise.toml
mise exec -- <cmd>           # run a command with mise's tools on PATH (when not activated)
```

Pins in this repo use a major.minor prefix (e.g. `python = "3.14"`), so patch releases are
picked up by `mise install`/`mise upgrade` without editing `mise.toml`.

## Troubleshooting

- `mise ERROR ... not trusted`: run `mise trust` in the repo root (only after reviewing `mise.toml`).
- Command not found or wrong version: check `mise ls --current`, then `mise install`. If the
  shell isn't activated, prefix the command with `mise exec --`.
- `.venv` built against the wrong interpreter: `rm -rf .venv && mise run sync`, then confirm
  `home` in `.venv/pyvenv.cfg` points into `~/.local/share/mise/installs/python/`.
- `mise doctor` reports activation and config problems.
