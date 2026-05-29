#!/bin/bash
set -e

### ===== USER CONFIG =====
ETH_IF="enx00e04c68b2c8"
CVD_BR="cvd-ebr"
HOST_IP="192.168.1.152/24"
RBVM_IP="192.168.140.1:5555"
TH_ADB="0.0.0.0:6520"
TH_ROOT="/home/cdc/Desktop/TH"
GRPC_IP="192.168.1.99:50051"
### ======================

echo "=== [1] Network setup ==="

OLD_IP=$(ip -4 addr show dev ${CVD_BR} \
          | grep -oP '(?<=inet\s)\d+(\.\d+){3}/\d+' || true)

if [ -n "${OLD_IP}" ]; then
    echo "Found old IP on ${CVD_BR}: ${OLD_IP}"
    sudo ip addr del ${OLD_IP} dev ${CVD_BR}
else
    echo "No existing IP on ${CVD_BR}"
fi

sudo ip addr add ${HOST_IP} dev ${CVD_BR}
sudo ip link set ${ETH_IF} master ${CVD_BR}

echo "=== [2] ADB ensure ==="

cd ${TH_ROOT}

echo "[2-1] Host CDC network setup"
chmod +x ./host_ends_setup.sh
./host_ends_setup.sh ${ETH_IF}

echo "[2-2] ADB connect ensure"
chmod +x ./ensure-adb.sh
./ensure-adb.sh ${ETH_IF}

echo "[2-3] ADB devices check"

ADB_OUT=$(adb devices | sed '1d' | grep -w device || true)
echo "${ADB_OUT}"

DEV_CNT=$(echo "${ADB_OUT}" | wc -l)
HAS_RBVM=$(echo "${ADB_OUT}" | grep -c "${RBVM_IP}" || true)

if [ "${DEV_CNT}" -lt 2 ]; then
    echo "❌ ERROR: Less than 2 adb devices detected"
    exit 1
fi

if [ "${HAS_RBVM}" -ne 1 ]; then
    echo "❌ ERROR: RBVM (${RBVM_IP}) not connected"
    exit 1
fi

echo "✅ ADB devices check OK (CDC + RBVM connected)"

echo "=== [3] Select TH version (GUI) ==="
python3 ${TH_ROOT}/select_th_gui.py

if [ ! -f /tmp/th_home.txt ]; then
    echo "❌ TH selection cancelled or file not found"
    exit 1
fi

TH_HOME=$(cat /tmp/th_home.txt)

if [ ! -d "${TH_HOME}" ]; then
    echo "❌ Invalid TH_HOME: ${TH_HOME}"
    exit 1
fi

echo "Selected TH_HOME: ${TH_HOME}"

echo "=== [4] Launch TH ==="

gnome-terminal --title="TH Server" -- bash -c "
cd ${TH_HOME} && \
sudo HOME=\$PWD ANDROID_HOST_OUT=\$PWD \
./bin/launch_cvd \
-report_anonymous_usage_stats=n \
-guest-enforce-security=false \
--extra_bootconfig_args='androidboot.selinux=permissive androidboot.sdv.authz.enable=false';
exec bash
"

sleep 40

echo "=== [5] TH microservice run ==="

set +e

cd ${TH_HOME}/harness/harness/th_script || exit 1

if ! adb devices | grep -q "${TH_ADB}"; then
    echo "❌ ERROR: TH adb device (${TH_ADB}) not found"
    exit 1
fi

MAX_RETRY=3
RETRY=1
INTERRUPTED=0

trap 'echo; echo "🔁 Ctrl+C detected. Retrying..."; INTERRUPTED=1' INT

while [ ${RETRY} -le ${MAX_RETRY} ]; do
    INTERRUPTED=0

    echo "▶ Try ${RETRY}/${MAX_RETRY}"
    ./th_run_microservice.sh "${TH_ADB}"

    # Ctrl+C가 아닌 경우 = 정상 종료
    if [ ${INTERRUPTED} -eq 0 ]; then
        echo "✅ Microservice step completed"

        echo
        read -r -p "➡ Press Enter to continue to next step..."
        break
    fi

    echo "🔁 Retry requested (Ctrl+C)"
    RETRY=$((RETRY + 1))
    sleep 1
done

trap - INT

if [ ${RETRY} -gt ${MAX_RETRY} ]; then
    echo "❌ ERROR: Microservice failed after ${MAX_RETRY} retries"
    exit 1
fi

sleep 5

echo "=== [6] gRPC topic list ==="
cd ${TH_HOME}/harness/harness/grpc_client/src
python3 client.py --dt_topic_list --ip_address ${GRPC_IP}

echo "✅ TH + gRPC auto connection completed"
