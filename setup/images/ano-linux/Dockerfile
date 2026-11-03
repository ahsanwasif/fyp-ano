# A minimal Linux server/firewall for the labs: SSH login, iproute2 (ip -j), iptables (busybox nc for test services).
# Build once:  docker build -t ano-linux:1 setup/images/ano-linux   and   export ANO_IMAGE_LINUX=ano-linux:1
# Login: lxadmin / ano-lab. Like a real server it is NOT root: ANO uses sudo with the same password
# (export ANO_SECRET_2=ano-lab), because reading and changing iptables needs root.
FROM alpine:3.20
RUN apk add --no-cache openssh-server iproute2 iptables sudo \
 && ssh-keygen -A \
 && adduser -D -s /bin/sh lxadmin && echo 'lxadmin:ano-lab' | chpasswd \
 && echo 'lxadmin ALL=(ALL) ALL' > /etc/sudoers.d/lxadmin \
 && printf 'PasswordAuthentication yes\nPermitRootLogin no\n' >> /etc/ssh/sshd_config
CMD ["/usr/sbin/sshd", "-D", "-e"]
