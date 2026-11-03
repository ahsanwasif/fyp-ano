#!/bin/sh
# Mixed lab firewall fw1 as an admin would have built it (ANO never reads this file; it reads the box over SSH).
sysctl -qw net.ipv4.ip_forward=1
ip addr add 10.3.3.2/24 dev eth1
ip addr add 10.4.4.1/24 dev eth2
ip route replace 10.0.0.0/8 via 10.3.3.1 dev eth1
iptables -A FORWARD -m state --state INVALID -j DROP
iptables -A FORWARD -d 10.4.4.0/24 -p tcp --dport 23 -j DROP
