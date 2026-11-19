'use strict';

// 렌더러: Node 권한 없이 preload 가 노출한 window.eduguard 만 사용한다.
// 로그의 호스트명 등 외부에서 온 문자열이 많으므로 innerHTML 을 쓰지 않고 textContent 로만 DOM 을 만든다.

const bridge = window.eduguard;
const BLOG_URL = 'https://note4all.tistory.com';
const GITHUB_URL = 'https://github.com/soonpyopark';
const $ = (selector) => document.querySelector(selector);

const MAX_LOG_ROWS = 500;
const LOG_ACTIONS = ['ALLOW', 'BLOCK', 'GUARD', 'ERROR'];
const LOG_BADGE = {
  ALLOW: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
  BLOCK: 'bg-rose-50 text-rose-700 ring-rose-600/20',
  GUARD: 'bg-amber-50 text-amber-700 ring-amber-600/20',
  ERROR: 'bg-slate-100 text-slate-600 ring-slate-500/20',
};
const LOG_LABEL = { ALLOW: '허용', BLOCK: '차단', GUARD: '감시', ERROR: '오류' };

const HERO_BASE = 'rounded-3xl bg-linear-to-br p-6 text-white shadow-lg transition-colors duration-500';
const HERO = {
  blocking: {
    tone: 'from-rose-600 to-red-700',
    title: '🔒 차단 중',
    sub: (s) => `허용 키워드 ${s.keywords.length}개가 포함된 사이트만 접속할 수 있습니다.${s.remote_support ? ' · 원격지원 허용' : ''}`,
  },
  temp_unlock: {
    tone: 'from-amber-500 to-orange-600',
    title: '⏱ 일시 해제 중',
    sub: (s) => `${s.temp_unlock_remaining}분 후 자동으로 다시 차단합니다.`,
  },
  waiting: {
    tone: 'from-amber-500 to-orange-600',
    title: '⏳ 자동 차단 대기 중',
    sub: () => 'StarPlayer 기동 또는 시작 지연 시간을 기다린 뒤 차단을 시작합니다.',
  },
  off: {
    tone: 'from-slate-500 to-slate-700',
    title: '🔓 차단 해제됨',
    sub: () => '모든 사이트에 접속할 수 있습니다.',
  },
};

let state = null;
let keywordsKey = '';
const selectedKeywords = new Set();
const hiddenActions = new Set();
let logRows = 0;
let logFollowing = true;
let logPending = 0;
let logIgnoreScroll = false;
let logIgnoreTimer = 0;
const LOG_STICK_PX = 8;
let setupOpen = false;
let exitOpen = false;

// ------------------------------------------------------------------ DOM / API 헬퍼
function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = value;
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'checked' || key === 'disabled' || key === 'value') el[key] = value;
    else el.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function call(method, path, body) {
  const res = await bridge.api(method, path, body);
  if (!res || res.ok === false) throw new ApiError((res && res.error) || '알 수 없는 오류가 발생했습니다.', (res && res.status) || 0);
  return res;
}

function toast(message, kind = 'info') {
  const tone = {
    info: 'bg-slate-900 text-white',
    success: 'bg-emerald-600 text-white',
    error: 'bg-rose-600 text-white',
  }[kind];
  const el = h('div', {
    class: `pointer-events-auto whitespace-pre-line rounded-xl px-4 py-3 text-sm shadow-lg ring-1 ring-black/5 transition duration-300 ${tone}`,
    text: message,
  });
  $('#toast-root').append(el);
  setTimeout(() => {
    el.classList.add('opacity-0', 'translate-y-2');
    setTimeout(() => el.remove(), 300);
  }, kind === 'error' ? 5500 : 3000);
}

async function guarded(fn) {
  try {
    await fn();
  } catch (err) {
    toast(err.message || String(err), 'error');
  }
}

// ------------------------------------------------------------------ 모달
function openModal({ title, description, body, actions = [], dismissible = true, size = 'max-w-md', onClose }) {
  const footer = actions.length ? h('div', { class: 'flex justify-end gap-2 border-t border-slate-100 px-6 py-4' }, actions) : null;
  const panel = h(
    'div',
    { class: `flex max-h-full w-full ${size} flex-col overflow-hidden rounded-2xl bg-white shadow-2xl ring-1 ring-slate-900/5` },
    h(
      'div',
      { class: 'px-6 pt-5' },
      h('h3', { class: 'text-base font-semibold text-slate-900', text: title }),
      description ? h('p', { class: 'hint mt-1 whitespace-pre-line text-sm', text: description }) : null,
    ),
    h('div', { class: 'min-h-0 flex-1 overflow-y-auto px-6 py-4' }, body),
    footer,
  );
  const backdrop = h('div', { class: 'fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-6 backdrop-blur-sm' }, panel);
  let closed = false;
  const onKey = (e) => {
    if (e.key === 'Escape' && dismissible) close(false);
  };
  function close(result) {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey);
    backdrop.remove();
    if (onClose) onClose(result);
  }
  document.addEventListener('keydown', onKey);
  if (dismissible) {
    backdrop.addEventListener('mousedown', (e) => {
      if (e.target === backdrop) close(false);
    });
  }
  $('#modal-root').append(backdrop);
  return { close, panel };
}

// 입력 폼 모달. run 이 성공하면 true 로 resolve, 취소하면 false.
// 비밀번호 오류(401)/잠금(423)은 창을 닫지 않고 그 자리에서 다시 입력받는다.
function formModal({ title, description, fields, submitLabel = '확인', variant = 'primary', dismissible = true, run }) {
  return new Promise((resolve) => {
    const inputs = {};
    const error = h('div', { class: 'hidden whitespace-pre-line rounded-xl bg-rose-50 px-3 py-2 text-sm text-rose-700' });
    const rows = fields.map((f) => {
      const input = h('input', {
        class: 'input',
        type: f.type || 'text',
        value: f.value === undefined ? '' : String(f.value),
        min: f.min,
        max: f.max,
        step: f.step,
        placeholder: f.placeholder,
        autocomplete: 'off',
      });
      inputs[f.name] = input;
      return h('label', { class: 'block' }, h('span', { class: 'field-label', text: f.label }), input, f.hint ? h('span', { class: 'hint mt-1 block', text: f.hint }) : null);
    });
    const submit = h('button', { class: `btn btn-${variant}`, type: 'submit', text: submitLabel });
    const cancel = dismissible ? h('button', { class: 'btn btn-ghost', type: 'button', text: '취소', onclick: () => modal.close(false) }) : null;
    const form = h('form', { class: 'flex flex-col gap-3' }, rows, error, h('div', { class: 'mt-2 flex justify-end gap-2' }, cancel, submit));
    const modal = openModal({ title, description, body: form, dismissible, onClose: (ok) => resolve(Boolean(ok)) });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const values = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value]));
      submit.disabled = true;
      error.classList.add('hidden');
      try {
        await run(values);
        modal.close(true);
      } catch (err) {
        error.textContent = err.message || String(err);
        error.classList.remove('hidden');
        for (const f of fields) if (f.type === 'password') inputs[f.name].value = '';
        const firstPassword = fields.find((f) => f.type === 'password');
        if (firstPassword) inputs[firstPassword.name].focus();
      } finally {
        submit.disabled = false;
      }
    });
    const focusField = fields.find((f) => f.autofocus) || fields[0];
    if (focusField) setTimeout(() => inputs[focusField.name].focus(), 30);
  });
}

function withPassword(purpose, run, { title = '부모님 비밀번호 확인', submitLabel = '확인', variant = 'primary', extraFields = [] } = {}) {
  return formModal({
    title,
    description: purpose,
    submitLabel,
    variant,
    fields: [...extraFields, { name: 'password', label: '부모님 비밀번호', type: 'password', autofocus: !extraFields.length }],
    run: (values) => run(values.password, values),
  });
}

function infoModal(title, body, extraActions = []) {
  const modal = openModal({
    title,
    body,
    size: 'max-w-lg',
    actions: [...extraActions, h('button', { class: 'btn btn-primary', text: '닫기', onclick: () => modal.close(true) })],
  });
  return modal;
}

// ------------------------------------------------------------------ 상태 반영
function applyState(next) {
  state = next;
  const hero = HERO[state.status] || HERO.off;
  $('#hero').className = `${HERO_BASE} ${hero.tone}`;
  $('#hero-title').textContent = hero.title;
  $('#hero-sub').textContent = hero.sub(state);
  $('#count-allowed').textContent = state.allowed.toLocaleString('ko-KR');
  $('#count-blocked').textContent = state.blocked.toLocaleString('ko-KR');
  $('#btn-start').disabled = state.status === 'blocking';
  $('#btn-stop').disabled = state.status !== 'blocking';
  $('#btn-temp').disabled = state.status !== 'blocking';
  $('#version-badge').textContent = state.build_time ? `v${state.version} · 빌드 ${state.build_time}` : `v${state.version} · 소스 실행`;
  $('#admin-badge').classList.toggle('hidden', state.admin);
  renderKeywords();
  renderBanners();
  if (!state.has_password) openSetup();
}

function renderKeywords() {
  const key = JSON.stringify(state.keywords);
  if (key === keywordsKey) return;
  keywordsKey = key;
  for (const kw of [...selectedKeywords]) if (!state.keywords.includes(kw)) selectedKeywords.delete(kw);
  $('#kw-count').textContent = String(state.keywords.length);
  const list = $('#kw-list');
  list.replaceChildren(
    ...state.keywords.map((kw) =>
      h('button', {
        type: 'button',
        class: keywordChipClass(selectedKeywords.has(kw)),
        text: kw,
        'data-keyword': kw,
        onclick: (e) => {
          if (selectedKeywords.has(kw)) selectedKeywords.delete(kw);
          else selectedKeywords.add(kw);
          e.currentTarget.className = keywordChipClass(selectedKeywords.has(kw));
          updateKeywordButtons();
        },
      }),
    ),
  );
  updateKeywordButtons();
}

function keywordChipClass(selected) {
  const base = 'rounded-lg px-2.5 py-1 font-mono text-xs ring-1 ring-inset transition';
  return selected
    ? `${base} bg-indigo-600 text-white ring-indigo-600`
    : `${base} bg-slate-50 text-slate-700 ring-slate-200 hover:bg-slate-100`;
}

function updateKeywordButtons() {
  const n = selectedKeywords.size;
  $('#kw-remove').disabled = n === 0;
  $('#kw-remove').textContent = n ? `선택 삭제 (${n})` : '선택 삭제';
}

function renderBanners() {
  const banners = [];
  if (state.update) {
    banners.push(
      h(
        'div',
        { class: 'flex items-center gap-3 rounded-2xl bg-indigo-50 px-4 py-3 text-sm text-indigo-800 ring-1 ring-indigo-600/15' },
        h('span', { class: 'font-semibold', text: `새 버전 ${state.update.version} 을 사용할 수 있습니다.` }),
        state.update.notes ? h('span', { class: 'truncate text-indigo-700/80', text: state.update.notes }) : null,
        h('button', { class: 'btn btn-primary btn-sm ml-auto shrink-0', text: '다운로드 페이지 열기', onclick: () => bridge.openExternal(state.update.url) }),
      ),
    );
  }
  const root = $('#banners');
  const key = JSON.stringify(state.update);
  if (root.dataset.key === key) return;
  root.dataset.key = key;
  root.replaceChildren(...banners);
}

// ------------------------------------------------------------------ 로그
function renderLogFilters() {
  $('#log-filters').replaceChildren(
    ...LOG_ACTIONS.map((action) => {
      const active = !hiddenActions.has(action);
      return h('button', {
        type: 'button',
        class: `rounded-lg px-2 py-1 text-[11px] font-semibold ring-1 ring-inset transition ${active ? LOG_BADGE[action] : 'bg-white text-slate-300 ring-slate-200'}`,
        text: LOG_LABEL[action],
        onclick: () => {
          if (hiddenActions.has(action)) hiddenActions.delete(action);
          else hiddenActions.add(action);
          for (const row of document.querySelectorAll(`#log [data-action="${action}"]`)) row.classList.toggle('hidden', hiddenActions.has(action));
          renderLogFilters();
          if (logFollowing) scrollLogToLatest();
        },
      });
    }),
  );
}

function logGap(log) {
  return log.scrollHeight - log.scrollTop - log.clientHeight;
}

function holdLogScroll() {
  logIgnoreScroll = true;
  window.clearTimeout(logIgnoreTimer);
  logIgnoreTimer = window.setTimeout(() => {
    logIgnoreScroll = false;
  }, 80);
}

function updateLogJump() {
  const btn = $('#log-jump');
  if (logFollowing) {
    logPending = 0;
    btn.classList.add('hidden');
    return;
  }
  btn.textContent = logPending > 0 ? `↓ 새 로그 ${logPending}` : '↓ 최신 로그';
  btn.classList.remove('hidden');
}

function followLog(on) {
  logFollowing = on;
  updateLogJump();
}

function scrollLogToLatest() {
  const log = $('#log');
  holdLogScroll();
  log.scrollTop = log.scrollHeight;
}

function appendLogs(events) {
  const log = $('#log');
  const fragment = document.createDocumentFragment();
  let visible = 0;
  for (const ev of events) {
    const action = LOG_ACTIONS.includes(ev.action) ? ev.action : 'ERROR';
    if (!hiddenActions.has(action)) visible += 1;
    fragment.append(
      h(
        'div',
        {
          class: `flex items-center gap-3 rounded-md px-1 hover:bg-slate-50${hiddenActions.has(action) ? ' hidden' : ''}`,
          'data-action': action,
          title: `${ev.host}  (${ev.detail})`,
        },
        h('span', { class: 'shrink-0 text-slate-400 tabular-nums', text: ev.time }),
        h('span', { class: `w-10 shrink-0 rounded px-1 text-center font-sans text-[10px] leading-5 font-semibold ring-1 ring-inset ${LOG_BADGE[action]}`, text: LOG_LABEL[action] }),
        h('span', { class: 'min-w-0 flex-1 truncate' }, h('span', { class: 'font-medium text-slate-700', text: ev.host }), h('span', { class: 'text-slate-400', text: `  ${ev.detail}` })),
      ),
    );
    logRows += 1;
  }
  $('#log-empty').classList.add('hidden');
  log.append(fragment);
  holdLogScroll();
  while (logRows > MAX_LOG_ROWS) {
    const first = log.querySelector('[data-action]');
    if (!first) break;
    const height = first.offsetHeight;
    first.remove();
    logRows -= 1;
    if (!logFollowing && height) log.scrollTop = Math.max(0, log.scrollTop - height);
  }
  $('#log-count').textContent = String(logRows);
  if (logFollowing) scrollLogToLatest();
  else if (visible) {
    logPending += visible;
    updateLogJump();
  }
}

function clearLogs() {
  for (const row of document.querySelectorAll('#log [data-action]')) row.remove();
  logRows = 0;
  $('#log-count').textContent = '0';
  $('#log-empty').classList.remove('hidden');
  followLog(true);
}

// ------------------------------------------------------------------ 연결 오버레이
function showOverlay(title, message, canRetry) {
  $('#overlay').classList.remove('hidden');
  $('#overlay-title').textContent = title;
  $('#overlay-message').textContent = message;
  $('#overlay-spinner').classList.toggle('hidden', canRetry);
  $('#overlay-retry').classList.toggle('hidden', !canRetry);
}

function hideOverlay() {
  $('#overlay').classList.add('hidden');
}

// ------------------------------------------------------------------ 동작
async function startBlocking() {
  await guarded(async () => {
    await call('POST', '/api/block/start');
    toast('차단을 시작했습니다.', 'success');
  });
}

async function stopBlocking() {
  const ok = await withPassword('차단을 해제합니다. 모든 사이트에 접속할 수 있게 됩니다.', (password) => call('POST', '/api/block/stop', { password }), {
    submitLabel: '차단 해제',
    variant: 'danger',
  });
  if (ok) toast('차단을 해제했습니다.', 'success');
}

async function tempUnlock() {
  let minutes = 0;
  const ok = await withPassword(
    '입력한 시간 동안 차단을 해제합니다. 시간이 지나면 자동으로 다시 차단합니다.',
    (password, values) => {
      minutes = Number(values.minutes);
      return call('POST', '/api/block/temp-unlock', { password, minutes: values.minutes });
    },
    {
      title: '일시 해제',
      submitLabel: '일시 해제',
      extraFields: [{ name: 'minutes', label: '해제 시간(분)', type: 'number', value: state.temp_unlock_minutes, min: 1, max: 480, autofocus: true, hint: '1 ~ 480분' }],
    },
  );
  if (ok) toast(`${minutes}분 동안 차단을 일시 해제했습니다.`, 'success');
}

async function addKeyword() {
  const input = $('#kw-input');
  const keyword = input.value.trim();
  if (!keyword) return;
  const ok = await withPassword(`키워드 '${keyword}' 를 추가합니다.`, (password) => call('POST', '/api/keywords/add', { password, keyword }));
  if (ok) {
    input.value = '';
    toast(`'${keyword}' 를 추가했습니다.`, 'success');
  }
}

async function removeKeywords() {
  const keywords = [...selectedKeywords];
  if (!keywords.length) return;
  const ok = await withPassword(`키워드 ${keywords.join(', ')} 를 삭제합니다.`, (password) => call('POST', '/api/keywords/remove', { password, keywords }), {
    submitLabel: '삭제',
    variant: 'danger',
  });
  if (ok) {
    selectedKeywords.clear();
    toast('선택한 키워드를 삭제했습니다.', 'success');
  }
}

async function resetKeywords() {
  const ok = await withPassword('허용 키워드를 기본값으로 되돌립니다.', (password) => call('POST', '/api/keywords/reset', { password }));
  if (ok) toast('기본 키워드로 복원했습니다.', 'success');
}

async function importKeywords() {
  const picked = await bridge.importKeywords();
  if (!picked.ok) {
    if (!picked.canceled) toast(picked.error, 'error');
    return;
  }
  const ok = await withPassword(`${picked.keywords.length}개 키워드로 기존 목록을 교체합니다.`, (password) =>
    call('POST', '/api/keywords/import', { password, keywords: picked.keywords, source: picked.path }),
  );
  if (ok) toast('허용 키워드를 가져왔습니다.', 'success');
}

async function exportKeywords() {
  const res = await bridge.exportKeywords(state.keywords);
  if (res.ok) toast('허용 키워드를 내보냈습니다.', 'success');
  else if (!res.canceled) toast(res.error, 'error');
}

async function testDomain() {
  const domain = $('#test-input').value.trim();
  if (!domain) return;
  await guarded(async () => {
    const res = await call('POST', '/api/test-domain', { domain });
    const box = $('#test-result');
    const tone = res.allowed
      ? 'bg-emerald-50 text-emerald-800 ring-emerald-600/20'
      : 'bg-rose-50 text-rose-800 ring-rose-600/20';
    box.className = `mt-2 h-10 truncate rounded-xl px-3 text-sm leading-10 ring-1 ring-inset ${tone}`;
    box.textContent = res.allowed ? `✅ 허용 · 키워드 '${res.reason}' 포함 (${res.host})` : `🚫 차단 · ${res.reason} (${res.host})`;
    box.title = box.textContent;
  });
}

async function showProxyStatus() {
  const pre = h('pre', { class: 'max-h-[55vh] overflow-auto rounded-xl bg-slate-950 p-4 font-mono text-xs leading-relaxed text-slate-100 select-text', text: '불러오는 중…' });
  const load = () =>
    guarded(async () => {
      pre.textContent = (await call('GET', '/api/proxy-status')).report;
    });
  infoModal('프록시 상태', pre, [h('button', { class: 'btn btn-soft', text: '새로고침', onclick: load })]);
  load();
}

async function checkUpdate() {
  const btn = $('#btn-update');
  if (btn.disabled) return;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = '확인 중…';
  try {
    const res = await call('POST', '/api/update/check');
    const message = (text) => h('p', { class: 'whitespace-pre-line text-sm text-slate-600', text });
    if (res.error) {
      infoModal('업데이트 확인', message(`업데이트 정보를 확인할 수 없습니다.\n\n${res.error}`), [
        h('button', {
          class: 'btn btn-soft',
          text: '릴리스 페이지 열기',
          onclick: () => bridge.openExternal(res.url || `${GITHUB_URL}/EduGuard/releases`),
        }),
      ]);
      return;
    }
    if (res.available) {
      if (state) {
        state.update = { version: res.latest, url: res.url, notes: res.notes || '' };
        renderBanners();
      }
      const lines = [`새 버전이 있습니다: v${res.latest}`, `현재 버전: v${res.current}`];
      if (res.notes) lines.push('', res.notes);
      infoModal('업데이트 확인', message(lines.join('\n')), [
        h('button', { class: 'btn btn-soft', text: '다운로드 페이지 열기', onclick: () => bridge.openExternal(res.url) }),
      ]);
      return;
    }
    if (state) {
      state.update = null;
      renderBanners();
    }
    infoModal('업데이트 확인', message(`최신 버전입니다.\n\n현재 버전: v${res.current}`));
  } catch (err) {
    toast(err.message || String(err), 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

function showVersionInfo() {
  const row = (label, value) => h('div', { class: 'flex justify-between gap-4 py-2' }, h('span', { class: 'text-slate-500', text: label }), h('span', { class: 'font-medium text-slate-900 select-text', text: value }));
  infoModal(
    '버전 정보',
    h(
      'div',
      { class: 'divide-y divide-slate-100 text-sm' },
      row('프로그램', 'EduGuard'),
      row('버전', state.version),
      row('빌드', state.build_time || '소스 실행 (빌드 시각 없음)'),
      row('라이선스', 'GNU GPL-3.0'),
    ),
  );
}

async function changePassword() {
  const ok = await formModal({
    title: '비밀번호 변경',
    fields: [
      { name: 'password', label: '현재 비밀번호', type: 'password', autofocus: true },
      { name: 'next', label: '새 비밀번호', type: 'password', hint: `${state.min_password_len}자 이상` },
      { name: 'confirm', label: '새 비밀번호 확인', type: 'password' },
    ],
    submitLabel: '변경',
    run: (v) => {
      if (v.next !== v.confirm) throw new ApiError('새 비밀번호가 일치하지 않습니다.', 400);
      return call('POST', '/api/password/change', { password: v.password, new_password: v.next });
    },
  });
  if (ok) toast('비밀번호를 변경했습니다.', 'success');
}

async function openSetup() {
  if (setupOpen) return;
  setupOpen = true;
  const warning = state.tampered ? '설정 파일이 손상되었거나 변조되어 기본값으로 초기화했습니다.\n' : '';
  await formModal({
    title: '부모님 비밀번호 설정',
    description: `${warning}차단 해제·설정 변경·종료에 사용할 비밀번호를 정해 주세요. (${state.min_password_len}자 이상)`,
    dismissible: false,
    submitLabel: '설정',
    fields: [
      { name: 'password', label: '비밀번호', type: 'password', autofocus: true },
      { name: 'confirm', label: '비밀번호 확인', type: 'password' },
    ],
    run: (v) => {
      if (v.password !== v.confirm) throw new ApiError('비밀번호가 일치하지 않습니다.', 400);
      return call('POST', '/api/setup-password', { password: v.password });
    },
  });
  setupOpen = false;
  toast('비밀번호를 설정했습니다.', 'success');
}

async function requestExit() {
  if (exitOpen) return;
  exitOpen = true;
  const ok = await withPassword('프로그램을 종료합니다. 종료하면 차단도 해제됩니다.', (password) => call('POST', '/api/exit', { password }), {
    title: '프로그램 종료',
    submitLabel: '종료',
    variant: 'danger',
  });
  exitOpen = false;
  if (ok) bridge.quit();
}

// ------------------------------------------------------------------ 환경설정
const SETTINGS_SCHEMA = [
  {
    tab: '시작',
    items: [
      { key: 'run_at_startup', type: 'bool', adminOnly: true, label: 'Windows 시작(로그온) 시 EduGuard 자동 실행', hint: '작업 스케줄러에 최고 권한으로 등록합니다. 관리자 권한으로 실행 중일 때만 바꿀 수 있습니다.' },
      { key: 'start_in_tray', type: 'bool', label: '시작할 때 창 없이 트레이 아이콘으로 실행', hint: '자동 실행 시 창을 띄우지 않고 알림 영역(트레이)에서만 동작합니다.' },
      { key: 'close_to_tray', type: 'bool', label: '창 닫기(X) 시 종료하지 않고 트레이로 숨기기', hint: "끄면 X 버튼이 '프로그램 종료'(비밀번호 확인)로 동작합니다." },
      { key: 'tray_notifications', type: 'bool', label: '트레이 알림 표시', hint: '차단 시작/해제, 일시 해제, 트레이 숨김 안내를 알림으로 표시합니다.' },
      { key: 'watchdog_enabled', type: 'bool', label: '강제 종료 감시(워치독) 사용', hint: '작업 관리자 등으로 엔진이 강제 종료되면 다시 실행합니다. 정상 종료는 재실행하지 않습니다.' },
    ],
  },
  {
    tab: '차단',
    items: [
      { key: 'auto_start', type: 'bool', label: '프로그램이 시작되면 자동으로 차단 시작', hint: "끄면 실행 후 '차단 시작' 버튼을 눌러야 차단됩니다." },
      { key: 'block_start_delay_seconds', type: 'number', min: 0, max: 600, unit: '초', label: '자동 차단 시작 지연', hint: 'StarPlayer가 먼저 기동할 시간을 줍니다. 0 ~ 600초, 기본 20초' },
      { key: 'wait_for_starplayer', type: 'bool', label: '자동 차단 전 StarPlayer 준비 확인', hint: 'StarPlayer/Axissoft 프로세스가 보일 때까지 차단을 미룹니다. 시간 초과 시 강제로 시작합니다.' },
      { key: 'starplayer_wait_timeout_seconds', type: 'number', min: 0, max: 600, unit: '초', label: 'StarPlayer 추가 대기', hint: '지연 이후에도 미기동이면 이 시간까지 더 기다립니다. 0 ~ 600초, 기본 120초' },
      { key: 'schedule_enabled', type: 'bool', label: '시간대 스케줄 사용', hint: '아래 시간대 안에서는 자동 차단, 밖에서는 자동 해제됩니다. 일시 해제 중이면 일시 해제가 우선입니다.' },
      { keys: ['schedule_start', 'schedule_end'], type: 'timerange', label: '차단 적용 시간대', hint: '시작과 끝이 같으면 하루 종일 적용되고, 자정을 넘기는 시간대도 가능합니다.' },
      { key: 'remote_support_enabled', type: 'bool', label: '원격지원모드 허용', hint: 'TeamViewer 연결 유지를 위해 teamviewer/dyngate 도메인을 추가로 허용합니다. 사용 후 끄는 것을 권장합니다.' },
      { key: 'temp_unlock_minutes', type: 'number', min: 1, max: 480, unit: '분', label: '일시 해제 기본 시간', hint: '1 ~ 480분' },
      { key: 'block_message', type: 'text', maxlength: 200, label: '차단 페이지 문구', hint: 'HTTP 차단 페이지에 표시됩니다. 200자 이하' },
    ],
  },
  {
    tab: '네트워크',
    items: [
      { key: 'port', type: 'number', min: 1024, max: 65535, label: '필터 프록시 포트', hint: '변경한 포트는 다음 차단 시작부터 적용됩니다. 기본 8899' },
      { key: 'guard_interval', type: 'number', min: 0.5, max: 30, step: 0.5, unit: '초', label: '프록시 설정 감시 주기', hint: '0.5 ~ 30초. 짧을수록 임의 변경을 빨리 되돌립니다.' },
    ],
  },
  {
    tab: '로그',
    items: [
      { key: 'suppress_repeated_logs', type: 'bool', label: '반복 로그 숨기기', hint: '같은 허용/차단 로그가 짧은 시간 안에 반복되면 다시 표시하지 않습니다.' },
      { key: 'repeat_log_window_seconds', type: 'number', min: 5, max: 600, unit: '초', label: '반복 로그 숨김 시간', hint: '5 ~ 600초, 기본 30초' },
      { key: 'log_to_file', type: 'bool', label: '접속 로그를 파일로 저장', hint: 'logs 폴더에 날짜별 파일로 저장합니다.' },
      { key: 'log_retention_days', type: 'number', min: 1, max: 365, unit: '일', label: '접속 로그 보관 기간', hint: '1 ~ 365일' },
      { key: 'audit_enabled', type: 'bool', label: '설정 변경 이력 기록', hint: '환경설정, 키워드 변경, 차단 시작/해제 등을 settings-history.log 에 기록합니다.' },
      { type: 'actions', buttons: [{ label: '로그 폴더 열기', target: 'logs' }, { label: '설정 변경 이력 열기', target: 'audit' }] },
    ],
  },
  {
    tab: '보안',
    items: [
      { key: 'password_max_fails', type: 'number', min: 1, max: 20, unit: '회', label: '비밀번호 실패 허용 횟수', hint: '초과하면 일정 시간 잠급니다. 1 ~ 20회' },
      { key: 'password_lockout_seconds', type: 'number', min: 5, max: 3600, unit: '초', label: '비밀번호 잠금 시간', hint: '5 ~ 3600초' },
    ],
  },
];

function settingRow(item, values, admin, inputs) {
  if (item.type === 'bool') {
    const disabled = Boolean(item.adminOnly && !admin);
    const input = h('input', { type: 'checkbox', class: 'peer sr-only', checked: Boolean(values[item.key]), disabled });
    inputs[item.key] = input;
    return h(
      'label',
      { class: `flex items-start gap-3 rounded-xl p-3 ${disabled ? 'opacity-60' : 'cursor-pointer hover:bg-slate-50'}` },
      input,
      h('span', {
        class:
          "relative mt-0.5 h-6 w-11 shrink-0 rounded-full bg-slate-300 transition peer-checked:bg-indigo-600 peer-focus-visible:ring-2 peer-focus-visible:ring-indigo-500/60 after:absolute after:top-0.5 after:left-0.5 after:h-5 after:w-5 after:rounded-full after:bg-white after:shadow after:transition after:content-[''] peer-checked:after:translate-x-5",
      }),
      h('span', { class: 'min-w-0' }, h('span', { class: 'block text-sm font-medium text-slate-800', text: item.label }), h('span', { class: 'hint mt-0.5 block', text: disabled ? `${item.hint} (현재 관리자 권한 아님)` : item.hint })),
    );
  }
  if (item.type === 'actions') {
    return h(
      'div',
      { class: 'flex gap-2 p-3' },
      item.buttons.map((b) => h('button', { type: 'button', class: 'btn btn-soft btn-sm', text: b.label, onclick: () => guarded(() => call('POST', '/api/open', { target: b.target })) })),
    );
  }
  let control;
  if (item.type === 'timerange') {
    const [startKey, endKey] = item.keys;
    const start = h('input', { class: 'input w-28', type: 'time', value: values[startKey] });
    const end = h('input', { class: 'input w-28', type: 'time', value: values[endKey] });
    inputs[startKey] = start;
    inputs[endKey] = end;
    control = h('div', { class: 'flex items-center gap-2' }, start, h('span', { class: 'text-slate-400', text: '~' }), end);
  } else {
    const input = h('input', {
      class: item.type === 'text' ? 'input' : 'input w-28',
      type: item.type === 'number' ? 'number' : 'text',
      value: values[item.key],
      min: item.min,
      max: item.max,
      step: item.step,
      maxlength: item.maxlength,
    });
    inputs[item.key] = input;
    control = h('div', { class: 'flex items-center gap-2' }, input, item.unit ? h('span', { class: 'text-sm text-slate-500', text: item.unit }) : null);
  }
  return h('div', { class: 'p-3' }, h('div', { class: 'mb-1.5 text-sm font-medium text-slate-800', text: item.label }), control, h('div', { class: 'hint mt-1', text: item.hint }));
}

function openSettings(password, values, admin) {
  const inputs = {};
  const pages = SETTINGS_SCHEMA.map((section) => h('div', { class: 'hidden divide-y divide-slate-100' }, section.items.map((item) => settingRow(item, values, admin, inputs))));
  const tabs = SETTINGS_SCHEMA.map((section, index) =>
    h('button', {
      type: 'button',
      class: 'w-full rounded-xl px-3 py-2 text-left text-sm font-medium',
      text: section.tab,
      onclick: () => selectTab(index),
    }),
  );
  function selectTab(index) {
    pages.forEach((page, i) => page.classList.toggle('hidden', i !== index));
    tabs.forEach((tab, i) => {
      tab.className = `w-full rounded-xl px-3 py-2 text-left text-sm font-medium transition ${i === index ? 'bg-indigo-50 text-indigo-700' : 'text-slate-600 hover:bg-slate-50'}`;
    });
  }
  selectTab(0);

  const error = h('div', { class: 'hidden whitespace-pre-line rounded-xl bg-rose-50 px-3 py-2 text-sm text-rose-700' });
  const body = h(
    'div',
    { class: 'flex flex-col gap-3' },
    error,
    h('div', { class: 'flex h-[56vh] gap-4' }, h('nav', { class: 'w-32 shrink-0 space-y-1' }, tabs), h('div', { class: 'min-w-0 flex-1 overflow-y-auto pr-1' }, pages)),
  );
  const save = h('button', { class: 'btn btn-primary', text: '저장' });
  const modal = openModal({
    title: '환경설정',
    body,
    size: 'max-w-3xl',
    actions: [h('button', { class: 'btn btn-ghost', text: '취소', onclick: () => modal.close(false) }), save],
  });
  save.addEventListener('click', async () => {
    const settings = {};
    for (const [key, input] of Object.entries(inputs)) settings[key] = input.type === 'checkbox' ? input.checked : input.value.trim();
    save.disabled = true;
    error.classList.add('hidden');
    try {
      const res = await call('POST', '/api/settings/save', { password, settings });
      modal.close(true);
      toast(res.message, 'success');
    } catch (err) {
      error.textContent = err.message;
      error.classList.remove('hidden');
    } finally {
      save.disabled = false;
    }
  });
}

async function settingsFlow() {
  let loaded = null;
  let password = '';
  const ok = await withPassword('환경설정을 엽니다.', async (pw) => {
    loaded = await call('POST', '/api/settings/get', { password: pw });
    password = pw;
  });
  if (ok && loaded) openSettings(password, loaded.settings, loaded.admin);
}

// ------------------------------------------------------------------ 초기화
function bind() {
  $('#btn-start').addEventListener('click', startBlocking);
  $('#btn-stop').addEventListener('click', stopBlocking);
  $('#btn-temp').addEventListener('click', tempUnlock);
  $('#btn-exit').addEventListener('click', requestExit);
  $('#kw-form').addEventListener('submit', (e) => {
    e.preventDefault();
    addKeyword();
  });
  $('#kw-remove').addEventListener('click', removeKeywords);
  $('#kw-reset').addEventListener('click', resetKeywords);
  $('#kw-import').addEventListener('click', importKeywords);
  $('#kw-export').addEventListener('click', exportKeywords);
  $('#test-form').addEventListener('submit', (e) => {
    e.preventDefault();
    testDomain();
  });
  $('#tool-settings').addEventListener('click', settingsFlow);
  $('#tool-password').addEventListener('click', changePassword);
  $('#tool-proxy').addEventListener('click', showProxyStatus);
  $('#tool-services').addEventListener('click', () => guarded(() => call('POST', '/api/open', { target: 'services' })));
  $('#tool-version').addEventListener('click', showVersionInfo);
  $('#log-clear').addEventListener('click', clearLogs);
  const log = $('#log');
  log.addEventListener('scroll', () => {
    if (logIgnoreScroll) return;
    followLog(logGap(log) <= LOG_STICK_PX);
  }, { passive: true });
  log.addEventListener('wheel', (e) => {
    if (e.deltaY < 0 && log.scrollHeight > log.clientHeight + LOG_STICK_PX) followLog(false);
  }, { passive: true });
  $('#log-jump').addEventListener('click', () => {
    followLog(true);
    scrollLogToLatest();
  });
  $('#link-tistory').addEventListener('click', () => bridge.openExternal(BLOG_URL));
  $('#link-github').addEventListener('click', () => bridge.openExternal(GITHUB_URL));
  $('#btn-update').addEventListener('click', () => guarded(checkUpdate));
  $('#overlay-retry').addEventListener('click', async () => {
    showOverlay('엔진을 시작하는 중…', "관리자 권한 확인(UAC) 창이 뜨면 '예'를 선택해 주세요.", false);
    const res = await bridge.restartBackend();
    if (!res.ok) showOverlay('엔진에 연결할 수 없습니다', '엔진을 시작하지 못했습니다. 잠시 후 다시 시도해 주세요.', true);
  });
}

async function init() {
  bind();
  renderLogFilters();
  const info = await bridge.info();
  if (info.logo) $('#logo').src = info.logo;
  bridge.onPoll((data) => {
    if (data.reset) clearLogs();
    const logs = data.events.filter((ev) => ev.kind === 'log');
    if (logs.length) appendLogs(logs);
    applyState(data.state);
    hideOverlay();
  });
  bridge.onBackend(({ connected, message }) => {
    if (!connected) showOverlay('엔진에 연결할 수 없습니다', message || 'EduGuard 엔진이 응답하지 않습니다.', true);
  });
  bridge.onRequestExit(requestExit);
}

init();
