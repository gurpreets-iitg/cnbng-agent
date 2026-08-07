#!/usr/bin/env python3
# __________________________________________________
#
# Cloud Native BNG / Cisco Subscriber Edge
# Control Plane Deployment Agent
#
# Author: Gurpreet Dhaliwal, TME MiG
# __________________________________________________

"""
Generate detailed cnBNG CP 3-server geo-red YAML from an XLSX workbook.

The workbook is intended to be customer-editable. This script reads the sheets,
normalizes the values, and writes the detailed YAML shape consumed by
CNBNG Day0 workflows and preflight validation.
"""

import argparse
import ipaddress
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import yaml


NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

NODE_ORDER = ("ucs01", "ucs02", "ucs03")
PROTO_NODES = ("ucs01", "ucs02")
LEAF_MODES = ("single_leaf", "two_leaf")
def text_or_empty(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def normalize_key(value):
    normalized = text_or_empty(value).lower()
    for char in (" ", "-", "/"):
        normalized = normalized.replace(char, "_")
    return "_".join(part for part in normalized.split("_") if part)


def col_to_index(cell_ref):
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


def parse_shared_strings(archive):
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


def workbook_sheet_paths(archive):
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


def read_sheet(archive, sheet_path, shared_strings):
    root = ET.fromstring(archive.read(sheet_path))
    rows = []
    for row in root.findall(".//main:sheetData/main:row", NS):
        values = []
        for cell in row.findall("main:c", NS):
            idx = col_to_index(cell.attrib["r"])
            while len(values) <= idx:
                values.append("")
            value_node = cell.find("main:v", NS)
            inline_node = cell.find("main:is/main:t", NS)
            raw_value = ""
            if inline_node is not None:
                raw_value = inline_node.text or ""
            elif value_node is not None:
                raw_value = value_node.text or ""
                if cell.attrib.get("t") == "s":
                    raw_value = shared_strings[int(raw_value)]
            values[idx] = text_or_empty(raw_value)
        while values and values[-1] == "":
            values.pop()
        rows.append(values)
    return rows


def read_workbook(path):
    with zipfile.ZipFile(path) as archive:
        shared_strings = parse_shared_strings(archive)
        sheet_paths = workbook_sheet_paths(archive)
        return {
            name: read_sheet(archive, sheet_path, shared_strings)
            for name, sheet_path in sheet_paths.items()
        }


def key_value_sheet(rows):
    result = {}
    if not rows:
        return result
    for row in rows[1:]:
        if len(row) < 2:
            continue
        key = normalize_key(row[0])
        value = text_or_empty(row[1])
        if key:
            result[key] = value
    return result


def table_sheet(rows, key_column):
    if not rows:
        return {}
    headers = [normalize_key(value) for value in rows[0]]
    result = {}
    for row in rows[1:]:
        if not any(text_or_empty(value) for value in row):
            continue
        item = {}
        for idx, header in enumerate(headers):
            if not header:
                continue
            item[header] = text_or_empty(row[idx]) if idx < len(row) else ""
        key = normalize_key(item.get(key_column, ""))
        if key:
            result[key] = item
    return result


def table_rows(rows):
    if not rows:
        return []
    headers = [normalize_key(value) for value in rows[0]]
    result = []
    for row in rows[1:]:
        if not any(text_or_empty(value) for value in row):
            continue
        item = {}
        for idx, header in enumerate(headers):
            if not header:
                continue
            item[header] = text_or_empty(row[idx]) if idx < len(row) else ""
        result.append(item)
    return result


def required(mapping, key, context):
    value = text_or_empty(mapping.get(key))
    if not value:
        raise ValueError(f"{context}.{key} is required")
    return value


def optional(mapping, key, default=""):
    return text_or_empty(mapping.get(key, default))


def bool_text(value, field):
    normalized = text_or_empty(value).lower()
    if normalized in ("true", "yes", "y", "1", "enabled", "enable"):
        return "true"
    if normalized in ("false", "no", "n", "0", "disabled", "disable", ""):
        return "false"
    raise ValueError(f"{field} must be true/false, yes/no, 1/0, or enabled/disabled")


def optional_any(mapping, keys, default=""):
    for key in keys:
        value = optional(mapping, key)
        if value:
            return value
    return text_or_empty(default)


def local_subnet_value(row):
    return optional_any(row, ("local_subnet_cidr", "network_subnet_cidr", "local_subnet", "subnet_cidr", "subnet"))


def peer_subnet_value(row):
    return optional_any(row, ("peer_cluster_subnet_cidr", "peer_subnet"))


def gateway_value(row):
    return optional_any(row, ("gateway_route_via", "gateway_next_hop", "gateway"))


def route_to_value(row):
    return optional_any(row, ("static_route_to", "route_to"))


def route_via_value(row):
    return optional_any(row, ("static_route_via", "route_via"))


def cidr(value, context):
    value = text_or_empty(value)
    if not value:
        return ""
    try:
        ipaddress.ip_interface(value)
    except ValueError as exc:
        raise ValueError(f"{context} must be IP/CIDR: {value} ({exc})")
    return value


def network(value, context):
    value = text_or_empty(value)
    if not value:
        return ""
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError as exc:
        raise ValueError(f"{context} must be subnet/CIDR: {value} ({exc})")
    return value


def ip(value, context):
    value = text_or_empty(value)
    if not value:
        return ""
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(f"{context} must be IP address: {value} ({exc})")
    return value


def asn(value, context):
    value = text_or_empty(value)
    if not value:
        return ""
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError(f"{context} must be a numeric ASN: {value} ({exc})")
    if number < 1 or number > 4294967295:
        raise ValueError(f"{context} must be between 1 and 4294967295: {value}")
    return number


def broadcast_for_subnet(subnet_value):
    net = ipaddress.ip_network(subnet_value, strict=False)
    return str(net.broadcast_address)


def comma_list(value):
    return [item.strip() for item in text_or_empty(value).split(",") if item.strip()]


def leaf_mode(cluster_values):
    mode = optional(cluster_values, "leaf_mode", "two_leaf").lower()
    normalized = mode.replace("-", "_").replace(" ", "_")
    aliases = {
        "single": "single_leaf",
        "one_leaf": "single_leaf",
        "1_leaf": "single_leaf",
        "1": "single_leaf",
        "dual_leaf": "two_leaf",
        "two": "two_leaf",
        "two_leaves": "two_leaf",
        "2_leaf": "two_leaf",
        "2": "two_leaf",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in LEAF_MODES:
        raise ValueError(
            f"Cluster.leaf_mode must be one of {', '.join(LEAF_MODES)} "
            f"(got {mode!r})"
        )
    return normalized


def bgp_slots(selected_leaf_mode):
    if selected_leaf_mode == "single_leaf":
        return (("bgp_a", "ebgp1"),)
    return (("bgp_a", "ebgp1"), ("bgp_b", "ebgp2"))


def bgp_session_rows(rows):
    sessions = {}
    path_level_rows = {}
    for row in rows:
        path_name = normalize_key(optional_any(row, ("bgp_path", "path")))
        node_name = normalize_key(optional(row, "node"))
        if not path_name:
            continue
        if node_name:
            sessions[(node_name, path_name)] = row
        else:
            path_level_rows[path_name] = row
    return sessions, path_level_rows


def build_images(images):
    result = {}
    for component in ("bng", "cee", "host_profile"):
        row = images.get(component)
        if not row:
            raise ValueError(f"Images sheet is missing component {component}")
        result[component] = {
            "name": required(row, "name", f"Images.{component}"),
            "url": required(row, "url", f"Images.{component}"),
            "sha256": required(row, "sha256", f"Images.{component}"),
        }
    return result


def build_bonds(bonds):
    result = {}
    for bond_name, row in bonds.items():
        links = comma_list(required(row, "links", f"Bonds.{bond_name}"))
        result[bond_name] = {"links": links}
    return result


def vlan(vlans, network_name, key="vlan"):
    row = vlans.get(network_name)
    if not row:
        raise ValueError(f"VLANs sheet is missing network {network_name}")
    return int(required(row, key, f"VLANs.{network_name}"))


def vlan_intf(vlans, network_name):
    row = vlans.get(network_name)
    if not row:
        raise ValueError(f"VLANs sheet is missing network {network_name}")
    return required(row, "interface", f"VLANs.{network_name}")


def build_cluster_networks(vlans, subnets):
    for name in ("mgmt", "k8s", "inttcp", "intudp", "n4", "cdl"):
        if name in subnets and local_subnet_value(subnets[name]):
            network(local_subnet_value(subnets[name]), f"Subnets.{name}.network_subnet_cidr")

    inttcp = subnets["inttcp"]
    intudp = subnets["intudp"]
    cdl = subnets["cdl"]
    n4_vips = subnets.get("n4_vips", {})

    networks = {
        "k8s": {
            "id": vlan(vlans, "k8s"),
            "intf": vlan_intf(vlans, "k8s"),
        },
        "mgmt": {
            "id": vlan(vlans, "mgmt"),
            "intf": vlan_intf(vlans, "mgmt"),
        },
        "inttcp": {
            "id": vlan(vlans, "inttcp"),
            "intf": vlan_intf(vlans, "inttcp"),
            "vip1": required(inttcp, "vip1", "Subnets.inttcp"),
            "vip2": required(inttcp, "vip2", "Subnets.inttcp"),
            "broadcast": optional(inttcp, "broadcast") or broadcast_for_subnet(local_subnet_value(inttcp)),
        },
        "intudp": {
            "id": vlan(vlans, "intudp"),
            "intf": vlan_intf(vlans, "intudp"),
            "vip1": required(intudp, "vip1", "Subnets.intudp"),
        },
        "n4": {
            "id": vlan(vlans, "n4"),
            "intf": vlan_intf(vlans, "n4"),
            "vip1": required(n4_vips, "vip1", "Subnets.n4_vips"),
            "vip2": required(n4_vips, "vip2", "Subnets.n4_vips"),
        },
        "cdl": {
            "id": vlan(vlans, "cdl"),
            "intf": vlan_intf(vlans, "cdl"),
            "vip1": required(cdl, "vip1", "Subnets.cdl"),
            "vip2": required(cdl, "vip2", "Subnets.cdl"),
            "vip3": required(cdl, "vip3", "Subnets.cdl"),
            "broadcast": optional(cdl, "broadcast") or broadcast_for_subnet(local_subnet_value(cdl)),
        },
    }

    n4_gateway = gateway_value(subnets.get("n4", {}))
    if n4_gateway:
        networks["n4"]["gateway"] = ip(n4_gateway, "Subnets.n4.gateway")

    return networks


def add_route(body, subnet_row):
    peer_subnet = peer_subnet_value(subnet_row)
    gateway = gateway_value(subnet_row)
    if peer_subnet and gateway:
        body["route"] = {
            "to": network(peer_subnet, "peer_subnet"),
            "via": ip(gateway, "gateway"),
        }


def node_network_from_server(server, network_name, subnets):
    ip_value = optional_any(
        server,
        (
            f"{network_name}_host_ip_cidr",
            f"{network_name}_ip_cidr",
            f"{network_name}_ip",
        ),
    )
    if not ip_value:
        return None
    body = {"ip": cidr(ip_value, f"Servers.{server['node']}.{network_name}_ip")}
    if network_name in ("inttcp", "cdl"):
        add_route(body, subnets[network_name])
    if network_name in ("k8s", "mgmt"):
        gateway = gateway_value(subnets.get(network_name, {}))
        if gateway:
            body["gateway"] = ip(gateway, f"Subnets.{network_name}.gateway")
    return body


def build_bgp_for_node(server, vlans, selected_leaf_mode, bgp_rows_by_session):
    bgp = {}

    for path_name, key in bgp_slots(selected_leaf_mode):
        row = bgp_rows_by_session.get((normalize_key(server["node"]), path_name), {})
        suffix = path_name.removeprefix("bgp_")
        ip_value = optional_any(
            row,
            (
                "local_host_ip_cidr",
                "local_ip_cidr",
                "bgp_host_ip_cidr",
                "ip_cidr",
                "ip",
            ),
        ) or optional_any(
            server,
            (
                f"bgp_{suffix}_host_ip_cidr",
                f"bgp_{suffix}_ip_cidr",
                f"bgp_{suffix}_ip",
            ),
        )
        if not ip_value:
            raise ValueError(f"BGP.{server['node']}.{path_name}.local_host_ip_cidr is required")
        item = {
            "id": vlan(vlans, path_name),
            "intf": vlan_intf(vlans, path_name),
            "ip": cidr(ip_value, f"BGP.{server['node']}.{path_name}.local_host_ip_cidr"),
        }
        bgp[key] = item
    return bgp


def build_bgp_peering(bgp_rows_by_session, legacy_path_rows, selected_leaf_mode):
    if not bgp_rows_by_session and not legacy_path_rows:
        return {}

    sessions = {}
    for node_name in PROTO_NODES:
        for path_name, _key in bgp_slots(selected_leaf_mode):
            row = bgp_rows_by_session.get((node_name, path_name), {})
            if not row:
                row = legacy_path_rows.get(path_name, {})
            peer_ip = optional_any(row, ("peer_gateway_ip", "peer_ip", "gateway_ip"))
            local_asn = optional(row, "local_asn")
            peer_asn = optional_any(row, ("peer_asn", "remote_asn"))

            if not any((peer_ip, local_asn, peer_asn)):
                continue
            if not all((peer_ip, local_asn, peer_asn)):
                raise ValueError(
                    f"BGP.{node_name}.{path_name}: peer_gateway_ip, local_asn, and peer_asn "
                    "must be provided together"
                )
            sessions.setdefault(node_name, {})[path_name] = {
                "peer_ip": ip(peer_ip, f"BGP.{node_name}.{path_name}.peer_gateway_ip"),
                "local_asn": asn(local_asn, f"BGP.{node_name}.{path_name}.local_asn"),
                "peer_asn": asn(peer_asn, f"BGP.{node_name}.{path_name}.peer_asn"),
            }

    return {"sessions": sessions} if sessions else {}


def build_nodes(
    servers,
    vlans,
    subnets,
    common_cimc_user,
    common_cimc_password,
    selected_leaf_mode,
    bgp_rows_by_session,
):
    result = {}
    for node_name in NODE_ORDER:
        server = servers.get(node_name)
        if not server:
            raise ValueError(f"Servers sheet is missing node {node_name}")

        networks = {}
        for network_name in ("k8s", "mgmt", "inttcp", "intudp", "n4", "cdl"):
            body = node_network_from_server(server, network_name, subnets)
            if body:
                networks[network_name] = body

        if node_name in PROTO_NODES:
            bgp = build_bgp_for_node(server, vlans, selected_leaf_mode, bgp_rows_by_session)
            if bgp:
                networks["bgp"] = bgp

        result[node_name] = {
            "networks": networks,
            "cimc": {
                "ip": ip(required(server, "cimc_ip", f"Servers.{node_name}"), f"Servers.{node_name}.cimc_ip"),
                "user": optional(server, "cimc_user") or common_cimc_user,
                "password": optional(server, "cimc_password") or common_cimc_password,
            },
        }
    return result


def build_yaml(sheets):
    cluster_values = key_value_sheet(sheets.get("Cluster", []))
    images = table_sheet(sheets.get("Images", []), "component")
    bonds = table_sheet(sheets.get("Bonds", []), "bond")
    vlans = table_sheet(sheets.get("VLANs", []), "network")
    subnets = table_sheet(sheets.get("Subnets", []), "network")
    servers = table_sheet(sheets.get("Servers", []), "node")
    bgp_sheet_rows = table_rows(sheets.get("BGP", []))
    bgp_rows_by_session, legacy_bgp_rows = bgp_session_rows(bgp_sheet_rows)

    common_cimc_user = optional(cluster_values, "common_cimc_user", "admin")
    common_cimc_password = required(cluster_values, "common_cimc_password", "Cluster")
    selected_leaf_mode = leaf_mode(cluster_values)
    inception_user = optional(cluster_values, "inception_vm_user", "cloud-user")
    default_ssh_private_key_file = f"/home/{inception_user}/.ssh/id_rsa"
    default_ssh_public_key_file = f"{default_ssh_private_key_file}.pub"

    cluster_networks = build_cluster_networks(vlans, subnets)
    nodes = build_nodes(
        servers,
        vlans,
        subnets,
        common_cimc_user,
        common_cimc_password,
        selected_leaf_mode,
        bgp_rows_by_session,
    )

    cp = {
        "node_defaults": {
            "route": required(cluster_values, "mgmt_static_route", "Cluster"),
            "gateway": required(cluster_values, "mgmt_gateway", "Cluster"),
            "dns": required(cluster_values, "dns_server", "Cluster"),
            "domain": required(cluster_values, "domain", "Cluster"),
            "ssh_private_key_file": optional(
                cluster_values,
                "ssh_private_key_file",
                default_ssh_private_key_file,
            ),
            "ssh_public_key_file": optional(
                cluster_values,
                "ssh_public_key_file",
                default_ssh_public_key_file,
            ),
            "cimc_boot_mode": optional(cluster_values, "cimc_boot_mode", "Uefi"),
            "bond_interfaces": build_bonds(bonds),
        },
        "cluster": {
            "name": required(cluster_values, "cluster_name", "Cluster"),
            "environment": "baremetal",
            "type": "3server_geo-red",
            "leaf_mode": selected_leaf_mode,
            "master": {
                "vip1": required(subnets["k8s"], "vip1", "Subnets.k8s"),
                "vip2": required(subnets["mgmt"], "vip1", "Subnets.mgmt"),
            },
            "networks": cluster_networks,
            "ntp": required(cluster_values, "ntp_server", "Cluster"),
            "istio": bool_text(optional(cluster_values, "istio", "false"), "Cluster.istio"),
            "gateway_api": bool_text(optional(cluster_values, "gateway_api", "true"), "Cluster.gateway_api"),
            "enable_network_policy": bool_text(
                optional(cluster_values, "enable_network_policy", "false"),
                "Cluster.enable_network_policy",
            ),
            "enable_ssh_firewall_rules": bool_text(
                optional(cluster_values, "enable_ssh_firewall_rules", "false"),
                "Cluster.enable_ssh_firewall_rules",
            ),
            "cee_ops_center": {
                "ssh_port": int(required(cluster_values, "cee_ssh_port", "Cluster")),
                "netconf_port": int(required(cluster_values, "cee_netconf_port", "Cluster")),
            },
            "bng_ops_center": {
                "ssh_port": int(required(cluster_values, "bng_ssh_port", "Cluster")),
                "netconf_port": int(required(cluster_values, "bng_netconf_port", "Cluster")),
            },
            "opscenter_password": optional(cluster_values, "opscenter_password", "REPLACE_WITH_OPSCENTER_PASSWORD"),
        },
    }

    if optional(cluster_values, "ingress"):
        cp["cluster"]["ingress"] = bool_text(optional(cluster_values, "ingress"), "Cluster.ingress")
    bgp_peering = build_bgp_peering(bgp_rows_by_session, legacy_bgp_rows, selected_leaf_mode)
    if bgp_peering:
        cp["bgp"] = bgp_peering

    cp.update(nodes)

    output = {
        "images": build_images(images),
        "cnbng_cp": cp,
        "smi_deployer": {
            "ip": ip(required(cluster_values, "smi_deployer_ip", "Cluster"), "Cluster.smi_deployer_ip"),
            "user": required(cluster_values, "smi_deployer_user", "Cluster"),
            "password": required(cluster_values, "smi_deployer_password", "Cluster"),
        },
    }

    inception_ip = optional(cluster_values, "inception_vm_ip", output["smi_deployer"]["ip"])
    inception_password = optional(cluster_values, "inception_vm_password", "REPLACE_WITH_INCEPTION_PASSWORD")
    inception_port = optional(cluster_values, "inception_vm_ssh_port", "22")
    if inception_ip or inception_user or inception_password:
        output["inception_vm"] = {
            "ip": ip(inception_ip, "Cluster.inception_vm_ip"),
            "user": inception_user,
            "password": inception_password,
            "port": int(inception_port),
        }

    return output


def main():
    parser = argparse.ArgumentParser(
        description="Generate detailed cnBNG CP 3-server geo-red YAML from XLSX."
    )
    parser.add_argument("xlsx_file", help="Path to cnBNG 3-server deployment XLSX")
    parser.add_argument(
        "--output",
        default="yaml/generated_3server_from_xls.yaml",
        help="Output YAML path",
    )
    args = parser.parse_args()

    input_path = Path(args.xlsx_file)
    output_path = Path(args.output)

    if not input_path.exists():
        print(f"ERROR: XLSX file not found: {input_path}", file=sys.stderr)
        return 2

    try:
        sheets = read_workbook(input_path)
        data = build_yaml(sheets)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as handle:
        yaml.safe_dump(data, handle, sort_keys=False, default_flow_style=False)

    print(f"Wrote {output_path}")
    print("Next:")
    print(f"  python3 src/cnbng/preflight.py {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
