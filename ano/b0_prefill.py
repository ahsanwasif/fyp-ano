"""Flow B, step B0 (when SSH access is given): read the devices first, so the user only describes the DESIGN.

  python a1_discover.py --range <mgmt range>          # same discovery as Flow A, on the new/blank devices
  python a2_model.py --no-batfish                      # cabling from LLDP; one-sided cables become questions
  python b0_prefill.py                                 # -> out/intake_draft.yaml
  python b1_intake.py --intake out/intake_draft.yaml   # every null is listed as a question
  python b1_intake.py --base out/intake_draft.yaml --prompt "..." --llm <model>   # or let the LLM fill the design

What is FACT (read from the device) is filled in: name, vendor, platform, OS version, hardware model, management IP and
interface, SSH port, credential profile, the list of interfaces that exist, and the cables LLDP reported.
What is DESIGN (role, addressing, VLANs, routing, policies, tests...) is left null or empty: ANO never guesses it.
b2_reality_check.py still runs before the push, because devices can change between B0 and the deploy.
"""
from __future__ import annotations
import argparse, ipaddress, json, re
from collections import Counter
from common import OUT, dump_yaml, load_yaml

LOOPBACK = re.compile(r"^(lo\d*|loopback\d*)$", re.I)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--discovery", default=str(OUT / "discovery.json"))
    ap.add_argument("--model", default=str(OUT / "model.yaml"))
    ap.add_argument("--out", default=str(OUT / "intake_draft.yaml"))
    a = ap.parse_args()
    disc = json.loads(open(a.discovery).read())
    model = load_yaml(a.model)
    by_ip = {ip: d for ip, d in disc["devices"].items() if d.get("collector") != "none"}

    names = Counter(d["facts"]["hostname"] for d in by_ip.values())
    devices, interfaces, notes = [], [], []
    for name, m in model["devices"].items():
        d = by_ip[m["mgmt_ip"]]
        mgmt_cidr = next((c for c in m["interfaces"].get(m["mgmt_interface"] or "", {}).get("ipv4", [])
                          if ipaddress.ip_interface(c).ip == ipaddress.ip_address(m["mgmt_ip"])), None)
        devices.append({
            "name": name, "vendor": m["vendor"], "platform": m["platform"], "os_version": m["os_version"],
            "hw_model": m["model"], "role": None, "mgmt_ip": mgmt_cidr, "mgmt_interface": m["mgmt_interface"],
            "ssh_port": m["ssh_port"], "cred_profile": m["cred_profile"],
            "loopback_interface": None, "loopback": None, "loopback_ospf_area": None, "router_id": None,
            "existing_config": None,
        })
        if mgmt_cidr is None:
            notes.append(f"{name}: the management prefix length was not readable; fill devices[].mgmt_ip as a.b.c.d/len")
        # every interface that physically exists on the box (not only the ones with an IP)
        for ifn in sorted(d.get("get_interfaces") or {}):
            if ifn == m["mgmt_interface"] or LOOPBACK.match(ifn):
                continue
            interfaces.append({"device": name, "name": ifn, "mode": None, "description": None, "ip": None,
                               "vrf": None, "access_vlan": None, "trunk_vlans": None, "lag": None, "lag_mode": None,
                               "ospf_area": None, "ospf_passive": None, "ospf_network": None, "mtu": None})
    dups = [n for n, c in names.items() if c > 1]
    if dups:
        notes.append(f"Several blank devices report the same hostname {dups}; give each a unique name.")

    links = [{"a": l["a"], "b": l["b"]} for l in model.get("links", [])]
    if not links:
        notes.append("No cabling was readable (no LLDP on these devices): describe links[] yourself; "
                     "b2_reality_check.py then asks you to confirm each cable.")

    draft = {
        "intent": None, "starting_point": None, "device_login": None,
        "devices": devices, "vlans": [], "vrfs": [], "interfaces": interfaces, "links": links,
        "routing": None, "policies": [], "services": None, "acceptance_tests": None, "failure_tests": None,
        "emulation_images": None, "change_constraints": None, "monitoring": None,
    }
    dump_yaml(draft, a.out)
    hdr = ("# Flow B draft, pre-filled from the devices over SSH (b0_prefill.py).\n"
           "# Facts read from the devices are filled in. Every null is a design decision for you (or the LLM).\n"
           "# Interfaces you do not use: delete their entries. starting_point: greenfield (blank boxes) or brownfield\n"
           "# (they already carry config: set devices[].existing_config).\n")
    hdr += "".join(f"# NOTE: {n}\n" for n in notes)
    with open(a.out) as f:
        body = f.read()
    with open(a.out, "w") as f:
        f.write(hdr + body)
    print(f"Draft: {len(devices)} devices, {len(interfaces)} interfaces, {len(links)} cables -> {a.out}")
    for n in notes:
        print("NOTE:", n)


if __name__ == "__main__":
    main()
