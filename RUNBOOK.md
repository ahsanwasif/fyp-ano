# ANO test lab runbook: Flow A and Flow B, every command

**New here? Start with [GUIDE.md](GUIDE.md):** four worked examples (Flow A small and complex, Flow B small and
complex), each step with the command, what it does and the output you should see. This runbook is the reference.

This lab tests both ANO flows end to end on one Linux machine, without real hardware.
A Containerlab network plays the role of the "real" network. ANO reaches it **only over SSH**
with a username and password, exactly as it would reach physical devices you open in PuTTY.
To run the same flow on real hardware, change only the management range and the credentials.

## Three labs: pick one (or run all)
| | FRR lab (`topologies/frr/`) | cEOS lab (`topologies/eos/`) | Mixed lab (`topologies/mixed/`) |
| --- | --- | --- | --- |
| Image | `quay.io/frrouting/frr:containerlab-10.7.1` (public, **no login**) | Arista cEOS (free arista.com account needed to download) | FRR image + `ano-linux:1` (built by setup, no login) |
| SSH login | admin / admin (set by containerlab) | admin / admin | routers admin/admin, Linux lxadmin/ano-lab (+ sudo) |
| Data collection | 6 vtysh JSON commands over SSH (no NAPALM driver exists for FRR) | 16 NAPALM getters + running config | FRR as left; Linux: `ip -j` + `iptables-save` |
| Batfish | Yes (as Cumulus-concatenated format, done by ANO) | Yes | Yes; Linux as hosts, firewall rules proven chain by chain |
| Packet filters | **No**: FRR only routes, so a "block" intent can only be a destination null route, which ANO asks you to accept | Yes (ACLs) | Yes on the Linux firewall (iptables) |
| Commit with auto-rollback | No: ANO applies without saving, then `--confirm` saves or `--rollback` sends the undo | Yes (NAPALM commit-confirm) | No: applied unsaved, `--confirm` asks how the box persists, `--rollback` sends the undo |
| Monitoring | ANO SSH exporter (no gNMI in FRR, SuzieQ does not list FRR) | gNMI + OpenConfig, SuzieQ | SSH exporter (routers), SuzieQ (Linux) |
| Tested by me before sharing | **Yes, end to end on FRR containers** (see "What was tested") | Batfish steps yes; live cEOS steps no (image needs the account) | **Yes, both flows end to end** (stand-in containers) |

The **mixed lab** (`topologies/mixed/`) adds the other component types: 3 FRR routers, a Linux **firewall**
(iptables) and a Linux **server**, plus a test PC. Linux boxes are discovered over SSH (Netmiko SSHDetect returns
`linux`), read with `ip -j` and `iptables-save`, verified in Batfish (host model), changed with iptables and rolled
back with the stored undo. Its image is built from `setup/images/ano-linux/Dockerfile` (no account needed).

Use the FRR lab to run everything today with no account. Use the cEOS lab to exercise the full chain
(ACL intents, NAPALM getters, commit-confirm, gNMI streaming) on one vendor.

### What was tested before sharing
Run on FRR containers wired exactly like `topologies/frr/*.clab.yml` (containerlab itself could not be installed in
my sandbox, so the same nodes and links were created with plain docker; FRR 8.4 was used there because the
10.7.1 containerlab image could not be pulled, and SSH into vtysh was provided the same way the 10.7.1 image does):
- **Flow A:** discovery identified all 3 FRR routers (Netmiko SSHDetect cannot; ANO's probe did), collected state,
  Batfish verified the change, the change was deployed over SSH, h1 to h2 was blocked, `--rollback` restored it,
  post-check passed against its baseline, `--confirm` saved it. A link taken down showed `oper_status 0` in the exporter.
- **Flow B:** the FRR intake rendered 4 full configs, Batfish passed all 3 tests and 2 failure tests, the reality
  check blocked a wrong OS version, the configs were pushed to 4 blank routers, OSPF + iBGP came up, HQ reached the
  branch, the BGP sinkhole dropped traffic, and pings survived a live link failure.
- **cEOS:** Flow A (a2 to a5) and the Flow B intake with LAG, VLAN/SVI, VRF, eBGP and two filter rules passed every
  Batfish check. Discovery, deploy and gNMI against live cEOS were not run (no image).

- **Mixed lab, Flow A:** discovery found 3 FRR routers (login 1) and 2 Linux boxes (login 2, SSHDetect `linux`).
  Intent "block SSH from 10.1.1.0/24 to the server LAN, HTTPS must keep working": ANO offered the firewall as an
  enforcement point found by Batfish traceroute; Batfish proved the rule (FORWARD and INPUT chains), deploy blocked
  port 22 live while 443 kept answering, `--rollback` reopened it, `--confirm` asked how the firewall persists its rules.
- **Mixed lab, Flow B:** a router, a firewall and a server, all blank. The intake rendered an FRR config and two
  Linux scripts; Batfish passed 3 tests; a policy order mistake (deny before permit) was caught with the exact rule;
  the push made HTTPS and ping work and SSH and any other port blocked, live.

## Folder layout
| Path | What it is |
| --- | --- |
| `setup/00-install.sh` | One-time host setup: Docker, Containerlab, gNMIc, Batfish, Python venv, lab images |
| `topologies/frr/prod.clab.yml` + `configs/frr/prod/` | Flow A "real network", FRR: r1, r2, r3 (OSPF triangle), hosts h1, h2 |
| `topologies/frr/blank.clab.yml` | Flow B "new hardware", FRR: s1 to s4 boot empty, hosts hq-pc, br-pc |
| `topologies/eos/prod.clab.yml` + `configs/eos/prod/` | Flow A "real network", cEOS (same shape) |
| `topologies/eos/blank.clab.yml` | Flow B "new hardware", cEOS |
| `topologies/mixed/prod.clab.yml` + `configs/mixed/prod/` | Flow A, routers + Linux firewall fw1 + Linux server srv1 + PC h1 |
| `topologies/mixed/blank.clab.yml` | Flow B, blank router g1 + firewall gfw + server gsrv + PC |
| `setup/images/ano-linux/` | Dockerfile of the Linux server/firewall image |
| `setup/sandbox-standin/` | The docker scripts the tests were run with where Containerlab was unavailable |
| `GUIDE.md` | Step-by-step walkthrough of four examples |
| `ano/` | The ANO reference scripts, one per pipeline step |
| `ano/inputs/` | What the user supplies: intents, endpoints, Flow B intakes, `answers.yaml` |
| `ano/inputs/answers.yaml` | Your answers to ANO's questions (each question prints its id) |
| `ano/platforms.py` | The ONLY file with vendor-specific facts (drivers, commands, Batfish format, capabilities, kinds) |
| `ano/templates/<platform>/` | Jinja2 templates per platform |
| `ano/ssh_exporter.py` | Monitoring for devices with no gNMI/SNMP/SuzieQ path |
| `monitoring/` | gNMIc, Prometheus, Alertmanager, Grafana (docker compose, versions pinned) |

## 0. One-time setup (Ubuntu 22.04/24.04, 4+ vCPU, 16 GB RAM)
```bash
cd ano-test-lab
bash setup/00-install.sh            # then log out and back in (docker group)
source ~/ano-venv/bin/activate
docker ps --filter name=batfish     # Batfish must be running (ports 9996/9997)
export ANO_IMAGE_FRR=quay.io/frrouting/frr:containerlab-10.7.1
# cEOS lab only: free arista.com account, download cEOS64-lab-<version>.tar.xz, then
#   docker import cEOS64-lab-4.32.0F.tar.xz ceos:4.32.0F && export ANO_IMAGE_ARISTA_CEOS=ceos:4.32.0F
```
Do not use `frrouting/frr:latest` from Docker Hub: it is FRR 8.4 and has no SSH.

## How ANO asks questions
Every step that is missing a fact stops with exit code 2 and prints numbered questions. Questions that can be answered
in a file print an id, for example:
```
Q1. r1:eth1 and r2:eth1 share a subnet (from the configs) but no LLDP/CDP confirms the cable. Answer: direct | via-switch | not-a-link
    answer in answers.yaml as   'link:r1:eth1|r2:eth1': <answer>
```
Add the line to `ano/inputs/answers.yaml` and re-run the step. Other questions name the flag to re-run with
(`--accept`, `--kind`, `--platform`, `--accept-limited`). The shipped `answers.yaml` holds the answers for the FRR
lab, so you can see each question first by renaming it, then put it back.

---------------------------------------------------------------------------------------------------------------

## Flow A: prompt + SSH credentials (fully automated)

### A0. Start the "real" network
```bash
cd topologies/frr && sudo -E containerlab deploy -t prod.clab.yml && cd ../..
ssh admin@172.31.1.11                                  # what you would do in PuTTY (password admin); lands in vtysh
docker exec clab-ano-prod-h1 ping -c 2 10.3.3.10       # BEFORE: h1 reaches h2
```

### A1. The user's input: prompt, management range, SSH login
```bash
export ANO_USER=admin ANO_PASS=admin        # PuTTY username/password (kept in memory only, never written)
# More logins for other device groups: ANO_USER_2/ANO_PASS_2 ... _9; discovery tries each and records which worked
# PuTTY key instead:  puttygen key.ppk -O private-openssh -o ~/.ssh/ano.pem && export ANO_SSH_KEY=~/.ssh/ano.pem
cd ano
```

### A2-A3. Discover devices and collect everything they expose
```bash
python a1_discover.py --range 172.31.1.0/24            # add --port N if SSH is not on 22
# Expect: 172.31.1.11-13 identified as frr "from probe 'show version'" (SSHDetect does not know FRR).
#         Each device lists what it could NOT give (FRR: no counters, ARP, LLDP, NTP... via vtysh) - never filled in.
# cEOS lab: identified as arista_eos by SSHDetect; all 16 NAPALM getters + running config.
# A device neither can identify: re-run with --platform <ip>=<platform> (you state it; ANO still proves the login).
jq '.devices[] | {host:.facts.hostname, platform, platform_source, collector, unsupported_getters}' out/discovery.json
```

### A4. Build the vendor-neutral model
```bash
python a2_model.py
# FRR has no LLDP, so the 3 links come from Batfish (shared subnets) and ANO asks you to confirm each cable.
# The shipped answers.yaml answers them ("direct"). It also checks Batfish read each file in the expected format.
grep -A3 '^links' out/model.yaml
```

### A5. Prompt to structured intent
```bash
# With an LLM:  python a3_intent.py --prompt "Block all traffic from 10.1.1.0/24 to 10.3.3.0/24" --llm ollama/qwen2.5:7b-instruct
python a3_intent.py --intent-json inputs/flowa_intent_frr.json          # no LLM: same validation path
python a3_intent.py --intent-json inputs/flowa_intent_frr.json --accept r1:eth3
# cEOS lab: inputs/flowa_intent.json (block HTTP, ICMP must still work) --accept r1:Ethernet3
```

### A6. Render the change
```bash
python a4_render.py
# FRR: stops and explains FRR cannot filter packets, only null-route the destination. Accept knowingly:
python a4_render.py --accept-limited
cat out/changes/r1.cfg out/undo/r1.cfg        # ip route 10.3.3.0/24 blackhole  /  its undo
```

### A7. Digital twin proof with Batfish (mandatory)
```bash
python a5_batfish.py --base out/batfish/base --candidate out/batfish/candidate --intent out/intent.json
# FRR: FAILS on purpose: step 4 shows collateral damage (traffic from r1 itself to 10.3.3.0/24 is dropped too).
# That is the real cost of a null route. If you accept it:
python a5_batfish.py --base out/batfish/base --candidate out/batfish/candidate --intent out/intent.json --accept-collateral
# cEOS: passes without flags: one changed flow (TCP/80), ICMP still works, 0 loops.
# Any line of the change that Batfish cannot parse is a FAIL (it would not be covered by the proof).
```

### A8. Virtual network: build the twin and rehearse the deploy (mandatory)
```bash
cp inputs/flowa_endpoints_frr.yaml inputs/my_endpoints.yaml   # FRR has no ARP getter, so you state the test hosts
python a6_twin.py --endpoints inputs/my_endpoints.yaml        # kinds and daemons come from the model and configs
cd out/twin && sudo -E containerlab deploy -t twin.clab.yml && cd ../..
python postcheck.py --target twin --baseline        # record what is already true/broken BEFORE the change
python deploy.py --target twin --diff
python deploy.py --target twin --commit
python postcheck.py --target twin                   # only NEW problems fail it
docker exec clab-ano-twin-h1 ping -c 2 -W 1 10.3.3.10 || echo "blocked (expected)"
python a5_batfish.py --candidate out/postcheck_twin --intent out/intent.json --accept-collateral   # re-proof on pulled configs
python deploy.py --target twin --confirm            # FRR: write memory. cEOS: confirms the commit-confirm
# cEOS lab: a6_twin.py ... --kind arista_eos=arista_ceos (ANO asks: cEOS or vEOS)
```

### A9. Approval
Show the user the A6 change, the A7 results and the A8 results. Continue only on an explicit "yes".

### A10-A11. Deploy to the real devices over SSH, verify, confirm (mandatory)
```bash
python postcheck.py --target prod --baseline
python deploy.py --target prod --diff
python deploy.py --target prod --commit           # cEOS: --revert-in 300 auto-rollback. FRR: applied, not saved
python postcheck.py --target prod && python deploy.py --target prod --confirm
python deploy.py --target prod --rollback         # if post-check fails (FRR: sends out/undo/r1.cfg)
docker exec clab-ano-prod-h1 ping -c 2 -W 1 10.3.3.10 || echo "blocked on prod (intent achieved)"
```

### A12. Monitoring (mandatory)
```bash
python a7_monitor.py
# Per device: gNMI with OpenConfig (ports 9339/6030/57400/32767/50051/9559) > SNMP if allowed > SuzieQ if it
# supports the platform > ANO's SSH exporter. FRR lab: all 3 routers -> "ano ssh exporter". No method = question.
python ssh_exporter.py &                          # FRR lab: metrics on :9805/metrics
cd ../monitoring && export LAB_MGMT_NET=ano-prod-mgmt && docker compose up -d
python ../ano/alert_receiver.py &                 # stands in for ANO's alert webhook
# Prometheus :9090  Grafana :3000 (admin/admin)  Alertmanager :9093
sudo ip netns exec clab-ano-prod-r1 ip link set eth1 down   # LinkNotUp fires in ~1 min
sudo ip netns exec clab-ano-prod-r1 ip link set eth1 up
cd ..
```
cEOS lab: a7 picks gNMI (port 6030) and SuzieQ:
```bash
docker run -itd --name suzieq -e ANO_USER -e ANO_PASS --network ano-prod-mgmt \
  -v $PWD/suzieq:/home/suzieq/inv netenglabs/suzieq:0.25.0
docker exec -it suzieq sq-poller -I /home/suzieq/inv/inventory.yml
docker exec -it suzieq suzieq-cli          # suzieq> ospf assert namespace=ano
```

---------------------------------------------------------------------------------------------------------------

## Flow B: prompt only (the user describes the network)

### B0. Start "new hardware"
```bash
cd topologies/frr && sudo -E containerlab deploy -t blank.clab.yml && cd ../../ano
export ANO_USER=admin ANO_PASS=admin
```

### B0b. Read the devices first (recommended whenever SSH access is given)
```bash
python a1_discover.py --range 172.31.3.0/24        # same discovery as Flow A, on the blank devices
python a2_model.py --no-batfish                     # may stop with an "islands" question: no LLDP on FRR, fine here
python b0_prefill.py                                # -> out/intake_draft.yaml: facts filled, design left null
python b1_intake.py --intake out/intake_draft.yaml  # lists every design field still empty as a question
# or let the LLM fill only the empty design fields (device facts are never overwritten):
python b1_intake.py --base out/intake_draft.yaml --prompt "..." --llm ollama/qwen2.5:7b-instruct
```
Without SSH access yet (planning before the hardware arrives) skip B0b and describe everything, as below.

### B1-B2. Intake, question loop, consistency checks
```bash
python b1_intake.py --prompt "Connect HQ 10.10.10.0/24 and branch 10.40.40.0/24 with OSPF" --llm ollama/qwen2.5:7b-instruct
# -> every field the prompt did not state is listed as a question (devices, interfaces, router IDs, tests ...)
python b1_intake.py --intake inputs/flowb_intake_frr.yaml        # the completed answers
# cEOS lab: inputs/flowb_intake.yaml (adds a LAG, VLAN + SVI, a GUEST VRF, eBGP to an ISP, two filter rules).
#   It asks whether 203.0.113.1 is an external BGP peer; answers.yaml says yes.
# The login is a hash only:  openssl passwd -6   (paste into device_login.secret_sha512; no password on disk)
# Try the no-assumption behaviour: delete router_id or set a VLAN that is not defined, and re-run.
```

### B3. Digital twin proof (mandatory), including failure tests
```bash
python a5_batfish.py --candidate out/batfish/candidate --tests inputs/flowb_intake_frr.yaml
# Expect: hq-to-branch, branch-to-hq-tcp, hq-to-sinkhole (deny) PASS; both failure tests PASS; 0 loops
```

### B4. Virtual network twin (mandatory): boots blank, ANO pushes the full configs
```bash
python a6_twin.py --blank --endpoints inputs/flowb_endpoints_frr.yaml
cd out/twin && sudo -E containerlab deploy -t twin.clab.yml && cd ../..
python deploy.py --target twin --changes out/twin/changes --commit
sleep 40                                                       # OSPF + BGP convergence
python postcheck.py --target twin
docker exec clab-ano-twin-hq-pc ping -c 2 10.40.40.10           # works
docker exec clab-ano-twin-hq-pc ping -c 2 -W 1 192.0.2.1 || echo "sinkholed (expected)"
sudo ip netns exec clab-ano-twin-s1 ip link set eth1 down       # live failure test
docker exec clab-ano-twin-hq-pc ping -c 2 10.40.40.10           # still works via s3
sudo ip netns exec clab-ano-twin-s1 ip link set eth1 up
python deploy.py --target twin --changes out/twin/changes --confirm
# cEOS lab: a6_twin.py --blank --kind arista_eos=arista_ceos --endpoints inputs/flowb_endpoints.yaml
```

### B5. Reality check, then deploy over SSH (mandatory)
```bash
python b2_reality_check.py --intake inputs/flowb_intake_frr.yaml
# Compares OS version, model, interfaces, cabling. FRR has no LLDP, so it asks you to confirm each cable
# physically ('cable:s1:eth1|s2:eth1': checked). Any mismatch blocks the deploy.
python deploy.py --target prod --commit
sleep 40 && python postcheck.py --target prod && python deploy.py --target prod --confirm
```

### B6. Monitoring (mandatory)
Same as A12 with `export LAB_MGMT_NET=ano-blank-mgmt`.

## Mixed lab: routers + Linux firewall + Linux server (both flows)
```bash
docker build -t ano-linux:1 setup/images/ano-linux && export ANO_IMAGE_LINUX=ano-linux:1
export ANO_IMAGE_FRR=quay.io/frrouting/frr:containerlab-10.7.1
cd topologies/mixed && sudo -E containerlab deploy -t prod.clab.yml && cd ../../ano
export ANO_USER=admin ANO_PASS=admin                  # login 1: routers
export ANO_USER_2=lxadmin ANO_PASS_2=ano-lab          # login 2: Linux boxes
export ANO_SECRET_2=ano-lab                           # sudo, because iptables needs root
python a1_discover.py --range 172.31.4.0/24
python a2_model.py                                    # Linux gaps are printed (extra routes, stateful rules)
python a3_intent.py --intent-json inputs/flowa_intent_mixed.json            # candidates: r1:eth3, r3:eth1, fw1:eth1
python a3_intent.py --intent-json inputs/flowa_intent_mixed.json --accept fw1:eth1
python a4_render.py
python a5_batfish.py --base out/batfish/base --candidate out/batfish/candidate --intent out/intent.json
python postcheck.py --target prod --baseline
python deploy.py --target prod --commit
docker exec clab-ano-mixed-h1 sh -c 'nc -zw2 10.4.4.10 22; echo ssh=$?; nc -zw2 10.4.4.10 443; echo https=$?'   # ssh=1 https=0
python postcheck.py --target prod
python deploy.py --target prod --rollback             # or --confirm (asks persist:fw1 the first time)
```
Flow B on blank devices: `sudo -E containerlab deploy -t topologies/mixed/blank.clab.yml`, then
```bash
python b1_intake.py --intake inputs/flowb_intake_mixed.yaml
python a5_batfish.py --candidate out/batfish/candidate --tests inputs/flowb_intake_mixed.yaml
python b2_reality_check.py --intake inputs/flowb_intake_mixed.yaml
python deploy.py --target prod --commit && python postcheck.py --target prod
```
What Batfish cannot prove for Linux boxes (ANO prints each one, they are covered by the twin and the post-check):
extra static routes (its host model has one default gateway), stateful rules (`-m state/conntrack`), NAT.
Batfish also does not apply a Linux box's iptables on the path, so ANO proves those rules chain by chain
(`searchFilters` on FORWARD and INPUT at the interface where the traffic enters).

## Scaling to complex topologies
Nothing above depends on the 3- or 4-node shape: the model is a graph, tests and failure tests are lists.
```bash
containerlab generate --name bigfabric --kind arista_ceos --image arista_ceos=$ANO_IMAGE_ARISTA_CEOS --nodes 8,4,2 --file big.clab.yml
sudo -E containerlab deploy -t big.clab.yml
cd ano && python a1_discover.py --range 172.20.20.0/24 && python a2_model.py
```
`containerlab generate` does not address the fabric links, so describe the addressing with a Flow B intake first.

## Adding a vendor (what "not vendor specific" means in practice)
Every vendor fact is in `ano/platforms.py`; the pipeline never branches on a vendor name. For a new platform add:
1. identification: a verified `PROBES` pattern, or use `--platform` ; `NETMIKO_TYPE` if needed
2. collection: a NAPALM driver, or `SHOW` commands + a `STATE_PARSERS` entry
3. `batfish_file` format (or nothing: the device is then reported "not formally verified")
4. `CAPABILITIES` (intents) and `FEATURES` (Flow B), `CLAB_KINDS`, an `ADAPTERS` entry, and `templates/<platform>/`
Nokia SR Linux today: `napalm-srlinux` (JSON-RPC) for collection and commit-confirm, Containerlab kind
`nokia_srlinux`, gNMI after OpenConfig is enabled. Missing: templates and adapter (not shipped because I could not
run SR Linux to test them), Batfish (not supported), SuzieQ (not supported).

## Clean up
```bash
sudo containerlab destroy -a --cleanup
cd monitoring && docker compose down
```
