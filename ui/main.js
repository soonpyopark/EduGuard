'use strict';

// EduGuard Electron 메인 프로세스.
// - 엔진(EduGuardCore.exe / python main.py --backend)을 찾거나 실행하고, runtime.json 의 포트·토큰으로 로컬 API 를 호출한다.
// - 렌더러는 Node 권한이 없고, preload 가 노출한 IPC 만 사용한다. API 호출은 모두 메인 프로세스를 거친다.

const { app, BrowserWindow, Tray, Menu, ipcMain, dialog, shell, nativeImage } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const { spawn } = require('node:child_process');

const APP_ID = 'EduGuard.FocusedLearning.1';
const POLL_MS = 700;
const argv = process.argv.slice(1);

let win = null;
let tray = null;
let backend = null; // { port, token }
let connected = false;
let quitting = false;
let lastId = 0;
let lastState = null;
let failures = 0;
let trayHintShown = false;
let trayStatus = '';
let pollTimer = null;

// ------------------------------------------------------------------ 경로 / 엔진 실행 명령
function homeDir() {
  return app.isPackaged ? path.dirname(process.execPath) : path.resolve(__dirname, '..');
}

function assetPath(name) {
  return app.isPackaged ? path.join(process.resourcesPath, name) : path.join(__dirname, '..', 'assets', name);
}

function coreCommand(extraArgs) {
  if (app.isPackaged) {
    return { cmd: path.join(homeDir(), 'EduGuardCore.exe'), args: extraArgs };
  }
  const args = [path.join(homeDir(), 'main.py'), ...extraArgs];
  if (process.env.EDUGUARD_NO_ADMIN === '1') args.push('--no-admin');
  return { cmd: process.env.EDUGUARD_PYTHON || 'python', args };
}

function spawnCore(extraArgs, { detached = true } = {}) {
  const { cmd, args } = coreCommand(extraArgs);
  const child = spawn(cmd, args, { cwd: homeDir(), detached, stdio: 'ignore', windowsHide: true });
  child.on('error', () => {});
  if (detached) child.unref();
  return child;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ------------------------------------------------------------------ 엔진 API
async function request(method, apiPath, body, timeoutMs = 20000) {
  if (!backend) {
    const err = new Error('EduGuard 엔진에 연결되어 있지 않습니다.');
    err.status = 503;
    throw err;
  }
  const res = await fetch(`http://127.0.0.1:${backend.port}${apiPath}`, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-EduGuard-Token': backend.token },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(timeoutMs),
  });
  let data = {};
  try {
    data = await res.json();
  } catch {
    // 본문 없는 오류 응답
  }
  if (!res.ok || data.ok === false) {
    const err = new Error(data.error || `HTTP ${res.status}`);
    err.status = data.status || res.status;
    throw err;
  }
  return data;
}

function readRuntime() {
  try {
    const rt = JSON.parse(fs.readFileSync(path.join(homeDir(), 'runtime.json'), 'utf8'));
    if (rt && rt.port && rt.token) return { port: Number(rt.port), token: String(rt.token) };
  } catch {
    // 아직 없거나 읽기 권한이 없음
  }
  return null;
}

async function tryAttach() {
  const rt = readRuntime();
  if (!rt) return false;
  const previous = backend;
  backend = rt;
  try {
    await request('GET', '/api/ping', undefined, 3000);
    if (!previous || previous.port !== rt.port || previous.token !== rt.token) lastId = 0;
    return true;
  } catch {
    backend = previous;
    return false;
  }
}

async function waitForAttach(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await tryAttach()) return true;
    await sleep(600);
  }
  return false;
}

async function connectBackend({ allowSpawn, attachWaitMs = 0 }) {
  if (await tryAttach()) return true;
  if (attachWaitMs && (await waitForAttach(attachWaitMs))) return true;
  if (!allowSpawn) return false;
  spawnCore(['--backend']);
  // UAC 승인을 기다릴 수 있으므로 넉넉히 기다린다.
  return waitForAttach(120000);
}

function setConnected(value, message) {
  if (connected === value && !message) return;
  connected = value;
  send('backend', { connected: value, message: message || '' });
}

async function pollOnce() {
  const reset = lastId === 0;
  try {
    const data = await request('GET', `/api/poll?since=${lastId}`, undefined, 5000);
    failures = 0;
    setConnected(true);
    lastId = data.last_id;
    lastState = data.state;
    if (!reset) {
      for (const ev of data.events) {
        if (ev.kind === 'notify') showBalloon(ev.detail);
      }
    }
    updateTray(data.state);
    send('poll', { ...data, reset });
  } catch {
    failures += 1;
    if (failures >= 3) {
      setConnected(false, '엔진 응답이 없습니다. 다시 연결하는 중…');
      if (await tryAttach()) failures = 0;
    }
  } finally {
    if (!quitting) pollTimer = setTimeout(pollOnce, POLL_MS);
  }
}

// ------------------------------------------------------------------ 창 / 트레이
function send(channel, payload) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}

function showWindow() {
  if (!win) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function requestExit() {
  showWindow();
  send('request-exit');
}

function createWindow(startHidden) {
  win = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 980,
    minHeight: 680,
    show: false,
    title: 'EduGuard',
    icon: assetPath('eduguard.ico'),
    backgroundColor: '#f1f5f9',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });
  win.removeMenu();
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  win.once('ready-to-show', () => {
    if (!startHidden) win.show();
  });
  win.webContents.on('did-finish-load', () => {
    lastId = 0; // 렌더러가 준비되기 전에 보낸 로그는 유실되므로 처음부터 다시 받는다.
  });
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', (e) => e.preventDefault());
  win.on('close', (e) => {
    if (quitting) return;
    e.preventDefault();
    const closeToTray = lastState ? lastState.close_to_tray : true;
    if (closeToTray || !connected) {
      win.hide();
      if (!trayHintShown && (!lastState || lastState.tray_notifications)) {
        trayHintShown = true;
        showBalloon('트레이에서 계속 실행 중입니다. 아이콘을 눌러 다시 열 수 있습니다.');
      }
    } else {
      requestExit();
    }
  });
}

function buildTrayMenu() {
  return Menu.buildFromTemplate([
    { label: 'EduGuard 열기', click: showWindow },
    { label: trayStatus || '상태 확인 중…', enabled: false },
    { type: 'separator' },
    { label: '종료 (비밀번호)', click: requestExit },
  ]);
}

function createTray() {
  tray = new Tray(nativeImage.createFromPath(assetPath('eduguard.ico')));
  tray.setToolTip('EduGuard');
  tray.on('click', showWindow);
  tray.setContextMenu(buildTrayMenu());
}

function updateTray(state) {
  const text = `상태: ${state.status_text}`;
  if (!tray || text === trayStatus) return;
  trayStatus = text;
  tray.setToolTip(`EduGuard - ${state.status_text}`);
  tray.setContextMenu(buildTrayMenu());
}

function showBalloon(content) {
  if (!tray || !content) return;
  tray.displayBalloon({ iconType: 'info', title: 'EduGuard', content: String(content) });
}

// ------------------------------------------------------------------ IPC
const API_PATH_RE = /^\/api\/[a-z0-9\-/]+(\?[a-z0-9=&_\-]*)?$/i;

ipcMain.handle('api', async (_e, method, apiPath, body) => {
  if (!['GET', 'POST'].includes(method) || typeof apiPath !== 'string' || !API_PATH_RE.test(apiPath)) {
    return { ok: false, status: 400, error: '잘못된 요청입니다.' };
  }
  try {
    return await request(method, apiPath, body);
  } catch (err) {
    return { ok: false, status: err.status || 0, error: err.message || String(err) };
  }
});

ipcMain.handle('app:info', () => {
  let logo = '';
  try {
    logo = nativeImage.createFromPath(assetPath('eduguard.png')).toDataURL();
  } catch {
    // 아이콘 없이도 동작
  }
  return { logo, connected };
});

ipcMain.handle('keywords:export', async (_e, keywords) => {
  const { canceled, filePath } = await dialog.showSaveDialog(win, {
    title: '허용 키워드 내보내기',
    defaultPath: 'eduguard-keywords.json',
    filters: [
      { name: 'JSON 파일', extensions: ['json'] },
      { name: '텍스트 파일', extensions: ['txt'] },
    ],
  });
  if (canceled || !filePath) return { ok: false, canceled: true };
  const list = Array.isArray(keywords) ? keywords.map(String) : [];
  try {
    const text = filePath.toLowerCase().endsWith('.txt')
      ? `${list.join('\n')}\n`
      : JSON.stringify({ keywords: list }, null, 2);
    fs.writeFileSync(filePath, text, 'utf8');
    return { ok: true, path: filePath };
  } catch (err) {
    return { ok: false, error: `내보내기 실패: ${err.message}` };
  }
});

ipcMain.handle('keywords:import', async () => {
  const { canceled, filePaths } = await dialog.showOpenDialog(win, {
    title: '허용 키워드 가져오기',
    properties: ['openFile'],
    filters: [
      { name: 'JSON/텍스트 파일', extensions: ['json', 'txt'] },
      { name: '모든 파일', extensions: ['*'] },
    ],
  });
  if (canceled || !filePaths.length) return { ok: false, canceled: true };
  const filePath = filePaths[0];
  try {
    const raw = fs.readFileSync(filePath, 'utf8').replace(/^\uFEFF/, '');
    let keywords;
    if (filePath.toLowerCase().endsWith('.json')) {
      const doc = JSON.parse(raw);
      keywords = Array.isArray(doc) ? doc : doc.keywords;
    } else {
      keywords = raw.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    }
    if (!Array.isArray(keywords)) throw new Error('keywords 목록을 찾을 수 없습니다.');
    return { ok: true, keywords: keywords.map(String), path: filePath };
  } catch (err) {
    return { ok: false, error: `가져오기 실패: ${err.message}` };
  }
});

ipcMain.handle('open-external', (_e, url) => {
  if (typeof url === 'string' && /^https?:\/\//i.test(url)) shell.openExternal(url);
});

ipcMain.handle('backend:restart', async () => {
  const ok = await connectBackend({ allowSpawn: true });
  setConnected(ok, ok ? '' : '엔진을 시작하지 못했습니다.');
  return { ok };
});

ipcMain.handle('app:quit', () => {
  quitting = true;
  if (pollTimer) clearTimeout(pollTimer);
  if (tray) tray.destroy();
  app.quit();
});

// ------------------------------------------------------------------ 앱 수명 주기
async function start() {
  const startHidden = argv.includes('--hidden');
  createTray();
  createWindow(startHidden);
  const ok = await connectBackend({ allowSpawn: true, attachWaitMs: argv.includes('--attach') ? 10000 : 0 });
  setConnected(ok, ok ? '' : 'EduGuard 엔진을 시작하지 못했습니다.');
  pollOnce();
}

app.setAppUserModelId(APP_ID);

if (argv.includes('--clear-stale-proxy')) {
  // 예전 버전이 등록한 로그온 정리 작업(EduGuard.exe --clear-stale-proxy) 호환: 엔진에 그대로 넘긴다.
  app.whenReady().then(() => {
    const child = spawnCore(['--clear-stale-proxy'], { detached: false });
    child.on('exit', () => app.quit());
    child.on('error', () => app.quit());
  });
} else if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', (_e, args) => {
    if (!args.includes('--hidden')) showWindow();
    tryAttach();
  });
  app.on('window-all-closed', () => {
    // 트레이에서 계속 동작한다. 종료는 app:quit 으로만.
  });
  app.whenReady().then(start);
}
