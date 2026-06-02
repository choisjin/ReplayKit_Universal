import axios from 'axios';

const api = axios.create({
  baseURL: '/api',
});

// Device APIs
export const deviceApi = {
  list: () => api.get('/device/list'),
  getInfo: (deviceId: string) => api.get(`/device/info/${deviceId}`),
  screenshot: (deviceId: string, screenType?: string, fmt: 'jpeg' | 'png' = 'jpeg') => api.get(`/device/screenshot/${deviceId}`, { params: { fmt, screen_type: screenType || 'front_center' } }),
  scan: () => api.get('/device/scan'),
  getScanSettings: () => api.get('/device/scan-settings'),
  saveScanSettings: (settings: any) => api.post('/device/scan-settings', settings),
  getCatalog: () => api.get('/device/catalog'),
  saveCatalog: (catalog: any) => api.post('/device/catalog', catalog),
  connect: (type: string, address: string, baudrate?: number, name?: string, category?: string, module?: string, connect_type?: string, extra_fields?: Record<string, any>, device_id?: string, port?: number, device_model?: string) =>
    api.post('/device/connect', { type, address, baudrate, name, category, module, connect_type, extra_fields, device_id, port, device_model }),
  listHkmcKeys: (deviceId?: string) =>
    api.get('/device/hkmc-keys', { params: deviceId ? { device_id: deviceId } : {} }),
  updateHkmcKeys: (deviceId: string, keys: Record<string, { cmd?: number; key?: number; dial?: boolean; visible?: boolean }>) =>
    api.post('/device/hkmc-keys', { device_id: deviceId, keys }),
  listHkmc5thWideKeys: (deviceId?: string) =>
    api.get('/device/hkmc5th-wide-keys', { params: deviceId ? { device_id: deviceId } : {} }),
  updateHkmc5thWideKeys: (deviceId: string, keys: Record<string, { cmd?: number; key?: number; dial?: boolean; visible?: boolean }>) =>
    api.post('/device/hkmc5th-wide-keys', { device_id: deviceId, keys }),
  listIsapKeys: (deviceId?: string) =>
    api.get('/device/isap-keys', { params: deviceId ? { device_id: deviceId } : {} }),
  updateIsapKeys: (deviceId: string, keys: Record<string, { cmd?: number; key?: number; dial?: boolean; visible?: boolean }>) =>
    api.post('/device/isap-keys', { device_id: deviceId, keys }),
  listIcasKeys: (deviceId?: string) =>
    api.get('/device/icas-keys', { params: deviceId ? { device_id: deviceId } : {} }),
  updateIcasKeys: (deviceId: string, keys: Record<string, { class?: 'short' | 'long'; key?: number; visible?: boolean }>) =>
    api.post('/device/icas-keys', { device_id: deviceId, keys }),
  listMibKeys: (deviceId?: string) =>
    api.get('/device/mib-keys', { params: deviceId ? { device_id: deviceId } : {} }),
  updateMibKeys: (deviceId: string, keys: Record<string, { class?: 'short' | 'long'; key?: number; visible?: boolean }>) =>
    api.post('/device/mib-keys', { device_id: deviceId, keys }),
  detectMibResolution: (deviceId: string) =>
    api.post('/device/mib/detect_resolution', { device_id: deviceId }),
  disconnect: (deviceId: string) => api.post('/device/disconnect', { address: deviceId }),
  updateDevice: (device_id: string, updates: Record<string, any>) =>
    api.post('/device/update', { device_id, ...updates }),
  reorderDevices: (prefix: string, ordered_ids: string[]) =>
    api.post('/device/reorder', { prefix, ordered_ids }),
  adbRestart: () => api.post('/device/adb-restart'),
  input: (deviceId: string, action: string, params: Record<string, any>) =>
    api.post('/device/input', { device_id: deviceId, action, params }),
  listModules: () => api.get('/device/modules'),
  getModuleFunctions: (moduleName: string) => api.get(`/device/modules/${moduleName}/functions`),
  localInterfaces: () => api.get('/device/local-interfaces'),
  visionForceIp: (mac: string, ip: string, subnet: string, gateway: string) =>
    api.post('/device/vision-force-ip', { mac, ip, subnet, gateway }),
  dltViewerLaunch: (projectFile?: string, logFile?: string) =>
    api.post('/device/dlt-viewer/launch', { project_file: projectFile || '', log_file: logFile || '' }),
  dltViewerClose: () => api.post('/device/dlt-viewer/close'),
  connectRegistered: (deviceIds?: string[]) =>
    api.post('/device/connect-registered', { device_ids: deviceIds || [] }),
  disconnectOne: (deviceId: string) =>
    api.post('/device/disconnect-one', { device_id: deviceId }),
  getWebcamExposure: (deviceId: string) =>
    api.get(`/device/webcam-exposure/${deviceId}`),
  setWebcamExposure: (deviceId: string, value?: number, auto?: boolean) =>
    api.post(`/device/webcam-exposure/${deviceId}`, { value, auto }),
  // WinControl
  winListProcesses: () => api.get('/device/wincontrol/processes'),
  winStatus: () => api.get('/device/wincontrol/status'),
  winAttach: (hwnd: number) => api.post('/device/wincontrol/attach', { hwnd }),
  winDetach: () => api.post('/device/wincontrol/detach'),
  winResize: (clientW: number, clientH: number) =>
    api.post('/device/wincontrol/resize', { client_w: clientW, client_h: clientH }),
};

// Scenario APIs
export const scenarioApi = {
  list: () => api.get('/scenario/list'),
  get: (name: string) => api.get(`/scenario/${name}`),
  delete: (name: string) => api.delete(`/scenario/${name}`),
  update: (name: string, data: any) => api.put(`/scenario/${name}`, data),
  rename: (name: string, newName: string) => api.post(`/scenario/${name}/rename`, { new_name: newName }),
  startRecording: (name: string, description?: string) =>
    api.post('/scenario/record/start', { name, description }),
  resumeRecording: (name: string) =>
    api.post('/scenario/record/resume', { name }),
  addStep: (step: any) => api.post('/scenario/record/step', step),
  deleteStep: (stepIndex: number) => api.post('/scenario/record/delete-step', { step_index: stepIndex }),
  syncSteps: (scenarioName: string, steps: any[]) =>
    api.post('/scenario/record/sync-steps', { scenario_name: scenarioName, steps }),
  stopRecording: () => api.post('/scenario/record/stop'),
  recordingStatus: () => api.get('/scenario/record/status'),
  play: (name: string, verify = true) =>
    api.post(`/scenario/${name}/play`, { verify }),
  stopPlayback: () => api.post('/scenario/playback/stop'),
  playbackStatus: () => api.get('/scenario/playback/status'),
  saveExpectedImage: (scenarioName: string, stepIndex: number, imageBase64: string, crop?: { x: number; y: number; width: number; height: number }, compareMode?: string, cropLabel?: string, preserveCrops?: boolean, screenType?: string) =>
    api.post('/scenario/record/save-expected-image', { scenario_name: scenarioName, step_index: stepIndex, image_base64: imageBase64, crop, compare_mode: compareMode, crop_label: cropLabel, preserve_crops: preserveCrops || false, screen_type: screenType }),
  captureExpectedImage: (scenarioName: string, stepIndex: number, deviceId: string, crop?: { x: number; y: number; width: number; height: number }, compareMode?: string, cropLabel?: string, screenType?: string, preserveCrops?: boolean) =>
    api.post('/scenario/record/capture-expected-image', { scenario_name: scenarioName, step_index: stepIndex, device_id: deviceId, crop, compare_mode: compareMode, crop_label: cropLabel, screen_type: screenType || 'front_center', preserve_crops: preserveCrops || false }),
  removeExpectedImage: (scenarioName: string, stepIndex: number) =>
    api.post('/scenario/record/remove-expected-image', { scenario_name: scenarioName, step_index: stepIndex }),
  importSteps: (targetName: string, sourceName: string, stepIndices: number[], move: boolean = false) =>
    api.post('/scenario/record/import-steps', { target_name: targetName, source_name: sourceName, step_indices: stepIndices, move }),
  removeCrop: (scenarioName: string, stepIndex: number, cropIndex: number) =>
    api.post('/scenario/record/remove-crop', { scenario_name: scenarioName, step_index: stepIndex, crop_index: cropIndex }),
  cropFromExpected: (scenarioName: string, stepIndex: number, crop: { x: number; y: number; width: number; height: number }, cropLabel?: string, replaceIndex?: number) =>
    api.post('/scenario/record/crop-from-expected', { scenario_name: scenarioName, step_index: stepIndex, crop, crop_label: cropLabel || '', replace_index: replaceIndex }),
  updateStep: (scenarioName: string, stepIndex: number, updates: Record<string, any>) =>
    api.post('/scenario/record/update-step', { scenario_name: scenarioName, step_index: stepIndex, updates }),
  testStep: (scenarioName: string, stepIndex: number, stepData?: any, overrides?: { screenshotDeviceId?: string; screenType?: string }) =>
    api.post('/scenario/test-step', {
      scenario_name: scenarioName,
      step_index: stepIndex,
      step_data: stepData,
      screenshot_device_id_override: overrides?.screenshotDeviceId,
      screen_type_override: overrides?.screenType,
    }),
  getCmdResult: (taskId: string) => api.get(`/scenario/cmd-result/${taskId}`),
  cancelCmdTask: (taskId: string) => api.delete(`/scenario/cmd-result/${taskId}`),
  cleanTestScreenshots: (scenarioName: string) => api.post(`/scenario/clean-test-screenshots?scenario_name=${encodeURIComponent(scenarioName)}`),
  recordImageTap: (
    scenarioName: string,
    deviceId: string,
    imageBase64: string,
    crop: { x: number; y: number; width: number; height: number },
    similarity: number,
    screenType?: string,
    delayAfterMs?: number,
    description?: string,
    xOffset?: number,
  ) => api.post('/scenario/record/image-tap', {
    scenario_name: scenarioName,
    device_id: deviceId,
    image_base64: imageBase64,
    crop,
    similarity,
    screen_type: screenType,
    delay_after_ms: delayAfterMs ?? 3000,
    description: description ?? '',
    x_offset: xOffset ?? 0,
  }),
  updateImageTap: (
    scenarioName: string,
    stepIndex: number,
    imageBase64: string,
    crop: { x: number; y: number; width: number; height: number },
    similarity: number,
    screenType?: string,
    deviceId?: string,
    xOffset?: number,
  ) => api.post('/scenario/record/update-image-tap', {
    scenario_name: scenarioName,
    step_index: stepIndex,
    image_base64: imageBase64,
    crop,
    similarity,
    screen_type: screenType,
    device_id: deviceId,
    x_offset: xOffset,
  }),
  // Folders
  getFolders: () => api.get('/scenario/folders'),
  createFolder: (name: string) => api.post('/scenario/folders/create', { name }),
  renameFolder: (oldName: string, newName: string) => api.post('/scenario/folders/rename', { old_name: oldName, new_name: newName }),
  deleteFolder: (name: string) => api.post('/scenario/folders/delete', { name }),
  moveToFolder: (scenarioName: string, folderName: string | null) => api.post('/scenario/folders/move', { scenario_name: scenarioName, folder_name: folderName }),
  // Group Folders (그룹을 폴더로 묶기)
  getGroupFolders: () => api.get('/scenario/group-folders'),
  createGroupFolder: (name: string) => api.post('/scenario/group-folders/create', { name }),
  renameGroupFolder: (oldName: string, newName: string) => api.post('/scenario/group-folders/rename', { old_name: oldName, new_name: newName }),
  deleteGroupFolder: (name: string) => api.post('/scenario/group-folders/delete', { name }),
  moveGroupToFolder: (groupName: string, folderName: string | null) => api.post('/scenario/group-folders/move', { group_name: groupName, folder_name: folderName }),
  // Groups
  getGroups: () => api.get('/scenario/groups'),
  createGroup: (name: string) => api.post('/scenario/groups', { name }),
  renameGroup: (oldName: string, newName: string) => api.put('/scenario/groups', { old_name: oldName, new_name: newName }),
  deleteGroup: (groupName: string) => api.delete(`/scenario/groups/${groupName}`),
  addToGroup: (groupName: string, scenarioName: string) =>
    api.post(`/scenario/groups/${groupName}/add`, { scenario_name: scenarioName }),
  removeFromGroup: (groupName: string, index: number) =>
    api.post(`/scenario/groups/${groupName}/remove`, { index }),
  reorderGroup: (groupName: string, orderedIndices: number[]) =>
    api.post(`/scenario/groups/${groupName}/reorder`, { ordered_indices: orderedIndices }),
  updateGroupJumps: (groupName: string, index: number, on_pass_goto: { scenario: number; step: number } | null, on_fail_goto: { scenario: number; step: number } | null) =>
    api.post(`/scenario/groups/${groupName}/jumps`, { index, on_pass_goto, on_fail_goto }),
  updateGroupStepJumps: (groupName: string, index: number, stepId: number, on_pass_goto: { scenario: number; step: number } | null, on_fail_goto: { scenario: number; step: number } | null) =>
    api.post(`/scenario/groups/${groupName}/step-jumps`, { index, step_id: stepId, on_pass_goto, on_fail_goto }),
  updateGroupPlayCount: (groupName: string, index: number, playCount: number) =>
    api.post(`/scenario/groups/${groupName}/play-count`, { index, play_count: playCount }),
  // Copy
  copy: (name: string, targetName: string) =>
    api.post(`/scenario/copy/${name}`, { target_name: targetName }),
  // Export / Import
  exportZip: (scenarios: string[], groups: string[], includeAll: boolean = false) =>
    api.post('/scenario/export', { scenarios, groups, include_all: includeAll }, { responseType: 'blob' }),
  importPreview: (file: File) => {
    const form = new FormData();
    form.append('file', file);
    return api.post('/scenario/import/preview', form);
  },
  importApply: (file: File, resolutions: object) => {
    const form = new FormData();
    form.append('file', file);
    form.append('resolutions', JSON.stringify(resolutions));
    return api.post('/scenario/import/apply', form);
  },
};

// Results APIs
export const resultsApi = {
  list: () => api.get('/results/list'),
  get: (filename: string) => api.get(`/results/${filename}`),
  delete: (filename: string) => api.delete(`/results/${filename}`),
  exportExcel: (filename: string) =>
    api.get(`/results/export/${filename}`, { responseType: 'blob' }),
  // Webcam recordings
  uploadRecording: (blob: Blob, resultFilename: string, repeatIndex: number) => {
    const form = new FormData();
    form.append('file', blob, 'webcam.webm');
    return api.post(`/results/webcam-upload?result_filename=${encodeURIComponent(resultFilename)}&repeat_index=${repeatIndex}`, form);
  },
  listRecordings: (resultFilename: string) =>
    api.get(`/results/recordings-for/${encodeURIComponent(resultFilename)}`),
  deleteRecording: (filename: string) =>
    api.delete(`/results/recordings/${filename}`),
  trimRecording: (filename: string, start: number, end: number) =>
    api.post(`/results/recordings/${filename}/trim?start=${start}&end=${end}`),
  exportBundle: (filename: string, exportPath?: string) =>
    exportPath
      ? api.post(`/results/export-bundle/${filename}`, null, { params: { export_path: exportPath } })
      : api.post(`/results/export-bundle/${filename}`, null, { responseType: 'blob' }),
  updateStepResult: (filename: string, stepIndex: number, message: string, status?: string) =>
    api.post(`/results/update-step/${filename}`, { step_index: stepIndex, message, ...(status ? { status } : {}) }),
  openFolder: (filename: string) =>
    api.post('/results/open-folder', { filename }),
  migrateLegacy: () => api.post('/results/migrate-legacy'),
};

// Server management APIs
export const serverApi = {
  restart: () => api.post('/settings/server-restart'),
  updateAndRestart: () => api.post('/settings/update-and-restart'),
  diskUsage: () => api.get('/settings/disk-usage'),
  openResultsFolder: () => api.post('/settings/open-results-folder'),
  gitLog: (limit?: number, fetch?: boolean) => api.get('/settings/git-log', { params: { limit: limit || 100, fetch: fetch || false } }),
  launcherLog: (lines?: number, date?: string, source?: string) => api.get('/settings/launcher-log', { params: { lines: lines || 500, date: date || '', source: source || '' } }),
  powerStatus: () => api.get('/settings/power-status'),
  memoryUsage: () => api.get('/settings/memory-usage'),
  resetMemoryPeak: () => api.post('/settings/memory-usage/reset-peak'),
  getVersion: () => api.get('/settings/version'),
};

// Compositor APIs (다중 캡처 합성 녹화)
export interface CompositorSourceConfig {
  id: string;
  type: 'webcam' | 'window';
  label?: string;
  x: number;
  y: number;
  width: number;
  height: number;
  crop?: { x: number; y: number; w: number; h: number } | null;
  z_order?: number;
  opacity?: number;
  // webcam 전용
  device_index?: number;
  capture_width?: number;
  capture_height?: number;
  // window 전용
  process_name?: string;
  title_pattern?: string;
  hwnd?: number;
  capture_fps?: number;
}

export interface CompositorCanvasConfig {
  width: number;
  height: number;
  fps: number;
  background?: string;
  show_labels?: boolean;
  show_timestamp?: boolean;
}

export interface CompositorLayout {
  canvas: CompositorCanvasConfig;
  sources: CompositorSourceConfig[];
}

export const compositorApi = {
  listWebcamSources: () => api.get('/compositor/sources/webcams'),
  listWindowSources: () => api.get('/compositor/sources/windows'),
  configure: (layout: CompositorLayout) => api.post('/compositor/configure', layout),
  getLayout: () => api.get('/compositor/layout'),
  startCapture: () => api.post('/compositor/capture/start'),
  stopCapture: () => api.post('/compositor/capture/stop'),
  status: () => api.get('/compositor/status'),
  recordStart: (output_path: string) => api.post('/compositor/record/start', { output_path }),
  recordStop: () => api.post('/compositor/record/stop'),
  recordPause: () => api.post('/compositor/record/pause'),
  recordResume: () => api.post('/compositor/record/resume'),
  // Presets
  listPresets: () => api.get('/compositor/presets'),
  savePreset: (name: string, layout: CompositorLayout) =>
    api.post('/compositor/presets', { name, layout }),
  deletePreset: (name: string) => api.delete(`/compositor/presets/${encodeURIComponent(name)}`),
  activatePreset: (name?: string, enabled?: boolean) =>
    api.post('/compositor/presets/activate', { name, enabled }),
};

export default api;
