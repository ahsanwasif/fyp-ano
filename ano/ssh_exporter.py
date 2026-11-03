"""Step A12 fallback: ANO's own SSH poller for devices with no gNMI/OpenConfig, no SNMP and no SuzieQ support
(e.g. FRR, Nokia SR Linux). Exposes the same metric name the gNMI path uses, so the same alert rules apply.

  python ssh_exporter.py [--interval 30] [--port 9805]
Metrics: ano_interfaces_interface_state_oper_status{source,interface_name} 1|0, ano_ssh_poll_ok{source} 1|0
"""
from __future__ import annotations
import argparse, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from nornir.core.task import Result, Task
from common import OUT, load_yaml
from deploy import init_nornir
from platforms import SHOW, STATE_PARSERS

STATE = {"text": ""}


def poll(task: Task, model: dict) -> Result:
    plat = model["devices"][task.host.name]["platform"]
    if task.host.data.get("napalm_driver"):
        ifs = task.host.get_connection("napalm", task.nornir.config).get_interfaces()
    else:
        conn = task.host.get_connection("netmiko", task.nornir.config)
        if getattr(conn, "secret", None) and not conn.check_enable_mode():
            conn.enable()          # e.g. Linux: sudo for iptables (ANO_SECRET<n>)
        raw = {"interfaces": conn.send_command(SHOW[plat]["interfaces"], read_timeout=30)}
        ifs = STATE_PARSERS[plat](raw)["get_interfaces"]
    return Result(host=task.host, result={n: int(bool(v.get("is_up"))) for n, v in ifs.items()})


def loop(nr, model, interval):
    while True:
        lines = ["# TYPE ano_interfaces_interface_state_oper_status gauge", "# TYPE ano_ssh_poll_ok gauge"]
        for h, mr in nr.run(task=poll, model=model).items():
            lines.append(f'ano_ssh_poll_ok{{source="{h}"}} {0 if mr.failed else 1}')
            if not mr.failed:
                lines += [f'ano_interfaces_interface_state_oper_status{{source="{h}",interface_name="{i}"}} {v}'
                          for i, v in sorted(mr[0].result.items())]
            for host in nr.inventory.hosts.values():   # drop broken sessions so the next poll reconnects
                host.close_connections()
        STATE["text"] = "\n".join(lines) + "\n"
        time.sleep(interval)


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = STATE["text"].encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--port", type=int, default=9805)
    ap.add_argument("--target", choices=["twin", "prod"], default="prod")
    a = ap.parse_args()
    model = load_yaml(OUT / "model.yaml")
    plan = load_yaml(OUT / "monitoring_plan.yaml")
    nr = init_nornir(a.target, str(OUT / "model.yaml"))
    nr = nr.filter(filter_func=lambda h: plan.get(h.name) == "ano ssh exporter")
    threading.Thread(target=loop, args=(nr, model, a.interval), daemon=True).start()
    print(f"Polling {sorted(nr.inventory.hosts)} every {a.interval}s; metrics on :{a.port}/metrics")
    HTTPServer(("0.0.0.0", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
