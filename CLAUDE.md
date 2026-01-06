# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Herr Ober ("Head Waiter") is a high-performance S3 ingress controller for Ceph RGW clusters. Uses HAProxy 3.3 (AWS-LC) for SSL offloading with two HA modes:
- **BGP/ECMP mode** (ExaBGP) - For environments with BGP-capable routers
- **Keepalived mode** (VRRP) - For environments without BGP support

- **PyPI package:** `herr-ober` (CLI command: `ober`)
- **Python:** 3.12+ required
- **Supported OS:** Ubuntu, Debian, RHEL 10+
- **Target:** Proxmox VMs achieving 50GB/s+ throughput

## Development Commands

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run all tests
pytest

# Run single test file
pytest tests/test_cli.py

# Run single test
pytest tests/test_cli.py::test_bootstrap -v

# Run with coverage
pytest --cov=ober --cov-report=term-missing

# Lint
ruff check .

# Auto-fix lint issues
ruff check . --fix

# Format
ruff format .

# Type check
mypy ober/
```

## Architecture

"Shared Nothing" cluster - each node operates independently with one of two HA modes:

**BGP/ECMP Mode:** Nodes announce a shared VIP via BGP; upstream router uses ECMP to distribute traffic.

**Keepalived Mode:** Multiple VIPs (one per node) with VRRP failover; DNS round-robin distributes clients.

Per-node components:
- **HAProxy 3.3 (AWS-LC)** - SSL termination, ACLs, proxies to Ceph RGW backends
- **ExaBGP** (BGP mode) - Announces VIP(s) to upstream router via BGP
- **Keepalived** (Keepalived mode) - VRRP-based VIP failover between nodes
- **ober CLI** - Python controller managing everything

Critical relationships:
- BGP mode: `ober-bgp.service` has `BindsTo=ober-http.service`. If HAProxy dies, BGP withdraws immediately.
- Keepalived mode: `ober-ha.service` has `BindsTo=ober-http.service`. If HAProxy dies, VIP fails over.

## Code Architecture

**CLI Layer** (`ober/cli.py`):
- Click-based CLI with `@click.group()` main entry point
- `Context` class holds shared state (verbose, quiet, json_output, config)
- Commands registered via `main.add_command()` from `ober/commands/`

**Command Modules** (`ober/commands/`):
- Each subcommand in separate file: `bootstrap.py`, `config.py`, `status.py`, etc.
- Commands use `@pass_context` decorator to access shared `Context`
- Service commands (`start`, `stop`, `restart`) all in `service.py`

**Configuration** (`ober/config.py`):
- Dataclass-based: `OberConfig` contains `BGPConfig`, `VIPConfig`, `BackendConfig`, `CertConfig`
- `OberConfig.load()` searches default paths, `OberConfig.save()` writes YAML
- Properties compute derived paths (`config_path`, `haproxy_config_path`, etc.)
- Secrets handled separately via `load_secrets()`/`save_secrets()` for `~/.ober/login`

**System Utilities** (`ober/system.py`):
- `SystemInfo` dataclass auto-detects OS family (DEBIAN/RHEL), version, local IP
- `ServiceInfo` wraps systemd service queries
- Helper functions: `get_haproxy_version()`, `get_exabgp_version()`, `run_command()`

## Key Implementation Notes

### Code Style
- Type annotations required throughout (strict mypy config)
- Google-style docstrings
- Linting/formatting via ruff (line length 100)

### CLI Behavior
- Exit codes: 0 success, 1 error
- Uses `click` framework, `rich` for output, `python-inquirer` for prompts
- Destructive ops (`uninstall`) require confirmation

### Configuration
- Format: YAML at `<install-path>/etc/ober.yaml`
- Secrets stored separately in `~/.ober/login` (permissions 600)
- Supports Slurm hostlists for node/router lists

### Testing Strategy
- Unit tests with mocked system calls
- Integration tests use `moto[server]` for mock S3 backends
- BGP-related code unit tested with mocked ExaBGP
- Minimum coverage: 50%

### Testing Patterns
Key fixtures in `tests/conftest.py`:
- `cli_runner` - Click CLI test runner for testing commands
- `temp_dir` / `temp_config` - Temporary test environments with isolated directories
- `mock_system_info` - Mock `SystemInfo` to simulate different OS environments (Ubuntu/RHEL)
- `mock_root_system_info` - Same as above but with `is_root = True` for testing privileged operations
- `mock_run_command` - Mock system command execution to avoid actual shell calls
- `mock_check_command_exists` - Mock command availability checks
- `sample_config` - Pre-configured `OberConfig` with BGP, VIPs, backends, and certs for testing

Pattern: All tests use these fixtures to avoid real system calls. Mock the system detection, command execution, and file operations consistently.

### Health Check Mechanism
**CRITICAL:** The `ober health <vip>` command is NOT run directly by users. It's spawned by ExaBGP as a process.

How it works:
1. ExaBGP starts `ober health <vip>` as a subprocess (configured in `bgp/config.ini`)
2. The health command continuously polls HAProxy's health endpoint (`http://127.0.0.1:8404/health`)
3. It outputs BGP commands to **stdout** using ExaBGP's text encoder format:
   - `announce route <vip>/32 next-hop self` - when HAProxy is healthy
   - `withdraw route <vip>/32 next-hop self` - when HAProxy fails
4. ExaBGP reads these commands from stdout and updates BGP routes accordingly
5. On SIGTERM/SIGINT, the process gracefully withdraws all routes before exiting

The health check is the bridge between HAProxy's operational state and BGP route announcements. If HAProxy fails, routes are withdrawn within ~1-2 seconds.

### Path Resolution Logic
**IMPORTANT:** Ober auto-detects whether it's running in a virtual environment (venv/pipx) vs a custom installation. This affects ALL config and certificate paths.

Detection logic (`ober/commands/bootstrap.py`):
```python
def _is_in_venv():
    return sys.prefix != sys.base_prefix

def _get_current_venv_path():
    if _is_in_venv():
        return Path(sys.prefix)
    return None
```

Behavior:
- **If in venv (pipx recommended):** Automatically uses `sys.prefix` as install path
  - Example: `~/.local/pipx/venvs/herr-ober/` becomes the base for all config/certs
  - Bootstrap command: `sudo ober bootstrap` (no path required)
- **If NOT in venv:** Requires explicit install path
  - Bootstrap command: `sudo ober bootstrap /opt/ober`
  - All paths derived from this explicit location

All config paths are computed as properties in `OberConfig`:
- `config_path = install_path / "etc" / "ober.yaml"`
- `haproxy_config_path = install_path / "etc" / "haproxy" / "haproxy.cfg"`
- `bgp_config_path = install_path / "etc" / "bgp" / "config.ini"`
- `cert_dir = install_path / "etc" / "certs"`

**There are NO hardcoded default paths.** This ensures predictable behavior across different installation methods.

### Key Paths
With pipx (recommended):
- `~/.local/pipx/venvs/herr-ober/etc/ober.yaml` - Main config
- `~/.local/pipx/venvs/herr-ober/etc/haproxy/haproxy.cfg` - HAProxy config
- `~/.local/pipx/venvs/herr-ober/etc/bgp/config.ini` - ExaBGP config
- `~/.local/pipx/venvs/herr-ober/etc/certs/` - SSL certificates

With custom installation (prompted during bootstrap):
- `<install-path>/etc/ober.yaml` - Main config
- `<install-path>/etc/haproxy/haproxy.cfg` - HAProxy config
- `<install-path>/etc/bgp/config.ini` - ExaBGP config
- `<install-path>/etc/certs/` - SSL certificates

**Note:** There are NO hardcoded default paths. When not in a venv, bootstrap requires an explicit path:
```bash
sudo ober bootstrap /path/to/install
```

### Systemd Services
- `ober-http.service` - HAProxy
- `ober-bgp.service` - ExaBGP (bound to ober-http) - BGP mode only
- `ober-ha.service` - Keepalived (bound to ober-http) - Keepalived mode only

---

## Keepalived Mode Implementation Plan

### Overview

Keepalived mode provides HA for environments where BGP is not available. It uses VRRP (Virtual Router Redundancy Protocol) for VIP failover between nodes.

**Key difference from BGP mode:**
- BGP mode: Single shared VIP, router distributes traffic via ECMP
- Keepalived mode: Multiple VIPs (one per node), DNS round-robin distributes clients

### Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Load balancing model | Multiple VIPs + DNS round-robin | Each node owns one VIP; if node fails, its VIP floats to another node |
| BGP vs Keepalived | Mutually exclusive | Simpler configuration and troubleshooting |
| Node priority | Equal priority (hash-based) | VIP ownership determined by hostname hash for consistency |
| Failback behavior | Preempt | Recovered nodes reclaim their original VIP immediately |
| VRRP communication | Unicast (default), multicast optional | Unicast works in all environments; multicast blocked by many networks |
| Authentication | None | Simplicity; internal network assumed trusted |
| Minimum nodes | 2 (recommended: 3) | Warn if fewer than 2 peers, but allow for testing |
| Package installation | Bootstrap installs both ExaBGP and keepalived | User chooses mode during `ober config` |
| DNS management | Out of scope | User manages DNS separately |

### VIP Assignment Algorithm

VIPs are assigned to nodes using a consistent hash based on hostname:

```python
def get_vip_owner(vip: str, nodes: list[str]) -> str:
    """Determine which node owns a VIP based on consistent hashing."""
    sorted_nodes = sorted(nodes)
    vip_hash = hash(vip) % len(sorted_nodes)
    return sorted_nodes[vip_hash]
```

This ensures:
- Deterministic assignment (same result on all nodes)
- Consistent even when nodes are added/removed
- Even distribution of VIPs across nodes

### Configuration Structure

New dataclass in `ober/config.py`:

```python
@dataclass
class KeepalivedConfig:
    """Keepalived/VRRP configuration."""
    enabled: bool = False
    peers: list[str] = field(default_factory=list)  # Other node IPs/hostnames
    interface: str = ""  # Network interface for VIP (auto-detected if empty)
    use_multicast: bool = False  # Default: unicast
    advert_int: int = 1  # VRRP advertisement interval (seconds)
```

Updated `OberConfig`:

```python
@dataclass
class OberConfig:
    # ... existing fields ...
    ha_mode: str = "bgp"  # "bgp" or "keepalived"
    keepalived: KeepalivedConfig = field(default_factory=KeepalivedConfig)
```

### Config File Example (`ober.yaml`)

```yaml
ha_mode: keepalived

keepalived:
  enabled: true
  peers:
    - 192.168.1.11
    - 192.168.1.12
    - 192.168.1.13
  interface: eth0  # Optional, auto-detected
  use_multicast: false
  advert_int: 1

vips:
  - 10.0.0.100
  - 10.0.0.101
  - 10.0.0.102

# ... rest of config (backends, certs, etc.)
```

### Generated Keepalived Config

Location: `<install-path>/etc/keepalived/keepalived.conf`

```conf
global_defs {
    router_id ober_<hostname>
    vrrp_skip_check_adv_addr
    vrrp_garp_master_delay 1
}

# One vrrp_instance per VIP
vrrp_instance VI_<vip_index> {
    state BACKUP  # All nodes start as BACKUP, preempt determines master
    interface eth0
    virtual_router_id <1-255 based on VIP hash>
    priority <100 if owner, 90 otherwise>
    preempt_delay 0
    advert_int 1

    # Unicast mode (default)
    unicast_src_ip <local_ip>
    unicast_peer {
        <peer_ip_1>
        <peer_ip_2>
    }

    # Health check - demote priority if HAProxy is down
    track_script {
        chk_haproxy
    }

    virtual_ipaddress {
        <vip>/32 dev eth0
    }
}

vrrp_script chk_haproxy {
    script "/usr/bin/curl -sf http://127.0.0.1:8404/health"
    interval 2
    weight -50  # Reduce priority by 50 if health check fails
    fall 2      # Require 2 failures before marking down
    rise 2      # Require 2 successes before marking up
}
```

### Health Check Mechanism (Keepalived Mode)

Unlike BGP mode where `ober health` outputs commands to stdout, keepalived uses `track_script`:

1. Keepalived runs the health check script every 2 seconds
2. Script checks HAProxy's `/health` endpoint
3. If check fails twice consecutively, node priority drops by 50
4. Lower priority triggers VRRP failover to another node
5. When HAProxy recovers, priority restores and node reclaims VIP (preempt)

### Service Configuration

**`ober-ha.service`** (replaces `ober-bgp.service` in keepalived mode):

```ini
[Unit]
Description=Ober HA (Keepalived)
After=network-online.target ober-http.service
Wants=network-online.target
BindsTo=ober-http.service

[Service]
Type=forking
PIDFile=/run/keepalived.pid
ExecStart=/usr/sbin/keepalived -f <install-path>/etc/keepalived/keepalived.conf
ExecReload=/bin/kill -HUP $MAINPID

[Install]
WantedBy=multi-user.target
```

### Firewall Configuration

Keepalived requires VRRP protocol (IP protocol 112):

**Unicast mode:**
```bash
# UFW (Ubuntu/Debian)
ufw allow proto vrrp from <peer_ip>

# firewalld (RHEL)
firewall-cmd --permanent --add-rich-rule='rule protocol value="vrrp" accept'
```

**Multicast mode (optional):**
```bash
# Allow VRRP multicast (224.0.0.18)
ufw allow proto vrrp to 224.0.0.18
```

### CLI Changes

**`ober config` wizard:**
1. New first question: "Select HA mode: BGP or Keepalived"
2. If Keepalived selected:
   - Skip BGP-specific questions (AS numbers, router neighbors)
   - Ask for peer node IPs (Slurm hostlist supported)
   - Ask for VIPs (one per node recommended)
   - Ask unicast vs multicast

**`ober status`:**
- Show keepalived state (MASTER/BACKUP for each VIP)
- Show peer connectivity
- Show which VIPs this node owns

**`ober doctor`:**
- Check keepalived is installed
- Verify VRRP connectivity to peers
- Validate VIP count matches node count (warn if mismatch)
- Check firewall allows VRRP protocol

### Bootstrap Changes

`ober bootstrap` installs **both** ExaBGP and keepalived:

**Debian/Ubuntu:**
```bash
apt install keepalived exabgp
```

**RHEL:**
```bash
dnf install keepalived exabgp
```

Only the selected mode's service is enabled during `ober config`.

### Failure Scenarios

| Event | Mechanism | Recovery Time |
|-------|-----------|---------------|
| HAProxy crash | `BindsTo=` stops keepalived, VIP fails over | Instant |
| HAProxy stall | `track_script` detects, priority drops, VIP fails over | ~4-6 seconds |
| Node crash | VRRP timeout, peer takes over VIP | ~3 seconds (3x advert_int) |
| Network partition | VRRP packets stop, both nodes may claim VIP (split-brain) | N/A - see note |

**Split-brain note:** In unicast mode with network partition, split-brain is possible. Mitigation: ensure reliable network between nodes. For critical deployments, consider fencing or use BGP mode instead.

### Future Features (Out of Scope for Initial Implementation)

- **Route53 integration:** Auto-register VIPs in DNS with health checks
- **Node weights:** Allow unequal VIP distribution based on node capacity
- **Fencing:** STONITH integration to prevent split-brain
- **IPv6 support:** VRRP for IPv6 VIPs
