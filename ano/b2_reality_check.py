"""Flow B, B-deploy gate: before pushing, prove the real devices match what the user described.

  python b2_reality_check.py --intake inputs/flowb_intake.yaml
Compares over SSH: OS version + hardware model, interface names, cabling (LLDP where the platform has it).
Brownfield: running config vs the uploaded config. Any mismatch = STOP.
Platforms without NAPALM (e.g. FRR) are read with their show commands from platforms.py; a platform with no
LLDP cannot prove its cables, so each such cable must be confirmed in inputs/answers.yaml.
"""
from __future__ import annotations
import argparse, difflib, sys
from nornir.core.task import Result, Task
from common import OUT, load_yaml
from deploy import init_nornir
from common import answer
from platforms import SHOW, STATE_PARSERS


def check(task: Task, intake: dict) -> Result:
    h = task.host.name
    d = next(x for x in intake["devices"] if x["name"] == h)
    if task.host.data.get("napalm_driver"):
        dev = task.host.get_connection("napalm", task.nornir.config)
        f, ifs, lldp = dev.get_facts(), dev.get_interfaces(), dev.get_lldp_neighbors()
        running_cfg = lambda: dev.get_config(retrieve="running")["running"]
    else:
        conn = task.host.get_connection("netmiko", task.nornir.config)
        if getattr(conn, "secret", None) and not conn.check_enable_mode():
            conn.enable()          # e.g. Linux: sudo for iptables (ANO_SECRET<n>)
        raw = {k: conn.send_command(c, read_timeout=60) for k, c in SHOW[d["platform"]].items()}
        st = STATE_PARSERS[d["platform"]](raw)
        f, ifs, lldp = st["get_facts"], st["get_interfaces"], None
        running_cfg = lambda: raw["config"]
    bad = []
    if d["os_version"] not in f["os_version"]:
        bad.append(f"OS is {f['os_version']}, described {d['os_version']}")
    if d["hw_model"].lower() not in f["model"].lower():
        bad.append(f"model is {f['model']}, described {d['hw_model']}")
    for x in intake["interfaces"]:
        if x["device"] == h and x["name"] not in ifs:
            bad.append(f"interface {x['name']} does not exist on the device")
    for l in intake["links"]:
        for me, peer in ((l["a"], l["b"]), (l["b"], l["a"])):
            md, mi = me.split(":", 1)
            if md != h:
                continue
            pd, pi = peer.split(":", 1)
            if lldp is None:
                qid = f"cable:{l['a']}|{l['b']}"
                if answer(qid) != "checked":
                    bad.append(f"{mi}: no LLDP on {d['platform']}, so the cable to {pd}:{pi} cannot be proven. "
                               f"Check it physically, then answer in answers.yaml  '{qid}': checked")
                continue
            seen = [(n["hostname"].split(".")[0], n["port"]) for n in lldp.get(mi, [])]
            if (pd, pi) not in seen:
                bad.append(f"{mi}: described cable to {pd}:{pi}, LLDP sees {seen or 'nothing'}")
    if intake["starting_point"] == "brownfield" and d.get("existing_config"):
        running = running_cfg().splitlines()
        given = open(d["existing_config"]).read().splitlines()
        diff = [l for l in difflib.unified_diff(given, running, lineterm="", n=0) if l[:1] in "+-" and l[:3] not in ("+++", "---")]
        if diff:
            bad.append(f"running config differs from the uploaded one in {len(diff)} line(s), e.g. {diff[:3]}")
    return Result(host=task.host, result=bad, failed=bool(bad))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intake", required=True)
    a = ap.parse_args()
    intake = load_yaml(a.intake)
    nr = init_nornir("prod", str(OUT / "model.yaml"))
    res = nr.run(task=check, intake=intake)
    stop = False
    for h, r in res.items():
        msg = r[0].result
        print(f"== {h}: " + ("matches the description" if not msg else "; ".join(map(str, msg if isinstance(msg, list) else [msg]))))
        stop |= r.failed
    print("\nREALITY CHECK", "FAILED: deploy blocked, the twin was built from a wrong description" if stop else "PASSED")
    sys.exit(1 if stop else 0)


if __name__ == "__main__":
    main()
