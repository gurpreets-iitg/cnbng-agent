# __________________________________________________
#
# Cloud Native BNG / Cisco Subscriber Edge
# Control Plane Deployment Agent
#
# Author: Gurpreet Dhaliwal, TME MiG
# __________________________________________________

"""Deployment cleanup for CNBNG."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import paramiko

from cnbng import __version__
from cnbng.input_resolver import resolve_input
from cnbng.inception_common import load_cluster_fields_from_xlsx


def release_root() -> Path:
    return Path(__file__).resolve().parents[2]


def make_run_dir(base_dir: Path | None = None, run_id: str | None = None) -> Path:
    base = base_dir or release_root() / "runs"
    run_name = run_id or "cnbng-cleanup-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = base / run_name
    for child in ("generated", "logs", "reports"):
        (run_dir / child).mkdir(parents=True, exist_ok=True)
    return run_dir


def run_command(command: list[str], log_file: Path, verbose: bool = False) -> int:
    if verbose:
        print("+ " + " ".join(command))
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


def print_log_tail(log_file: Path, lines: int = 30) -> None:
    if not log_file.exists():
        return
    content = log_file.read_text(errors="replace").splitlines()
    print(f"\nLast {min(lines, len(content))} line(s) from {log_file}:")
    for line in content[-lines:]:
        print(line)


def generate_yaml(args: argparse.Namespace, run_dir: Path) -> Path:
    output_yaml = run_dir / "generated" / "day0_3s.yaml"
    command = [
        sys.executable,
        "src/cnbng/xlsx_to_yaml.py",
        args.input,
        "--output",
        str(output_yaml),
    ]
    print("Generating internal YAML from XLSX...")
    log_file = run_dir / "logs" / "01-generate-yaml.log"
    rc = run_command(command, log_file, verbose=args.verbose)
    if rc != 0:
        print_log_tail(log_file)
        raise RuntimeError(f"XLSX to YAML generation failed; see {log_file}")
    print("YAML generated.")
    return output_yaml


def run_virtual_drive_cleanup(args: argparse.Namespace, run_dir: Path, yaml_path: Path) -> None:
    command = [
        sys.executable,
        "src/cnbng/cimc_virtual_drives.py",
        str(yaml_path),
        "--cleanup-virtual-drive",
        "--timeout",
        str(args.cimc_timeout),
    ]
    if args.apply:
        # The UCS cleanup utility keeps its own safety interlock. CNBNG asks
        # the operator interactively before this point and then satisfies the
        # lower-level guard on their behalf.
        command.extend(["--apply", "--i-understand-data-loss"])

    print("Cleaning UCS virtual drives..." if args.apply else "Checking UCS virtual drives...")
    log_file = run_dir / "logs" / "02-ucs-virtual-drive-cleanup.log"
    rc = run_command(command, log_file, verbose=args.verbose or not args.apply)
    if rc != 0:
        print_log_tail(log_file)
        raise RuntimeError(f"UCS virtual-drive cleanup failed; see {log_file}")
    print("UCS virtual-drive cleanup step completed.")


def read_shell(shell: paramiko.Channel, idle_seconds: float = 0.5, timeout: int = 8) -> str:
    end = time.time() + timeout
    last_data = time.time()
    chunks: list[str] = []
    while time.time() < end:
        if shell.recv_ready():
            chunks.append(shell.recv(65535).decode("utf-8", errors="replace"))
            last_data = time.time()
        elif chunks and time.time() - last_data >= idle_seconds:
            break
        else:
            time.sleep(0.1)
    return "".join(chunks)


def send_cli(shell: paramiko.Channel, command: str, timeout: int) -> str:
    shell.send(command + "\n")
    time.sleep(0.6)
    return read_shell(shell, timeout=timeout)


def remove_cluster_config(args: argparse.Namespace, run_dir: Path) -> None:
    fields = load_cluster_fields_from_xlsx(args.input, getattr(args, "cluster", None))
    cluster_name = args.cluster_name or fields.get("cluster_name")
    host = args.smi_host or fields.get("smi_deployer_ip")
    user = args.smi_user or fields.get("smi_deployer_user")
    password = args.smi_password or fields.get("smi_deployer_password")
    port = args.smi_port

    if not cluster_name:
        raise RuntimeError("cluster_name is required in XLSX Cluster sheet or via --cluster-name")
    if not host or not user or not password:
        raise RuntimeError("SMI deployer ip/user/password are required in XLSX Cluster sheet or via overrides")

    delete_command = args.smi_delete_command_template.format(
        cluster_name=cluster_name,
        cluster=cluster_name,
    )

    log_file = run_dir / "logs" / "03-smi-cluster-config-cleanup.log"
    print("Removing cluster config from SMI deployer..." if args.apply else "Checking SMI cluster config removal plan...")
    with log_file.open("w") as log:
        log.write(f"SMI CLI target: {user}@{host}:{port}\n")
        log.write(f"Cluster: {cluster_name}\n")
        log.write(f"Delete command: {delete_command}\n")
        if not args.apply:
            print(f"Would run on SMI CLI: configure ; {delete_command} ; commit")
            print(f"SMI cluster config cleanup plan logged: {log_file}")
            return

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            host,
            port=port,
            username=user,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=args.timeout,
            banner_timeout=args.timeout,
            auth_timeout=args.timeout,
        )
        try:
            shell = client.invoke_shell(width=240, height=1000)
            log.write(read_shell(shell, timeout=args.timeout))
            for command in ("config", delete_command, "commit", "exit", "exit"):
                output = send_cli(shell, command, args.timeout)
                log.write(f"\n$ {command}\n{output}\n")
                lowered = output.lower()
                if "syntax error" in lowered or "aborted" in lowered or "error:" in lowered:
                    raise RuntimeError(f"SMI CLI command failed: {command}; see {log_file}")
        finally:
            client.close()

    print(f"SMI cluster config cleanup completed. Log: {log_file}")


def cleanup(args: argparse.Namespace) -> int:
    if args.deployment != "3s":
        print(f"ERROR: v{__version__} supports --deployment 3s only", file=sys.stderr)
        return 2
    args.input = resolve_input(args.input, getattr(args, "cluster", None))
    print(f"Using XLSX: {args.input}")

    run_dir = make_run_dir(Path(args.run_base) if args.run_base else None, args.run_id)
    print(f"CNBNG cleanup run directory: {run_dir}")
    if not args.apply:
        print("Mode: DRY-RUN")
    else:
        print("Mode: APPLY")
        print("This will remove the SMI cluster config and delete UCS virtual drives.")
        try:
            answer = input("Continue? Type yes or no: ").strip().lower()
        except EOFError:
            print("ERROR: apply mode requires interactive yes/no confirmation", file=sys.stderr)
            return 2
        if answer != "yes":
            print("Cleanup aborted. No changes made.")
            return 0

    try:
        yaml_path = generate_yaml(args, run_dir)
        remove_cluster_config(args, run_dir)
        run_virtual_drive_cleanup(args, run_dir, yaml_path)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"Run artifacts: {run_dir}", file=sys.stderr)
        return 1

    print()
    print("Cleanup workflow completed.")
    print(f"Run artifacts: {run_dir}")
    return 0


def add_cleanup_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--deployment", required=True, choices=["3s"], help="Deployment model.")
    parser.add_argument("--input", required=True, help="Deployment XLSX input.")
    parser.add_argument("--cluster", help="Optional cluster selector: 1 or 2.")
    parser.add_argument("--run-id", help="Optional run identifier.")
    parser.add_argument("--run-base", help="Optional run directory base. Defaults to the CNBNG release runs directory.")
    parser.add_argument("--apply", action="store_true", help="Actually remove SMI cluster config and delete UCS virtual drives.")
    parser.add_argument("--verbose", action="store_true", help="Stream detailed command output to the terminal.")
    parser.add_argument("--cluster-name", help="Cluster name override. Defaults to XLSX Cluster.cluster_name.")
    parser.add_argument("--smi-host", help="SMI deployer IP override. Defaults to XLSX Cluster.smi_deployer_ip.")
    parser.add_argument("--smi-user", help="SMI deployer CLI user override. Defaults to XLSX Cluster.smi_deployer_user.")
    parser.add_argument("--smi-password", help="SMI deployer CLI password override. Defaults to XLSX Cluster.smi_deployer_password.")
    parser.add_argument("--smi-port", type=int, default=2022, help="SMI deployer CLI SSH port.")
    parser.add_argument(
        "--smi-delete-command-template",
        default="no clusters {cluster_name}",
        help="SMI CLI delete command template. Use {cluster_name} placeholder.",
    )
    parser.add_argument("--timeout", type=int, default=15, help="SMI CLI SSH command timeout in seconds.")
    parser.add_argument("--cimc-timeout", type=int, default=10, help="CIMC SSH command timeout in seconds.")
