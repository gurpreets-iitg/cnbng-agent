# __________________________________________________
#
# Cloud Native BNG / Cisco Subscriber Edge
# Control Plane Deployment Agent
#
# Author: Gurpreet Dhaliwal, TME MiG
# __________________________________________________

"""Shared Inception VM helpers for CNBNG."""

from __future__ import annotations

import argparse
import dataclasses
import shlex
import zipfile
from typing import Iterable
from xml.etree import ElementTree as ET

import paramiko

from cnbng.input_resolver import resolve_input


DEFAULT_COMPANION_CONTAINERS = ("registry",)
DEFAULT_DEPLOYER_DATA_PATHS = (
    "/data/inception",
    "/data/data/deployer-inception",
    "/data/deployer-inception",
)

NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


@dataclasses.dataclass
class CommandResult:
    command: str
    rc: int
    stdout: str
    stderr: str


class RemoteCommandError(RuntimeError):
    """Raised when a remote command fails unexpectedly."""


def text_or_empty(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def normalize_key(value) -> str:
    normalized = text_or_empty(value).lower()
    for char in (" ", "-", "/"):
        normalized = normalized.replace(char, "_")
    return "_".join(part for part in normalized.split("_") if part)


def col_to_index(cell_ref: str) -> int:
    col = ""
    for char in cell_ref:
        if char.isalpha():
            col += char
        else:
            break
    index = 0
    for char in col:
        index = index * 26 + (ord(char.upper()) - ord("A") + 1)
    return index - 1


def parse_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    path = "xl/sharedStrings.xml"
    if path not in archive.namelist():
        return []
    root = ET.fromstring(archive.read(path))
    strings = []
    for si in root.findall("main:si", NS):
        parts = []
        for text in si.findall(".//main:t", NS):
            parts.append(text.text or "")
        strings.append("".join(parts))
    return strings


def workbook_sheet_paths(archive: zipfile.ZipFile) -> dict[str, str]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rel_map = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels.findall("pkgrel:Relationship", NS)
    }
    paths = {}
    for sheet in workbook.findall("main:sheets/main:sheet", NS):
        name = sheet.attrib["name"]
        rel_id = sheet.attrib[f"{{{NS['rel']}}}id"]
        target = rel_map[rel_id]
        if target.startswith("/"):
            target = target.lstrip("/")
        elif not target.startswith("xl/"):
            target = "xl/" + target
        paths[name] = target
    return paths


def parse_sheet(archive: zipfile.ZipFile, path: str, shared_strings: list[str]) -> list[list[str]]:
    root = ET.fromstring(archive.read(path))
    rows = []
    for row in root.findall("main:sheetData/main:row", NS):
        values = []
        for cell in row.findall("main:c", NS):
            index = col_to_index(cell.attrib.get("r", "A1"))
            while len(values) <= index:
                values.append("")
            raw_value = cell.find("main:v", NS)
            inline_value = cell.find("main:is/main:t", NS)
            if cell.attrib.get("t") == "s" and raw_value is not None:
                values[index] = shared_strings[int(raw_value.text)]
            elif inline_value is not None:
                values[index] = inline_value.text or ""
            elif raw_value is not None:
                values[index] = raw_value.text or ""
            else:
                values[index] = ""
        rows.append(values)
    return rows


def load_cluster_fields_from_xlsx(path: str, cluster: str | None = None) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        shared_strings = parse_shared_strings(archive)
        sheets = workbook_sheet_paths(archive)
        if "Cluster" not in sheets:
            raise ValueError(f"{path}: missing required 'Cluster' sheet")
        rows = parse_sheet(archive, sheets["Cluster"], shared_strings)

    fields = {}
    if not rows:
        return fields
    for row in rows[1:]:
        if len(row) < 2:
            continue
        key = normalize_key(row[0])
        value = text_or_empty(row[1])
        if key:
            fields[key] = value
    return fields


def apply_xlsx_defaults(args: argparse.Namespace) -> None:
    if not args.input:
        return
    original_input = args.input
    args.input = resolve_input(args.input, getattr(args, "cluster", None))
    if args.input != original_input:
        print(f"Using XLSX: {args.input}")

    fields = load_cluster_fields_from_xlsx(args.input, getattr(args, "cluster", None))
    args.host = args.host or fields.get("inception_vm_ip")
    args.user = args.user or fields.get("inception_vm_user")
    args.password = args.password or fields.get("inception_vm_password")
    if args.port is None and fields.get("inception_vm_ssh_port"):
        args.port = int(fields["inception_vm_ssh_port"])


def connect_ssh(host: str, user: str, password: str | None, port: int, timeout: int) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=port,
        username=user,
        password=password,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def run(client: paramiko.SSHClient, command: str, timeout: int = 30) -> CommandResult:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    return CommandResult(
        command=command,
        rc=rc,
        stdout=stdout.read().decode("utf-8", errors="replace"),
        stderr=stderr.read().decode("utf-8", errors="replace"),
    )


def run_with_input(
    client: paramiko.SSHClient,
    command: str,
    input_text: str = "",
    timeout: int = 30,
) -> CommandResult:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    if input_text:
        stdin.write(input_text)
        stdin.flush()
    stdin.channel.shutdown_write()
    rc = stdout.channel.recv_exit_status()
    return CommandResult(
        command=command,
        rc=rc,
        stdout=stdout.read().decode("utf-8", errors="replace"),
        stderr=stderr.read().decode("utf-8", errors="replace"),
    )


def require_success(result: CommandResult) -> CommandResult:
    if result.rc != 0:
        raise RemoteCommandError(
            f"command failed rc={result.rc}: {result.command}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def split_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def sudo_command(password: str | None, command: str) -> tuple[str, str]:
    if password:
        return f"sudo -S -p '' bash -lc {shlex.quote(command)}", password + "\n"
    return f"sudo -n bash -lc {shlex.quote(command)}", ""


def run_sudo(client: paramiko.SSHClient, password: str | None, command: str, timeout: int = 30) -> CommandResult:
    sudo, input_text = sudo_command(password, command)
    return run_with_input(client, sudo, input_text, timeout=timeout)


def docker_available(client: paramiko.SSHClient) -> bool:
    result = run(client, "command -v docker >/dev/null 2>&1")
    return result.rc == 0


def collect_by_prefix(client: paramiko.SSHClient, kind: str, prefixes: Iterable[str], password: str | None = None) -> list[str]:
    if kind == "container":
        command = "docker ps -a --format '{{.Names}}'"
    elif kind == "network":
        command = "docker network ls --format '{{.Name}}'"
    elif kind == "volume":
        command = "docker volume ls --format '{{.Name}}'"
    else:
        raise ValueError(f"unsupported docker resource kind: {kind}")

    result = require_success(run_sudo(client, password, command))
    names = split_lines(result.stdout)
    return [name for name in names if any(name.startswith(prefix) for prefix in prefixes)]


def collect_exact_containers(client: paramiko.SSHClient, names: Iterable[str], password: str | None = None) -> list[str]:
    result = require_success(run_sudo(client, password, "docker ps -a --format '{{.Names}}'"))
    existing = set(split_lines(result.stdout))
    return [name for name in names if name in existing]


def collect_matching_processes(client: paramiko.SSHClient, prefixes: Iterable[str]) -> list[str]:
    patterns = "|".join(prefixes)
    result = run(
        client,
        "ps -eo pid=,args= | grep -E "
        + shlex.quote(patterns)
        + " | grep -v grep || true",
    )
    return split_lines(result.stdout)


def collect_matching_paths(client: paramiko.SSHClient, user: str, prefixes: Iterable[str]) -> list[str]:
    quoted_prefixes = " ".join(shlex.quote(prefix) for prefix in prefixes)
    search_roots = [
        f"/home/{user}",
        "/opt",
        "/data",
    ]
    roots = " ".join(shlex.quote(root) for root in search_roots)
    command = (
        "for root in "
        + roots
        + "; do "
        + "[ -d \"$root\" ] || continue; "
        + "find \"$root\" -maxdepth 4 \\( "
        + " ".join(f"-name {shlex.quote(prefix + '*')} -o" for prefix in prefixes)[:-3]
        + " \\) -print 2>/dev/null; "
        + "done"
    )
    result = run(client, command)
    return split_lines(result.stdout)


def collect_existing_data_paths(client: paramiko.SSHClient, paths: Iterable[str]) -> list[str]:
    quoted_paths = " ".join(shlex.quote(path) for path in paths)
    command = (
        "for path in "
        + quoted_paths
        + "; do "
        + "[ -e \"$path\" ] && echo \"$path\"; "
        + "done"
    )
    return split_lines(run(client, command).stdout)
