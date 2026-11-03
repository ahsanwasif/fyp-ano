#!/bin/sh
# Mixed lab server srv1, with a test service on 22 (sshd) and 443 (a listener that answers "tls-ok").
ip addr add 10.4.4.10/24 dev eth1
ip route replace 10.0.0.0/8 via 10.4.4.1 dev eth1
(while true; do nc -l -p 443 -e echo tls-ok; done) >/dev/null 2>&1 &
