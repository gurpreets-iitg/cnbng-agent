# CNBNG 3-Server Deployment Agent

CNBNG helps operators deploy a geo-redundant cnBNG control-plane pair on bare-metal UCS servers. It uses an XLSX workbook as the deployment input, generates the required Day0 XML, pushes the cluster configuration to an existing SMI deployer, applies the BNG Ops Center Day0 step2 configuration, and runs post-deployment checks.

Use this agent for the 3-server control-plane deployment model. It assumes the SMI deployer already exists and does not deploy AIO/1-server, VMware, or subscriber provisioning workflows.

## Deployment Model

A geo-redundant deployment has two control-plane clusters:

- CP1: three UCS servers.
- CP2: three UCS servers.
- One existing SMI deployer reachable by the deployment environment, or one SMI deployer per CP site.
- One XLSX workbook per CP cluster.

The two CP clusters must share the same N4 service VIPs because those VIPs are advertised for user-plane and external service reachability. Cluster-local node IPs, management IPs, k8s IPs, BGP peering IPs, IntTCP subnets, and CDL subnets are filled per workbook.

## Prerequisites

Before running CNBNG, make sure these are ready:

- Python 3.9 or later is installed on the operator workstation or deployment host.
- SMI deployer is already installed on the Inception VM and reachable. See the [Inception Server (SMI Deployer) Deployment Guide](https://xrdocs.io/cnbng/tutorials/inception-server-deployment-guide) if the Inception VM or SMI deployer still needs to be prepared.
- SMI deployer services are reachable through the configured deployer management IP.
- Inception VM can route to the planned UCS management and k8s networks.
- Inception VM can reach each UCS CIMC IP.
- UCS CIMC credentials are valid.
- Required BNG, CEE, and host-profile images are available from URLs reachable by the SMI deployment workflow.
- Leaf switch VLANs, BGP peering links, and geo-redundancy transport are provisioned.

## Install

Run the installer from the package directory:

```bash
./install.sh
source .venv/bin/activate
./bin/cnbng --version
```

The installer creates a local `.venv` and installs the Python packages listed in `requirements.txt`. Keep the virtual environment activated when running CNBNG commands.

If the deployment host cannot reach the Python package index, pre-stage the required wheels locally and install them into `.venv` using your site-approved offline Python package process.

## Workbook

Start from the template:

```bash
excel-inputs/cnbng_3server_template.xlsx
```

For a geo-redundant pair, create one workbook per CP cluster. Recommended names:

```text
<profile>_cnbng_3server_deployment_cp1.xlsx
<profile>_cnbng_3server_deployment_cp2.xlsx
```

Example:

```text
site_cnbng_3server_deployment_cp1.xlsx
site_cnbng_3server_deployment_cp2.xlsx
```

When you pass `site 1`, CNBNG resolves the CP1 workbook. When you pass `site 2`, CNBNG resolves the CP2 workbook. You can also pass a full XLSX path directly.

CNBNG prints the resolved XLSX path before it acts. Check this path carefully before running a command that changes infrastructure.

## Normal Workflow

Run these commands from the package directory.

1. Validate each workbook:

```bash
./bin/cnbng check site 1
./bin/cnbng check site 2
```

2. Deploy CP1 and CP2 Day0 step1:

```bash
./bin/cnbng deploy step1 site 1
./bin/cnbng deploy step1 site 2
```

3. Reattach to the deployment watcher if needed:

```bash
./bin/cnbng watch site 1
./bin/cnbng watch site 2
```

4. Apply Day0 step2 after each cluster is deployed:

```bash
./bin/cnbng deploy step2 site 1
./bin/cnbng deploy step2 site 2
```

5. Run post-deployment checks for the CP pair:

```bash
./bin/cnbng postcheck site
```

## Commands

### `check`

Validates the workbook without pushing cluster config.

```bash
./bin/cnbng check <profile-or-xlsx> [1|2]
```

The check validates workbook structure, required fields, IP/CIDR syntax, duplicate owned addresses, peer workbook conflicts, expected interface definitions, BGP leaf-mode consistency, and the geo-redundant N4 VIP rule. It also verifies that the Inception VM can route toward the planned UCS management and k8s networks and can reach CIMC TCP endpoints.

### `deploy step1`

Generates and pushes the SMI cluster XML, then starts the SMI sync-log watcher.

```bash
./bin/cnbng deploy step1 <profile-or-xlsx> [1|2]
```

Step1 performs workbook resolution, YAML generation, preflight checks, SMI cluster XML rendering, NETCONF push to the SMI deployer, and sync-log monitoring.

Management and k8s OS IPs may not answer before Day0 because they can be assigned during cluster deployment. CNBNG checks route availability to those networks instead. CIMC IPs must already be reachable.

### `watch`

Reattaches to SMI deployment progress for a cluster.

```bash
./bin/cnbng watch <profile-or-xlsx> [1|2]
```

This streams the SMI deployer command:

```text
monitor sync-logs <cluster-name>
```

### `deploy step2`

Applies the BNG Ops Center Day0 step2 delta through NETCONF.

```bash
./bin/cnbng deploy step2 <profile-or-xlsx> [1|2]
```

Step2 configures the BNG application runtime, geo-redundancy, CDL, routing/BGP, endpoint geo settings, profile radius attributes, and related service configuration. It avoids reapplying default cluster deployment blocks created during Day0 step1.

### `cleanup`

Removes deployment state for a failed or abandoned attempt.

```bash
./bin/cnbng cleanup <profile-or-xlsx> [1|2]
```

Cleanup removes the SMI cluster config with `no clusters <cluster-name>` and checks/deletes SMI-created UCS virtual drives through CIMC after interactive confirmation. It does not modify Inception VM networking, netplan, host IP addresses, routes, DNS, or `/etc/hosts`.

### `postcheck`

Validates the deployed cluster.

```bash
./bin/cnbng postcheck <profile-or-xlsx>
```

Postcheck resolves the CP1 and CP2 workbooks for the profile and validates UCS OS interface presence, local interface IPs, local VIPs, BGP peer reachability from proto nodes, and geo IntTCP/CDL reachability between CP clusters.

## Generated Files

CNBNG writes generated YAML/XML and command logs under package-local `runs/` and `config/` directories. These files are runtime artifacts and can be used for troubleshooting after a run.
