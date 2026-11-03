"""Step A6: render the vendor-neutral intent into per-platform CLI with Jinja2.

Usage:  python a4_render.py [--accept-limited]
Input:  out/intent.json, out/model.yaml, out/configs (raw running configs)
Output: out/changes/<device>.cfg, out/undo/<device>.cfg (if the platform has an undo template),
        out/batfish/candidate/configs (base + change, in the format Batfish needs)
The platform must be able to enforce the intent (platforms.CAPABILITIES). A limited method (e.g. FRR can only
null-route a destination) is used only after the user accepts it with --accept-limited.
"""
from __future__ import annotations
import argparse, json, shutil
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from common import OUT, ROOT, ask, load_yaml
from platforms import CAPABILITIES, adapter, write_batfish


def existing_filter(host: str, dev: str, ifn: str, direction: str) -> str | None:
    """Vendor-neutral: ask Batfish which ACL is already bound to the interface."""
    from pybatfish.client.session import Session
    bf = Session(host=host)
    bf.set_network("ano")
    bf.init_snapshot(str(OUT / "batfish" / "base"), name="base", overwrite=True)
    col = "Incoming_Filter_Name" if direction == "in" else "Outgoing_Filter_Name"
    df = bf.q.interfaceProperties(nodes=f"/^{dev}$/", interfaces=f"/^{ifn}$/",
                                  properties=col).answer(snapshot="base").frame()
    return None if df.empty else df.iloc[0][col]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batfish-host", default="localhost")
    ap.add_argument("--accept-limited", action="store_true", help="user accepts the platform's limited method")
    a = ap.parse_args()
    data = json.loads((OUT / "intent.json").read_text())
    intent, enforce = data["intent"], data["enforce"]
    model = load_yaml(OUT / "model.yaml")
    dev = enforce["device"]
    platform = model["devices"][dev]["platform"]
    base_cfg = (OUT / "configs" / f"{dev}.cfg").read_text()

    questions = []
    caps = CAPABILITIES.get(platform, {})
    if intent["kind"] not in caps:
        ask([f"{dev} ({platform}) has no known way to do '{intent['kind']}'. Choose another enforcement device "
             f"(a3_intent.py --accept), or add the capability and template for {platform}."])
    if caps[intent["kind"]] and not a.accept_limited:
        questions.append(f"{dev}: {caps[intent['kind']]} Re-run with --accept-limited to use it (Batfish step 4 "
                         f"will list every flow it changes), or choose another enforcement device.")
    tpl_dir = ROOT / "templates" / platform
    if not (tpl_dir / f"{intent['kind']}.j2").exists():
        questions.append(f"No template for platform '{platform}' and intent '{intent['kind']}'. "
                         f"Add templates/{platform}/{intent['kind']}.j2, or approve the LLM-drafted "
                         f"(unverified) path for {dev} separately.")
    ad = adapter(platform)
    if not ad:
        questions.append(f"No CLI adapter for platform '{platform}' in platforms.py (needed to build the candidate).")
    existing = existing_filter(a.batfish_host, dev, enforce["interface"], enforce["direction"]) \
        if model["devices"][dev].get("batfish") else None
    if existing:
        questions.append(f"{dev} {enforce['interface']} already has filter {existing} ({enforce['direction']}). "
                         f"Replacing it would drop its rules. Merge the new rule into {existing}, or replace it?")
    ask(questions)

    env = Environment(loader=FileSystemLoader(tpl_dir), undefined=StrictUndefined,
                      keep_trailing_newline=True)
    change = env.get_template(f"{intent['kind']}.j2").render(intent=intent, enforce=enforce)
    for d in ("changes", "undo"):
        (OUT / d).mkdir(exist_ok=True)
        for old in (OUT / d).glob("*.cfg"):
            old.unlink()
    (OUT / "changes" / f"{dev}.cfg").write_text(change)
    print(f"--- change for {dev} ({platform}) ---\n{change}")
    if (tpl_dir / f"{intent['kind']}.undo.j2").exists():   # used to roll back platforms without commit-confirm
        undo = env.get_template(f"{intent['kind']}.undo.j2").render(intent=intent, enforce=enforce)
        (OUT / "undo" / f"{dev}.cfg").write_text(undo)

    cand = OUT / "batfish" / "candidate"
    shutil.rmtree(cand, ignore_errors=True)
    shutil.copytree(OUT / "batfish" / "base", cand)
    gaps = write_batfish(cand, platform, dev, ad["merge"](base_cfg, change))
    if gaps:
        print(f"{dev}: parts Batfish does not model (twin test only): {gaps}")
    if gaps is None:
        print(f"{dev}: Batfish does not support {platform}; this change is NOT formally verified (twin test only).")
    print(f"Candidate snapshot -> {cand}")


if __name__ == "__main__":
    main()
