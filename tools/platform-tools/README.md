# 동봉 adb (platform-tools)

전 PC에서 **동일한 adb 클라이언트/서버**를 쓰기 위해 adb 바이너리를 여기에 직접 동봉한다.
시스템 PATH에 잡힌 제각각 버전의 adb 대신 이 폴더의 adb가 우선 사용된다
(`backend/app/services/adb_path.py::resolve_adb_path`).

## 여기에 넣을 파일 (Google 공식 platform-tools에서 추출)

Windows (필수 3개 — DLL 빠지면 디바이스 인식 실패):

```
tools/platform-tools/adb.exe
tools/platform-tools/AdbWinApi.dll
tools/platform-tools/AdbWinUsbApi.dll
```

Linux 배포까지 통일하려면:

```
tools/platform-tools/adb
```

## 받는 곳

https://dl.google.com/android/repository/platform-tools-latest-windows.zip
(압축 안의 `platform-tools/` 에서 위 파일만 이 폴더로 복사)

전 PC가 같은 버전을 쓰도록 **한 버전으로 고정**해 커밋한다(현재 권장: 35.x).
이 파일들은 `.gitignore` 에서 negative pattern으로 추적 대상에 포함되어 `git pull` 동기화로 배포된다.

## 동작 메모

- 미배치(파일 없음) 시 resolver는 시스템 PATH의 `adb` 로 graceful fallback 한다.
- adb 서버 포트는 **기본 5037을 그대로 공유**한다(시스템 `adb devices` 와 동일 서버·동일
  디바이스 목록). 전용 포트로 격리하면 별도 서버가 USB 디바이스를 5037 서버와 경합해
  앱이 디바이스를 못 보는 문제가 생기므로 격리하지 않는다.
- `ADB_PATH` 환경변수를 지정하면 그 경로가 최우선으로 사용된다(디버그/예외 PC용).
