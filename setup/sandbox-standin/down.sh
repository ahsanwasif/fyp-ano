#!/bin/bash
# Removes every stand-in lab container and the per-container sshd processes.
docker rm -f r1 r2 r3 h1 h2 s1 s2 s3 s4 hq-pc br-pc fw1 srv1 g1 gfw gsrv pc >/dev/null 2>&1
sudo pkill -f '[s]shd -f /tmp/sshd_' 2>/dev/null; sudo ip link del tmpA 2>/dev/null; true
