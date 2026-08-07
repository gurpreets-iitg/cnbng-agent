#!/usr/bin/env python3
# __________________________________________________
#
# Cloud Native BNG / Cisco Subscriber Edge
# Control Plane Deployment Agent
#
# Author: Gurpreet Dhaliwal, TME MiG
# __________________________________________________

"""
Preflight validation for cnBNG CP 3-server geo-red YAML files.

This script is read-only. It does not render XML or connect to SMI Deployer.
Use it before Day0 step1 to catch common addressing, VLAN, VIP, route, and
interface-placement issues in the 3-server CP-GR input YAML.
"""

import argparse
import ipaddress
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml
from yaml.loader import SafeLoader


NODES = ("ucs01", "ucs02", "ucs03")
PROTO_NODES = ("ucs01", "ucs02")
GEO_ROUTE_NETWORKS = ("inttcp", "cdl")
DOC_EXPECTED_GEO_VIP_NETWORKS = ("inttcp", "cdl")
LEAF_MODES = ("single_leaf", "two_leaf")


@dataclass
class Issue:
    severity: str
    message: str


class Preflight:
    def __init__(self, data, strict=False, check_inception_reachability=False, peer_yamls=None):
        self.data = data
        self.cp = data.get("cnbng_cp", {})
        self.cluster = self.cp.get("cluster", {})
        self.strict = strict
        self.check_inception_reachability = check_inception_reachability
        self.peer_yamls = peer_yamls or []
        self.issues = []
        self.node_interfaces = {}
        self.node_networks = {}

    def error(self, message):
        self.issues.append(Issue("ERROR", message))

    def warn(self, message):
        self.issues.append(Issue("WARN", message))

    def info(self, message):
        self.issues.append(Issue("INFO", message))

    def parse_interface(self, value, context):
        try:
            return ipaddress.ip_interface(value)
        except Exception as exc:
            self.error(f"{context}: invalid IP/CIDR '{value}' ({exc})")
            return None

    def parse_address(self, value, context):
        try:
            return ipaddress.ip_address(value)
        except Exception as exc:
            self.error(f"{context}: invalid IP address '{value}' ({exc})")
            return None

    def parse_network(self, value, context):
        try:
            return ipaddress.ip_network(value, strict=False)
        except Exception as exc:
            self.error(f"{context}: invalid route/network '{value}' ({exc})")
            return None

    def parse_asn(self, value, context):
        try:
            number = int(value)
        except Exception as exc:
            self.error(f"{context}: invalid ASN '{value}' ({exc})")
            return None
        if number < 1 or number > 4294967295:
            self.error(f"{context}: ASN {value} is outside valid range 1-4294967295")
            return None
        return number

    def load_node_networks(self):
        for node_name in NODES:
            node = self.cp.get(node_name, {})
            networks = node.get("networks", {})
            parsed = {}
            for net_name, body in networks.items():
                if net_name == "bgp":
                    continue
                if isinstance(body, dict) and "ip" in body:
                    iface = self.parse_interface(body["ip"], f"{node_name}.{net_name}.ip")
                    if iface:
                        parsed[net_name] = {
                            "interface": iface,
                            "network": iface.network,
                            "body": body,
                        }
            self.node_networks[node_name] = parsed

    def validate_deployment_type(self):
        environment = self.cluster.get("environment")
        deployment_type = self.cluster.get("type")
        if environment != "baremetal" or deployment_type != "3server_geo-red":
            self.error(
                "YAML is not a baremetal 3server_geo-red deployment "
                f"(environment={environment!r}, type={deployment_type!r})"
            )

        leaf_mode = self.leaf_mode()
        if leaf_mode not in LEAF_MODES:
            self.error(
                "cluster.leaf_mode must be 'single_leaf' or 'two_leaf' "
                f"(got {leaf_mode!r})"
            )

    def leaf_mode(self):
        raw = self.cluster.get("leaf_mode", "two_leaf")
        return str(raw).strip().lower().replace("-", "_").replace(" ", "_")

    def validate_network_vlan_inventory(self):
        networks = self.cluster.get("networks", {})
        expected = ("mgmt", "k8s", "inttcp", "intudp", "n4", "cdl")
        for name in expected:
            if name not in networks:
                self.error(f"cluster.networks.{name}: missing required network")
                continue
            for key in ("id", "intf"):
                if key not in networks[name]:
                    self.error(f"cluster.networks.{name}.{key}: missing")

        vlan_uses = {}
        for name, body in networks.items():
            vlan_id = body.get("id")
            if vlan_id is not None:
                vlan_uses.setdefault(vlan_id, []).append(f"cluster.networks.{name}")

        for node_name in PROTO_NODES:
            bgps = self.cp.get(node_name, {}).get("networks", {}).get("bgp", {})
            for bgp_name, bgp in bgps.items():
                vlan_id = bgp.get("id")
                if vlan_id is not None:
                    vlan_uses.setdefault(vlan_id, []).append(f"{node_name}.bgp.{bgp_name}")

        duplicate_vlans = {
            vlan_id: uses for vlan_id, uses in vlan_uses.items() if len(set(uses)) > 1
        }
        for vlan_id, uses in duplicate_vlans.items():
            # Same BGP VLAN on ucs01 and ucs02 is expected.
            unique_network_names = {use.split(".")[-1] for use in uses}
            if not all(".bgp." in use for use in uses) and len(unique_network_names) > 1:
                self.warn(f"VLAN {vlan_id} is reused by: {', '.join(uses)}")

        total_vlans = sorted(vlan_uses)
        expected_vlan_count = 7 if self.leaf_mode() == "single_leaf" else 8
        expected_text = (
            "single-leaf model normally has 7: mgmt, k8s, inttcp, intudp, n4, cdl, bgp-a"
            if self.leaf_mode() == "single_leaf"
            else "documented full model normally has 8: mgmt, k8s, inttcp, intudp, n4, cdl, bgp-a, bgp-b"
        )
        if len(total_vlans) < expected_vlan_count:
            self.warn(
                f"Only {len(total_vlans)} unique VLAN IDs found. {expected_text}."
            )

    def validate_ssh_key_material(self):
        node_defaults = self.cp.get("node_defaults", {})
        inception = self.data.get("inception_vm", {})
        private_key = node_defaults.get("ssh_private_key")
        private_key_file = node_defaults.get("ssh_private_key_file")
        public_key = node_defaults.get("ssh_public_key")
        public_key_file = node_defaults.get("ssh_public_key_file")

        if inception:
            for key in ("ip", "user", "password"):
                if not inception.get(key):
                    self.error(f"inception_vm.{key}: required to fetch SSH key files from Inception VM")
            if inception.get("port"):
                try:
                    port = int(inception["port"])
                    if port < 1 or port > 65535:
                        self.error(f"inception_vm.port: invalid TCP port {inception['port']}")
                except Exception:
                    self.error(f"inception_vm.port: invalid TCP port {inception['port']}")

        if not private_key and not private_key_file:
            default_user = inception.get("user", "<user>") if inception else "<user>"
            self.info(f"cnbng_cp.node_defaults.ssh_private_key_file: not set; deployer will use /home/{default_user}/.ssh/id_rsa on Inception VM")
        if not public_key and not public_key_file:
            default_user = inception.get("user", "<user>") if inception else "<user>"
            self.info(f"cnbng_cp.node_defaults.ssh_public_key_file: not set; deployer will use /home/{default_user}/.ssh/id_rsa.pub on Inception VM")

        if inception:
            if private_key_file:
                if (
                    inception.get("user")
                    and inception.get("user") != "admin"
                    and str(private_key_file).startswith("/home/admin/")
                ):
                    self.warn(
                        f"cnbng_cp.node_defaults.ssh_private_key_file uses /home/admin while "
                        f"inception_vm.user is {inception.get('user')}. The deployer will also try "
                        f"/home/{inception.get('user')}/{str(private_key_file)[len('/home/admin/'):]}"
                    )
                self.info(
                    f"cnbng_cp.node_defaults.ssh_private_key_file: will be created if missing, then fetched from "
                    f"{inception.get('user', '<user>')}@{inception.get('ip', '<ip>')}:{inception.get('port', 22)}"
                )
            if public_key_file:
                if (
                    inception.get("user")
                    and inception.get("user") != "admin"
                    and str(public_key_file).startswith("/home/admin/")
                ):
                    self.warn(
                        f"cnbng_cp.node_defaults.ssh_public_key_file uses /home/admin while "
                        f"inception_vm.user is {inception.get('user')}. The deployer will also try "
                        f"/home/{inception.get('user')}/{str(public_key_file)[len('/home/admin/'):]}"
                    )
                self.info(
                    f"cnbng_cp.node_defaults.ssh_public_key_file: will be created if missing, then fetched from "
                    f"{inception.get('user', '<user>')}@{inception.get('ip', '<ip>')}:{inception.get('port', 22)}"
                )
            return

        for key_name, path_value in (
            ("ssh_private_key_file", private_key_file),
            ("ssh_public_key_file", public_key_file),
        ):
            if not path_value:
                continue
            path = Path(str(path_value)).expanduser()
            if not path.exists():
                self.warn(
                    f"cnbng_cp.node_defaults.{key_name}: file not found locally at {path}. "
                    "This is OK only if Day0 step1 will run on a different host where the file exists."
                )

    def validate_node_ips_and_vips(self):
        self.load_node_networks()
        master = self.cluster.get("master", {})
        network_defs = self.cluster.get("networks", {})

        self.validate_vip_in_node_subnet("k8s", master.get("vip1"), "cluster.master.vip1")
        self.validate_vip_in_node_subnet("mgmt", master.get("vip2"), "cluster.master.vip2")

        for net_name, body in network_defs.items():
            for key, value in body.items():
                if key.startswith("vip"):
                    context = f"cluster.networks.{net_name}.{key}"
                    if net_name == "n4":
                        self.validate_external_vip(value, context)
                    else:
                        self.validate_vip_in_node_subnet(net_name, value, context)

            if "broadcast" in body:
                self.parse_address(body["broadcast"], f"cluster.networks.{net_name}.broadcast")

    def validate_vip_in_node_subnet(self, net_name, vip, context):
        if not vip:
            self.error(f"{context}: missing VIP")
            return
        address = self.parse_address(vip, context)
        if not address:
            return

        node_nets = [
            item["network"]
            for networks in self.node_networks.values()
            for name, item in networks.items()
            if name == net_name
        ]
        if not node_nets:
            self.warn(f"{context}: no node IPs found for {net_name}; cannot validate VIP subnet")
            return

        if not any(address in network for network in node_nets):
            nets = ", ".join(str(network) for network in sorted(set(node_nets), key=str))
            self.error(f"{context}: VIP {vip} is outside {net_name} node subnet(s): {nets}")

    def validate_external_vip(self, vip, context):
        address = self.parse_address(vip, context)
        if not address:
            return
        node_nets = [
            item["network"]
            for networks in self.node_networks.values()
            for name, item in networks.items()
            if name == "n4"
        ]
        if node_nets and any(address in network for network in node_nets):
            self.info(f"{context}: N4 VIP {vip} is inside native N4 subnet")
        else:
            self.info(f"{context}: N4 VIP {vip} treated as external /32 service VIP advertised by BGP")

    def n4_vip_set(self, data, context):
        n4 = (
            data.get("cnbng_cp", {})
            .get("cluster", {})
            .get("networks", {})
            .get("n4", {})
        )
        addresses = set()
        for key, value in n4.items():
            if not key.startswith("vip"):
                continue
            address = self.parse_address(value, f"{context}.cluster.networks.n4.{key}")
            if address:
                addresses.add(address)
        return addresses

    def validate_peer_n4_vips(self):
        local_vips = self.n4_vip_set(self.data, "local")
        if not local_vips:
            self.error("cluster.networks.n4: at least one N4 VIP is required")
            return

        for peer_path in self.peer_yamls:
            try:
                peer_data = load_yaml(peer_path)
            except Exception as exc:
                self.error(f"peer {peer_path}: failed to read peer YAML ({exc})")
                continue
            peer_vips = self.n4_vip_set(peer_data, f"peer {peer_path}")
            if not peer_vips:
                self.error(f"peer {peer_path}: cluster.networks.n4 has no VIPs")
                continue
            if local_vips != peer_vips:
                self.error(
                    "N4 VIPs must match across the geo-redundant cluster pair. "
                    f"local has {', '.join(str(ip) for ip in sorted(local_vips))}; "
                    f"peer {peer_path} has {', '.join(str(ip) for ip in sorted(peer_vips))}"
                )

    def collect_address_inventory(self):
        addresses = []
        networks = []

        def add_address(value, context):
            address = self.parse_address(value, context)
            if address:
                addresses.append((address, context))

        def add_interface(value, context):
            interface = self.parse_interface(value, context)
            if interface:
                addresses.append((interface.ip, context))
                networks.append((interface.network, context))

        for node_name, node_networks in self.node_networks.items():
            for net_name, item in node_networks.items():
                context = f"{node_name}.{net_name}.ip"
                addresses.append((item["interface"].ip, context))
                networks.append((item["network"], context))

        for node_name in NODES:
            cimc_ip = self.cp.get(node_name, {}).get("cimc", {}).get("ip")
            if cimc_ip:
                add_address(cimc_ip, f"{node_name}.cimc.ip")

        master = self.cluster.get("master", {})
        if master.get("vip1"):
            add_address(master["vip1"], "cluster.master.vip1")
        if master.get("vip2"):
            add_address(master["vip2"], "cluster.master.vip2")

        for net_name, body in self.cluster.get("networks", {}).items():
            for key, value in body.items():
                if key.startswith("vip") or key == "broadcast":
                    add_address(value, f"cluster.networks.{net_name}.{key}")

        smi_ip = self.data.get("smi_deployer", {}).get("ip")
        if smi_ip:
            add_address(smi_ip, "smi_deployer.ip")

        for node_name in NODES:
            bgps = self.cp.get(node_name, {}).get("networks", {}).get("bgp", {})
            for bgp_name, bgp in bgps.items():
                if bgp.get("ip"):
                    add_interface(bgp["ip"], f"{node_name}.bgp.{bgp_name}.ip")

        return addresses, networks

    def validate_address_overlaps(self):
        if not self.node_networks:
            self.load_node_networks()

        addresses, networks = self.collect_address_inventory()

        seen_addresses = {}
        for address, context in addresses:
            seen_addresses.setdefault(address, []).append(context)

        for address, contexts in sorted(seen_addresses.items(), key=lambda item: str(item[0])):
            unique_contexts = sorted(set(contexts))
            if len(unique_contexts) > 1:
                self.error(f"IP address {address} is reused by: {', '.join(unique_contexts)}")

        for index, (left_network, left_context) in enumerate(networks):
            for right_network, right_context in networks[index + 1:]:
                if left_network.version != right_network.version:
                    continue
                if not left_network.overlaps(right_network):
                    continue
                left_name = self.network_name_from_context(left_context)
                right_name = self.network_name_from_context(right_context)
                same_logical_network = left_name and left_name == right_name
                same_bgp_vlan = (
                    ".bgp." in left_context
                    and ".bgp." in right_context
                    and self.bgp_vlan_from_context(left_context) == self.bgp_vlan_from_context(right_context)
                )
                if same_logical_network or same_bgp_vlan:
                    continue
                self.error(
                    f"Subnet overlap: {left_context} {left_network} overlaps "
                    f"{right_context} {right_network}"
                )

    def network_name_from_context(self, context):
        parts = context.split(".")
        if len(parts) >= 3 and parts[0] in NODES and parts[2] == "ip":
            return parts[1]
        return None

    def bgp_vlan_from_context(self, context):
        parts = context.split(".")
        if len(parts) < 4 or parts[0] not in NODES or parts[1] != "bgp":
            return None
        bgp = self.cp.get(parts[0], {}).get("networks", {}).get("bgp", {}).get(parts[2], {})
        return bgp.get("id")

    def validate_routes(self):
        for node_name, networks in self.node_networks.items():
            for net_name, item in networks.items():
                if net_name != "mgmt" and item["body"].get("gateway"):
                    self.warn(
                        f"{node_name}.{net_name}.gateway: gateway is present, but 3-server template "
                        "renders the default route only on mgmt. Use an explicit route for non-mgmt reachability."
                    )
                route = item["body"].get("route")
                if not route:
                    continue
                to = route.get("to")
                via = route.get("via")
                if not to or not via:
                    self.error(f"{node_name}.{net_name}.route: both 'to' and 'via' are required")
                    continue
                self.parse_network(to, f"{node_name}.{net_name}.route.to")
                via_ip = self.parse_address(via, f"{node_name}.{net_name}.route.via")
                if via_ip and via_ip not in item["network"]:
                    self.warn(
                        f"{node_name}.{net_name}.route.via {via} is outside local subnet {item['network']}"
                    )

        for net_name in GEO_ROUTE_NETWORKS:
            nodes_with_net = [
                node_name
                for node_name, networks in self.node_networks.items()
                if net_name in networks
            ]
            for node_name in nodes_with_net:
                route = self.cp[node_name]["networks"][net_name].get("route")
                if not route:
                    self.warn(
                        f"{node_name}.{net_name}: no peer-cluster route configured; "
                        f"{net_name} VIPs will be local unless reachability is provided elsewhere"
                    )

        for net_name in DOC_EXPECTED_GEO_VIP_NETWORKS:
            network_def = self.cluster.get("networks", {}).get(net_name, {})
            has_vip = any(key.startswith("vip") for key in network_def)
            if not has_vip:
                continue
            has_any_route = any(
                net_name in networks and networks[net_name]["body"].get("route")
                for networks in self.node_networks.values()
            )
            if not has_any_route:
                self.warn(
                    f"{net_name}: VIP is configured but no route is declared. "
                    "Cisco CP-GR docs expect cdl, udp/intudp, and inttcp VIP reachability between sites."
                )

    def validate_bgp(self):
        selected_leaf_mode = self.leaf_mode()
        required_bgps = 1 if selected_leaf_mode == "single_leaf" else 2
        required_peer_names = ("bgp_a",) if selected_leaf_mode == "single_leaf" else ("bgp_a", "bgp_b")
        configured_sessions = self.cp.get("bgp", {}).get("sessions", {})
        legacy_peers = self.cp.get("bgp", {}).get("peers", {})

        for node_name in PROTO_NODES:
            for peer_name in required_peer_names:
                peer = configured_sessions.get(node_name, {}).get(peer_name)
                context = f"cnbng_cp.bgp.sessions.{node_name}.{peer_name}"
                if not peer and legacy_peers:
                    peer = legacy_peers.get(peer_name)
                    context = f"cnbng_cp.bgp.peers.{peer_name}"
                if not peer:
                    self.warn(
                        f"{context}: peer gateway IP, local ASN, and peer ASN are not configured"
                    )
                    continue
                peer_ip = peer.get("peer_ip")
                local_asn = peer.get("local_asn")
                peer_asn = peer.get("peer_asn")
                if peer_ip:
                    self.parse_address(peer_ip, f"{context}.peer_ip")
                else:
                    self.error(f"{context}.peer_ip: missing")
                if local_asn:
                    self.parse_asn(local_asn, f"{context}.local_asn")
                else:
                    self.error(f"{context}.local_asn: missing")
                if peer_asn:
                    self.parse_asn(peer_asn, f"{context}.peer_asn")
                else:
                    self.error(f"{context}.peer_asn: missing")

        for node_name in PROTO_NODES:
            bgps = self.cp.get(node_name, {}).get("networks", {}).get("bgp", {})
            if not bgps:
                self.error(f"{node_name}: protocol node has no BGP interfaces configured")
                continue
            if len(bgps) < required_bgps:
                self.warn(
                    f"{node_name}: only {len(bgps)} BGP interface(s) configured; "
                    f"{selected_leaf_mode} design expects {required_bgps} per proto node"
                )
            if selected_leaf_mode == "single_leaf" and len(bgps) > 1:
                self.warn(
                    f"{node_name}: {len(bgps)} BGP interfaces configured while cluster.leaf_mode is single_leaf; "
                    "generator will normally emit only BGP-A for single-leaf"
                )

            for bgp_name, bgp in bgps.items():
                context = f"{node_name}.bgp.{bgp_name}"
                parent = bgp.get("intf")
                ip_value = bgp.get("ip")
                if not parent:
                    self.error(f"{context}.intf: missing")
                elif parent.startswith("bd"):
                    message = f"{context}.intf: BGP is configured on bond interface {parent}; documented CP-GR examples use raw physical VLAN parents"
                    if self.strict:
                        self.error(message)
                    else:
                        self.warn(message)
                if ip_value:
                    self.parse_interface(ip_value, f"{context}.ip")
                else:
                    self.error(f"{context}.ip: missing")

                route = bgp.get("route")
                if route:
                    to = route.get("to")
                    via = route.get("via")
                    if to:
                        self.parse_network(to, f"{context}.route.to")
                    else:
                        self.error(f"{context}.route.to: missing")
                    if via:
                        self.parse_address(via, f"{context}.route.via")
                    else:
                        self.error(f"{context}.route.via: missing")

        if self.cp.get("ucs03", {}).get("networks", {}).get("bgp"):
            self.warn("ucs03 has BGP configured; documented 3-server CP-GR model normally runs BGP on proto nodes only")

    def validate_inception_reachability(self):
        inception = self.data.get("inception_vm", {})
        host = inception.get("ip")
        user = inception.get("user")
        password = inception.get("password")
        try:
            port = int(inception.get("port", 22) or 22)
        except Exception:
            self.error(f"inception_vm.port: invalid TCP port {inception.get('port')}")
            return

        if not host or not user or not password:
            self.error(
                "inception_vm.ip, inception_vm.user, and inception_vm.password are required "
                "for --check-inception-reachability"
            )
            return

        try:
            import paramiko
        except ImportError:
            self.error("paramiko is required for --check-inception-reachability")
            return

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host,
                port=port,
                username=user,
                password=password,
                timeout=15,
                banner_timeout=15,
                auth_timeout=15,
                look_for_keys=False,
                allow_agent=False,
            )
        except Exception as exc:
            self.error(f"inception_vm: failed to SSH to {user}@{host}:{port} ({exc})")
            return

        try:
            for node_name in NODES:
                node = self.cp.get(node_name, {})
                networks = node.get("networks", {})
                mgmt_ip = self.ip_without_prefix(networks.get("mgmt", {}).get("ip"))
                k8s_ip = self.ip_without_prefix(networks.get("k8s", {}).get("ip"))
                cimc_ip = node.get("cimc", {}).get("ip")

                if mgmt_ip:
                    self.check_route_from_inception(client, node_name, "mgmt", mgmt_ip)
                else:
                    self.error(f"{node_name}.mgmt.ip: missing; cannot validate Inception reachability")

                if k8s_ip:
                    self.check_route_from_inception(client, node_name, "k8s", k8s_ip)
                else:
                    self.error(f"{node_name}.k8s.ip: missing; cannot validate Inception reachability")

                if cimc_ip:
                    self.check_route_from_inception(client, node_name, "cimc", cimc_ip)
                    self.check_tcp_from_inception(
                        client, node_name, "cimc", cimc_ip, (22, 443), require_any=True
                    )
                else:
                    self.error(f"{node_name}.cimc.ip: missing; cannot validate Inception reachability")
        finally:
            client.close()

    def ip_without_prefix(self, value):
        if not value:
            return None
        try:
            return str(ipaddress.ip_interface(value).ip)
        except ValueError:
            try:
                return str(ipaddress.ip_address(value))
            except ValueError:
                return None

    def check_tcp_from_inception(self, client, node_name, network_name, ip_address, ports, require_any):
        passed_ports = []
        failed_ports = []
        for target_port in ports:
            if self.remote_tcp_check(client, ip_address, target_port):
                passed_ports.append(target_port)
            else:
                failed_ports.append(target_port)

        context = f"inception_vm reachability: {node_name}.{network_name} {ip_address}"
        if require_any:
            if passed_ports:
                self.info(f"{context}: TCP {', '.join(str(port) for port in passed_ports)} reachable")
            else:
                self.error(
                    f"{context}: failed on TCP {', '.join(str(port) for port in failed_ports)}"
                )
            return

        if failed_ports:
            self.error(f"{context}: TCP {failed_ports[0]} failed")
        else:
            self.info(f"{context}: TCP {ports[0]} reachable")

    def check_route_from_inception(self, client, node_name, network_name, ip_address):
        safe_ip = shlex.quote(str(ip_address))
        command = f"ip route get {safe_ip}"
        context = f"inception_vm route: {node_name}.{network_name} {ip_address}"
        try:
            _, stdout, stderr = client.exec_command(command, timeout=8)
            rc = stdout.channel.recv_exit_status()
            output = stdout.read().decode("utf-8", errors="replace").strip()
            error = stderr.read().decode("utf-8", errors="replace").strip()
        except Exception as exc:
            self.error(f"{context}: route check failed ({exc})")
            return
        if rc != 0:
            self.error(f"{context}: no route ({error or output or f'rc={rc}'})")
            return
        first_line = output.splitlines()[0] if output else "route present"
        self.info(f"{context}: {first_line}")

    def remote_tcp_check(self, client, ip_address, port):
        safe_ip = shlex.quote(str(ip_address))
        safe_port = shlex.quote(str(port))
        command = f"timeout 5 bash -lc '</dev/tcp/{safe_ip}/{safe_port}'"
        try:
            _, stdout, stderr = client.exec_command(command, timeout=8)
            rc = stdout.channel.recv_exit_status()
            stderr.read()
            return rc == 0
        except Exception:
            return False

    def run(self):
        self.validate_deployment_type()
        self.validate_network_vlan_inventory()
        self.validate_ssh_key_material()
        self.validate_node_ips_and_vips()
        self.validate_peer_n4_vips()
        self.validate_address_overlaps()
        self.validate_routes()
        self.validate_bgp()
        if self.check_inception_reachability:
            self.validate_inception_reachability()
        return self.issues

    def print_summary(self):
        print(f"cnBNG CP 3-server preflight: {self.cluster.get('name', '<unknown>')}")
        print("")
        self.print_bonds()
        self.print_networks()
        self.print_bgp()
        self.print_bgp_peering()
        self.print_issues()

    def print_bonds(self):
        print("Bond Summary")
        bonds = self.cp.get("node_defaults", {}).get("bond_interfaces", {})
        if not bonds:
            print("  none")
        for bond, body in bonds.items():
            print(f"  {bond}: {', '.join(body.get('links', [])) or 'no members'}")
        print("")

    def print_networks(self):
        print("Network Summary")
        master = self.cluster.get("master", {})
        for name, body in self.cluster.get("networks", {}).items():
            vlan = body.get("id", "NA")
            intf = body.get("intf", "NA")
            node_ips = []
            routes = []
            for node_name in NODES:
                node_net = self.cp.get(node_name, {}).get("networks", {}).get(name, {})
                if "ip" in node_net:
                    node_ips.append(f"{node_name}:{node_net['ip']}")
                if "route" in node_net:
                    route = node_net["route"]
                    routes.append(f"{node_name}:{route.get('to')} via {route.get('via')}")
            vips = []
            if name == "k8s" and master.get("vip1"):
                vips.append(master["vip1"])
            elif name == "mgmt" and master.get("vip2"):
                vips.append(master["vip2"])
            else:
                vips = [value for key, value in body.items() if key.startswith("vip")]

            print(f"  {name}: VLAN {vlan}, {intf}.{name}.{vlan}")
            print(f"    node IPs: {', '.join(node_ips) if node_ips else 'none'}")
            print(f"    VIPs: {', '.join(vips) if vips else 'none'}")
            print(f"    routes: {', '.join(routes) if routes else 'none'}")
        print("")

    def print_bgp(self):
        print("BGP Summary")
        for node_name in PROTO_NODES:
            bgps = self.cp.get(node_name, {}).get("networks", {}).get("bgp", {})
            if not bgps:
                print(f"  {node_name}: none")
                continue
            for bgp_name, bgp in bgps.items():
                route = bgp.get("route", {})
                route_text = ""
                if route:
                    route_text = f", route {route.get('to')} via {route.get('via')}"
                print(
                    f"  {node_name}.{bgp_name}: VLAN {bgp.get('id')}, "
                    f"{bgp.get('intf')}.{bgp.get('id')}, {bgp.get('ip')}{route_text}"
                )
        ucs03_bgps = self.cp.get("ucs03", {}).get("networks", {}).get("bgp", {})
        if ucs03_bgps:
            for bgp_name, bgp in ucs03_bgps.items():
                print(
                    f"  ucs03.{bgp_name}: VLAN {bgp.get('id')}, "
                    f"{bgp.get('intf')}.{bgp.get('id')}, {bgp.get('ip')} (unexpected)"
                )
        print("")

    def print_bgp_peering(self):
        print("BGP Peering Summary")
        sessions = self.cp.get("bgp", {}).get("sessions", {})
        legacy_peers = self.cp.get("bgp", {}).get("peers", {})
        if not sessions and not legacy_peers:
            print("  none")
        for node_name, node_sessions in sessions.items():
            for peer_name, peer in node_sessions.items():
                print(
                    f"  {node_name}.{peer_name}: peer {peer.get('peer_ip', 'NA')}, "
                    f"local-as {peer.get('local_asn', 'NA')}, peer-as {peer.get('peer_asn', 'NA')}"
                )
        for peer_name, peer in legacy_peers.items():
            print(
                f"  {peer_name}: peer {peer.get('peer_ip', 'NA')}, "
                f"local-as {peer.get('local_asn', 'NA')}, peer-as {peer.get('peer_asn', 'NA')}"
            )
        print("")

    def print_issues(self):
        counts = {severity: 0 for severity in ("ERROR", "WARN", "INFO")}
        for issue in self.issues:
            counts[issue.severity] += 1

        print(f"Issues: {counts['ERROR']} error(s), {counts['WARN']} warning(s), {counts['INFO']} info")
        for severity in ("ERROR", "WARN", "INFO"):
            severity_issues = [issue for issue in self.issues if issue.severity == severity]
            if not severity_issues:
                continue
            print(f"\n{severity}")
            for issue in severity_issues:
                print(f"  - {issue.message}")


def load_yaml(path):
    with open(path, "r") as handle:
        return yaml.load(handle, Loader=SafeLoader)


def main():
    parser = argparse.ArgumentParser(
        description="Preflight validation for cnBNG CP 3-server geo-red YAML files."
    )
    parser.add_argument("yaml_file", help="Path to 3-server deployment YAML")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat BGP overlap with bond members as an error instead of a warning",
    )
    parser.add_argument(
        "--check-inception-reachability",
        action="store_true",
        help="SSH to the Inception VM and test routes to UCS mgmt/k8s plus route/TCP access to CIMC IPs",
    )
    parser.add_argument(
        "--peer-yaml",
        action="append",
        default=[],
        help="Peer cluster YAML for geo-redundant pair validation, including shared N4 VIP checks",
    )
    args = parser.parse_args()

    path = Path(args.yaml_file)
    if not path.exists():
        print(f"ERROR: YAML file not found: {path}", file=sys.stderr)
        return 2

    data = load_yaml(path)
    preflight = Preflight(
        data,
        strict=args.strict,
        check_inception_reachability=args.check_inception_reachability,
        peer_yamls=[Path(peer_yaml) for peer_yaml in args.peer_yaml],
    )
    issues = preflight.run()
    preflight.print_summary()

    return 1 if any(issue.severity == "ERROR" for issue in issues) else 0


if __name__ == "__main__":
    sys.exit(main())
