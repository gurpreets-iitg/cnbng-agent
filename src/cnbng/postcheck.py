# __________________________________________________
#
# Cloud Native BNG / Cisco Subscriber Edge
# Control Plane Deployment Agent
#
# Author: Gurpreet Dhaliwal, TME MiG
# __________________________________________________

"""Post-Day0 network validation for CNBNG 3-server geo-redundant clusters."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import paramiko
import yaml

from cnbng.input_resolver import resolve_input


NODE_NAMES = ("ucs01", "ucs02", "ucs03")
GEO_NETWORKS = ("inttcp", "cdl")
SERVICE_NETWORKS = ("inttcp", "intudp", "n4", "cdl")
NETWORK_ORDER = ("mgmt", "k8s", "inttcp", "intudp", "n4", "cdl", "bgp")
BGP_PATH_TO_INTERFACE = {
    "bgp_a": "ebgp1",
    "bgp_b": "ebgp2",
}


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


@dataclass
class CheckSection:
    title: str
    results: list[CheckResult]


class ProgressBar:
    def __init__(self, title: str, total: int):
        self.title = title
        self.total = max(total, 1)
        self.current = 0
        self.is_tty = sys.stdout.isatty()
        if self.is_tty:
            self.render("starting")
        else:
            print(f"Running {self.title} ({total} checks)...")

    def step(self, label: str = "") -> None:
        self.current += 1
        self.render(label)

    def advance(self, count: int, label: str = "") -> None:
        self.current += count
        self.render(label)

    def render(self, label: str = "") -> None:
        if not self.is_tty:
            return
        width = 28
        current = min(self.current, self.total)
        filled = int(width * current / self.total)
        bar = "#" * filled + "-" * (width - filled)
        pct = int(100 * current / self.total)
        detail = f" {label}" if label else ""
        detail = detail[:54]
        print(f"\r{self.title:28} [{bar}] {current:3}/{self.total:<3} {pct:3}%{detail}", end="", flush=True)

    def done(self) -> None:
        if self.current < self.total:
            self.current = self.total
        if self.is_tty:
            self.render("done")
            print()
        else:
            print(f"Completed {self.title}.")


def release_root() -> Path:
    return Path(__file__).resolve().parents[2]


def make_run_dir(run_id: str | None = None) -> Path:
    run_name = run_id or "cnbng-postcheck-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = release_root() / "runs" / run_name
    for child in ("generated", "logs"):
        (run_dir / child).mkdir(parents=True, exist_ok=True)
    return run_dir


def run_local(command: list[str], log_file: Path) -> None:
    with log_file.open("w") as log:
        process = subprocess.run(
            command,
            cwd=str(release_root()),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    if process.returncode != 0:
        raise RuntimeError(f"command failed; see {log_file}")


def generate_yaml(xlsx_path: str, output_yaml: Path, log_file: Path, cluster: str | None = None) -> None:
    command = [
        sys.executable,
        "src/cnbng/xlsx_to_yaml.py",
        xlsx_path,
        "--output",
        str(output_yaml),
    ]
    run_local(command, log_file)


def load_yaml(path: Path) -> dict:
    with path.open() as handle:
        return yaml.safe_load(handle) or {}


def strip_cidr(value: str) -> str:
    return str(value).split("/", 1)[0]


def expected_interface_name(cluster_networks: dict, logical_name: str, body: dict) -> str | None:
    if logical_name == "bgp":
        return None
    network = cluster_networks.get(logical_name, {})
    base = network.get("intf")
    vlan = network.get("id")
    if not base or vlan is None:
        return None
    return f"{base}.{logical_name}.{vlan}"


def expected_node_interfaces(data: dict, node_name: str) -> list[dict]:
    cp = data.get("cnbng_cp", {})
    cluster_networks = cp.get("cluster", {}).get("networks", {})
    node_networks = cp.get(node_name, {}).get("networks", {})
    expected: list[dict] = []

    for logical_name, body in node_networks.items():
        if logical_name == "bgp":
            for bgp_name, bgp_body in body.items():
                if bgp_body.get("intf") and bgp_body.get("id") is not None and bgp_body.get("ip"):
                    expected.append(
                        {
                            "logical": f"bgp.{bgp_name}",
                            "interface": f"{bgp_body['intf']}.{bgp_body['id']}",
                            "ip": bgp_body["ip"],
                        }
                    )
            continue

        if not isinstance(body, dict) or not body.get("ip"):
            continue
        interface = expected_interface_name(cluster_networks, logical_name, body)
        if interface:
            expected.append({"logical": logical_name, "interface": interface, "ip": body["ip"]})

    return expected


def node_mgmt_ip(data: dict, node_name: str) -> str:
    return strip_cidr(data["cnbng_cp"][node_name]["networks"]["mgmt"]["ip"])


def connect_inception(data: dict, timeout: int) -> paramiko.SSHClient:
    inception = data.get("inception_vm", {})
    host = inception.get("ip")
    user = inception.get("user", "cloud-user")
    password = inception.get("password")
    port = int(inception.get("port", 22))
    if not host or not password:
        raise RuntimeError("inception_vm.ip and inception_vm.password are required for postcheck")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        port=port,
        username=user,
        password=password,
        look_for_keys=False,
        allow_agent=False,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
    )
    return client


def run_on_node(client: paramiko.SSHClient, node_ip: str, command: str, timeout: int) -> tuple[int, str, str]:
    ssh_command = (
        "ssh -o BatchMode=yes -o LogLevel=ERROR -o StrictHostKeyChecking=no "
        "-o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 "
        f"cloud-user@{shlex.quote(node_ip)} {shlex.quote(command)}"
    )
    transport = client.get_transport()
    if transport is None:
        raise RuntimeError("Inception SSH transport is not connected")

    channel = transport.open_session(timeout=timeout)
    channel.set_combine_stderr(True)
    channel.exec_command(ssh_command)
    output_chunks: list[bytes] = []
    deadline = time.time() + timeout
    while True:
        while channel.recv_ready():
            output_chunks.append(channel.recv(65535))
        if channel.exit_status_ready():
            while channel.recv_ready():
                output_chunks.append(channel.recv(65535))
            return channel.recv_exit_status(), b"".join(output_chunks).decode(errors="replace"), ""
        if time.time() > deadline:
            channel.close()
            return 124, b"".join(output_chunks).decode(errors="replace"), f"command timed out after {timeout}s"
        time.sleep(0.1)


def interface_inventory(client: paramiko.SSHClient, node_ip: str, timeout: int) -> tuple[dict, str]:
    rc, stdout, stderr = run_on_node(client, node_ip, "ip -j addr", timeout)
    if rc != 0:
        raise RuntimeError(f"failed to read interfaces from {node_ip}: {stderr.strip() or stdout.strip()}")
    by_name = {}
    for item in json.loads(stdout):
        by_name[item.get("ifname")] = item
    return by_name, stdout


def check_interface(inventory: dict, expected: dict) -> CheckResult:
    interface_name = expected["interface"]
    expected_interface = ipaddress.ip_interface(expected["ip"])
    item = inventory.get(interface_name)
    if not item:
        return CheckResult(interface_name, False, f"missing interface for {expected['logical']}")

    actual_addresses = []
    for addr in item.get("addr_info", []):
        if addr.get("family") == "inet":
            actual_addresses.append(f"{addr.get('local')}/{addr.get('prefixlen')}")
    if str(expected_interface) not in actual_addresses:
        return CheckResult(
            interface_name,
            False,
            f"{expected['logical']} expected {expected_interface}, found {', '.join(actual_addresses) or 'no IPv4'}",
        )

    if item.get("operstate") not in {"UP", "UNKNOWN"}:
        return CheckResult(interface_name, False, f"{expected['logical']} operstate is {item.get('operstate')}")

    return CheckResult(interface_name, True, f"{expected['logical']} {expected_interface}")


def ping(
    client: paramiko.SSHClient,
    source_node_ip: str,
    source_ip: str,
    dest_ip: str,
    timeout: int,
) -> CheckResult:
    command = f"timeout 6 ping -c 1 -W 2 -I {shlex.quote(strip_cidr(source_ip))} {shlex.quote(strip_cidr(dest_ip))}"
    rc, stdout, stderr = run_on_node(client, source_node_ip, command, min(timeout, 10))
    detail = (stdout + stderr).strip().splitlines()
    short = detail[-1] if detail else "no output"
    return CheckResult(f"{strip_cidr(source_ip)} -> {strip_cidr(dest_ip)}", rc == 0, short)


def network_node_ips(data: dict, logical_name: str) -> list[tuple[str, str]]:
    result = []
    for node_name in NODE_NAMES:
        body = data.get("cnbng_cp", {}).get(node_name, {}).get("networks", {}).get(logical_name)
        if isinstance(body, dict) and body.get("ip"):
            result.append((node_name, body["ip"]))
    return result


def network_vips(data: dict, logical_name: str) -> list[tuple[str, str]]:
    cluster = data.get("cnbng_cp", {}).get("cluster", {})
    result: list[tuple[str, str]] = []
    if logical_name == "k8s" and cluster.get("master", {}).get("vip1"):
        result.append(("master.vip1", str(cluster["master"]["vip1"])))
    if logical_name == "mgmt" and cluster.get("master", {}).get("vip2"):
        result.append(("master.vip2", str(cluster["master"]["vip2"])))

    body = cluster.get("networks", {}).get(logical_name, {})
    result.extend((key, str(value)) for key, value in sorted(body.items()) if key.startswith("vip") and value)
    return result


def local_ping_vips(data: dict, logical_name: str) -> list[tuple[str, str]]:
    if logical_name == "n4":
        return []
    return network_vips(data, logical_name)


def print_network_coverage(label: str, data: dict) -> None:
    print(f"\n{label} Network Coverage")
    logical_networks = sorted(
        {
            logical
            for node_name in NODE_NAMES
            for logical in data.get("cnbng_cp", {}).get(node_name, {}).get("networks", {})
            if logical != "bgp"
        }
    )
    for logical in logical_networks:
        node_ips = network_node_ips(data, logical)
        configured_nodes = {node for node, _ip in node_ips}
        configured = ", ".join(f"{node}:{ip}" for node, ip in node_ips)
        vips = ", ".join(f"{name}:{ip}" for name, ip in network_vips(data, logical))
        vip_text = f"; VIPs {vips}" if vips else ""
        if logical in SERVICE_NETWORKS:
            missing = ", ".join(node for node in NODE_NAMES if node not in configured_nodes)
            missing_text = f"; not configured on {missing}" if missing else ""
            print(f"  {logical}: configured on {configured if configured else 'none'}{missing_text}{vip_text}")
        else:
            print(f"  {logical}: {configured if configured else 'not configured on any node'}{vip_text}")

    bgp_nodes = []
    for node_name in NODE_NAMES:
        bgps = data.get("cnbng_cp", {}).get(node_name, {}).get("networks", {}).get("bgp", {})
        if bgps:
            bgp_items = ", ".join(f"{name}:{body.get('ip')}" for name, body in sorted(bgps.items()))
            bgp_nodes.append(f"{node_name}({bgp_items})")
    print(f"  bgp: {', '.join(bgp_nodes) if bgp_nodes else 'not configured on any node'}")


def print_report_header(cp1_xlsx: str, cp2_xlsx: str, run_dir: Path) -> None:
    print("CNBNG Postcheck Report")
    print("======================")
    print(f"CP1 workbook: {cp1_xlsx}")
    print(f"CP2 workbook: {cp2_xlsx}")
    print(f"Run directory: {run_dir}")


def count_cluster_interface_operations(data: dict) -> int:
    return sum(1 + len(expected_node_interfaces(data, node_name)) for node_name in NODE_NAMES)


def count_intra_cluster_checks(data: dict) -> int:
    total = 0
    logical_networks = sorted(
        {
            logical
            for node_name in NODE_NAMES
            for logical in data.get("cnbng_cp", {}).get(node_name, {}).get("networks", {})
            if logical != "bgp"
        }
    )
    for logical in logical_networks:
        node_count = len(network_node_ips(data, logical))
        total += node_count * max(node_count - 1, 0)
    return total


def count_local_vip_checks(data: dict) -> int:
    total = 0
    logical_networks = sorted(
        {
            logical
            for node_name in NODE_NAMES
            for logical in data.get("cnbng_cp", {}).get(node_name, {}).get("networks", {})
            if logical != "bgp"
        }
    )
    for logical in logical_networks:
        total += len(network_node_ips(data, logical)) * len(local_ping_vips(data, logical))
    return total


def count_geo_checks(left: dict, right: dict, logical: str) -> int:
    left_sources = network_node_ips(left, logical)
    right_targets = network_node_ips(right, logical) + [
        (f"{logical}-{vip_name}", vip_ip) for vip_name, vip_ip in network_vips(right, logical)
    ]
    return len(left_sources) * len(right_targets)


def count_bgp_peer_checks(data: dict) -> int:
    cp = data.get("cnbng_cp", {})
    sessions = cp.get("bgp", {}).get("sessions", {})
    legacy_peers = cp.get("bgp", {}).get("peers", {})
    total = 0
    for node_name in NODE_NAMES:
        node_sessions = sessions.get(node_name, {})
        if not node_sessions and legacy_peers:
            node_sessions = legacy_peers
        total += len(node_sessions)
    return total


def check_cluster_interfaces(
    label: str,
    data: dict,
    client: paramiko.SSHClient,
    timeout: int,
    progress: ProgressBar | None = None,
) -> list[CheckResult]:
    results = []
    for node_name in NODE_NAMES:
        node_ip = node_mgmt_ip(data, node_name)
        expected_interfaces = expected_node_interfaces(data, node_name)
        try:
            inventory, _ = interface_inventory(client, node_ip, timeout)
            if progress:
                progress.step(f"{label}.{node_name}.inventory")
        except Exception as exc:  # noqa: BLE001
            results.append(CheckResult(f"{label}.{node_name}.ssh", False, str(exc)))
            if progress:
                progress.advance(1 + len(expected_interfaces), f"{label}.{node_name}.ssh failed")
            continue
        for expected in expected_interfaces:
            result = check_interface(inventory, expected)
            result.name = f"{label}.{node_name}.{result.name}"
            results.append(result)
            if progress:
                progress.step(result.name)
    return results


def check_intra_cluster(
    label: str,
    data: dict,
    client: paramiko.SSHClient,
    timeout: int,
    progress: ProgressBar | None = None,
) -> list[CheckResult]:
    results = []
    logical_networks = sorted(
        {
            logical
            for node_name in NODE_NAMES
            for logical in data.get("cnbng_cp", {}).get(node_name, {}).get("networks", {})
            if logical != "bgp"
        }
    )
    for logical in logical_networks:
        node_ips = network_node_ips(data, logical)
        for source_node, source_ip in node_ips:
            source_mgmt = node_mgmt_ip(data, source_node)
            for dest_node, dest_ip in node_ips:
                if source_node == dest_node:
                    continue
                result = ping(client, source_mgmt, source_ip, dest_ip, timeout)
                result.name = f"{label}.{logical}.{source_node}->{dest_node}"
                results.append(result)
                if progress:
                    progress.step(result.name)
    return results


def check_local_vips(
    label: str,
    data: dict,
    client: paramiko.SSHClient,
    timeout: int,
    progress: ProgressBar | None = None,
) -> list[CheckResult]:
    results = []
    logical_networks = sorted(
        {
            logical
            for node_name in NODE_NAMES
            for logical in data.get("cnbng_cp", {}).get(node_name, {}).get("networks", {})
            if logical != "bgp"
        }
    )
    for logical in logical_networks:
        node_ips = network_node_ips(data, logical)
        vips = local_ping_vips(data, logical)
        for vip_name, vip_ip in vips:
            for source_node, source_ip in node_ips:
                source_mgmt = node_mgmt_ip(data, source_node)
                result = ping(client, source_mgmt, source_ip, vip_ip, timeout)
                result.name = f"{label}.{logical}.{source_node}->{vip_name}"
                results.append(result)
                if progress:
                    progress.step(result.name)
    return results


def check_geo_between_clusters(
    left_label: str,
    left: dict,
    right_label: str,
    right: dict,
    logical: str,
    client: paramiko.SSHClient,
    timeout: int,
    progress: ProgressBar | None = None,
) -> list[CheckResult]:
    results = []
    left_sources = network_node_ips(left, logical)
    right_targets = network_node_ips(right, logical) + [
        (f"{logical}-{vip_name}", vip_ip) for vip_name, vip_ip in network_vips(right, logical)
    ]
    for source_node, source_ip in left_sources:
        source_mgmt = node_mgmt_ip(left, source_node)
        for target_name, target_ip in right_targets:
            result = ping(client, source_mgmt, source_ip, target_ip, timeout)
            result.name = f"{left_label}->{right_label}.{logical}.{source_node}->{target_name}"
            results.append(result)
            if progress:
                progress.step(result.name)
    return results


def check_bgp_peer_reachability(
    label: str,
    data: dict,
    client: paramiko.SSHClient,
    timeout: int,
    progress: ProgressBar | None = None,
) -> list[CheckResult]:
    results = []
    cp = data.get("cnbng_cp", {})
    sessions = cp.get("bgp", {}).get("sessions", {})
    legacy_peers = cp.get("bgp", {}).get("peers", {})

    for node_name in NODE_NAMES:
        node_sessions = sessions.get(node_name, {})
        node_bgps = cp.get(node_name, {}).get("networks", {}).get("bgp", {})
        if not node_sessions and legacy_peers:
            node_sessions = legacy_peers
        if not node_sessions:
            continue

        source_mgmt = node_mgmt_ip(data, node_name)
        for peer_name, session in sorted(node_sessions.items()):
            bgp_interface_name = BGP_PATH_TO_INTERFACE.get(peer_name)
            local_bgp = node_bgps.get(bgp_interface_name, {}) if bgp_interface_name else {}
            local_ip = local_bgp.get("ip")
            peer_ip = session.get("peer_ip")
            result_name = f"{label}.bgp.{node_name}.{peer_name}"

            if not local_ip:
                results.append(CheckResult(result_name, False, f"missing local BGP source IP for {bgp_interface_name or peer_name}"))
                if progress:
                    progress.step(result_name)
                continue
            if not peer_ip:
                results.append(CheckResult(result_name, False, "missing BGP peer/gateway IP"))
                if progress:
                    progress.step(result_name)
                continue

            result = ping(client, source_mgmt, local_ip, peer_ip, timeout)
            result.name = result_name
            results.append(result)
            if progress:
                progress.step(result.name)

    return results


def result_counts(results: list[CheckResult]) -> tuple[int, int, int]:
    passed = sum(1 for result in results if result.ok)
    failed = len(results) - passed
    return len(results), passed, failed


def logical_from_result(section_title: str, result: CheckResult) -> str:
    if section_title == "Node Interface Check":
        if result.detail.startswith("missing interface for "):
            return result.detail.removeprefix("missing interface for ").split()[0]
        return result.detail.split()[0] if result.detail else "unknown"

    if section_title in {"Intra-Cluster Ping Check", "Local VIP Ping Check"}:
        match = re.match(r"^[^.]+\.(?P<network>[^.]+)\.", result.name)
        if match:
            return match.group("network")

    if section_title in {"Geo IntTCP Ping Check", "Geo CDL Ping Check"}:
        match = re.match(r"^[^.]+\.(?P<network>[^.]+)\.", result.name)
        if match:
            return match.group("network")

    if section_title == "BGP Peer Reachability Check":
        return "bgp"

    return "unknown"


def network_sort_key(name: str) -> tuple[int, str]:
    base_name = name.split(".", 1)[0]
    if base_name in NETWORK_ORDER:
        return NETWORK_ORDER.index(base_name), name
    return len(NETWORK_ORDER), name


def result_counts_by_network(section: CheckSection) -> list[tuple[str, int, int, int]]:
    grouped: dict[str, list[CheckResult]] = {}
    for result in section.results:
        grouped.setdefault(logical_from_result(section.title, result), []).append(result)

    rows = []
    for logical, results in grouped.items():
        total, passed, failed = result_counts(results)
        rows.append((logical, total, passed, failed))
    return sorted(rows, key=lambda row: network_sort_key(row[0]))


def print_check_summary(sections: list[CheckSection]) -> None:
    print("\nCheck Summary")
    print("-------------")
    for section in sections:
        total, passed, failed = result_counts(section.results)
        status = "PASS" if failed == 0 else "FAIL"
        print(f"{status:4} {section.title:28} {passed:3}/{total:<3} passed, {failed:3} failed")
        for logical, logical_total, logical_passed, logical_failed in result_counts_by_network(section):
            logical_status = "OK" if logical_failed == 0 else "FAIL"
            print(
                f"      {logical_status:4} {logical:8} "
                f"{logical_passed:3}/{logical_total:<3} passed, {logical_failed:3} failed"
            )


def action_hint(section_title: str, result: CheckResult) -> str:
    if section_title == "Node Interface Check":
        return "Verify Day0 netplan, VLAN ID, parent bond/interface, and physical link for this node."
    if section_title == "Intra-Cluster Ping Check":
        return "Check same-cluster VLAN reachability, subnet mask, source interface, and local switching path."
    if section_title == "Local VIP Ping Check":
        return "Check VIP ownership/VRRP, keepalived status, service binding, and whether the VIP belongs on this node set."
    if section_title in {"Geo IntTCP Ping Check", "Geo CDL Ping Check"}:
        return "Check inter-cluster route, gateway, ACL/firewall, and L3 path for the named geo network."
    if section_title == "BGP Peer Reachability Check":
        return "Check proto server BGP VLAN/subnet, leaf gateway IP, source interface, switchport VLAN, and ACL/firewall path."
    return "Review the failed check and related node/network configuration."


def failure_group_key(section_title: str, result: CheckResult) -> str:
    if section_title in {"Geo IntTCP Ping Check", "Geo CDL Ping Check"}:
        match = re.match(r"^(?P<direction>[^.]+)\.(?P<network>[^.]+)\.", result.name)
        if match:
            return f"{match.group('direction')} {match.group('network')}"
    if section_title in {"Intra-Cluster Ping Check", "Local VIP Ping Check"}:
        match = re.match(r"^(?P<cluster>[^.]+)\.(?P<network>[^.]+)\.", result.name)
        if match:
            return f"{match.group('cluster')} {match.group('network')}"
    if section_title == "BGP Peer Reachability Check":
        match = re.match(r"^(?P<cluster>[^.]+)\.bgp\.(?P<node>[^.]+)\.", result.name)
        if match:
            return f"{match.group('cluster')} bgp {match.group('node')}"
    return result.name


def grouped_failures(section_title: str, results: list[CheckResult]) -> list[tuple[str, list[CheckResult]]]:
    groups: dict[str, list[CheckResult]] = {}
    for result in results:
        if not result.ok:
            groups.setdefault(failure_group_key(section_title, result), []).append(result)
    return list(groups.items())


def print_failure_group(group_name: str, results: list[CheckResult], section_title: str) -> None:
    if len(results) == 1:
        result = results[0]
        print(f"  - {result.name}")
        print(f"    Evidence: {result.detail}")
        print(f"    Next step: {action_hint(section_title, result)}")
        return

    print(f"  - {group_name}: {len(results)} failed checks")
    print(f"    Evidence: {results[0].detail}")
    print(f"    Examples: {', '.join(result.name for result in results[:3])}")
    if len(results) > 3:
        print(f"    Additional failures suppressed: {len(results) - 3}")
    print(f"    Next step: {action_hint(section_title, results[0])}")


def print_action_required(sections: list[CheckSection]) -> None:
    failures = [section for section in sections if any(not result.ok for result in section.results)]
    print("\nAction Required")
    print("---------------")
    if not failures:
        print("No action required by postcheck.")
        return

    for section in failures:
        print(f"\n{section.title}")
        for group_name, results in grouped_failures(section.title, section.results):
            print_failure_group(group_name, results, section.title)


def print_verification_details(sections: list[CheckSection]) -> None:
    print("\nVerification Details")
    print("--------------------")
    for section in sections:
        total, passed, failed = result_counts(section.results)
        if failed == 0:
            print(f"{section.title}: OK ({passed}/{total} checks passed)")
            for logical, logical_total, logical_passed, logical_failed in result_counts_by_network(section):
                print(f"  {logical}: OK ({logical_passed}/{logical_total} checks passed)")
            continue
        print(f"{section.title}: FAILED ({failed}/{total} checks failed)")
        for logical, logical_total, logical_passed, logical_failed in result_counts_by_network(section):
            logical_status = "OK" if logical_failed == 0 else "FAILED"
            print(
                f"  {logical}: {logical_status} "
                f"({logical_passed}/{logical_total} checks passed, {logical_failed} failed)"
            )
        for group_name, results in grouped_failures(section.title, section.results):
            if len(results) == 1:
                result = results[0]
                print(f"  FAIL {result.name}: {result.detail}")
            else:
                print(f"  FAIL {group_name}: {len(results)} failed checks")
                for result in results[:3]:
                    print(f"    example {result.name}: {result.detail}")
                if len(results) > 3:
                    print(f"    ... {len(results) - 3} additional failures suppressed")


def run(args: argparse.Namespace) -> int:
    cp1_xlsx = resolve_input(args.input, "1")
    cp2_xlsx = resolve_input(args.input, "2")

    run_dir = make_run_dir(args.run_id)
    print_report_header(cp1_xlsx, cp2_xlsx, run_dir)
    cp1_yaml = run_dir / "generated" / "cp1.yaml"
    cp2_yaml = run_dir / "generated" / "cp2.yaml"
    try:
        print("\nPreparing postcheck inputs...")
        generate_yaml(cp1_xlsx, cp1_yaml, run_dir / "logs" / "01-generate-cp1-yaml.log", "1")
        generate_yaml(cp2_xlsx, cp2_yaml, run_dir / "logs" / "02-generate-cp2-yaml.log", "2")
        cp1 = load_yaml(cp1_yaml)
        cp2 = load_yaml(cp2_yaml)

        print("Connecting to Inception VM...")
        client = connect_inception(cp1, args.timeout)
        try:
            print("Running postcheck validations...")
            progress = ProgressBar(
                "Node Interface Check",
                count_cluster_interface_operations(cp1) + count_cluster_interface_operations(cp2),
            )
            interface_results = (
                check_cluster_interfaces("CP1", cp1, client, args.timeout, progress)
                + check_cluster_interfaces("CP2", cp2, client, args.timeout, progress)
            )
            progress.done()

            progress = ProgressBar(
                "Intra-Cluster Ping Check",
                count_intra_cluster_checks(cp1) + count_intra_cluster_checks(cp2),
            )
            intra_results = (
                check_intra_cluster("CP1", cp1, client, args.timeout, progress)
                + check_intra_cluster("CP2", cp2, client, args.timeout, progress)
            )
            progress.done()

            progress = ProgressBar(
                "Local VIP Ping Check",
                count_local_vip_checks(cp1) + count_local_vip_checks(cp2),
            )
            local_vip_results = (
                check_local_vips("CP1", cp1, client, args.timeout, progress)
                + check_local_vips("CP2", cp2, client, args.timeout, progress)
            )
            progress.done()

            progress = ProgressBar(
                "BGP Peer Reachability Check",
                count_bgp_peer_checks(cp1) + count_bgp_peer_checks(cp2),
            )
            bgp_peer_results = (
                check_bgp_peer_reachability("CP1", cp1, client, args.timeout, progress)
                + check_bgp_peer_reachability("CP2", cp2, client, args.timeout, progress)
            )
            progress.done()

            progress = ProgressBar(
                "Geo IntTCP Ping Check",
                count_geo_checks(cp1, cp2, "inttcp") + count_geo_checks(cp2, cp1, "inttcp"),
            )
            geo_inttcp_results = (
                check_geo_between_clusters("CP1", cp1, "CP2", cp2, "inttcp", client, args.timeout, progress)
                + check_geo_between_clusters("CP2", cp2, "CP1", cp1, "inttcp", client, args.timeout, progress)
            )
            progress.done()

            progress = ProgressBar(
                "Geo CDL Ping Check",
                count_geo_checks(cp1, cp2, "cdl") + count_geo_checks(cp2, cp1, "cdl"),
            )
            geo_cdl_results = (
                check_geo_between_clusters("CP1", cp1, "CP2", cp2, "cdl", client, args.timeout, progress)
                + check_geo_between_clusters("CP2", cp2, "CP1", cp1, "cdl", client, args.timeout, progress)
            )
            progress.done()
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"Run artifacts: {run_dir}", file=sys.stderr)
        return 1

    print_network_coverage("CP1", cp1)
    print_network_coverage("CP2", cp2)

    sections = [
        CheckSection("Node Interface Check", interface_results),
        CheckSection("Intra-Cluster Ping Check", intra_results),
        CheckSection("Local VIP Ping Check", local_vip_results),
        CheckSection("BGP Peer Reachability Check", bgp_peer_results),
        CheckSection("Geo IntTCP Ping Check", geo_inttcp_results),
        CheckSection("Geo CDL Ping Check", geo_cdl_results),
    ]
    print_check_summary(sections)
    print_action_required(sections)
    print_verification_details(sections)

    all_results = [result for section in sections for result in section.results]
    failed = [result for result in all_results if not result.ok]
    print()
    if failed:
        print(f"Result: FAIL - {len(failed)} issue(s) require action.")
        return 1
    print("Result: PASS - all postcheck checks passed.")
    return 0


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("input", help="Deployment XLSX path or profile name, for example site.")
    parser.add_argument("--timeout", type=int, default=20, help=argparse.SUPPRESS)
    parser.add_argument("--run-id", help=argparse.SUPPRESS)
