#!/bin/sh
# 13.1" scene 해상도 + 레이어 원본크기 확인 (등록 해상도가 실제와 맞는지 진단).
# 사용법: cmd /c "ssh root@<13.1_IP> sh -s < <경로>\mib_res_check.sh"
export XDG_RUNTIME_DIR=/run/platform/weston
echo "===== get screen 0 (resolution + layer order) ====="
LayerManagerControl get screen 0 2>&1
echo
echo "===== 각 layer original size / dest ====="
for l in $(LayerManagerControl get screen 0 2>/dev/null | grep -oE '[0-9]+\(0x' | grep -oE '^[0-9]+'); do
  echo "-- layer $l"
  LayerManagerControl get layer "$l" 2>&1 | grep -iE 'original size|destination region|source region'
done
echo
echo "===== HMI 후보 surface 크기 (참고) ====="
for s in $(LayerManagerControl get surfaces 2>/dev/null | grep -oE '\(0x[0-9a-fA-F]+\)' | tr -d '()'); do
  echo "-- surface $s"
  LayerManagerControl get surface "$s" 2>&1 | grep -iE 'original size|destination region|visibility'
done
echo "===== DONE ====="
