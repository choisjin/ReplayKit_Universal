import { useState, useMemo, useEffect, useCallback } from 'react';
import { Button, Card, Checkbox, Input, InputNumber, List, Modal, Select, Space, Table, Tabs, Tag, Typography, message } from 'antd';
import { ReloadOutlined, PlusOutlined, DisconnectOutlined, DeleteOutlined, WifiOutlined, SearchOutlined, EditOutlined, ApiOutlined, LinkOutlined, SettingOutlined, HolderOutlined, FolderOpenOutlined } from '@ant-design/icons';
import { DndContext, closestCenter, PointerSensor, useSensor, useSensors, type DragEndEvent } from '@dnd-kit/core';
import { SortableContext, verticalListSortingStrategy, useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { useDevice, ManagedDevice } from '../context/DeviceContext';
import { useSettings } from '../context/SettingsContext';
import { deviceApi } from '../services/api';
import { useTranslation } from '../i18n';

const { Option } = Select;

interface ConnectField {
  name: string;
  label: string;
  type: 'text' | 'number' | 'select' | 'object_list' | 'password' | 'folder';
  default?: string;
  options?: string[];
  // object_list 전용: 각 row의 sub-field 정의
  item_fields?: ConnectField[];
  // object_list 전용: 기본 row 데이터 (편집 시 최초 항목 추가용)
  default_items?: Record<string, any>[];
  // true 면 폼에서 숨김. extra_fields 에는 default 값이 그대로 들어가서 백엔드에 전달됨.
  // 사용자 편집 UI 가 필요 없는 고급 설정 필드용.
  hidden?: boolean;
}

interface ModuleInfo {
  name: string;
  label: string;
  connect_type?: string;
  connect_fields?: ConnectField[];
}

interface SerialPort {
  port: string;
  description: string;
  hwid: string;
  manufacturer: string;
  vid: string;
  pid: string;
}

// 디바이스 ID에서 prefix 추출 (Android_1 → Android, POWER_2 → POWER)
function getDevicePrefix(id: string): string {
  // 시스템 기본 디바이스 — 단독 그룹을 만들지 않고 Common 그룹에 합쳐서 표시.
  if (id === 'WinControl') return 'Common';
  if (id === 'OCR') return 'Common';
  const m = id.match(/^(.+?)_\d+$/);
  return m ? m[1] : id;
}

// 그룹 표시 이름
const GROUP_LABELS: Record<string, string> = {
  Android: 'Android (ADB)',
  HKMC: 'HKMC Agent',
  iSAP: 'iSAP Agent',
  Serial: 'Serial',
  VisionCam: 'Vision Camera',
  Webcam: 'Webcam',
  Device: 'Device',
  Common: 'Common',  // 시스템 기본 디바이스 (Common, WinControl)
};

function SortableDeviceRow({ device, children }: { device: ManagedDevice; children: React.ReactNode }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: device.id });
  return (
    <div
      ref={setNodeRef}
      style={{
        transform: CSS.Transform.toString(transform),
        transition,
        opacity: isDragging ? 0.5 : 1,
        display: 'flex',
        alignItems: 'center',
        gap: 3,
        padding: '6px 8px',
        borderBottom: '1px solid #f0f0f0',
        background: isDragging ? '#fafafa' : undefined,
      }}
    >
      <HolderOutlined
        {...attributes}
        {...listeners}
        style={{ cursor: 'grab', color: '#bbb', flexShrink: 0, fontSize: 12 }}
      />
      {children}
    </div>
  );
}

export default function DevicePage() {
  const { t } = useTranslation();
  const { primaryDevices, auxiliaryDevices, loading, fetchDevices, connectDevice, disconnectDevice, updateDeviceLists, pauseDevicePolling, resumeDevicePolling } = useDevice();
  const { browseFolder } = useSettings();

  // ADB reconnect state
  const [reconnecting, setReconnecting] = useState(false);

  // 체크박스 선택 & 연결 상태
  const [selectedDeviceIds, setSelectedDeviceIds] = useState<Set<string>>(new Set());
  const [connectingAll, setConnectingAll] = useState(false);
  const [connectingIds, setConnectingIds] = useState<Set<string>>(new Set());

  const allDevices = [...primaryDevices, ...auxiliaryDevices];

  const handleAdbReconnect = async () => {
    setReconnecting(true);
    try {
      await deviceApi.adbRestart();
      message.success(t('device.adbRestart'));
      await fetchDevices();
    } catch {
      message.error(t('device.adbRestartFailed'));
    }
    setReconnecting(false);
  };

  // 전체 연결
  const handleConnectAll = async () => {
    setConnectingAll(true);
    try {
      const res = await deviceApi.connectRegistered();
      updateDeviceLists(res.data);
      message.success(t('device.connectAllSuccess'));
    } catch {
      message.error(t('device.connectFailed'));
    }
    setConnectingAll(false);
  };

  // 선택 연결
  const handleConnectSelected = async () => {
    if (selectedDeviceIds.size === 0) {
      message.warning(t('device.noSelection'));
      return;
    }
    setConnectingAll(true);
    try {
      const res = await deviceApi.connectRegistered(Array.from(selectedDeviceIds));
      updateDeviceLists(res.data);
      message.success(t('device.connectSelectedSuccess'));
    } catch {
      message.error(t('device.connectFailed'));
    }
    setConnectingAll(false);
  };

  // 전체 연결 끊기
  const [disconnectingAll, setDisconnectingAll] = useState(false);
  const handleDisconnectAll = async () => {
    setDisconnectingAll(true);
    try {
      for (const d of allDevices) {
        if (d.protected) continue;
        if (d.status === 'device' || d.status === 'connected') {
          await deviceApi.disconnectOne(d.id);
        }
      }
      await fetchDevices();
      message.success(t('device.disconnectAllSuccess'));
    } catch {
      message.error(t('device.disconnectFailed'));
    }
    setDisconnectingAll(false);
  };

  // 선택 연결 끊기
  const handleDisconnectSelected = async () => {
    if (selectedDeviceIds.size === 0) { message.warning(t('device.noSelection')); return; }
    setDisconnectingAll(true);
    try {
      for (const id of selectedDeviceIds) {
        const d = allDevices.find(dd => dd.id === id);
        if (d && (d.status === 'device' || d.status === 'connected')) {
          await deviceApi.disconnectOne(id);
        }
      }
      await fetchDevices();
      message.success(t('device.disconnectSelectedSuccess'));
    } catch {
      message.error(t('device.disconnectFailed'));
    }
    setDisconnectingAll(false);
  };

  // 개별 연결
  const handleConnectOne = async (deviceId: string) => {
    setConnectingIds(prev => new Set(prev).add(deviceId));
    try {
      const res = await deviceApi.connectRegistered([deviceId]);
      updateDeviceLists(res.data);
      const result = res.data.results?.find((r: any) => r.device_id === deviceId);
      message.success(result?.message || t('device.connectOneSuccess'));
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('device.connectFailed'));
    }
    setConnectingIds(prev => {
      const next = new Set(prev);
      next.delete(deviceId);
      return next;
    });
  };

  // 체크박스 토글
  const toggleDeviceSelection = (deviceId: string, checked: boolean) => {
    setSelectedDeviceIds(prev => {
      const next = new Set(prev);
      if (checked) next.add(deviceId);
      else next.delete(deviceId);
      return next;
    });
  };

  const toggleSelectAll = (checked: boolean) => {
    if (checked) {
      setSelectedDeviceIds(new Set(allDevices.map(d => d.id)));
    } else {
      setSelectedDeviceIds(new Set());
    }
  };

  // Modal state
  const [modalOpen, setModalOpen] = useState(false);
  const [modalCategory, setModalCategory] = useState<'primary' | 'auxiliary'>('primary');
  const [scanning, setScanning] = useState(false);
  const [scannedAdb, setScannedAdb] = useState<any[]>([]);
  const [scannedSerial, setScannedSerial] = useState<SerialPort[]>([]);
  const [scannedHkmc, setScannedHkmc] = useState<{ ip: string; port: number; raw: string }[]>([]);
  const [scannedIsap, setScannedIsap] = useState<{ ip: string; port: number }[]>([]);
  const [scannedIcas, setScannedIcas] = useState<{ ip: string; port: number }[]>([]);
  const [scannedMib, setScannedMib] = useState<{ ip: string; port: number }[]>([]);
  const [scannedBench, setScannedBench] = useState<{ ip: string; port: number; verified?: boolean }[]>([]);
  const [scannedVision, setScannedVision] = useState<{ id: string; mac: string; model: string; serial: string; vendor: string; tl_type: string; ip: string; subnet?: string; gateway?: string }[]>([]);
  const [scannedWebcams, setScannedWebcams] = useState<{ index: number; label: string; width: number; height: number; already_registered?: boolean; in_use_by_recording?: boolean }[]>([]);
  const [hasScanned, setHasScanned] = useState(false);
  const [scannedDlt, setScannedDlt] = useState<{ ip: string; port: number }[]>([]);
  const [scannedSmartbench, setScannedSmartbench] = useState<{ ip: string; port: number; label: string; module: string }[]>([]);
  const [scannedScar, setScannedScar] = useState<{ ip: string; port: number; container: string; api_alive: boolean; docker_running: boolean; docker_installed?: boolean; interfaces?: string[]; label: string; module: string }[]>([]);
  const [scannedRadmoon, setScannedRadmoon] = useState<{
    bridge: string;
    bridge_operstate: string;
    current_ips: string[];
    members: { interface: string; mac: string; operstate: string }[];
    label: string;
    module: string;
  }[]>([]);
  const [scannedSsh, setScannedSsh] = useState<{ ip: string; port: number }[]>([]);
  const [scannedCustom, setScannedCustom] = useState<{ label: string; hosts: { ip: string; port: number }[] }[]>([]);
  const [pcInterfaces, setPcInterfaces] = useState<{ name: string; ip: string; prefix: number }[]>([]);
  const [forceIpModal, setForceIpModal] = useState<{ mac: string; currentIp: string } | null>(null);
  const [forceIpAddr, setForceIpAddr] = useState('');
  const [forceIpSubnet, setForceIpSubnet] = useState('255.255.255.0');
  const [forceIpGateway, setForceIpGateway] = useState('0.0.0.0');
  const [forceIpLoading, setForceIpLoading] = useState(false);
  const [connectType, setConnectType] = useState<'adb' | 'serial' | 'module' | 'hkmc_agent' | 'isap_agent' | 'icas_agent' | 'mib_agent' | 'vision_camera' | 'webcam' | 'ssh'>('adb');
  // MIB Agent 전용 — 등록 시 입력하는 해상도 ("WxH")
  const MIB_RESOLUTION_PRESETS: { label: string; value: string }[] = [
    { label: '10.0" — 1560x700',                  value: '1560x700'  },
    { label: '10.4" — 1560x878',                  value: '1560x878'  },
    { label: '12.9" — 1920x1080',                 value: '1920x1080' },
    { label: '15.0" — 2240x1260',                 value: '2240x1260' },
    { label: '13.1" — 1920x1080 (SK only)',       value: '1920x1080' },
    { label: '8.0" — 800x480 (mib3oi-gp-mqb)',    value: '800x480'   },
    { label: '9.2" — 1280x640 (mib3oi-gp-mqb)',   value: '1280x640'  },
    { label: '14.6" — 1080x1920 (Ford Portrait)', value: '1080x1920' },
  ];
  const MIB_DEFAULT_RESOLUTION = '1560x700';
  const [mibResolution, setMibResolution] = useState<string>(MIB_DEFAULT_RESOLUTION);
  const [connectAddress, setConnectAddress] = useState('');
  const [baudrate, setBaudrate] = useState(115200);
  const [connecting, setConnecting] = useState(false);
  const [hkmcPort, setHkmcPort] = useState(5000);
  const [sshPort, setSshPort] = useState(22);
  const [sshUser, setSshUser] = useState('');
  const [sshPass, setSshPass] = useState('');
  const [sshKeyFile, setSshKeyFile] = useState('');
  const [modalTabKey, setModalTabKey] = useState('scan');
  const [deviceProject, setDeviceProject] = useState('');
  const [deviceModel, setDeviceModel] = useState('');

  // 프로젝트/모델 콤보는 backend/device_catalog.json 에서 로드 (AdminPage에서 편집)
  interface CatalogModel { value: string; enabled: boolean; agent?: string }
  interface CatalogProject { name: string; enabled: boolean; models: CatalogModel[] }
  interface CatalogAgent { name: string; type: string; enabled: boolean }
  const [catalogProjects, setCatalogProjects] = useState<CatalogProject[]>([]);
  const [moduleVisibility, setModuleVisibility] = useState<Record<string, boolean>>({});
  const [catalogAgents, setCatalogAgents] = useState<CatalogAgent[]>([]);

  useEffect(() => {
    deviceApi.getCatalog().then(res => {
      const data = res.data || {};
      setCatalogProjects(Array.isArray(data.projects) ? data.projects : []);
      setModuleVisibility(data.module_visibility || {});
      setCatalogAgents(Array.isArray(data.agents) ? data.agents : []);
    }).catch(() => {
      setCatalogProjects([]);
      setModuleVisibility({});
      setCatalogAgents([]);
    });
  }, []);

  // 모델 value → agent type 매핑 (수동 연결 탭에서 자동 connect type 설정에 활용)
  const modelAgentType = useMemo(() => {
    const map = new Map<string, string>();
    for (const p of catalogProjects) {
      for (const m of (p.models || [])) {
        if (!m.value || !m.agent) continue;
        const agent = catalogAgents.find(a => a.name === m.agent);
        if (agent?.type) map.set(m.value, agent.type);
      }
    }
    return map;
  }, [catalogProjects, catalogAgents]);

  // 주 디바이스 추가 모달의 프로젝트 콤보 — 전체("") 항목은 의미 없음(특정 프로젝트 선택 강제).
  const PROJECT_OPTIONS = useMemo(() => (
    catalogProjects
      .filter(p => p.enabled !== false && typeof p.name === 'string' && p.name.length > 0)
      .map(p => ({ label: p.name, value: p.name }))
      .sort((a, b) => (a.label || '').localeCompare(b.label || ''))
  ), [catalogProjects]);

  const DEVICE_MODELS = useMemo(() => {
    const enabledProjects = catalogProjects.filter(p => p.enabled !== false);
    const src = deviceProject
      ? enabledProjects.filter(p => p.name === deviceProject)
      : enabledProjects;
    const flat: { label: string; value: string }[] = [];
    for (const p of src) {
      for (const m of (p.models || [])) {
        if (m.enabled === false) continue;
        const v = typeof m.value === 'string' ? m.value : '';
        if (!v) continue; // value 누락 항목 스킵
        flat.push({ label: v, value: v });
      }
    }
    return flat.sort((a, b) => (a.label || '').localeCompare(b.label || ''));
  }, [deviceProject, catalogProjects]);

  const isModuleVisible = useCallback((name?: string) => {
    if (!name) return true;
    return moduleVisibility[name] !== false;
  }, [moduleVisibility]);

  // VisionCamera
  const [vcMac, setVcMac] = useState('');
  const [vcModel, setVcModel] = useState('exo264CGE');
  const [vcSerial, setVcSerial] = useState('');
  const [vcSubnet, setVcSubnet] = useState('255.255.0.0');

  // Webcam
  const [webcamIndex, setWebcamIndex] = useState<number>(0);
  const [webcamWidth, setWebcamWidth] = useState<number>(0);
  const [webcamHeight, setWebcamHeight] = useState<number>(0);

  // Module
  const [modules, setModules] = useState<ModuleInfo[]>([]);
  // 표시 가능한 모듈 목록 (사용자 선택 UI에서 참조 — AdminPage에서 체크 해제 시 숨김)
  const visibleModules = useMemo(() => modules.filter(m => isModuleVisible(m.name)), [modules, isModuleVisible]);
  const [selectedModule, setSelectedModule] = useState<string | undefined>(undefined);
  const [scanSelectedModule, setScanSelectedModule] = useState<string | undefined>(undefined);
  const [extraFieldValues, setExtraFieldValues] = useState<Record<string, any>>({});

  // Edit device modal
  const [editModalOpen, setEditModalOpen] = useState(false);
  const [editDevice, setEditDevice] = useState<ManagedDevice | null>(null);
  const [editName, setEditName] = useState('');
  const [editDeviceId, setEditDeviceId] = useState('');
  const [editBaudrate, setEditBaudrate] = useState(115200);
  const [editModule, setEditModule] = useState<string | undefined>(undefined);
  const [editExtraFields, setEditExtraFields] = useState<Record<string, any>>({});
  const [editSaving, setEditSaving] = useState(false);
  // MIB 수정 모달 전용 — 해상도("WxH") + 자동 감지 로딩 상태
  const [editMibResolution, setEditMibResolution] = useState<string>('');
  const [detectingMibRes, setDetectingMibRes] = useState(false);

  // Scan settings modal
  const [scanSettingsOpen, setScanSettingsOpen] = useState(false);
  type ScanCategory = 'primary' | 'auxiliary';
  // 고정 포트 정의 — 사용자가 변경할 수 없는 빌트인 스캔 포트
  const FIXED_PORTS: Record<string, number[]> = {
    hkmc: [6655, 5000],
    isap: [20000],
    dlt: [3490],
  };
  // bench는 host+port 고정값. 백엔드 설정과 동기화 시 항상 강제 적용.
  const BENCH_DEFAULT_HOST = '192.168.1.101';
  const BENCH_DEFAULT_PORT = 25000;
  // 백엔드에서 받아온 builtin 설정에 고정 포트를 강제 적용
  const applyFixedPorts = (
    b: Record<string, { enabled: boolean; module: string; port?: number; ports?: number[]; host?: string; category?: ScanCategory }>,
  ) => {
    const result = { ...b };
    for (const [key, ports] of Object.entries(FIXED_PORTS)) {
      result[key] = { ...(result[key] || { enabled: true, module: '' }), ports };
    }
    // bench: host 미지정이면 기본 채움 + ports는 더 이상 사용 안 함
    const benchCur = result['bench'] || { enabled: true, module: 'WoohyunBench', category: 'auxiliary' };
    result['bench'] = {
      ...benchCur,
      host: benchCur.host || BENCH_DEFAULT_HOST,
      port: benchCur.port ?? BENCH_DEFAULT_PORT,
      ports: undefined,
    };
    return result;
  };
  const [scanBuiltin, setScanBuiltin] = useState<Record<string, { enabled: boolean; module: string; port?: number; ports?: number[]; host?: string; container?: string; category?: ScanCategory }>>({
    adb: { enabled: true, module: '', category: 'primary' },
    serial: { enabled: true, module: 'SerialLogging', category: 'auxiliary' },
    hkmc: { enabled: true, module: '', ports: [6655, 5000], category: 'primary' },
    isap: { enabled: false, module: '', ports: [20000], category: 'primary' },
    dlt: { enabled: true, module: 'DLTLogging', ports: [3490], category: 'auxiliary' },
    bench: { enabled: true, module: 'WoohyunBench', host: BENCH_DEFAULT_HOST, port: BENCH_DEFAULT_PORT, category: 'auxiliary' },
    vision_camera: { enabled: false, module: 'VisionCamera', category: 'primary' },
    webcam: { enabled: true, module: 'WebcamDevice', category: 'primary' },
    ssh: { enabled: true, module: 'SSHManager', port: 22, category: 'auxiliary' },
    smartbench: { enabled: true, module: 'SmartBench', host: '192.167.0.5', port: 8000, category: 'auxiliary' },
    scar: { enabled: true, module: 'SCAR', host: 'localhost', port: 8081, category: 'auxiliary' },
    radmoon: { enabled: true, module: 'TH', category: 'auxiliary' },
  });
  const [scanCustom, setScanCustom] = useState<{ label: string; type: string; port: number; module: string; enabled: boolean; category?: ScanCategory }[]>([]);

  // 앱 시작 시 스캔 설정 동기화 — device 추가 모달에서 category 기준 필터링 위해 필요
  useEffect(() => {
    deviceApi.getScanSettings().then(res => {
      if (res.data?.builtin) setScanBuiltin(prev => applyFixedPorts({ ...prev, ...res.data.builtin }));
      if (Array.isArray(res.data?.custom)) setScanCustom(res.data.custom);
    }).catch(() => { /* 실패 시 프론트 기본값 유지 */ });
  }, []);

  // key → category 해석 (값 없으면 기본 정책 적용)
  const _defaultCategoryForKey = (key: string): ScanCategory => {
    const primaryKeys = new Set(['adb', 'hkmc', 'isap', 'icas', 'mib', 'vision_camera', 'webcam']);
    return primaryKeys.has(key) ? 'primary' : 'auxiliary';
  };
  const scanItemCategory = (key: string): ScanCategory =>
    (scanBuiltin[key]?.category as ScanCategory) || _defaultCategoryForKey(key);
  const [newCustomLabel, setNewCustomLabel] = useState('');
  const [newCustomPort, setNewCustomPort] = useState<number | null>(null);
  const [newCustomType, setNewCustomType] = useState<string>('tcp');
  const [newCustomModule, setNewCustomModule] = useState<string>('');
  const [newCustomCategory, setNewCustomCategory] = useState<ScanCategory>('auxiliary');

  const getModuleInfo = (moduleName?: string): ModuleInfo | undefined => {
    if (!moduleName) return undefined;
    return modules.find(m => m.name === moduleName);
  };

  const getModuleConnectType = (moduleName?: string) => {
    return getModuleInfo(moduleName)?.connect_type;
  };

  const getModuleConnectFields = (moduleName?: string): ConnectField[] => {
    return getModuleInfo(moduleName)?.connect_fields || [];
  };

  const handleDisconnect = async (deviceId: string) => {
    try {
      const prefix = getDevicePrefix(deviceId);
      const result = await disconnectDevice(deviceId);
      message.info(result);
      // 삭제 후 같은 그룹 디바이스 번호 재정렬
      await fetchDevices();
      // fetchDevices 후 최신 목록에서 같은 prefix 디바이스 추출
      const remaining = [...primaryDevices, ...auxiliaryDevices]
        .filter(d => d.id !== deviceId && getDevicePrefix(d.id) === prefix)
        .sort((a, b) => {
          const na = parseInt(a.id.match(/_(\d+)$/)?.[1] || '0');
          const nb = parseInt(b.id.match(/_(\d+)$/)?.[1] || '0');
          return na - nb;
        });
      if (remaining.length > 0) {
        try {
          const res = await deviceApi.reorderDevices(prefix, remaining.map(d => d.id));
          updateDeviceLists(res.data);
        } catch { /* 재정렬 실패해도 삭제는 완료 */ }
      }
    } catch {
      message.error(t('device.disconnectFailed'));
    }
  };

  const closeAddModal = () => {
    setModalOpen(false);
    resumeDevicePolling();
  };

  const openScanSettings = async () => {
    // 모듈 목록 로드 (커스텀 스캔에서 모듈 선택용)
    try {
      const modRes = await deviceApi.listModules();
      setModules((modRes.data.modules || []).sort((a: ModuleInfo, b: ModuleInfo) => (a.label || a.name || '').localeCompare(b.label || b.name || '')));
    } catch { /* ignore */ }
    try {
      const res = await deviceApi.getScanSettings();
      setScanBuiltin(applyFixedPorts(res.data.builtin || {}));
      setScanCustom(res.data.custom || []);
    } catch { /* use defaults */ }
    setScanSettingsOpen(true);
  };

  const saveScanSettings = async () => {
    const settings = { builtin: applyFixedPorts(scanBuiltin), custom: scanCustom };
    try {
      await deviceApi.saveScanSettings(settings);
      message.success(t('common.saved'));
    } catch { message.error(t('common.saveFailed')); }
    setScanSettingsOpen(false);
  };

  const addCustomScan = () => {
    if (!newCustomPort) return;
    setScanCustom([...scanCustom, {
      label: newCustomLabel || `${newCustomType.toUpperCase()}:${newCustomPort}`,
      type: newCustomType,
      port: newCustomPort,
      module: newCustomModule,
      enabled: true,
      category: newCustomCategory,
    }]);
    setNewCustomLabel('');
    setNewCustomPort(null);
    setNewCustomModule('');
    setNewCustomCategory('auxiliary');
  };

  const openAddModal = (category: 'primary' | 'auxiliary') => {
    pauseDevicePolling();
    setModalCategory(category);
    setConnectType(category === 'primary' ? 'adb' : 'serial');
    setSelectedModule(undefined);
    setScanSelectedModule(undefined);
    setExtraFieldValues({});
    setDeviceProject('');
    setDeviceModel('');
    setModalTabKey('scan');
    setModalOpen(true);
    // 네트워크 스캔은 사용자가 명시적으로 버튼을 눌렀을 때만 수행 (IDS 오탐 방지)
    // 이전 스캔 결과를 초기화해서 stale 결과가 보이지 않도록 함
    setScannedAdb([]);
    setScannedSerial([]);
    setScannedHkmc([]);
    setScannedIsap([]);
    setScannedIcas([]);
    setScannedMib([]);
    setScannedBench([]);
    setScannedVision([]);
    setScannedWebcams([]);
    setScannedDlt([]);
    setScannedSmartbench([]);
    setScannedScar([]);
    setScannedRadmoon([]);
    setScannedSsh([]);
    setScannedCustom([]);
    setHasScanned(false);
    if (category === 'auxiliary') {
      deviceApi.listModules().then(res => setModules((res.data.modules || []).sort((a: ModuleInfo, b: ModuleInfo) => (a.label || a.name || '').localeCompare(b.label || b.name || '')))).catch(() => {});
    }
  };

  const handleScan = async () => {
    setScanning(true);
    try {
      const [res, ifRes] = await Promise.all([deviceApi.scan(), deviceApi.localInterfaces()]);
      setScannedAdb(res.data.adb_devices || []);
      setScannedSerial(res.data.serial_ports || []);
      setScannedHkmc(res.data.hkmc_devices || []);
      setScannedIsap(res.data.isap_hosts || []);
      setScannedIcas(res.data.icas_hosts || []);
      setScannedMib(res.data.mib_hosts || []);
      setScannedBench(res.data.bench_devices || []);
      setScannedVision(res.data.vision_cameras || []);
      setScannedWebcams(res.data.webcams || []);
      setScannedDlt(res.data.dlt_devices || []);
      setScannedSmartbench(res.data.smartbench_devices || []);
      setScannedScar(res.data.scar_devices || []);
      setScannedRadmoon(res.data.radmoon_devices || []);
      setScannedSsh(res.data.ssh_hosts || []);
      setScannedCustom(res.data.custom_results || []);
      setPcInterfaces(ifRes.data.interfaces || []);
      setHasScanned(true);
    } catch {
      message.error(t('device.scanFailed'));
    }
    setScanning(false);
  };

  const handleConnect = async () => {
    // 주 디바이스는 프로젝트·모델 필수
    if (!ensurePrimaryProjectModel()) return;
    const moduleConnType = getModuleConnectType(selectedModule);
    const fields = getModuleConnectFields(selectedModule);

    // SSH 전용 처리
    if (connectType === 'ssh') {
      if (!connectAddress.trim()) { message.warning(t('device.sshHostPlaceholder')); return; }
      if (!sshUser.trim()) { message.warning(t('device.sshUserPlaceholder')); return; }
      // 비밀번호는 선택 — 일부 디바이스는 비밀번호 없이 접속 가능 (e.g. root/empty)
      setConnecting(true);
      try {
        const extra = {
          username: sshUser.trim(),
          password: sshPass,
          key_file_path: sshKeyFile.trim(),
        };
        const result = await connectDevice(
          'ssh', connectAddress.trim(), undefined, '', modalCategory,
          undefined, 'ssh', extra, '', sshPort,
        );
        message.success(result);
        setConnectAddress('');
        setSshUser('');
        setSshPass('');
        setSshKeyFile('');
        closeAddModal();
      } catch (e: any) {
        message.error(e.response?.data?.detail || t('device.connectFailed'));
      }
      setConnecting(false);
      return;
    }

    // VisionCamera 전용 처리
    if (connectType === 'vision_camera') {
      if (!vcMac.trim()) {
        message.warning('MAC Address is required');
        return;
      }
      setConnecting(true);
      try {
        const extra = {
          mac: vcMac.trim(),
          model: vcModel.trim(),
          serial: vcSerial.trim(),
          subnetmask: vcSubnet.trim(),
        };
        const result = await connectDevice('vision_camera', connectAddress.trim(), undefined, '', 'primary', undefined, 'vision_camera', extra);
        message.success(result);
        setConnectAddress('');
        setVcMac('');
        closeAddModal();
      } catch (e: any) {
        message.error(e.response?.data?.detail || t('device.connectFailed'));
      }
      setConnecting(false);
      return;
    }

    // Webcam 전용 처리
    if (connectType === 'webcam') {
      setConnecting(true);
      try {
        const extra = {
          device_index: webcamIndex,
          width: webcamWidth,
          height: webcamHeight,
        };
        const result = await connectDevice('webcam', String(webcamIndex), undefined, '', 'primary', undefined, 'webcam', extra);
        message.success(result);
        closeAddModal();
      } catch (e: any) {
        message.error(e.response?.data?.detail || t('device.connectFailed'));
      }
      setConnecting(false);
      return;
    }

    if (moduleConnType !== 'none' && moduleConnType !== 'can' && !connectAddress.trim()) {
      message.warning(t('device.addressPlaceholder'));
      return;
    }
    setConnecting(true);
    try {
      let devType: string = connectType;
      if (selectedModule && (moduleConnType === 'socket' || moduleConnType === 'none' || moduleConnType === 'can')) {
        devType = 'module';
      }
      // Build extra_fields from connect_fields
      let extra: Record<string, any> | undefined = undefined;
      if (fields.length > 0) {
        extra = {};
        for (const f of fields) {
          if (f.type === 'object_list') {
            // object_list는 array 자체를 그대로 전송 (axios가 JSON 직렬화)
            const v = extraFieldValues[f.name];
            extra[f.name] = Array.isArray(v) ? v : (f.default_items ?? []);
          } else {
            extra[f.name] = extraFieldValues[f.name] ?? f.default ?? '';
          }
        }
      }
      const tcpPort = (devType === 'hkmc_agent' || devType === 'isap_agent' || devType === 'icas_agent' || devType === 'mib_agent') ? hkmcPort : undefined;
      const model = (devType === 'adb' || devType === 'hkmc_agent' || devType === 'isap_agent' || devType === 'icas_agent' || devType === 'mib_agent') ? (deviceModel || undefined) : undefined;
      // ICAS Agent는 SSH 자격증명이 필요 — extra_fields로 전달
      if (devType === 'icas_agent') {
        extra = extra || {};
        extra.username = (sshUser && sshUser.trim()) || 'root';
        extra.password = sshPass || '';
        extra.resolution = '1560x700';
      }
      // MIB Agent도 SSH 자격증명 + 해상도 (사용자 지정 가능)
      if (devType === 'mib_agent') {
        extra = extra || {};
        extra.username = (sshUser && sshUser.trim()) || 'root';
        extra.password = sshPass || '';
        extra.resolution = (mibResolution || MIB_DEFAULT_RESOLUTION).trim();
      }
      // HKMC Agent: 클러스터 캡처는 항상 QNX SSH+screenshot+SCP (legacy CLU_IMG_GET 호환).
      // 자격증명을 비워두면 ICAS QNX 패턴(root/빈 패스워드) 자동 사용. SSH 실패 시 backend가 TCP CMD_GETIMG로 폴백.
      if (devType === 'hkmc_agent') {
        extra = extra || {};
        extra.ssh_username = (sshUser && sshUser.trim()) || 'root';
        extra.ssh_password = sshPass || '';
        extra.ssh_port = sshPort || 22;
        extra.cluster_resolution = '2720x720';
        extra.cluster_display = '1';
      }
      const result = await connectDevice(devType, connectAddress.trim(), baudrate, '', modalCategory, selectedModule, moduleConnType, extra, '', tcpPort, model);
      message.success(result);
      setConnectAddress('');
      setExtraFieldValues({});
      closeAddModal();
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('device.connectFailed'));
    }
    setConnecting(false);
  };

  // 주 디바이스 등록 시 프로젝트·모델 필수 선택 여부 확인
  const primaryProjectModelMissing = modalCategory === 'primary' && (!deviceProject || !deviceModel);
  const ensurePrimaryProjectModel = (): boolean => {
    if (primaryProjectModelMissing) {
      message.warning('주 디바이스 추가 시 프로젝트와 장비 모델을 먼저 선택하세요.');
      return false;
    }
    return true;
  };

  // 스캔 결과에서 이미 등록된 디바이스 검출
  const findExisting = (predicate: (d: ManagedDevice) => boolean): ManagedDevice | undefined =>
    allDevices.find(predicate);

  // 제거 후 다시 추가: 기존 등록을 완전히 제거하고 새 add 액션을 실행.
  // disconnectDevice는 백엔드 /device/disconnect를 호출해 등록 자체를 제거함.
  const handleRemoveAndAdd = async (existingId: string, addAction: () => Promise<void> | void) => {
    try {
      await disconnectDevice(existingId);
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('device.disconnectFailed'));
      return;
    }
    await addAction();
  };

  // 스캔 결과의 액션 셀 — 등록 여부에 따라 [추가] 또는 [추가됨 + 제거 후 연결] 렌더링.
  const renderScanAction = (
    existing: ManagedDevice | undefined,
    addLabel: string,
    doAdd: () => Promise<void> | void,
    opts?: { disabled?: boolean; title?: string },
  ) => {
    if (existing) {
      return (
        <Space size={4}>
          <Tag color="success">{t('device.alreadyAdded')}</Tag>
          <Button size="small" danger loading={connecting}
            onClick={() => handleRemoveAndAdd(existing.id, doAdd)}>
            {t('device.removeAndConnect')}
          </Button>
        </Space>
      );
    }
    return (
      <Button size="small" type="primary" loading={connecting}
        disabled={opts?.disabled} title={opts?.title}
        onClick={doAdd}>
        {addLabel}
      </Button>
    );
  };

  const handleAddSerial = async (port: string, description: string) => {
    if (!ensurePrimaryProjectModel()) return;
    setConnecting(true);
    try {
      // STM Virtual COM Port는 어떤 탭에서 등록하든 CANAT/115200 고정
      const isCanat = (description || '').includes('STMicroelectronics Virtual COM Port');
      const effectiveModule = isCanat ? 'CANAT' : scanSelectedModule;
      const effectiveBaudrate = isCanat ? 115200 : baudrate;
      const scanModuleConnType = getModuleConnectType(effectiveModule);
      const result = await connectDevice('serial', port, effectiveBaudrate, description, modalCategory, effectiveModule, scanModuleConnType);
      message.success(result);
      closeAddModal();
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('device.connectFailed'));
    }
    setConnecting(false);
  };

  const handleAddAdb = async (serial: string) => {
    if (!ensurePrimaryProjectModel()) return;
    setConnecting(true);
    try {
      const result = await connectDevice('adb', serial, undefined, '', 'primary', undefined, undefined, undefined, '', undefined, deviceModel || undefined);
      message.success(result);
      closeAddModal();
    } catch (e: any) {
      await fetchDevices();
      closeAddModal();
    }
    setConnecting(false);
  };

  const handleAddHkmc = async (ip: string, port: number) => {
    if (!ensurePrimaryProjectModel()) return;
    setConnecting(true);
    try {
      // 모델명에 "Gen5"가 포함되면 HKMC 5th Wide 프로토콜 — hkmc_agent와 다른 service.
      // (Gen6/cc* 등 다른 모델은 기존 hkmc_agent 유지)
      const isGen5 = /gen5/i.test(deviceModel || '');
      const devType: 'hkmc_agent' | 'hkmc5th_wide_agent' = isGen5 ? 'hkmc5th_wide_agent' : 'hkmc_agent';
      const result = await connectDevice(devType, ip, undefined, '', 'primary', undefined, undefined, undefined, '', port, deviceModel || undefined);
      message.success(result);
      closeAddModal();
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('device.connectFailed'));
    }
    setConnecting(false);
  };

  const handleAddIsap = async (ip: string, port: number) => {
    if (!ensurePrimaryProjectModel()) return;
    setConnecting(true);
    try {
      const result = await connectDevice('isap_agent', ip, undefined, '', 'primary', undefined, undefined, undefined, '', port, deviceModel || undefined);
      message.success(result);
      closeAddModal();
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('device.connectFailed'));
    }
    setConnecting(false);
  };

  // SSH 기반 주 디바이스(ICAS/MIB) 즉시 등록·연결 — scan 결과 클릭 시 사용.
  // 다른 주 디바이스 핸들러와 동일하게 프로젝트·모델 선택을 강제한다.
  // SSH 자격증명은 입력값 또는 root/(공백) 기본값.
  const handleAddSshAgent = async (
    devType: 'icas_agent' | 'mib_agent',
    ip: string,
    port: number,
  ) => {
    if (!ensurePrimaryProjectModel()) return;
    setConnecting(true);
    try {
      const extra: Record<string, any> = {
        username: (sshUser && sshUser.trim()) || 'root',
        password: sshPass || '',
      };
      if (devType === 'mib_agent') {
        extra.resolution = (mibResolution || MIB_DEFAULT_RESOLUTION).trim();
      } else {
        // ICAS Agent — 모델별 기본 해상도 (ICAS3 CN: 2240x1260, 기존 ICAS EU: 1560x700)
        const _model = (deviceModel || '').toUpperCase();
        extra.resolution = _model.includes('ICAS3') ? '2240x1260' : '1560x700';
      }
      const result = await connectDevice(
        devType, ip, undefined, '', 'primary',
        undefined, undefined, extra, '', port,
        deviceModel || undefined,
      );
      message.success(result);
      closeAddModal();
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('device.connectFailed'));
    }
    setConnecting(false);
  };

  const handleAddBench = async (ip: string, port: number) => {
    const moduleName = scanSelectedModule || 'WoohyunBench';
    setConnecting(true);
    try {
      const extra = { udp_port: port };
      const result = await connectDevice('module', ip, undefined, '', 'auxiliary', moduleName, 'socket', extra);
      message.success(result);
      closeAddModal();
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('device.connectFailed'));
    }
    setConnecting(false);
  };

  // --- Edit device ---
  const openEditModal = async (dev: ManagedDevice) => {
    let mods = modules;
    if (mods.length === 0) {
      try {
        const res = await deviceApi.listModules();
        mods = (res.data.modules || []).sort((a: ModuleInfo, b: ModuleInfo) => (a.label || a.name || '').localeCompare(b.label || b.name || ''));
        setModules(mods);
      } catch { /* ignore */ }
    }
    setEditDevice(dev);
    setEditDeviceId(dev.id);
    setEditName(dev.name);
    setEditBaudrate(dev.info?.baudrate || 115200);
    setEditModule(dev.info?.module);
    const extras: Record<string, any> = {};
    const modInfo = mods.find(m => m.name === dev.info?.module);
    if (modInfo?.connect_fields) {
      for (const f of modInfo.connect_fields) {
        extras[f.name] = dev.info?.[f.name] ?? f.default ?? '';
      }
    }
    setEditExtraFields(extras);
    // MIB 해상도 초기값: resolution_str → dict → 기본값 순. 다른 type은 빈 문자열.
    if (dev.type === 'mib_agent') {
      const rs = dev.info?.resolution_str;
      const r = dev.info?.resolution;
      if (typeof rs === 'string' && rs.trim()) {
        setEditMibResolution(rs.trim());
      } else if (r && typeof r === 'object' && r.width && r.height) {
        setEditMibResolution(`${r.width}x${r.height}`);
      } else {
        setEditMibResolution(MIB_DEFAULT_RESOLUTION);
      }
    } else {
      setEditMibResolution('');
    }
    setEditModalOpen(true);
  };

  // MIB 해상도 자동 감지 — 1회 캡처 후 PNG 실제 크기로 dev.info 업데이트.
  const handleDetectMibResolution = async () => {
    if (!editDevice) return;
    setDetectingMibRes(true);
    try {
      const res = await deviceApi.detectMibResolution(editDevice.id);
      const w = res.data?.width;
      const h = res.data?.height;
      const wxh = res.data?.resolution_str || (w && h ? `${w}x${h}` : '');
      if (wxh) {
        setEditMibResolution(wxh);
        message.success(`해상도 자동 감지: ${wxh}`);
      } else {
        message.warning('해상도 응답 형식이 올바르지 않습니다');
      }
      await fetchDevices();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '자동 감지 실패 — 디바이스 연결 상태를 확인하세요');
    }
    setDetectingMibRes(false);
  };

  const handleSaveEdit = async () => {
    if (!editDevice) return;
    setEditSaving(true);
    try {
      const updates: Record<string, any> = {};
      // Device ID (prefix) 변경
      const oldPrefix = getDevicePrefix(editDevice.id);
      const newPrefix = getDevicePrefix(editDeviceId);
      if (editDeviceId !== editDevice.id && newPrefix !== oldPrefix) {
        // 새 그룹의 마지막 번호 + 1
        const samePrefix = allDevices.filter(d => getDevicePrefix(d.id) === newPrefix && d.id !== editDevice.id);
        const maxNum = samePrefix.reduce((max, d) => {
          const n = parseInt(d.id.match(/_(\d+)$/)?.[1] || '0');
          return Math.max(max, n);
        }, 0);
        updates.new_device_id = `${newPrefix}_${maxNum + 1}`;
      } else if (editDeviceId !== editDevice.id) {
        updates.new_device_id = editDeviceId;
      }
      if (editName !== editDevice.name) updates.name = editName;
      if (editBaudrate !== (editDevice.info?.baudrate || 115200)) updates.baudrate = editBaudrate;
      if (editModule !== editDevice.info?.module) {
        updates.module = editModule;
        const ct = getModuleConnectType(editModule);
        if (ct) updates.connect_type = ct;
      }
      if (Object.keys(editExtraFields).length > 0) {
        updates.extra_fields = editExtraFields;
      }
      // MIB 해상도 변경: 기존 resolution_str과 비교, 다르면 extra_fields.resolution으로 전달.
      if (editDevice.type === 'mib_agent') {
        const newRes = (editMibResolution || '').trim();
        const oldRes = (editDevice.info?.resolution_str || '').trim()
          || (editDevice.info?.resolution
            ? `${editDevice.info.resolution.width}x${editDevice.info.resolution.height}`
            : '');
        if (newRes && newRes !== oldRes) {
          updates.extra_fields = { ...(updates.extra_fields || {}), resolution: newRes };
        }
      }
      await deviceApi.updateDevice(editDevice.id, updates);
      message.success(t('device.editSuccess'));
      setEditModalOpen(false);
      await fetchDevices();
      // 기존 그룹 번호 재정렬
      if (updates.new_device_id && newPrefix !== oldPrefix) {
        const oldGroup = [...primaryDevices, ...auxiliaryDevices]
          .filter(d => d.id !== editDevice.id && getDevicePrefix(d.id) === oldPrefix)
          .sort((a, b) => parseInt(a.id.match(/_(\d+)$/)?.[1] || '0') - parseInt(b.id.match(/_(\d+)$/)?.[1] || '0'));
        if (oldGroup.length > 0) {
          try {
            const res = await deviceApi.reorderDevices(oldPrefix, oldGroup.map(d => d.id));
            updateDeviceLists(res.data);
          } catch { /* ignore */ }
        }
      }
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('device.editFailed'));
    }
    setEditSaving(false);
  };


  const isDeviceConnected = (d: ManagedDevice) => d.status === 'device' || d.status === 'connected';

  const getStatusLabel = (status: string) => {
    switch (status) {
      case 'device': case 'connected': return t('device.statusConnected');
      case 'reconnecting': return t('device.statusConnecting');
      case 'disconnected': case 'unknown': return t('device.statusDisconnected');
      case 'offline': return t('device.statusOffline');
      case 'error': return t('device.statusError');
      default: return status;
    }
  };
  const getStatusColor = (status: string) => {
    switch (status) {
      case 'device': case 'connected': return 'green';
      case 'reconnecting': return 'processing';
      case 'disconnected': case 'unknown': return 'default';
      case 'offline': case 'error': return 'red';
      default: return 'orange';
    }
  };

  const [disconnectingIds, setDisconnectingIds] = useState<Set<string>>(new Set());

  const handleDisconnectOne = async (deviceId: string) => {
    setDisconnectingIds(prev => new Set(prev).add(deviceId));
    try {
      const res = await deviceApi.disconnectOne(deviceId);
      updateDeviceLists(res.data);
      message.info(res.data.result || t('device.disconnectOneSuccess'));
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('device.disconnectFailed'));
    }
    setDisconnectingIds(prev => { const next = new Set(prev); next.delete(deviceId); return next; });
  };

  // ── 그룹화 + DnD ──
  const dndSensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }));

  // 디바이스를 prefix 기준으로 그룹화, 번호순 정렬
  const deviceGroups = useMemo(() => {
    const groups: Record<string, ManagedDevice[]> = {};
    for (const d of allDevices) {
      const prefix = getDevicePrefix(d.id);
      if (!groups[prefix]) groups[prefix] = [];
      groups[prefix].push(d);
    }
    // 각 그룹 내 번호순 정렬
    for (const arr of Object.values(groups)) {
      arr.sort((a, b) => {
        const na = parseInt(a.id.match(/_(\d+)$/)?.[1] || '0');
        const nb = parseInt(b.id.match(/_(\d+)$/)?.[1] || '0');
        return na - nb;
      });
    }
    return groups;
  }, [allDevices]);

  const groupOrder = useMemo(() => {
    // primary 그룹(Android, HKMC, VisionCam) 우선, 나머지는 알파벳
    const primary = ['Android', 'HKMC', 'iSAP', 'VisionCam', 'Webcam'];
    const keys = Object.keys(deviceGroups);
    const first = primary.filter(k => keys.includes(k));
    const rest = keys.filter(k => !primary.includes(k)).sort();
    return [...first, ...rest];
  }, [deviceGroups]);

  // 주/보조 그룹 분리 — 카테고리 명확 구분 + Common은 보조 최하단 고정.
  // 그룹의 카테고리는 첫 디바이스 기준 (같은 prefix 그룹 내 디바이스는 같은 category 가정).
  const primaryGroupOrder = useMemo(() => {
    return groupOrder.filter(prefix => {
      const g = deviceGroups[prefix];
      return g && g.length > 0 && g[0].category === 'primary';
    });
  }, [groupOrder, deviceGroups]);

  const auxiliaryGroupOrder = useMemo(() => {
    const aux = groupOrder.filter(prefix => {
      const g = deviceGroups[prefix];
      return g && g.length > 0 && g[0].category === 'auxiliary';
    });
    // Common 그룹은 항상 보조 최하단
    const withoutCommon = aux.filter(p => p !== 'Common');
    return aux.includes('Common') ? [...withoutCommon, 'Common'] : withoutCommon;
  }, [groupOrder, deviceGroups]);

  const handleGroupDragEnd = async (prefix: string, event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const group = deviceGroups[prefix];
    if (!group) return;
    const oldIdx = group.findIndex(d => d.id === active.id);
    const newIdx = group.findIndex(d => d.id === over.id);
    if (oldIdx < 0 || newIdx < 0) return;
    // 순서 변경 후 API 호출
    const reordered = [...group];
    const [moved] = reordered.splice(oldIdx, 1);
    reordered.splice(newIdx, 0, moved);
    try {
      const res = await deviceApi.reorderDevices(prefix, reordered.map(d => d.id));
      updateDeviceLists(res.data);
    } catch (e: any) {
      message.error(e.response?.data?.detail || 'Reorder failed');
    }
  };

  const renderDeviceRow = (d: ManagedDevice, extraModuleTags?: string[]) => (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flex: 1, minWidth: 0 }}>
      <Checkbox
        checked={selectedDeviceIds.has(d.id)}
        onChange={(e) => toggleDeviceSelection(d.id, e.target.checked)}
        disabled={d.protected}
        style={{ flexShrink: 0, visibility: d.protected ? 'hidden' : 'visible' }}
      />
      <Tag color={getStatusColor(d.status)} style={{ flexShrink: 0 }}>
        {getStatusLabel(d.status)}
      </Tag>
      {/* wincontrol 디바이스는 OS 별 표시명(LinuxControl/WinControl) 사용 — ID 는 시나리오 호환 위해 "WinControl" 고정. */}
      <span style={{ fontWeight: 500, flexShrink: 0 }}>{d.type === 'wincontrol' && d.name ? d.name : d.id}</span>
      {d.protected && <Tag color="gold" style={{ flexShrink: 0 }}>SYSTEM</Tag>}
      <span style={{ color: '#aaa', fontSize: 11, flexShrink: 0 }}>{d.address}</span>
      {d.info?.module && <Tag color="cyan" style={{ flexShrink: 0 }}>{d.info.module}</Tag>}
      {extraModuleTags?.map(tag => <Tag key={tag} color="cyan" style={{ flexShrink: 0 }}>{tag}</Tag>)}
      {d.info?.baudrate && <Tag style={{ flexShrink: 0 }}>{d.info.baudrate}</Tag>}
      {d.info?.resolution && <Tag style={{ flexShrink: 0 }}>{d.info.resolution.width}x{d.info.resolution.height}</Tag>}
      <div style={{ marginLeft: 'auto', display: 'flex', gap: 3, flexShrink: 0 }}>
        {d.type === 'wincontrol' ? (
          // WinControl: 삭제·편집은 금지(protected)지만 연결/해제는 사용자가 토글.
          isDeviceConnected(d) ? (
            <Button size="small" icon={<DisconnectOutlined />} loading={disconnectingIds.has(d.id)}
              onClick={() => handleDisconnectOne(d.id)}>{t('device.disconnectOne')}</Button>
          ) : (
            <Button size="small" type="primary" icon={<LinkOutlined />} loading={connectingIds.has(d.id)}
              onClick={() => handleConnectOne(d.id)}>{t('device.connectOne')}</Button>
          )
        ) : d.protected ? null : (
          <>
            {isDeviceConnected(d) ? (
              <Button size="small" icon={<DisconnectOutlined />} loading={disconnectingIds.has(d.id)}
                onClick={() => handleDisconnectOne(d.id)}>{t('device.disconnectOne')}</Button>
            ) : (
              <Button size="small" type="primary" icon={<LinkOutlined />} loading={connectingIds.has(d.id)}
                onClick={() => handleConnectOne(d.id)}>{t('device.connectOne')}</Button>
            )}
            <Button size="small" icon={<EditOutlined />} onClick={() => openEditModal(d)} />
            <Button size="small" danger icon={<DeleteOutlined />} onClick={() => handleDisconnect(d.id)} />
          </>
        )}
      </div>
    </div>
  );

  // object_list: 외부에서 들어온 값을 항상 array of records로 정규화
  const normalizeObjectListValue = (raw: any, f: ConnectField): Record<string, any>[] => {
    if (Array.isArray(raw)) return raw;
    if (typeof raw === 'string' && raw.trim()) {
      // JSON 또는 Python repr 양쪽 다 시도 (단일 따옴표/None/True/False 치환)
      try {
        return JSON.parse(raw);
      } catch {
        try {
          const pyToJson = raw
            .replace(/'/g, '"')
            .replace(/\bNone\b/g, 'null')
            .replace(/\bTrue\b/g, 'true')
            .replace(/\bFalse\b/g, 'false');
          return JSON.parse(pyToJson);
        } catch {
          // ignore
        }
      }
    }
    return f.default_items ? JSON.parse(JSON.stringify(f.default_items)) : [];
  };

  // object_list의 각 row를 sub-field로 렌더링
  const renderObjectList = (f: ConnectField, values: Record<string, any>, onChange: (vals: Record<string, any>) => void) => {
    const items = normalizeObjectListValue(values[f.name], f);
    const itemFields = f.item_fields || [];

    const update = (newItems: Record<string, any>[]) => {
      onChange({ ...values, [f.name]: newItems });
    };

    const addItem = () => {
      // 새 row의 기본값: 첫 번째 default_item을 복제하거나, item_fields의 default 모음
      const proto = f.default_items?.[0]
        ? { ...f.default_items[0] }
        : Object.fromEntries(itemFields.map(sf => [sf.name, sf.default ?? '']));
      update([...items, proto]);
    };

    const removeItem = (idx: number) => {
      update(items.filter((_, i) => i !== idx));
    };

    const updateItem = (idx: number, key: string, val: any) => {
      const next = items.map((it, i) => (i === idx ? { ...it, [key]: val } : it));
      update(next);
    };

    return (
      <div style={{ border: '1px solid #e0e0e0', borderRadius: 4, padding: 6, background: '#fafafa' }}>
        {items.length === 0 && (
          <div style={{ fontSize: 11, color: '#999', marginBottom: 4 }}>채널 없음 — 추가 버튼으로 등록하세요</div>
        )}
        {items.map((item, idx) => (
          <div key={idx} style={{
            display: 'grid',
            gridTemplateColumns: `repeat(${itemFields.length}, minmax(0, 1fr)) auto`,
            gap: 4,
            alignItems: 'end',
            marginBottom: 4,
            padding: 4,
            background: '#fff',
            border: '1px solid #eee',
            borderRadius: 3,
          }}>
            {itemFields.map(sf => (
              <div key={sf.name} style={{ minWidth: 0 }}>
                <div style={{ fontSize: 10, color: '#888' }}>{sf.label}</div>
                {sf.type === 'select' && sf.options ? (
                  <Select
                    size="small"
                    style={{ width: '100%' }}
                    value={String(item[sf.name] ?? sf.default ?? '')}
                    onChange={(v) => updateItem(idx, sf.name, v)}
                  >
                    {sf.options.map(o => <Option key={o} value={o}>{o}</Option>)}
                  </Select>
                ) : sf.type === 'number' ? (
                  <InputNumber
                    size="small"
                    style={{ width: '100%' }}
                    value={item[sf.name] ?? (sf.default ? Number(sf.default) : undefined)}
                    onChange={(v) => updateItem(idx, sf.name, v)}
                  />
                ) : (
                  <Input
                    size="small"
                    value={item[sf.name] ?? sf.default ?? ''}
                    onChange={(e) => updateItem(idx, sf.name, e.target.value)}
                  />
                )}
              </div>
            ))}
            <Button
              size="small"
              danger
              icon={<DeleteOutlined />}
              onClick={() => removeItem(idx)}
              title="이 행 제거"
            />
          </div>
        ))}
        <Button size="small" icon={<PlusOutlined />} onClick={addItem} style={{ marginTop: 2 }}>
          채널 추가
        </Button>
      </div>
    );
  };

  // Render dynamic connect_fields inputs — hidden=true 인 필드는 폼에서 제외 (extra_fields 에는 default 가 들어감).
  const renderConnectFields = (fields: ConnectField[], values: Record<string, any>, onChange: (vals: Record<string, any>) => void) => {
    return fields.filter(f => !f.hidden).map(f => (
      <div key={f.name} style={{ marginBottom: 3 }}>
        <span style={{ fontSize: 11, color: '#888', marginRight: 6 }}>{f.label}:</span>
        {f.type === 'object_list' ? (
          renderObjectList(f, values, onChange)
        ) : f.type === 'select' && f.options ? (
          <Select
            style={{ width: '100%' }}
            value={values[f.name] ?? f.default}
            onChange={(v) => onChange({ ...values, [f.name]: v })}
          >
            {f.options.map(o => <Option key={o} value={o}>{o}</Option>)}
          </Select>
        ) : f.type === 'number' ? (
          <InputNumber
            style={{ width: '100%' }}
            value={values[f.name] ?? (f.default ? Number(f.default) : undefined)}
            onChange={(v) => onChange({ ...values, [f.name]: v })}
          />
        ) : f.type === 'password' ? (
          <Input.Password
            value={values[f.name] ?? f.default ?? ''}
            onChange={(e) => onChange({ ...values, [f.name]: e.target.value })}
            autoComplete="new-password"
          />
        ) : f.type === 'folder' ? (
          <Space.Compact style={{ width: '100%' }}>
            <Input
              value={values[f.name] ?? f.default ?? ''}
              onChange={(e) => onChange({ ...values, [f.name]: e.target.value })}
              placeholder="폴더 경로 (또는 찾아보기로 선택)"
            />
            <Button
              icon={<FolderOpenOutlined />}
              onClick={async () => {
                try {
                  const initial = values[f.name] || f.default || '';
                  const picked = await browseFolder(initial);
                  if (picked) onChange({ ...values, [f.name]: picked });
                } catch (e: any) {
                  message.error(e?.response?.data?.detail || '폴더 선택 실패');
                }
              }}
            >
              찾아보기
            </Button>
          </Space.Compact>
        ) : (
          <Input
            value={values[f.name] ?? f.default ?? ''}
            onChange={(e) => onChange({ ...values, [f.name]: e.target.value })}
          />
        )}
      </div>
    ));
  };

  const serialColumns = [
    { title: t('device.port'), dataIndex: 'port', key: 'port', render: (v: string) => <Tag color="blue">{v}</Tag> },
    { title: t('common.description'), dataIndex: 'description', key: 'description' },
    { title: t('device.manufacturer'), dataIndex: 'manufacturer', key: 'manufacturer' },
    { title: 'VID:PID', key: 'vidpid', render: (_: any, r: SerialPort) => r.vid ? `${r.vid}:${r.pid}` : '-' },
    {
      title: '',
      key: 'action',
      width: 160,
      render: (_: any, r: SerialPort) => {
        const existing = findExisting(d => d.type === 'serial' && d.address === r.port);
        return renderScanAction(
          existing,
          t('common.add'),
          () => handleAddSerial(r.port, r.description),
          { disabled: primaryProjectModelMissing, title: primaryProjectModelMissing ? '프로젝트·모델을 먼저 선택하세요' : undefined },
        );
      },
    },
  ];

  const baudrateOptions = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600];

  return (
    <div>
      {/* 좌측: 새로고침/연결/해제 — 우측: 디바이스 추가/스캔설정 (justify-between으로 양쪽 정렬) */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 6, marginBottom: 6 }}>
        <Space wrap>
          <Button icon={<ReloadOutlined />} onClick={fetchDevices} loading={loading}>{t('common.refresh')}</Button>
          <Button icon={<ApiOutlined />} type="primary" onClick={handleConnectAll} loading={connectingAll}>{t('device.connectAll')}</Button>
          <Button icon={<LinkOutlined />} onClick={handleConnectSelected} loading={connectingAll} disabled={selectedDeviceIds.size === 0}>{t('device.connectSelected')} ({selectedDeviceIds.size})</Button>
          <Button icon={<DisconnectOutlined />} danger onClick={handleDisconnectAll} loading={disconnectingAll}>{t('device.disconnectAll')}</Button>
          <Button icon={<DisconnectOutlined />} onClick={handleDisconnectSelected} loading={disconnectingAll} disabled={selectedDeviceIds.size === 0}>{t('device.disconnectSelected')} ({selectedDeviceIds.size})</Button>
        </Space>
        <Space wrap>
          <Button icon={<PlusOutlined />} type="primary" onClick={() => openAddModal('primary')}>{t('device.addPrimary')}</Button>
          <Button icon={<PlusOutlined />} onClick={() => openAddModal('auxiliary')}>{t('device.addAuxiliary')}</Button>
          <Button icon={<SettingOutlined />} onClick={openScanSettings}>{t('device.scanSettings')}</Button>
        </Space>
      </div>

      {/* 주/보조 디바이스를 별도 Card로 명확히 구분.
          - 주 디바이스 카드: primary 그룹들 (Android/HKMC/iSAP/VisionCam/Webcam 등)
          - 보조 디바이스 카드: auxiliary 그룹들 + Common 그룹 (항상 최하단 고정)
          - Common 그룹은 보호 디바이스(Common, WinControl) 전용 — 드래그 순서변경 비활성화. */}
      {(() => {
        const renderGroupCard = (prefix: string) => {
          const group = deviceGroups[prefix];
          if (!group || group.length === 0) return null;
          const label = GROUP_LABELS[prefix] || prefix;
          const connectedCount = group.filter(isDeviceConnected).length;
          // Common 그룹: 드래그 비활성. 보호 디바이스만 들어있으므로 ID/순서 변경 의미 없음.
          const isCommonGroup = prefix === 'Common';
          return (
            <Card
              key={prefix}
              size="small"
              type="inner"
              title={
                <Space>
                  <span style={{ fontWeight: 600 }}>{label}</span>
                  <Tag>{group.length}</Tag>
                  {connectedCount > 0 && <Tag color="green">{connectedCount} {t('device.statusConnected')}</Tag>}
                </Space>
              }
              styles={{ body: { padding: 0 } }}
            >
              {isCommonGroup ? (
                // 드래그 없이 단순 렌더 — 보호 디바이스(Common, WinControl, OCR) 순서 고정.
                // OCR은 별도 행 없이 Common 행에 모듈 태그로 통합.
                (() => {
                  const ocrDev = group.find(d => d.id === 'OCR');
                  return group
                    .filter(d => d.id !== 'OCR')
                    .map(d => (
                      <div key={d.id} style={{ padding: '6px 12px', borderTop: '1px solid rgba(0,0,0,0.06)' }}>
                        {renderDeviceRow(
                          d,
                          d.id === 'Common' && ocrDev?.info?.module ? [ocrDev.info.module] : undefined
                        )}
                      </div>
                    ));
                })()
              ) : (
                <DndContext sensors={dndSensors} collisionDetection={closestCenter}
                  onDragEnd={(e) => handleGroupDragEnd(prefix, e)}>
                  <SortableContext items={group.map(d => d.id)} strategy={verticalListSortingStrategy}>
                    {group.map(d => (
                      <SortableDeviceRow key={d.id} device={d}>
                        {renderDeviceRow(d)}
                      </SortableDeviceRow>
                    ))}
                  </SortableContext>
                </DndContext>
              )}
            </Card>
          );
        };

        const primaryCount = primaryGroupOrder.reduce((n, p) => n + (deviceGroups[p]?.length || 0), 0);
        const auxiliaryCount = auxiliaryGroupOrder.reduce((n, p) => n + (deviceGroups[p]?.length || 0), 0);

        return (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {/* 주 디바이스 카드 */}
            <Card
              size="small"
              title={
                <Space>
                  <Checkbox
                    indeterminate={(() => {
                      const selCount = primaryDevices.filter(d => selectedDeviceIds.has(d.id) && !d.protected).length;
                      const total = primaryDevices.filter(d => !d.protected).length;
                      return selCount > 0 && selCount < total;
                    })()}
                    checked={(() => {
                      const total = primaryDevices.filter(d => !d.protected).length;
                      const selCount = primaryDevices.filter(d => selectedDeviceIds.has(d.id) && !d.protected).length;
                      return total > 0 && selCount === total;
                    })()}
                    onChange={(e) => {
                      const next = new Set(selectedDeviceIds);
                      primaryDevices.forEach(d => {
                        if (d.protected) return;
                        if (e.target.checked) next.add(d.id); else next.delete(d.id);
                      });
                      setSelectedDeviceIds(next);
                    }}
                  />
                  <span style={{ fontWeight: 600 }}>{t('record.primaryDevices')}</span>
                  <Tag>{primaryCount}</Tag>
                </Space>
              }
            >
              {primaryGroupOrder.length === 0 ? (
                <div style={{ color: '#999', textAlign: 'center', padding: 20 }}>{t('device.noDevicesRegistered')}</div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {primaryGroupOrder.map(renderGroupCard)}
                </div>
              )}
            </Card>

            {/* 보조 디바이스 카드 */}
            <Card
              size="small"
              title={
                <Space>
                  <Checkbox
                    indeterminate={(() => {
                      const selCount = auxiliaryDevices.filter(d => selectedDeviceIds.has(d.id) && !d.protected).length;
                      const total = auxiliaryDevices.filter(d => !d.protected).length;
                      return selCount > 0 && selCount < total;
                    })()}
                    checked={(() => {
                      const total = auxiliaryDevices.filter(d => !d.protected).length;
                      const selCount = auxiliaryDevices.filter(d => selectedDeviceIds.has(d.id) && !d.protected).length;
                      return total > 0 && selCount === total;
                    })()}
                    onChange={(e) => {
                      const next = new Set(selectedDeviceIds);
                      auxiliaryDevices.forEach(d => {
                        if (d.protected) return;
                        if (e.target.checked) next.add(d.id); else next.delete(d.id);
                      });
                      setSelectedDeviceIds(next);
                    }}
                  />
                  <span style={{ fontWeight: 600 }}>{t('record.auxiliaryDevices')}</span>
                  <Tag>{auxiliaryCount}</Tag>
                </Space>
              }
            >
              {auxiliaryGroupOrder.length === 0 ? (
                <div style={{ color: '#999', textAlign: 'center', padding: 20 }}>{t('device.noDevicesRegistered')}</div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {auxiliaryGroupOrder.map(renderGroupCard)}
                </div>
              )}
            </Card>
          </div>
        );
      })()}

      {/* 장치 추가 모달 */}
      <Modal
        title={t('device.addModalTitle', { category: modalCategory === 'primary' ? t('device.primary') : t('device.auxiliary') })}
        open={modalOpen}
        onCancel={() => closeAddModal()}
        width={700}
        footer={null}
      >
        <Tabs
          activeKey={modalTabKey}
          onChange={setModalTabKey}
          items={[
            {
              key: 'scan',
              label: <span><SearchOutlined /> {t('device.scan')}</span>,
              children: (
                <div>
                  <Space style={{ marginBottom: 6 }} wrap>
                    <Button
                      type={hasScanned ? 'default' : 'primary'}
                      icon={hasScanned ? <ReloadOutlined /> : <SearchOutlined />}
                      onClick={handleScan}
                      loading={scanning}
                    >
                      {hasScanned ? t('device.rescan') : t('device.scan')}
                    </Button>
                    {modalCategory === 'primary' && (
                      <>
                        <Select
                          value={deviceProject || undefined}
                          onChange={(v) => { setDeviceProject(v); setDeviceModel(''); }}
                          style={{ minWidth: 120 }}
                          options={PROJECT_OPTIONS}
                          placeholder={t('device.projectPlaceholder')}
                          status={primaryProjectModelMissing && !deviceProject ? 'warning' : undefined}
                        />
                        <Select
                          allowClear
                          value={deviceModel || undefined}
                          onChange={(v) => {
                            const nextModel = v || '';
                            setDeviceModel(nextModel);
                            // 모델에 에이전트가 할당돼 있으면 connect type 동기화 (수동 연결 탭 호환)
                            const agentType = modelAgentType.get(nextModel);
                            if (agentType) {
                              setConnectType(agentType as any);
                            } else if (nextModel === 'SSH') {
                              setConnectType('ssh');
                              setModalTabKey('manual');
                            }
                          }}
                          style={{ minWidth: 200 }}
                          placeholder={t('device.deviceModelPlaceholder')}
                          options={DEVICE_MODELS}
                          status={primaryProjectModelMissing && !deviceModel ? 'warning' : undefined}
                        />
                        {primaryProjectModelMissing && (
                          <Typography.Text type="warning" style={{ fontSize: 11 }}>
                            ← {t('device.selectProjectAndModelFirst')}
                          </Typography.Text>
                        )}
                      </>
                    )}
                  </Space>

                  {(() => {
                    // 카테고리별 tab 구성 — 결과 있는 것만 표시
                    const PAGE_SIZE = 5;
                    const scanTabs: { key: string; label: React.ReactNode; children: React.ReactNode }[] = [];

                    if (scanItemCategory('adb') === modalCategory && scannedAdb.length > 0) {
                      scanTabs.push({
                        key: 'adb',
                        label: <span>{t('device.detectedAdb')} <Tag style={{ marginLeft: 3 }}>{scannedAdb.length}</Tag></span>,
                        children: (
                          <List
                            size="small"
                            dataSource={scannedAdb}
                            pagination={scannedAdb.length > PAGE_SIZE ? { pageSize: PAGE_SIZE, size: 'small' } : false}
                            renderItem={(d) => {
                              const existing = findExisting(x => x.type === 'adb' && x.address === d.serial);
                              return (
                                <List.Item actions={[
                                  renderScanAction(existing, t('common.add'), () => handleAddAdb(d.serial), {
                                    disabled: primaryProjectModelMissing,
                                    title: primaryProjectModelMissing ? '프로젝트·모델을 먼저 선택하세요' : undefined,
                                  })
                                ]}>
                                  <Tag color="green">{d.serial}</Tag> {d.model} <Tag>{d.status}</Tag>
                                </List.Item>
                              );
                            }}
                          />
                        ),
                      });
                    }

                    // STM Virtual COM Port(CANAT)는 별도 탭으로 분리 — 일반 시리얼 탭에는 노출 안 함
                    const canatPorts = scannedSerial.filter(p =>
                      (p.description || '').includes('STMicroelectronics Virtual COM Port')
                    );
                    const otherSerialPorts = scannedSerial.filter(p =>
                      !(p.description || '').includes('STMicroelectronics Virtual COM Port')
                    );

                    if (scanItemCategory('serial') === modalCategory && otherSerialPorts.length > 0) {
                      scanTabs.push({
                        key: 'serial',
                        label: <span>{t('device.detectedSerial')} <Tag style={{ marginLeft: 3 }}>{otherSerialPorts.length}</Tag></span>,
                        children: (
                          <>
                            {modalCategory === 'auxiliary' && (
                              <Space style={{ marginBottom: 6, width: '100%' }} direction="vertical">
                                {modules.length > 0 && (
                                  <div>
                                    <span style={{ marginRight: 6, color: '#888', fontSize: 11 }}>{`${t('device.module')}:`}</span>
                                    <Select
                                      allowClear
                                      placeholder={t('device.moduleSelect')}
                                      value={scanSelectedModule}
                                      onChange={setScanSelectedModule}
                                      style={{ width: 280 }}
                                      options={visibleModules.map(m => ({ label: m.label, value: m.name }))}
                                    />
                                  </div>
                                )}
                                <div>
                                  <span style={{ marginRight: 6, color: '#888', fontSize: 11 }}>Baudrate:</span>
                                  <Select
                                    value={baudrate}
                                    onChange={setBaudrate}
                                    style={{ width: 150 }}
                                    options={baudrateOptions.map(b => ({ label: `${b}`, value: b }))}
                                  />
                                </div>
                              </Space>
                            )}
                            <Table
                              columns={serialColumns}
                              dataSource={otherSerialPorts}
                              rowKey="port"
                              size="small"
                              pagination={otherSerialPorts.length > PAGE_SIZE ? { pageSize: PAGE_SIZE, size: 'small' } : false}
                            />
                          </>
                        ),
                      });
                    }

                    if (scanItemCategory('serial') === modalCategory && canatPorts.length > 0) {
                      scanTabs.push({
                        key: 'canat',
                        label: <span>{t('device.detectedCanat')} <Tag color="green" style={{ marginLeft: 3 }}>{canatPorts.length}</Tag></span>,
                        children: (
                          <>
                            <div style={{ marginBottom: 6, padding: '6px 10px', background: 'rgba(82,196,26,0.08)', borderLeft: '3px solid #52c41a', borderRadius: 3, fontSize: 11, color: '#888' }}>
                              {t('device.canatHint')}
                            </div>
                            <Table
                              columns={serialColumns}
                              dataSource={canatPorts}
                              rowKey="port"
                              size="small"
                              pagination={canatPorts.length > PAGE_SIZE ? { pageSize: PAGE_SIZE, size: 'small' } : false}
                            />
                          </>
                        ),
                      });
                    }

                    if (scanItemCategory('hkmc') === modalCategory && scannedHkmc.length > 0) {
                      scanTabs.push({
                        key: 'hkmc',
                        label: <span>{t('device.detectedHkmc')} <Tag style={{ marginLeft: 3 }}>{scannedHkmc.length}</Tag></span>,
                        children: (
                          <List
                            size="small"
                            dataSource={scannedHkmc}
                            pagination={scannedHkmc.length > PAGE_SIZE ? { pageSize: PAGE_SIZE, size: 'small' } : false}
                            renderItem={(d) => {
                              // HKMC 스캔 결과는 hkmc_agent 또는 hkmc5th_wide_agent 어느 쪽으로도 등록될 수 있음.
                              const existing = findExisting(x => (x.type === 'hkmc_agent' || x.type === 'hkmc5th_wide_agent') && x.address === d.ip);
                              return (
                                <List.Item actions={[
                                  renderScanAction(existing, t('common.add'), () => handleAddHkmc(d.ip, d.port), {
                                    disabled: primaryProjectModelMissing,
                                    title: primaryProjectModelMissing ? '프로젝트·모델을 먼저 선택하세요' : undefined,
                                  })
                                ]}>
                                  <Tag color="volcano">HKMC</Tag> <Tag color="blue">{d.ip}</Tag> <span style={{ color: '#888' }}>TCP: {d.port}</span>
                                </List.Item>
                              );
                            }}
                          />
                        ),
                      });
                    }

                    if (scanItemCategory('isap') === modalCategory && scannedIsap.length > 0) {
                      scanTabs.push({
                        key: 'isap',
                        label: <span>{t('device.detectedIsap')} <Tag style={{ marginLeft: 3 }}>{scannedIsap.length}</Tag></span>,
                        children: (
                          <List
                            size="small"
                            dataSource={scannedIsap}
                            pagination={scannedIsap.length > PAGE_SIZE ? { pageSize: PAGE_SIZE, size: 'small' } : false}
                            renderItem={(d) => {
                              const existing = findExisting(x => x.type === 'isap_agent' && x.address === d.ip);
                              return (
                                <List.Item actions={[
                                  renderScanAction(existing, t('common.add'), () => handleAddIsap(d.ip, d.port), {
                                    disabled: primaryProjectModelMissing,
                                    title: primaryProjectModelMissing ? '프로젝트·모델을 먼저 선택하세요' : undefined,
                                  })
                                ]}>
                                  <Tag color="geekblue">iSAP</Tag> <Tag color="blue">{d.ip}</Tag> <span style={{ color: '#888' }}>TCP: {d.port}</span>
                                </List.Item>
                              );
                            }}
                          />
                        ),
                      });
                    }

                    if (scanItemCategory('icas') === modalCategory && scannedIcas.length > 0) {
                      scanTabs.push({
                        key: 'icas',
                        label: <span>{t('device.detectedIcas')} <Tag style={{ marginLeft: 3 }}>{scannedIcas.length}</Tag></span>,
                        children: (
                          <List
                            size="small"
                            dataSource={scannedIcas}
                            pagination={scannedIcas.length > PAGE_SIZE ? { pageSize: PAGE_SIZE, size: 'small' } : false}
                            renderItem={(h) => {
                              const existing = findExisting(x => x.type === 'icas_agent' && x.address === h.ip);
                              return (
                                <List.Item actions={[
                                  renderScanAction(existing, t('common.connect'), () => handleAddSshAgent('icas_agent', h.ip, h.port), {
                                    disabled: primaryProjectModelMissing,
                                    title: primaryProjectModelMissing ? '프로젝트·모델을 먼저 선택하세요' : undefined,
                                  })
                                ]}>
                                  <Tag color="purple">ICAS</Tag> <Tag color="blue">{h.ip}</Tag> <span style={{ color: '#888' }}>SSH: {h.port}</span>
                                </List.Item>
                              );
                            }}
                          />
                        ),
                      });
                    }

                    if (scanItemCategory('mib') === modalCategory && scannedMib.length > 0) {
                      scanTabs.push({
                        key: 'mib',
                        label: <span>MIB Agent <Tag style={{ marginLeft: 3 }}>{scannedMib.length}</Tag></span>,
                        children: (
                          <List
                            size="small"
                            dataSource={scannedMib}
                            pagination={scannedMib.length > PAGE_SIZE ? { pageSize: PAGE_SIZE, size: 'small' } : false}
                            renderItem={(h) => {
                              const existing = findExisting(x => x.type === 'mib_agent' && x.address === h.ip);
                              return (
                                <List.Item actions={[
                                  renderScanAction(existing, t('common.connect'), () => handleAddSshAgent('mib_agent', h.ip, h.port), {
                                    disabled: primaryProjectModelMissing,
                                    title: primaryProjectModelMissing ? '프로젝트·모델을 먼저 선택하세요' : undefined,
                                  })
                                ]}>
                                  <Tag color="geekblue">MIB</Tag> <Tag color="blue">{h.ip}</Tag> <span style={{ color: '#888' }}>SSH: {h.port}</span>
                                </List.Item>
                              );
                            }}
                          />
                        ),
                      });
                    }

                    if (scanItemCategory('bench') === modalCategory && scannedBench.length > 0) {
                      scanTabs.push({
                        key: 'bench',
                        label: <span>{t('device.detectedBench')} <Tag style={{ marginLeft: 3 }}>{scannedBench.length}</Tag></span>,
                        children: (
                          <>
                            {modules.length > 0 && (
                              <div style={{ marginBottom: 6 }}>
                                <span style={{ marginRight: 6, color: '#888', fontSize: 11 }}>{`${t('device.module')}:`}</span>
                                <Select
                                  placeholder={t('device.moduleSelect')}
                                  value={scanSelectedModule}
                                  onChange={setScanSelectedModule}
                                  style={{ width: 280 }}
                                  defaultValue="WoohyunBench"
                                  options={visibleModules.filter(m => m.connect_type === 'socket').map(m => ({ label: m.label, value: m.name }))}
                                />
                              </div>
                            )}
                            <List
                              size="small"
                              dataSource={scannedBench}
                              pagination={scannedBench.length > PAGE_SIZE ? { pageSize: PAGE_SIZE, size: 'small' } : false}
                              renderItem={(d) => {
                                const benchModule = scanSelectedModule || 'WoohyunBench';
                                const existing = findExisting(x => x.type === 'module' && x.info?.module === benchModule && x.address === d.ip);
                                return (
                                  <List.Item actions={[
                                    renderScanAction(existing, t('common.add'), () => handleAddBench(d.ip, d.port))
                                  ]}>
                                    {d.verified ? <Tag color="green">Bench</Tag> : <Tag color="default">Host</Tag>}
                                    <Tag color="blue">{d.ip}</Tag>
                                    <span style={{ color: '#888' }}>UDP: {d.port}</span>
                                    {d.verified && <Tag color="green" style={{ marginLeft: 3 }}>응답확인</Tag>}
                                  </List.Item>
                                );
                              }}
                            />
                          </>
                        ),
                      });
                    }

                    if (scanItemCategory('vision_camera') === modalCategory && scannedVision.length > 0) {
                      scanTabs.push({
                        key: 'vision',
                        label: <span>{t('device.detectedVision')} <Tag style={{ marginLeft: 3 }}>{scannedVision.length}</Tag></span>,
                        children: (
                          <>
                            {pcInterfaces.length > 0 && (
                              <div style={{ marginBottom: 6, fontSize: 11, color: '#888' }}>
                                {t('device.pcInterfaces')}: {pcInterfaces.map(i => `${i.ip}/${i.prefix} (${i.name})`).join(' | ')}
                              </div>
                            )}
                            <List
                              size="small"
                              dataSource={scannedVision}
                              pagination={scannedVision.length > PAGE_SIZE ? { pageSize: PAGE_SIZE, size: 'small' } : false}
                              renderItem={(cam) => (
                                <List.Item actions={[
                                  <Space size={4}>
                                    {cam.mac && (
                                      <Button size="small" onClick={() => {
                                        setForceIpModal({ mac: cam.mac, currentIp: cam.ip || '' });
                                        const iface = pcInterfaces[0];
                                        if (iface) {
                                          const parts = iface.ip.split('.');
                                          const camParts = (cam.ip || '').split('.');
                                          const prefixLen = iface.prefix || 24;
                                          const sameSubnet = prefixLen >= 24
                                            && parts[0] === camParts[0]
                                            && parts[1] === camParts[1]
                                            && parts[2] === camParts[2];
                                          if (sameSubnet) {
                                            setForceIpAddr(cam.ip || '');
                                            setForceIpSubnet(cam.subnet || '255.255.255.0');
                                          } else {
                                            const lastOctet = parseInt(parts[3]) < 200 ? parseInt(parts[3]) + 100 : parseInt(parts[3]) - 100;
                                            setForceIpAddr(`${parts[0]}.${parts[1]}.${parts[2]}.${Math.min(Math.max(lastOctet, 2), 254)}`);
                                            const masks: Record<number, string> = { 8: '255.0.0.0', 16: '255.255.0.0', 24: '255.255.255.0' };
                                            setForceIpSubnet(masks[prefixLen] || '255.255.255.0');
                                          }
                                        } else {
                                          setForceIpAddr(cam.ip || '');
                                          setForceIpSubnet(cam.subnet || '255.255.255.0');
                                        }
                                        setForceIpGateway(cam.gateway || '0.0.0.0');
                                      }}>{t('device.visionForceIp')}</Button>
                                    )}
                                    <Button size="small" type="primary" loading={connecting} onClick={() => {
                                      setConnectType('vision_camera');
                                      setVcMac(cam.mac);
                                      setVcModel(cam.model || '');
                                      setVcSerial(cam.serial || '');
                                      setConnectAddress(cam.ip || '');
                                      setModalTabKey('manual');
                                    }}>{t('common.connect')}</Button>
                                  </Space>
                                ]}>
                                  <div>
                                    <Tag color="magenta">VisionCam</Tag>
                                    {cam.model && <span style={{ marginRight: 6, fontWeight: 500 }}>{cam.model}</span>}
                                    {cam.vendor && <span style={{ color: '#888', marginRight: 6 }}>{cam.vendor}</span>}
                                    <br />
                                    {cam.mac && <Tag color="blue">MAC: {cam.mac}</Tag>}
                                    {cam.ip ? <Tag color="cyan">IP: {cam.ip}</Tag> : <Tag color="orange">IP: unknown</Tag>}
                                    {cam.subnet && <Tag>/{cam.subnet}</Tag>}
                                  </div>
                                </List.Item>
                              )}
                            />
                          </>
                        ),
                      });
                    }

                    if (scanItemCategory('webcam') === modalCategory && scannedWebcams.length > 0) {
                      scanTabs.push({
                        key: 'webcam',
                        label: <span>{t('device.detectedWebcam')} <Tag style={{ marginLeft: 3 }}>{scannedWebcams.length}</Tag></span>,
                        children: (
                          <List
                            size="small"
                            dataSource={scannedWebcams}
                            pagination={scannedWebcams.length > PAGE_SIZE ? { pageSize: PAGE_SIZE, size: 'small' } : false}
                            renderItem={(w) => {
                              const busy = !!w.in_use_by_recording;
                              const dup = !!w.already_registered;
                              const handleQuickAdd = async () => {
                                if (!ensurePrimaryProjectModel()) return;
                                setConnecting(true);
                                try {
                                  const extra = { device_index: w.index, width: w.width || 0, height: w.height || 0 };
                                  const result = await connectDevice('webcam', String(w.index), undefined, '', 'primary', undefined, 'webcam', extra);
                                  message.success(result);
                                  closeAddModal();
                                } catch (e: any) {
                                  message.error(e.response?.data?.detail || t('device.connectFailed'));
                                }
                                setConnecting(false);
                              };
                              return (
                                <List.Item actions={[
                                  <Button size="small" type="primary" loading={connecting}
                                          disabled={dup || busy || primaryProjectModelMissing}
                                          title={primaryProjectModelMissing ? '프로젝트·모델을 먼저 선택하세요' : undefined}
                                          onClick={handleQuickAdd}>
                                    {t('common.add')}
                                  </Button>
                                ]}>
                                  <div>
                                    <Tag color="purple">{t('device.webcam')}</Tag>
                                    <strong>{w.label}</strong>
                                    {w.width > 0 && <Tag style={{ marginLeft: 6 }}>{w.width}×{w.height}</Tag>}
                                    {dup && <Tag color="default" style={{ marginLeft: 6 }}>{t('device.alreadyRegistered')}</Tag>}
                                    {busy && <Tag color="orange" style={{ marginLeft: 6 }}>{t('device.webcamInUseByRecording')}</Tag>}
                                  </div>
                                </List.Item>
                              );
                            }}
                          />
                        ),
                      });
                    }

                    if (scanItemCategory('dlt') === modalCategory && scannedDlt.length > 0) {
                      const dltModule = (scanBuiltin.dlt as any)?.module || 'DLTLogging';
                      scanTabs.push({
                        key: 'dlt',
                        label: <span>{t('dlt.detectedDlt')} <Tag style={{ marginLeft: 3 }}>{scannedDlt.length}</Tag></span>,
                        children: (
                          <List
                            size="small"
                            bordered
                            dataSource={scannedDlt}
                            pagination={scannedDlt.length > PAGE_SIZE ? { pageSize: PAGE_SIZE, size: 'small' } : false}
                            renderItem={(d) => {
                              const existing = findExisting(x => x.type === 'module' && x.info?.module === dltModule && x.address === d.ip);
                              const doAdd = async () => {
                                try {
                                  await connectDevice('module', d.ip, undefined, `${dltModule}_${d.ip}`, 'auxiliary', dltModule, 'socket', { port: d.port });
                                  message.success(`DLT ${d.ip}:${d.port} ${t('common.connect')}`);
                                  closeAddModal();
                                } catch (e: any) {
                                  message.error(e.response?.data?.detail || 'Connect failed');
                                }
                              };
                              return (
                                <List.Item actions={[renderScanAction(existing, t('common.connect'), doAdd)]}>
                                  <div>
                                    <Tag color="geekblue">DLT</Tag>
                                    <strong>{d.ip}</strong>:{d.port}
                                    <Tag style={{ marginLeft: 6 }}>{dltModule}</Tag>
                                  </div>
                                </List.Item>
                              );
                            }}
                          />
                        ),
                      });
                    }

                    if (scanItemCategory('smartbench') === modalCategory && scannedSmartbench.length > 0) {
                      scanTabs.push({
                        key: 'smartbench',
                        label: <span>SmartBench <Tag style={{ marginLeft: 3 }}>{scannedSmartbench.length}</Tag></span>,
                        children: (
                          <List
                            size="small"
                            bordered
                            dataSource={scannedSmartbench}
                            pagination={scannedSmartbench.length > PAGE_SIZE ? { pageSize: PAGE_SIZE, size: 'small' } : false}
                            renderItem={(h) => {
                              const existing = findExisting(x => x.type === 'module' && x.info?.module === 'SmartBench' && x.address === h.ip);
                              const doAdd = async () => {
                                try {
                                  const devId = `SmartBench_${h.ip}`;
                                  await connectDevice('module', h.ip, undefined, devId, 'auxiliary', 'SmartBench', 'socket', { port: h.port });
                                  message.success(`SmartBench ${h.ip}:${h.port} ${t('common.connect')}`);
                                  closeAddModal();
                                } catch (e: any) {
                                  message.error(e.response?.data?.detail || 'Connect failed');
                                }
                              };
                              return (
                                <List.Item actions={[renderScanAction(existing, t('common.connect'), doAdd)]}>
                                  <div>
                                    <Tag color="orange">SmartBench</Tag>
                                    <strong>{h.ip}</strong>:{h.port}
                                  </div>
                                </List.Item>
                              );
                            }}
                          />
                        ),
                      });
                    }

                    if (scanItemCategory('scar') === modalCategory && scannedScar.length > 0) {
                      scanTabs.push({
                        key: 'scar',
                        label: <span>SCAR <Tag style={{ marginLeft: 3 }}>{scannedScar.length}</Tag></span>,
                        children: (
                          <List
                            size="small"
                            bordered
                            dataSource={scannedScar}
                            pagination={scannedScar.length > PAGE_SIZE ? { pageSize: PAGE_SIZE, size: 'small' } : false}
                            renderItem={(h) => {
                              const firstIface = (h.interfaces && h.interfaces[0]) || '';
                              const existing = findExisting(x => x.type === 'module' && x.info?.module === 'SCAR' && x.address === h.ip);
                              const doAdd = () => {
                                // SCAR 모듈 폼으로 전환 — netns VLAN 구성을 검토 후 등록.
                                // (netns clean 은 파괴적이므로 즉시 연결 대신 폼에서 확인하게 한다.
                                //  vlan_config_dir 가 비어 있으면 netns 단계는 건너뛰고 런타임만 사용.)
                                const scarInfo = modules.find(m => m.name === 'SCAR');
                                const defaults: Record<string, any> = {};
                                if (scarInfo?.connect_fields) {
                                  for (const f of scarInfo.connect_fields) {
                                    defaults[f.name] = f.default ?? '';
                                  }
                                }
                                defaults.api_base = `http://${h.ip}:${h.port}`;
                                defaults.container = h.container;
                                if (firstIface) defaults.iface = firstIface;  // 스캔한 인터페이스 자동 채움
                                setConnectType('module');
                                setSelectedModule('SCAR');
                                setConnectAddress(h.ip);
                                setExtraFieldValues(defaults);
                                setModalTabKey('manual');
                              };
                              return (
                                <List.Item actions={[renderScanAction(existing, t('common.connect'), doAdd)]}>
                                  <div>
                                    <Tag color="purple">SCAR</Tag>
                                    <strong>{h.ip}</strong>:{h.port}
                                    <span style={{ marginLeft: 8, color: '#888' }}>container={h.container}</span>
                                    {h.api_alive && <Tag color="green" style={{ marginLeft: 6 }}>API</Tag>}
                                    {h.docker_running && <Tag color="blue" style={{ marginLeft: 4 }}>DOCKER</Tag>}
                                    {!h.api_alive && !h.docker_running && (
                                      <Tag color="orange" style={{ marginLeft: 6 }}>미기동 — 등록 시 자동 기동</Tag>
                                    )}
                                    {h.interfaces && h.interfaces.length > 0 && (
                                      <span style={{ marginLeft: 8, color: '#888' }}>
                                        iface={h.interfaces.join(',')}
                                      </span>
                                    )}
                                  </div>
                                </List.Item>
                              );
                            }}
                          />
                        ),
                      });
                    }

                    if (scanItemCategory('radmoon') === modalCategory && scannedRadmoon.length > 0) {
                      scanTabs.push({
                        key: 'radmoon',
                        label: <span>RAD_Moon (TH) <Tag style={{ marginLeft: 3 }}>{scannedRadmoon.length}</Tag></span>,
                        children: (
                          <List
                            size="small"
                            bordered
                            dataSource={scannedRadmoon}
                            pagination={scannedRadmoon.length > PAGE_SIZE ? { pageSize: PAGE_SIZE, size: 'small' } : false}
                            renderItem={(h) => {
                              const firstMember = h.members && h.members[0];
                              const memberIface = firstMember?.interface || '';
                              const existing = findExisting(x => x.type === 'module' && x.info?.module === 'TH' && x.info?.cvd_br === h.bridge);
                              const doAdd = () => {
                                // TH 모듈 폼으로 전환. cvd_br + (있으면) eth_if + 나머지 connect_th.sh 디폴트 자동 채움.
                                const thInfo = modules.find(m => m.name === 'TH');
                                const defaults: Record<string, any> = {};
                                if (thInfo?.connect_fields) {
                                  for (const f of thInfo.connect_fields) {
                                    defaults[f.name] = f.default ?? '';
                                  }
                                }
                                defaults.cvd_br = h.bridge;                     // 스캔한 bridge 이름
                                if (memberIface) defaults.eth_if = memberIface; // 이미 attach 되어 있으면 자동
                                setConnectType('module');
                                setSelectedModule('TH');
                                setConnectAddress(memberIface || h.bridge);     // 표시용
                                setExtraFieldValues(defaults);
                                setModalTabKey('manual');
                              };
                              return (
                                <List.Item actions={[renderScanAction(existing, t('common.connect'), doAdd)]}>
                                  <div>
                                    <Tag color="geekblue">RAD_Moon</Tag>
                                    <strong>bridge:{h.bridge}</strong>
                                    <Tag color={h.bridge_operstate === 'up' ? 'green' : 'default'} style={{ marginLeft: 6 }}>
                                      {h.bridge_operstate}
                                    </Tag>
                                    {h.current_ips && h.current_ips.length > 0 && (
                                      <span style={{ marginLeft: 8, color: '#888' }}>
                                        ip={h.current_ips.join(',')}
                                      </span>
                                    )}
                                    {h.members && h.members.length > 0 ? (
                                      <span style={{ marginLeft: 8, color: '#888' }}>
                                        eth_if={h.members.map(m => m.interface).join(',')}
                                      </span>
                                    ) : (
                                      <Tag color="orange" style={{ marginLeft: 8 }}>no member — eth_if 수동 입력 필요</Tag>
                                    )}
                                  </div>
                                </List.Item>
                              );
                            }}
                          />
                        ),
                      });
                    }

                    if (scanItemCategory('ssh') === modalCategory && scannedSsh.length > 0) {
                      scanTabs.push({
                        key: 'ssh',
                        label: <span>{t('device.detectedSsh')} <Tag style={{ marginLeft: 3 }}>{scannedSsh.length}</Tag></span>,
                        children: (
                          <List
                            size="small"
                            bordered
                            dataSource={scannedSsh}
                            pagination={scannedSsh.length > PAGE_SIZE ? { pageSize: PAGE_SIZE, size: 'small' } : false}
                            renderItem={(h) => {
                              const existing = findExisting(x => x.type === 'ssh' && x.address === h.ip);
                              const doAdd = () => {
                                setConnectType('ssh');
                                setConnectAddress(h.ip);
                                setSshPort(h.port);
                                if (modalCategory === 'primary') {
                                  setDeviceProject('General');
                                  setDeviceModel('SSH');
                                }
                                setModalTabKey('manual');
                              };
                              return (
                                <List.Item actions={[renderScanAction(existing, t('common.connect'), doAdd)]}>
                                  <div>
                                    <Tag color="magenta">SSH</Tag>
                                    <strong>{h.ip}</strong>:{h.port}
                                  </div>
                                </List.Item>
                              );
                            }}
                          />
                        ),
                      });
                    }

                    // 커스텀 스캔 결과 — 설정 카테고리 일치 시에만 노출 (미지정은 auxiliary 기본)
                    scannedCustom.forEach((group, gi) => {
                      if (group.hosts.length === 0) return;
                      const customEntry = scanCustom.find(c => c.label === group.label);
                      const customCat: ScanCategory = (customEntry?.category as ScanCategory) || 'auxiliary';
                      if (customCat !== modalCategory) return;
                      const moduleName = customEntry?.module || '';
                      scanTabs.push({
                        key: `custom_${gi}`,
                        label: <span>{group.label} <Tag style={{ marginLeft: 3 }}>{group.hosts.length}</Tag></span>,
                        children: (
                          <List
                            size="small"
                            bordered
                            dataSource={group.hosts}
                            pagination={group.hosts.length > PAGE_SIZE ? { pageSize: PAGE_SIZE, size: 'small' } : false}
                            renderItem={(h) => {
                              const existing = findExisting(x => x.type === 'module' && x.address === h.ip
                                && (moduleName ? x.info?.module === moduleName : (x.info?.port === h.port)));
                              const doAdd = async () => {
                                try {
                                  const devId = moduleName ? `${moduleName}_${h.ip}` : `tcp_${h.ip}_${h.port}`;
                                  await connectDevice('module', h.ip, undefined, devId, 'auxiliary', moduleName || undefined, 'socket', { port: h.port });
                                  message.success(`${group.label} ${h.ip}:${h.port} ${t('common.connect')}`);
                                  closeAddModal();
                                } catch (e: any) {
                                  message.error(e.response?.data?.detail || 'Connect failed');
                                }
                              };
                              return (
                                <List.Item actions={[renderScanAction(existing, t('common.connect'), doAdd)]}>
                                  <div>
                                    <Tag color="cyan">{group.label}</Tag>
                                    <strong>{h.ip}</strong>:{h.port}
                                    {moduleName && <Tag style={{ marginLeft: 6 }}>{moduleName}</Tag>}
                                  </div>
                                </List.Item>
                              );
                            }}
                          />
                        ),
                      });
                    });

                    if (scanTabs.length === 0 && !scanning) {
                      if (!hasScanned) {
                        return (
                          <div style={{ color: '#888', textAlign: 'center', padding: 26 }}>
                            <SearchOutlined style={{ fontSize: 23, marginBottom: 6 }} />
                            <div style={{ fontSize: 12 }}>{t('device.clickToScan')}</div>
                          </div>
                        );
                      }
                      return (
                        <div style={{ color: '#666', textAlign: 'center', padding: 19 }}>
                          {t('device.noDevicesFound')}
                        </div>
                      );
                    }

                    return <Tabs size="small" items={scanTabs} />;
                  })()}
                </div>
              ),
            },
            {
              key: 'manual',
              label: <span><WifiOutlined /> {t('device.manualConnect')}</span>,
              children: (() => {
                const moduleConnType = getModuleConnectType(selectedModule);
                const connectFields = getModuleConnectFields(selectedModule);
                return (
                  <Space direction="vertical" style={{ width: '100%' }}>
                    {modalCategory === 'primary' && (
                      <Space style={{ width: '100%' }} wrap>
                        <Select
                          value={deviceProject || undefined}
                          onChange={(v) => { setDeviceProject(v); setDeviceModel(''); }}
                          style={{ minWidth: 120 }}
                          options={PROJECT_OPTIONS}
                          placeholder={t('device.projectPlaceholder')}
                          status={primaryProjectModelMissing && !deviceProject ? 'warning' : undefined}
                        />
                        <Select
                          allowClear
                          value={deviceModel || undefined}
                          onChange={(v) => {
                            const nextModel = v || '';
                            setDeviceModel(nextModel);
                            // 모델에 에이전트가 할당돼 있으면 해당 type으로 connect type 자동 전환
                            const agentType = modelAgentType.get(nextModel);
                            if (agentType) {
                              setConnectType(agentType as any);
                            } else if (nextModel === 'SSH') {
                              setConnectType('ssh');
                            } else if (connectType === 'ssh') {
                              setConnectType('adb');
                            }
                          }}
                          style={{ minWidth: 200, flex: 1 }}
                          placeholder={t('device.deviceModelPlaceholder')}
                          options={DEVICE_MODELS}
                          status={primaryProjectModelMissing && !deviceModel ? 'warning' : undefined}
                        />
                        {primaryProjectModelMissing && (
                          <Typography.Text type="warning" style={{ fontSize: 11 }}>
                            ← {t('device.selectProjectAndModelFirst')}
                          </Typography.Text>
                        )}
                      </Space>
                    )}
                    {modalCategory === 'auxiliary' && modules.length > 0 && (
                      <Select
                        allowClear
                        placeholder={t('device.moduleSelect')}
                        value={selectedModule}
                        onChange={(v) => {
                          setSelectedModule(v);
                          // hidden 필드도 backend 에 보내려면 default 를 미리 seed 해야 함.
                          // 사용자가 보이는 필드만 편집하고 connect 누르면 hidden 필드는
                          // default 그대로 전송됨.
                          const modInfo = modules.find(m => m.name === v);
                          const seed: Record<string, any> = {};
                          if (modInfo?.connect_fields) {
                            for (const f of modInfo.connect_fields) {
                              seed[f.name] = f.default ?? '';
                            }
                          }
                          setExtraFieldValues(seed);
                          const ct = getModuleConnectType(v);
                          if (ct === 'serial') setConnectType('serial');
                          else if (ct === 'socket' || ct === 'none' || ct === 'can') setConnectType('module');
                          else setConnectType('serial');
                        }}
                        style={{ width: '100%' }}
                        options={visibleModules.map(m => ({ label: `${m.label} [${m.connect_type}]`, value: m.name }))}
                      />
                    )}

                    {(!selectedModule || moduleConnType === undefined) && (
                      <Select value={connectType} onChange={setConnectType} style={{ width: '100%' }}>
                        <Option value="adb">ADB (WiFi / TCP)</Option>
                        {modalCategory === 'primary' && <Option value="hkmc_agent">HKMC Agent (TCP)</Option>}
                        {modalCategory === 'primary' && <Option value="isap_agent">iSAP Agent (TCP)</Option>}
                        {modalCategory === 'primary' && <Option value="icas_agent">ICAS Agent (SSH)</Option>}
                        {modalCategory === 'primary' && <Option value="mib_agent">MIB Agent (SSH)</Option>}
                        {modalCategory === 'primary' && <Option value="vision_camera">Vision Camera</Option>}
                        {modalCategory === 'primary' && <Option value="webcam">{t('device.webcam')}</Option>}
                        <Option value="serial">{t('device.serialPort')}</Option>
                        <Option value="ssh">{t('device.ssh')}</Option>
                      </Select>
                    )}

                    {moduleConnType === 'serial' && (
                      <>
                        <Input
                          placeholder={t('device.comPlaceholder')}
                          value={connectAddress}
                          onChange={(e) => setConnectAddress(e.target.value)}
                          onPressEnter={handleConnect}
                        />
                        <div>
                          <span style={{ fontSize: 11, color: '#888', marginRight: 6 }}>Baudrate:</span>
                          <Select
                            value={baudrate}
                            onChange={setBaudrate}
                            style={{ width: 150 }}
                            options={baudrateOptions.map(b => ({ label: `${b}`, value: b }))}
                          />
                        </div>
                      </>
                    )}

                    {moduleConnType === 'socket' && (
                      <Input
                        placeholder={t('device.ipPlaceholder')}
                        value={connectAddress}
                        onChange={(e) => setConnectAddress(e.target.value)}
                        onPressEnter={handleConnect}
                      />
                    )}

                    {moduleConnType === 'can' && (
                      <>
                        {renderConnectFields(connectFields, extraFieldValues, setExtraFieldValues)}
                      </>
                    )}

                    {moduleConnType === 'none' && (
                      connectFields.length > 0 ? (
                        // TH / SCAR 처럼 connect_type='none' 이지만 module-specific 설정 필드가
                        // 있는 경우 — eth_if / th_home / sudo_password 등을 폼에 노출.
                        renderConnectFields(connectFields, extraFieldValues, setExtraFieldValues)
                      ) : (
                        <div style={{ color: '#888', fontSize: 11, padding: '8px 0' }}>
                          {t('device.noConnectionRequired')}
                        </div>
                      )
                    )}

                    {!selectedModule && connectType === 'hkmc_agent' && (
                      <>
                        <Input
                          placeholder={t('device.hkmcIpPlaceholder')}
                          value={connectAddress}
                          onChange={(e) => setConnectAddress(e.target.value)}
                          onPressEnter={handleConnect}
                        />
                        <div>
                          <span style={{ fontSize: 11, color: '#888', marginRight: 6 }}>TCP Port:</span>
                          <InputNumber
                            value={hkmcPort}
                            onChange={(v) => setHkmcPort(v || 5000)}
                            min={1} max={65535}
                            style={{ width: 150 }}
                          />
                        </div>
                        <div style={{ marginTop: 6, fontSize: 11, color: '#888' }}>
                          클러스터 캡처용 SSH (cluster screen은 QNX SSH+screenshot+SCP). 비워두면 기본값 <b>root / 빈 패스워드 / 22</b> 사용 (ICAS QNX 패턴).
                        </div>
                        <Space wrap>
                          <Input
                            placeholder="SSH user (default: root)"
                            value={sshUser}
                            onChange={(e) => setSshUser(e.target.value)}
                            style={{ width: 160 }}
                          />
                          <Input.Password
                            placeholder="SSH password (default: empty)"
                            value={sshPass}
                            onChange={(e) => setSshPass(e.target.value)}
                            style={{ width: 180 }}
                          />
                          <span style={{ fontSize: 11, color: '#888' }}>SSH Port:</span>
                          <InputNumber
                            value={sshPort}
                            onChange={(v) => setSshPort(v || 22)}
                            min={1} max={65535}
                            style={{ width: 90 }}
                          />
                        </Space>
                      </>
                    )}

                    {!selectedModule && connectType === 'isap_agent' && (
                      <>
                        <Input
                          placeholder="iSAP Agent IP (예: 192.168.105.1)"
                          value={connectAddress}
                          onChange={(e) => setConnectAddress(e.target.value)}
                          onPressEnter={handleConnect}
                        />
                        <div>
                          <span style={{ fontSize: 11, color: '#888', marginRight: 6 }}>TCP Port:</span>
                          <InputNumber
                            value={hkmcPort}
                            onChange={(v) => setHkmcPort(v || 20000)}
                            min={1} max={65535}
                            style={{ width: 150 }}
                          />
                          <span style={{ fontSize: 10, color: '#888', marginLeft: 6 }}>
                            20000=전석, 20003=클러스터, 20004=HUD
                          </span>
                        </div>
                      </>
                    )}

                    {!selectedModule && connectType === 'icas_agent' && (
                      <>
                        <Input
                          placeholder="ICAS IP (예: 192.168.1.4)"
                          value={connectAddress}
                          onChange={(e) => setConnectAddress(e.target.value)}
                          onPressEnter={handleConnect}
                        />
                        <Space wrap>
                          <span style={{ fontSize: 11, color: '#888' }}>SSH Port:</span>
                          <InputNumber
                            value={hkmcPort}
                            onChange={(v) => setHkmcPort(v || 22)}
                            min={1} max={65535}
                            style={{ width: 100 }}
                          />
                          <span style={{ fontSize: 11, color: '#888' }}>User:</span>
                          <Input value={sshUser} onChange={(e) => setSshUser(e.target.value)} style={{ width: 120 }} placeholder="root" />
                          <span style={{ fontSize: 11, color: '#888' }}>Password:</span>
                          <Input.Password value={sshPass} onChange={(e) => setSshPass(e.target.value)} style={{ width: 160 }} placeholder="(blank if none)" />
                        </Space>
                        <div style={{ fontSize: 10, color: '#888' }}>
                          해상도는 1560x700(10") 또는 2240x1260(15") 중 선택 — 등록 후 수정 모달에서 변경 가능
                        </div>
                      </>
                    )}

                    {!selectedModule && connectType === 'mib_agent' && (
                      <>
                        <Input
                          placeholder="MIB IP (예: 192.168.1.4)"
                          value={connectAddress}
                          onChange={(e) => setConnectAddress(e.target.value)}
                          onPressEnter={handleConnect}
                        />
                        <Space wrap>
                          <span style={{ fontSize: 11, color: '#888' }}>SSH Port:</span>
                          <InputNumber
                            value={hkmcPort}
                            onChange={(v) => setHkmcPort(v || 22)}
                            min={1} max={65535}
                            style={{ width: 100 }}
                          />
                          <span style={{ fontSize: 11, color: '#888' }}>User:</span>
                          <Input value={sshUser} onChange={(e) => setSshUser(e.target.value)} style={{ width: 120 }} placeholder="root" />
                          <span style={{ fontSize: 11, color: '#888' }}>Password:</span>
                          <Input.Password value={sshPass} onChange={(e) => setSshPass(e.target.value)} style={{ width: 160 }} placeholder="(blank if none)" />
                        </Space>
                        <Space wrap>
                          <span style={{ fontSize: 11, color: '#888' }}>Resolution:</span>
                          <Select
                            style={{ width: 280 }}
                            value={mibResolution}
                            onChange={(v) => setMibResolution(v)}
                            options={(() => {
                              const opts = MIB_RESOLUTION_PRESETS.map(p => ({ label: p.label, value: p.value }));
                              if (!opts.find(o => o.value === mibResolution) && mibResolution) {
                                opts.push({ label: `사용자 정의 — ${mibResolution}`, value: mibResolution });
                              }
                              return opts;
                            })()}
                          />
                          <Input
                            placeholder="WxH 직접 입력"
                            value={mibResolution}
                            onChange={(e) => setMibResolution(e.target.value)}
                            style={{ width: 140 }}
                          />
                        </Space>
                        <div style={{ fontSize: 10, color: '#888' }}>
                          MIB는 캡처 시 PNG 실제 크기로 자동 보정됩니다. 등록 후 수정 모달에서도 변경/자동 감지 가능.
                        </div>
                      </>
                    )}

                    {!selectedModule && connectType === 'vision_camera' && (
                      <>
                        <Input
                          placeholder="MAC Address (예: AC4FFC011D82)"
                          value={vcMac}
                          onChange={(e) => setVcMac(e.target.value)}
                        />
                        <Input
                          placeholder="IP Address (예: 169.254.4.191)"
                          value={connectAddress}
                          onChange={(e) => setConnectAddress(e.target.value)}
                        />
                        <Input
                          placeholder="Model (예: exo264CGE)"
                          value={vcModel}
                          onChange={(e) => setVcModel(e.target.value)}
                        />
                        <Input
                          placeholder="Serial Number"
                          value={vcSerial}
                          onChange={(e) => setVcSerial(e.target.value)}
                        />
                        <Input
                          placeholder="Subnet Mask (예: 255.255.0.0)"
                          value={vcSubnet}
                          onChange={(e) => setVcSubnet(e.target.value)}
                        />
                      </>
                    )}

                    {!selectedModule && connectType === 'webcam' && (
                      <>
                        <div>
                          <span style={{ fontSize: 11, color: '#888', marginRight: 6 }}>{t('device.webcamIndex')}:</span>
                          <InputNumber
                            value={webcamIndex}
                            onChange={(v) => setWebcamIndex(v || 0)}
                            min={0} max={15}
                            style={{ width: 150 }}
                          />
                        </div>
                        <div style={{ display: 'flex', gap: 6 }}>
                          <div style={{ flex: 1 }}>
                            <span style={{ fontSize: 11, color: '#888', marginRight: 6 }}>{t('device.webcamWidth')}:</span>
                            <InputNumber
                              value={webcamWidth}
                              onChange={(v) => setWebcamWidth(v || 0)}
                              min={0} max={7680}
                              placeholder="auto"
                              style={{ width: 110 }}
                            />
                          </div>
                          <div style={{ flex: 1 }}>
                            <span style={{ fontSize: 11, color: '#888', marginRight: 6 }}>{t('device.webcamHeight')}:</span>
                            <InputNumber
                              value={webcamHeight}
                              onChange={(v) => setWebcamHeight(v || 0)}
                              min={0} max={4320}
                              placeholder="auto"
                              style={{ width: 110 }}
                            />
                          </div>
                        </div>
                        <div style={{ fontSize: 10, color: '#888' }}>
                          {t('device.webcamHint')}
                        </div>
                      </>
                    )}

                    {!selectedModule && connectType === 'ssh' && (
                      <>
                        <Input
                          placeholder={t('device.sshHostPlaceholder')}
                          value={connectAddress}
                          onChange={(e) => setConnectAddress(e.target.value)}
                          onPressEnter={handleConnect}
                        />
                        <div>
                          <span style={{ fontSize: 11, color: '#888', marginRight: 6 }}>{t('device.sshPort')}:</span>
                          <InputNumber
                            value={sshPort}
                            onChange={(v) => setSshPort(v || 22)}
                            min={1} max={65535}
                            style={{ width: 150 }}
                          />
                        </div>
                        <Input
                          placeholder={t('device.sshUserPlaceholder')}
                          value={sshUser}
                          onChange={(e) => setSshUser(e.target.value)}
                        />
                        <Input.Password
                          placeholder={t('device.sshPassPlaceholder')}
                          value={sshPass}
                          onChange={(e) => setSshPass(e.target.value)}
                          onPressEnter={handleConnect}
                        />
                        <Input
                          placeholder={t('device.sshKeyFilePlaceholder')}
                          value={sshKeyFile}
                          onChange={(e) => setSshKeyFile(e.target.value)}
                        />
                      </>
                    )}

                    {!selectedModule && connectType !== 'hkmc_agent' && connectType !== 'isap_agent' && connectType !== 'vision_camera' && connectType !== 'ssh' && (
                      <>
                        <Input
                          placeholder={connectType === 'adb' ? t('device.adbPlaceholder') : t('device.comPlaceholder')}
                          value={connectAddress}
                          onChange={(e) => setConnectAddress(e.target.value)}
                          onPressEnter={handleConnect}
                        />
                        {connectType === 'serial' && (
                          <div>
                            <span style={{ fontSize: 11, color: '#888', marginRight: 6 }}>Baudrate:</span>
                            <Select
                              value={baudrate}
                              onChange={setBaudrate}
                              style={{ width: 150 }}
                              options={baudrateOptions.map(b => ({ label: `${b}`, value: b }))}
                            />
                          </div>
                        )}
                      </>
                    )}

                    {/* Show extra connect_fields for serial modules too */}
                    {moduleConnType === 'serial' && connectFields.length > 0 && (
                      renderConnectFields(connectFields, extraFieldValues, setExtraFieldValues)
                    )}

                    <Button
                      type="primary"
                      icon={<PlusOutlined />}
                      onClick={handleConnect}
                      loading={connecting}
                      disabled={primaryProjectModelMissing}
                      title={primaryProjectModelMissing ? '프로젝트·모델을 먼저 선택하세요' : undefined}
                      block
                    >
                      {t('common.connect')}
                    </Button>
                  </Space>
                );
              })(),
            },
          ]}
        />
      </Modal>

      {/* 디바이스 수정 모달 */}
      <Modal
        title={t('device.editTitle')}
        open={editModalOpen}
        onCancel={() => setEditModalOpen(false)}
        onOk={handleSaveEdit}
        confirmLoading={editSaving}
        okText={t('common.save')}
        cancelText={t('common.cancel')}
      >
        {editDevice && (
          <Space direction="vertical" style={{ width: '100%' }}>
            {/* 디바이스 정보 */}
            <div style={{ background: '#fafafa', borderRadius: 6, padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 3 }}>
              <div style={{ display: 'flex', gap: 6, fontSize: 11, alignItems: 'center' }}>
                <span style={{ color: '#888', minWidth: 80 }}>Device ID:</span>
                <span>{editDevice.id}</span>
              </div>
              <div style={{ display: 'flex', gap: 6, fontSize: 11 }}>
                <span style={{ color: '#888', minWidth: 80 }}>Type:</span>
                <span>{editDevice.type}</span>
              </div>
              <div style={{ display: 'flex', gap: 6, fontSize: 11 }}>
                <span style={{ color: '#888', minWidth: 80 }}>{`${t('common.address')}:`}</span>
                <span>{editDevice.address || '-'}</span>
              </div>
              {editDevice.info?.resolution && (
                <div style={{ display: 'flex', gap: 6, fontSize: 11 }}>
                  <span style={{ color: '#888', minWidth: 80 }}>Resolution:</span>
                  <span>{editDevice.info.resolution.width}x{editDevice.info.resolution.height}</span>
                </div>
              )}
            </div>
            {/* 수정 가능한 필드 */}
            {editDevice.type === 'mib_agent' && (
              <div>
                <span style={{ fontSize: 11, color: '#888' }}>Resolution:</span>
                <Space.Compact style={{ width: '100%' }}>
                  <Select
                    style={{ flex: 1 }}
                    value={editMibResolution}
                    onChange={(v) => setEditMibResolution(v)}
                    options={(() => {
                      const opts = MIB_RESOLUTION_PRESETS.map(p => ({ label: p.label, value: p.value }));
                      if (editMibResolution && !opts.find(o => o.value === editMibResolution)) {
                        opts.push({ label: `사용자 정의 — ${editMibResolution}`, value: editMibResolution });
                      }
                      return opts;
                    })()}
                  />
                  <Input
                    style={{ width: 120 }}
                    placeholder="WxH"
                    value={editMibResolution}
                    onChange={(e) => setEditMibResolution(e.target.value)}
                  />
                  <Button
                    onClick={handleDetectMibResolution}
                    loading={detectingMibRes}
                    disabled={editDevice.status !== 'connected'}
                    title={editDevice.status !== 'connected' ? '디바이스 연결 후 사용 가능' : '실제 화면 캡처로 해상도 감지'}
                  >
                    자동 감지
                  </Button>
                </Space.Compact>
                <div style={{ fontSize: 10, color: '#888', marginTop: 2 }}>
                  자동 감지: 디바이스를 1회 캡처해 PNG 실제 크기를 읽어 자동 저장. 첫 캡처 시에도 자동 보정.
                </div>
              </div>
            )}
            {(editDevice.type === 'serial' || editDevice.info?.baudrate) && (
              <div>
                <span style={{ fontSize: 11, color: '#888' }}>Baudrate:</span>
                <Select
                  value={editBaudrate}
                  onChange={setEditBaudrate}
                  style={{ width: '100%' }}
                  options={baudrateOptions.map(b => ({ label: `${b}`, value: b }))}
                />
              </div>
            )}
            {editDevice.category === 'auxiliary' && (
              <div>
                <span style={{ fontSize: 11, color: '#888' }}>{`${t('device.module')}:`}</span>
                <Select
                  allowClear
                  placeholder={t('device.moduleSelectPlaceholder')}
                  value={editModule}
                  onChange={setEditModule}
                  style={{ width: '100%' }}
                  options={modules
                    .filter(m => isModuleVisible(m.name) || m.name === editModule)
                    .map(m => ({ label: m.label, value: m.name }))}
                />
              </div>
            )}
            {(() => {
              const fields = getModuleConnectFields(editModule);
              if (fields.length > 0) {
                return renderConnectFields(fields, editExtraFields, setEditExtraFields);
              }
              return null;
            })()}
          </Space>
        )}
      </Modal>

      {/* ForceIP Modal */}
      <Modal
        title={t('device.visionForceIpTitle')}
        open={!!forceIpModal}
        onCancel={() => setForceIpModal(null)}
        onOk={async () => {
          if (!forceIpModal) return;
          setForceIpLoading(true);
          try {
            await deviceApi.visionForceIp(forceIpModal.mac, forceIpAddr, forceIpSubnet, forceIpGateway);
            message.success(t('device.visionForceIpSuccess'));
            setForceIpModal(null);
            handleScan();
          } catch (e: any) {
            message.error(`${t('device.visionForceIpFailed')}: ${e.response?.data?.detail || e.message}`);
          }
          setForceIpLoading(false);
        }}
        confirmLoading={forceIpLoading}
        width={480}
      >
        {forceIpModal && (
          <Space direction="vertical" style={{ width: '100%' }}>
            <div><strong>MAC:</strong> {forceIpModal.mac}</div>
            {forceIpModal.currentIp && <div><strong>{t('device.visionCurrentIp')}:</strong> {forceIpModal.currentIp}</div>}
            {pcInterfaces.length > 0 && (
              <div style={{ fontSize: 11, color: '#888' }}>
                {t('device.pcInterfaces')}: {pcInterfaces.map(i => `${i.ip}/${i.prefix}`).join(', ')}
              </div>
            )}
            <Input addonBefore={t('device.visionNewIp')} value={forceIpAddr} onChange={e => setForceIpAddr(e.target.value)} placeholder="192.168.20.10" />
            <Input addonBefore={t('device.visionSubnet')} value={forceIpSubnet} onChange={e => setForceIpSubnet(e.target.value)} />
            <Input addonBefore={t('device.visionGateway')} value={forceIpGateway} onChange={e => setForceIpGateway(e.target.value)} />
          </Space>
        )}
      </Modal>
      {/* 스캔 설정 모달 */}
      <Modal
        title={t('device.scanSettings')}
        open={scanSettingsOpen}
        onCancel={() => setScanSettingsOpen(false)}
        onOk={saveScanSettings}
        okText={t('common.save')}
        cancelText={t('common.cancel')}
        width={620}
      >
        {(() => {
          // 스캔 설정 UI — 주/보조를 두 섹션으로 명확히 구분, 각 섹션은 연결방식→이름 순으로 정렬
          const builtinItems = [
            { key: 'adb',            label: 'ADB',            proto: 'USB/WiFi', editablePorts: false },
            { key: 'serial',         label: 'Serial',         proto: 'COM',      editablePorts: false },
            { key: 'hkmc',           label: 'HKMC',           proto: 'TCP',      editablePorts: false },
            { key: 'isap',           label: 'iSAP Agent',     proto: 'TCP',      editablePorts: false },
            { key: 'icas',           label: 'ICAS Agent',     proto: 'SSH',      editablePorts: false },
            { key: 'mib',            label: 'MIB Agent',      proto: 'SSH',      editablePorts: false },
            { key: 'dlt',            label: 'DLT',            proto: 'TCP',      editablePorts: false },
            { key: 'bench',          label: 'WoohyunBench',   proto: 'UDP',      editablePorts: false },
            { key: 'vision_camera',  label: 'Vision Camera',  proto: 'GigE',     editablePorts: false },
            { key: 'webcam',         label: 'Webcam',         proto: 'USB',      editablePorts: false },
            { key: 'ssh',            label: 'SSH',            proto: 'TCP',      editablePorts: false },
            { key: 'smartbench',     label: 'SmartBench',     proto: 'TCP',      editablePorts: false },
            { key: 'scar',           label: 'SCAR',           proto: 'HTTP',     editablePorts: false },
            { key: 'radmoon',        label: 'RAD_Moon (TH)',  proto: 'USB',      editablePorts: false },
          ];
          type BuiltinItem = typeof builtinItems[number];
          type CustomItem = { label: string; type: string; port: number; module?: string; enabled?: boolean; category?: ScanCategory; __idx: number; __kind: 'custom' };
          type BuiltinRow = BuiltinItem & { __kind: 'builtin' };
          type Row = BuiltinRow | CustomItem;

          const resolvedCategory = (r: Row): ScanCategory => {
            if (r.__kind === 'builtin') {
              const v = scanBuiltin[r.key] || {};
              return (v.category as ScanCategory) || _defaultCategoryForKey(r.key);
            }
            return (r.category as ScanCategory) || 'auxiliary';
          };
          const protoOf = (r: Row): string =>
            r.__kind === 'builtin' ? r.proto : r.type.toUpperCase();
          const labelOf = (r: Row): string => r.label;

          const allRows: Row[] = [
            ...builtinItems.map<BuiltinRow>(b => ({ ...b, __kind: 'builtin' as const })),
            ...scanCustom.map<CustomItem>((c, idx) => ({ ...c, __kind: 'custom' as const, __idx: idx })),
          ];
          const sortRows = (rows: Row[]) =>
            rows.slice().sort((a, b) => {
              const p = protoOf(a).localeCompare(protoOf(b));
              if (p !== 0) return p;
              return labelOf(a).localeCompare(labelOf(b));
            });
          const primaryRows = sortRows(allRows.filter(r => resolvedCategory(r) === 'primary'));
          const auxiliaryRows = sortRows(allRows.filter(r => resolvedCategory(r) === 'auxiliary'));

          const renderBuiltinRow = (item: BuiltinItem) => {
            const v = scanBuiltin[item.key] || { enabled: true, module: '' };
            const portsStr = v.ports && v.ports.length > 0 ? v.ports.join(',') : '';
            const portLabel = (item.key === 'ssh' || item.key === 'icas' || item.key === 'mib')
              ? String(v.port ?? 22)
              : (portsStr || '-');
            return (
              <tr key={`b_${item.key}`} style={{ borderBottom: '1px solid #f0f0f0' }}>
                <td style={{ padding: '4px' }}>
                  <Checkbox checked={v.enabled !== false}
                    onChange={e => setScanBuiltin({ ...scanBuiltin, [item.key]: { ...v, enabled: e.target.checked } })} />
                </td>
                <td style={{ padding: '4px' }}>{item.label}</td>
                <td style={{ padding: '4px' }}><Tag>{item.proto}</Tag></td>
                <td style={{ padding: '4px' }}>
                  <Select size="small"
                    disabled
                    value={(v.category as ScanCategory) || _defaultCategoryForKey(item.key)}
                    onChange={(cat) => setScanBuiltin({ ...scanBuiltin, [item.key]: { ...v, category: cat } })}
                    style={{ width: '100%' }}
                    options={[
                      { label: t('device.primary'), value: 'primary' },
                      { label: t('device.auxiliary'), value: 'auxiliary' },
                    ]}
                  />
                </td>
                <td style={{ padding: '4px' }}>
                  {item.editablePorts ? (
                    <Input
                      size="small"
                      disabled
                      value={portsStr}
                      placeholder={t('device.portsPlaceholder')}
                      onChange={e => {
                        const ports = e.target.value
                          .split(/[,\s]+/)
                          .map(p => parseInt(p.trim(), 10))
                          .filter(p => !isNaN(p) && p > 0 && p < 65536);
                        setScanBuiltin({ ...scanBuiltin, [item.key]: { ...v, ports } });
                      }}
                    />
                  ) : (item.key === 'smartbench' || item.key === 'bench' || item.key === 'scar') ? (
                    <Space.Compact size="small" style={{ width: '100%' }}>
                      <Input
                        size="small"
                        disabled
                        value={v.host ?? (item.key === 'bench' ? '192.168.1.101' : item.key === 'scar' ? 'localhost' : '192.167.0.5')}
                        placeholder="host"
                        style={{ flex: 1 }}
                        onChange={e => setScanBuiltin({ ...scanBuiltin, [item.key]: { ...v, host: e.target.value } })}
                      />
                      <InputNumber
                        size="small"
                        disabled
                        min={1} max={65535}
                        value={v.port ?? (item.key === 'bench' ? 25000 : item.key === 'scar' ? 8081 : 8000)}
                        placeholder="port"
                        style={{ width: 80 }}
                        onChange={p => setScanBuiltin({ ...scanBuiltin, [item.key]: { ...v, port: p ?? (item.key === 'bench' ? 25000 : item.key === 'scar' ? 8081 : 8000) } })}
                      />
                    </Space.Compact>
                  ) : (item.key === 'ssh' || item.key === 'icas' || item.key === 'mib') ? (
                    <InputNumber
                      size="small"
                      disabled
                      min={1} max={65535}
                      value={v.port ?? 22}
                      style={{ width: '100%' }}
                      onChange={p => setScanBuiltin({ ...scanBuiltin, [item.key]: { ...v, port: p ?? 22 } })}
                    />
                  ) : portLabel}
                </td>
                <td style={{ padding: '4px' }}>
                  <Select size="small" allowClear disabled placeholder="-" value={v.module || undefined}
                    onChange={val => setScanBuiltin({ ...scanBuiltin, [item.key]: { ...v, module: val || '' } })}
                    style={{ width: '100%' }} options={visibleModules.map(m => ({ label: m.label, value: m.name }))} />
                </td>
                <td></td>
              </tr>
            );
          };

          const renderCustomRow = (entry: CustomItem) => {
            const idx = entry.__idx;
            return (
              <tr key={`c_${idx}`} style={{ borderBottom: '1px solid #f0f0f0' }}>
                <td style={{ padding: '4px' }}>
                  <Checkbox checked={entry.enabled !== false}
                    onChange={e => { const n = [...scanCustom]; n[idx] = { ...scanCustom[idx], enabled: e.target.checked }; setScanCustom(n); }} />
                </td>
                <td style={{ padding: '4px' }}>{entry.label}</td>
                <td style={{ padding: '4px' }}><Tag color={entry.type === 'udp' ? 'orange' : 'blue'}>{entry.type.toUpperCase()}</Tag></td>
                <td style={{ padding: '4px' }}>
                  <Select size="small"
                    value={(entry.category as ScanCategory) || 'auxiliary'}
                    onChange={(cat) => { const n = [...scanCustom]; n[idx] = { ...scanCustom[idx], category: cat }; setScanCustom(n); }}
                    style={{ width: '100%' }}
                    options={[
                      { label: t('device.primary'), value: 'primary' },
                      { label: t('device.auxiliary'), value: 'auxiliary' },
                    ]}
                  />
                </td>
                <td style={{ padding: '4px' }}>{entry.port}</td>
                <td style={{ padding: '4px' }}>
                  <Select size="small" allowClear placeholder="-" value={entry.module || undefined}
                    onChange={val => { const n = [...scanCustom]; n[idx] = { ...scanCustom[idx], module: val || '' }; setScanCustom(n); }}
                    style={{ width: '100%' }} options={visibleModules.map(m => ({ label: m.label, value: m.name }))} />
                </td>
                <td style={{ padding: '4px' }}>
                  <Button size="small" type="text" danger icon={<DeleteOutlined />}
                    onClick={() => setScanCustom(scanCustom.filter((_, i) => i !== idx))} />
                </td>
              </tr>
            );
          };

          const renderRow = (r: Row) =>
            r.__kind === 'builtin' ? renderBuiltinRow(r) : renderCustomRow(r);

          const renderSection = (title: string, rows: Row[], color: string) => (
            <tbody>
              <tr>
                <td colSpan={7} style={{ padding: '8px 4px 4px', fontWeight: 600, fontSize: 11, color, borderTop: '1px solid #d9d9d9' }}>
                  <Tag color={color === '#1677ff' ? 'blue' : 'default'}>{title}</Tag>
                  <span style={{ color: '#888', fontWeight: 400, marginLeft: 3 }}>({rows.length})</span>
                </td>
              </tr>
              {rows.length === 0 ? (
                <tr><td colSpan={7} style={{ padding: '6px 8px', color: '#bbb', fontSize: 11 }}>—</td></tr>
              ) : (
                rows.map(r => renderRow(r))
              )}
            </tbody>
          );

          return (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
              <thead>
                <tr style={{ borderBottom: '2px solid #d9d9d9', textAlign: 'left' }}>
                  <th style={{ padding: '6px 4px', width: 40 }}></th>
                  <th style={{ padding: '6px 4px' }}>{t('common.name')}</th>
                  <th style={{ padding: '6px 4px', width: 80 }}>{t('device.protocol')}</th>
                  <th style={{ padding: '6px 4px', width: 100 }}>{t('device.category')}</th>
                  <th style={{ padding: '6px 4px', width: 110 }}>{t('device.port')}</th>
                  <th style={{ padding: '6px 4px', width: 140 }}>{t('device.module')}</th>
                  <th style={{ padding: '6px 4px', width: 40 }}></th>
                </tr>
              </thead>
              {renderSection(t('device.primaryDevice'), primaryRows, '#1677ff')}
              {renderSection(t('device.auxiliaryDevice'), auxiliaryRows, '#666')}
              <tbody>
                {/* 추가 행 */}
                <tr style={{ borderTop: '2px solid #d9d9d9' }}>
                  <td></td>
                  <td style={{ padding: '4px' }}>
                    <Input size="small" placeholder={t('device.customLabel')} value={newCustomLabel}
                      onChange={e => setNewCustomLabel(e.target.value)} />
                  </td>
                  <td style={{ padding: '4px' }}>
                    <Select size="small" value={newCustomType} onChange={setNewCustomType} style={{ width: '100%' }}
                      options={[{ label: 'TCP', value: 'tcp' }, { label: 'UDP', value: 'udp' }]} />
                  </td>
                  <td style={{ padding: '4px' }}>
                    <Select size="small" value={newCustomCategory} onChange={setNewCustomCategory} style={{ width: '100%' }}
                      options={[{ label: t('device.primary'), value: 'primary' }, { label: t('device.auxiliary'), value: 'auxiliary' }]} />
                  </td>
                  <td style={{ padding: '4px' }}>
                    <InputNumber size="small" placeholder="Port" value={newCustomPort}
                      onChange={v => setNewCustomPort(v)} min={1} max={65535} style={{ width: '100%' }} />
                  </td>
                  <td style={{ padding: '4px' }}>
                    <Select size="small" allowClear placeholder="Module" value={newCustomModule || undefined}
                      onChange={v => setNewCustomModule(v || '')} style={{ width: '100%' }}
                      options={visibleModules.map(m => ({ label: m.label, value: m.name }))} />
                  </td>
                  <td style={{ padding: '4px' }}>
                    <Button size="small" type="primary" icon={<PlusOutlined />}
                      onClick={addCustomScan} disabled={!newCustomPort} />
                  </td>
                </tr>
              </tbody>
            </table>
          );
        })()}
      </Modal>
    </div>
  );
}
