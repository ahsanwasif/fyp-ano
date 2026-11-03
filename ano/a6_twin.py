"""Step A8 (mandatory): generate the virtual network (Containerlab twin) from the model.

Usage:  python a6_twin.py [--endpoints FILE] [--kind arista_eos=arista_ceos] [--mgmt 172.31.2.0/24] [--blank]
Images: one env var per Containerlab kind, set by the user, e.g. ANO_IMAGE_ARISTA_CEOS=ceos:4.32.0F
Then:   cd out/twin && sudo -E containerlab deploy -t twin.clab.yml
Output: out/twin/twin.clab.yml, out/twin/configs/ (or out/twin/changes/ with --blank), out/twin/inventory.yaml
"""
from __future__ import annotations
import argparse, ipaddress, os, re
from common import OUT, answer, ask, dump_yaml, load_yaml
from platforms import CLAB_KINDS, adapter, image_env


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoints")
    ap.add_argument("--mgmt", default="172.31.2.0/24", help="management subnet for the twin (must not clash)")
    ap.add_argument("--model", default=str(OUT / "model.yaml"))
    ap.add_argument("--configs", default=str(OUT / "configs"), help="raw running configs (not the Batfish copies)")
    ap.add_argument("--name", default="ano-twin")
    ap.add_argument("--kind", action="append", default=[], help="platform=containerlab_kind, chosen by the user")
    ap.add_argument("--blank", action="store_true",
                    help="Flow B: twin boots blank (like new hardware); full configs are pushed by deploy.py")
    a = ap.parse_args()
    model = load_yaml(a.model)
    chosen = dict(x.split("=", 1) for x in a.kind)
    net = ipaddress.ip_network(a.mgmt)
    pool = iter(list(net.hosts())[10:])

    questions, nodes, kinds, inv = [], {}, {}, {}
    # Physical boxes use names the virtual image may not have (Ethernet49/1, xe-0/0/0, GigabitEthernet1/0/24).
    # ANO never guesses a substitute: the user names the twin interface once, and the twin copy of the config
    # is renamed to match (the real device and Batfish keep the real names).
    rename: dict[str, dict[str, str]] = {}

    def port(dev: str, ifn: str):
        ad = adapter(model["devices"][dev]["platform"])
        try:
            return ad["ifmap"](ifn)
        except ValueError:
            alt = answer(f"twin-if:{dev}:{ifn}")
            if alt:
                try:
                    p_ = ad["ifmap"](alt)
                    rename.setdefault(dev, {})[ifn] = alt
                    return p_
                except ValueError:
                    pass
            questions.append((f"twin-if:{dev}:{ifn}",
                              f"{dev} interface {ifn} has no equivalent in the virtual image. Which twin interface "
                              f"should stand in for it (e.g. a free Ethernet<N> / eth<N>)? Its config is renamed in "
                              f"the twin only."))
            return None

    links = []
    for l in model["links"]:
        eps = [f"{e.split(':', 1)[0]}:{port(*e.split(':', 1))}" for e in (l["a"], l["b"])]
        links.append({"endpoints": eps})
    ep_list = load_yaml(a.endpoints)["endpoints"] if a.endpoints else []
    ep_ports = {e["name"]: port(*e["attach"].split(":", 1)) for e in ep_list}
    ask(questions)

    def twin_cfg(dev: str, cfg: str) -> str:
        for old, new in rename.get(dev, {}).items():
            cfg = re.sub(rf"(?<![\w/.]){re.escape(old)}(?![\w/.])", new, cfg)
        return cfg

    out = OUT / "twin"
    (out / "configs").mkdir(parents=True, exist_ok=True)
    for dev, d in model["devices"].items():
        p, ad = d["platform"], adapter(d["platform"])
        options = CLAB_KINDS.get(p, [])
        kind = chosen.get(p) or (options[0] if len(options) == 1 else None)
        if not options:
            questions.append(f"{dev} ({p}): no known emulation kind. Upload an image/kind, or accept "
                             f"'not emulated' (Batfish-only) for this device.")
            continue
        if not kind:
            questions.append(f"{dev} ({p}, model {d.get('model')}): which emulation kind matches it? "
                             f"Options {options}. Re-run with --kind {p}=<kind>.")
            continue
        if not ad:
            questions.append(f"{dev} ({p}): no CLI adapter in platforms.py (interface mapping, management rewrite).")
            continue
        image = os.environ.get(image_env(kind))
        if not image:
            questions.append(f"{dev} runs {d.get('vendor')} {d.get('model')} OS {d.get('os_version')}. "
                             f"Set {image_env(kind)} to a {kind} image of that same OS version.")
            continue
        kinds[kind] = {"image": image}
        ip = str(next(pool))
        mif = d.get("mgmt_interface")
        if a.blank:
            # the pushed config must keep the TWIN's management IP, or deploy.py would cut its own SSH session
            (out / "changes").mkdir(exist_ok=True)
            chg = twin_cfg(dev, (OUT / "changes" / f"{dev}.cfg").read_text())
            (out / "changes" / f"{dev}.cfg").write_text(ad["mgmt"](chg, ip, net.prefixlen, mif))
            nodes[dev] = {"kind": kind, "mgmt-ipv4": ip, **ad["clab"](chg)}
        else:
            cfg = twin_cfg(dev, open(os.path.join(a.configs, f"{dev}.cfg")).read())
            (out / "configs" / f"{dev}.cfg").write_text(ad["mgmt"](cfg, ip, net.prefixlen, mif))
            boot = {"binds": [f"configs/{dev}.cfg:/ano/config.sh:ro"], "exec": ["sh /ano/config.sh"]} \
                if ad.get("startup") == "exec" else {"startup-config": f"configs/{dev}.cfg"}
            nodes[dev] = {"kind": kind, "mgmt-ipv4": ip, **boot, **ad["clab"](cfg)}
        inv[dev] = {"hostname": ip, "platform": p, "cred_profile": d.get("cred_profile", 1)}
    ask(questions)

    if ep_list:
        # every data-plane subnet in the model, so endpoints can reach all of it (nothing like 10/8 assumed)
        nets = sorted({str(ipaddress.ip_interface(c).network) for d in model["devices"].values()
                       for ifn, i in d["interfaces"].items() if ifn != d.get("mgmt_interface") for c in i["ipv4"]})
        for e in ep_list:
            dev, ifn = e["attach"].split(":", 1)
            own = str(ipaddress.ip_interface(e["ip"]).network)
            nodes[e["name"]] = {"kind": "linux", "image": "ghcr.io/hellt/network-multitool:latest",
                                "exec": [f"ip addr add {e['ip']} dev eth1"] +
                                        [f"ip route add {n} via {e['gateway']}" for n in nets if n != own]}
            links.append({"endpoints": [f"{e['name']}:eth1", f"{dev}:{ep_ports[e['name']]}"]})

    dump_yaml({"name": a.name, "mgmt": {"network": f"{a.name}-mgmt", "ipv4-subnet": str(net)},
               "topology": {"kinds": kinds, "nodes": nodes, "links": links}}, out / "twin.clab.yml")
    dump_yaml(inv, out / "inventory.yaml")
    print(f"Twin: {len(nodes)} nodes, {len(links)} links -> {out/'twin.clab.yml'}")
    print(f"Next: cd {out} && sudo -E containerlab deploy -t twin.clab.yml")


if __name__ == "__main__":
    main()
