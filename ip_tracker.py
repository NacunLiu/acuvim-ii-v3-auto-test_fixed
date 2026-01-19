"""ip_tracker.py

Resolve Kasa smart-plug IPs by scanning the local subnet and matching MAC addresses.

Compatibility note
------------------
This module exposes BOTH:
  - `get_target_ip_map()`  (recommended)
  - `targetIp`             (legacy global used by older modules)

If scanning fails, it returns an empty dict rather than throwing, so the rest of
the program can still start and print a helpful message.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Dict, Tuple


# =========================
# User-configurable section
# =========================

# MAC -> plug id
# Keep MACs uppercase with ':' separators.
TARGET_MACS: Dict[str, int] = {
    # Example from your environment
    "78:8C:B5:B5:15:9C": 1,
    "78:8C:B5:B5:07:58": 2,
    # Add more here if needed
    # "9C:A2:F4:95:3E:27": 3,
    # "9C:A2:F4:95:3D:55": 4,
    # "9C:A2:F4:95:3E:47": 5,
}

# Your mask is 255.255.255.0, so /24 is the standard CIDR.
SUBNET = os.environ.get("ACU_PLUG_SUBNET", "192.168.61.0/24")


# =========================
# Implementation
# =========================

_CACHE: Dict[int, Tuple[str, str]] | None = None


def _run_nmap_ping_scan(subnet: str, timeout_s: int = 30) -> str:
    """Run `nmap -sn` and return stdout text.

    Notes on Windows:
    - If nmap isn't in PATH, this will fail.
    - If you run without admin rights, sometimes MACs won't appear.
    """
    # -sn: ping scan (no port scan)
    return subprocess.check_output(
        ["nmap", "-sn", subnet],
        universal_newlines=True,
        errors="ignore",
        timeout=timeout_s,
    )


def scan_subnet_for_macs(subnet: str, timeout_s: int = 30) -> Dict[str, str]:
    """Scan subnet and return mapping: MAC (UPPER) -> IP."""
    try:
        out = _run_nmap_ping_scan(subnet, timeout_s=timeout_s)
    except Exception:
        return {}

    ip_for_mac: Dict[str, str] = {}
    current_ip: str | None = None

    for line in out.splitlines():
        m_ip = re.search(r"Nmap scan report for\s+(\d+\.\d+\.\d+\.\d+)", line)
        if m_ip:
            current_ip = m_ip.group(1)
            continue

        m_mac = re.search(
            r"MAC Address:\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})",
            line,
        )
        if m_mac and current_ip:
            mac = m_mac.group(1).upper()
            ip_for_mac[mac] = current_ip

    return ip_for_mac


def get_target_ip_map(force_refresh: bool = False) -> Dict[int, Tuple[str, str]]:
    """Resolve plug IPs once and return:

    Returns:
        Dict[int, Tuple[str, str]]: {plug_id: (mac, ip), ...}
    """
    global _CACHE
    if _CACHE is not None and not force_refresh:
        return _CACHE

    mapping = scan_subnet_for_macs(SUBNET)
    result: Dict[int, Tuple[str, str]] = {}

    for mac, plug_no in TARGET_MACS.items():
        ip = mapping.get(mac.upper())
        if ip:
            result[plug_no] = (mac.upper(), ip)

    _CACHE = result
    return result


# Legacy global for backward compatibility
try:
    targetIp: Dict[int, Tuple[str, str]] = get_target_ip_map()
except Exception:
    targetIp = {}


if __name__ == "__main__":
    targetIp = get_target_ip_map(force_refresh=True)
    print("targetIp:", targetIp)
    if not targetIp:
        print(
            "No plug found by MAC on this subnet. Possible reasons: "
            "Wi-Fi client isolation / no MAC in nmap output / nmap not in PATH / need Admin."
        )
