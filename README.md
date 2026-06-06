# Matter & Thread Network Self-Healing & Route Optimization Guide

This repository is a self-contained guide, including scripts and configurations, for diagnosing and automatically remediating unresponsive Matter-over-Thread smart home devices in Linux-based dockerized environments (e.g., Home Assistant with `python-matter-server` and OpenThread Border Router).

---

## 1. Network-Level Issues & Solutions

### A. IPv6 Routing Metric Conflict
#### The Background
In smart home environments that combine a local OpenThread Border Router (OTBR) USB dongle with remote border routers (like Apple HomePods or Google Nest Hubs), the Linux host frequently encounters routing conflicts:
- **Remote Border Routers** advertise the Thread network ULA prefix (e.g., `fdbc:271:669f::/64`) over the LAN using IPv6 Router Advertisements (RAs). The Linux host automatically adds routes for this interface via the physical Ethernet/Wi-Fi adapter (e.g., `enu1c2`) with a low metric (typically `105`).
- **The Local OTBR** registers a local network interface (`wpan0`) and configures a route for the same prefix, but defaults to a higher metric (typically `120`).
- **The Failure**: Since a lower metric has higher priority (`105 < 120`), the Linux host routes Thread traffic across the LAN via third-party border routers instead of directly using the local USB coordinator dongle on `wpan0`. If remote border routers restart or drop packets, your local Thread devices instantly go unresponsive.

#### The Solution
We use a **NetworkManager dispatcher script** that runs automatically on network interface changes to dynamically replace the Thread route with a preferred metric (`99`), forcing the local Thread radio interface to take priority.

---

### B. Hardware Offloading & Multicast Snooping
#### The Background
- **Hardware Offloading**: Linux hosts often use TCP Segmentation Offload (TSO), Generic Segmentation Offload (GSO), and Generic Receive Offload (GRO) to coalesce small packets to save CPU. However, these features can corrupt or drop small IPv6 multicast packets essential for Thread discovery (mDNS and MLE).
- **Multicast Snooping**: Docker bridge networks (`br-*` interfaces) enable IGMP/MLD multicast snooping by default. Without an active IGMP/MLD query agent on the virtual bridge, the bridge's multicast forwarding table times out, causing containers to stop receiving critical mDNS packets.

#### The Solution
We disable network offloads on the physical link and disable multicast snooping on all Docker bridge interfaces. 

---

### The Combined Network Fix: NetworkManager Dispatcher Script

Create the following script on your host. It handles routing priority, offloading, and multicast snooping in one event-driven hook.

**File Location**: `/etc/NetworkManager/dispatcher.d/99-thread-route.sh`
```bash
#!/bin/bash
INTERFACE=$1
ACTION=$2

# Replace 'enu1c2' with your physical ethernet interface name
PHYS_IF="enu1c2"
THREAD_PREFIX="fdbc:271:669f::/64"

if [ "$INTERFACE" = "wpan0" ] || [ "$INTERFACE" = "$PHYS_IF" ]; then
    if [ "$ACTION" = "up" ] || [ "$ACTION" = "route-change" ]; then
        # Force local wpan0 interface to take priority for Thread traffic
        ip -6 route replace $THREAD_PREFIX dev wpan0 metric 99 2>/dev/null
    fi
    
    if [ "$INTERFACE" = "$PHYS_IF" ] && [ "$ACTION" = "up" ]; then
        # Disable hardware offloads to prevent multicast packet drop/corruption
        ethtool -K $PHYS_IF tso off gso off gro off 2>/dev/null
        
        # Disable multicast snooping on all Docker bridges to allow clean mDNS flow
        for bridge in /sys/class/net/br-*/bridge/multicast_snooping; do
            [ -f "$bridge" ] && echo 0 > "$bridge" 2>/dev/null
        done
    fi
fi
```

Make the script executable:
```bash
sudo chmod +x /etc/NetworkManager/dispatcher.d/99-thread-route.sh
```

---

## 2. Application-Level Watchdog (Home Assistant)

### The Background
Even with optimized routes, Home Assistant's Matter integration can occasionally lose connection to the WebSocket server or get into a stalled state where devices appear offline in the UI. Reloading the integration config entry clears the cache and establishes a clean connection.

### The Solution
We create a Home Assistant shell command and a watchdog automation.

1. **Register the Shell Commands** in your `configuration.yaml` to ensure the route is corrected on demand and offline devices are logged:
```yaml
shell_command:
  optimize_thread_routes: "ip -6 route replace fdbc:271:669f::/64 dev wpan0 metric 99"
  log_offline_devices: "echo \"$(date): {{ devices }}\" >> /config/offline_devices.log"
```
*(Note: If Home Assistant is running in docker with `privileged: true` and `network_mode: host`, it has direct privileges to modify the host routing table. No SSH credentials are required).*

2. **Add the Watchdog & Nightly Healing Automations** to `automations.yaml`:
```yaml
# 1. Watchdog: Reactively heals the network if any devices go offline for 5 minutes
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
    # Log the exact offline devices before healing
    - service: shell_command.log_offline_devices
      data:
        devices: >-
          {{ integration_entities('matter') | select('is_state', 'unavailable') | map(attribute='entity_id') | list | join(', ') }}
      continue_on_error: true

    # Re-apply correct local Thread route metrics on the host
    - service: shell_command.optimize_thread_routes
      continue_on_error: true
    
    # Reload the Matter integration config entry (replace with your config entry ID)
    - service: homeassistant.reload_config_entry
      data:
        entry_id: YOUR_MATTER_CONFIG_ENTRY_ID
      continue_on_error: true

    # Wait 45 seconds to let the integration re-establish connections
    - delay: '00:00:45'
    
    # Retry reload once if devices are still unavailable (resiliency check)
    - if:
        - condition: template
          value_template: >-
            {{ integration_entities('matter') | select('is_state', 'unavailable') | list | length > 0 }}
      then:
        - service: homeassistant.reload_config_entry
          data:
            entry_id: YOUR_MATTER_CONFIG_ENTRY_ID
          continue_on_error: true

# 2. Nightly Healing: Proactively optimizes routes and flushes integration caches at 3:30 AM
- id: nightly_matter_thread_network_healing
  alias: System - Nightly Matter & Thread Network Healing
  description: Performs a proactive optimization, verifies host routes, and refreshes device links nightly.
  trigger:
    - platform: time
      at: '03:30:00'
  action:
    # Ensure the host IPv6 routes are correct
    - service: shell_command.optimize_thread_routes
    
    # Reload the Matter integration to clear caches and stale states
    - service: homeassistant.reload_config_entry
      data:
        entry_id: YOUR_MATTER_CONFIG_ENTRY_ID
```

---

## 3. Container-Level Recovery (`python-matter-server` + `autoheal`)

### The Background
Sometimes the `python-matter-server` daemon itself stops communicating with the Thread mesh or enters a state where it reports nodes as available to clients but cannot communicate with them. 

By combining a **custom container healthcheck** with the **autoheal** utility (a container that monitors Docker events and restarts unhealthy containers), we can automatically restart `python-matter-server` if the Thread network fails.

### The Solution

1. Put the following `healthcheck.py` script inside the data directory mapped to your `python-matter-server` container (e.g. `/data/healthcheck.py`).

**`healthcheck.py`**:
```python
import asyncio
import sys
import socket
import subprocess
import os
import time
from aiohttp import ClientSession
from matter_server.client import MatterClient

async def ping_ip(ip):
    cmd = ['ping', '-6', '-c', '1', '-W', '1', ip] if ':' in ip else ['ping', '-c', '1', '-W', '1', ip]
    def run_ping():
        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return res.returncode == 0
        except Exception:
            return False
    return await asyncio.to_thread(run_ping)

async def check():
    # 1. Quick socket check (runs immediately even during startup)
    try:
        with socket.create_connection(('localhost', 5580), timeout=2):
            pass
    except Exception:
        print('Port 5580 is closed')
        sys.exit(1)

    # 2. Get container uptime to bypass checks during container warmup
    try:
        uptime = time.time() - os.path.getmtime('/proc/1')
    except Exception:
        uptime = 999999

    if uptime < 900: # 15 minutes warmup threshold
        print(f'Healthy (Warmup - uptime: {int(uptime)}s)')
        sys.exit(0)

    # 3. Check Matter nodes via WebSocket
    try:
        async with ClientSession() as session:
            async with MatterClient('ws://localhost:5580/ws', session) as client:
                await client.connect()
                listen_task = asyncio.create_task(client.start_listening())
                await asyncio.sleep(2)
                
                nodes = client.get_nodes()
                
                async def check_node(node):
                    if node.available:
                        return None
                    try:
                        ips = await client.get_node_ip_addresses(node.node_id)
                        for ip in ips:
                            if await ping_ip(ip):
                                return (node.node_id, ip)
                    except Exception:
                        pass
                    return None

                tasks = [check_node(node) for node in nodes]
                results = await asyncio.gather(*tasks)
                stuck_nodes = [r for r in results if r is not None]
                unavailable_nodes = [node for node in nodes if not node.available]
                total_count = len(nodes)
                
                listen_task.cancel()
                try:
                    await listen_task
                except asyncio.CancelledError:
                    pass
                
                # Unhealthiness conditions:
                # 1. 5 or more nodes are stuck (pingable but unavailable)
                # 2. 8 or more nodes are completely offline
                # 3. 50%+ of all configured nodes are offline (when total nodes >= 5)
                is_unhealthy = False
                reason = ""
                
                if len(stuck_nodes) >= 5:
                    is_unhealthy = True
                    reason = f"{len(stuck_nodes)} nodes are stuck (pingable but unavailable)"
                elif len(unavailable_nodes) >= 8:
                    is_unhealthy = True
                    reason = f"{len(unavailable_nodes)} nodes are completely offline"
                elif total_count >= 5 and (len(unavailable_nodes) / total_count) >= 0.5:
                    is_unhealthy = True
                    reason = f"{len(unavailable_nodes)} out of {total_count} nodes are offline (>= 50%)"
                
                if is_unhealthy:
                    print(f'Unhealthy: {reason}')
                    sys.exit(1)
                
                print(f'Healthy (Total: {total_count}, Offline: {len(unavailable_nodes)}, Stuck: {len(stuck_nodes)})')
                sys.exit(0)
    except Exception as e:
        print(f'Error checking Matter client: {e}')
        sys.exit(1)

if __name__ == '__main__':
    asyncio.run(check())
```

2. **Configure Docker Compose**: Enable the healthcheck and run the autoheal container.

**`docker-compose.yml`**:
```yaml
services:
  matter-server:
    image: ghcr.io/home-assistant-libs/python-matter-server:stable
    container_name: matter-server
    network_mode: host
    restart: unless-stopped
    labels:
      autoheal: "true"
    volumes:
      - /path/to/matter-server/data:/data
      - /run/dbus:/run/dbus:ro
    healthcheck:
      test: ["CMD", "python3", "/data/healthcheck.py"]
      interval: 1m
      timeout: 10s
      retries: 3
      start_period: 30s

  autoheal:
    image: willfarrell/autoheal:latest
    container_name: autoheal
    restart: unless-stopped
    environment:
      AUTOHEAL_CONTAINER_LABEL: autoheal
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock

---

## 4. Radio-Level Optimizations (Transmit Power)

### The Background
By default, some Thread Border Router coordinators initialize with a low transmit power (e.g., `5 dBm` or ~3mW). This severely limits the range of the coordinator mesh, causing packet dropouts and poor link quality (LQ 1) for distant routers. Setting the transmit power to the hardware limit (typically `20 dBm` or 100mW) dramatically improves range and stability.

Because `ot-ctl` settings do not survive container restarts or reboots, we can persist this setting in the Docker Compose environment by adding the configuration directly to the OTBR container's health check loop.

### The Solution

Update your `docker-compose.yml` to set the transmit power inside the health check script:

```yaml
  otbr:
    image: ghcr.io/ownbee/hass-otbr-docker:latest
    container_name: otbr
    privileged: true
    network_mode: host
    restart: unless-stopped
    devices:
      - /dev/serial/by-id/usb-YOUR_DONGLE_ID:/dev/ttyUSB0
    environment:
      DEVICE: /dev/ttyUSB0
      BAUDRATE: "460800"
      BACKBONE_IF: eth0
    healthcheck:
      test:
        - CMD-SHELL
        - ot-ctl txpower 20 >/dev/null && ot-ctl state | grep -iE "router|leader|child"
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
```

*Every 30 seconds, the container automatically re-applies the `20 dBm` transmit power setting, ensuring it persists permanently across reboots.*

---

## 5. Benchmarking & Optimal Transmit Power Tuning

### Why Balance Matters
Simply cranking the coordinator's transmit power to the maximum (`20 dBm` / 100mW) is not always ideal:
1. **Receiver Saturation**: Devices located very close to the coordinator can have their radios overloaded, causing packet drops.
2. **Wi-Fi/Bluetooth Interference**: Thread sharing the 2.4GHz spectrum can interfere with nearby Wi-Fi routers or Zigbee coordinators if transmitting at high power.
3. **Power & Thermal Load**: Higher power increases thermal output on small USB dongles.

The goal is to find the **lowest possible transmit power** where all devices maintain a solid, stable link (typically an average RSSI >= `-85 dBm`).

### The Benchmarking Script (`benchmark.py`)
We created a Python script that automatically cycles through power levels (`5, 8, 11, 14, 17, and 20 dBm`), logs the signal strengths of all connected Thread neighbors, and provides a clear recommendation.

> [!NOTE]
> OpenThread uses exponentially smoothed sliding averages for the `Avg RSSI` metric. This means changing power levels takes time to reflect in the neighbor table. The script defaults to a **5-minute (300s) settle time** at each step to allow averages to converge. For a quick test, you can edit `SETTLE_TIME_SECONDS` in the script to `15` seconds.

#### How to Run the Benchmark
1. Ensure the script is executable on your host:
   ```bash
   chmod +x benchmark.py
   ```
2. Run the script:
   ```bash
   python3 benchmark.py
   ```

#### Sample Output
Below is an example output showing how the signal strength scales across different power levels:

```text
================================================================
 Thread Transmit Power Benchmark Tool
================================================================
Starting benchmark. We will test power levels: [5, 8, 11, 14, 17, 20]
Waiting 15 seconds at each step for link metrics to settle...
Do not modify network settings during the test.

--> Setting Tx Power to 5 dBm...
    Reading neighbor table...
...
================================================================
 Benchmark Results (Average RSSI per power level)
================================================================
Neighbor (Extended MAC) | RLOC16   |   5dBm |   8dBm |  11dBm |  14dBm |  17dBm |  20dBm
----------------------------------------------------------------------------------------
1211546fef5f3b73        | 0x3400   |   -78  |   -78  |   -78  |   -78  |   -78  |   -78
1e7dcb48c41b8ed8        | 0x5400   |   -74  |   -74  |   -74  |   -74  |   -74  |   -74
96d2a73831e3e919        | 0xe800   |   -89  |   -89  |   -89  |   -90  |   -90  |   -90
aa52d4a00dba4622        | 0x8000   |   -57  |   -57  |   -57  |   -56  |   -56  |   -56
...

Recommendations:
⚠ No single power level satisfied all conditions (due to distant node 96d2a73831e3e919 at -90 dBm).
  We recommend staying at 20 dBm to maximize range for weaker/distant nodes.
```

