#!/usr/bin/env node
/**
 * npm run dev          CSS 를 빌드하고 Tailwind 감시와 python main.py 를 띄운다.
 * npm run dev:stop     이 저장소의 엔진(python main.py)·Electron·CSS 감시만 종료한다.
 * npm run dev:restart  종료한 뒤 dev 를 다시 시작한다.
 *
 * 관리자가 아니면 main.py 가 UAC 로 자신을 다시 띄우고 첫 프로세스는 바로 끝난다.
 * 그때도 감시 프로세스와 관리자 권한 엔진은 남으며, dev:stop 이 그 프로세스를 찾는다.
 */
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const uiDir = path.join(root, 'ui');
const pidFile = path.join(root, '.cache', 'dev-pids.json');
const command = process.argv[2] ?? 'start';
const isWin = process.platform === 'win32';

const log = (message) => console.log(`[dev] ${message}`);

function fail(message) {
  console.error(`[dev] ${message}`);
  process.exit(1);
}

/** @param {string} value */
function norm(value) {
  return String(value ?? '').replace(/\//g, '\\').toLowerCase();
}

const rootMark = norm(root);
const electronMark = norm(path.join(uiDir, 'node_modules', 'electron'));
const mainMark = norm(path.join(root, 'main.py'));

/** @returns {{ pid: number, name: string, command: string, exe: string }[]} */
function listCandidates() {
  if (!isWin) return [];
  const script =
    "Get-CimInstance Win32_Process | " +
    "Where-Object { $_.Name -match '^(pythonw?|electron|node)(\\.exe)?$' } | " +
    'Select-Object ProcessId,Name,CommandLine,ExecutablePath | ConvertTo-Json -Compress';
  const result = spawnSync(
    'powershell.exe',
    ['-NoProfile', '-NonInteractive', '-Command', script],
    { encoding: 'utf8' },
  );
  const raw = (result.stdout || '').trim();
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return (Array.isArray(parsed) ? parsed : [parsed]).map((item) => ({
      pid: Number(item.ProcessId),
      name: String(item.Name ?? ''),
      command: String(item.CommandLine ?? ''),
      exe: String(item.ExecutablePath ?? ''),
    }));
  } catch {
    return [];
  }
}

/** @param {{ pid: number, name: string, command: string, exe: string }} proc */
function isProjectProcess(proc) {
  if (!proc.pid || proc.pid === process.pid) return false;
  const blob = norm(`${proc.command} ${proc.exe}`);
  if (!blob.includes(rootMark)) return false;
  const name = proc.name.toLowerCase();
  if (name.startsWith('electron')) return blob.includes(electronMark);
  if (name.startsWith('python')) return blob.includes(mainMark);
  if (name.startsWith('node')) {
    return blob.includes('watch:css') || blob.includes('tailwindcss');
  }
  return false;
}

function readWatchPid() {
  try {
    const data = JSON.parse(fs.readFileSync(pidFile, 'utf8'));
    const pid = Number(data.watch);
    return pid > 0 ? pid : 0;
  } catch {
    return 0;
  }
}

/** @param {number} pid @returns {boolean} 종료에 성공했는지 */
function killTree(pid) {
  if (!pid || pid === process.pid) return true;
  const result = spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { encoding: 'utf8' });
  return result.status === 0;
}

function stopDev() {
  const targets = new Map();
  for (const proc of listCandidates().filter(isProjectProcess)) targets.set(proc.pid, proc.name);
  const watchPid = readWatchPid();
  if (watchPid) targets.set(watchPid, 'css-watch');

  fs.rmSync(pidFile, { force: true });
  if (targets.size === 0) {
    log('실행 중인 개발 프로세스가 없습니다.');
    return;
  }
  const summary = [...targets.entries()].map(([pid, name]) => `${name}:${pid}`).join(', ');
  log(`종료: ${summary}`);
  const failed = [...targets.keys()].filter((pid) => !killTree(pid));
  if (failed.length > 0) {
    log(`종료하지 못한 프로세스: ${failed.join(', ')} (관리자 권한으로 실행 중일 수 있습니다).`);
  }
  // 관리자 권한 엔진은 명령줄을 읽을 수 없어 위 목록에 없을 수 있다. 그런 python 이 남아 있는지 알린다.
  const hidden = listCandidates().filter((proc) => proc.name.toLowerCase().startsWith('python') && !proc.command);
  if (hidden.length > 0) {
    log(`관리자 권한 엔진이 남아 있습니다(PID ${hidden.map((p) => p.pid).join(', ')}). ` +
      '관리자 PowerShell 에서 npm run dev:stop 을 실행하거나 UI 의 프로그램 종료(비밀번호)를 사용하세요.');
  }
  log('종료 절차를 마쳤습니다. 차단 중이었다면 다음 실행 때 잔여 프록시를 되돌립니다.');
}

function startWatch() {
  const child = spawn('npm run watch:css', {
    cwd: uiDir,
    shell: true,
    detached: true,
    stdio: 'ignore',
    windowsHide: true,
  });
  child.unref();
  fs.mkdirSync(path.dirname(pidFile), { recursive: true });
  fs.writeFileSync(pidFile, JSON.stringify({ watch: child.pid }), 'utf8');
  log(`Tailwind 감시 시작 (PID ${child.pid})`);
}

function startDev() {
  const css = spawnSync('npm run build:css', { cwd: uiDir, shell: true, stdio: 'inherit' });
  if (css.status !== 0) fail('CSS 빌드에 실패했습니다. ui 폴더에서 npm install 을 먼저 실행하세요.');
  startWatch();
  log('엔진을 시작합니다. 관리자가 아니면 UAC 창에서 예를 누르세요.');
  const app = spawn('python', ['main.py'], { cwd: root, stdio: 'inherit' });
  app.on('exit', (code) => {
    // 종료 코드 0 은 관리자 권한으로 다시 띄운 경우다. 실제 엔진은 UAC 로 뜬 다른 프로세스이므로
    // (명령줄을 못 읽을 수 있음) CSS 감시를 유지하고, 끄려면 npm run dev:stop 을 쓴다.
    if (code === 0) {
      log('관리자 권한 엔진을 실행했습니다(UAC). CSS 감시는 유지되며, 끄려면 npm run dev:stop 을 사용하세요.');
      process.exit(0);
    }
    killTree(readWatchPid());
    fs.rmSync(pidFile, { force: true });
    process.exit(code ?? 1);
  });
  app.on('error', () => fail('python 을 실행하지 못했습니다. PATH 에 python 이 있는지 확인하세요.'));
}

function restartDev() {
  stopDev();
  log('다시 시작합니다.');
  const child = spawn('npm run dev', { cwd: root, shell: true, stdio: 'inherit' });
  child.on('exit', (code) => process.exit(code ?? 0));
}

if (command === 'start') startDev();
else if (command === 'stop') stopDev();
else if (command === 'restart') restartDev();
else fail('사용법: node scripts/dev-process.mjs [start|stop|restart]');
