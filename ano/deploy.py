"""Steps A8/A10 (mandatory): push the change over SSH with Nornir + NAPALM. Same code for twin and real devices.

  python deploy.py --target twin --diff          # show per-device diff only (approval screen)
  python deploy.py --target twin --commit        # commit with commit-confirm (auto-rollback after --revert-in s)
  python deploy.py --target twin --confirm       # post-checks passed -> make it permanent
  python deploy.py --target twin --rollback      # undo now
  (same with --target prod; prod uses the management IPs discovered in Flow A or given in Flow B)

Credentials: ANO_USER / ANO_PASS (/ ANO_SSH_KEY / ANO_SECRET), the same ones used in PuTTY.
"""
from __future__ import annotations
import re
import argparse, sys
from pathlib import Path
from nornir import InitNornir
from nornir.core.task import Result, Task
from common import OUT, creds, dump_yaml, load_yaml
from platforms import CLI_ERROR, CONFIG_BUILDERS, SAVE_ASK, SHOW, adapter, napalm_args, napalm_driver, netmiko_type
from common import answer

BACKUP = OUT / "backup"
UNDO = OUT / "undo"


def build_inventory(target: str, model_path: str) -> tuple[Path, dict]:
    """Writes hosts.yaml WITHOUT secrets; returns {host: credential profile} for in-memory injection."""
    d = OUT / f"nornir_{target}"
    if target == "twin":
        src = load_yaml(OUT / "twin" / "inventory.yaml")
    else:
        m = load_yaml(model_path)
        src = {h: {"hostname": v["mgmt_ip"], "platform": v["platform"], "cred_profile": v.get("cred_profile", 1),
                   "ssh_port": v.get("ssh_port", 22)} for h, v in m["devices"].items()}
    hosts, profiles = {}, {}
    for h, v in src.items():
        drv = napalm_driver(v["platform"])
        extras, q = napalm_args(drv) if drv else ({}, None)
        if q:
            sys.exit(f"{h}: {q}")
        hosts[h] = {"hostname": v["hostname"], "platform": v["platform"], "port": v.get("ssh_port", 22),
                    "data": {"napalm_driver": drv},
                    "connection_options": {
                        "napalm": {"platform": drv, "extras": {"optional_args": extras}},
                        "netmiko": {"platform": netmiko_type(v["platform"])}}}
        profiles[h] = v.get("cred_profile", 1)
    dump_yaml(hosts, d / "hosts.yaml")
    return d, profiles


def init_nornir(target: str, model_path: str):
    """Nornir with each device's login injected in memory only (never written to the inventory files)."""
    inv, profiles = build_inventory(target, model_path)
    nr = InitNornir(runner={"plugin": "threaded", "options": {"num_workers": 20}},
                    inventory={"plugin": "SimpleInventory", "options": {"host_file": str(inv / "hosts.yaml")}},
                    logging={"enabled": False})
    for h, host in nr.inventory.hosts.items():
        c = creds(profiles[h])
        host.username, host.password = c["username"], c["password"]
        for conn in ("napalm", "netmiko"):
            opts = host.connection_options[conn]
            extra = dict(opts.extras or {})
            if conn == "napalm":
                oa = dict(extra.get("optional_args", {}))
                oa.update({k: c[k] for k in ("secret", "key_file") if k in c})
                extra["optional_args"] = oa
            else:
                extra.update({k: c[k] for k in ("secret", "key_file") if k in c})
                if "key_file" in c:
                    extra["use_keys"] = True
            opts.extras = extra
    return nr


def ordered(changes: dict[str, str], model: dict | None) -> list[str]:
    """Devices whose change touches their own management interface go LAST (protects ANO's SSH path).
    Unknown (no adapter / no model entry) is treated as risky, never as safe."""
    def risky(h):
        dev = (model or {}).get("devices", {}).get(h)
        ad = adapter(dev["platform"]) if dev else None
        if not dev or not ad or not dev.get("mgmt_interface"):
            return True
        return ad["touches"](changes[h], dev["mgmt_interface"])
    return sorted(changes, key=lambda h: (risky(h), h))


def do(task: Task, action: str, change: str, revert_in: int) -> Result:
    h = task.host
    if h.data["napalm_driver"]:
        dev = h.get_connection("napalm", task.nornir.config)
        if action == "diff":
            dev.load_merge_candidate(config=change)
            diff = dev.compare_config()
            dev.discard_config()
            return Result(host=h, result=diff or "(no change)")
        if action == "commit":
            BACKUP.mkdir(exist_ok=True)
            (BACKUP / f"{h.name}.cfg").write_text(dev.get_config(retrieve="running")["running"])
            dev.load_merge_candidate(config=change)
            diff = dev.compare_config()
            try:
                dev.commit_config(revert_in=revert_in)
                return Result(host=h, result=f"committed with auto-rollback in {revert_in}s\n{diff}")
            except NotImplementedError:
                dev.commit_config()
                return Result(host=h, result=f"committed (platform has no commit-confirm; backup kept)\n{diff}")
        if action in ("confirm", "rollback") and hasattr(dev, "_get_pending_commits") \
                and getattr(dev, "config_session", None) is None:
            # EOS: confirm/rollback runs in a NEW SSH session, so re-attach to the pending config session by name
            pending = dev._get_pending_commits()
            if pending:
                dev.config_session = next(iter(pending))
        if action == "confirm":
            if dev.has_pending_commit():
                dev.confirm_commit()
                return Result(host=h, result="confirmed")
            return Result(host=h, result="nothing pending (already confirmed or rolled back)")
        if action == "rollback":
            if dev.has_pending_commit():
                dev.rollback()  # discards the pending commit-confirm
                return Result(host=h, result="pending commit rolled back")
            backup = BACKUP / f"{h.name}.cfg"
            dev.load_replace_candidate(config=backup.read_text())
            dev.commit_config()
            return Result(host=h, result=f"restored {backup}")
    # Netmiko-only platforms (e.g. FRR): no on-device commit-confirm. Backup first; the change is applied but NOT
    # saved, so a reboot also reverts it; --rollback sends the rendered undo (out/undo/<device>.cfg).
    conn = h.get_connection("netmiko", task.nornir.config)
    if getattr(conn, "secret", None) and not conn.check_enable_mode():
        conn.enable()          # e.g. Linux: sudo for iptables (ANO_SECRET<n>)
    plat = h.data.get("ano_platform", h.platform)
    err = re.compile(CLI_ERROR.get(plat, r"^\s*%"), re.M)

    def send(lines):
        if plat in CONFIG_BUILDERS:           # shell platforms: plain commands, one by one (no config mode)
            return "\n".join(f"$ {l}\n" + conn.send_command(l, read_timeout=60) for l in lines if l.strip())
        return conn.send_config_set(lines)

    def running():
        if plat in CONFIG_BUILDERS:
            return CONFIG_BUILDERS[plat]({k: conn.send_command(c, read_timeout=60) for k, c in SHOW[plat].items()})
        return conn.send_command(SHOW.get(plat, {}).get("config", "show running-config"))
    if action == "diff":
        return Result(host=h, result=f"(no NAPALM diff for {h.platform}; lines to send:)\n{change}")
    if action == "commit":
        BACKUP.mkdir(exist_ok=True)
        (BACKUP / f"{h.name}.cfg").write_text(running())
        out = send(change.splitlines())
        bad = [l for l in out.splitlines() if err.search(l) and not l.startswith("$ ")]   # CLI error lines
        return Result(host=h, result=out + ("\nCLI ERRORS: " + "; ".join(bad) if bad else
                      "\napplied, not saved (no commit-confirm on this platform: run postcheck, then --confirm "
                      "or --rollback)"), failed=bool(bad))
    if action == "confirm":
        if plat in SAVE_ASK:
            how = answer(f"persist:{h.name}")
            if not how:
                return Result(host=h, failed=True, result=f"QUESTION persist:{h.name}: {SAVE_ASK[plat]}")
            return Result(host=h, result="kept until reboot only (as answered)" if how == "none" else send([how]))
        return Result(host=h, result=conn.save_config())
    undo = UNDO / f"{h.name}.cfg"
    if undo.exists():
        out = send(undo.read_text().splitlines())
        return Result(host=h, result=f"undo applied from {undo}\n{out}")
    return Result(host=h, result=f"no undo template for {h.platform}: restore by hand from "
                                 f"{BACKUP / (h.name + '.cfg')}", failed=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["twin", "prod"], required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    for f in ("diff", "commit", "confirm", "rollback"):
        g.add_argument(f"--{f}", action="store_true")
    ap.add_argument("--changes", default=str(OUT / "changes"))
    ap.add_argument("--model", default=str(OUT / "model.yaml"))
    ap.add_argument("--revert-in", type=int, default=300)
    a = ap.parse_args()
    action = next(x for x in ("diff", "commit", "confirm", "rollback") if getattr(a, x))

    nr = init_nornir(a.target, a.model)
    changes = {p.stem: p.read_text() for p in Path(a.changes).glob("*.cfg")}
    missing = [h for h in changes if h not in nr.inventory.hosts]
    if missing:
        sys.exit(f"Changes for unknown devices {missing}. Stopping (no assumptions).")
    model = load_yaml(a.model) if Path(a.model).exists() else None
    failed = False
    for h in ordered(changes, model):   # serial in risk order; inside a device NAPALM is atomic
        r = nr.filter(name=h).run(task=do, action=action, change=changes[h], revert_in=a.revert_in)
        res = r[h][0]
        print(f"===== {h} [{action}] {'FAILED' if res.failed else 'ok'}\n{res.result}")
        failed |= res.failed
        if res.failed and action == "commit":
            print("Stopping rollout: remaining devices untouched; failed device auto-reverts on timer.")
            break
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
