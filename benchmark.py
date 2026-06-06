#!/usr/bin/env python3
import subprocess
import time
import re
import sys

# TX Power levels to test (in dBm)
TEST_LEVELS = [5, 8, 11, 14, 17, 20]
SETTLE_TIME_SECONDS = 300  # Allow link metrics/averages to settle (5 mins recommended)

def run_cmd(cmd):
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True)
        return res.stdout
    except subprocess.CalledProcessError as e:
        print(f"Error running command: {cmd}\nStderr: {e.stderr}")
        return None

def set_txpower(level):
    run_cmd(f"docker exec otbr ot-ctl txpower {level}")

def get_neighbors():
    output = run_cmd("docker exec otbr ot-ctl neighbor table")
    if not output:
        return {}
    
    neighbors = {}
    # Parse table lines like:
    # |   R  | 0x1800 |   9 |      -77 |       -77 |1|1|1| 521746bb9a88e772 |       5 |
    pattern = re.compile(r"\|\s*([RCD])\s*\|\s*(0x[0-9a-fA-F]+)\s*\|\s*\d+\s*\|\s*(-?\d+)\s*\|\s*-?\d+\s*\|.*\|\s*([0-9a-fA-F]{16})\s*\|")
    for line in output.splitlines():
        match = pattern.search(line)
        if match:
            role, rloc, rssi, mac = match.groups()
            neighbors[mac] = {
                "role": role,
                "rloc": rloc,
                "rssi": int(rssi)
            }
    return neighbors

def main():
    print("================================================================")
    print(" Thread Transmit Power Benchmark Tool")
    print("================================================================")
    print(f"Starting benchmark. We will test power levels: {TEST_LEVELS}")
    print(f"Waiting {SETTLE_TIME_SECONDS} seconds at each step for link metrics to settle...")
    print("Do not modify network settings during the test.\n")

    # Keep track of initial txpower to restore it later
    initial_power_out = run_cmd("docker exec otbr ot-ctl txpower")
    initial_power = 20
    if initial_power_out:
        match = re.search(r"(\d+)\s*dBm", initial_power_out)
        if match:
            initial_power = int(match.group(1))

    results = {}  # {mac: {level: rssi}}
    neighbor_meta = {}  # {mac: {rloc, role}}

    try:
        for level in TEST_LEVELS:
            print(f"--> Setting Tx Power to {level} dBm...", flush=True)
            set_txpower(level)
            time.sleep(SETTLE_TIME_SECONDS)
            
            print(f"    Reading neighbor table...", flush=True)
            current_neighbors = get_neighbors()
            
            for mac, data in current_neighbors.items():
                if mac not in results:
                    results[mac] = {}
                    neighbor_meta[mac] = {"rloc": data["rloc"], "role": data["role"]}
                results[mac][level] = data["rssi"]

        # Restore initial power
        print(f"\nRestoring initial transmit power of {initial_power} dBm...")
        set_txpower(initial_power)

        print("\n================================================================")
        print(" Benchmark Results (Average RSSI per power level)")
        print("================================================================")
        
        # Header
        header = f"{'Neighbor (Extended MAC)':<22} | {'RLOC16':<8} | " + " | ".join(f"{l:>3}dBm" for l in TEST_LEVELS)
        print(header)
        print("-" * len(header))

        for mac in sorted(results.keys()):
            meta = neighbor_meta[mac]
            row_parts = [f"{mac:<22}", f"{meta['rloc']:<8}"]
            for level in TEST_LEVELS:
                rssi = results[mac].get(level)
                if rssi is None:
                    row_parts.append(f"{'OFF':>5}")
                else:
                    row_parts.append(f"{rssi:>5}")
            print(" | ".join(row_parts))

        print("\nRecommendations:")
        # Analyze results to find optimal level
        # Optimal is the lowest level where all nodes are online and RSSI >= -85
        optimal_level = None
        for level in TEST_LEVELS:
            all_online = True
            all_acceptable = True
            for mac in results.keys():
                rssi = results[mac].get(level)
                if rssi is None:
                    all_online = False
                elif rssi < -85:
                    all_acceptable = False
            
            if all_online and all_acceptable:
                optimal_level = level
                break

        if optimal_level:
            print(f"✔ Optimal Transmit Power Level: {optimal_level} dBm")
            print("  This is the lowest tested power level where all neighbors are online")
            print("  and maintain an acceptable signal strength (>= -85 dBm).")
        else:
            print("⚠ No single power level satisfied all conditions.")
            print("  We recommend staying at 20 dBm to maximize range for weaker/distant nodes.")

    except KeyboardInterrupt:
        print(f"\nAborted. Restoring initial transmit power of {initial_power} dBm...")
        set_txpower(initial_power)
        sys.exit(1)

if __name__ == "__main__":
    main()
