# __________________________________________________
#
# Cloud Native BNG / Cisco Subscriber Edge
# Control Plane Deployment Agent
#
# Author: Gurpreet Dhaliwal, TME MiG
# __________________________________________________

"""Shared XLSX input resolution for CNBNG commands."""

from __future__ import annotations

from pathlib import Path


def release_root() -> Path:
    return Path(__file__).resolve().parents[2]


def normalize_cluster_selector(value: str | None) -> str | None:
    if not value:
        return None
    normalized = str(value).strip().lower().replace("_", "").replace("-", "")
    if normalized in ("1", "cp1", "cluster1", "cluster01", "site1"):
        return "1"
    if normalized in ("2", "cp2", "cluster2", "cluster02", "site2"):
        return "2"
    raise ValueError(f"cluster selector must be 1 or 2, got {value!r}")


def cluster_candidate_paths(value: str, cluster_selector: str | None) -> list[Path]:
    if cluster_selector == "1":
        suffixes = ("cp1", "cluster_cp1", "cluster1", "cluster_1")
    elif cluster_selector == "2":
        suffixes = ("cp2", "cluster_cp2", "cluster2", "cluster_2")
    else:
        suffixes = ()
    candidates = [
        release_root() / "excel-inputs" / f"{value}_cnbng_3server_deployment_{suffix}.xlsx"
        for suffix in suffixes
    ]
    candidates.append(release_root() / "excel-inputs" / f"{value}_cnbng_3server_deployment.xlsx")
    candidates.append(release_root() / "excel-inputs" / f"{value}.xlsx")
    return candidates


def resolve_input(value: str, cluster: str | None = None) -> str:
    path = Path(value)
    if path.exists():
        return str(path)
    cluster_selector = normalize_cluster_selector(cluster)
    if not value.endswith(".xlsx"):
        candidates = cluster_candidate_paths(value, cluster_selector)
        if not cluster_selector:
            candidates.extend(
                [
                    release_root() / "excel-inputs" / f"{value}_cnbng_3server_deployment.xlsx",
                    release_root() / "excel-inputs" / f"{value}_cnbng_3server_deployment_cp1.xlsx",
                    release_root() / "excel-inputs" / f"{value}_cnbng_3server_deployment_cluster_cp1.xlsx",
                    release_root() / "excel-inputs" / f"{value}.xlsx",
                ]
            )
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
    return value


def selected_input(value: str, cluster: str | None = None) -> str:
    resolved = resolve_input(value, cluster)
    print(f"Using XLSX: {resolved}")
    return resolved
