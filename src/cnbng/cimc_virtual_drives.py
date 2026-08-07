#!/usr/bin/env python3
# __________________________________________________
#
# Cloud Native BNG / Cisco Subscriber Edge
# Control Plane Deployment Agent
#
# Author: Gurpreet Dhaliwal, TME MiG
# __________________________________________________

"""
Clean SMI-created CIMC storage virtual drives.

This is destructive. By default the script only lists storage adapters and
virtual drives. To delete the SMI-created install disk, use:

  --cleanup-virtual-drive --apply --i-understand-data-loss

Boot virtual drives are selected only by the cleanup mode used by CNBNG.
"""

import argparse
import re
import sys
import time
from pathlib import Path

import paramiko
import yaml
from yaml.loader import SafeLoader


NODE_NAMES = ("ucs01", "ucs02", "ucs03")
COMMON_STORAGE_ADAPTERS = ("MRAID",)


def load_yaml(path):
    with open(path, "r") as handle:
        return yaml.load(handle, Loader=SafeLoader)


def collect_targets(data):
    cp = data.get("cnbng_cp", {})
    targets = []

    for node_name in NODE_NAMES:
        node = cp.get(node_name)
        if not node or "cimc" not in node:
            continue
        cimc = node["cimc"]
        targets.append({
            "name": node_name,
            "host": cimc["ip"],
            "user": cimc.get("user", "admin"),
            "password": cimc.get("password"),
        })

    if "clusters" in cp:
        defaults = data.get("cimc", {})
        for cluster in cp["clusters"]:
            cimc = cluster.get("cimc")
            if not cimc:
                continue
            targets.append({
                "name": cluster.get("site", cluster.get("cluster", {}).get("name", "ucs")),
                "host": cimc["ip"],
                "user": cimc.get("user", defaults.get("user", "admin")),
                "password": cimc.get("password", defaults.get("password")),
            })

    if not targets and "cimc" in data and "ip" in data["cimc"]:
        cimc = data["cimc"]
        targets.append({
            "name": cp.get("cluster", {}).get("name", "ucs"),
            "host": cimc["ip"],
            "user": cimc.get("user", "admin"),
            "password": cimc.get("password"),
        })

    return targets


def read_available(shell, idle_seconds=0.5, timeout=8):
    end = time.time() + timeout
    last_data = time.time()
    chunks = []

    while time.time() < end:
        if shell.recv_ready():
            chunks.append(shell.recv(65535).decode("utf-8", errors="replace"))
            last_data = time.time()
        elif chunks and time.time() - last_data >= idle_seconds:
            break
        else:
            time.sleep(0.1)

    return "".join(chunks)


def run_command(shell, command, timeout=8):
    shell.send(command + "\n")
    time.sleep(0.8)
    return read_available(shell, timeout=timeout)


def run_confirmed_command(shell, command, confirmation="yes", timeout=8):
    shell.send(command + "\n")
    time.sleep(0.8)
    first = read_available(shell, idle_seconds=0.3, timeout=timeout)
    shell.send(confirmation + "\n")
    time.sleep(0.8)
    second = read_available(shell, timeout=timeout)
    return first + second


def connect_cimc(target, timeout):
    if not target.get("password"):
        raise RuntimeError(f"{target['name']}: missing CIMC password")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        target["host"],
        username=target["user"],
        password=target["password"],
        look_for_keys=False,
        allow_agent=False,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
    )
    return client


def parse_storage_adapters(output):
    adapters = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or is_prompt_or_command(stripped):
            continue
        lowered = stripped.lower()
        if lowered.startswith(("pci slot", "------------")):
            continue
        columns = re.split(r"\s{2,}", stripped)
        if len(columns) >= 3:
            adapters.append(columns[0])
    return unique(adapters)


def parse_virtual_drives_detail(output, adapter):
    drives = []
    current = None

    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or is_prompt_or_command(stripped):
            continue

        header = re.match(r"^Virtual Drive\s+(.+?):\s*$", stripped, flags=re.IGNORECASE)
        if header:
            current = {"adapter": adapter, "id": header.group(1).strip()}
            drives.append(current)
            continue

        if current:
            kv = re.match(r"^([^:]+):\s*(.*?)\s*$", stripped)
            if kv:
                current[normalize_key(kv.group(1))] = kv.group(2).strip()

    if drives:
        return drives

    return parse_virtual_drives_table(output, adapter)


def parse_virtual_drives_table(output, adapter):
    drives = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or is_prompt_or_command(stripped):
            continue
        lowered = stripped.lower()
        if lowered.startswith(("virtual drive", "-------------")):
            continue
        columns = re.split(r"\s{2,}", stripped)
        if len(columns) >= 8:
            drives.append({
                "adapter": adapter,
                "id": columns[0],
                "health": columns[1],
                "status": columns[2],
                "name": columns[3],
                "size": columns[4],
                "physical_drives": columns[5],
                "raid_level": columns[6],
                "boot_drive": columns[7],
            })
    return drives


def normalize_key(value):
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def is_prompt_or_command(value):
    if value.startswith(("show ", "scope ", "exit")):
        return True
    return bool(re.search(r"(?:^|\s)/(?:chassis|storageadapter|virtual-drive)\s+#\s*$", value)) or value.endswith("#")


def unique(items):
    result = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result


def is_boot_drive(drive):
    return str(drive.get("boot_drive", "")).strip().lower() == "true"


def drive_summary(drive):
    return (
        f"adapter={drive.get('adapter', 'NA')}, id={drive.get('id', 'NA')}, "
        f"name={drive.get('name', 'NA')}, size={drive.get('size', 'NA')}, "
        f"raid={drive.get('raid_level', 'NA')}, status={drive.get('status', 'NA')}, "
        f"boot={drive.get('boot_drive', 'NA')}, physical_drives={drive.get('physical_drives', 'NA')}"
    )


def inventory_virtual_drives(target, requested_adapters, timeout):
    client = connect_cimc(target, timeout)
    shell = client.invoke_shell(width=240, height=1000)
    read_available(shell, timeout=3)

    try:
        run_command(shell, "scope chassis", timeout=timeout)
        adapters = requested_adapters
        if not adapters:
            adapter_output = run_command(shell, "show storageadapter", timeout=timeout)
            adapters = parse_storage_adapters(adapter_output)
        for adapter in COMMON_STORAGE_ADAPTERS:
            if adapter not in adapters:
                adapters.append(adapter)

        drives = []
        for adapter in adapters:
            scope_output = run_command(shell, f"scope storageadapter {adapter}", timeout=timeout)
            if "% invalid command" in scope_output.lower():
                continue
            detail = run_command(shell, "show virtual-drive detail", timeout=timeout)
            adapter_drives = parse_virtual_drives_detail(detail, adapter)
            if not adapter_drives:
                table = run_command(shell, "show virtual-drive", timeout=timeout)
                adapter_drives = parse_virtual_drives_table(table, adapter)
            drives.extend(adapter_drives)
            run_command(shell, "exit", timeout=timeout)
    finally:
        run_command(shell, "exit", timeout=timeout)
        client.close()

    return drives


def delete_virtual_drives(
    target,
    drives,
    apply,
    include_boot,
    cleanup_virtual_drive,
    requested_drive_ids,
    timeout,
):
    actions = []
    selected, selection_error = select_drives(drives, requested_drive_ids, cleanup_virtual_drive)
    if selection_error:
        return [("ERROR", {}, selection_error)]
    if cleanup_virtual_drive and not selected:
        return [("SKIP", {}, "no bootable virtual drives found; cleanup not required")]

    if not apply:
        for drive in selected:
            if is_boot_drive(drive) and not include_boot and not cleanup_virtual_drive:
                actions.append((
                    "SKIP",
                    drive,
                    "boot/install drive; use CNBNG cleanup to select it",
                ))
            else:
                reason = "would delete selected SMI-created virtual drive" if cleanup_virtual_drive else "would delete"
                actions.append(("DRY-RUN", drive, reason))
        return actions

    client = connect_cimc(target, timeout)
    shell = client.invoke_shell(width=240, height=1000)
    read_available(shell, timeout=3)

    try:
        run_command(shell, "scope chassis", timeout=timeout)
        current_adapter = None
        for drive in selected:
            if is_boot_drive(drive) and not include_boot and not cleanup_virtual_drive:
                actions.append((
                    "SKIP",
                    drive,
                    "boot/install drive; use CNBNG cleanup to delete it",
                ))
                continue

            if current_adapter != drive["adapter"]:
                if current_adapter:
                    run_command(shell, "exit", timeout=timeout)
                run_command(shell, f"scope storageadapter {drive['adapter']}", timeout=timeout)
                current_adapter = drive["adapter"]

            run_command(shell, f"scope virtual-drive {drive['id']}", timeout=timeout)
            output = run_confirmed_command(shell, "delete-virtual-drive", "yes", timeout=timeout)
            actions.append(("DELETE", drive, compact_output(output)))
            run_command(shell, "exit", timeout=timeout)

        if current_adapter:
            run_command(shell, "exit", timeout=timeout)
    finally:
        run_command(shell, "exit", timeout=timeout)
        client.close()

    return actions


def select_drives(drives, requested_drive_ids, cleanup_virtual_drive):
    if requested_drive_ids:
        return [drive for drive in drives if drive["id"] in requested_drive_ids], None

    if not cleanup_virtual_drive:
        return drives, None

    if not drives:
        return [], None

    boot_drives = [drive for drive in drives if is_boot_drive(drive)]
    if len(boot_drives) == 1:
        return boot_drives, None

    if not boot_drives:
        return [], None

    ids = ", ".join(drive["id"] for drive in boot_drives)
    return [], (
        "cleanup found multiple bootable virtual drives; specify one with "
        f"--drive <id>. Bootable IDs: {ids}"
    )


def compact_output(output):
    lines = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped and not is_prompt_or_command(stripped):
            lines.append(stripped)
    return " | ".join(lines) or "command completed"


def print_targets(targets):
    for target in targets:
        print(f"{target['name']}: CIMC {target['host']} user={target['user']}")


def print_inventory(target, drives):
    print(f"\n=== {target['name']} CIMC {target['host']} ===")
    if not drives:
        print("Virtual drives: none")
        return
    print("Virtual drives:")
    for drive in drives:
        print(f"  - {drive_summary(drive)}")


def print_actions(actions):
    if not actions:
        print("Actions: none")
        return
    print("Actions:")
    for action, drive, message in actions:
        if drive:
            print(f"  - {action}: {drive_summary(drive)}; {message}")
        else:
            print(f"  - {action}: {message}")


def main():
    parser = argparse.ArgumentParser(
        description="List or clean SMI-created CIMC storage virtual drives."
    )
    parser.add_argument("yaml_file", help="Deployment YAML containing UCS CIMC details")
    parser.add_argument("--node", action="append", help="Limit to node name; may be repeated")
    parser.add_argument("--adapter", action="append", help="Limit to storage adapter slot, for example MRAID")
    parser.add_argument(
        "--drive",
        action="append",
        help="Limit to virtual drive ID.",
    )
    parser.add_argument("--list-targets", action="store_true", help="List CIMC targets and exit")
    parser.add_argument("--apply", action="store_true", help="Actually delete selected virtual drives")
    parser.add_argument(
        "--cleanup-virtual-drive",
        action="store_true",
        help="Select boot/install virtual drives for CNBNG virtual-drive cleanup",
    )
    parser.add_argument(
        "--include-boot",
        action="store_true",
        help="Generic override to allow deleting boot virtual drives",
    )
    parser.add_argument(
        "--i-understand-data-loss",
        action="store_true",
        help="Required with --apply; confirms that deleting virtual drives wipes data",
    )
    parser.add_argument("--timeout", type=int, default=10, help="CIMC SSH command timeout in seconds")
    args = parser.parse_args()

    if args.apply and not args.i_understand_data_loss:
        print("ERROR: --apply requires --i-understand-data-loss", file=sys.stderr)
        return 2

    if args.apply and args.include_boot and not args.cleanup_virtual_drive:
        print(
            "WARNING: --include-boot used without --cleanup-virtual-drive. "
            "Use this only for intentional non-SMI storage cleanup.",
            file=sys.stderr,
        )

    yaml_path = Path(args.yaml_file)
    if not yaml_path.exists():
        print(f"ERROR: YAML file not found: {yaml_path}", file=sys.stderr)
        return 2

    targets = collect_targets(load_yaml(yaml_path))
    if args.node:
        selected_nodes = set(args.node)
        targets = [target for target in targets if target["name"] in selected_nodes]

    if not targets:
        print("ERROR: no CIMC targets found in YAML", file=sys.stderr)
        return 2

    if args.list_targets:
        print_targets(targets)
        return 0

    failures = 0
    requested_drive_ids = set(args.drive or [])
    for target in targets:
        try:
            drives = inventory_virtual_drives(target, args.adapter or [], args.timeout)
            print_inventory(target, drives)
            actions = delete_virtual_drives(
                target,
                drives,
                apply=args.apply,
                include_boot=args.include_boot,
                cleanup_virtual_drive=args.cleanup_virtual_drive,
                requested_drive_ids=requested_drive_ids,
                timeout=args.timeout,
            )
            print_actions(actions)
        except Exception as exc:
            failures += 1
            print(f"\n=== ERROR {target['name']} CIMC {target['host']} ===")
            print(exc)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
