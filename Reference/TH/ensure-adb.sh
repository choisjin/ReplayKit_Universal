#!/bin/bash

set -eu

if env | grep -q CLUSTER_DEVICE && env | grep -q IVI_DEVICE; then
  if ! adb devices -l | grep -q cdc_r_cluster; then
    adb connect "${CLUSTER_DEVICE}"
  fi
  if ! adb devices -l | grep -q cdc_r_ivi; then
    adb connect "${IVI_DEVICE}"
  fi
  set +eu

  return 0
fi

CLUSTER_DEVICE=$(adb devices -l | awk '/cdc_r_cluster/{print $1; exit}')
# OBS
if [[ -z "${CLUSTER_DEVICE}" ]]; then
  adb connect 192.168.140.1
  CLUSTER_DEVICE=$(adb devices -l | awk '/cdc_r_cluster/{print $1; exit}')
fi
# Legacy
if [[ -z "${CLUSTER_DEVICE}" ]]; then
  adb connect localhost
  CLUSTER_DEVICE=$(adb devices -l | awk '/cdc_r_cluster/{print $1; exit}')
fi
# Error
if [[ -z "${CLUSTER_DEVICE}" ]]; then
  echo 'CLUSTER not detected'
  set +eu

  return 1
fi

IVI_DEVICE=$(adb devices -l | awk '/cdc_r_ivi/{print $1; exit}')
# OBS
if [[ -z "${IVI_DEVICE}" ]]; then
  adb connect 192.168.140.2
  IVI_DEVICE=$(adb devices -l | awk '/cdc_r_ivi/{print $1; exit}')
fi
# Legacy
if [[ -z "${IVI_DEVICE}" ]]; then
  adb connect localhost
  IVI_DEVICE=$(adb devices -l | awk '/cdc_r_ivi/{print $1; exit}')
fi

# Error
if [[ -z "${IVI_DEVICE}" ]]; then
  echo 'IVI not detected'
  set +eu

  return 1
fi

export CLUSTER_DEVICE
export IVI_DEVICE

set +eu
