#!/bin/bash
# Stand-in for: cd topologies/frr && sudo -E containerlab deploy -t blank.clab.yml
set -e; source "$(dirname "$0")/lib.sh"
net anoblank 172.31.3.0/24
B=$(mktemp -d); chmod 755 $B
for n in s1:11 s2:12 s3:13 s4:14; do r=${n%%:*}; printf 'frr defaults traditional\nhostname %s\n!\n' $r > $B/$r.conf; chmod 666 $B/$r.conf
  frr $r anoblank 172.31.3.${n##*:} $B/$r.conf; done
lnx hq-pc anoblank 172.31.3.21; lnx br-pc anoblank 172.31.3.22
link s1 eth1 s2 eth1; link s1 eth2 s3 eth1; link s2 eth2 s4 eth1; link s3 eth2 s4 eth2; link s1 eth3 hq-pc eth1; link s4 eth3 br-pc eth1
docker exec hq-pc sh -c 'ip addr add 10.10.10.10/24 dev eth1; ip route add 10.0.0.0/8 via 10.10.10.1; ip route add 192.0.2.0/24 via 10.10.10.1'
docker exec br-pc sh -c 'ip addr add 10.40.40.10/24 dev eth1; ip route add 10.0.0.0/8 via 10.40.40.1'
bash $SI/ssh_shim.sh s1:frr s2:frr s3:frr s4:frr
echo "FRR blank lab up (s1-s4, hq-pc, br-pc)"
