"""Flow A, steps A2-A3: discover devices, platforms, configs, state and cabling over SSH only.

Usage:  python a1_discover.py --range 172.31.1.0/24 [--range ...] [--hosts 10.9.9.1,10.9.9.2]
                              [--platform 10.9.9.7=nokia_srl]   (only for an IP that SSHDetect and ANO's probes could not identify)
Output: out/discovery.json, out/configs/<hostname>.cfg

What is collected per device (every getter the driver supports; unsupported ones are recorded, not guessed):
  identity      get_facts (vendor, model, OS version, serial, uptime, interface list)
  interfaces    get_interfaces (state, speed, MTU, MAC), get_interfaces_ip, get_interfaces_counters
  cabling       get_lldp_neighbors_detail; CDP via Netmiko+TextFSM on Cisco platforms
  L2            get_vlans, get_mac_address_table
  L3 / routing  get_network_instances (VRFs), get_bgp_neighbors, get_bgp_config, get_arp_table,
                get_ipv6_neighbors_table   (OSPF/IS-IS and full RIBs: SuzieQ + Batfish, see RUNBOOK)
  services      get_ntp_servers, get_snmp_information
  hardware      get_environment, get_optics
  config        get_config(running)  -> Batfish models everything configured (ACLs, NAT, routing policy...)
No NAPALM driver (e.g. FRR): the platform's show commands in platforms.SHOW are run over SSH and parsed into
the same shapes; anything a platform cannot give is listed per device, never filled in.
"""
from __future__ import annotations
import argparse, asyncio, ipaddress
from concurrent.futures import ThreadPoolExecutor
from napalm import get_network_driver
from netmiko import SSHDetect, ConnectHandler
from common import OUT, creds_list, dump_json
from platforms import (CONFIG_BUILDERS, PROBE_COMMAND, PROBES, SHOW, STATE_PARSERS, adapter, napalm_args, napalm_driver,
                       netmiko_type)

PORT = 22   # set from --port (the SSH port the user gave, same as in PuTTY)
GETTERS = ["get_facts", "get_interfaces", "get_interfaces_ip", "get_interfaces_counters",
           "get_lldp_neighbors_detail", "get_vlans", "get_mac_address_table", "get_network_instances",
           "get_bgp_neighbors", "get_bgp_config", "get_arp_table", "get_ipv6_neighbors_table",
           "get_ntp_servers", "get_snmp_information", "get_environment", "get_optics"]


async def _probe(ip: str, port: int = 22, timeout: float = 2.0) -> str | None:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
        w.close()
        return ip
    except Exception:
        return None


async def sweep(addresses: list[str], port: int) -> list[str]:
    sem = asyncio.Semaphore(256)
    async def one(ip):
        async with sem:
            return await _probe(ip, port)
    return [ip for ip in await asyncio.gather(*(one(a) for a in addresses)) if ip]


def _nm_params(platform: str, ip: str, c: dict) -> dict:
    p = {"device_type": netmiko_type(platform), "host": ip, "port": PORT, "username": c["username"],
         "password": c["password"]}
    if "secret" in c:
        p["secret"] = c["secret"]
    if "key_file" in c:
        p.update(use_keys=True, key_file=c["key_file"])
    return p


def detect(ip: str, profiles: dict, hints: dict) -> dict:
    """Try each credential profile; return platform + which profile worked. Never guesses a platform."""
    last = "no credential profile worked"
    for n, c in profiles.items():
        try:
            if ip in hints:   # user-stated platform: still prove the login works
                ConnectHandler(**_nm_params(hints[ip], ip, c)).disconnect()
                return {"ip": ip, "platform": hints[ip], "profile": n, "platform_source": "user"}
            found = SSHDetect(**_nm_params("autodetect", ip, c)).autodetect()
            if found:
                return {"ip": ip, "platform": found, "profile": n, "platform_source": "SSHDetect"}
            # SSHDetect does not know FRR, SR Linux, VyOS, MikroTik: try the verified probes in platforms.py
            with ConnectHandler(**_nm_params("generic", ip, c)) as conn:
                text = conn.send_command(PROBE_COMMAND, read_timeout=20)
            for rx, plat in PROBES:
                if rx.search(text):
                    return {"ip": ip, "platform": plat, "profile": n, "platform_source": f"probe '{PROBE_COMMAND}'"}
            last = "logged in, but neither SSHDetect nor ANO's probes could identify the platform"
            return {"ip": ip, "platform": None, "profile": n, "reason": last}
        except Exception as e:
            last = str(e).splitlines()[0][:200]
    return {"ip": ip, "platform": None, "profile": None, "reason": last}


def collect(ip: str, platform: str, c: dict) -> dict:
    data, unsupported = {}, []
    drv = napalm_driver(platform)
    if drv:
        opt, q = napalm_args(drv)
        if q:
            return {"collector": "none", "facts": {"hostname": ip}, "config": "", "question": f"{ip}: {q}"}
        for k in ("secret", "key_file"):
            if k in c:
                opt[k] = c[k]
        if PORT != 22 and drv != "srlinux":      # srlinux talks JSON-RPC/HTTPS, not SSH
            opt["port"] = PORT
        dev = get_network_driver(drv)(ip, c["username"], c["password"], optional_args=opt)
        dev.open()
        try:
            for g in GETTERS:
                try:
                    data[g] = getattr(dev, g)()
                except NotImplementedError:
                    unsupported.append(g)
                except Exception as e:
                    unsupported.append(f"{g} (error: {str(e).splitlines()[0][:120]})")
            data["config"] = dev.get_config(retrieve="running")["running"]
        finally:
            dev.close()
        data["collector"] = f"napalm:{drv}"
    else:
        cmds = SHOW.get(platform)
        if not cmds:
            return {"collector": "none", "facts": {"hostname": ip}, "config": "",
                    "question": f"{ip} ({platform}): no NAPALM driver and no known show commands. "
                                f"Which commands print its running config and its LLDP neighbours?"}
        raw = {}
        with ConnectHandler(**_nm_params(platform, ip, c)) as conn:
            if "secret" in c:
                conn.enable()
            for key, cmd in cmds.items():
                if key == "cdp":
                    continue
                raw[key] = conn.send_command(cmd, use_textfsm=(key == "lldp"), read_timeout=60)
            prompt_host = conn.find_prompt().strip("#>$ ").split("@")[-1]
        data["config"] = CONFIG_BUILDERS[platform](raw) if platform in CONFIG_BUILDERS else raw["config"]
        raw.pop("config")
        if "lldp" in raw:
            data["lldp_textfsm"] = raw.pop("lldp")
        parser = STATE_PARSERS.get(platform)
        if parser:
            data.update(parser(raw))       # same shapes as the NAPALM getters (facts, interfaces, IPs)
        data.setdefault("get_facts", {"hostname": prompt_host})
        data["collector"] = f"netmiko:{netmiko_type(platform)} ({len(cmds)} commands)"
        unsupported = [g for g in GETTERS if g not in data]
        if "lldp" not in cmds:
            unsupported.append("cabling: platform has no LLDP/CDP source; links come from Batfish + your answers")
    # CDP as a second cabling source on Cisco platforms (LLDP may be disabled there)
    if "cdp" in SHOW.get(platform, {}):
        try:
            with ConnectHandler(**_nm_params(platform, ip, c)) as conn:
                data["cdp_textfsm"] = conn.send_command(SHOW[platform]["cdp"], use_textfsm=True)
        except Exception as e:
            unsupported.append(f"cdp ({str(e).splitlines()[0][:80]})")
    data["facts"] = data.pop("get_facts", {"hostname": ip})
    data["unsupported_getters"] = unsupported
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--range", action="append", default=[], help="management CIDR given by the user")
    ap.add_argument("--hosts", default="", help="comma-separated management IPs given by the user")
    ap.add_argument("--platform", action="append", default=[], help="IP=platform, stated by the user")
    ap.add_argument("--port", type=int, default=22, help="SSH port the user gave (as in PuTTY); 22 if unchanged")
    a = ap.parse_args()
    global PORT
    PORT = a.port
    addrs = [str(h) for r in a.range for h in ipaddress.ip_network(r, strict=False).hosts()]
    addrs += [h.strip() for h in a.hosts.split(",") if h.strip()]
    if not addrs:
        raise SystemExit("Give --range or --hosts. ANO does not scan networks it was not given.")
    hints = dict(x.split("=", 1) for x in a.platform)
    profiles = creds_list()

    alive = asyncio.run(sweep(addrs, a.port))
    print(f"[A2] {len(alive)} address(es) answer on TCP/{a.port}")
    with ThreadPoolExecutor(max_workers=32) as pool:
        detected = list(pool.map(lambda ip: detect(ip, profiles, hints), alive))
    devices = {d["ip"]: d for d in detected if d["platform"]}
    unidentified = [d for d in detected if not d["platform"]]
    for d in devices.values():
        print(f"[A2] {d['ip']}: {d['platform']} (from {d['platform_source']}, login profile {d['profile']})")

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = dict(zip(devices, pool.map(
            lambda ip: collect(ip, devices[ip]["platform"], profiles[devices[ip]["profile"]]), devices)))
    cfg_dir = OUT / "configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    questions = []
    for ip, data in results.items():
        devices[ip].update(data)
        devices[ip]["mgmt_ip"] = ip
        devices[ip]["ssh_port"] = a.port
        if "question" in data:
            questions.append(data["question"])
            continue
        name = data["facts"]["hostname"]
        ad = adapter(devices[ip]["platform"])
        cfg = ad["clean"](data["config"]) if ad else data["config"]
        devices[ip]["config"] = cfg
        (cfg_dir / f"{name}.cfg").write_text(cfg)        # raw running config; a2 prepares the Batfish copy
        print(f"[A3] {ip} -> {name} ({data['collector']}); not supported by driver: "
              f"{data['unsupported_getters'] or 'none'}")

    dump_json({"devices": devices, "unidentified": unidentified, "questions": questions}, OUT / "discovery.json")
    for u in unidentified:
        print(f"[A2] NOT identified {u['ip']}: {u['reason']}  -> if it is a network device, re-run with "
              f"--platform {u['ip']}=<netmiko device_type>")
    for q in questions:
        print("[A3] QUESTION:", q)
    print(f"\nWrote {OUT/'discovery.json'}")


if __name__ == "__main__":
    main()
