"""Step A7 (mandatory): digital twin proof with Batfish, before anything is deployed.

Usage (Flow A): python a5_batfish.py --base out/batfish/base --candidate out/batfish/candidate --intent out/intent.json
Usage (Flow B): python a5_batfish.py --candidate out/batfish/candidate --tests inputs/flowb_intake.yaml
Needs the Batfish container on localhost:9996 (see setup/00-install.sh). Exit code 0 = all checks passed.
"""
from __future__ import annotations
import argparse, ipaddress, json, re, sys
from pybatfish.client.session import Session
from pybatfish.datamodel import HeaderConstraints, PathConstraints
from pybatfish.datamodel.primitives import Interface
from common import load_yaml


def existing_ips(net: str) -> list[str]:
    """Addresses inside <net> that a device in the model really owns (servers, firewalls, router interfaces)."""
    try:
        from common import OUT
        m = load_yaml(OUT / "model.yaml")
    except Exception:
        return []
    n = ipaddress.ip_network(net)
    return sorted({str(ipaddress.ip_interface(c).ip) for d in m["devices"].values() for i in d["interfaces"].values()
                   for c in i.get("ipv4", []) if ipaddress.ip_interface(c).ip in n})


def hc(t: dict) -> HeaderConstraints:
    dst = str(t["dst"])
    if t["expect"] == "permit" and existing_ips(dst):
        # 'must work' means: reaches every address that exists there. Unused addresses can never answer, and
        # Batfish reports them NEIGHBOR_UNREACHABLE (verified), which is not a failure of the change.
        dst = ",".join(existing_ips(dst))
    kw = {"srcIps": str(t["src"]), "dstIps": dst}
    if t["protocol"] != "ip":
        kw["ipProtocols"] = [t["protocol"].upper()]
    if t.get("dst_port"):
        kw["dstPorts"] = str(t["dst_port"])
    return HeaderConstraints(**kw)


def tests_from_intent(p: str) -> list[dict]:
    d = json.load(open(p))
    i, e = d["intent"], d["enforce"]
    start = f"@enter({e['device']}[{e['interface']}])"
    expect = "deny" if i["kind"] == "block_traffic" else "permit"
    tests = [{"name": f"intent: {i['name']}", "start": start, **i["match"], "expect": expect}]
    tests += [{"name": f"must still work #{n}", "start": start, **t, "expect": "permit"}
              for n, t in enumerate(i["must_still_work"], 1)]
    return tests


def tests_from_intake(p: str) -> list[dict]:
    return [{"name": t["name"], "start": f"@enter({t['from'].replace(':', '[', 1)}])", **t}
            for t in load_yaml(p)["acceptance_tests"]]


def _model():
    try:
        from common import OUT
        return load_yaml(OUT / "model.yaml")
    except Exception:
        return {"devices": {}}


def run_filters(bf: Session, snap: str, t: dict, dev: str, chains: dict) -> bool:
    """Rule-level proof on a device whose filters Batfish does not place on the path (see platforms.py)."""
    own = {str(ipaddress.ip_interface(c).ip) for i in _model()["devices"].get(dev, {}).get("interfaces", {}).values()
           for c in i.get("ipv4", [])}
    h = hc(t)
    dst = [x for x in h.dstIps.split(",")]
    to_self = [ip for ip in own if any(ipaddress.ip_address(ip) in ipaddress.ip_network(d, strict=False) for d in dst)]
    bad = "permit" if t["expect"] == "deny" else "deny"
    ok = True
    for chain, ips in ((chains["through"], h.dstIps), (chains["to_self"], ",".join(to_self))):
        if not ips:
            continue
        hh = HeaderConstraints(srcIps=h.srcIps, dstIps=ips, ipProtocols=h.ipProtocols, dstPorts=h.dstPorts)
        df = bf.q.searchFilters(headers=hh, nodes=dev, filters=chain, startLocation=t["start"],
                                action=bad).answer(snapshot=snap).frame()
        print(f"  [{'ok' if df.empty else 'no'}] {t['name']}: {dev} {chain} {'denies' if t['expect'] == 'deny' else 'permits'} all of it (dst {ips})")
        if not df.empty:
            print("        counter-example:", df.iloc[0]["Flow"], "->", df.iloc[0].get("Action"), df.iloc[0].get("Line_Content"))
        ok &= df.empty
    return ok


def filter_hops(bf: Session, snap: str, t: dict) -> list[tuple[str, str]]:
    """Devices on the test's path whose filters Batfish does not apply on the path, with their ingress interface."""
    from platforms import BATFISH_FILTER_ONLY
    devs = {h.lower(): (h, d.get("platform")) for h, d in _model()["devices"].items()}
    fr = bf.q.traceroute(startLocation=t["start"], headers=hc(t)).answer(snapshot=snap).frame()
    out = []
    for traces in fr.get("Traces", []):
        for tr in traces:
            for hop in tr.hops:
                name, plat = devs.get(hop.node.lower(), (hop.node, None))
                for st in hop.steps:
                    iif = getattr(st.detail, "inputInterface", None)
                    if iif and plat in BATFISH_FILTER_ONLY and (name, iif) not in out:
                        out.append((name, iif))
    return out


def run(bf: Session, snap: str, t: dict) -> bool:
    """Path proof with Batfish reachability. A firewall whose rules Batfish cannot place on the path (Linux) is
    proven rule-by-rule at the interface where the traffic enters it, and combined with the path result."""
    from platforms import BATFISH_FILTER_ONLY
    hops = filter_hops(bf, snap, t)
    if not hops:
        return run_path(bf, snap, t)
    plat = {h: d.get("platform") for h, d in _model()["devices"].items()}
    res = [run_filters(bf, snap, dict(t, start=f"@enter({d}[{i}])"), d, BATFISH_FILTER_ONLY[plat[d]]) for d, i in hops]
    if t["expect"] == "deny":
        ok = any(res) or run_path(bf, snap, t)
        print(f"  [{'PASS' if ok else 'FAIL'}] {t['name']}: denied by {'firewall rules on the path' if any(res) else 'the path'}"
              if ok else f"  [FAIL] {t['name']}: no firewall on the path and no path filter denies all of it")
        return ok
    return run_path(bf, snap, t) and all(res)


def run_path(bf: Session, snap: str, t: dict) -> bool:
    # look for the OPPOSITE of what is expected; an empty answer proves the expectation for ALL matching packets
    bad = "SUCCESS" if t["expect"] == "deny" else "FAILURE"
    ans = bf.q.reachability(pathConstraints=PathConstraints(startLocation=t["start"]),
                            headers=hc(t), actions=bad).answer(snapshot=snap)
    if not hasattr(ans, "frame"):     # Batfish could not run the question (e.g. unknown start interface)
        print(f"  [FAIL] {t['name']}: Batfish could not evaluate it: {str(ans)[:200]}")
        return False
    df = ans.frame()
    ok = df.empty
    print(f"  [{'PASS' if ok else 'FAIL'}] {t['name']}: expect {t['expect']} (dst {hc(t).dstIps})")
    if not ok:
        print("        counter-example:", df.iloc[0]["Flow"], "->", df.iloc[0]["Traces"][0].disposition)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--base")
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--intent")
    ap.add_argument("--tests")
    ap.add_argument("--accept-collateral", action="store_true",
                    help="user accepts changed flows that are inside the intent's destination but outside its match "
                         "(only for platforms with a limited method, e.g. FRR null route)")
    a = ap.parse_args()

    bf = Session(host=a.host)
    bf.set_network("ano")
    ok = True
    if a.base:
        bf.init_snapshot(a.base, name="base", overwrite=True)
    bf.init_snapshot(a.candidate, name="candidate", overwrite=True)

    print("1. Parse check (unsupported or unparsed lines are reported, never ignored)")
    status = bf.q.fileParseStatus().answer(snapshot="candidate").frame()
    for _, r in status.iterrows():
        flag = "PASS" if r["Status"] == "PASSED" else "WARN"
        print(f"  [{flag}] {r['File_Name']}: {r['Status']}")
        if r["Status"] not in ("PASSED", "PARTIALLY_UNRECOGNIZED"):
            print("        -> Batfish cannot verify this device: mark 'not formally verified', rely on Containerlab test")
    warn = bf.q.parseWarning().answer(snapshot="candidate").frame()
    # a line ANO is about to push that Batfish cannot read means the proof would not cover it: hard stop
    from common import OUT
    new_lines = {l.strip() for p in (OUT / "changes").glob("*.cfg") for l in p.read_text().splitlines() if l.strip()}
    import re
    from platforms import BATFISH_MGMT_ONLY
    try:
        plat = {h: d["platform"] for h, d in load_yaml(OUT / "model.yaml")["devices"].items()}
    except Exception:
        plat = {}
    for _, r in warn.iterrows():
        text = r["Text"].strip()
        dev = r["Filename"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
        mgmt = any(re.match(rx, text) for rx in BATFISH_MGMT_ONLY.get(plat.get(dev), []))
        mine = text in new_lines and not mgmt
        tag = "FAIL" if mine else ("INFO" if mgmt else "WARN")
        print(f"  [{tag}] {r['Filename']} line {r['Line']}: {r['Text']!r} ({r['Comment']})"
              + ("  <- part of the change: Batfish cannot verify it" if mine else
                 "  (management plane only, cannot change forwarding)" if mgmt else ""))
        ok &= not mine

    # conversion warnings: Batfish read the line but SKIPPED it (e.g. iptables NAT targets). Same rule as above.
    issues = bf.q.initIssues().answer(snapshot="candidate").frame()
    for _, r in issues.iterrows():
        skipped = re.findall(r"Skipped '([^']+)'", str(r["Details"]))
        mine = any(sk.strip() in l for sk in skipped for l in new_lines)
        if skipped:
            print(f"  [{'FAIL' if mine else 'WARN'}] {r['Nodes']}: {str(r['Details'])[:160]}"
                  + ("  <- part of the change: Batfish cannot verify it" if mine else ""))
        ok &= not mine

    tests = tests_from_intent(a.intent) if a.intent else tests_from_intake(a.tests)
    if a.base and a.intent:
        print("2. Sanity: is the intent already true today?")
        t0 = dict(tests[0]); t0["expect"] = "permit" if t0["expect"] == "deny" else "deny"
        t0["name"] = "baseline (should FAIL the intent, i.e. traffic currently allowed)"
        if not run(bf, "base", t0):
            print("        -> the network already behaves this way; tell the user, nothing to change")
    try:
        from common import OUT, load_yaml as _ly
        nv = _ly(OUT / "model.yaml").get("not_formally_verified") or []
        if nv:
            print(f"  [WARN] not formally verified (Batfish does not support them): {nv} -> twin tests are the proof")
    except Exception:
        pass
    print("3. Intent and must-still-work checks on the candidate")
    for t in tests:
        ok &= run(bf, "candidate", t)

    if a.base:
        print("4. Collateral damage: every flow whose outcome changed")
        diff = bf.q.differentialReachability().answer(snapshot="candidate", reference_snapshot="base").frame()
        m = json.load(open(a.intent))["intent"]["match"] if a.intent else None
        for _, r in diff.iterrows():
            f = r["Flow"]
            src, dst = ipaddress.ip_address(f.srcIp), ipaddress.ip_address(f.dstIp)
            in_dst = bool(m) and dst in ipaddress.ip_network(m["dst"])
            expected = in_dst and src in ipaddress.ip_network(m["src"]) and \
                (m["protocol"] == "ip" or f.ipProtocol.upper() == m["protocol"].upper()) and \
                (not m.get("dst_port") or f.dstPort == m["dst_port"])
            if expected:
                print(f"  [OK] changed (intended): {f}")
            elif in_dst and a.accept_collateral:
                print(f"  [ACCEPTED BY USER] collateral: {f}")
            else:
                print(f"  [FAIL] collateral: {f}")
                ok = False
        if diff.empty:
            print("  (no flow changed on the forwarding path)")
        # devices whose filters are not on Batfish's path: every rule whose behaviour changed must be one ANO added
        from platforms import BATFISH_FILTER_ONLY
        from common import OUT
        added = {re.sub(r"^iptables -I \S+ 1 ", "", l.strip()) for p in (OUT / "changes").glob("*.cfg")
                 for l in p.read_text().splitlines()}
        for dev, d in _model()["devices"].items():
            if d.get("platform") in BATFISH_FILTER_ONLY:
                cf = bf.q.compareFilters(nodes=dev).answer(snapshot="candidate", reference_snapshot="base").frame()
                for _, r in cf.iterrows():
                    mine = r["Line_Content"] in added
                    print(f"  [{'OK' if mine else 'FAIL'}] {dev} {r['Filter_Name']}: '{r['Line_Content']}' now "
                          f"{r['Line_Action']} (was '{r['Reference_Line_Content']}')"
                          + ("" if mine else "  <- a rule ANO did not add changed behaviour"))
                    ok &= mine

    if a.tests:
        print("4b. Failure tests (Batfish forks the snapshot with interfaces shut)")
        by_name = {t["name"]: t for t in tests}
        for f in load_yaml(a.tests).get("failure_tests", []):
            down = [Interface(hostname=x.split(":")[0], interface=x.split(":", 1)[1]) for x in f["down"]]
            bf.fork_snapshot("candidate", name=f"fail-{f['name']}", deactivate_interfaces=down, overwrite=True)
            t = dict(by_name[f["then"]]); t["name"] = f"{f['name']} -> {f['then']}"
            ok &= run(bf, f"fail-{f['name']}", t)

    print("5. Forwarding loops")
    loops = bf.q.detectLoops().answer(snapshot="candidate").frame()
    print(f"  [{'PASS' if loops.empty else 'FAIL'}] {len(loops)} loop(s)")
    ok &= loops.empty

    print("\nRESULT:", "ALL CHECKS PASSED" if ok else "FAILED - back to intent/render step (max 3 tries), then ask user")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
