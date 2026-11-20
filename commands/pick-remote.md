---
description: Browse your Home LAN / Tailscale computers (Mac Mini, PC, Sparks) and pick or create a project folder
argument-hint: [machine] [optional-path]
---

Help me pick or create a project folder across my Home LAN / Tailscale computers (`$ARGUMENTS`).

1. Call `fleet_list_machines` (or run `fleet status --json`) to check my online machines (`mac-mini`, `pc`, `spark-1`, `spark-2`, `spark-3`, and any Tailscale peers).
2. If a machine was provided in `$ARGUMENTS`, call `fleet_browse_folders` on that machine (and path if given) and show me the available folders. Otherwise present the online machines clearly so I can pick one.
3. Once I choose an existing folder or give a new folder name, call `fleet_select_project` or `fleet_create_project` and use `remote-fleet` tools for all file edits and remote commands on that project.
