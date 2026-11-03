"""Step A12 (mandatory): pick the monitoring method per device and generate every collector config.

Usage:  python a7_monitor.py [--gnmi-port r1=6030] [--snmp-ok r1,r2]
Per device, in order:
  1. gNMI: probe the port the user stated, else the common defaults (9339 IANA, 6030 Arista, 57400 Nokia/Cisco XR,
     32767 Juniper, 50051 Cisco NX-OS, 9559 Cisco IOS-XE), TLS-without-verify and plaintext; require the
     openconfig-interfaces model in `gnmic capabilities` (else native paths are needed: question).
  2. SNMP: only for devices the user gave SNMP credentials for.
  3. SSH polling with SuzieQ, for the platforms SuzieQ supports (platforms.SUZIEQ).
  4. Otherwise ANO's own SSH exporter (ssh_exporter.py), for any platform ANO can read interface state from
     (NAPALM driver or a parser in platforms.py). Nothing left = a question, never a silent gap.
Output: ../monitoring/gnmic.yaml, ../monitoring/alert.rules.yml, ../suzieq/inventory.yml, out/monitoring_plan.yaml
"""
from __future__ import annotations
import argparse, re, subprocess
from pathlib import Path
from common import OUT, ask, creds, dump_yaml, load_yaml
from platforms import STATE_PARSERS, SUZIEQ, napalm_driver

TOP = Path(__file__).resolve().parent.parent
PORTS = [9339, 6030, 57400, 32767, 50051, 9559]


def gnmi_probe(ip: str, c: dict, ports: list[int]) -> dict | None:
    for port in ports:
        for tls in (["--skip-verify"], ["--insecure"]):
            try:
                r = subprocess.run(["gnmic", "-a", f"{ip}:{port}", *tls, "-u", c["username"], "-p", c["password"],
                                    "--timeout", "4s", "capabilities"], capture_output=True, text=True, timeout=10)
            except Exception:
                continue
            if r.returncode == 0 and "gNMI version" in r.stdout:
                low = r.stdout.lower()
                enc = next((e for e in ("json_ietf", "json", "proto") if re.search(rf"\b{e}\b", low)), None)
                return {"port": port, "tls": tls[0] == "--skip-verify", "encoding": enc,
                        "openconfig_interfaces": "openconfig-interfaces" in r.stdout}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=str(OUT / "model.yaml"))
    ap.add_argument("--gnmi-port", action="append", default=[], help="device=port, if the user knows it")
    ap.add_argument("--snmp-ok", default="")
    a = ap.parse_args()
    model = load_yaml(a.model)
    stated = {k: int(v) for k, v in (x.split("=", 1) for x in a.gnmi_port)}
    snmp_ok = {x for x in a.snmp_ok.split(",") if x}
    plan, targets, questions = {}, {}, []
    for h, d in model["devices"].items():
        c = creds(d.get("cred_profile", 1))
        g = gnmi_probe(d["mgmt_ip"], c, [stated[h]] if h in stated else PORTS)
        if g and g["openconfig_interfaces"]:
            plan[h] = f"gnmi (port {g['port']}, {'TLS' if g['tls'] else 'plaintext'})"
            t = {"address": f"{d['mgmt_ip']}:{g['port']}", "encoding": g["encoding"]}
            t.update({"skip-verify": True} if g["tls"] else {"insecure": True})
            if d.get("cred_profile", 1) != 1:
                t.update({"username": c["username"], "password": c["password"]})
                questions.append(f"{h} uses login profile {d['cred_profile']}: its credentials were written into "
                                 f"gnmic.yaml; protect that file or use a secrets store.")
            targets[h] = t
        elif h in snmp_ok:
            plan[h] = "snmp (add to the Prometheus snmp_exporter job)"
        elif d["platform"] in SUZIEQ:
            plan[h] = "suzieq ssh polling"
        elif napalm_driver(d["platform"]) or d["platform"] in STATE_PARSERS:
            plan[h] = "ano ssh exporter"
        else:
            questions.append(f"{h} ({d['platform']}): no gNMI with OpenConfig, no SNMP allowed, not supported by "
                             f"SuzieQ and no state parser in platforms.py. How should it be monitored?")
        if g and not g["openconfig_interfaces"]:
            questions.append(f"{h} answers gNMI on port {g['port']} without OpenConfig interface models "
                             f"(on SR Linux they must be enabled). Using '{plan.get(h)}' until then.")

    dump_yaml({
        "targets": targets,
        "subscriptions": {
            "ifcounters": {"paths": ["/interfaces/interface/state/counters"], "mode": "stream",
                           "stream-mode": "sample", "sample-interval": "10s"},
            "ifoper": {"paths": ["/interfaces/interface/state/oper-status"], "mode": "stream",
                       "stream-mode": "sample", "sample-interval": "10s"},
        },
        # OpenConfig oper-status is a string ("UP", "DOWN", "LOWER_LAYER_DOWN"...). Only "UP" becomes 1;
        # other strings are not numbers, so they are dropped and the metric expires -> alert below.
        "processors": {"oper-up-to-1": {"event-strings": {"value-names": ["oper-status$"], "transforms": [
            {"replace": {"apply-on": "value", "old": "UP", "new": "1"}}]}}},
        "outputs": {"prom": {"type": "prometheus", "listen": ":9804", "metric-prefix": "ano",
                             "expiration": "60s", "event-processors": ["oper-up-to-1"]}},
    }, TOP / "monitoring" / "gnmic.yaml")

    # Alert only on interfaces that are links in the model (names come from the devices, no vendor pattern)
    link_ifs = sorted({e.split(":", 1)[1] for l in model["links"] for e in (l["a"], l["b"])})
    rx = "|".join(re.escape(i) for i in link_ifs) or "^$"
    cnt = "ano_interfaces_interface_state_counters_in_pkts"
    oper = "ano_interfaces_interface_state_oper_status"
    dump_yaml({"groups": [{"name": "ano", "rules": [
        {"alert": "TelemetryLost", "expr": 'up{job=~"gnmic|ano-ssh"} == 0', "for": "30s",
         "labels": {"severity": "critical"}, "annotations": {"summary": "gNMIc collector is down"}},
        {"alert": "LinkNotUp",   # gNMI drops non-UP samples; the SSH exporter reports 0 explicitly
         "expr": f'(count by (source, interface_name) ({cnt}{{interface_name=~"{rx}"}}) '
                 f'unless on (source, interface_name) ({oper} == 1)) '
                 f'or ({oper}{{job="ano-ssh", interface_name=~"{rx}"}} == 0)',
         "for": "30s", "labels": {"severity": "major"},
         "annotations": {"summary": "{{ $labels.source }} {{ $labels.interface_name }} is not UP"}},
        {"alert": "InterfaceErrors", "expr": "rate(ano_interfaces_interface_state_counters_in_errors[2m]) > 0",
         "for": "1m", "labels": {"severity": "warning"},
         "annotations": {"summary": "{{ $labels.source }} {{ $labels.interface_name }} input errors"}},
    ]}]}, TOP / "monitoring" / "alert.rules.yml")

    sq = {h: d for h, d in model["devices"].items() if plan.get(h) == "suzieq ssh polling"}
    profiles = sorted({d.get("cred_profile", 1) for d in sq.values()})
    dump_yaml({
        "sources": [{"name": f"ano-p{p}", "hosts": [{"url": f"ssh://{d['mgmt_ip']}"} for d in sq.values()
                                                    if d.get("cred_profile", 1) == p]} for p in profiles],
        "devices": [{"name": "ano-devices", "ignore-known-hosts": True}],
        "auths": [{"name": f"ano-auth-p{p}", "username": f"env:ANO_USER{'' if p == 1 else f'_{p}'}",
                   "password": f"env:ANO_PASS{'' if p == 1 else f'_{p}'}"} for p in profiles],
        "namespaces": [{"name": "ano", "source": f"ano-p{p}", "device": "ano-devices", "auth": f"ano-auth-p{p}"}
                       for p in profiles],
    }, TOP / "suzieq" / "inventory.yml")

    dump_yaml(plan, OUT / "monitoring_plan.yaml")
    for h, p in plan.items():
        print(f"{h:14s} -> {p}")
    ssh = sorted(h for h, p in plan.items() if p == "ano ssh exporter")
    if ssh:
        print(f"Start the SSH exporter for {ssh}:  python ssh_exporter.py   (Prometheus job 'ano-ssh', port 9805)")
    print("Wrote monitoring/gnmic.yaml, monitoring/alert.rules.yml, suzieq/inventory.yml")
    unmonitored = [q for q in questions if "How should it be monitored" in q]
    for q in questions:
        if q not in unmonitored:
            print("NOTE:", q)
    ask(unmonitored)       # monitoring is mandatory: a device with no method stops the flow


if __name__ == "__main__":
    main()
