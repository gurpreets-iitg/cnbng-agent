# CNBNG 3-Server Architecture

## Flow

```text
XLSX workbook
  -> workbook resolver
  -> XLSX parser
  -> internal deployment model
  -> preflight validators
  -> Day0 XML renderer
  -> SMI deployer NETCONF push
  -> SMI sync-log watcher
  -> Day0 step2 BNG Ops Center config
  -> postcheck
```

Generated YAML and XML are implementation artifacts. Operators should normally work only with the XLSX workbook and `./bin/cnbng`.

## Release Layout

```text
bin/cnbng
src/cnbng/
templates/
excel-inputs/
docs/
requirements.txt
install.sh
VERSION
```

Runtime output is written under release-local `runs/` and `config/` directories. These directories are ignored by git.

## Command Groups

```text
./bin/cnbng check <profile-or-xlsx> [1|2]
./bin/cnbng deploy step1 <profile-or-xlsx> [1|2]
./bin/cnbng watch <profile-or-xlsx> [1|2]
./bin/cnbng deploy step2 <profile-or-xlsx> [1|2]
./bin/cnbng cleanup <profile-or-xlsx> [1|2]
./bin/cnbng postcheck <profile-or-xlsx> [1|2]
```

Profile names resolve from `excel-inputs/`. For geo-redundant deployments, keep one workbook per control-plane cluster.

## Workbook Resolution

When a cluster selector is provided, CNBNG resolves names such as:

```text
<profile>_cnbng_3server_deployment_cp1.xlsx
<profile>_cnbng_3server_deployment_cp2.xlsx
<profile>_cnbng_3server_deployment_cluster_cp1.xlsx
<profile>_cnbng_3server_deployment_cluster_cp2.xlsx
```

A non-suffixed workbook can still be used for a single cluster profile.

## Preflight

Preflight validates workbook structure, required fields, IP/CIDR syntax, duplicate owned addresses, peer workbook conflicts, expected interface definitions, BGP leaf-mode consistency, and the geo-redundant N4 VIP rule.

The Inception VM access check verifies:

- Route presence toward planned UCS management and k8s networks.
- TCP access to each CIMC IP.

Planned OS management and k8s IPs are not required to answer TCP before Day0 because they may be assigned during deployment.

## Day0 Step1

Day0 step1 renders the 3-server SMI cluster XML from the workbook model and pushes it to the SMI deployer through NETCONF. After the push, the watcher streams:

```text
monitor sync-logs <cluster-name>
```

The watcher is intentionally simple in this release. It does not run background remediation.

## Day0 Step2

Day0 step2 renders BNG Ops Center delta config and applies it through NETCONF. It avoids default SMI-created cluster deployment blocks and focuses on BNG application configuration needed after the control plane is deployed.

Step2 includes service, geo-redundancy, endpoint, routing/BGP, and profile radius configuration derived from the workbook model.

## Cleanup

Cleanup removes the SMI cluster config and SMI-created UCS virtual drives. It does not alter Inception VM networking or netplan.

## Safety Model

Default validation is read-only. Commands that change infrastructure state are explicit operator actions:

- `deploy step1`
- `deploy step2`
- `cleanup`

Generated files, logs, customer workbooks, and secrets should not be published with the release.
