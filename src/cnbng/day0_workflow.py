# __________________________________________________
#
# Cloud Native BNG / Cisco Subscriber Edge
# Control Plane Deployment Agent
#
# Author: Gurpreet Dhaliwal, TME MiG
# __________________________________________________

"""Minimal Day0 deployment workflow for CNBNG."""

from __future__ import annotations

import argparse
import ipaddress
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import paramiko
import yaml

from cnbng import __version__


def release_root() -> Path:
    return Path(__file__).resolve().parents[2]


def make_run_dir(base_dir: Path | None = None, run_id: str | None = None) -> Path:
    base = base_dir or release_root() / "runs"
    run_name = run_id or "cnbng-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    run_dir = base / run_name
    for child in ("input", "generated", "logs"):
        (run_dir / child).mkdir(parents=True, exist_ok=True)
    return run_dir


def run_command(command: list[str], log_file: Path, *, verbose: bool = False) -> int:
    with log_file.open("w") as log:
        process = subprocess.Popen(
            command,
            cwd=str(release_root()),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            if verbose:
                print(line, end="")
            log.write(line)
        return process.wait()


def print_log_tail(log_file: Path, lines: int = 20) -> None:
    if not log_file.exists():
        return
    content = log_file.read_text(errors="replace").splitlines()
    print(f"\nLast {min(lines, len(content))} line(s) from {log_file}:")
    for line in content[-lines:]:
        print(line)


def copy_if_exists(source: Path, dest: Path) -> None:
    if source.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)


def load_yaml(path: Path) -> dict:
    with path.open() as handle:
        return yaml.safe_load(handle) or {}


def smi_deployer_ip(data: dict) -> str | None:
    value = data.get("smi_deployer", {}).get("ip")
    return str(value) if value else None


def cluster_name(data: dict) -> str:
    return str(data.get("cnbng_cp", {}).get("cluster", {}).get("name", ""))


def normalize_ip(value: object):
    if value is None or value == "":
        return None
    try:
        return ipaddress.ip_address(str(value).split("/", 1)[0])
    except ValueError:
        return None


def normalize_network(value: object):
    if value is None or value == "":
        return None
    try:
        return ipaddress.ip_interface(str(value)).network
    except ValueError:
        try:
            return ipaddress.ip_network(str(value), strict=False)
        except ValueError:
            return None


def add_owned_ip(items: list[tuple[object, str]], value: object, context: str) -> None:
    address = normalize_ip(value)
    if address:
        items.append((address, context))


def add_owned_interface(
    addresses: list[tuple[object, str]],
    networks: list[tuple[object, str, str]],
    value: object,
    context: str,
    logical_network: str,
) -> None:
    address = normalize_ip(value)
    network = normalize_network(value)
    if address:
        addresses.append((address, context))
    if network:
        networks.append((network, context, logical_network))


def collect_owned_inventory(data: dict) -> tuple[list[tuple[object, str]], list[tuple[object, str, str]]]:
    cp = data.get("cnbng_cp", {})
    cluster = cp.get("cluster", {})
    name = cluster_name(data)
    addresses: list[tuple[object, str]] = []
    networks: list[tuple[object, str, str]] = []

    master = cluster.get("master", {})
    add_owned_ip(addresses, master.get("vip1"), f"{name}.cluster.master.vip1")
    add_owned_ip(addresses, master.get("vip2"), f"{name}.cluster.master.vip2")

    for net_name, body in cluster.get("networks", {}).items():
        for key, value in body.items():
            if key.startswith("vip"):
                if net_name == "n4":
                    continue
                add_owned_ip(addresses, value, f"{name}.cluster.networks.{net_name}.{key}")

    for node_name in ("ucs01", "ucs02", "ucs03"):
        node = cp.get(node_name, {})
        add_owned_ip(addresses, node.get("cimc", {}).get("ip"), f"{name}.{node_name}.cimc.ip")
        for net_name, body in node.get("networks", {}).items():
            if net_name == "bgp":
                for bgp_name, bgp in body.items():
                    add_owned_interface(
                        addresses,
                        networks,
                        bgp.get("ip"),
                        f"{name}.{node_name}.bgp.{bgp_name}.ip",
                        f"bgp.{bgp_name}",
                    )
                continue
            if isinstance(body, dict) and body.get("ip"):
                add_owned_interface(
                    addresses,
                    networks,
                    body.get("ip"),
                    f"{name}.{node_name}.{net_name}.ip",
                    net_name,
                )

    return addresses, networks


def summarize_contexts(contexts: list[str]) -> str:
    if len(contexts) <= 3:
        return ", ".join(sorted(contexts))
    sorted_contexts = sorted(contexts)
    return ", ".join(sorted_contexts[:3]) + f", and {len(sorted_contexts) - 3} more"


def context_label(context: str) -> str:
    if context.endswith(".cluster.master.vip1"):
        return "k8s VIP"
    if context.endswith(".cluster.master.vip2"):
        return "management VIP"
    if ".cluster.networks." in context and ".vip" in context:
        parts = context.split(".cluster.networks.", 1)[1].split(".")
        if len(parts) >= 2:
            return f"{parts[0]} {parts[1].upper()}"
    if ".bgp." in context and context.endswith(".ip"):
        parts = context.split(".")
        if len(parts) >= 4:
            return f"{parts[-4]} {parts[-3]} local IP"
    if context.endswith(".cimc.ip"):
        return context.split(".")[-3] + " CIMC IP"
    if context.endswith(".ip"):
        parts = context.split(".")
        if len(parts) >= 3:
            return f"{parts[-3]} {parts[-2]} node IP"
    return context


def duplicate_ip_hint(contexts: list[str]) -> str:
    labels = {context_label(context) for context in contexts}
    if labels == {"management VIP"}:
        return "Assign a different management VIP per cluster."
    if any("BGP" in label or "bgp" in label for label in labels):
        return "Assign unique BGP local IPs per proto node and per cluster."
    if any("k8s" in label for label in labels):
        return "Assign unique k8s node/VIP addresses per cluster."
    if any("CIMC" in label for label in labels):
        return "Each UCS CIMC address must be unique."
    return "Use a unique address for each cluster-owned endpoint."


def n4_vip_set(data: dict) -> set[object]:
    n4 = data.get("cnbng_cp", {}).get("cluster", {}).get("networks", {}).get("n4", {})
    result = set()
    for key, value in n4.items():
        if key.startswith("vip"):
            address = normalize_ip(value)
            if address:
                result.add(address)
    return result


def subnet_overlap_hint(logical_network: str) -> str:
    if logical_network == "k8s":
        return "This is allowed when both clusters share the same reachable k8s L2/VLAN and all node/VIP IPs are unique."
    if logical_network.startswith("bgp."):
        return "Use a different BGP transit subnet, or verify this is intentionally the same L2 leaf segment."
    if logical_network in {"inttcp", "cdl"}:
        return "Geo-redundancy networks should be reachable between clusters but should not reuse the same subnet unless they are intentionally one stretched L2 domain."
    if logical_network == "n4":
        return "N4 service VIPs must be the same across the geo pair; node subnets should be unique unless the design intentionally stretches the N4 VLAN."
    return "Use a non-overlapping subnet per cluster for this logical network."


def compare_peer_cluster(current_yaml: Path, peer_yaml: Path) -> list[str]:
    current = load_yaml(current_yaml)
    peer = load_yaml(peer_yaml)
    errors: list[str] = []

    if cluster_name(current) and cluster_name(current) == cluster_name(peer):
        errors.append(
            f"Cluster name {cluster_name(current)!r} is reused by {current_yaml.name} and {peer_yaml.name}"
        )

    current_n4_vips = n4_vip_set(current)
    peer_n4_vips = n4_vip_set(peer)
    if current_n4_vips and peer_n4_vips and current_n4_vips != peer_n4_vips:
        errors.append(
            "N4 VIPs must match across the geo-redundant cluster pair. "
            f"{current_yaml.name} has {', '.join(str(ip) for ip in sorted(current_n4_vips))}; "
            f"{peer_yaml.name} has {', '.join(str(ip) for ip in sorted(peer_n4_vips))}."
        )

    current_addresses, current_networks = collect_owned_inventory(current)
    peer_addresses, peer_networks = collect_owned_inventory(peer)
    peer_by_ip: dict[object, list[str]] = {}
    for address, context in peer_addresses:
        peer_by_ip.setdefault(address, []).append(context)

    for address, current_context in current_addresses:
        peer_contexts = peer_by_ip.get(address, [])
        if peer_contexts:
            all_contexts = [current_context, *peer_contexts]
            errors.append(
                f"Duplicate {context_label(current_context)} {address}: "
                f"{current_context} also appears as {', '.join(context_label(context) for context in sorted(peer_contexts))}. "
                f"{duplicate_ip_hint(all_contexts)}"
            )

    current_by_logical_network: dict[tuple[str, object], list[str]] = {}
    peer_by_logical_network: dict[tuple[str, object], list[str]] = {}
    for network, context, logical in current_networks:
        if logical in {"mgmt", "k8s"}:
            continue
        current_by_logical_network.setdefault((logical, network), []).append(context)
    for network, context, logical in peer_networks:
        if logical in {"mgmt", "k8s"}:
            continue
        peer_by_logical_network.setdefault((logical, network), []).append(context)

    reported_overlaps: set[tuple[str, object, object]] = set()
    for (current_logical, current_network), current_contexts in current_by_logical_network.items():
        for (peer_logical, peer_network), peer_contexts in peer_by_logical_network.items():
            if current_logical != peer_logical or current_network.version != peer_network.version:
                continue
            if current_network.overlaps(peer_network):
                key = (current_logical, current_network, peer_network)
                if key in reported_overlaps:
                    continue
                reported_overlaps.add(key)
                errors.append(
                    f"Overlapping {current_logical} subnet: current cluster uses {current_network} "
                    f"({summarize_contexts([context_label(context) for context in current_contexts])}); "
                    f"peer cluster uses {peer_network} "
                    f"({summarize_contexts([context_label(context) for context in peer_contexts])}). "
                    f"{subnet_overlap_hint(current_logical)}"
                )

    return errors


def load_day0_lib():
    from cnbng import day0_lib  # pylint: disable=import-outside-toplevel

    return day0_lib


def generate_yaml(args: argparse.Namespace, run_dir: Path) -> Path:
    output_yaml = run_dir / "generated" / "day0_3s.yaml"
    copy_if_exists(Path(args.input), run_dir / "input" / Path(args.input).name)
    print("Generating YAML from XLSX...")
    log_file = run_dir / "logs" / "01-generate-yaml.log"
    command = [
        sys.executable,
        "src/cnbng/xlsx_to_yaml.py",
        args.input,
        "--output",
        str(output_yaml),
    ]
    rc = run_command(
        command,
        log_file,
        verbose=args.verbose,
    )
    if rc != 0:
        print_log_tail(log_file)
        raise RuntimeError(f"YAML generation failed; see {log_file}")
    print("YAML generated.")
    return output_yaml


def workbook_profile_key(path: Path) -> str:
    stem = path.stem.lower()
    marker = "_cnbng_3server_deployment"
    if marker in stem:
        return stem.split(marker, 1)[0]
    for suffix in ("_cp1", "_cp2", "_cluster1", "_cluster2", "_cluster_cp1", "_cluster_cp2"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def peer_workbook_candidates(input_path: Path) -> list[Path]:
    if not input_path.exists() or input_path.suffix.lower() != ".xlsx":
        return []
    profile_key = workbook_profile_key(input_path)
    return sorted(
        candidate
        for candidate in input_path.parent.glob("*.xlsx")
        if candidate.resolve() != input_path.resolve() and not candidate.name.startswith("~$")
        and workbook_profile_key(candidate) == profile_key
    )


def generate_peer_yaml(
    candidate: Path,
    run_dir: Path,
    index: int,
    verbose: bool,
    suffix: str | None = None,
) -> Path:
    output_suffix = suffix or candidate.stem
    output_yaml = run_dir / "generated" / f"peer-{index}-{output_suffix}.yaml"
    log_file = run_dir / "logs" / f"02-peer-{index}-{output_suffix}.log"
    command = [
        sys.executable,
        "src/cnbng/xlsx_to_yaml.py",
        str(candidate),
        "--output",
        str(output_yaml),
    ]
    rc = run_command(
        command,
        log_file,
        verbose=verbose,
    )
    if rc != 0:
        print_log_tail(log_file, lines=5)
        raise RuntimeError(f"Peer workbook {candidate.name} could not be parsed; see {log_file}")
    return output_yaml


def matching_peer_yamls(args: argparse.Namespace, run_dir: Path, yaml_path: Path) -> list[tuple[Path, Path]]:
    input_path = Path(args.input)
    current_data = load_yaml(yaml_path)
    current_smi_ip = smi_deployer_ip(current_data)
    if not current_smi_ip:
        return []

    matches: list[tuple[Path, Path]] = []
    for index, candidate in enumerate(peer_workbook_candidates(input_path), start=1):
        peer_yaml = generate_peer_yaml(candidate, run_dir, index, args.verbose)
        peer_data = load_yaml(peer_yaml)
        if smi_deployer_ip(peer_data) == current_smi_ip:
            matches.append((candidate, peer_yaml))
    return matches


def preflight_peer_workbooks(args: argparse.Namespace, run_dir: Path, yaml_path: Path) -> None:
    input_path = Path(args.input)
    matched: list[str] = []
    peer_errors: list[tuple[Path, list[str]]] = []
    for candidate, peer_yaml in matching_peer_yamls(args, run_dir, yaml_path):
        matched.append(candidate.name)
        candidate_errors = compare_peer_cluster(yaml_path, peer_yaml)
        if candidate_errors:
            peer_errors.append((candidate, candidate_errors))

    if peer_errors:
        print("\nPeer workbook conflict check failed:")
        print(f"Current workbook: {input_path}")
        for candidate, candidate_errors in peer_errors:
            print(f"Peer workbook: {candidate}")
            for error in candidate_errors:
                print(f"ERROR: {error}")
        raise RuntimeError("Peer workbook conflict check failed")

    if matched:
        print(f"Peer workbook check passed for same SMI deployer: {', '.join(matched)}")


def preflight(args: argparse.Namespace, run_dir: Path, yaml_path: Path) -> None:
    if args.skip_preflight:
        print("Preflight skipped.")
        return
    print("Running preflight...")
    log_file = run_dir / "logs" / "02-preflight.log"
    peer_yamls = matching_peer_yamls(args, run_dir, yaml_path)
    command = [sys.executable, "src/cnbng/preflight.py", str(yaml_path)]
    for _, peer_yaml in peer_yamls:
        command.extend(["--peer-yaml", str(peer_yaml)])
    rc = run_command(
        command,
        log_file,
        verbose=args.verbose,
    )
    if rc != 0:
        print_log_tail(log_file)
        raise RuntimeError(f"Preflight failed; see {log_file}")
    matched: list[str] = []
    peer_errors: list[tuple[Path, list[str]]] = []
    for candidate, peer_yaml in peer_yamls:
        matched.append(candidate.name)
        candidate_errors = compare_peer_cluster(yaml_path, peer_yaml)
        if candidate_errors:
            peer_errors.append((candidate, candidate_errors))

    if peer_errors:
        print("\nPeer workbook conflict check failed:")
        print(f"Current workbook: {Path(args.input)}")
        for candidate, candidate_errors in peer_errors:
            print(f"Peer workbook: {candidate}")
            for error in candidate_errors:
                print(f"ERROR: {error}")
        raise RuntimeError("Peer workbook conflict check failed")

    if matched:
        print(f"Peer workbook check passed for same SMI deployer: {', '.join(matched)}")
    print("Local validation passed.")


def inception_access_check(args: argparse.Namespace, run_dir: Path, yaml_path: Path) -> None:
    print("Checking Inception VM routes to UCS mgmt/k8s networks and access to CIMC...")
    log_file = run_dir / "logs" / "03-inception-access-check.log"
    rc = run_command(
        [
            sys.executable,
            "src/cnbng/preflight.py",
            str(yaml_path),
            "--check-inception-reachability",
        ],
        log_file,
        verbose=args.verbose,
    )
    if rc != 0:
        print_log_tail(log_file)
        raise RuntimeError(
            "Inception VM access check failed. Fix routes to UCS mgmt/k8s targets and route/TCP reachability to CIMC before deployment."
        )
    print("Inception VM access check passed.")


def push_day0(args: argparse.Namespace, run_dir: Path, yaml_path: Path) -> None:
    if args.dry_run:
        print("Check completed. Day0 config was not pushed.")
        return
    print("Pushing Day0 config to SMI deployer...")
    day0_lib = load_day0_lib()
    data = load_yaml(yaml_path)
    environment, deployment_type = day0_lib.getDeploymentEnvironmentType(data)
    if environment != "baremetal" or deployment_type != "3server_geo-red":
        raise RuntimeError(f"Day0 step1 supports only baremetal 3server_geo-red, got {environment} {deployment_type}")
    day0_lib.deploy_cndp_3server(data)
    copy_if_exists(
        release_root() / "config" / "cluster-config_cndp_3server_geo-red.xml",
        run_dir / "generated" / "cluster-config_cndp_3server_geo-red.xml",
    )
    print("Day0 config pushed.")

def push_day0_step2(args: argparse.Namespace, run_dir: Path, yaml_path: Path) -> None:
    print("Applying Day0 step2 config to BNG Ops Center...")
    day0_lib = load_day0_lib()
    data = load_yaml(yaml_path)
    environment, deployment_type = day0_lib.getDeploymentEnvironmentType(data)
    if environment != "baremetal" or deployment_type != "3server_geo-red":
        raise RuntimeError(f"Day0 step2 supports only baremetal 3server_geo-red, got {environment} {deployment_type}")
    day0_lib.init_cndp_3server(data, dry_run=args.dry_run)
    copy_if_exists(
        release_root() / "config" / "bng-ops-center_step2-config_cndp_3server_geo-red.xml",
        run_dir / "generated" / "bng-ops-center_step2-config_cndp_3server_geo-red.xml",
    )
    if args.dry_run:
        print("Day0 step2 config rendered. No Ops Center changes were applied.")
        return
    print("Day0 step2 config applied.")


def read_shell(shell, timeout: float) -> str:
    deadline = time.time() + timeout
    chunks: list[str] = []
    while time.time() < deadline:
        if shell.recv_ready():
            chunks.append(shell.recv(65535).decode(errors="replace"))
            deadline = time.time() + 0.1
        else:
            time.sleep(0.1)
    return "".join(chunks)


def monitor_targets(yaml_path: Path, args: argparse.Namespace) -> list[dict]:
    cluster = load_yaml(yaml_path)
    smi = cluster["smi_deployer"]
    return [
        {
            "host": args.watch_smi_host or smi["ip"],
            "user": args.watch_smi_user or smi.get("user", "admin"),
            "password": args.watch_smi_password or smi.get("password"),
            "port": args.watch_smi_port,
            "cluster_name": cluster["cnbng_cp"]["cluster"]["name"],
        }
    ]


def stream_monitor(target: dict, args: argparse.Namespace, log_file: Path) -> str:
    command = f"monitor sync-logs {target['cluster_name']}"
    print(f"Monitoring {target['cluster_name']}...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            target["host"],
            port=target["port"],
            username=target["user"],
            password=target["password"],
            look_for_keys=False,
            allow_agent=False,
            timeout=args.watch_connect_timeout,
            banner_timeout=args.watch_connect_timeout,
            auth_timeout=args.watch_connect_timeout,
        )
        shell = client.invoke_shell(width=240, height=1000)
        with log_file.open("w") as log:
            banner = read_shell(shell, 2.0)
            if banner:
                log.write(banner)
            shell.send(command + "\n")
            deadline = time.time() + args.watch_timeout
            while time.time() < deadline:
                output = read_shell(shell, args.watch_poll_interval)
                if not output:
                    continue
                log.write(output)
                log.flush()
                print(output, end="")
                lowered = output.lower()
                if "sync complete" in lowered or "completed successfully" in lowered:
                    return "success"
                if "fatal:" in lowered or "failed!" in lowered or "no more hosts left" in lowered:
                    return "failure"
            return "timeout"
    finally:
        client.close()


def watch_generated(args: argparse.Namespace, run_dir: Path, yaml_path: Path) -> None:
    if args.dry_run or args.no_watch:
        return
    for index, target in enumerate(monitor_targets(yaml_path, args), start=1):
        log_file = run_dir / "logs" / f"04-watch-{index}-{target['cluster_name']}.log"
        status = stream_monitor(target, args, log_file)
        if status == "success":
            print(f"Deployment completed for {target['cluster_name']}.")
            continue
        if status == "failure":
            raise RuntimeError(f"Deployment failed for {target['cluster_name']}; see {log_file}")
        raise RuntimeError(f"Deployment watch timed out for {target['cluster_name']}; see {log_file}")


def deploy(args: argparse.Namespace) -> int:
    if args.deployment != "3s":
        print(f"ERROR: v{__version__} supports deployment 3s only", file=sys.stderr)
        return 2
    run_dir = make_run_dir(Path(args.run_base) if args.run_base else None, args.run_id)
    try:
        yaml_path = generate_yaml(args, run_dir)
        preflight(args, run_dir, yaml_path)
        inception_access_check(args, run_dir, yaml_path)
        print("Preflight passed.")
        push_day0(args, run_dir, yaml_path)
        watch_generated(args, run_dir, yaml_path)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"Run artifacts: {run_dir}", file=sys.stderr)
        return 1
    return 0

def step2(args: argparse.Namespace) -> int:
    if args.deployment != "3s":
        print(f"ERROR: v{__version__} supports deployment 3s only", file=sys.stderr)
        return 2
    run_dir = make_run_dir(Path(args.run_base) if args.run_base else None, args.run_id)
    try:
        yaml_path = generate_yaml(args, run_dir)
        preflight(args, run_dir, yaml_path)
        push_day0_step2(args, run_dir, yaml_path)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"Run artifacts: {run_dir}", file=sys.stderr)
        return 1
    return 0


def watch(args: argparse.Namespace) -> int:
    if args.deployment != "3s":
        print(f"ERROR: v{__version__} supports deployment 3s only", file=sys.stderr)
        return 2
    run_dir = make_run_dir(Path(args.run_base) if args.run_base else None, args.run_id)
    try:
        yaml_path = generate_yaml(args, run_dir)
        for index, target in enumerate(monitor_targets(yaml_path, args), start=1):
            log_file = run_dir / "logs" / f"01-watch-{index}-{target['cluster_name']}.log"
            status = stream_monitor(target, args, log_file)
            if status == "success":
                print(f"Deployment completed for {target['cluster_name']}.")
                continue
            if status == "failure":
                raise RuntimeError(f"Deployment failed for {target['cluster_name']}; see {log_file}")
            raise RuntimeError(f"Deployment watch timed out for {target['cluster_name']}; see {log_file}")
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"Run artifacts: {run_dir}", file=sys.stderr)
        return 1
    return 0


def add_watch_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--no-watch", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--watch-timeout", type=int, default=7200, help=argparse.SUPPRESS)
    parser.add_argument("--watch-poll-interval", type=float, default=2.0, help=argparse.SUPPRESS)
    parser.add_argument("--watch-connect-timeout", type=int, default=15, help=argparse.SUPPRESS)
    parser.add_argument("--watch-smi-host", help=argparse.SUPPRESS)
    parser.add_argument("--watch-smi-user", help=argparse.SUPPRESS)
    parser.add_argument("--watch-smi-password", help=argparse.SUPPRESS)
    parser.add_argument("--watch-smi-port", type=int, default=2022, help=argparse.SUPPRESS)


def add_deploy_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--deployment", required=True, choices=["3s"], help="Deployment model.")
    parser.add_argument("--input", required=True, help="Deployment XLSX input.")
    parser.add_argument("--run-id", help=argparse.SUPPRESS)
    parser.add_argument("--run-base", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skip-preflight", action="store_true", help=argparse.SUPPRESS)
    add_watch_options(parser)


def add_watch_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--deployment", required=True, choices=["3s"], help="Deployment model.")
    parser.add_argument("--input", required=True, help="Deployment XLSX input.")
    parser.add_argument("--run-id", help=argparse.SUPPRESS)
    parser.add_argument("--run-base", help=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true", help=argparse.SUPPRESS)
    add_watch_options(parser)


def default_smart_trigger_markers() -> tuple[str, ...]:
    return ()
