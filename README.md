# Matter & Thread Network Self-Healing & Route Optimization Guide

This repository contains troubleshooting notes, scripts, and configurations for automatically diagnosing and remediating unresponsive Matter/Thread smart home devices in a self-hosted environment (e.g., Home Assistant, Docker, OTBR).

## 1. The Core Issue: IPv6 Routing Metric Conflicts

In multi-Border Router setups (e.g., combining an OpenThread Border Router (OTBR) USB dongle with Apple HomePods or Google Nest Hubs), Linux hosts frequently encounter IPv6 routing metric conflicts.

### The Conflict
- **Remote Border Routers**: Apple/Google border routers advertise the Thread network ULA prefix (e.g., `fdbc:271:669f::/64`) over the LAN via Router Advertisements (RAs). The host learns this route on its physical network interface (e.g., `enu1c2`) with a default RA metric (often `105`).
- **Local Border Router**: The local OTBR agent configures the local `wpan0` Thread interface and registers a route for the same prefix, but typically assigns it a higher metric (often `120`).
- **The Result**: Because a lower metric indicates higher priority (`105 < 120`), the Linux host routes Thread traffic destined for local devices out of the physical Ethernet interface and across the LAN, instead of directly using the local coordinator dongle on `wpan0`. If remote border routers restart or drop packets, all local Thread devices instantly go unresponsive.

### Additional Thread Failure Modes
1. **Sleepy End Devices (SEDs) & Orphaned Children**: Battery-powered Thread devices disable their radios to save energy and poll their Parent Router for messages. If the Parent Router reboots or exhausts its child table, children are orphaned and remain offline until forced to re-pair.
2. **Network Partitions**: The Thread mesh can partition if physical obstructions or interference prevent routers from communicating, splitting the network into independent islands.

---

## 2. The Solutions

We implemented a three-tier self-healing system to automatically optimize network routing and recover from device drops.

### Tier 1: Host-Level Route Metric Optimization
To ensure the local Thread interface (`wpan0`) is always preferred, we deploy a NetworkManager dispatcher script. This script dynamically replaces the Thread route with a preferred metric (`99`) whenever interfaces change state.

**File**: `/etc/NetworkManager/dispatcher.d/99-thread-route.sh`
```bash
#!/bin/bash
INTERFACE=$1
ACTION=$2

if [ "$INTERFACE" = "wpan0" ] || [ "$INTERFACE" = "enu1c2" ]; then
    if [ "$ACTION" = "up" ] || [ "$ACTION" = "route-change" ]; then
        # Ensure local wpan0 Thread prefix route is preferred (metric 99 < RA metric 105)
        ip -6 route replace fdbc:271:669f::/64 dev wpan0 metric 99 2>/dev/null
    fi
fi
```

Make it executable:
```bash
sudo chmod +x /etc/NetworkManager/dispatcher.d/99-thread-route.sh
```

---

### Tier 2: Home Assistant Self-Healing Watchdog
We configured a Home Assistant watchdog automation to detect offline Matter devices, re-apply route optimizations, and reload the integration configuration entry automatically.

**`configuration.yaml`**: Register route optimization command (no SSH required if Home Assistant runs in `privileged` mode with `network_mode: host`):
```yaml
shell_command:
  optimize_thread_routes: "ip -6 route replace fdbc:271:669f::/64 dev wpan0 metric 99"
```

**`automations.yaml`**: Watchdog Automation:
```yaml
- id: system_matter_thread_watchdog
  alias: System - Matter & Thread Watchdog (Self-Healing)
  description: Detects when Matter devices go offline, optimizes routing, and reloads the integration.
  trigger:
    - platform: template
      value_template: >-
        {{ integration_entities('matter') | select('is_state', 'unavailable') | list | length > 0 }}
      for:
        minutes: 5
  action:
    # 1. Re-apply correct local Thread route metrics on the host
    - service: shell_command.optimize_thread_routes
    
    # 2. Reload the Matter integration config entry (replace with your config entry ID)
    - service: homeassistant.reload_config_entry
      data:
        entry_id: YOUR_MATTER_CONFIG_ENTRY_ID
```

---

### Tier 3: Matter Server Health Watchdog (Docker Autoheal)
We improved the `healthcheck.py` script utilized by the `python-matter-server` container (working in tandem with `autoheal`) to reboot the server container if a global Thread partition or major device drop is detected.

**Health check rules**:
- Fail if **5 or more** nodes are stuck (pingable but unavailable).
- Fail if **8 or more** nodes are completely offline.
- Fail if **50% or more** of all configured nodes go offline (when total nodes >= 5).

**`healthcheck.py` snippet**:
```python
# Extract nodes availability
unavailable_nodes = [node for node in nodes if not node.available]
total_count = len(nodes)

is_unhealthy = False
reason = ""

if len(stuck_nodes) >= 5:
    is_unhealthy = True
    reason = f"{len(stuck_nodes)} nodes are stuck"
elif len(unavailable_nodes) >= 8:
    is_unhealthy = True
    reason = f"{len(unavailable_nodes)} nodes are completely offline"
elif total_count >= 5 and (len(unavailable_nodes) / total_count) >= 0.5:
    is_unhealthy = True
    reason = f"{len(unavailable_nodes)} out of {total_count} nodes are offline (>= 50%)"

if is_unhealthy:
    sys.exit(1)
```

---

## 3. Results
With this configuration:
1. Routing conflicts are permanently avoided.
2. Minor transient drops trigger automatic integration reloads within 5 minutes.
3. Major network partitions trigger a safe, automated container reboot.
