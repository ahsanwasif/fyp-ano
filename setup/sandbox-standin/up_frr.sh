#!/bin/bash
# Stand-in for: cd topologies/frr && sudo -E containerlab deploy -t prod.clab.yml   (container names without clab-ano-prod-)
set -e; source "$(dirname "$0")/lib.sh"
net anomgmt 172.31.1.0/24
for n in r1:11 r2:12 r3:13; do frr ${n%%:*} anomgmt 172.31.1.${n##*:} $ROOT/configs/frr/prod/${n%%:*}.conf; done
lnx h1 anomgmt 172.31.1.21; lnx h2 anomgmt 172.31.1.22
link r1 eth1 r2 eth1; link r1 eth2 r3 eth1; link r2 eth2 r3 eth2; link r1 eth3 h1 eth1; link r3 eth3 h2 eth1
docker exec h1 sh -c 'ip addr add 10.1.1.10/24 dev eth1; ip route add 10.0.0.0/8 via 10.1.1.1'
docker exec h2 sh -c 'ip addr add 10.3.3.10/24 dev eth1; ip route add 10.0.0.0/8 via 10.3.3.1'
bash $SI/ssh_shim.sh r1:frr r2:frr r3:frr
echo "FRR lab up (r1 r2 r3, h1 h2)"
