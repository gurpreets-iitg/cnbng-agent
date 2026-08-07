# __________________________________________________
#
# Cloud Native BNG / Cisco Subscriber Edge
# Control Plane Deployment Agent
#
# Author: Gurpreet Dhaliwal, TME MiG
# __________________________________________________

"""CNBNG command-line interface."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from cnbng import __version__
from cnbng import day0_workflow
from cnbng.input_resolver import normalize_cluster_selector, selected_input
from cnbng import deployment_cleanup
from cnbng import postcheck


RELEASE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TIMEOUT = 15
DEFAULT_WATCH_TIMEOUT = 7200
DEFAULT_SMI_CLI_PORT = 2022


def print_branding_banner() -> None:
    print("CNBNG | Cisco Subscriber Edge")
    print(f"Control Plane Deployment Agent v{__version__}")
    print("-" * 48)


def ns(**values: object) -> argparse.Namespace:
    return argparse.Namespace(**values)


def day0_defaults(input_value: str, *, cluster: str | None = None, dry_run: bool = False, verbose: bool = False) -> argparse.Namespace:
    cluster_selector = normalize_cluster_selector(cluster)
    return ns(
        deployment="3s",
        input=selected_input(input_value, cluster_selector),
        cluster=cluster_selector,
        run_id=None,
        run_base=None,
        dry_run=dry_run,
        verbose=verbose,
        skip_preflight=False,
        no_watch=False,
        watch_timeout=DEFAULT_WATCH_TIMEOUT,
        watch_poll_interval=2.0,
        watch_connect_timeout=DEFAULT_TIMEOUT,
        watch_smi_host=None,
        watch_smi_user=None,
        watch_smi_password=None,
        watch_smi_port=DEFAULT_SMI_CLI_PORT,
    )


def simple_day0_deploy(args: argparse.Namespace) -> int:
    deploy_args = day0_defaults(
        args.input,
        cluster=getattr(args, "cluster", None),
        dry_run=getattr(args, "dry_run", False),
        verbose=getattr(args, "verbose", False),
    )
    return day0_workflow.deploy(deploy_args)


def simple_day0_check(args: argparse.Namespace) -> int:
    return day0_workflow.deploy(day0_defaults(args.input, cluster=getattr(args, "cluster", None), dry_run=True))


def simple_day0_watch(args: argparse.Namespace) -> int:
    cluster_selector = normalize_cluster_selector(getattr(args, "cluster", None))
    watch_args = ns(
        deployment="3s",
        input=selected_input(args.input, cluster_selector),
        cluster=cluster_selector,
        run_id=None,
        run_base=None,
        verbose=False,
        dry_run=False,
        watch_timeout=DEFAULT_WATCH_TIMEOUT,
        watch_poll_interval=2.0,
        watch_connect_timeout=DEFAULT_TIMEOUT,
        watch_smi_host=None,
        watch_smi_user=None,
        watch_smi_password=None,
        watch_smi_port=DEFAULT_SMI_CLI_PORT,
    )
    return day0_workflow.watch(watch_args)

def simple_day0_step2(args: argparse.Namespace) -> int:
    cluster_selector = normalize_cluster_selector(getattr(args, "cluster", None))
    step2_args = ns(
        deployment="3s",
        input=selected_input(args.input, cluster_selector),
        cluster=cluster_selector,
        run_id=None,
        run_base=None,
        dry_run=getattr(args, "dry_run", False),
        verbose=getattr(args, "verbose", False),
        skip_preflight=False,
    )
    return day0_workflow.step2(step2_args)


def require_deploy_step(args: argparse.Namespace) -> int:
    print("ERROR: deploy requires an explicit step: step1 or step2", file=sys.stderr)
    print("Examples:", file=sys.stderr)
    print("  ./bin/cnbng deploy step1 site 1", file=sys.stderr)
    print("  ./bin/cnbng deploy step2 site 1", file=sys.stderr)
    return 2


def simple_ucs_cleanup(args: argparse.Namespace) -> int:
    cleanup_args = ns(
        deployment="3s",
        input=args.input,
        cluster=getattr(args, "cluster", None),
        run_id=None,
        run_base=None,
        apply=True,
        verbose=False,
        cluster_name=None,
        smi_host=None,
        smi_user=None,
        smi_password=None,
        smi_port=DEFAULT_SMI_CLI_PORT,
        smi_delete_command_template="no clusters {cluster_name}",
        timeout=DEFAULT_TIMEOUT,
        cimc_timeout=10,
    )
    return deployment_cleanup.cleanup(cleanup_args)


def simple_postcheck(args: argparse.Namespace) -> int:
    return postcheck.run(args)


def build_parser() -> argparse.ArgumentParser:
    prog = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else "cnbng"
    parser = argparse.ArgumentParser(prog=prog, description="CNBNG for cnBNG geo-redundant deployments.")
    parser.add_argument("--version", action="version", version=f"cnbng {__version__}")

    subparsers = parser.add_subparsers(dest="command", metavar="{deploy,check,watch,cleanup,postcheck}")

    simple_check = subparsers.add_parser("check", help="Validate deployment input before deploy.")
    simple_check.add_argument("input", help="Deployment XLSX path or profile name, for example site.")
    simple_check.add_argument("cluster", nargs="?", help="Optional cluster selector: 1 or 2.")
    simple_check.set_defaults(func=simple_day0_check)

    simple_deploy = subparsers.add_parser("deploy", help="Run deployment steps from XLSX.")
    simple_deploy.set_defaults(func=require_deploy_step)
    simple_deploy_sub = simple_deploy.add_subparsers(dest="deploy_step", metavar="{step1,step2}")
    deploy_step1 = simple_deploy_sub.add_parser("step1", help="Run Step 1: push cluster config to SMI deployer.")
    deploy_step1.add_argument("input", help="Deployment XLSX path or profile name, for example site.")
    deploy_step1.add_argument("cluster", nargs="?", help="Optional cluster selector: 1 or 2.")
    deploy_step1.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    deploy_step1.add_argument("--verbose", action="store_true", help=argparse.SUPPRESS)
    deploy_step1.set_defaults(func=simple_day0_deploy)
    deploy_step2 = simple_deploy_sub.add_parser("step2", help="Run Step 2: apply BNG Ops Center config.")
    deploy_step2.add_argument("input", help="Deployment XLSX path or profile name, for example site.")
    deploy_step2.add_argument("cluster", nargs="?", help="Optional cluster selector: 1 or 2.")
    deploy_step2.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    deploy_step2.add_argument("--verbose", action="store_true", help=argparse.SUPPRESS)
    deploy_step2.set_defaults(func=simple_day0_step2)

    simple_watch = subparsers.add_parser("watch", help="Watch Day0 deploy from XLSX.")
    simple_watch.add_argument("input", help="Deployment XLSX path or profile name, for example site.")
    simple_watch.add_argument("cluster", nargs="?", help="Optional cluster selector: 1 or 2.")
    simple_watch.set_defaults(func=simple_day0_watch)

    simple_cleanup = subparsers.add_parser("cleanup", help="Clean SMI cluster config and UCS virtual drives.")
    simple_cleanup.add_argument("input", help="Deployment XLSX path or profile name, for example site.")
    simple_cleanup.add_argument("cluster", nargs="?", help="Optional cluster selector: 1 or 2.")
    simple_cleanup.set_defaults(func=simple_ucs_cleanup)

    simple_postcheck_parser = subparsers.add_parser("postcheck", help="Validate deployed UCS interfaces and geo network reachability.")
    postcheck.add_args(simple_postcheck_parser)
    simple_postcheck_parser.set_defaults(func=simple_postcheck)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "func"):
        parser.print_help()
        return 2

    print_branding_banner()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
