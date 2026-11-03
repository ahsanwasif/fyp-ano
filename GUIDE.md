# ANO hands-on guide: run both flows yourself

Four worked examples, each step with the exact command, what it does, and what you should see.

| # | Flow | Example | Devices | What you will do |
|---|---|---|---|---|
| 1 | A | **Small** | 3 routers, 2 PCs | Block one LAN from reaching another |
| 2 | A | **Complex** | 3 routers, a Linux firewall, a Linux server, a PC | Block SSH to the servers on the firewall while HTTPS keeps working |
| 3 | B | **Small** | a router, a firewall and a server, all blank | Read the boxes over SSH, describe the design, build it from zero |
| 4 | B | **Complex** | 4 blank routers, 2 PCs | OSPF, iBGP over loopbacks, a BGP sinkhole, and survive link failures |

Flow A = you give a prompt and the SSH login; ANO reads the network itself.
Flow B = you describe the network (ANO reads the facts it can over SSH first); ANO builds it.

The "what you should see" blocks are copied from real runs of these exact examples (see "What was tested" at the end).

---------------------------------------------------------------------------------------------------------------

## Part 0. Set up the machine once

You need Ubuntu 22.04 or 24.04, 4+ vCPU, 16 GB RAM, internet access and sudo.

```bash
unzip ano-test-lab.zip && cd ano-test-lab
bash setup/00-install.sh        # Docker, Containerlab, gNMIc, Batfish, Python venv, lab images (10-15 min)
# log out and log back in (so your user is in the docker group), then:
cd ano-test-lab
source ~/ano-venv/bin/activate
source ~/.bashrc                # loads ANO_IMAGE_FRR and ANO_IMAGE_LINUX
```

Check that everything is there before you start:

```bash
docker ps --filter name=batfish --format '{{.Names}} {{.Status}}'    # batfish Up ...
containerlab version | head -3
echo $ANO_IMAGE_FRR $ANO_IMAGE_LINUX    # quay.io/frrouting/frr:containerlab-10.7.1 ano-linux:1
python -c "import netmiko, napalm, nornir, pybatfish; print('python ok')"
```

### How ANO talks to you

* **Questions instead of guesses.** When a fact is missing, the step stops with exit code 2 and prints numbered
  questions. Some say which flag to re-run with (`--accept ...`, `--accept-limited`). Others print an id:
  ```
  Q1. ... Answer: direct | via-switch | not-a-link
      answer in answers.yaml as   'link:r1:eth1|r2:eth1': <answer>
  ```
  Put `link:r1:eth1|r2:eth1: direct` in `ano/inputs/answers.yaml` and run the same command again.
  The shipped `answers.yaml` already holds the answers for these examples, except the ones this guide asks
  you to add yourself so you see the question first.
* **Exit codes.** 0 = done, 2 = questions for you, 1 = a check failed (read the FAIL lines).
* **Credentials** are only ever read from environment variables (`ANO_USER`, `ANO_PASS`, `ANO_USER_2` ...).
  Nothing writes them to disk.
* **Everything ANO produces** goes to `ano/out/`:

| File | Made by | What it is |
|---|---|---|
| `out/discovery.json` | a1 | everything read from each device |
| `out/configs/<dev>.cfg` | a1 | each device's running config as read |
| `out/model.yaml` | a2 / b1 | the vendor-neutral model: devices, interfaces, links |
| `out/intent.json` | a3 | the validated intent and where to enforce it |
| `out/changes/<dev>.cfg` | a4 / b1 | exactly what will be sent to each device |
| `out/undo/<dev>.cfg` | a4 | how to take the change back |
| `out/batfish/base`, `out/batfish/candidate` | a2, a4 / b1 | the network before and after, for Batfish |
| `out/twin/` | a6 | the virtual copy (Containerlab topology + configs) |
| `out/backup/<dev>.cfg` | deploy | the running config saved just before a commit |

### Between examples (start clean)

```bash
sudo containerlab destroy -a --cleanup            # removes every lab and twin
cd ~/ano-test-lab && rm -rf ano/out/*             # forgets the previous example (adjust the path to where you unzipped)
```

---------------------------------------------------------------------------------------------------------------

## Example 1. Flow A, small: block one LAN from another (3 routers)

```
   h1 10.1.1.10                                   h2 10.3.3.10
      |                                              |
   eth3 (10.1.1.1)                              eth3 (10.3.3.1)
     r1 ---------------- r2 ---------------- r3        OSPF area 0 between all three
      \__________________________________________/     (r1-r3 direct link too)
```
**Prompt:** "Block all traffic from 10.1.1.0/24 to 10.3.3.0/24."
**What ANO is given:** the management range 172.31.1.0/24 and the SSH login admin/admin. Nothing else.

### A0. Start the "real" network
```bash
cd topologies/frr && sudo -E containerlab deploy -t prod.clab.yml && cd ../..
docker exec clab-ano-prod-h1 ping -c 2 10.3.3.10       # BEFORE the change: h1 reaches h2
ssh admin@172.31.1.11                                  # optional: what you would do in PuTTY (password admin)
```
You should see `2 packets received`. The SSH login lands in the router CLI (vtysh); type `exit`.

### A1. Give ANO the login
```bash
cd ano
export ANO_USER=admin ANO_PASS=admin
```
A PuTTY key instead of a password: `puttygen key.ppk -O private-openssh -o ~/.ssh/ano.pem && export ANO_SSH_KEY=~/.ssh/ano.pem`.

### A2-A3. Discover the devices and read everything they expose
```bash
python a1_discover.py --range 172.31.1.0/24            # add --port N if SSH is not on port 22
```
What it does: scans the range for SSH, logs in, works out the platform (Netmiko SSHDetect first, then ANO's own
`show version` probe), then reads the device with the right method.
You should see:
```
[A2] 3 address(es) answer on TCP/22
[A2] 172.31.1.11: frr (from probe 'show version', login profile 1)
...
[A3] 172.31.1.11 -> r1 (netmiko:cisco_ios (6 commands)); not supported by driver: ['get_interfaces_counters', ...]
Wrote ./ano/out/discovery.json
```
"not supported" lists what this platform cannot give. ANO never invents those values.
Look at what was collected: `jq '.devices[] | {host:.facts.hostname, platform, collector}' out/discovery.json`

### A4. Build the model
```bash
python a2_model.py
```
What it does: turns the three devices into one vendor-neutral model, and loads their configs into Batfish.
FRR has no LLDP, so ANO finds the cables from shared subnets and asks you to confirm each one. The shipped
`answers.yaml` already answers them (`direct`). You should see:
```
Model: 3 devices, 3 links -> ./ano/out/model.yaml
```
To see the questions yourself: comment out the three `link:r...` lines in `inputs/answers.yaml` and run it again.

### A5. Turn the prompt into an intent
```bash
python a3_intent.py --intent-json inputs/flowa_intent_frr.json
# with an LLM instead of the JSON file:
#   python a3_intent.py --prompt "Block all traffic from 10.1.1.0/24 to 10.3.3.0/24" --llm ollama/qwen2.5:7b-instruct
```
You should see the intent, then a question:
```
Q1. Enforce on which ingress interface? Candidates in path order, closest to the source first: ['r1:eth3', 'r3:eth1'].
```
Choose the interface closest to the source and confirm:
```bash
python a3_intent.py --intent-json inputs/flowa_intent_frr.json --accept r1:eth3
```
You should see `Saved ./ano/out/intent.json`.

### A6. Render the change
```bash
python a4_render.py
```
ANO stops on purpose: FRR is a router and cannot filter packets. The only way to block is a null route for the
destination, which drops **all** traffic to 10.3.3.0/24 entering r1, whatever its source. If you accept that:
```bash
python a4_render.py --accept-limited
cat out/changes/r1.cfg out/undo/r1.cfg
```
You should see `ip route 10.3.3.0/24 blackhole` and its undo `no ip route 10.3.3.0/24 blackhole`.

### A7. Prove it with Batfish (mandatory)
```bash
python a5_batfish.py --base out/batfish/base --candidate out/batfish/candidate --intent out/intent.json
```
You should see the intent PASS, then a FAIL in step 4:
```
3. Intent and must-still-work checks on the candidate
  [PASS] intent: block-lan1-to-lan3: expect deny (dst 10.3.3.0/24)
4. Collateral damage: every flow whose outcome changed
  [FAIL] collateral: start=r1 [10.0.12.0->10.3.3.0 ICMP (type=8, code=0)]
  [OK] changed (intended): start=r1 interface=eth3 [10.1.1.2->10.3.3.0 ICMP (type=8, code=0)]
RESULT: FAILED
```
This is Batfish finding the real side effect of a null route: r1 itself can no longer reach 10.3.3.0/24 either.
The `ip ospf passive` WARN lines are FRR syntax Batfish does not read; they are in the existing configs, not in
the change, so they do not fail the run. If you accept the side effect:
```bash
python a5_batfish.py --base out/batfish/base --candidate out/batfish/candidate --intent out/intent.json --accept-collateral
```
You should see `RESULT: ALL CHECKS PASSED`.

### A8. Build the virtual copy (twin) and rehearse (mandatory)
```bash
python a6_twin.py --endpoints inputs/flowa_endpoints_frr.yaml
cd out/twin && sudo -E containerlab deploy -t twin.clab.yml && cd ../..
python postcheck.py --target twin --baseline            # record what is true BEFORE the change
python deploy.py --target twin --commit
python postcheck.py --target twin                       # only NEW problems fail it
docker exec clab-ano-twin-h1 ping -c 2 -W 1 10.3.3.10 || echo "blocked in the twin (expected)"
python deploy.py --target twin --confirm
```
`a6_twin.py` prints `Twin: 5 nodes, 5 links`. If it asks for an image, set `ANO_IMAGE_FRR` (Part 0).

### A9. Approval
Look at `out/changes/r1.cfg`, the A7 result and the A8 result. Continue only if you agree.

### A10-A11. Deploy to the real devices, check, then confirm or roll back (mandatory)
```bash
python postcheck.py --target prod --baseline
python deploy.py --target prod --diff                   # shows exactly the lines that will be sent
python deploy.py --target prod --commit
docker exec clab-ano-prod-h1 ping -c 2 -W 1 10.3.3.10   # now: 100% packet loss
python postcheck.py --target prod                       # POSTCHECK PASSED (no new problems)
```
Practise the rollback once:
```bash
python deploy.py --target prod --rollback               # sends out/undo/r1.cfg
docker exec clab-ano-prod-h1 ping -c 2 -W 1 10.3.3.10   # works again
python deploy.py --target prod --commit && python deploy.py --target prod --confirm   # apply again and save
```
FRR has no on-device commit timer, so ANO applies the change unsaved. `--confirm` saves it (write memory).
`--rollback` sends the undo.

### A12. Monitoring (mandatory)
```bash
python a7_monitor.py                 # r1, r2, r3 -> ano ssh exporter (FRR has no gNMI; SuzieQ does not list FRR)
python ssh_exporter.py &             # metrics on :9805/metrics
cd ../monitoring && export LAB_MGMT_NET=ano-prod-mgmt && docker compose up -d && cd ../ano
python alert_receiver.py &           # stands in for ANO's alert webhook
sudo ip netns exec clab-ano-prod-r1 ip link set eth1 down   # the LinkNotUp alert fires in about 1 minute
sudo ip netns exec clab-ano-prod-r1 ip link set eth1 up
```
Prometheus is on :9090, Grafana on :3000 (admin/admin) and Alertmanager on :9093.

---------------------------------------------------------------------------------------------------------------

## Example 2. Flow A, complex: routers + firewall + server

```
 h1 10.1.1.10 -- r1 -- r2 -- r3 --(10.3.3.0/24)-- fw1 --(10.4.4.0/24)-- srv1 10.4.4.10
                 \___________/                   Linux        Linux, SSH on 22, a test service on 443
                OSPF, r3 has a static route to 10.4.4.0/24 via fw1 and shares it in OSPF
```
**Prompt:** "Block SSH from 10.1.1.0/24 to the server LAN 10.4.4.0/24. HTTPS to the servers must keep working."
**What ANO is given:** the range 172.31.4.0/24 and **two logins**: routers admin/admin, Linux boxes
lxadmin/ano-lab. The Linux login is not root, so ANO also gets the sudo password.

What is new compared with Example 1:
- two device types with two logins
- a firewall in the middle of the path
- a real port-level rule instead of a null route
- Batfish gaps that ANO reports
- a question about how the firewall saves its rules

### A0. Start the network
```bash
cd topologies/mixed && sudo -E containerlab deploy -t prod.clab.yml && cd ../../ano
docker exec clab-ano-mixed-h1 sh -c 'echo | nc -w2 10.4.4.10 22 | head -c 20; echo; echo | nc -w2 10.4.4.10 443'
```
BEFORE the change you should see `SSH-2.0-OpenSSH...` (port 22 open) and `tls-ok` (port 443 open).

### A1. Give ANO both logins
```bash
export ANO_USER=admin ANO_PASS=admin                 # login 1: routers
export ANO_USER_2=lxadmin ANO_PASS_2=ano-lab         # login 2: Linux servers and firewalls
export ANO_SECRET_2=ano-lab                          # sudo password: reading/changing iptables needs root
```
Discovery tries each login on each device and records which one worked.

### A2-A3. Discover
```bash
python a1_discover.py --range 172.31.4.0/24
```
You should see:
```
[A2] 5 address(es) answer on TCP/22
[A2] 172.31.4.11: frr (from probe 'show version', login profile 1)      (also .12 and .13)
[A2] 172.31.4.21: linux (from SSHDetect, login profile 2)
[A2] 172.31.4.22: linux (from SSHDetect, login profile 2)
[A3] 172.31.4.21 -> fw1 (netmiko:linux (7 commands)); ...
```
For Linux boxes ANO reads:
- addresses, links and routes with `ip -j`
- the firewall rules with `iptables-save`
- the hostname, the OS release and whether the box forwards traffic

It turns these into one replayable script: `cat out/configs/fw1.cfg`.

### A4. Model
```bash
python a2_model.py
```
You should see:
```
[A4] fw1: Batfish models this device only partly; NOT verified by Batfish: ['route 10.0.0.0/8 via 10.3.3.1 (Batfish
     hosts model only one default gateway)', "iptables stateful rule '-A FORWARD -m state --state INVALID -j DROP'
     (Batfish skips connection-state matches)"]
[A4] srv1: Batfish models this device only partly; ...
Model: 5 devices, 5 links -> ./ano/out/model.yaml
```
These are ANO telling you exactly what the proof will not cover. The twin (A8) and the post-check cover them.
The two Linux cables are answered in `answers.yaml` (`link:fw1:eth1|r3:eth3: direct`, `link:fw1:eth2|srv1:eth1: direct`).

### A5. Intent and where to enforce it
```bash
python a3_intent.py --intent-json inputs/flowa_intent_mixed.json
```
You should see:
```
Q1. Enforce on which ingress interface? Candidates in path order, closest to the source first: ['r1:eth3', 'r3:eth1', 'fw1:eth1'].
```
ANO found these with a Batfish traceroute along the real path. The firewall is the right place: it can match
the port, while the routers can only null-route the whole subnet.
```bash
python a3_intent.py --intent-json inputs/flowa_intent_mixed.json --accept fw1:eth1
```
`inputs/flowa_intent_mixed.json` also says that HTTPS (tcp/443) must keep working. ANO will test that too.

### A6. Render
```bash
python a4_render.py
cat out/undo/fw1.cfg
```
You should see:
```
--- change for fw1 (linux) ---
iptables -I FORWARD 1 -i eth1 -s 10.1.1.0/24 -d 10.4.4.0/24 -p tcp -m tcp --dport 22 -j DROP
iptables -I INPUT 1 -i eth1 -s 10.1.1.0/24 -d 10.4.4.0/24 -p tcp -m tcp --dport 22 -j DROP
```
The FORWARD rule stops traffic passing through the firewall. The INPUT rule stops traffic addressed to the
firewall itself (10.4.4.1), which never passes FORWARD; Batfish found that gap during testing. The undo uses `-D`
for the same two rules.

### A7. Prove it with Batfish
```bash
python a5_batfish.py --base out/batfish/base --candidate out/batfish/candidate --intent out/intent.json
```
You should see:
```
3. Intent and must-still-work checks on the candidate
  [ok] intent: block-ssh-lan1-to-servers: fw1 filter::FORWARD denies all of it (dst 10.4.4.0/24)
  [ok] intent: block-ssh-lan1-to-servers: fw1 filter::INPUT denies all of it (dst 10.4.4.1)
  [PASS] intent: block-ssh-lan1-to-servers: denied by firewall rules on the path
  [ok] must still work #1: fw1 filter::FORWARD permits all of it (dst 10.4.4.1,10.4.4.10)
  [PASS] must still work #1: expect permit (dst 10.4.4.1,10.4.4.10)
4. Collateral damage: every flow whose outcome changed
  [OK] fw1 filter::FORWARD: '-i eth1 -s 10.1.1.0/24 -d 10.4.4.0/24 -p tcp -m tcp --dport 22 -j DROP' now DENY (was 'default')
RESULT: ALL CHECKS PASSED
```
Why the `[ok]` lines: Batfish reads Linux firewall rules but does not apply them on the path. So ANO proves the
rules chain by chain where the traffic enters the firewall, then combines that with the path check. Step 4
fails if any rule other than the ones ANO added changes behaviour.

### A8. Twin and rehearsal
```bash
python a6_twin.py --endpoints inputs/flowa_endpoints_mixed.yaml     # Twin: 6 nodes, 6 links
cd out/twin && sudo -E containerlab deploy -t twin.clab.yml && cd ../..
python postcheck.py --target twin --baseline
python deploy.py --target twin --commit
python postcheck.py --target twin
docker exec clab-ano-twin-h1 sh -c 'echo | nc -w2 10.4.4.10 22; echo | nc -w2 10.4.4.10 443'
python deploy.py --target twin --rollback
```
In the twin, the Linux boxes replay their own config script at boot (`binds` + `exec` in `twin.clab.yml`).

### A10-A11. Deploy, check, roll back, apply, confirm
```bash
python postcheck.py --target prod --baseline            # r1, r2, r3, fw1, srv1: ok
python deploy.py --target prod --diff
python deploy.py --target prod --commit
docker exec clab-ano-mixed-h1 sh -c 'echo | nc -w2 10.4.4.10 22 | head -c 20; echo; echo | nc -w2 10.4.4.10 443'
```
AFTER: the first line is empty (SSH blocked), the second is `tls-ok` (HTTPS still works).
```bash
python postcheck.py --target prod                       # POSTCHECK PASSED (no new problems)
python deploy.py --target prod --rollback               # SSH answers again
python deploy.py --target prod --commit
python deploy.py --target prod --confirm
```
`--confirm` on Linux needs to know how this server keeps its rules after a reboot. That differs per Linux
distribution, so ANO asks the first time:
```
QUESTION persist:fw1: How does this server persist iptables and routes (e.g. 'iptables-save > /etc/iptables/rules.v4', ...)
```
The shipped `answers.yaml` has the answer for this lab (Alpine):
`persist:fw1: "mkdir -p /etc/iptables && iptables-save > /etc/iptables/rules-save"`.
For a Debian/Ubuntu server it would be `iptables-save > /etc/iptables/rules.v4` (package iptables-persistent).

### A12. Monitoring
```bash
python a7_monitor.py
```
You should see r1, r2 and r3 on `ano ssh exporter`, and fw1 and srv1 on `suzieq ssh polling`. Then start
them as in Example 1, A12, with `LAB_MGMT_NET=ano-mixed-mgmt`.

---------------------------------------------------------------------------------------------------------------

## Example 3. Flow B, small: build a router + firewall + server from blank

```
 pc 10.5.0.10 --(10.5.0.0/24)-- g1 --(10.5.1.0/24)-- gfw --(10.5.2.0/24)-- gsrv 10.5.2.10
                              router            Linux firewall          Linux server (test service on 443)
```
**Goal:** "The PC LAN reaches the server LAN through a firewall: HTTPS and ping allowed, SSH and everything else blocked."
**Starting point:** all three boxes are blank (only their management address and login), like new hardware.

### B0. Start the blank devices
```bash
cd topologies/mixed && sudo -E containerlab deploy -t blank.clab.yml && cd ../../ano
export ANO_USER=admin ANO_PASS=admin ANO_USER_2=lxadmin ANO_PASS_2=ano-lab ANO_SECRET_2=ano-lab
```

### B0b. Read the devices first (because you have SSH access)
```bash
python a1_discover.py --range 172.31.5.0/24
python a2_model.py --no-batfish
python b0_prefill.py
```
`a2_model.py` asks whether the islands `[['g1'], ['gfw'], ['gsrv']]` are separate networks. That is expected: the
boxes are blank, so no cabling is visible yet. Continue anyway. `b0_prefill.py` should print:
```
Draft: 3 devices, 5 interfaces, 0 cables -> ./ano/out/intake_draft.yaml
```
Open `out/intake_draft.yaml`.
- **Filled from the devices:** every fact ANO could read, such as the name, platform, OS version, management IP, SSH port, which login worked, and every interface that exists.
- **Left as `null` for you:** every design field, such as the role, addresses, routes and policies.

To see what is missing:
```bash
python b1_intake.py --intake out/intake_draft.yaml
```
You should see 28 numbered questions, for example
`Missing or invalid: devices.0.role` and `Missing or invalid: interfaces.0.mode`.

### B1-B2. Describe the design
Fill in the draft, or compare it with the finished example `inputs/flowb_intake_mixed.yaml`.
In that file:
- **Device facts** match what B0b read.
- **Interfaces:** each one has its mode and IP.
- **Static routes:**
  - g1 reaches the server LAN via gfw.
  - gfw reaches the PC LAN via g1.
  - gsrv reaches 10.5.0.0/16 via gfw.
- **Three policies on gfw:eth1 inbound:** permit tcp/443 (seq 10), permit icmp (seq 20), deny the rest (seq 30).
- **Three acceptance tests:** HTTPS permit, ping permit, SSH deny.

The login written to new devices is given as a hash only: create one with `openssl passwd -6`.
```bash
python b1_intake.py --intake inputs/flowb_intake_mixed.yaml
cat out/changes/g1.cfg out/changes/gfw.cfg out/changes/gsrv.cfg
```
You should see `Intake valid. 3 devices, 2 links.` plus two lines naming the Linux static routes Batfish cannot
model. `gfw.cfg` is a shell script: `sysctl -w net.ipv4.ip_forward=1`, the addresses, the route, then the iptables
rules in sequence order. ANO decided gfw must forward because g1 routes through it. For a Linux box with several
interfaces and no such evidence, it asks (`forward:<device>: yes | no`).

With an LLM instead of writing YAML: `python b1_intake.py --base out/intake_draft.yaml --prompt "..." --llm <model>`.
The LLM fills only the empty fields; facts read from the devices are never overwritten.

### B3. Prove the design with Batfish
```bash
python a5_batfish.py --candidate out/batfish/candidate --tests inputs/flowb_intake_mixed.yaml
```
You should see `[PASS] pc-https-to-server`, `[PASS] pc-ping-to-server`, `[PASS] pc-ssh-blocked: denied by firewall
rules on the path` and `RESULT: ALL CHECKS PASSED`.

**Try a mistake:** in a copy of the intake, change the deny rule's `sequence: 30` to `sequence: 5`, then run b1 and
a5 on the copy. HTTPS now fails, and ANO names the rule that blocks it:
```
[no] pc-https-to-server: gfw filter::FORWARD permits all of it ...
     counter-example: ... -> DENY -i eth1 -s 10.5.0.0/24 -d 10.5.2.0/24 -j DROP
```

### B4. Twin
```bash
python a6_twin.py --blank --endpoints inputs/flowb_endpoints_mixed.yaml      # Twin: 4 nodes, 3 links
cd out/twin && sudo -E containerlab deploy -t twin.clab.yml && cd ../..
python deploy.py --target twin --changes out/twin/changes --commit
docker exec clab-ano-twin-pc sh -c 'echo | nc -w2 10.5.2.10 443; ping -c1 -W2 10.5.2.10'
```

### B5. Reality check, then deploy to the real devices
```bash
python b2_reality_check.py --intake inputs/flowb_intake_mixed.yaml
```
You should see `== g1: matches the description` and the same for gfw and gsrv, then `REALITY CHECK PASSED`.
The check compares the description with the real devices: OS version, model, interfaces, and cabling.
These boxes have no LLDP, so each cable must be confirmed in `answers.yaml`.
The shipped file has `cable:g1:eth1|gfw:eth1: checked`.
```bash
python deploy.py --target prod --commit
docker exec clab-ano-mixed-blank-pc sh -c 'for p in 443 22 8080; do echo port $p: $(echo | nc -w2 10.5.2.10 $p); done; ping -c1 -W2 10.5.2.10'
```
You should see `port 443: tls-ok`, `port 22:` (empty, blocked) and `port 8080:` (empty, blocked).
Ping should answer (`1 packets received`).
```bash
python postcheck.py --target prod                    # POSTCHECK PASSED
python deploy.py --target prod --confirm
```
`--confirm` saves g1 (write memory) and asks `persist:gfw` and `persist:gsrv`. Add them to `answers.yaml`, for example:
```yaml
persist:gfw: "mkdir -p /etc/iptables && iptables-save > /etc/iptables/rules-save"
persist:gsrv: none          # 'none' = keep until reboot only; on a real server give its netplan/nmcli command
```
Run `--confirm` again. You should see `gfw [confirm] ok` and `gsrv [confirm] ok ... kept until reboot only (as answered)`.

---------------------------------------------------------------------------------------------------------------

## Example 4. Flow B, complex: 4 routers, OSPF, iBGP, a sinkhole, failure tests

```
                 s2
    hq-pc --- s1 <  > s4 --- br-pc            HQ LAN 10.10.10.0/24, branch LAN 10.40.40.0/24
                 s3                           two paths, OSPF everywhere, iBGP s1<->s4 over loopbacks,
                                              s4 sinkholes 192.0.2.0/24 and announces it in BGP
```
**Goal:** HQ and branch reach each other over either path. Traffic from HQ to 192.0.2.0/24 is dropped at the
branch. The network must survive the loss of one link on each side.

### B0. Start 4 blank routers
```bash
cd topologies/frr && sudo -E containerlab deploy -t blank.clab.yml && cd ../../ano
export ANO_USER=admin ANO_PASS=admin
```

### B0b. Read them first
```bash
python a1_discover.py --range 172.31.3.0/24
python a2_model.py --no-batfish          # islands question: expected on blank boxes
python b0_prefill.py                     # Draft: 4 devices, 10 interfaces, 0 cables
```
Compare `out/intake_draft.yaml` with the finished `inputs/flowb_intake_frr.yaml`. The draft carries the real OS
version and interface names read from the boxes. Copy those facts into your intake: they are what B5 checks.

### B1-B2. The full description
`inputs/flowb_intake_frr.yaml` states every fact, and nothing has a default:
- **Per device:** the loopback and router ID.
- **Per interface:** the IP, the OSPF area and network type, and whether it is passive.
- **Cables:** the four links between the routers.
- **OSPF:** process 1.
- **BGP:** AS 65000 on s1 and s4 with update-source lo, and s4 announces 192.0.2.0/24.
- **Sinkhole:** a static blackhole for 192.0.2.0/24 on s4.
- **Acceptance tests (3):** HQ to branch ICMP permit, branch to HQ TCP/22 permit, HQ to the sinkhole deny.
- **Failure tests (2):** s1:eth1 down, then HQ to branch must still work; s4:eth2 down, then branch to HQ must still work.
```bash
python b1_intake.py --intake inputs/flowb_intake_frr.yaml
cat out/changes/s1.cfg
```
You should see `Intake valid. 4 devices, 4 links.` To see the no-assumption rule at work, delete one `router_id`
or point an interface at an undefined VLAN, then run b1 again. It stops with a question.

### B3. Prove it, including the failures
```bash
python a5_batfish.py --candidate out/batfish/candidate --tests inputs/flowb_intake_frr.yaml
```
You should see:
```
  [PASS] hq-to-branch: expect permit (dst 10.40.40.1)
  [PASS] branch-to-hq-tcp: expect permit (dst 10.10.10.1)
  [PASS] hq-to-sinkhole: expect deny (dst 192.0.2.0/24)
4b. Failure tests (Batfish forks the snapshot with interfaces shut)
  [PASS] survive-s1-s2-down -> hq-to-branch: expect permit (dst 10.40.40.1)
  [PASS] survive-s3-s4-down -> branch-to-hq-tcp: expect permit (dst 10.10.10.1)
  [PASS] 0 loop(s)
RESULT: ALL CHECKS PASSED
```

### B4. Twin, with a live failure test
```bash
python a6_twin.py --blank --endpoints inputs/flowb_endpoints_frr.yaml        # Twin: 6 nodes, 6 links
cd out/twin && sudo -E containerlab deploy -t twin.clab.yml && cd ../..
python deploy.py --target twin --changes out/twin/changes --commit
sleep 45                                                                      # OSPF + BGP come up
python postcheck.py --target twin
docker exec clab-ano-twin-hq-pc ping -c 2 10.40.40.10                        # works
docker exec clab-ano-twin-hq-pc ping -c 2 -W 1 192.0.2.1 || echo "sinkholed (expected)"
sudo ip netns exec clab-ano-twin-s1 ip link set eth1 down
docker exec clab-ano-twin-hq-pc ping -c 2 10.40.40.10                        # still works, via s3
sudo ip netns exec clab-ano-twin-s1 ip link set eth1 up
```

### B5. Reality check, deploy, verify
```bash
python b2_reality_check.py --intake inputs/flowb_intake_frr.yaml
```
**If the OS version does not match:** the shipped intake says FRR 10.7.1, the version of the image named in Part 0.
If your routers run another version, ANO blocks the deploy:
```
== s1: OS is 8.4_git, described 10.7.1
REALITY CHECK FAILED: deploy blocked, the twin was built from a wrong description
```
This is the check doing its job. Fix the description (B0b shows the real version) and run b1 and b2 again.
When it prints `REALITY CHECK PASSED`:
```bash
python deploy.py --target prod --commit
sleep 45
docker exec clab-ano-blank-s1 vtysh -c 'show ip ospf neighbor'   # 2 neighbours in state Full
docker exec clab-ano-blank-hq-pc ping -c 2 10.40.40.10      # 2 packets received
docker exec clab-ano-blank-hq-pc ping -c 2 -W 1 192.0.2.1   # 100% packet loss (sinkhole)
python postcheck.py --target prod && python deploy.py --target prod --confirm
```

### B6. Monitoring
Same as Example 1, A12, with `LAB_MGMT_NET=ano-blank-mgmt`.

---------------------------------------------------------------------------------------------------------------

## Running on your real devices

Change only three things.
1. **The range and logins:**
   - `--range` takes your management subnet.
   - Set `ANO_USER`/`ANO_PASS` (plus `_2`, `_3` ... for other device groups) and `ANO_SECRET<n>` where enable or sudo is needed.
2. **The twin images:** set `ANO_IMAGE_<KIND>` to the same OS version your devices run. ANO tells you which kind and version it needs.
3. **Interface names:** physical names such as `Ethernet49/1` may not exist in the virtual image. `a6_twin.py` asks
   which twin port stands in for each one (`twin-if:<device>:<interface>`) and renames it only in the twin.

Before any production push, keep console access ready for devices whose management or login settings you change.
ANO cannot roll back over SSH if the change cuts SSH itself.

## What was tested, and what was not

Tested, with the outputs above:
- **All four examples, end to end.**
  - Each ran from a clean `out/` folder, with the same commands as in this guide.
  - The networks were built with the scripts in `setup/sandbox-standin/` instead of Containerlab (it could not be installed in that sandbox).
  - The routers ran FRR 8.4, because the 10.7.1 image could not be downloaded there.
  - Container names there have no `clab-...-` prefix.
- **Every Batfish check**, run on Batfish 2026.08.

Not run there, so do these first on your machine and report anything that differs:
- `containerlab deploy` of the labs and twins, which covers all the A8 and B4 steps.
- Building the `ano-linux` image. It was replaced by an equivalent Alpine container logged in as root, so the
  sudo path (`ANO_SECRET_2`) was not exercised.
- The monitoring stack, because Docker Hub rate-limited the Prometheus image.
