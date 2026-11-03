"""Flow B, steps B1-B2: description -> validated intake -> model + full configs. Nothing is defaulted.

  python b1_intake.py --intake inputs/flowb_intake.yaml
  python b1_intake.py --prompt "..." --llm ollama/qwen2.5:7b-instruct   (LLM fills what it can; the rest becomes questions)
Output: out/model.yaml (same format as Flow A), out/changes/*.cfg (full configs to push),
        out/batfish/candidate/configs/*.cfg (only for platforms Batfish supports)
No password is written anywhere: the intake carries the login's SHA-512 hash (openssl passwd -6).
"""
from __future__ import annotations
import argparse, ipaddress, re, shutil
from collections import Counter, defaultdict
from typing import Optional
import networkx as nx
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import ValidationError, create_model
from common import OUT, ROOT, answer, ask, dump_yaml, load_yaml
from intake_schema import Intake
from platforms import FEATURES, NEEDS_LOOPBACK, write_batfish


def draft_from_prompt(prompt: str, llm: str) -> dict:
    import instructor, litellm
    Draft = create_model("IntakeDraft", **{k: (Optional[f.annotation], None) for k, f in Intake.model_fields.items()})
    client = instructor.from_litellm(litellm.completion)
    d = client.chat.completions.create(model=llm, response_model=Draft, max_retries=3, messages=[
        {"role": "system", "content": "Fill only fields the user explicitly stated. Leave everything else null."},
        {"role": "user", "content": prompt}])
    return {k: v for k, v in d.model_dump(by_alias=True).items() if v is not None}


def features_used(i: Intake, dev: str) -> set[str]:
    f = {x.mode for x in i.interfaces if x.device == dev}
    f |= {"vlan"} if any(v.device == dev for v in i.vlans) else set()
    f |= {"vrf"} if any(v.device == dev for v in i.vrfs) else set()
    f |= {"ospf"} if any(x.device == dev and x.ospf_area for x in i.interfaces) else set()
    f |= {"bgp"} if any(b.device == dev for b in i.routing.bgp) else set()
    f |= {"static"} if any(r.device == dev for r in i.routing.static_routes) else set()
    f |= {"policy"} if any(p.apply_on.startswith(dev + ":") for p in i.policies) else set()
    f |= {"services"} if (i.services.ntp or i.services.dns or i.services.syslog) else set()
    return f


def forwarding(i: Intake, dev: str) -> str | None:
    """Does a Linux box route between its interfaces? 'yes' when another device's static route points at it,
    'no' when it has a single data interface, otherwise the user's answer (forward:<dev>), or None = ask."""
    mine = {str(x.ip.ip) for x in i.interfaces if x.device == dev and x.ip}
    if any(str(r.next_hop) in mine for r in i.routing.static_routes if r.device != dev):
        return "yes"
    if len(mine) <= 1:
        return "no"
    return answer(f"forward:{dev}")


def consistency(i: Intake) -> list:
    q = []
    for d in i.devices:
        if d.platform == "linux" and forwarding(i, d.name) is None:
            q.append((f"forward:{d.name}", f"{d.name} has several addressed interfaces. Should it route traffic "
                                            f"between them (firewall/router) or not (multi-homed server)? Answer: yes | no"))
    devs = {d.name: d for d in i.devices}
    if i.starting_point == "brownfield":
        q += [f"{d.name}: brownfield needs the current running config (existing_config)." for d in i.devices if not d.existing_config]
    if not re.fullmatch(r"\$6\$[^$]+\$[./A-Za-z0-9]{86}", i.device_login.secret_sha512):
        q.append("device_login.secret_sha512 is not a SHA-512 crypt hash. Create one with: openssl passwd -6")
    for d in i.devices:
        runs = features_used(i, d.name) & {"ospf", "bgp"}
        if runs and not (d.router_id and d.loopback and d.loopback_interface):
            q.append(f"{d.name} runs {sorted(runs)}: give router_id, loopback_interface and loopback.")
        if d.platform in NEEDS_LOOPBACK and not d.loopback:
            q.append(f"{d.name} ({d.platform}): its template configures a loopback; give loopback_interface and loopback.")
        if not (ROOT / "templates" / d.platform / "full_device.j2").exists():
            q.append(f"{d.name}: no template for platform {d.platform}. Add templates/{d.platform}/full_device.j2.")
            continue
        extra = features_used(i, d.name) - FEATURES.get(d.platform, set())
        if extra:
            q.append(f"{d.name} ({d.platform}): its template cannot configure {sorted(extra)}. Move those to "
                     f"another device, or extend templates/{d.platform}/ and platforms.FEATURES.")
    ifs = {f"{x.device}:{x.name}": x for x in i.interfaces}
    vlans = {(v.device, v.id) for v in i.vlans}
    vrfs = {(v.device, v.name) for v in i.vrfs}
    nets = [(f"{x.device}:{x.name}", x.ip, x.vrf) for x in i.interfaces if x.ip] + \
           [(f"{d.name}:{d.loopback_interface}", d.loopback, None) for d in i.devices if d.loopback]
    for n, (a, ia, va) in enumerate(nets):
        for b, ib, vb in nets[n + 1:]:
            if va == vb and ia.network.overlaps(ib.network) and ia.network != ib.network:
                q.append(f"{a} {ia.network} overlaps {b} {ib.network} but is not the same subnet.")
            if va == vb and ia.ip == ib.ip:
                q.append(f"{a} and {b} both use {ia.ip}.")
    for x in i.interfaces:
        if x.device not in devs:
            q.append(f"Interface {x.device}:{x.name} belongs to an undescribed device.")
        if x.ip and x.ip.ip in (x.ip.network.network_address, x.ip.network.broadcast_address) and x.ip.network.prefixlen < 31:
            q.append(f"{x.device}:{x.name} uses the network/broadcast address {x.ip}.")
        for v in ([x.access_vlan] if x.access_vlan else []) + (x.trunk_vlans or []):
            if (x.device, v) not in vlans:
                q.append(f"{x.device}:{x.name} uses VLAN {v}, which is not defined on {x.device}.")
        if x.vrf and (x.device, x.vrf) not in vrfs:
            q.append(f"{x.device}:{x.name} is in VRF {x.vrf}, which is not defined on {x.device}.")
        if x.vrf and x.ospf_area:
            q.append(f"{x.device}:{x.name}: OSPF inside VRF {x.vrf} is not in the templates yet. Remove it or extend them.")
        if x.mode == "lag-member" and f"{x.device}:{x.lag}" not in ifs:
            q.append(f"{x.device}:{x.name} joins {x.lag}, which is not a described interface.")
        if x.ospf_area and not i.routing.ospf:
            q.append(f"{x.device}:{x.name} has an OSPF area but routing.ospf is null.")
    used = Counter(e for l in i.links for e in (l.a, l.b))
    for e, c in used.items():
        if e not in ifs:
            q.append(f"Link endpoint {e} has no interface definition.")
        if c > 1:
            q.append(f"{e} is cabled {c} times.")
    for l in i.links:
        ia, ib = ifs.get(l.a), ifs.get(l.b)
        if ia and ib and ia.ip and ib.ip and ia.ip.network != ib.ip.network:
            q.append(f"Link {l.a} ({ia.ip}) <-> {l.b} ({ib.ip}) ends are in different subnets.")
        if ia and ib and ia.mode != ib.mode and {ia.mode, ib.mode} != {"access", "routed"}:
            q.append(f"Link {l.a} ({ia.mode}) <-> {l.b} ({ib.mode}): the two ends do not match.")
    g = nx.Graph(); g.add_nodes_from(devs)
    g.add_edges_from((l.a.split(":")[0], l.b.split(":")[0]) for l in i.links)
    if devs and not nx.is_connected(g):
        q.append(f"Topology is not connected: {[sorted(c) for c in nx.connected_components(g)]}. Missing links?")
    # routing: every next hop / neighbour must be reachable from what was described, or explicitly external
    connected = defaultdict(list)
    for x in i.interfaces:
        if x.ip:
            connected[(x.device, x.vrf)].append(x.ip.network)
    all_ips = {x.ip.ip for x in i.interfaces if x.ip} | {d.loopback.ip for d in i.devices if d.loopback}
    for r in i.routing.static_routes:
        if r.next_hop != "blackhole" and not any(r.next_hop in n for n in connected[(r.device, r.vrf)]):
            q.append(f"{r.device}: static route {r.prefix} via {r.next_hop} is not on a connected subnet of {r.device}.")
    for b in i.routing.bgp:
        if b.device not in devs:
            q.append(f"BGP is described for unknown device {b.device}.")
        for n in b.neighbors:
            qid = f"bgp-external:{b.device}:{n.ip}"
            if n.ip not in all_ips and answer(qid) != "yes":
                q.append((qid, f"{b.device}: BGP neighbour {n.ip} (AS {n.remote_as}) is not an address in this "
                               f"description. Is it an external peer outside the network? Answer: yes"))
        if devs.get(b.device) and devs[b.device].platform == "frr" and not b.ebgp_without_policy and \
                any(n.remote_as != b.asn for n in b.neighbors):
            q.append(f"{b.device} (FRR) has eBGP neighbours but no routing policy: FRR exchanges no eBGP routes "
                     f"without one. Set ebgp_without_policy: true to allow it, or describe the policy.")
    for p in i.policies:
        if p.apply_on not in ifs:
            q.append(f"Policy {p.name} applies on {p.apply_on}, which is not a described interface.")
    seqs = Counter((p.apply_on, p.direction, p.sequence) for p in i.policies)
    q += [f"Two policies on {k[0]} {k[1]} share sequence {k[2]}." for k, c in seqs.items() if c > 1]
    for t in i.acceptance_tests:
        if t.from_ not in ifs:
            q.append(f"Test {t.name} starts at {t.from_}, which is not a described interface.")
    tests = {t.name for t in i.acceptance_tests}
    q += [f"Failure test {f.name} refers to unknown test {f.then}." for f in i.failure_tests if f.then not in tests]
    q += [f"Failure test {f.name} shuts {x}, which is not a described interface."
          for f in i.failure_tests for x in f.down if x not in ifs]
    return q


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intake"); ap.add_argument("--prompt"); ap.add_argument("--llm")
    ap.add_argument("--base", help="draft from b0_prefill.py: its device facts are kept, the LLM only fills its nulls")
    a = ap.parse_args()
    if a.intake:
        raw = load_yaml(a.intake)
    else:
        raw = load_yaml(a.base) if a.base else {}
        llm = draft_from_prompt(a.prompt, a.llm)
        if a.base:
            # facts read from the devices win; the prompt never overrides them. Interfaces are merged per
            # device:name so the LLM's design (mode, ip, ospf...) lands on the interfaces that really exist.
            facts = {x["name"]: x for x in raw.get("devices") or []}
            for d in llm.get("devices") or []:
                if d.get("name") in facts:
                    facts[d["name"]].update({k: v for k, v in d.items() if facts[d["name"]].get(k) is None})
            have = {(x["device"], x["name"]): x for x in raw.get("interfaces") or []}
            for x in llm.get("interfaces") or []:
                key = (x.get("device"), x.get("name"))
                if key in have:
                    have[key].update({k: v for k, v in x.items() if have[key].get(k) is None})
                else:
                    raw.setdefault("interfaces", []).append(x)   # not on the device: b1/b2 report it
            for k, v in llm.items():
                if k not in ("devices", "interfaces") and raw.get(k) in (None, []):
                    raw[k] = v
        else:
            raw = llm
    try:
        intake = Intake.model_validate(raw)
    except ValidationError as e:
        dump_yaml(raw, OUT / "intake_draft.yaml")
        ask([f"Missing or invalid: {'.'.join(map(str, er['loc']))} ({er['msg']})" for er in e.errors()])
    ask(consistency(intake))

    env = Environment(loader=FileSystemLoader(ROOT / "templates"), undefined=StrictUndefined,
                      trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    cand = OUT / "batfish" / "candidate" / "configs"
    shutil.rmtree(cand.parent, ignore_errors=True); cand.mkdir(parents=True)
    chg = OUT / "changes"; shutil.rmtree(chg, ignore_errors=True); chg.mkdir()
    model = {"source": "described by user (Flow B)", "devices": {}, "not_formally_verified": [],
             "links": [l.model_dump() | {"source": "user"} for l in intake.links]}
    for d in intake.devices:
        ifaces = [x for x in intake.interfaces if x.device == d.name]
        pols = sorted((p for p in intake.policies if p.apply_on.startswith(d.name + ":")), key=lambda p: p.sequence)
        filters = []
        for (on, direction) in dict.fromkeys((p.apply_on, p.direction) for p in pols):
            ifn = on.split(":", 1)[1]
            filters.append({"interface": ifn, "direction": direction,
                            "acl": f"ANO-{re.sub(r'[^A-Za-z0-9]', '-', ifn)}-{direction.upper()}",
                            "policies": [p for p in pols if (p.apply_on, p.direction) == (on, direction)]})
        cfg = env.get_template(f"{d.platform}/full_device.j2").render(
            dev=d, ifaces=ifaces, filters=filters, ospf=intake.routing.ospf, services=intake.services,
            vlans=[v for v in intake.vlans if v.device == d.name], vrfs=[v for v in intake.vrfs if v.device == d.name],
            statics=[r for r in intake.routing.static_routes if r.device == d.name],
            bgp=next((b for b in intake.routing.bgp if b.device == d.name), None),
            login=intake.device_login, monitoring=intake.monitoring,
            route_dev={str(r.prefix): next((x.name for x in ifaces if x.ip and r.next_hop != "blackhole"
                                            and ipaddress.ip_address(r.next_hop) in x.ip.network), None)
                       for r in intake.routing.static_routes if r.device == d.name},
            forwards=forwarding(intake, d.name) == "yes")
        gaps = write_batfish(cand.parent, d.platform, d.name, cfg)
        bf = gaps is not None
        if not bf:
            model["not_formally_verified"].append(d.name)
        elif gaps:
            print(f"{d.name}: parts Batfish does not model (twin test only): {gaps}")
        (chg / f"{d.name}.cfg").write_text(re.sub(r"\nend\s*$", "\n", cfg))
        model["devices"][d.name] = {
            "mgmt_ip": str(d.mgmt_ip.ip), "mgmt_interface": d.mgmt_interface, "cred_profile": d.cred_profile,
            "ssh_port": d.ssh_port, "platform": d.platform, "vendor": d.vendor, "model": d.hw_model,
            "os_version": d.os_version, "batfish": bool(bf),
            "interfaces": {x.name: {"ipv4": [str(x.ip)] if x.ip else [], "vrf": x.vrf, "mode": x.mode} for x in ifaces}
                          | ({d.loopback_interface: {"ipv4": [str(d.loopback)]}} if d.loopback else {})
                          | {d.mgmt_interface: {"ipv4": [str(d.mgmt_ip)]}}}
    dump_yaml(model, OUT / "model.yaml")
    print(f"Intake valid. {len(intake.devices)} devices, {len(intake.links)} links.")
    if model["not_formally_verified"]:
        print(f"Batfish does not support {model['not_formally_verified']}: twin tests are their only proof.")
    print(f"Configs -> {chg}\nBatfish -> {cand}\nModel   -> {OUT/'model.yaml'}")


if __name__ == "__main__":
    main()
