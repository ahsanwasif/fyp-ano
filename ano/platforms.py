"""Every vendor-specific fact ANO uses lives in THIS file. The rest of the pipeline is vendor-neutral.

Adding a vendor = (1) entries in the tables below, (2) a templates/<platform>/ folder.
A platform missing from a table is never guessed: the step that needs it stops and asks the user.
Platform keys are Netmiko device_type names (what SSHDetect returns), plus 'frr' (FRRouting, see NETMIKO_TYPE).

Every entry below was checked against the tool's own docs or a running device; the source is noted.
"""
from __future__ import annotations
import json, os, re

# ---- Identification -----------------------------------------------------------------------------
# Netmiko SSHDetect knows 52 platforms. It does NOT know FRR, Nokia SR Linux, VyOS or MikroTik.
# When SSHDetect returns nothing, ANO runs 'show version' and matches these patterns (verified ones only).
# Anything else: the user states it with  a1_discover.py --platform <ip>=<platform>
PROBE_COMMAND = "show version"
PROBES = [
    (re.compile(r"^FRRouting \d", re.M), "frr"),        # verified on FRR 8.4 and 10.x vtysh
]

# Netmiko driver used for a platform when it is not the platform name itself.
# FRR's SSH login lands in vtysh (containerlab image: user admin, shell vtysh), which speaks
# Cisco-IOS-style CLI: prompt 'r1#', 'configure terminal', 'end', 'write memory'. Verified with Netmiko 4.8.
NETMIKO_TYPE = {"frr": "cisco_ios"}


def netmiko_type(platform: str) -> str:
    return NETMIKO_TYPE.get(platform, platform)


# ---- NAPALM drivers (structured getters + commit/rollback) ---------------------------------------
NAPALM_CORE = {               # shipped with napalm 5.x itself
    "arista_eos": "eos", "cisco_ios": "ios", "cisco_xe": "ios", "cisco_nxos": "nxos_ssh",
    "cisco_xr": "iosxr", "juniper_junos": "junos",
}
NAPALM_COMMUNITY = {          # separate pip packages; tried only if installed
    "nokia_srl": "srlinux",        # pip install napalm-srlinux   (0.3.1, 2026-07; JSON-RPC over HTTPS, not SSH;
                                   #   commit_config(revert_in) supported). Replaces napalm-srl (gNMI, last 2024-05).
    "nokia_sros": "sros",          # pip install napalm-sros       (last release 2024-05)
    "mikrotik_routeros": "ros",    # pip install napalm-ros        (active, 2026)
    "paloalto_panos": "panos",     # pip install napalm-panos      (active, 2026)
    "huawei": "huawei_vrp",        # pip install napalm-huawei-vrp (last release 2024-04)
}
# FRR has no NAPALM driver (core or community): it uses the Netmiko path below.


def napalm_driver(platform: str) -> str | None:
    if platform in NAPALM_CORE:
        return NAPALM_CORE[platform]
    drv = NAPALM_COMMUNITY.get(platform)
    if drv:
        try:
            from napalm import get_network_driver
            get_network_driver(drv)
            return drv
        except Exception:
            return None
    return None


def napalm_args(drv: str) -> tuple[dict, str | None]:
    """Driver options. Returns (optional_args, question-or-None). TLS trust is never assumed."""
    if drv == "eos":
        return {"transport": "ssh"}, None            # keep EOS on SSH (default would be eAPI)
    if drv == "srlinux":                             # JSON-RPC over HTTPS: the user must say how to trust the cert
        if os.environ.get("ANO_TLS_CA"):
            return {"tls_ca": os.environ["ANO_TLS_CA"]}, None
        if os.environ.get("ANO_TLS_SKIP_VERIFY") == "1":
            return {"skip_verify": True}, None
        return {}, ("SR Linux is managed over JSON-RPC/HTTPS. Set ANO_TLS_CA=<ca.pem> to verify its certificate, "
                    "or ANO_TLS_SKIP_VERIFY=1 to accept self-signed lab certificates.")
    return {}, None


# ---- Netmiko-only platforms: commands and parsers -----------------------------------------------
SHOW = {
    "cisco_ios": {"config": "show running-config", "lldp": "show lldp neighbors detail", "cdp": "show cdp neighbors detail"},
    "cisco_xe": {"config": "show running-config", "lldp": "show lldp neighbors detail", "cdp": "show cdp neighbors detail"},
    "cisco_nxos": {"config": "show running-config", "lldp": "show lldp neighbors detail", "cdp": "show cdp neighbors detail"},
    "cisco_xr": {"config": "show running-config", "lldp": "show lldp neighbors detail", "cdp": "show cdp neighbors detail"},
    "arista_eos": {"config": "show running-config", "lldp": "show lldp neighbors detail"},
    "juniper_junos": {"config": "show configuration | display set", "lldp": "show lldp neighbors"},
    # FRR: vtysh JSON output (verified on FRR 8.4 and 10.x). No LLDP in FRR: cabling comes from Batfish + user.
    "frr": {"config": "show running-config", "version": "show version", "interfaces": "show interface json",
            "routes": "show ip route json", "ospf_neighbors": "show ip ospf neighbor json",
            "bgp_summary": "show bgp summary json"},
    # Linux server / firewall / router (iproute2 >= 4.14 for -j; iptables or iptables-nft). Reading the filter table
    # needs root: log in as root, or give ANO_SECRET<n> so Netmiko can sudo. Verified on Alpine 3.x, iproute2 5.17.
    "linux": {"config": "iptables-save", "addr": "ip -j addr show", "interfaces": "ip -j link show",
              "routes": "ip -j -4 route show", "hostname": "hostname", "os": "cat /etc/os-release",
              "fwd": "cat /proc/sys/net/ipv4/ip_forward"},
}


def _frr_state(out: dict) -> dict:
    """FRR vtysh JSON -> the same shapes NAPALM getters return, so the model code stays vendor-neutral."""
    ifs = json.loads(out.get("interfaces") or "{}")
    get_interfaces, get_ip = {}, {}
    for n, i in ifs.items():
        get_interfaces[n] = {"is_up": i.get("operationalStatus") == "up",
                             "is_enabled": i.get("administrativeStatus") == "up",
                             "mtu": i.get("mtu"), "speed": i.get("speed"),
                             "mac_address": i.get("hardwareAddress", ""), "description": i.get("description", "")}
        fams = {"ipv4": {}, "ipv6": {}}
        for a in i.get("ipAddresses", []):
            ip, plen = a["address"].split("/")
            fams["ipv6" if ":" in ip else "ipv4"][ip] = {"prefix_length": int(plen)}
        if fams["ipv4"] or fams["ipv6"]:
            get_ip[n] = {k: v for k, v in fams.items() if v}
    m = re.search(r"^FRRouting (\S+) \((\S+)\)", out.get("version", ""), re.M)
    facts = {"hostname": m.group(2) if m else "", "vendor": "FRRouting", "os_version": m.group(1) if m else "",
             "model": "FRR", "serial_number": "", "interface_list": sorted(ifs)}
    return {"get_facts": facts, "get_interfaces": get_interfaces, "get_interfaces_ip": get_ip,
            "routes_json": json.loads(out.get("routes") or "{}"),
            "ospf_neighbors_json": json.loads(out.get("ospf_neighbors") or "{}")}


def _jl(txt: str | None) -> list:
    try:
        return json.loads(txt or "[]")
    except json.JSONDecodeError:
        return []


def _linux_state(out: dict) -> dict:
    """iproute2 JSON -> NAPALM getter shapes. Kernel routes are the RIB (connected, static, from any daemon)."""
    get_interfaces, get_ip = {}, {}
    for l in _jl(out.get("interfaces")):
        n = l["ifname"]
        get_interfaces[n] = {"is_up": l.get("operstate") in ("UP", "UNKNOWN") and "UP" in l.get("flags", []),
                             "is_enabled": "UP" in l.get("flags", []), "mtu": l.get("mtu"), "speed": None,
                             "mac_address": l.get("address", ""), "description": l.get("ifalias", "")}
    for a in _jl(out.get("addr")):
        fams = {"ipv4": {}, "ipv6": {}}
        for ai in a.get("addr_info", []):
            fam = {"inet": "ipv4", "inet6": "ipv6"}.get(ai.get("family"))
            if fam and ai.get("scope") not in ("link", "host"):     # skip 127.0.0.1 / ::1 and link-local
                fams[fam][ai["local"]] = {"prefix_length": ai["prefixlen"]}
        if fams["ipv4"] or fams["ipv6"]:
            get_ip[a["ifname"]] = {k: v for k, v in fams.items() if v}
    routes = {}
    for r in _jl(out.get("routes")):
        dst = "0.0.0.0/0" if r.get("dst") == "default" else (r["dst"] if "/" in r["dst"] else r["dst"] + "/32")
        routes.setdefault(dst, []).append({"selected": True, "protocol": r.get("protocol", "boot"),
                                           "nexthop": r.get("gateway"), "dev": r.get("dev")})
    osr = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', out.get("os", ""), re.M))
    facts = {"hostname": (out.get("hostname") or "").strip(), "vendor": osr.get("ID", ""),
             "os_version": osr.get("PRETTY_NAME", ""), "model": "Linux", "serial_number": "",
             "interface_list": sorted(get_interfaces)}
    return {"get_facts": facts, "get_interfaces": get_interfaces, "get_interfaces_ip": get_ip, "routes_json": routes}


def _linux_config(raw: dict) -> str:
    """The running state of a Linux box as ONE replayable script: ANO's 'running config' for Linux.
    Built from what the box reports (addresses, non-kernel routes, forwarding, iptables-save), nothing added."""
    st = _linux_state(raw)
    lines = ["#!/bin/sh", f"# ANO Linux config of {st['get_facts']['hostname']}",
             f"sysctl -w net.ipv4.ip_forward={(raw.get('fwd') or '0').strip()}"]
    for ifn, fams in sorted(st["get_interfaces_ip"].items()):
        if ifn == "lo":
            continue
        for ip, p in fams.get("ipv4", {}).items():
            lines.append(f"ip addr replace {ip}/{p['prefix_length']} dev {ifn}")
        lines.append(f"ip link set {ifn} up")
    for dst, rs in st["routes_json"].items():
        for r in rs:
            if r["protocol"] != "kernel" and r["nexthop"]:
                lines.append(f"ip route replace {'default' if dst == '0.0.0.0/0' else dst} via {r['nexthop']} dev {r['dev']}")
    rules = (raw.get("config") or "").strip()
    lines += ["iptables-restore <<'ANO_EOF'", rules, "ANO_EOF"] if rules else []
    return "\n".join(lines) + "\n"


STATE_PARSERS = {"frr": _frr_state, "linux": _linux_state}
CONFIG_BUILDERS = {"linux": _linux_config}           # platforms whose 'config' is assembled from several commands
# How the post-check proves a subnet is reachable: 'exact' = a selected route for that very prefix (routers),
# 'covering' = any route that contains it, default included (servers / firewalls usually use a default route).
RIB_MATCH = {"linux": "covering"}
# Lines in a Netmiko session's output that mean a command failed (default: IOS-style '%').
CLI_ERROR = {"linux": r"(not found|Bad argument|bad rule|can't|No such|Invalid|Error|unknown option|RTNETLINK)"}
# Command that makes the running state survive a reboot. Linux: distro-specific, so it is asked (never guessed).
SAVE_ASK = {"linux": "How does this server persist iptables and routes (e.g. 'iptables-save > /etc/iptables/rules.v4', "
                     "netplan, nmcli)? Answer with the exact command, or 'none' to keep the change until reboot only."}


# ---- SuzieQ (SSH state polling): platforms its docs list (suzieq.readthedocs.io, 0.25.0) --------------
# Not listed: Nokia SR Linux, FRR (its SSH login is vtysh, not the Linux shell SuzieQ's 'linux' type needs).
SUZIEQ = {"arista_eos", "cisco_ios", "cisco_xe", "cisco_xr", "cisco_nxos", "juniper_junos", "cumulus_linux",
          "paloalto_panos", "linux"}


# ---- Batfish -----------------------------------------------------------------------------------
# How each platform's running config must be presented to Batfish. None = Batfish does not support it,
# so the device is reported "not formally verified" (never fed to Batfish, where it could be mis-detected:
# a bare frr.conf is silently parsed as CISCO_IOS - verified).
RANCID = {"arista_eos": "arista", "cisco_nxos": "cisco-nx", "cisco_xr": "cisco-xr", "juniper_junos": "juniper",
          "paloalto_panos": "paloalto", "f5_tmsh": "bigip", "dell_force10": "force10"}
BATFISH_AUTODETECT = {"cisco_ios", "cisco_xe", "cisco_asa", "fortinet", "checkpoint_gaia", "cumulus_linux"}
# Format Batfish must report for the file, checked after parsing (verified with Batfish 2026).
EXPECTED_FORMAT = {"arista_eos": "ARISTA", "frr": "CUMULUS_CONCATENATED"}


# Platforms whose filters Batfish PARSES but does not apply on the forwarding path (verified 2026.08: a Linux host's
# iptables chains become named filters, yet traceroute passes through a FORWARD policy of DROP). Their rules are
# proven with searchFilters on the chain instead: 'through' for traffic routed across, 'to_self' for its own IPs.
BATFISH_FILTER_ONLY = {"linux": {"through": "filter::FORWARD", "to_self": "filter::INPUT"}}


# Lines Batfish does not model that only touch the management plane (they cannot change forwarding).
# Listed explicitly per platform; any OTHER unparsed line in a change stops the flow.
BATFISH_MGMT_ONLY = {"arista_eos": [r"^management api gnmi$", r"^transport grpc \S+$"]}


def _frr_for_batfish(hostname: str, cfg: str) -> str:
    # Batfish reads FRR inside its Cumulus 'concatenated' layout: hostname, /etc/network/interfaces,
    # ports.conf, frr.conf (batfish formats.html). The 'exit' lines FRR >= 8 prints are not parsed; drop them.
    frr = "\n".join(l for l in cfg.splitlines() if l.strip() != "exit")
    frr = frr[frr.index("frr version"):] if "frr version" in frr else "frr version unknown\n" + frr
    return (f"{hostname}\n# This file describes the network interfaces\nauto lo\niface lo inet loopback\n\n"
            f"# ports.conf --\n\n{frr}\n")


def _linux_for_batfish(hostname: str, cfg: str) -> dict:
    """Batfish 'host' format (hosts/<h>.json + iptables/<h>.iptables), verified with Batfish 2026.08:
    filter rules are modelled (stateless), NAT targets (MASQUERADE/SNAT/DNAT) are NOT, and a host has only
    connected routes plus one default gateway. Anything else is returned under '_gaps' so it is reported."""
    ifs, gaps = {}, []
    for ip, dev in re.findall(r"^ip addr replace (\S+) dev (\S+)$", cfg, re.M):
        ifs.setdefault(dev, {"name": dev, "prefix": ip})
    for b in re.findall(r"^ip route replace blackhole (\S+)$", cfg, re.M):
        gaps.append(f"blackhole route {b} (Batfish hosts have no static routes)")
    for dst, gw, dev in re.findall(r"^ip route replace (\S+) via (\S+) dev (\S+)$", cfg, re.M):
        if dst == "default" and dev in ifs and "gateway" not in ifs[dev]:
            ifs[dev]["gateway"] = gw
        else:
            gaps.append(f"route {dst} via {gw} (Batfish hosts model only one default gateway)")
    m = re.search(r"<<'ANO_EOF'\n(.*?)\nANO_EOF", cfg, re.S)
    rules = m.group(1) if m else ""
    if not m and re.search(r"^iptables (-P|-A) ", cfg, re.M):      # Flow B script: commands, not iptables-save
        pol = dict(re.findall(r"^iptables -P (\S+) (\S+)$", cfg, re.M))
        rules = "\n".join(["*filter"] + [f":{c} {pol.get(c, 'ACCEPT')} [0:0]" for c in ("INPUT", "FORWARD", "OUTPUT")]
                          + [f"-A {c} {r}" for c, r in re.findall(r"^iptables -A (\S+) (.*)$", cfg, re.M)] + ["COMMIT"])
    gaps += [f"iptables NAT rule '{l}' (Batfish does not model NAT on hosts)" for l in rules.splitlines()
             if re.search(r"-j (MASQUERADE|SNAT|DNAT|REDIRECT)\b", l)]
    gaps += [f"iptables stateful rule '{l}' (Batfish skips connection-state matches)" for l in rules.splitlines()
             if re.search(r"-m (state|conntrack)\b", l)]
    host = {"hostname": hostname, "hostInterfaces": ifs}
    files = {f"hosts/{hostname}.json": json.dumps(host, indent=1)}
    if rules:
        host["iptablesFile"] = f"iptables/{hostname}.iptables"
        files = {f"hosts/{hostname}.json": json.dumps(host, indent=1), f"iptables/{hostname}.iptables": rules + "\n"}
    files["_gaps"] = gaps
    return files


def batfish_file(platform: str, hostname: str, config: str):
    """Text for configs/<host>.cfg, or {relative path: text, '_gaps': [...]} for multi-file formats, or None."""
    if platform == "linux":
        return _linux_for_batfish(hostname, config)
    if platform == "frr":
        return _frr_for_batfish(hostname, config)
    if platform in RANCID:
        return config if config.startswith("!RANCID-CONTENT-TYPE") else \
            f"!RANCID-CONTENT-TYPE: {RANCID[platform]}\n{config}"
    if platform in BATFISH_AUTODETECT:
        return config
    return None


def write_batfish(snapshot_dir, platform: str, hostname: str, config: str):
    """Writes the device into a Batfish snapshot dir. Returns None (unsupported) or the list of modelling gaps."""
    from pathlib import Path
    out = batfish_file(platform, hostname, config)
    if out is None:
        return None
    root = Path(snapshot_dir)
    if isinstance(out, str):
        (root / "configs").mkdir(parents=True, exist_ok=True)
        (root / "configs" / f"{hostname}.cfg").write_text(out)
        return []
    for rel, text in out.items():
        if rel != "_gaps":
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
    return out.get("_gaps", [])


# ---- What each platform can enforce (checked before rendering) ----------------------------------
# value = None: full match (src, dst, protocol, port). A string = a limited method the user must accept.
CAPABILITIES = {
    "arista_eos": {"block_traffic": None},
    "linux": {"block_traffic": None},          # iptables: full src/dst/protocol/port match
    "frr": {"block_traffic": "FRR is a routing daemon and cannot filter packets. The only way is a null route "
                             "for the DESTINATION on this device: it drops ALL traffic to that subnet entering "
                             "here, whatever the source, protocol or port."},
}


# ---- Flow B: which intake features each platform's full_device.j2 can render -----------------------
# FRR: L2, VRFs, filters and system services live in Linux (bridges, VRF devices, nftables, chrony), not in
# frr.conf, so an intake that needs them on an FRR device is a question, never silently dropped.
FEATURES = {
    "arista_eos": {"routed", "access", "trunk", "svi", "lag-member", "vlan", "vrf", "ospf", "bgp", "static",
                   "policy", "services", "gnmi"},
    "frr": {"routed", "ospf", "bgp", "static"},
    "linux": {"routed", "static", "policy"},   # server / firewall: addresses, static routes, iptables filters
}


# Platforms whose full_device.j2 always configures a loopback (so b1 asks for one).
NEEDS_LOOPBACK = {"frr", "arista_eos"}


# ---- Containerlab: candidate node kinds per platform (several = the user must pick one) ----------
CLAB_KINDS = {
    "arista_eos": ["arista_ceos", "arista_veos"],
    "cisco_ios": ["cisco_iol", "cisco_vios"],
    "cisco_xe": ["cisco_c8000v", "cisco_csr1000v", "cisco_iol"],
    "cisco_xr": ["cisco_xrd", "cisco_xrv9k"],
    "cisco_nxos": ["cisco_n9kv"],
    "juniper_junos": ["juniper_vjunosrouter", "juniper_vjunosswitch", "juniper_vmx", "juniper_vqfx",
                      "juniper_vsrx", "juniper_crpd"],
    "nokia_srl": ["nokia_srlinux"],
    "nokia_sros": ["nokia_sros", "nokia_srsim"],
    "huawei": ["huawei_vrp"],
    "mikrotik_routeros": ["mikrotik_ros"],
    "fortinet": ["fortinet_fortigate"],
    "paloalto_panos": ["paloalto_panos"],
    "aruba_aoscx": ["aruba_aoscx"],
    "vyos": ["vyosnetworks_vyos"],
    "cumulus_linux": ["nvidia_cumulusvx"],
    "linux": ["linux"],           # any Linux image with iproute2 + iptables, chosen by the user (ANO_IMAGE_LINUX)
    "frr": ["frr"],               # image quay.io/frrouting/frr:containerlab-<version> (SSH built in, from 10.7.1)
}


def image_env(kind: str) -> str:
    """The image tag for a kind comes from the user: ANO_IMAGE_<KIND>, e.g. ANO_IMAGE_FRR=quay.io/frrouting/frr:containerlab-10.7.1"""
    return "ANO_IMAGE_" + kind.upper()


# ---- CLI adapters: the few text operations that depend on config syntax ---------------------------
def _eos_ifmap(ifname: str) -> str:
    m = re.fullmatch(r"Ethernet(\d+)", ifname)
    if not m:
        raise ValueError(f"no containerlab mapping for EOS interface {ifname}")
    return f"eth{m.group(1)}"


def _eos_mgmt(cfg: str, ip: str, plen: int, mgmt_if: str) -> str:
    return re.sub(rf"(^interface {re.escape(mgmt_if)}\n(?:[ \t]+.*\n)*?[ \t]+ip address )\S+",
                  rf"\g<1>{ip}/{plen}", cfg, count=1, flags=re.M)


def _end_merge(base: str, change: str) -> str:
    return re.sub(r"\nend\s*$", "\n", base) + change + "end\n"


def _touches(change: str, ifname: str) -> bool:
    return re.search(rf"^interface {re.escape(ifname)}\s*$", change, re.M) is not None


def _frr_ifmap(ifname: str) -> str:
    if not re.fullmatch(r"eth\d+", ifname):
        raise ValueError(f"no containerlab mapping for FRR interface {ifname}")
    return ifname                          # FRR runs on Linux: the kernel name is the config name


def _frr_mgmt(cfg: str, ip: str, plen: int, mgmt_if: str) -> str:
    return cfg                             # management IP is set by containerlab on eth0, not in frr.conf


def _frr_clab(cfg: str) -> dict:
    """Daemons are read from the config itself (never assumed): router ospf -> ospfd, ip route -> staticd ..."""
    need = [d for pat, d in ((r"^router ospf\b", "ospfd"), (r"^router bgp\b", "bgpd"), (r"^router isis\b", "isisd"),
                             (r"^router ospf6\b", "ospf6d"), (r"^ip(v6)? route\b", "staticd"), (r"^bfd\b", "bfdd"))
            if re.search(pat, cfg, re.M)]
    return {"daemons": need} if need else {}


def _frr_clean(cfg: str) -> str:
    """What vtysh 'show running-config' prints before the config itself."""
    return cfg[cfg.index("frr version"):] if "frr version" in cfg else cfg


def _linux_merge(base: str, change: str) -> str:
    """Applies 'iptables -I <CHAIN> 1 <rule>' / 'iptables -D <CHAIN> <rule>' to the iptables-save block of the
    config script, so Batfish sees exactly the rule order the box will have."""
    m = re.search(r"<<'ANO_EOF'\n(.*?)\nANO_EOF", base, re.S)
    rules = m.group(1).splitlines() if m else []
    if "*filter" not in rules:          # no filter table loaded yet: the kernel defaults are ACCEPT with no rules
        rules += ["*filter", ":INPUT ACCEPT [0:0]", ":FORWARD ACCEPT [0:0]", ":OUTPUT ACCEPT [0:0]", "COMMIT"]
    start = rules.index("*filter")
    end = rules.index("COMMIT", start)
    for line in change.splitlines():
        ins = re.match(r"^iptables -I (\S+) 1 (.*)$", line.strip())
        dele = re.match(r"^iptables -D (\S+) (.*)$", line.strip())
        if ins:
            chain, rule = ins.groups()
            pos = next((i for i in range(start, end) if rules[i].startswith(f"-A {chain} ")), end)
            rules.insert(pos, f"-A {chain} {rule}"); end += 1
        elif dele:
            rules.remove(f"-A {dele.group(1)} {dele.group(2)}"); end -= 1
        elif line.strip() and not line.startswith("#"):
            base += line.strip() + "\n"   # ip addr / ip route lines are appended to the script as they are
    block = "iptables-restore <<'ANO_EOF'\n" + "\n".join(rules) + "\nANO_EOF"
    return base.replace(m.group(0), block) if m else base + block + "\n"


def _linux_mgmt(cfg: str, ip: str, plen: int, mgmt_if: str) -> str:
    """Twin: the management address is set by the lab tool, so the box's own mgmt address lines are dropped."""
    return "\n".join(l for l in cfg.splitlines()
                     if not re.match(rf"^ip (addr replace \S+|route replace \S+ via \S+) dev {re.escape(mgmt_if)}$", l)) + "\n"


ADAPTERS = {
    "linux": {"ifmap": _frr_ifmap, "mgmt": _linux_mgmt, "merge": _linux_merge,
              "touches": lambda change, ifn: re.search(rf"(-i|-o|dev) {re.escape(ifn)}\b", change) is not None,
              # containerlab 'linux' kind has no startup-config: the script is bind-mounted and run at boot
              "clab": lambda cfg: {}, "clean": lambda cfg: cfg, "startup": "exec"},
    "arista_eos": {"ifmap": _eos_ifmap, "mgmt": _eos_mgmt, "merge": _end_merge, "touches": _touches,
                   "clab": lambda cfg: {}, "clean": lambda cfg: cfg},
    "frr": {"ifmap": _frr_ifmap, "mgmt": _frr_mgmt, "merge": _end_merge, "touches": _touches,
            "clab": _frr_clab, "clean": _frr_clean},
}


def adapter(platform: str) -> dict | None:
    return ADAPTERS.get(platform)
