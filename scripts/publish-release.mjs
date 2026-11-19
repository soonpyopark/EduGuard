#!/usr/bin/env node
/**
 * npm run publish:release -- --notes-file=<노트.md>   ("배포해줘" 의 마지막 단계)
 *
 * 버전·빌드·커밋은 끝나 있어야 한다 (순서는 .cursor/rules/deploy.mdc):
 *   npm run version:bump → npm run build:update_all → 테스트 → 커밋 → 이 스크립트
 *
 * 하는 일:
 *   1. 점검: package.json 버전 = APP_VERSION, main 브랜치, 작업 트리 깨끗함, MSI 존재
 *   2. 태그 `v1.0.0-N` 생성 → main 과 태그를 GitHub 로 push
 *   3. GitHub Release(제목 `v1.0.0.N`)를 만들고 MSI 를 올림
 *      새 릴리스 본문에는 직전 릴리스의 「변경」을 「이전 버전 이력」으로 이어 붙인다.
 *   4. 업로드를 확인한 뒤에만 이전 릴리스와 이전 버전 태그를 모두 삭제 (원격 + 로컬)
 *      삭제한 태그와 커밋 SHA 는 .cache/logs/publish-release.log 에 남는다.
 *
 * 옵션: --notes="…" | --notes-file=경로   (릴리스를 새로 만들 때 필요)
 *       --dry-run          아무것도 바꾸지 않고 할 일만 출력
 *       --keep-previous    이전 릴리스·태그를 지우지 않음
 */
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const msiDir = path.join(root, 'msi');
const logFile = path.join(root, '.cache', 'logs', 'publish-release.log');

const args = process.argv.slice(2);
const dryRun = args.includes('--dry-run');
const keepPrevious = args.includes('--keep-previous');
const argValue = (name) => args.find((item) => item.startsWith(`${name}=`))?.slice(name.length + 1);

/** 버전 태그로 보이는 것만 정리 대상으로 삼는다 (v1.0.0-1, v1.0.0.1 …). */
const VERSION_TAG = /^v\d+\.\d+\.\d+([-.]\d+)?$/;

const log = (message) => console.log(`[publish] ${message}`);
const fail = (message) => {
  throw new Error(message);
};

function readVersionPy() {
  const source = fs.readFileSync(path.join(root, 'version.py'), 'utf8');
  const version = /APP_VERSION = "([^"]*)"/.exec(source)?.[1];
  const repo = /UPDATE_REPO = "([^"]*)"/.exec(source)?.[1];
  const name = /APP_NAME = "([^"]*)"/.exec(source)?.[1] ?? 'EduGuard';
  if (!version) fail('version.py 에서 APP_VERSION 을 찾지 못했습니다.');
  return { name, version, repo };
}

function semverOf(version) {
  const matched = /^(\d+)\.(\d+)\.(\d+)\.(\d+)$/.exec(version);
  if (!matched) fail(`APP_VERSION 형식이 1.0.0.N 이 아닙니다: ${version}`);
  return `${matched[1]}.${matched[2]}.${matched[3]}-${matched[4]}`;
}

function appendLog(line) {
  fs.mkdirSync(path.dirname(logFile), { recursive: true });
  fs.appendFileSync(logFile, `[${new Date().toISOString()}] ${line}\n`, 'utf8');
}

/**
 * @param {string} command
 * @param {string[]} commandArgs
 * @param {{ mutating?: boolean, allowFail?: boolean }} [options]
 */
function run(command, commandArgs, { mutating = false, allowFail = false } = {}) {
  if (mutating && dryRun) {
    log(`(dry-run) ${command} ${commandArgs.join(' ')}`);
    return { status: 0, stdout: '', stderr: '' };
  }
  const result = spawnSync(command, commandArgs, { cwd: root, encoding: 'utf8' });
  if (result.status !== 0 && !allowFail) {
    fail(`${command} ${commandArgs.join(' ')} 실패 (exit ${result.status})\n${result.stderr || result.stdout}`);
  }
  return result;
}

const git = (gitArgs, options) => run('git', gitArgs, options);
const gh = (ghArgs, options) => run('gh', ghArgs, options);

/** @returns {string[]} */
function lines(text) {
  return String(text ?? '').split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

/** 이번 버전의 MSI 중 빌드 스탬프가 가장 늦은 것 하나. */
function findArtifact(version) {
  if (!fs.existsSync(msiDir)) fail(`산출물 폴더가 없습니다: ${msiDir}`);
  const prefix = `EduGuard_${version}_`;
  const names = fs.readdirSync(msiDir).filter((name) => name.startsWith(prefix) && name.toLowerCase().endsWith('.msi'));
  if (names.length === 0) {
    fail(`버전 ${version} 의 MSI 가 없습니다. 먼저 npm run build:update_all 을 실행하세요.`);
  }
  names.sort();
  return path.join(msiDir, names[names.length - 1]);
}

const HISTORY_HEADING = '## 이전 버전 이력';

/** @param {string} body */
function extractChanges(body) {
  const text = String(body ?? '').replace(/\r\n/g, '\n');
  const match = /^##\s*변경\s*\n([\s\S]*?)(?=\n##\s|$)/i.exec(text);
  if (match) return match[1].trim();
  return text
    .replace(/^##\s*파일\s*\n[\s\S]*?(?=\n##\s|$)/gi, '')
    .replace(/^##\s*이전 버전 이력\s*\n[\s\S]*$/gi, '')
    .trim();
}

/** @param {string} body */
function extractPriorHistory(body) {
  const text = String(body ?? '').replace(/\r\n/g, '\n');
  const match = /^##\s*이전 버전 이력\s*\n([\s\S]*)$/im.exec(text);
  return match ? match[1].trim() : '';
}

/** @param {string} repo @param {string} keepTag */
function fetchPreviousRelease(repo, keepTag) {
  const list = JSON.parse(gh(['release', 'list', '--repo', repo, '--limit', '50', '--json', 'tagName,isDraft,isLatest']).stdout || '[]');
  const candidate =
    list.find((item) => item.isLatest && !item.isDraft && item.tagName !== keepTag) ||
    list.find((item) => !item.isDraft && item.tagName !== keepTag);
  if (!candidate?.tagName) return null;
  return JSON.parse(gh(['release', 'view', candidate.tagName, '--repo', repo, '--json', 'body,name,tagName']).stdout);
}

/**
 * @param {string} notes
 * @param {string[]} fileNames
 * @param {{ body?: string, name?: string, tagName?: string } | null} previous
 */
function releaseBody(notes, fileNames, previous = null) {
  let text = `## 변경\n${notes.trim()}\n\n## 파일\n${fileNames.map((name) => `- ${name}`).join('\n')}\n`;
  const prevBody = String(previous?.body ?? '').trim();
  if (!prevBody) return text;
  const title = previous.name || previous.tagName || '이전 버전';
  const changes = extractChanges(prevBody) || '(이전 변경 내용 없음)';
  const prior = extractPriorHistory(prevBody);
  text += `\n${HISTORY_HEADING}\n\n### ${title}\n${changes}\n`;
  if (prior) text += `\n${prior}\n`;
  return text;
}

function main() {
  const { name, version, repo } = readVersionPy();
  const tag = `v${semverOf(version)}`;
  const title = `v${version}`;
  if (!repo) fail('version.py 의 UPDATE_REPO 가 비어 있습니다.');
  log(`${name} ${title} → 태그 ${tag} / 저장소 ${repo}${dryRun ? ' (dry-run)' : ''}`);

  if (gh(['auth', 'status'], { allowFail: true }).status !== 0) fail('gh 로그인이 필요합니다: gh auth login');

  for (const file of ['package.json', path.join('ui', 'package.json')]) {
    const pkg = JSON.parse(fs.readFileSync(path.join(root, file), 'utf8'));
    if (pkg.version !== semverOf(version)) {
      fail(`${file} version(${pkg.version}) 이 APP_VERSION(${version}) 과 다릅니다. npm run version:bump -- --sync`);
    }
  }

  const branch = lines(git(['rev-parse', '--abbrev-ref', 'HEAD']).stdout)[0];
  if (branch !== 'main') fail(`main 브랜치에서만 배포합니다 (현재: ${branch}).`);

  const releaseExists = gh(['release', 'view', tag, '--repo', repo], { allowFail: true }).status === 0;

  const dirty = lines(git(['status', '--porcelain']).stdout);
  if (dirty.length > 0) {
    fail(`커밋되지 않은 변경이 있습니다. 먼저 커밋하세요:\n  ${dirty.join('\n  ')}`);
  }

  const artifact = findArtifact(version);
  const fileName = path.basename(artifact);
  log(`산출물: ${fileName}`);

  if (!releaseExists) {
    const head = lines(git(['rev-parse', 'HEAD']).stdout)[0];
    const existing = git(['rev-parse', '-q', '--verify', `refs/tags/${tag}^{commit}`], { allowFail: true });
    const existingSha = lines(existing.stdout)[0];
    if (existing.status === 0 && existingSha !== head) {
      fail(`로컬 태그 ${tag} 가 현재 커밋이 아닌 ${existingSha?.slice(0, 7)} 를 가리킵니다. 확인 후 지우세요: git tag -d ${tag}`);
    }
    if (existing.status !== 0) git(['tag', '-a', tag, '-m', `${name} ${title}`], { mutating: true });
    log(`태그 ${tag} → ${head.slice(0, 7)}`);
    git(['push', 'origin', 'main'], { mutating: true });
    git(['push', 'origin', tag], { mutating: true });
  } else {
    log(`릴리스 ${tag} 가 이미 있어 MSI 만 다시 올립니다.`);
  }

  if (!releaseExists) {
    const notesFile = argValue('--notes-file');
    const notes = argValue('--notes') ?? (notesFile ? fs.readFileSync(path.resolve(notesFile), 'utf8') : '');
    if (!notes.trim()) fail('릴리스를 새로 만들려면 --notes="…" 또는 --notes-file=경로 가 필요합니다.');
    const previous = fetchPreviousRelease(repo, tag);
    if (previous?.tagName) log(`이전 릴리스 본문을 이력에 포함: ${previous.name || previous.tagName}`);
    else log('이전 릴리스가 없어 이력 없이 본문만 작성합니다.');
    const bodyFile = path.join(root, '.cache', 'release-body.md');
    fs.mkdirSync(path.dirname(bodyFile), { recursive: true });
    fs.writeFileSync(bodyFile, releaseBody(notes, [fileName], previous), 'utf8');
    gh(
      ['release', 'create', tag, artifact, '--repo', repo, '--verify-tag', '--title', title, '--notes-file', bodyFile, '--latest'],
      { mutating: true },
    );
  } else {
    gh(['release', 'upload', tag, artifact, '--repo', repo, '--clobber'], { mutating: true });
  }

  if (!dryRun) {
    const view = JSON.parse(gh(['release', 'view', tag, '--repo', repo, '--json', 'assets,url,isDraft']).stdout);
    const uploaded = new Set(view.assets.filter((asset) => asset.state === 'uploaded').map((asset) => asset.name));
    if (!uploaded.has(fileName)) fail(`업로드되지 않은 파일이 있어 이전 릴리스를 지우지 않고 중단합니다: ${fileName}`);
    if (view.isDraft) fail('릴리스가 초안 상태입니다. 이전 릴리스를 지우지 않고 중단합니다.');
    log(`릴리스 확인 완료: ${view.url}`);
    appendLog(`released ${tag} (${title}) assets=${fileName}`);
  }

  if (keepPrevious) {
    log('--keep-previous: 이전 릴리스·태그를 지우지 않습니다.');
    return;
  }
  cleanupPrevious(repo, tag);
}

/** @param {string} repo @param {string} keepTag */
function cleanupPrevious(repo, keepTag) {
  const releaseTags = JSON.parse(gh(['release', 'list', '--repo', repo, '--limit', '200', '--json', 'tagName']).stdout)
    .map((item) => item.tagName)
    .filter((name) => name !== keepTag);

  const remoteTags = lines(gh(['api', '--paginate', `repos/${repo}/git/matching-refs/tags`, '--jq', '.[].ref']).stdout)
    .map((ref) => ref.replace(/^refs\/tags\//, ''))
    .filter((name) => VERSION_TAG.test(name) && name !== keepTag);

  const localTags = lines(git(['tag', '--list']).stdout).filter((name) => VERSION_TAG.test(name) && name !== keepTag);

  log(`삭제 대상 — 릴리스 ${releaseTags.length}개, 원격 태그 ${remoteTags.length}개, 로컬 태그 ${localTags.length}개`);

  const shaOf = (name) => lines(git(['rev-parse', '-q', '--verify', `refs/tags/${name}^{commit}`], { allowFail: true }).stdout)[0] ?? '(로컬에 없음)';
  if (!dryRun) {
    for (const name of new Set([...releaseTags, ...remoteTags, ...localTags])) appendLog(`delete ${name} ${shaOf(name)}`);
  }

  for (const name of releaseTags) {
    gh(['release', 'delete', name, '--repo', repo, '--yes', '--cleanup-tag'], { mutating: true, allowFail: true });
    log(`릴리스 삭제: ${name}`);
  }

  const stillRemote = dryRun
    ? remoteTags
    : lines(gh(['api', '--paginate', `repos/${repo}/git/matching-refs/tags`, '--jq', '.[].ref']).stdout)
        .map((ref) => ref.replace(/^refs\/tags\//, ''))
        .filter((name) => VERSION_TAG.test(name) && name !== keepTag);
  if (stillRemote.length > 0) {
    git(['push', 'origin', '--delete', ...stillRemote], { mutating: true });
    for (const name of stillRemote) log(`원격 태그 삭제: ${name}`);
  }

  for (const name of localTags) git(['tag', '-d', name], { mutating: true, allowFail: true });
  if (localTags.length > 0) log(`로컬 태그 ${localTags.length}개 삭제`);
  if (!dryRun) log(`삭제 기록(커밋 SHA 포함): ${path.relative(root, logFile)}`);
}

try {
  main();
} catch (error) {
  console.error('[publish] 실패:', error instanceof Error ? error.message : error);
  process.exit(1);
}
