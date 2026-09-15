#!/usr/bin/env python3
"""Discover hypervisor topology and compute a SAP HANA KVM guest layout.

Follows Red Hat "Deploying SAP HANA on Red Hat Virtualization 4.4"
(Cooper Lake / LUN PT / 6TB):
  - 1 GiB hugepages (fall back to 2 MiB if the kernel has no 1 GiB size)
  - Reserve the first physical core of each NUMA node for emulator/iothread
  - Guest sockets/cores/threads derived from the host
  - Equal memory per NUMA cell, aligned to hugepage size
  - Hypervisor memory reserve (100 GiB rule of thumb, scaled down on small hosts)
  - TSC frequency taken from the host clocksource calibration
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any


SYS_NODE = "/sys/devices/system/node"
SYS_CPU = "/sys/devices/system/cpu"
HP_1G_KB = 1048576
HP_2M_KB = 2048


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read().strip()


def parse_cpu_list(text: str) -> list[int]:
    cpus: list[int] = []
    if not text or text in ("", "\n"):
        return cpus
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            cpus.extend(range(int(start), int(end) + 1))
        else:
            cpus.append(int(part))
    return cpus


def format_cpuset(cpus: list[int] | tuple[int, ...]) -> str:
    ordered = sorted(set(int(c) for c in cpus))
    if not ordered:
        return ""
    ranges: list[tuple[int, int]] = []
    start = prev = ordered[0]
    for cpu in ordered[1:]:
        if cpu == prev + 1:
            prev = cpu
            continue
        ranges.append((start, prev))
        start = prev = cpu
    ranges.append((start, prev))
    parts = [str(a) if a == b else f"{a}-{b}" for a, b in ranges]
    return ",".join(parts)


def list_numa_nodes() -> list[int]:
    nodes = []
    for name in sorted(os.listdir(SYS_NODE)):
        if name.startswith("node") and name[4:].isdigit():
            node = int(name[4:])
            if os.path.isdir(os.path.join(SYS_NODE, name, "hugepages")) or os.path.isfile(
                os.path.join(SYS_NODE, name, "cpulist")
            ):
                nodes.append(node)
    return nodes


def node_memtotal_kib(node: int) -> int:
    path = os.path.join(SYS_NODE, f"node{node}", "meminfo")
    text = read_text(path)
    match = re.search(rf"Node {node} MemTotal:\s+(\d+)\s+kB", text)
    if not match:
        match = re.search(r"MemTotal:\s+(\d+)\s+kB", text)
    return int(match.group(1)) if match else 0


def node_cpulist(node: int) -> list[int]:
    return parse_cpu_list(read_text(os.path.join(SYS_NODE, f"node{node}", "cpulist")))


def thread_siblings(cpu: int) -> list[int]:
    path = os.path.join(SYS_CPU, f"cpu{cpu}", "topology", "thread_siblings_list")
    if not os.path.exists(path):
        return [cpu]
    return parse_cpu_list(read_text(path))


def cores_for_node(node: int) -> list[tuple[int, ...]]:
    """Physical cores on a NUMA node, each as a tuple of SMT sibling CPUs."""
    cpus = node_cpulist(node)
    cpu_set = set(cpus)
    seen: set[int] = set()
    cores: list[tuple[int, ...]] = []
    for cpu in sorted(cpus):
        if cpu in seen:
            continue
        sibs = tuple(sorted(s for s in thread_siblings(cpu) if s in cpu_set))
        if not sibs:
            sibs = (cpu,)
        seen.update(sibs)
        cores.append(sibs)
    cores.sort(key=lambda sibs: sibs[0])
    return cores


def lscpu_field(pattern: str) -> str | None:
    try:
        out = subprocess.check_output(["lscpu"], text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return None
    match = re.search(pattern, out, re.MULTILINE)
    return match.group(1).strip() if match else None


def parse_size_to_mib(value: str) -> int:
    """Parse qemu-style sizes like 150G, 20480M, 131072 into MiB."""
    text = str(value).strip().upper().replace("IB", "")
    match = re.fullmatch(r"(\d+)([KMGT])?", text)
    if not match:
        raise ValueError(f"invalid size: {value}")
    amount = int(match.group(1))
    suffix = match.group(2) or "M"
    mul = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[suffix]
    return int(amount * mul)


def format_gib(mib: int) -> str:
    if mib % 1024 == 0:
        return f"{mib // 1024}G"
    return f"{mib}M"


def hugepage_size_kb(preferred_kb: int) -> int:
    base = "/sys/kernel/mm/hugepages"
    preferred = os.path.join(base, f"hugepages-{preferred_kb}kB")
    if os.path.isdir(preferred):
        return preferred_kb
    if os.path.isdir(os.path.join(base, f"hugepages-{HP_1G_KB}kB")):
        return HP_1G_KB
    if os.path.isdir(os.path.join(base, f"hugepages-{HP_2M_KB}kB")):
        return HP_2M_KB
    return preferred_kb


def tsc_frequency_hz(override: int | None = None) -> int | None:
    if override:
        return int(override)

    candidates = [
        os.path.join(SYS_CPU, "cpu0", "tsc_freq_khz"),
        "/sys/devices/system/cpu/tsc_freq_khz",
    ]
    for path in candidates:
        if os.path.exists(path):
            khz = int(read_text(path))
            if khz > 0:
                return khz * 1000

    patterns = [
        r"Refined TSC clocksource calibration:\s+([0-9.]+)\s+MHz",
        r"tsc: Refined TSC clocksource calibration:\s+([0-9.]+)\s+MHz",
        r"tsc:\s+Detected\s+([0-9.]+)\s+MHz",
        r"TSC frequency:\s+([0-9.]+)\s+MHz",
        r"host frequency ([0-9]+) Hz",
    ]
    for cmd in (
        ["dmesg"],
        ["journalctl", "-kb", "-o", "cat", "--no-pager"],
    ):
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError):
            continue
        for pattern in patterns:
            match = re.search(pattern, out)
            if match:
                value = match.group(1)
                if "Hz" in match.group(0) and "MHz" not in match.group(0):
                    return int(value)
                return int(round(float(value) * 1_000_000))

    try:
        cpuinfo = read_text("/proc/cpuinfo")
    except OSError:
        return None
    match = re.search(r"^cpu MHz\s*:\s*([0-9.]+)", cpuinfo, re.MULTILINE)
    if match:
        return int(round(float(match.group(1)) * 1_000_000))
    return None


def dir_free_mib(path: str) -> int:
    probe = path
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            probe = "/"
            break
        probe = parent or "/"
    st = os.statvfs(probe)
    return int(st.f_bavail * st.f_frsize / (1024 * 1024))


def compute_guest_memory_mib(
    node_mem_kib: list[int],
    numa_nodes: int,
    page_kb: int,
    reserve_gib: int,
    reserve_min_gib_per_node: int,
    memory_mib_override: int | None,
) -> tuple[int, int, int]:
    """Return (total_mib, per_node_mib, per_node_reserve_mib)."""
    page_mib = max(1, page_kb // 1024)
    node_mib = [m // 1024 for m in node_mem_kib]
    min_node_mib = min(node_mib) if node_mib else 0

    per_node_reserve = max(reserve_min_gib_per_node * 1024, (reserve_gib * 1024) // numa_nodes)
    # Lab/small hosts: keep at least one hugepage per node for the guest.
    if min_node_mib - per_node_reserve < page_mib:
        per_node_reserve = max(reserve_min_gib_per_node * 1024, min_node_mib // 8)
        per_node_reserve = min(per_node_reserve, max(0, min_node_mib - page_mib))

    if memory_mib_override is not None:
        per_node = (memory_mib_override // numa_nodes // page_mib) * page_mib
    else:
        per_node = ((min_node_mib - per_node_reserve) // page_mib) * page_mib

    per_node = max(page_mib, per_node)
    return per_node * numa_nodes, per_node, per_node_reserve


def build_layout(
    cores_by_node: list[list[tuple[int, ...]]],
    reserve_first_core: bool,
    threads: int,
    vcpus_override: int | None,
) -> dict[str, Any]:
    reserved: list[tuple[int, ...]] = []
    guest_cores: list[list[tuple[int, ...]]] = []
    for cores in cores_by_node:
        if not cores:
            guest_cores.append([])
            continue
        if reserve_first_core:
            reserved.append(cores[0])
            rest = cores[1:] if len(cores) > 1 else []
        else:
            rest = list(cores)
        guest_cores.append(rest)

    if not guest_cores or any(len(c) == 0 for c in guest_cores):
        raise SystemExit(
            "Not enough CPU cores per NUMA node to reserve the first core for the hypervisor"
        )

    cores_per_socket = min(len(c) for c in guest_cores)
    numa_nodes = len(guest_cores)
    if vcpus_override:
        if vcpus_override % (numa_nodes * threads) != 0:
            raise SystemExit(
                f"--vcpus {vcpus_override} is not divisible by "
                f"numa_nodes*threads ({numa_nodes}*{threads})"
            )
        limit = vcpus_override // (numa_nodes * threads)
        if limit < 1:
            raise SystemExit("--vcpus is too small for this topology")
        cores_per_socket = min(cores_per_socket, limit)

    guest_cores = [c[:cores_per_socket] for c in guest_cores]
    pins = []
    vcpu = 0
    for node_cores in guest_cores:
        for sibs in node_cores:
            cpuset = format_cpuset(sibs)
            for _ in range(threads):
                pins.append({"vcpu": vcpu, "cpuset": cpuset})
                vcpu += 1

    reserved_cpus = [cpu for core in reserved for cpu in core]
    iothreadpins = []
    for idx, core in enumerate(reserved, start=1):
        iothreadpins.append({"iothread": idx, "cpuset": format_cpuset(core)})

    return {
        "vcpus": vcpu,
        "sockets": numa_nodes,
        "cores_per_socket": cores_per_socket,
        "threads_per_core": threads,
        "numa_nodes": numa_nodes,
        "cpu_pins": pins,
        "reserved_cores": [list(c) for c in reserved],
        "emulatorpin": format_cpuset(reserved_cpus) if reserved_cpus else format_cpuset(
            [c for cores in cores_by_node for sibs in cores[:1] for c in sibs]
        ),
        "iothreadpins": iothreadpins,
    }


def discover(args: argparse.Namespace) -> dict[str, Any]:
    nodes = list_numa_nodes()
    if not nodes:
        raise SystemExit("No NUMA nodes found under /sys/devices/system/node")

    cores_by_node = [cores_for_node(n) for n in nodes]
    node_mem_kib = [node_memtotal_kib(n) for n in nodes]
    sibling_widths = [len(sibs) for cores in cores_by_node for sibs in cores]
    host_threads = max(sibling_widths) if sibling_widths else 1

    lscpu_sockets = lscpu_field(r"^Socket\(s\):\s+(\d+)")
    lscpu_cores = lscpu_field(r"^Core\(s\) per socket:\s+(\d+)")
    lscpu_model = lscpu_field(r"^Model name:\s+(.+)$")
    lscpu_cpus = lscpu_field(r"^CPU\(s\):\s+(\d+)")

    page_kb = hugepage_size_kb(args.hugepage_size_kb)
    layout = build_layout(
        cores_by_node=cores_by_node,
        reserve_first_core=args.reserve_first_core,
        threads=host_threads,
        vcpus_override=args.vcpus,
    )

    memory_mib, per_node_mib, per_node_reserve_mib = compute_guest_memory_mib(
        node_mem_kib=node_mem_kib,
        numa_nodes=layout["numa_nodes"],
        page_kb=page_kb,
        reserve_gib=args.reserve_gib,
        reserve_min_gib_per_node=args.reserve_min_gib_per_node,
        memory_mib_override=args.memory_mib,
    )

    free_mib = dir_free_mib(args.kvm_dir)
    if args.disk_size:
        disk_size = args.disk_size
        disk_mib = parse_size_to_mib(args.disk_size)
    else:
        preferred_mib = args.disk_preferred_gib * 1024
        reserve_mib = args.disk_reserve_gib * 1024
        usable = max(1024, free_mib - reserve_mib)
        disk_mib = min(preferred_mib, usable)
        disk_size = format_gib(disk_mib)

    tsc_hz = tsc_frequency_hz(args.tsc_hz)
    iothreads = max(1, args.iothreads)
    iothreadpins = layout["iothreadpins"][:iothreads]
    if not iothreadpins:
        iothreadpins = [{"iothread": 1, "cpuset": layout["emulatorpin"]}]

    host = {
        "model": lscpu_model,
        "cpus": int(lscpu_cpus) if lscpu_cpus else sum(
            len(cpu) for cores in cores_by_node for cpu in cores
        ),
        "sockets": int(lscpu_sockets) if lscpu_sockets else len(nodes),
        "cores_per_socket": int(lscpu_cores) if lscpu_cores else (
            len(cores_by_node[0]) if cores_by_node else 0
        ),
        "threads_per_core": host_threads,
        "numa_nodes": nodes,
        "numa_cpus": {str(n): node_cpulist(n) for n in nodes},
        "numa_cores": {str(n): [list(c) for c in cores] for n, cores in zip(nodes, cores_by_node)},
        "numa_memory_mib": {str(n): m // 1024 for n, m in zip(nodes, node_mem_kib)},
        "hugepage_size_kb_available": page_kb,
        "disk_free_mib": free_mib,
        "tsc_frequency_hz": tsc_hz,
    }

    guest = {
        "vcpus": layout["vcpus"],
        "sockets": layout["sockets"],
        "cores_per_socket": layout["cores_per_socket"],
        "threads_per_core": layout["threads_per_core"],
        "numa_nodes": layout["numa_nodes"],
        "memory_mib": memory_mib,
        "numa_memory_mib": per_node_mib,
        "cpu_pins": layout["cpu_pins"],
        "emulatorpin": layout["emulatorpin"],
        "iothreads": iothreads,
        "iothreadpins": iothreadpins,
        "iothreadpin": iothreadpins[0]["cpuset"] if iothreadpins else layout["emulatorpin"],
        "hugepages_size_kb": page_kb,
        "hugepages_per_node": (per_node_mib * 1024) // page_kb,
        "hugepage_nodes": nodes,
        "tsc_frequency": tsc_hz,
        "disk_size": disk_size,
        "hypervisor_reserve_mib_per_node": per_node_reserve_mib,
    }

    summary = [
        f"Host: {host['model'] or 'unknown'}",
        f"Host CPUs: {host['cpus']} ({host['sockets']} sockets, "
        f"{host['cores_per_socket']} cores/socket, {host['threads_per_core']} threads)",
        f"Host NUMA nodes: {host['numa_nodes']}",
        f"Host memory/node MiB: {host['numa_memory_mib']}",
        f"Guest: {guest['sockets']} sockets × {guest['cores_per_socket']} cores × "
        f"{guest['threads_per_core']} threads = {guest['vcpus']} vCPUs",
        f"Guest memory: {guest['memory_mib']} MiB "
        f"({guest['numa_memory_mib']} MiB × {guest['numa_nodes']} NUMA cells)",
        f"Hugepages: {guest['hugepages_per_node']} × {guest['hugepages_size_kb']} KiB per node",
        f"Emulator pin: {guest['emulatorpin']}",
        f"IO thread pin: {guest['iothreadpin']}",
        f"TSC frequency: {guest['tsc_frequency'] or 'not detected'} Hz",
        f"OS disk: {guest['disk_size']} (fs free {host['disk_free_mib']} MiB)",
    ]

    return {"host": host, "guest": guest, "summary": summary}


def self_test() -> None:
    """Verify pinning against the RHV Cooper Lake 4-socket example."""
    # Interleaved 4-socket, 28 cores, HT offset 112.
    cores_by_node: list[list[tuple[int, ...]]] = []
    for node in range(4):
        cores = []
        for core in range(28):
            t0 = node + core * 4
            cores.append((t0, t0 + 112))
        cores_by_node.append(cores)
    layout = build_layout(cores_by_node, reserve_first_core=True, threads=2, vcpus_override=None)
    assert layout["vcpus"] == 216, layout["vcpus"]
    assert layout["cores_per_socket"] == 27
    assert layout["cpu_pins"][0] == {"vcpu": 0, "cpuset": "4,116"}
    assert layout["cpu_pins"][1] == {"vcpu": 1, "cpuset": "4,116"}
    assert layout["cpu_pins"][54] == {"vcpu": 54, "cpuset": "5,117"}
    assert layout["cpu_pins"][-1] == {"vcpu": 215, "cpuset": "111,223"}
    assert layout["emulatorpin"] == "0-3,112-115"
    assert layout["iothreadpins"][0]["cpuset"] == "0,112"
    mem, per, _res = compute_guest_memory_mib(
        node_mem_kib=[64 * 1024 * 1024] * 4,
        numa_nodes=4,
        page_kb=HP_1G_KB,
        reserve_gib=100,
        reserve_min_gib_per_node=2,
        memory_mib_override=131072,
    )
    assert mem == 131072 and per == 32768, (mem, per)
    print("self-test ok", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reserve-gib", type=int, default=100)
    parser.add_argument("--reserve-min-gib-per-node", type=int, default=2)
    parser.add_argument("--kvm-dir", default="/home/kvm")
    parser.add_argument("--disk-preferred-gib", type=int, default=150)
    parser.add_argument("--disk-reserve-gib", type=int, default=32)
    parser.add_argument("--disk-size", default=None)
    parser.add_argument("--hugepage-size-kb", type=int, default=HP_1G_KB)
    parser.add_argument("--reserve-first-core", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--vcpus", type=int, default=None)
    parser.add_argument("--memory-mib", type=int, default=None)
    parser.add_argument("--tsc-hz", type=int, default=None)
    parser.add_argument("--iothreads", type=int, default=1)
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    result = discover(args)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
