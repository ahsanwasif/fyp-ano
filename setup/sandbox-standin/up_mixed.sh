#!/bin/bash
# Stand-in for: cd topologies/mixed && sudo -E containerlab deploy -t prod.clab.yml
#   h1 (10.1.1.10) -- r1 -- r2 -- r3 -- fw1 -- srv1 (10.4.4.10)
set -e; source "$(dirname "$0")/lib.sh"
net anomix 172.31.4.0/24
for n in r1:11 r2:12 r3:13; do frr ${n%%:*} anomix 172.31.4.${n##*:} $ROOT/configs/mixed/prod/${n%%:*}.conf; done
lnx fw1 anomix 172.31.4.21; lnx srv1 anomix 172.31.4.22; lnx h1 anomix 172.31.4.31
link r1 eth1 r2 eth1; link r1 eth2 r3 eth1; link r2 eth2 r3 eth2; link r1 eth3 h1 eth1; link r3 eth3 fw1 eth1; link fw1 eth2 srv1 eth1
for b in fw1 srv1; do docker cp $ROOT/configs/mixed/prod/$b.sh $b:/start.sh; docker exec $b sh /start.sh; done
docker exec h1 sh -c 'ip addr add 10.1.1.10/24 dev eth1; ip route add 10.0.0.0/8 via 10.1.1.1'
bash $SI/ssh_shim.sh r1:frr r2:frr r3:frr fw1:linux srv1:linux
echo "mixed lab up (r1 r2 r3 fw1 srv1 h1)"
