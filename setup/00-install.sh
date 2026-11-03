#!/usr/bin/env bash
# ANO test lab: one-time host setup (Ubuntu 22.04/24.04, 4+ vCPU, 16 GB RAM recommended)
set -euo pipefail

# 1. Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"      # log out and back in after this

# 2. Containerlab (official installer)
bash -c "$(curl -sL https://get.containerlab.dev)"

# 3. gNMIc (gNMI client, used for monitoring capability checks)
bash -c "$(curl -sL https://get-gnmic.openconfig.net)"

# 4. PuTTY tools (only needed to convert a .ppk key to OpenSSH format)
sudo apt-get update && sudo apt-get install -y putty-tools python3-venv jq

# 5. Python environment for the ANO scripts
python3 -m venv ~/ano-venv
~/ano-venv/bin/pip install -U pip
~/ano-venv/bin/pip install -r "$(dirname "$0")/../ano/requirements.txt"

# 6. Batfish service (digital twin engine), ports 9996/9997
docker run -d --name batfish --restart unless-stopped \
  -v batfish-data:/data -p 9996:9996 -p 9997:9997 batfish/batfish

# 7. Lab images
#    a) FRR lab (public, no login). The containerlab-<ver> tag has SSH built in (FRR 10.7.1 and later only).
#       Do NOT use frrouting/frr:latest from Docker Hub (FRR 8.4, no SSH).
docker pull quay.io/frrouting/frr:containerlab-10.7.1
echo 'export ANO_IMAGE_FRR=quay.io/frrouting/frr:containerlab-10.7.1' >> ~/.bashrc
#    b) cEOS lab (optional): free arista.com account required. Download cEOS64-lab-<version>.tar.xz, then:
#       docker import cEOS64-lab-<version>.tar.xz ceos:<version>
#       export ANO_IMAGE_ARISTA_CEOS=ceos:<version>   (topologies and a6_twin.py read ANO_IMAGE_<KIND>)
#    c) Linux test endpoints (public):
docker pull ghcr.io/hellt/network-multitool:latest
#    d) Mixed lab: the Linux server / firewall image (built locally from Alpine, no account needed)
docker build -t ano-linux:1 "$(dirname "$0")/images/ano-linux"
echo 'export ANO_IMAGE_LINUX=ano-linux:1' >> ~/.bashrc

echo "Done. Log out/in for docker group, then follow GUIDE.md (step by step) or RUNBOOK.md (reference)."
