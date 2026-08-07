# __________________________________________________
#
# Cloud Native BNG / Cisco Subscriber Edge
# Control Plane Deployment Agent
#
# Author: Gurpreet Dhaliwal, TME MiG
# __________________________________________________

import yaml
from yaml.loader import SafeLoader
from jinja2 import Environment, FileSystemLoader
from ncclient import manager
import re
import shlex
import ipaddress
from pathlib import Path
from xml.etree import ElementTree as ET

def bannerText(text):
    print("="*100)
    print(text)
    print("="*100)

def getDeploymentEnvironmentType(data):
    cluster = data.get('cnbng_cp', {}).get('cluster', {})
    cluster_defaults = data.get('cnbng_cp', {}).get('cluster_defaults', {})
    environment = cluster.get('environment', cluster_defaults.get('environment'))
    deployment_type = cluster.get('type', cluster_defaults.get('type'))
    return environment, deployment_type

def stripCidr(value):
    return str(value).split("/", 1)[0]

def inferSiteId(cluster):
    explicit_id = cluster.get('id') or cluster.get('site_id')
    if explicit_id:
        return int(explicit_id)
    name = str(cluster.get('name', '')).lower()
    if any(token in name for token in ('geored2', 'geo-red2', 'gr2', 'tb2', 'cluster-2')):
        return 2
    return 1

def inferRemoteSiteId(cluster, site_id):
    explicit_id = cluster.get('remote_id') or cluster.get('peer_site_id')
    if explicit_id:
        return int(explicit_id)
    return 1 if int(site_id) == 2 else 2

def inferPeerVip(local_vip, peer_subnet):
    if not local_vip or not peer_subnet:
        return None
    try:
        local_ip = ipaddress.ip_address(stripCidr(local_vip))
        remote_network = ipaddress.ip_network(peer_subnet, strict=False)
        host_index = int(local_ip) - int(ipaddress.ip_network(str(local_ip) + "/24", strict=False).network_address)
        remote_ip = ipaddress.ip_address(int(remote_network.network_address) + host_index)
        if remote_ip in remote_network:
            return str(remote_ip)
    except ValueError:
        return None
    return None

def inferPeerVipByPrefix(local_vip, peer_hint_subnet):
    if not local_vip or not peer_hint_subnet:
        return None
    try:
        local_ip = ipaddress.ip_address(stripCidr(local_vip))
        peer_network = ipaddress.ip_network(peer_hint_subnet, strict=False)
    except ValueError:
        return None
    if local_ip.version != 4 or peer_network.version != 4:
        return None
    local_parts = str(local_ip).split(".")
    peer_parts = str(peer_network.network_address).split(".")
    return ".".join(peer_parts[:2] + local_parts[2:])

def vlanInterface(network):
    return f"{network['intf']}.{network.get('name', 'net')}.{network['id']}"

def namedVlanInterface(name, network):
    return f"{network['intf']}.{name}.{network['id']}"

def firstBgpLocalSession(cp, node_name, path_name):
    node_networks = cp.get(node_name, {}).get('networks', {})
    bgp_network = node_networks.get('bgp', {})
    path_key = 'ebgp1' if path_name == 'bgp_a' else 'ebgp2'
    return bgp_network.get(path_key, {})

def buildCndp3ServerStep2Model(data):
    cp = data.setdefault('cnbng_cp', {})
    cluster = cp.setdefault('cluster', {})
    networks = cluster.setdefault('networks', {})
    cdl = networks.get('cdl', {})
    n4 = networks.get('n4', {})
    bgp = cp.get('bgp', {})
    bgp_sessions = bgp.get('sessions', {})

    site_id = inferSiteId(cluster)
    remote_site_id = inferRemoteSiteId(cluster, site_id)

    cdl_route = cp.get('ucs02', {}).get('networks', {}).get('cdl', {}).get('route', {})
    inttcp_route = cp.get('ucs01', {}).get('networks', {}).get('inttcp', {}).get('route', {})
    peer_cdl_subnet = cdl_route.get('to')
    peer_inttcp_subnet = inttcp_route.get('to')
    cdl_remote_vip1 = cdl.get('remote_vip1') or inferPeerVip(cdl.get('vip1'), peer_cdl_subnet)
    cdl_remote_vip2 = cdl.get('remote_vip2') or inferPeerVip(cdl.get('vip2'), peer_cdl_subnet)
    cdl_remote_vip3 = cdl.get('remote_vip3') or inferPeerVip(cdl.get('vip3'), peer_cdl_subnet)
    inttcp = networks.get('inttcp', {})
    intudp = networks.get('intudp', {})
    inttcp_remote_vip1 = inttcp.get('remote_vip1') or inferPeerVip(inttcp.get('vip1'), peer_inttcp_subnet)
    inttcp_remote_vip2 = inttcp.get('remote_vip2') or inferPeerVip(inttcp.get('vip2'), peer_inttcp_subnet)
    intudp_remote_vip1 = (
        intudp.get('remote_vip1')
        or inferPeerVipByPrefix(intudp.get('vip1'), peer_cdl_subnet)
        or inferPeerVipByPrefix(intudp.get('vip1'), peer_inttcp_subnet)
    )

    if site_id == 1:
        inttcp_site1_vip1 = inttcp.get('vip1')
        inttcp_site1_vip2 = inttcp.get('vip2')
        inttcp_site2_vip1 = inttcp_remote_vip1
        inttcp_site2_vip2 = inttcp_remote_vip2
        intudp_site1_vip1 = intudp.get('vip1')
        intudp_site2_vip1 = intudp_remote_vip1
    elif site_id == 2:
        inttcp_site1_vip1 = inttcp_remote_vip1
        inttcp_site1_vip2 = inttcp_remote_vip2
        inttcp_site2_vip1 = inttcp.get('vip1')
        inttcp_site2_vip2 = inttcp.get('vip2')
        intudp_site1_vip1 = intudp_remote_vip1
        intudp_site2_vip1 = intudp.get('vip1')
    else:
        inttcp_site1_vip1 = inttcp.get('vip1')
        inttcp_site1_vip2 = inttcp.get('vip2')
        inttcp_site2_vip1 = inttcp_remote_vip1
        inttcp_site2_vip2 = inttcp_remote_vip2
        intudp_site1_vip1 = intudp.get('vip1')
        intudp_site2_vip1 = intudp_remote_vip1

    router_asn = None
    router_interfaces = {}
    for node_name, node_sessions in bgp_sessions.items():
        for path_name, session in node_sessions.items():
            local_session = firstBgpLocalSession(cp, node_name, path_name)
            local_intf = local_session.get('intf')
            vlan_id = local_session.get('id')
            if not local_intf or not vlan_id:
                continue
            router_asn = router_asn or session.get('local_asn')
            interface_name = f"{local_intf}.{vlan_id}"
            router_interfaces.setdefault(
                interface_name,
                {
                    'interface': interface_name,
                    'bonding_interface': local_intf,
                    'neighbors': [],
                    'neighbor_keys': set(),
                },
            )
            neighbor_key = (session.get('peer_ip'), session.get('peer_asn'))
            if neighbor_key not in router_interfaces[interface_name]['neighbor_keys']:
                router_interfaces[interface_name]['neighbors'].append({
                    'neighbor': session.get('peer_ip'),
                    'remote_as': session.get('peer_asn'),
                })
                router_interfaces[interface_name]['neighbor_keys'].add(neighbor_key)

    for router_interface in router_interfaces.values():
        router_interface.pop('neighbor_keys', None)

    n4_interface = namedVlanInterface('n4', n4) if n4 else None
    default_policies = [
        {'name': 'allow-9k-1', 'prefix': '192.69.0.0/16', 'mask_range': '16..32'},
        {'name': 'allow-9k-2', 'prefix': '192.70.0.0/16', 'mask_range': '16..32'},
        {'name': 'allow-radius-1', 'prefix': '192.71.0.0/16', 'mask_range': '16..32'},
        {'name': 'allow-radius-2', 'prefix': '192.72.0.0/16', 'mask_range': '16..32'},
    ]
    policies = bgp.get('policies', default_policies)
    n4_gateway = n4.get('gateway')

    step2 = {
        'site_id': site_id,
        'remote_site_id': remote_site_id,
        'opscenter_ip': cluster.get('master', {}).get('vip2'),
        'opscenter_netconf_port': cluster.get('bng_ops_center', {}).get('netconf_port', 3024),
        'cdl_local_vip1': cdl.get('vip1'),
        'cdl_local_vip2': cdl.get('vip2'),
        'cdl_local_vip3': cdl.get('vip3'),
        'cdl_remote_vip1': cdl_remote_vip1,
        'cdl_remote_vip2': cdl_remote_vip2,
        'cdl_remote_vip3': cdl_remote_vip3,
        'cdl_db_port': cdl.get('db_port', 8882),
        'cdl_kafka_port1': cdl.get('kafka_port1', 10092),
        'cdl_kafka_port2': cdl.get('kafka_port2', 10093),
        'inttcp_local_vip1': inttcp.get('vip1'),
        'inttcp_local_vip2': inttcp.get('vip2'),
        'inttcp_remote_vip1': inttcp_remote_vip1,
        'inttcp_remote_vip2': inttcp_remote_vip2,
        'inttcp_site1_vip1': inttcp_site1_vip1,
        'inttcp_site1_vip2': inttcp_site1_vip2,
        'inttcp_site2_vip1': inttcp_site2_vip1,
        'inttcp_site2_vip2': inttcp_site2_vip2,
        'inttcp_geo_internal_port': inttcp.get('geo_internal_port', 7001),
        'inttcp_geo_external_port': inttcp.get('geo_external_port', 7002),
        'intudp_local_vip1': intudp.get('vip1'),
        'intudp_remote_vip1': intudp_remote_vip1,
        'intudp_site1_vip1': intudp_site1_vip1,
        'intudp_site2_vip1': intudp_site2_vip1,
        'n4_vip1': n4.get('vip1'),
        'n4_vip2': n4.get('vip2'),
        'radius_nas_identifier1': cp.get('profile', {}).get('radius', {}).get('nas_identifier1', 'CISCO-BNG-1'),
        'radius_nas_identifier2': cp.get('profile', {}).get('radius', {}).get('nas_identifier2', 'CISCO-BNG-2'),
        'radius_accounting_nas_identifier1': cp.get('profile', {}).get('radius', {}).get('accounting_nas_identifier1', 'cisco-acct-1'),
        'radius_accounting_nas_identifier2': cp.get('profile', {}).get('radius', {}).get('accounting_nas_identifier2', 'cisco-acct-2'),
        'radius_port': n4.get('radius_port', 3799),
        'router_asn': router_asn,
        'router_interfaces': list(router_interfaces.values()),
        'bgp_aspath_prepend': bgp.get('aspath_prepend', True),
        'bgp_bfd_interval': bgp.get('bfd_interval', 250000),
        'bgp_bfd_min_rx': bgp.get('bfd_min_rx', 250000),
        'bgp_bfd_multiplier': bgp.get('bfd_multiplier', 3),
        'n4_interface': n4_interface,
        'n4_gateway': n4_gateway,
        'n4_policies': policies if n4_gateway else [],
    }
    cp['step2'] = step2
    return step2

def gdAgentReleaseRoot():
    return Path(__file__).resolve().parents[2]

def generatedConfigPath(name):
    config_dir = gdAgentReleaseRoot() / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    return str(config_dir / name)

def gdAgentTemplate(name):
    release_template_dir = gdAgentReleaseRoot() / "templates"
    release_template = release_template_dir / name
    if release_template.exists():
        environment = Environment(loader=FileSystemLoader(str(release_template_dir)))
        return environment.get_template(name)

    raise FileNotFoundError(f"template {name} not found in {release_template_dir}")

def readTextFile(path_value, context):
    path = Path(str(path_value)).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"{context} file not found: {path}")
    return path.read_text()

def remotePathCandidates(path_value, user):
    path_text = str(path_value)
    candidates = []

    def add(candidate):
        if candidate not in candidates:
            candidates.append(candidate)

    if path_text.startswith("~/"):
        add(f"/home/{user}/{path_text[2:]}")
    elif path_text == "~":
        add(f"/home/{user}")
    else:
        add(path_text)

    if not path_text.startswith("/") and not path_text.startswith("~"):
        add(f"/home/{user}/{path_text}")

    if user != "admin" and path_text.startswith("/home/admin/"):
        add(f"/home/{user}/{path_text[len('/home/admin/'):]}")

    return candidates

def remoteKeyPath(path_value, user):
    return remotePathCandidates(path_value, user)[0]

def connectRemoteSsh(host, port, user, password):
    try:
        import paramiko
    except ImportError as exc:
        raise ImportError(
            "paramiko is required to fetch SSH key files from inception_vm. "
            "Install with: python3 -m pip install paramiko"
        ) from exc

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=int(port),
        username=user,
        password=password,
        look_for_keys=False,
        allow_agent=False,
    )
    return client

def runRemoteCommand(client, command, context):
    stdin, stdout, stderr = client.exec_command(command)
    exit_status = stdout.channel.recv_exit_status()
    error_text = stderr.read().decode().strip()
    if exit_status != 0:
        raise RuntimeError(f"{context}: command failed with exit {exit_status}: {error_text}")

def remoteFileExists(sftp, path):
    try:
        sftp.stat(path)
        return True
    except FileNotFoundError:
        return False
    except IOError:
        return False

def ensureRemoteSshKeyPair(client, user, private_key_path, public_key_path):
    sftp = client.open_sftp()
    try:
        private_exists = remoteFileExists(sftp, private_key_path)
        public_exists = remoteFileExists(sftp, public_key_path)
    finally:
        sftp.close()

    if private_exists and public_exists:
        print(f"Using existing Inception SSH key pair: {private_key_path}")
        return

    key_dir = private_key_path.rsplit("/", 1)[0]
    command = (
        f"mkdir -p {shlex.quote(key_dir)} && "
        f"chmod 700 {shlex.quote(key_dir)} && "
        f"ssh-keygen -t rsa -b 4096 -f {shlex.quote(private_key_path)} -N '' -q && "
        f"chmod 600 {shlex.quote(private_key_path)} && "
        f"chmod 644 {shlex.quote(public_key_path)}"
    )
    print(f"Creating Inception SSH key pair: {private_key_path}")
    runRemoteCommand(client, command, "inception_vm SSH key generation")

def readRemoteTextFileFromClient(client, path_value, user, context):
    sftp = client.open_sftp()
    try:
        failures = []
        for candidate in remotePathCandidates(path_value, user):
            try:
                with sftp.open(candidate, "r") as remote_file:
                    content = remote_file.read()
                    if isinstance(content, bytes):
                        content = content.decode()
                    return content
            except FileNotFoundError as exc:
                failures.append(f"{candidate}: {exc}")
        raise FileNotFoundError("; ".join(failures))
    finally:
        sftp.close()

def loadSshKeyMaterial(data):
    node_defaults = data.setdefault('cnbng_cp', {}).setdefault('node_defaults', {})
    inception = data.get('inception_vm')

    if inception:
        host = inception.get('ip')
        user = inception.get('user')
        password = inception.get('password')
        port = inception.get('port', 22)
        if not host or not user or not password:
            raise ValueError("inception_vm.ip, inception_vm.user, and inception_vm.password are required")

        node_defaults.setdefault('ssh_private_key_file', f"/home/{user}/.ssh/id_rsa")
        node_defaults.setdefault('ssh_public_key_file', str(node_defaults['ssh_private_key_file']) + '.pub')
        private_key_path = remoteKeyPath(node_defaults['ssh_private_key_file'], user)
        public_key_path = remoteKeyPath(node_defaults['ssh_public_key_file'], user)

        client = connectRemoteSsh(host, port, user, password)
        try:
            ensureRemoteSshKeyPair(client, user, private_key_path, public_key_path)

            if not node_defaults.get('ssh_private_key'):
                node_defaults['ssh_private_key_file'] = private_key_path
                node_defaults['ssh_private_key'] = readRemoteTextFileFromClient(
                    client,
                    private_key_path,
                    user,
                    'cnbng_cp.node_defaults.ssh_private_key_file'
                )

            if not node_defaults.get('ssh_public_key'):
                node_defaults['ssh_public_key_file'] = public_key_path
                node_defaults['ssh_public_key'] = readRemoteTextFileFromClient(
                    client,
                    public_key_path,
                    user,
                    'cnbng_cp.node_defaults.ssh_public_key_file'
                )
        finally:
            client.close()
    else:
        if not node_defaults.get('ssh_private_key') and node_defaults.get('ssh_private_key_file'):
            node_defaults['ssh_private_key'] = readTextFile(
                node_defaults['ssh_private_key_file'],
                'cnbng_cp.node_defaults.ssh_private_key_file'
            )

        if not node_defaults.get('ssh_public_key') and node_defaults.get('ssh_public_key_file'):
            node_defaults['ssh_public_key'] = readTextFile(
                node_defaults['ssh_public_key_file'],
                'cnbng_cp.node_defaults.ssh_public_key_file'
            )

    if not node_defaults.get('ssh_private_key'):
        raise ValueError(
            "cnbng_cp.node_defaults.ssh_private_key or ssh_private_key_file is required "
            "for 3-server deployment"
        )
    if not node_defaults.get('ssh_public_key'):
        raise ValueError(
            "cnbng_cp.node_defaults.ssh_public_key or ssh_public_key_file is required "
            "for 3-server deployment"
        )

def sendConfigNetconf(host, port, user, password, config_file):
    m = manager.connect(host=host, port=port, username=user, password=password,
                         hostkey_verify=False, device_params={'name':'default'},
                         look_for_keys=False, allow_agent=False)

    config_file = open(config_file, "r")
    rpc = config_file.read()
    config_file.close()

    reply = m.edit_config(rpc, target='candidate')
    print("RPC reply from "+host+" :")
    print(reply)
    reply = m.commit()
    print(reply)

def hasStep2NetconfConfig(host, port, user, password):
    m = manager.connect(host=host, port=port, username=user, password=password,
                         hostkey_verify=False, device_params={'name':'default'},
                         look_for_keys=False, allow_agent=False)
    try:
        root = ET.fromstring(m.get_config(source='running').xml)
    finally:
        m.close_session()

    markers = [
        ".//{http://cisco.com/cisco-smi-cdl}cdl/{http://cisco.com/cisco-smi-cdl}datastore",
        ".//{http://tail-f.com/cisco-mobile-infra}local-instance",
        ".//{http://tail-f.com/cisco-mobile-infra}router",
        ".//{http://tail-f.com/cisco-mobile-infra}instance/{http://tail-f.com/cisco-mobile-infra}instance-id",
    ]
    return any(root.find(marker) is not None for marker in markers)

def deploy_cndp_3server(data):
    bannerText("Create XML Config files from templates for cnBNG CP cluster deployment")
    print("1. Creating cluster-config_cndp_3server_geo-red.xml")
    loadSshKeyMaterial(data)
    template = gdAgentTemplate("cluster-config_cndp_3server_geo-red.j2")
    filename = generatedConfigPath('cluster-config_cndp_3server_geo-red.xml')
    content = template.render(data)
    
    with open(filename, mode="w") as message:
        message.write(content)
    
    print("\n")
    bannerText("Start cnBNG CP Cluster Deployment by applying config to SMI Deployer using netconf")
    
    # Apply configurations to SMI Deployer
    print("1. Pushing cnBNG CP Cluster Config XML to SMI Deployer")
    sendConfigNetconf(data['smi_deployer']['ip'],830,data['smi_deployer']['user'],data['smi_deployer']['password'],filename)

def render_cndp_3server_step2(data, filename):
    bannerText("Rendering cnBNG CP 3-server Ops Center step2 XML config")
    buildCndp3ServerStep2Model(data)
    template = gdAgentTemplate("bng-ops-center_step2-config_cndp_3server_geo-red.xml.j2")
    content = template.render(data)

    with open(filename, mode="w") as message:
        message.write(content)
    print("1. Created "+filename)

def init_cndp_3server(data, dry_run=False):
    filename = generatedConfigPath('bng-ops-center_step2-config_cndp_3server_geo-red.xml')
    render_cndp_3server_step2(data, filename)
    step2 = data['cnbng_cp']['step2']

    if dry_run:
        print("Step2 config rendered only; no Ops Center changes were applied.")
        return

    if not step2.get('opscenter_ip'):
        raise RuntimeError("cnbng_cp.cluster.master.vip2 is required as BNG Ops Center management IP for step2")
    opscenter_password = data['cnbng_cp']['cluster'].get('opscenter_password')
    if not opscenter_password or opscenter_password == 'REPLACE_WITH_OPSCENTER_PASSWORD':
        raise RuntimeError("Cluster.opscenter_password must be set in the XLSX before applying step2")

    print("2. Applying step2 XML config to BNG Ops Center using NETCONF")
    if hasStep2NetconfConfig(
        step2['opscenter_ip'],
        step2['opscenter_netconf_port'],
        'admin',
        opscenter_password,
    ):
        print("Step2 XML config already exists on BNG Ops Center; skipping XML apply.")
    else:
        sendConfigNetconf(
            step2['opscenter_ip'],
            step2['opscenter_netconf_port'],
            'admin',
            opscenter_password,
            filename,
        )
        print("Step2 XML config applied.")
