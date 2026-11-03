"""Step A5 / B1: prompt -> validated vendor-neutral Intent, then propose where to enforce it.

Usage (LLM):    python a3_intent.py --prompt "Block HTTP from 10.1.1.0/24 to 10.3.3.0/24, ping must keep working" \
                                    --llm ollama/qwen2.5:7b-instruct
Usage (no LLM): python a3_intent.py --intent-json inputs/flowa_intent.json      (same validation path)
Then confirm:   python a3_intent.py ... --accept r1:Ethernet3
Output: out/intent.json
"""
from __future__ import annotations
import argparse, ipaddress, json
from common import OUT, answer, ask, dump_json, load_yaml
from intent_schema import Intent

SYSTEM = (
    "You convert a network engineer's request into the Intent schema. "
    "Use ONLY values stated in the request or present in the provided network facts. "
    "If anything needed is not stated (ports, protocols, which traffic must keep working), "
    "put a question in 'unclear' instead of choosing a value."
)


def llm_intent(prompt: str, model_name: str, facts: str) -> Intent:
    import instructor, litellm
    client = instructor.from_litellm(litellm.completion)
    return client.chat.completions.create(
        model=model_name, response_model=Intent, max_retries=3,
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": f"Network facts:\n{facts}\n\nRequest:\n{prompt}"}])


def enforcement_candidates(model: dict, src: ipaddress.IPv4Network) -> list[str]:
    """Ingress interfaces whose connected subnet overlaps the source: closest point to the source."""
    out = []
    for dev, d in model["devices"].items():
        for ifn, i in d["interfaces"].items():
            if ifn == d.get("mgmt_interface"):      # identified by its IP, not by a vendor's naming
                continue
            for cidr in i["ipv4"]:
                net = ipaddress.ip_interface(cidr).network
                if net.overlaps(src):
                    out.append(f"{dev}:{ifn}")
    return out


def path_candidates(model: dict, intent: Intent, starts: list[str], host: str) -> list[str]:
    """Every interface where the traffic ENTERS a device on its way (Batfish traceroute on the base snapshot),
    so a firewall in the middle of the path can be chosen, not only the device next to the source."""
    try:
        from pybatfish.client.session import Session
        from pybatfish.datamodel.flow import HeaderConstraints
        bf = Session(host=host); bf.set_network("ano")
        bf.init_snapshot(str(OUT / "batfish" / "base"), name="base", overwrite=True)
    except Exception as e:
        print(f"(Batfish not reachable, only source-facing interfaces are offered: {str(e)[:80]})")
        return []
    m = intent.match
    hc = HeaderConstraints(srcIps=str(m.src), dstIps=str(next(m.dst.hosts(), m.dst.network_address)),
                           ipProtocols=None if m.protocol == "ip" else [m.protocol.upper()],
                           dstPorts=str(m.dst_port) if m.dst_port else None)
    out = []
    for s in starts:
        dev, ifn = s.split(":", 1)
        fr = bf.q.traceroute(startLocation=f"@enter({dev}[{ifn}])", headers=hc).answer(snapshot="base").frame()
        for traces in fr.get("Traces", []):
            for tr in traces:
                for hop in tr.hops:
                    for st in hop.steps:
                        iif = getattr(st.detail, "inputInterface", None)
                        if iif and f"{hop.node}:{iif}" not in out:
                            out.append(f"{hop.node}:{iif}")
    by_lower = {d.lower(): d for d in model["devices"]}
    return [f"{by_lower.get(c.split(':')[0], c.split(':')[0])}:{c.split(':', 1)[1]}" for c in out]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt")
    ap.add_argument("--llm", help="LiteLLM model id, e.g. ollama/qwen2.5:7b-instruct or anthropic/<model>")
    ap.add_argument("--intent-json")
    ap.add_argument("--model", default=str(OUT / "model.yaml"))
    ap.add_argument("--accept", help="device:interface the user approves as the enforcement point")
    ap.add_argument("--batfish-host", default="localhost")
    a = ap.parse_args()
    model = load_yaml(a.model)

    if a.intent_json:
        intent = Intent.model_validate_json(open(a.intent_json).read())
    else:
        facts = json.dumps({d: list(v["interfaces"].items()) for d, v in model["devices"].items()})[:12000]
        intent = llm_intent(a.prompt, a.llm, facts)
    print("Intent:", intent.model_dump_json(indent=2))

    questions = list(intent.unclear)
    # every address in the intent must exist in the model (no invented subnets)
    known = [ipaddress.ip_interface(c).network for d in model["devices"].values()
             for i in d["interfaces"].values() for c in i["ipv4"]]
    for t in [intent.match, *intent.must_still_work]:
        for side in (t.src, t.dst):
            if not any(side.overlaps(k) for k in known):
                questions.append(f"{side} is not a subnet in the network. Is it correct?")
    qid = f"must_still_work:{intent.name}"
    if intent.kind == "block_traffic" and not intent.must_still_work and answer(qid) != "nothing else":
        questions.append((qid, "You asked to block traffic. Which traffic between these subnets must keep "
                               "working (add it to must_still_work), or answer 'nothing else'."))
    cands = enforcement_candidates(model, intent.match.src)
    if cands:
        cands += [c for c in path_candidates(model, intent, cands, a.batfish_host) if c not in cands]
    if not cands:
        questions.append(f"No interface in the network faces {intent.match.src}. Where does that traffic enter?")
    elif not a.accept:
        questions.append(f"Enforce on which ingress interface? Candidates in path order, closest to the source first: {cands}. "
                         f"Re-run with --accept <device:interface>.")
    elif a.accept not in cands:
        questions.append(f"{a.accept} is not a candidate. Choose one of {cands}.")
    ask(questions)

    dev, ifn = a.accept.split(":", 1)
    dump_json({"intent": json.loads(intent.model_dump_json()),
               "enforce": {"device": dev, "interface": ifn, "direction": "in"},
               "approved_by_user": True}, OUT / "intent.json")
    print(f"Saved {OUT/'intent.json'}")


if __name__ == "__main__":
    main()
