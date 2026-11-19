#!/usr/bin/env node
/**
 * 배포 버전을 한 번에 올린다. (Tiny PDF Editor - neo / All In One Tools 와 같은 표기)
 *
 *   npm run version:bump               1.0.0.8 → 1.0.0.9, 1.0.0.9 → 1.0.1.0
 *   npm run version:bump -- --set=1.1.0.0
 *   npm run version:bump -- --sync     올리지 않고 package.json 만 APP_VERSION 에 맞춤
 *
 * 바꾸는 곳:
 *   version.py           APP_VERSION     `1.0.0.N`  (UI·MSI·릴리스 제목은 v1.0.0.N)
 *   package.json         version         `1.0.0-N`
 *   ui/package.json      version         `1.0.0-N`
 *   ui/package-lock.json version         `1.0.0-N`  (맨 위와 packages[""])
 * Git 태그는 `v1.0.0-N` 이며 version.py 의 app_git_tag() 가 같은 값을 계산한다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const versionPath = path.join(root, 'version.py');

const VERSION_RE = /^(\d+)\.(\d+)\.(\d+)\.(\d+)$/;

function readCurrent() {
  const source = fs.readFileSync(versionPath, 'utf8');
  const match = /APP_VERSION = "([^"]*)"/.exec(source);
  if (!match) throw new Error('version.py 에서 APP_VERSION 을 찾지 못했습니다.');
  if (!VERSION_RE.test(match[1])) throw new Error(`APP_VERSION 형식이 1.0.0.N 이 아닙니다: ${match[1]}`);
  return { source, version: match[1] };
}

/** `1.0.0.1` → `1.0.0-1` */
function semverOf(version) {
  const matched = VERSION_RE.exec(version);
  return `${matched[1]}.${matched[2]}.${matched[3]}-${matched[4]}`;
}

/** JSON 의 패키지 version 만 바꾼다. lock 은 맨 위와 packages[""] 두 곳. */
function writeJsonVersion(file, semver, { lock = false } = {}) {
  if (!fs.existsSync(file)) return;
  const text = fs.readFileSync(file, 'utf8');
  let count = 0;
  const limit = lock ? 2 : 1;
  const next = text.replace(/("version"\s*:\s*")[^"]*(")/g, (whole, open, close) => {
    if (count >= limit) return whole;
    count += 1;
    return `${open}${semver}${close}`;
  });
  if (count < limit) throw new Error(`${file} 에서 version 필드를 충분히 찾지 못했습니다.`);
  fs.writeFileSync(file, next, 'utf8');
}

const args = process.argv.slice(2);
const { source, version: current } = readCurrent();

let next = current;
const setArg = args.find((item) => item.startsWith('--set='));
if (setArg) {
  next = setArg.slice('--set='.length).replace(/^v/i, '');
  if (!VERSION_RE.test(next)) throw new Error(`--set 값은 1.0.0.N 형식이어야 합니다: ${next}`);
} else if (!args.includes('--sync')) {
  const matched = VERSION_RE.exec(current);
  const major = Number(matched[1]);
  const minor = Number(matched[2]);
  const patch = Number(matched[3]);
  const build = Number(matched[4]);
  // 끝자리는 한 자리(0–9). 9 다음은 patch+1.0 (1.0.0.9 → 1.0.1.0).
  if (build >= 9) next = `${major}.${minor}.${patch + 1}.0`;
  else next = `${major}.${minor}.${patch}.${build + 1}`;
}

if (next !== current) {
  fs.writeFileSync(versionPath, source.replace(/(APP_VERSION = )"[^"]*"/, `$1"${next}"`), 'utf8');
}
const semver = semverOf(next);
writeJsonVersion(path.join(root, 'package.json'), semver);
writeJsonVersion(path.join(root, 'ui', 'package.json'), semver);
writeJsonVersion(path.join(root, 'ui', 'package-lock.json'), semver, { lock: true });

console.log(`[version] ${current} → ${next}  (표시 v${next}, 태그 v${semver}, package.json ${semver})`);
