"""Flow A, step A4: build the vendor-neutral network model from what was discovered.

Usage:  python a2_model.py [--batfish-host localhost] [--no-batfish]
Input:  out/discovery.json, out/configs/*.cfg
Output: out/model.yaml, out/batfish/base/configs/*.cfg, out/endpoint_candidates.yaml

Cabling sources, in order of strength:
  1. LLDP (and CDP on Cisco) seen from BOTH ends       -> fact
  2. LLDP/CDP seen from one end                         -> question to the user
  3. Batfish layer3Edges (two interfaces share a subnet) -> logical L3 adjacency is a fact from the configs;
     the physical cable behind it is asked when no LLDP/CDP confirms it
"""
from __future__ import annotations
import argparse, ipaddress, json, shutil
import networkx as nx
from common import OUT, answer, ask, dump_yaml
from platforms import EXPECTED_FORMAT, write_batfish


def short(name: str) -> str:
    return (name or "").split(".")[0]


def neighbours(d: dict):
    """Yield (local_if, remote_host, remote_if, source) from every cabling source the device gave."""
    for lif, ns in (d.get("get_lldp_neighbors_detail") or {}).items():
        for n in ns:
            yield lif, n.get("remote_system_name", ""), n.get("remote_port", ""), "lldp"
    for key, src in (("lldp_textfsm", "lldp"), ("cdp_textfsm", "cdp")):
        rows = d.get(key)
        if isinstance(rows, list):
            for r in rows:
                yield (r.get("local_interface", ""),
                       r.get("neighbor_name") or r.get("destination_host", ""),
                       r.get("neighbor_interface") or r.get("remote_port", ""), src)


def l3_edges(host: str, configs_dir, devices: dict, questions: list) -> list[tuple[str, str, str, str]]:
    from pybatfish.client.session import Session
    bf = Session(host=host)
    bf.set_network("ano")
    bf.init_snapshot(str(configs_dir.parent), name="base", overwrite=True)
    # Batfish must read each file in the format we expect; a wrong guess would make every later proof wrong
    fmt = bf.q.nodeProperties(properties="Configuration_Format").answer(snapshot="base").frame()
    lower = {h.lower(): h for h in devices}
    for _, r in fmt.iterrows():
        h = lower.get(r["Node"].lower())
        want = EXPECTED_FORMAT.get(devices.get(h, {}).get("platform"))
        if h:
            devices[h]["batfish_format"] = r["Configuration_Format"]
        if h and want and r["Configuration_Format"] != want:
            questions.append(f"{h}: Batfish read its config as {r['Configuration_Format']}, expected {want}. "
                             f"Formal checks for it would be wrong; fix the format in platforms.py first.")
    df = bf.q.layer3Edges().answer(snapshot="base").frame()
    return [(r["Interface"].hostname, r["Interface"].interface,
             r["Remote_Interface"].hostname, r["Remote_Interface"].interface) for _, r in df.iterrows()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batfish-host", default="localhost")
    ap.add_argument("--no-batfish", action="store_true")
    a = ap.parse_args()
    disc = json.loads((OUT / "discovery.json").read_text())
    devices, questions = {}, list(disc.get("questions", []))
    for ip, d in disc["devices"].items():
        if d.get("collector") == "none":
            continue
        host = short(d["facts"]["hostname"])
        ifaces, mgmt_if = {}, None
        for ifname, fams in (d.get("get_interfaces_ip") or {}).items():
            v4 = [f"{x}/{p['prefix_length']}" for x, p in fams.get("ipv4", {}).items()]
            v6 = [f"{x}/{p['prefix_length']}" for x, p in fams.get("ipv6", {}).items()]
            iface = d.get("get_interfaces", {}).get(ifname, {})
            ifaces[ifname] = {"ipv4": v4, "ipv6": v6, "up": iface.get("is_up"), "mtu": iface.get("mtu"),
                              "speed": iface.get("speed")}
            if any(ipaddress.ip_interface(c).ip == ipaddress.ip_address(ip) for c in v4):
                mgmt_if = ifname
        if not ifaces:
            questions.append(f"{host}: interface addressing not available from its driver; it will be taken "
                             f"from the Batfish parse of its config (check out/model.yaml after re-run).")
        devices[host] = {
            "mgmt_ip": ip, "mgmt_interface": mgmt_if, "cred_profile": d.get("profile", 1),
            "ssh_port": d.get("ssh_port", 22),
            "platform": d["platform"], "platform_source": d.get("platform_source"),
            "collector": d["collector"], "vendor": d["facts"].get("vendor"), "model": d["facts"].get("model"),
            "os_version": d["facts"].get("os_version"), "serial": d["facts"].get("serial_number"),
            "vrfs": sorted((d.get("get_network_instances") or {}).keys()),
            "vlans": sorted((d.get("get_vlans") or {}).keys(), key=lambda v: int(v) if str(v).isdigit() else 0),
            "interfaces": ifaces, "unsupported_getters": d.get("unsupported_getters", []),
        }
    by_lower = {h.lower(): h for h in devices}

    seen = {}
    for ip, d in disc["devices"].items():
        if d.get("collector") == "none":
            continue
        a_host = short(d["facts"]["hostname"])
        for lif, rhost, rif, src in neighbours(d):
            b = by_lower.get(short(rhost).lower(), short(rhost))
            key = tuple(sorted([(a_host, lif), (b, rif)]))
            seen.setdefault(key, {"reporters": set(), "sources": set()})
            seen[key]["reporters"].add(a_host)
            seen[key]["sources"].add(src)
    links = []
    for (e1, e2), info in seen.items():
        for dev, ifn in (e1, e2):
            if dev not in devices:
                questions.append(f"Neighbour '{dev}' ({ifn}) is outside the ranges you gave. "
                                 f"Give its management IP, or confirm it is out of scope.")
        both = len(info["reporters"]) == 2
        qid = f"link:{e1[0]}:{e1[1]}|{e2[0]}:{e2[1]}"
        if not both and all(e[0] in devices for e in (e1, e2)):
            if answer(qid) == "not-a-link":
                continue
            if answer(qid) != "direct":
                questions.append((qid, f"Cable {e1} <-> {e2} is reported only by {next(iter(info['reporters']))}. "
                                       f"Answer: direct | not-a-link"))
        links.append({"a": f"{e1[0]}:{e1[1]}", "b": f"{e2[0]}:{e2[1]}",
                      "source": "+".join(sorted(info["sources"])), "confirmed_both_ends": both})

    snap = OUT / "batfish" / "base" / "configs"
    shutil.rmtree(snap.parent, ignore_errors=True)
    snap.mkdir(parents=True)
    not_verified = []
    for host, dev in devices.items():
        raw = OUT / "configs" / f"{host}.cfg"
        gaps = write_batfish(snap.parent, dev["platform"], host, raw.read_text()) if raw.exists() else None
        dev["batfish"] = gaps is not None
        if gaps is None:
            not_verified.append(host)
        elif gaps:
            dev["batfish_gaps"] = gaps
            print(f"[A4] {host}: Batfish models this device only partly; NOT verified by Batfish: {gaps}")
    if not_verified:
        print(f"[A4] Batfish does not support {not_verified}: they are NOT formally verified; the Containerlab "
              f"twin tests are their only pre-deploy proof (shown on the approval screen).")

    if not a.no_batfish:
        linked = {(l["a"].split(":")[0].lower(), l["a"].split(":", 1)[1]) for l in links} | \
                 {(l["b"].split(":")[0].lower(), l["b"].split(":", 1)[1]) for l in links}
        try:
            for h1, i1, h2, i2 in l3_edges(a.batfish_host, snap, devices, questions):
                if (h1, i1) in linked or (h2, i2) in linked or h1 > h2:
                    continue
                A, B = by_lower.get(h1, h1), by_lower.get(h2, h2)
                if devices.get(A, {}).get("mgmt_interface") == i1 or devices.get(B, {}).get("mgmt_interface") == i2:
                    continue   # shared management subnet is not a data-plane link
                qid = f"link:{A}:{i1}|{B}:{i2}"
                ans = answer(qid)
                if ans == "not-a-link":
                    continue
                links.append({"a": f"{A}:{i1}", "b": f"{B}:{i2}", "source": "batfish-l3",
                              "confirmed_both_ends": False, "confirmed_by_user": ans == "direct"})
                if ans not in ("direct", "via-switch"):
                    questions.append((qid, f"{A}:{i1} and {B}:{i2} share a subnet (from the configs) but no "
                                           f"LLDP/CDP confirms the cable. Answer: direct | via-switch | not-a-link"))
                elif ans == "via-switch":
                    links[-1]["via_switch"] = True
        except Exception as e:
            print(f"[A4] Batfish L3 edge check skipped: {e}")

    g = nx.Graph()
    g.add_nodes_from(devices)
    g.add_edges_from((l["a"].split(":")[0], l["b"].split(":")[0]) for l in links)
    if devices and not nx.is_connected(g):
        questions.append(f"Topology is split into islands {[sorted(c) for c in nx.connected_components(g)]}. "
                         f"Missing links, or separate networks?")

    # Test endpoint candidates from ARP: live hosts seen on non-link, non-management interfaces
    link_ifs = {(l[s].split(":")[0], l[s].split(":", 1)[1]) for l in links for s in ("a", "b")}
    cands = []
    for ip, d in disc["devices"].items():
        host = short(d["facts"]["hostname"])
        if host not in devices:
            continue
        for e in d.get("get_arp_table") or []:
            ifn = e.get("interface", "")
            if (host, ifn) in link_ifs or ifn == devices[host]["mgmt_interface"]:
                continue
            gw = next((c for c in devices[host]["interfaces"].get(ifn, {}).get("ipv4", [])
                       if ipaddress.ip_address(e["ip"]) in ipaddress.ip_interface(c).network), None)
            if gw:
                net = ipaddress.ip_interface(gw).network
                cands.append({"name": f"ep-{e['ip'].replace('.', '-')}", "attach": f"{host}:{ifn}",
                              "ip": f"{e['ip']}/{net.prefixlen}", "gateway": str(ipaddress.ip_interface(gw).ip)})
    dump_yaml({"endpoints": cands}, OUT / "endpoint_candidates.yaml")

    dump_yaml({"source": "discovered (Flow A)", "devices": devices, "links": links,
               "not_formally_verified": not_verified,
               "unidentified_hosts": disc.get("unidentified", [])}, OUT / "model.yaml")
    print(f"Model: {len(devices)} devices, {len(links)} links -> {OUT/'model.yaml'}")
    print(f"Test endpoint candidates from ARP ({len(cands)}) -> {OUT/'endpoint_candidates.yaml'} (user confirms)")
    ask(questions)


if __name__ == "__main__":
    main()
