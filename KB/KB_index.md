# KB Index (Context Router)

> **LOAD THIS FILE ON EVERY BOOTSTRAP.** This is the routing table for all KB content.
> Obey the `Load` column. Do NOT load `on-demand` files unless current task matches the tags.

| # | File | Description | Tags | Load |
|---|------|-------------|------|------|
| B | `KB/blueprints/BLUEPRINT_INDEX.md` | Version pointer + history table | arch | always |
| B+ | `KB/blueprints/v0.1_brain_integration_plan.md` | Brain integration plan (9 phases) | arch, scaffold | always (latest only) |
| 01 | `KB/KB_01_architecture.md` | Architecture + Decision Journal | arch, decisions | always: overview. on-demand: DJ entries by tag |
| 02 | `KB/KB_02_intuitive_ai_reference.md` | Source reference: all 13 intuitive-AI modules | brain, memory, arch | on-demand: when implementing any brain module |

<!--
LOADING RULES (for Opus 4.6):

1. Bootstrap: load all "always" files. For "always (latest only)", check BLUEPRINT_INDEX.md for current version pointer.
2. During work: if current task touches a tag domain, grep DJ headers for matching [tag] and load those entries.
3. NEVER load all blueprint versions at once. Load historical versions ONLY to trace a specific decision evolution.
4. When adding new KB pages, assign tags and set Load column. Default = on-demand.

ADDING PAGES:
| XX | `KB/KB_XX_<topic>.md` | Description | tags | on-demand |
-->
