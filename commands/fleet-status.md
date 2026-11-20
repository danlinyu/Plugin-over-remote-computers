---
description: Check live connectivity, LAN vs Tailscale transport, and active project across all fleet computers
---

Call `fleet_list_machines(probe=true)` and `fleet_current_project()` (or run `fleet status`) and display a concise table of all computers in my fleet, whether each is LOCAL, on Home LAN, or on Tailscale, its latency, and the currently selected project.
