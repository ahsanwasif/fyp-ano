#!/bin/bash
# Stand-in for: cd topologies/mixed && sudo -E containerlab deploy -t blank.clab.yml
#   pc (10.5.0.10) -- g1 (router) -- gfw (firewall) -- gsrv (server), all blank
set -e; source "$(dirname "$0")/lib.sh"
net anomixb 172.31.5.0/24
B=$(mktemp -d); chmod 755 $B; printf 'frr defaults traditional\nhostname g1\n!\n' > $B/g1.conf; chmod 666 $B/g1.conf
frr g1 anomixb 172.31.5.11 $B/g1.conf
lnx gfw anomixb 172.31.5.21; lnx gsrv anomixb 172.31.5.22; lnx pc anomixb 172.31.5.31
link g1 eth1 gfw eth1; link gfw eth2 gsrv eth1; link g1 eth2 pc eth1
docker exec -d gsrv sh -c 'while true; do nc -l -p 443 -e echo tls-ok; done'
docker exec pc sh -c 'ip addr add 10.5.0.10/24 dev eth1; ip route add 10.5.0.0/16 via 10.5.0.1'
bash $SI/ssh_shim.sh g1:frr gfw:linux gsrv:linux
echo "blank mixed lab up (g1 gfw gsrv pc)"
