#!/usr/bin/env python3
"""Ober status command - show current state."""

import json
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from ober.config import OberConfig
from ober.system import ServiceInfo, get_haproxy_version

console = Console()


@click.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show current status of Ober services.

    Displays BGP session status, HAProxy health, announced routes,
    and systemd service status.
    """
    parent_ctx = ctx.obj
    json_output = parent_ctx.json_output if parent_ctx else False

    config = OberConfig.load()
    result: dict[str, Any] = {
        "services": {},
        "bgp": {},
        "keepalived": {},
        "haproxy": {},
        "config": {},
    }

    # Service status
    http_service = ServiceInfo.from_service_name("ober-http")
    bgp_service = ServiceInfo.from_service_name("ober-bgp")
    ka_service = ServiceInfo.from_service_name("ober-ha")

    result["services"]["ober-http"] = {
        "active": http_service.is_active,
        "enabled": http_service.is_enabled,
        "status": http_service.status,
        "pid": http_service.pid,
    }

    # HA service based on mode
    if config.ha_mode == "bgp":
        result["services"]["ober-bgp"] = {
            "active": bgp_service.is_active,
            "enabled": bgp_service.is_enabled,
            "status": bgp_service.status,
            "pid": bgp_service.pid,
        }
    else:
        result["services"]["ober-ha"] = {
            "active": ka_service.is_active,
            "enabled": ka_service.is_enabled,
            "status": ka_service.status,
            "pid": ka_service.pid,
        }

    # HAProxy info
    result["haproxy"]["version"] = get_haproxy_version()
    result["haproxy"]["config_exists"] = config.haproxy_config_path.exists()

    # BGP or Keepalived info based on mode
    if config.ha_mode == "bgp":
        from ober.system import get_exabgp_version

        result["bgp"]["version"] = get_exabgp_version()
        result["bgp"]["config_exists"] = config.bgp_config_path.exists()
    else:
        from ober.system import get_keepalived_version

        result["keepalived"]["version"] = get_keepalived_version()
        result["keepalived"]["config_exists"] = config.keepalived_config_path.exists()

    # Get BGP announced routes (if service is running in BGP mode)
    if config.ha_mode == "bgp" and bgp_service.is_active:
        result["bgp"]["announced_routes"] = _get_announced_routes()
    else:
        result["bgp"]["announced_routes"] = []

    # Get VRRP state (if in keepalived mode and service is running)
    if config.ha_mode == "keepalived" and ka_service.is_active:
        result["keepalived"]["vrrp_state"] = _get_vrrp_state()
    else:
        result["keepalived"]["vrrp_state"] = {}

    # Config info
    result["config"]["exists"] = config.config_path.exists()
    result["config"]["path"] = str(config.config_path)
    if config.vips:
        result["config"]["vips"] = [v.address for v in config.vips]
    if config.backends:
        result["config"]["backends"] = [b.name for b in config.backends]

    # HAProxy stats (if running)
    if http_service.is_active:
        result["haproxy"]["stats"] = _get_haproxy_stats(config.stats_port)

    if json_output:
        click.echo(json.dumps(result, indent=2, default=str))
    else:
        ha_service = bgp_service if config.ha_mode == "bgp" else ka_service
        ha_name = "ober-bgp" if config.ha_mode == "bgp" else "ober-ha"
        _print_status(result, http_service, ha_service, ha_name)


def _get_announced_routes() -> list[str]:
    """Get currently announced BGP routes."""
    # This would parse ExaBGP state, for now return empty
    # In production, we'd query ExaBGP's API or parse its state
    return []


def _get_vrrp_state() -> dict[str, str]:
    """Get VRRP state for each instance from journalctl.

    Returns:
        Dict mapping instance name (e.g., "VI_1") to state ("MASTER" or "BACKUP")
    """
    import re
    import subprocess

    states: dict[str, str] = {}

    try:
        # Get recent keepalived logs
        result = subprocess.run(
            ["journalctl", "-u", "ober-ha", "-n", "100", "--no-pager", "-q"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            # Parse for state transitions - find the most recent state for each instance
            # Pattern: "VI_1 Entering MASTER STATE" or "VI_1 Entering BACKUP STATE"
            pattern = re.compile(
                r"(VI_\d+)\s+(?:Entering|entering)\s+(MASTER|BACKUP)\s+STATE", re.I
            )
            for line in result.stdout.split("\n"):
                match = pattern.search(line)
                if match:
                    instance = match.group(1)
                    state = match.group(2).upper()
                    states[instance] = state
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return states


def _get_haproxy_stats(port: int) -> dict[str, Any]:
    """Get HAProxy stats from the stats endpoint."""
    import requests

    try:
        resp = requests.get(f"http://127.0.0.1:{port}/stats;json", timeout=2)
        if resp.status_code == 200:
            data: dict[str, Any] = resp.json()
            return data
    except Exception:
        pass
    return {}


def _print_status(
    result: dict[str, Any],
    http_service: ServiceInfo,
    ha_service: ServiceInfo,
    ha_name: str,
) -> None:
    """Print status in a human-readable format."""
    console.print()
    console.print("[bold]Ober Status[/bold]")
    console.print()

    # Services table
    table = Table(title="Services", show_header=True, header_style="bold")
    table.add_column("Service")
    table.add_column("Status")
    table.add_column("PID")
    table.add_column("Enabled")

    for name, service in [("ober-http", http_service), (ha_name, ha_service)]:
        status_str = (
            f"[green]{service.status}[/green]"
            if service.is_active
            else f"[red]{service.status}[/red]"
        )
        pid_str = str(service.pid) if service.pid else "-"
        enabled_str = "[green]yes[/green]" if service.is_enabled else "[dim]no[/dim]"
        table.add_row(name, status_str, pid_str, enabled_str)

    console.print(table)
    console.print()

    # Component versions
    haproxy_ver = result["haproxy"].get("version", "not installed")
    console.print(f"[bold]HAProxy:[/bold] {haproxy_ver}")

    # Show HA component version based on mode
    if ha_name == "ober-bgp":
        exabgp_ver = result["bgp"].get("version", "not installed")
        console.print(f"[bold]ExaBGP:[/bold] {exabgp_ver}")
    else:
        keepalived_ver = result["keepalived"].get("version", "not installed")
        console.print(f"[bold]Keepalived:[/bold] {keepalived_ver}")

    console.print()

    # Configuration
    if result["config"]["exists"]:
        console.print(f"[bold]Config:[/bold] {result['config']['path']}")
        if result["config"].get("vips"):
            console.print(f"[bold]VIPs:[/bold] {', '.join(result['config']['vips'])}")
        if result["config"].get("backends"):
            console.print(f"[bold]Backends:[/bold] {', '.join(result['config']['backends'])}")
    else:
        console.print("[yellow]Configuration not found. Run 'ober config' to configure.[/yellow]")

    console.print()

    # BGP routes (only shown in BGP mode)
    routes = result["bgp"].get("announced_routes", [])
    if routes:
        console.print(f"[bold]Announced Routes:[/bold] {', '.join(routes)}")
    elif ha_name == "ober-bgp" and ha_service.is_active:
        console.print("[bold]Announced Routes:[/bold] [dim]none[/dim]")

    # VRRP state (only shown in keepalived mode)
    vrrp_state = result["keepalived"].get("vrrp_state", {})
    if vrrp_state:
        console.print()
        console.print("[bold]VRRP State:[/bold]")
        for instance, state in sorted(vrrp_state.items()):
            state_color = "green" if state == "MASTER" else "yellow"
            console.print(f"  {instance}: [{state_color}]{state}[/{state_color}]")
    elif ha_name == "ober-ha" and ha_service.is_active:
        console.print()
        console.print("[bold]VRRP State:[/bold] [dim]unknown (check logs)[/dim]")

    # Show systemd status output for verbose mode
    console.print()
    console.print("[dim]Use 'ober logs' to view service logs[/dim]")
