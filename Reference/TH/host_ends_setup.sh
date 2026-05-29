#!/bin/bash

set -u

obs_vlan_id='2120'
obs_network='192.168.140.0/24'
obs_cluster_macaddr='AA:BB:CC:DD:00:01'
obs_cluster_ipaddr='192.168.140.1'
obs_ivi_macaddr='AA:BB:CC:DD:00:02'
obs_ivi_ipaddr='192.168.140.2'
obs_qnx_macaddr='AA:BB:CC:DD:00:2A'
obs_qnx_ipaddr='192.168.140.42'
obs_host_macaddr='AA:BB:CC:DD:00:38'
obs_host_ipaddr='192.168.140.56/24'
qnx_host_ipaddr='192.168.1.22/24'

if [[ $# -eq 1 ]]; then
  # phylink name connected to CDC
  # enX vs hardware
  devlink="$1"
else
  echo "usage : ./host_ends_setup.sh <CDC_NETWORK_INTERFACE>"
  exit
fi


# Create a veth pair
#sudo ip link add veth-obs type veth peer name veth-obs-peer

# Host OBS
sudo ip link add link "${devlink}" name "veth-obs.${obs_vlan_id}" type vlan id "${obs_vlan_id}"
# Renault spec is 1 for VLAN2120
sudo ip link set "veth-obs.${obs_vlan_id}" type vlan egress 0:1 1:1 2:1 3:1 4:1 5:1 6:1 7:1
sudo ip link set dev "veth-obs.${obs_vlan_id}" address "${obs_host_macaddr}"
sudo ip link set dev "veth-obs.${obs_vlan_id}" up
sudo ip addr add "${obs_host_ipaddr}" dev "veth-obs.${obs_vlan_id}"
sudo ip route add "${obs_network}" dev "veth-obs.${obs_vlan_id}"

# neighbours
sudo ip link set dev "veth-obs.${obs_vlan_id}" arp off
sudo ip neigh replace "${obs_cluster_ipaddr}" lladdr "${obs_cluster_macaddr}" dev "veth-obs.${obs_vlan_id}"
sudo ip neigh replace "${obs_ivi_ipaddr}" lladdr "${obs_ivi_macaddr}" dev "veth-obs.${obs_vlan_id}"
sudo ip neigh replace "${obs_qnx_ipaddr}" lladdr "${obs_qnx_macaddr}" dev "veth-obs.${obs_vlan_id}"

<<COMMENT
# add to cvd-ebr bridge
# should be created by TH
sudo ip link add name cvd-ebr type bridge || true
sudo ip link set cvd-ebr up
sudo ip link set veth-obs-peer master cvd-ebr
sudo ip link set veth-obs-peer up

if [[ $# -eq 1 ]]; then
  # phylink name connected to CDC
  # enX vs hardware
  devlink="$1"
  sudo ip link set "${devlink}" master cvd-ebr

  # qnx
  sudo ip addr add "${qnx_host_ipaddr}" dev cvd-ebr
fi
COMMENT
