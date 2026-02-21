# Workflows

Project-specific workflow templates. Each file defines a repeatable procedure for a specific concern (deploy, architecture checks, custom gates, etc.).

## How It Works

1. CLAUDE.md references this directory as the on-demand workflow slot
2. The agent knows to look here when a task matches a workflow concern
3. Projects fill in the templates they need, ignore the rest
4. Workflows are loaded on-demand, not at bootstrap

## Provided Templates

| File | Purpose | Fill when |
|------|---------|-----------|
| `deploy.md` | Deploy ceremony (local test -> approval -> deploy) | Project has a deploy target |

## Adding Workflows

Create a new `.md` file in this directory. Use a clear, short name: `deploy.md`, `architecture-checks.md`, `release-process.md`.

Each workflow should contain:
- **When to use** — trigger conditions
- **Steps** — numbered, in order
- **Forbidden** — what must never happen
- **Verification** — how to confirm success
