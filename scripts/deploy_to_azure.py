#!/usr/bin/env python3
"""Build, push, and deploy the Taiga MCP container to Azure Container Apps.

This helper script wraps the docker build/push workflow and the `az containerapp`
update command so operators can redeploy the MCP server with a single invocation.
Environment variables provide sensible defaults for all parameters and can be
overridden via command line flags when needed.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Sequence


def _run(command: Sequence[str]) -> None:
    """Execute a command, streaming its output."""
    print(f"$ {' '.join(command)}")
    subprocess.run(command, check=True)


def _require_value(name: str, value: str | None) -> str:
    if value:
        return value
    env_value = os.environ.get(name)
    if not env_value:
        raise SystemExit(f"Missing required value for {name}. Provide --{name.lower().replace('_', '-')} or set {name}.")
    return env_value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy the Taiga MCP container to Azure Container Apps")
    parser.add_argument("--image", help="Container image name (e.g. ghcr.io/org/taiga-mcp)")
    parser.add_argument("--tag", help="Container image tag to deploy", default=None)
    parser.add_argument("--resource-group", help="Azure resource group name", default=None)
    parser.add_argument("--container-app", help="Azure Container App name", default=None)
    parser.add_argument("--skip-build", action="store_true", help="Skip docker build step")
    parser.add_argument("--skip-push", action="store_true", help="Skip docker push step")
    parser.add_argument(
        "--latest-tag",
        default="latest",
        help="Optional secondary tag to push alongside --tag (default: latest)",
    )
    parser.add_argument("--context", default=".", help="Build context path (default: current directory)")

    args = parser.parse_args(argv)

    image = _require_value("CONTAINER_IMAGE", args.image)
    tag = _require_value("IMAGE_TAG", args.tag)
    resource_group = _require_value("AZURE_RESOURCE_GROUP", args.resource_group)
    container_app = _require_value("AZURE_CONTAINER_APP", args.container_app)

    if not args.skip_build:
        _run(["docker", "build", "-t", f"{image}:{tag}", "-t", f"{image}:{args.latest_tag}", args.context])

    if not args.skip_push:
        _run(["docker", "push", f"{image}:{tag}"])
        if args.latest_tag:
            _run(["docker", "push", f"{image}:{args.latest_tag}"])

    _run(
        [
            "az",
            "containerapp",
            "update",
            "--resource-group",
            resource_group,
            "--name",
            container_app,
            "--image",
            f"{image}:{tag}",
        ]
    )

    print("Deployment command executed. Use 'az containerapp revision list' to verify the active revision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
