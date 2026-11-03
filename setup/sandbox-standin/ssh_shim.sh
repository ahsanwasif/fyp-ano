#!/bin/bash
# TEST-ONLY: an SSH login on each container's own mgmt IP (sshd runs in the container's network namespace).
#   routers: user admin  -> vtysh        (like the FRR containerlab image)
#   linux:   user lxadmin -> root shell  (like a server you log into)
id admin >/dev/null 2>&1 || { sudo useradd -m admin; echo admin:admin | sudo chpasswd; }
id lxadmin >/dev/null 2>&1 || { sudo useradd -m -s /bin/bash lxadmin; echo lxadmin:ano-lab | sudo chpasswd; }
sudo usermod -aG docker admin; sudo usermod -aG docker lxadmin
sudo mkdir -p /run/sshd
for spec in "$@"; do r=${spec%%:*}; kind=${spec##*:}
  pid=$(docker inspect -f '{{.State.Pid}}' $r)
  if [ "$kind" = frr ]; then user=admin; cmd="docker exec -it $r vtysh"; docker exec $r sh -c 'touch /etc/frr/vtysh.conf'
  else user=lxadmin; cmd="docker exec -it $r sh"; fi
  sudo tee /tmp/sshd_$r.conf >/dev/null <<EOC
Port 22
ListenAddress 0.0.0.0
PidFile /run/sshd_$r.pid
PasswordAuthentication yes
KbdInteractiveAuthentication no
UsePAM yes
AllowUsers $user
Match User $user
  ForceCommand $cmd
EOC
  sudo nsenter -t $pid -n /usr/sbin/sshd -f /tmp/sshd_$r.conf
done
