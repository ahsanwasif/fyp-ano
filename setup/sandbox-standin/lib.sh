# shared helpers for the stand-in labs (sourced)
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"; SI="$ROOT/setup/sandbox-standin"
FRR_IMG=${STANDIN_FRR_IMAGE:-frrouting/frr:latest}
net(){ docker network inspect $1 >/dev/null 2>&1 || docker network create --subnet $2 $1 >/dev/null; }
# each router gets a COPY of its config: 'write memory' must never change the files in this package.
# FRR's zebra sometimes crashes on its first start in this sandbox kernel: the router is then re-created
# (before any cable is attached), up to 3 times.
frr(){ for try in 1 2 3; do frr_once "$@"; sleep 4; docker exec $1 pgrep zebra >/dev/null 2>&1 && return 0
  echo "zebra did not start on $1, re-creating it"; done; return 1; }
frr_once(){ docker rm -f $1 >/dev/null 2>&1 || true
  local c; c=$(mktemp -d)/frr.conf; cp $4 $c; chmod 666 $c; chmod 755 $(dirname $c)
  docker run -d --name $1 --hostname $1 --privileged --network $2 --ip $3 \
    -v $SI/daemons:/etc/frr/daemons -v $c:/etc/frr/frr.conf $FRR_IMG >/dev/null; }
lnx(){ docker rm -f $1 >/dev/null 2>&1 || true
  docker run -d --name $1 --hostname $1 --privileged --network $2 --ip $3 --entrypoint sleep $FRR_IMG infinity >/dev/null; }
link(){ pa=$(docker inspect -f '{{.State.Pid}}' $1); pb=$(docker inspect -f '{{.State.Pid}}' $3)
  sudo ip link add tmpA type veth peer name tmpB; sudo ip link set tmpA netns $pa; sudo ip link set tmpB netns $pb
  sudo nsenter -t $pa -n ip link set tmpA name $2; sudo nsenter -t $pa -n ip link set $2 up
  sudo nsenter -t $pb -n ip link set tmpB name $4; sudo nsenter -t $pb -n ip link set $4 up; }
