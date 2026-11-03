"""Steps A8/A11: post-change checks over SSH (twin or prod). Exit 0 = safe to --confirm.

  python postcheck.py --target twin
  python postcheck.py --target prod
Checks: every modelled link still has its LLDP neighbour and is up; every device pings one address of every other device (a /32 if it has one);
running configs are pulled to out/postcheck_<target>/configs for a Batfish re-check (a5_batfish.py --candidate).
"""
from __future__ import annotations
import argparse, ipaddress, json, sys
from nornir.core.task import Result, Task
from common import OUT, dump_json, load_yaml
from deploy import init_nornir
from platforms import CONFIG_BUILDERS, RIB_MATCH, SHOW, STATE_PARSERS, write_batfish


def probe_targets(model: dict) -> dict[str, str]:
    """One probe address per device, chosen by address, not by vendor interface naming:
    a /32 host address if the device has one (typically its loopback), else its first data-plane IPv4."""
    out = {}
    for h, d in model["devices"].items():
        addrs = [(ifn, ipaddress.ip_interface(c)) for ifn, v in d["interfaces"].items()
                 if ifn != d.get("mgmt_interface") for c in v["ipv4"]]
        host32 = [a for _, a in addrs if a.network.prefixlen == 32]
        pick = host32[0] if host32 else (addrs[0][1] if addrs else None)
        if pick:
            out[h] = str(pick.ip)
    return out


def _save_for_batfish(cfg_dir, h: str, platform: str, cfg: str) -> None:
    """Pulled running config, in the format Batfish needs, for the post-change re-proof (a5 --candidate)."""
    from platforms import adapter
    ad = adapter(platform)
    write_batfish(cfg_dir.parent, platform, h, ad["clean"](cfg) if ad else cfg)


def model_subnets(model: dict) -> set[str]:
    return {str(ipaddress.ip_interface(c).network) for d in model["devices"].values()
            for ifn, v in d["interfaces"].items() if ifn != d.get("mgmt_interface") for c in v["ipv4"]}


def check_cli(task: Task, model: dict, target: str) -> Result:
    """Platforms without NAPALM (e.g. FRR): interface state + routing table from the platform's show commands.
    A ping from vtysh cannot be bounded, so reachability is proven by the RIB here and by endpoint pings."""
    h, plat = task.host.name, model["devices"][task.host.name]["platform"]
    conn = task.host.get_connection("netmiko", task.nornir.config)
    if getattr(conn, "secret", None) and not conn.check_enable_mode():
        conn.enable()          # e.g. Linux: sudo for iptables (ANO_SECRET<n>)
    raw = {k: conn.send_command(c, read_timeout=60) for k, c in SHOW[plat].items()}
    st = STATE_PARSERS[plat](raw)
    errors = []
    for l in model["links"]:
        for me in (l["a"], l["b"]):
            d, ifn = me.split(":", 1)
            if d == h and not st["get_interfaces"].get(ifn, {}).get("is_up"):
                errors.append(f"{ifn} is down")
    rib = st.get("routes_json", {})
    covering = RIB_MATCH.get(plat) == "covering"
    for net in sorted(model_subnets(model)):
        n = ipaddress.ip_network(net)
        if covering:
            hit = any(n.subnet_of(ipaddress.ip_network(p)) and any(r.get("selected") for r in rs) for p, rs in rib.items())
        else:
            hit = any(r.get("selected") for r in rib.get(net, []))
        if not hit:
            errors.append(f"no active route to {net}")
    cfg_dir = OUT / f"postcheck_{target}" / "configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    _save_for_batfish(cfg_dir, h, plat, CONFIG_BUILDERS[plat](raw) if plat in CONFIG_BUILDERS else raw["config"])
    return Result(host=task.host, result=errors or ["all checks passed (interfaces up, every subnet routed)"],
                  failed=bool(errors))


def check(task: Task, model: dict, target: str) -> Result:
    h = task.host.name
    if not task.host.data.get("napalm_driver"):
        return check_cli(task, model, target)
    dev = task.host.get_connection("napalm", task.nornir.config)
    errors = []
    lldp, ifs = dev.get_lldp_neighbors(), dev.get_interfaces()
    for l in model["links"]:
        if l.get("source") == "batfish-l3":     # logical adjacency only; no LLDP expected
            continue
        for me, peer in ((l["a"], l["b"]), (l["b"], l["a"])):
            d, ifn = me.split(":", 1)
            if d != h:
                continue
            pd, pif = peer.split(":", 1)
            if not ifs.get(ifn, {}).get("is_up"):
                errors.append(f"{ifn} is down")
            if not any(n["hostname"].split(".")[0] == pd and n["port"] == pif for n in lldp.get(ifn, [])):
                errors.append(f"{ifn}: expected LLDP neighbour {pd}:{pif}, saw {lldp.get(ifn)}")
    lbs = probe_targets(model)
    for peer, ip in lbs.items():
        if peer == h:
            continue
        r = dev.ping(ip, source=lbs.get(h, ""), count=3)
        if "success" not in r or r["success"]["packet_loss"] == 3:
            errors.append(f"ping {peer} loopback {ip} failed")
    cfg_dir = OUT / f"postcheck_{target}" / "configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    _save_for_batfish(cfg_dir, h, model["devices"][h]["platform"], dev.get_config(retrieve="running")["running"])
    return Result(host=task.host, result=errors or ["all checks passed"], failed=bool(errors))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["twin", "prod"], required=True)
    ap.add_argument("--model", default=str(OUT / "model.yaml"))
    ap.add_argument("--baseline", action="store_true",
                    help="run BEFORE the commit: records problems that already exist, so they are not blamed on the change")
    a = ap.parse_args()
    model = load_yaml(a.model)
    nr = init_nornir(a.target, a.model)
    nr = nr.filter(filter_func=lambda h: h.name in model["devices"])
    res = nr.run(task=check, model=model, target=a.target)
    found = {h: (mr[0].result if mr.failed and isinstance(mr[0].result, list) else
                 [] if not mr.failed else [str(mr[0].result)]) for h, mr in res.items()}
    base_file = OUT / f"postcheck_{a.target}" / "baseline.json"
    if a.baseline:
        dump_json(found, base_file)
        for h, errs in found.items():
            print(f"== {h}: " + ("; ".join(errs) if errs else "ok"))
        print(f"\nBaseline saved -> {base_file} ({sum(map(len, found.values()))} pre-existing problem(s))")
        return
    before = json.loads(base_file.read_text()) if base_file.exists() else {}
    if not before:
        print("(no baseline: every problem counts as new. Run with --baseline before the commit.)")
    bad = False
    for h, errs in found.items():
        new = [e for e in errs if e not in before.get(h, [])]
        old = [e for e in errs if e in before.get(h, [])]
        print(f"== {h}: " + ("; ".join(new) if new else "no new problems")
              + (f"   [already before the change: {'; '.join(old)}]" if old else ""))
        bad |= bool(new)
    print("\nPOSTCHECK", "FAILED: do NOT confirm (roll back)" if bad else "PASSED (no new problems)")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
