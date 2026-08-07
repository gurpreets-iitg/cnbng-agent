# CNBNG 3-Server Release Notes

## v0.1.0-3server

Initial published release for geo-redundant 3-server bare-metal cnBNG control-plane deployments.

Included:

- XLSX-driven config intake for one CP cluster per workbook.
- Day0 step1 SMI cluster config generation and push.
- Day0 step2 BNG Ops Center delta config generation and apply.
- SMI sync-log watcher.
- Deployment cleanup for SMI cluster config and UCS virtual drives.
- Postcheck for node interfaces, local VIPs, BGP peer reachability, and inter-cluster IntTCP/CDL reachability.
- Operator install support through `install.sh` and `requirements.txt`.
- Sanitized XLSX template for customer input.

Excluded:

- Customer-specific workbooks and logs.
- AIO/1-server deployment flows.
- VMware deployment flows.
- SMI deployer lifecycle management.
- Single-workbook geo-pair intake.
- Day1/Day2 provisioning workflows.
