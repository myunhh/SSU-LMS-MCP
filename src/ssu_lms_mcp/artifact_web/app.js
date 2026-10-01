'use strict';

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const state = {
    manifest: null, documents: new Map(), drafts: new Map(), csrf: '', root: '',
    active: null, route: null, routeSequence: 0, expanded: new Set(), mode: 'read',
    loading: true, saving: false, conflict: null, storageWarning: false,
  };
  const roles = { materials: '학습 자료', reviews: '복습 큐', records: '학습 기록' };
  const symbols = { materials: '▤', reviews: '◷', records: '▧', page: '▱', database: '▦' };
  const ratings = { understood: '이해함', uncertain: '애매함', unknown: '모름' };
  const readonlyProperties = new Set(['키', '과목ID', '자료키', 'SHA256', '수정버전', '수집일', '기준일', '복습일', '최근평가', '시도', '활성', '난도우선', '마지막수정', '처리이벤트', 'LMS', '원본', '최신학습', '자료', '상태']);
  const reviewProperties = new Set(['내답', '평가', '제출']);
  const errors = {
    artifact_revision_conflict: '다른 변경 사항이 있습니다. 내 초안을 유지한 채 저장본과 비교하세요.',
    artifact_document_not_found: '문서를 찾을 수 없습니다. 목록을 새로고침해 주세요.',
    artifact_repository_conflict: '라이브러리 파일 충돌이 발견되었습니다. OneDrive 충돌 파일을 확인해야 합니다.',
    artifact_repository_uninitialized: '아직 연결된 라이브러리가 없습니다. 초기화 또는 마이그레이션을 완료해 주세요.',
    artifact_invalid_document: '문서 내용을 저장할 수 없습니다. 입력 형식과 파일 상태를 확인해 주세요.',
    artifact_duplicate_key: '같은 식별 키의 문서가 있습니다. 원본을 덮어쓰지 않았습니다.',
    csrf_invalid: '편집기 연결이 만료되었습니다. 초안은 유지됩니다. 새로고침 후 다시 시도하세요.',
  };
  const escape = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const text = (value) => value == null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const node = (tag, className, value) => {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (value !== undefined) result.textContent = value;
    return result;
  };
  const button = (label, action, className = 'quiet-button') => {
    const result = node('button', className, label);
    result.type = 'button';
    result.addEventListener('click', action);
    return result;
  };
  const pageURL = id => `#/page/${encodeURIComponent(id)}`;
  const roleURL = (role, filter = 'all') => `#/collection/${role}?filter=${filter}`;
  const dayFormatter = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit' });
  const calendarDay = (value = Date.now()) => {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    const parts = dayFormatter.formatToParts(date);
    return ['year', 'month', 'day'].map(type => parts.find(part => part.type === type).value).join('-');
  };
  const dateLabel = value => {
    if (!value) return '—';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? text(value) : new Intl.DateTimeFormat('ko-KR', { year: 'numeric', month: 'short', day: 'numeric', ...(String(value).includes('T') ? { hour: '2-digit', minute: '2-digit' } : {}) }).format(date);
  };
  const property = (doc, name) => doc.properties?.[name];
  const isPending = doc => property(doc, '제출') === '__YES__' || property(doc, '제출') === true;
  const isActive = doc => !['__NO__', false].includes(property(doc, '활성'));
  const dueAt = doc => property(doc, 'date:복습일:start') || property(doc, '복습일')?.start || property(doc, '복습일') || '';
  const isDue = doc => {
    if (!doc.managed_key || !isActive(doc) || isPending(doc)) return false;
    const due = calendarDay(dueAt(doc));
    return Boolean(due) && due <= calendarDay();
  };
  const isWeak = doc => Boolean(doc.managed_key) && isActive(doc) && ['모름', '애매함', 'unknown', 'uncertain'].includes(property(doc, '최근평가'));
  const roleOf = doc => {
    const seen = new Set();
    while (doc && !seen.has(doc.id)) {
      seen.add(doc.id);
      if (doc.role && roles[doc.role]) return doc.role;
      const collection = Object.entries(state.manifest?.collections || {}).find(([, id]) => id === doc.id);
      if (collection) return collection[0];
      doc = state.documents.get(doc.parent_id);
    }
    return null;
  };
  const isCollection = doc => Object.values(state.manifest?.collections || {}).includes(doc.id);
  const rowsFor = role => [...state.documents.values()].filter(doc => roleOf(doc) === role && !isCollection(doc) && doc.kind !== 'database');
  const dirtyDocument = draft => draft && (draft.title !== draft.base.title || draft.content !== draft.base.content || Object.keys(draft.properties).some(key => !equal(draft.properties[key], draft.base.properties?.[key])));
  const dirty = draft => dirtyDocument(draft) || Boolean(draft?.reviewDirty);
  const activeDraft = () => state.active ? state.drafts.get(state.active) : null;

  function notify(message) {
    const toast = $('#toast');
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(notify.timer);
    notify.timer = setTimeout(() => { toast.hidden = true; }, 5000);
  }
  function showBanner(message, action, label = '다시 시도') {
    const banner = $('#banner');
    banner.replaceChildren(node('span', '', message));
    if (action) banner.append(button(label, action));
    banner.append(button('닫기', () => { banner.hidden = true; }, 'text-button'));
    banner.hidden = false;
  }
  function messageFor(error) {
    return errors[error.code] || (error.status === 403 ? '연결 보안 확인에 실패했습니다. 초안을 유지하고 목록을 새로고침한 뒤 다시 시도하세요.' : error.message || '요청을 완료할 수 없습니다. 초안은 유지됩니다.');
  }
  async function api(path, method = 'GET', body) {
    let response;
    try {
      response = await fetch(path, { method, credentials: 'same-origin', cache: 'no-store', headers: body === undefined ? {} : { 'Content-Type': 'application/json', 'X-SSU-CSRF': state.csrf }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    } catch {
      throw new Error('로컬 편집기에 연결할 수 없습니다. 실행 창을 확인하세요. 내 초안은 유지됩니다.');
    }
    const result = await response.json().catch(() => null);
    if (!response.ok || !result) {
      const error = new Error(response.ok ? '서버의 응답 형식을 확인할 수 없습니다.' : `요청을 완료하지 못했습니다 (HTTP ${response.status}). 초안은 유지됩니다.`);
      error.status = response.status;
      error.code = typeof result?.error === 'string' ? result.error : result?.error?.code;
      throw error;
    }
    return result;
  }
  const draftKey = () => `ssu-artifact-drafts:${state.manifest?.hub_id}`;
  function persistDrafts() {
    if (!state.manifest) return;
    try {
      const drafts = [...state.drafts.values()].filter(dirty);
      if (drafts.length) sessionStorage.setItem(draftKey(), JSON.stringify(drafts));
      else sessionStorage.removeItem(draftKey());
    } catch {
      if (!state.storageWarning) showBanner('탭 임시 저장 공간을 사용할 수 없습니다. 초안은 현재 화면에 남아 있지만, 탭을 닫기 전에 문서를 저장하거나 초안을 내려받아 주세요.');
      state.storageWarning = true;
    }
  }
  function restoreDrafts() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(draftKey()) || '[]');
      for (const draft of saved) {
        if (draft?.base?.id && typeof draft.title === 'string' && typeof draft.content === 'string' && draft.properties && typeof draft.properties === 'object') state.drafts.set(draft.base.id, draft);
      }
      if (saved.length) notify('이 탭의 저장하지 않은 초안을 복원했습니다.');
    } catch {
      showBanner('탭에 보관된 초안을 읽을 수 없습니다. 저장된 원본 문서는 변경되지 않았습니다.');
    }
  }
  function makeDraft(doc) {
    return { base: doc, title: doc.title || '', content: doc.content || '', properties: {}, answer: text(property(doc, '내답')), rating: '', reviewDirty: false, conflict: false };
  }
  function changed() {
    persistDrafts();
    updateStatus();
  }
  function updateStatus() {
    const draft = activeDraft();
    const unsaved = [...state.drafts.values()].filter(dirty).length;
    const status = $('#save-status');
    status.className = `save-status${dirty(draft) ? ' is-dirty' : ''}${draft?.conflict ? ' is-conflict' : ''}`;
    status.textContent = state.loading ? '불러오는 중' : !state.manifest ? '연결되지 않음' : state.saving ? '로컬 파일 저장 중…' : draft?.conflict ? '충돌 · 내 초안 유지됨' : dirtyDocument(draft) ? '저장하지 않은 변경' : draft?.reviewDirty ? '미제출 복습 답안' : unsaved ? `다른 페이지 초안 ${unsaved}개` : '로컬 파일 저장됨';
    status.title = '로컬 저장 상태입니다. OneDrive 클라우드 동기화 상태는 OneDrive 앱에서 확인하세요.';
    $('#save').disabled = state.saving || state.loading || !dirtyDocument(draft);
  }
  async function bootstrap(initial = false) {
    try {
      const result = await api('/api/bootstrap');
      state.manifest = result.manifest;
      state.csrf = result.csrf_token;
      state.root = result.root_path;
      state.documents = new Map(result.documents.map(doc => [doc.id, doc]));
      $('#root-path').textContent = result.root_path;
      $('.workspace strong').textContent = result.manifest.title || 'SSU 학습 라이브러리';
      if (initial) restoreDrafts();
      state.loading = false;
      renderNavigation();
      await renderRoute();
      if (!initial) notify('저장된 문서 목록을 새로고침했습니다. 내 초안은 유지됩니다.');
    } catch (error) {
      state.loading = false;
      if (initial) {
        const box = emptyState('라이브러리에 연결하지 못했습니다', messageFor(error));
        box.append(button('다시 연결', () => bootstrap(true), 'primary-button'));
        $('#main').replaceChildren(box);
      } else showBanner(messageFor(error), () => bootstrap(false));
      updateStatus();
    }
  }

  function navLink(label, href, icon, count, active = false) {
    const link = node('a', `nav-link${active ? ' active' : ''}`);
    link.href = href;
    if (active) link.setAttribute('aria-current', 'page');
    link.append(node('span', 'nav-icon', icon), node('span', 'nav-label', label));
    if (count !== undefined) link.append(node('span', 'nav-count', String(count)));
    return link;
  }
  function renderNavigation() {
    if (!state.manifest) return;
    const nav = $('#primary-nav');
    nav.replaceChildren(navLink('라이브러리 홈', '#/', '⌂', undefined, state.route?.type === 'home'));
    for (const role of Object.keys(roles)) {
      nav.append(navLink(roles[role], roleURL(role), symbols[role], rowsFor(role).length, state.route?.role === role));
      if (role === 'reviews') {
        const sub = node('div', 'nav-sub');
        sub.append(navLink('오늘의 복습', roleURL(role, 'due'), '', rowsFor(role).filter(isDue).length, state.route?.role === role && state.route.filter === 'due'), navLink('취약 개념', roleURL(role, 'weak'), '', rowsFor(role).filter(isWeak).length, state.route?.role === role && state.route.filter === 'weak'));
        nav.append(sub);
      }
      if (role === 'records') {
        const sub = node('div', 'nav-sub');
        sub.append(navLink('학습 노트', roleURL(role, 'learning'), '', undefined, state.route?.role === role && state.route.filter === 'learning'), navLink('일일 브리핑', roleURL(role, 'briefings'), '', undefined, state.route?.role === role && state.route.filter === 'briefings'), navLink('시험 대비', roleURL(role, 'exams'), '', undefined, state.route?.role === role && state.route.filter === 'exams'));
        nav.append(sub);
      }
    }
    const children = new Map();
    for (const doc of state.documents.values()) {
      const key = state.documents.has(doc.parent_id) ? doc.parent_id : null;
      if (!children.has(key)) children.set(key, []);
      children.get(key).push(doc);
    }
    const ancestors = new Set();
    let current = state.documents.get(state.active);
    while (current && !ancestors.has(current.id)) { ancestors.add(current.id); current = state.documents.get(current.parent_id); }
    const visited = new Set();
    const branch = doc => {
      if (visited.has(doc.id)) return null;
      visited.add(doc.id);
      const descendants = children.get(doc.id) || [];
      const link = navLink(doc.title || '제목 없음', pageURL(doc.id), symbols[doc.kind] || symbols.page, undefined, state.active === doc.id);
      if (dirty(state.drafts.get(doc.id))) link.append(node('span', 'draft-dot', '•'));
      if (!descendants.length) { link.classList.add('leaf-link'); return link; }
      const container = node('details', 'tree-branch');
      container.open = state.expanded.has(doc.id) || ancestors.has(doc.id);
      const summary = node('summary');
      summary.append(link);
      const inner = node('div', 'tree-children');
      for (const child of descendants) { const item = branch(child); if (item) inner.append(item); }
      container.append(summary, inner);
      container.addEventListener('toggle', () => container.open ? state.expanded.add(doc.id) : state.expanded.delete(doc.id));
      return container;
    };
    const tree = $('#page-tree');
    const scroll = tree.scrollTop;
    tree.replaceChildren();
    for (const doc of children.get(null) || []) { const item = branch(doc); if (item) tree.append(item); }
    for (const doc of state.documents.values()) if (!visited.has(doc.id)) { const item = branch(doc); if (item) tree.append(item); }
    tree.scrollTop = scroll;
    renderSearch();
  }
  function renderSearch() {
    const query = $('#search').value.trim().toLocaleLowerCase();
    const results = $('#search-results');
    results.hidden = !query;
    results.replaceChildren();
    if (!query) return;
    const matches = [...state.documents.values()].filter(doc => `${state.drafts.get(doc.id)?.title || doc.title} ${JSON.stringify(doc.properties || {})}`.toLocaleLowerCase().includes(query));
    results.append(node('div', 'search-caption', `${matches.length}개 문서 · 제목과 속성`));
    for (const doc of matches) results.append(navLink(doc.title || '제목 없음', pageURL(doc.id), symbols[doc.kind] || symbols.page));
    if (!matches.length) results.append(node('p', 'muted search-empty', '일치하는 문서가 없습니다. 다른 검색어를 입력해 보세요.'));
  }
  function parseRoute() {
    const [path, query = ''] = location.hash.slice(1).split('?');
    const parts = (path || '/').split('/').filter(Boolean);
    if (parts[0] === 'page' && parts[1]) return { type: 'page', id: parts[1] };
    if (parts[0] === 'collection' && roles[parts[1]]) return { type: 'collection', role: parts[1], filter: new URLSearchParams(query).get('filter') || 'all' };
    return { type: 'home' };
  }
  function breadcrumbs(doc, label) {
    const target = $('#breadcrumbs');
    target.replaceChildren();
    const home = node('a', '', '라이브러리'); home.href = '#/'; target.append(home);
    const path = [];
    const seen = new Set();
    while (doc && !seen.has(doc.id)) { seen.add(doc.id); path.unshift(doc); doc = state.documents.get(doc.parent_id); }
    for (const item of path) { const link = node('a', '', item.title || '제목 없음'); link.href = pageURL(item.id); target.append(node('span', 'breadcrumb-divider', '/'), link); }
    if (label) target.append(node('span', 'breadcrumb-divider', '/'), node('span', '', label));
  }
  function emptyState(title, description) {
    const container = node('section', 'empty-state');
    container.append(node('div', 'empty-symbol', '▱'), node('h2', '', title), node('p', 'muted', description));
    return container;
  }
  async function renderRoute() {
    if (!state.manifest) return;
    persistDrafts();
    const sequence = ++state.routeSequence;
    state.route = parseRoute();
    state.active = state.route.type === 'page' ? state.route.id : null;
    $('#sidebar').classList.remove('mobile-open');
    $('#menu-toggle').setAttribute('aria-expanded', 'false');
    $('#banner').hidden = true;
    renderNavigation();
    if (state.route.type === 'home') { state.loading = false; renderHome(); updateStatus(); return; }
    if (state.route.type === 'collection') { state.loading = false; renderCollection(); updateStatus(); return; }
    const id = state.active;
    state.loading = true;
    breadcrumbs(state.documents.get(id));
    $('#main').replaceChildren(node('div', 'loading-state', '문서를 불러오고 있습니다…'));
    updateStatus();
    try {
      const { document: doc } = await api(`/api/documents/${encodeURIComponent(id)}`);
      const parent = state.documents.get(doc.parent_id);
      if (parent?.kind === 'database' && !parent.schema) {
        const response = await api(`/api/documents/${encodeURIComponent(parent.id)}`);
        state.documents.set(parent.id, response.document);
      }
      if (sequence !== state.routeSequence) return;
      state.documents.set(doc.id, doc);
      let draft = state.drafts.get(id);
      if (!draft || !dirty(draft)) { draft = makeDraft(doc); state.drafts.set(id, draft); }
      else draft.conflict = draft.base.revision !== doc.revision;
      state.loading = false;
      state.mode = dirtyDocument(draft) ? 'split' : doc.content ? 'read' : 'edit';
      renderDocument(draft);
      renderNavigation();
      if (draft.conflict) showBanner('저장본이 변경되었습니다. 보관된 내 초안을 먼저 표시합니다.', () => openConflict(draft), '저장본과 비교');
      updateStatus();
    } catch (error) {
      if (sequence !== state.routeSequence) return;
      state.loading = false;
      const draft = state.drafts.get(id);
      if (draft) { renderDocument(draft); showBanner(messageFor(error), renderRoute); }
      else { const empty = emptyState('문서를 열 수 없습니다', messageFor(error)); empty.append(button('다시 불러오기', renderRoute)); $('#main').replaceChildren(empty); }
      updateStatus();
    }
  }
  function renderHome() {
    breadcrumbs(null, '홈');
    document.title = `${state.manifest.title || '라이브러리'} · SSU 학습`;
    const canvas = node('div', 'canvas home-canvas');
    canvas.append(node('div', 'eyebrow', 'MY LEARNING SPACE'), node('h1', '', state.manifest.title || 'SSU 학습 라이브러리'), node('p', 'lead', '자료를 연결하고, 생각을 남기고, 배운 것을 다시 꺼내는 공간.'));
    const cards = node('div', 'collection-cards');
    for (const role of Object.keys(roles)) {
      const card = node('a', `collection-card ${role}`); card.href = roleURL(role);
      card.append(node('span', 'card-icon', symbols[role]), node('h2', '', roles[role]), node('p', '', role === 'materials' ? '강의 원본과 학습 자료' : role === 'reviews' ? '내 답으로 확인하는 학습' : '학습 노트 · 브리핑 · 시험 대비'), node('span', 'card-count', `${rowsFor(role).length}개 문서 →`)); cards.append(card);
    }
    canvas.append(cards);
    const reviewRows = rowsFor('reviews');
    const queue = node('section', 'home-review');
    const queueText = node('div'); queueText.append(node('h2', '', '오늘 다시 살펴볼 것'), node('p', 'muted', `복습 기한이 된 카드 ${reviewRows.filter(isDue).length}개 · 취약 개념 ${reviewRows.filter(isWeak).length}개 · 제출 대기 ${reviewRows.filter(isPending).length}개`));
    const link = node('a', 'primary-button', '복습 큐 열기'); link.href = roleURL('reviews', 'due'); queue.append(queueText, link); canvas.append(queue);
    const drafts = [...state.drafts.values()].filter(dirty);
    if (drafts.length) {
      const section = node('section', 'home-section'); section.append(node('h2', '', '이 탭의 미저장 초안'));
      section.append(documentList(drafts.map(draft => ({ ...draft.base, title: draft.title })), 'drafts')); canvas.append(section);
    }
    const recent = [...state.documents.values()].filter(doc => doc.kind !== 'database').sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at))).slice(0, 12);
    const section = node('section', 'home-section'); section.append(node('h2', '', '최근 업데이트'));
    section.append(recent.length ? documentList(recent) : emptyState('아직 문서가 없습니다', '새 페이지를 만들거나 가져오기를 완료하면 이곳에 표시됩니다.')); canvas.append(section);
    $('#main').replaceChildren(canvas);
  }
  function filterRows(rows, filter) {
    if (filter === 'due') return rows.filter(isDue).sort((a, b) => String(dueAt(a)).localeCompare(String(dueAt(b))));
    if (filter === 'weak') return rows.filter(isWeak).sort((a, b) => Number(property(a, '난도우선') ?? 9) - Number(property(b, '난도우선') ?? 9));
    if (filter === 'pending') return rows.filter(isPending);
    if (filter === 'learning') return rows.filter(doc => !['일일브리핑', '시험대비'].includes(property(doc, '분류')));
    if (filter === 'briefings') return rows.filter(doc => property(doc, '분류') === '일일브리핑');
    if (filter === 'exams') return rows.filter(doc => property(doc, '분류') === '시험대비');
    return rows;
  }
  function renderCollection() {
    const { role, filter } = state.route;
    breadcrumbs(null, roles[role]);
    document.title = `${roles[role]} · SSU 학습`;
    const canvas = node('div', 'canvas collection-canvas');
    canvas.append(node('div', 'page-symbol', symbols[role]), node('h1', '', roles[role]), node('p', 'lead', role === 'reviews' ? '정답을 열기 전에 내 답을 적어 보세요. 제출한 자기 평가는 다음 학습 실행에 반영됩니다.' : role === 'materials' ? '강의 자료와 원본 링크를 한곳에서 찾아보세요.' : '학습 흐름과 새로 연결한 생각들을 모아 둡니다.'));
    const database = state.manifest.collections[role];
    if (database) { const link = node('a', 'collection-document-link', '컬렉션 소개·본문 편집 ↗'); link.href = pageURL(database); canvas.append(link); }
    const controls = node('div', 'collection-controls');
    const tabs = node('nav', 'filter-tabs'); tabs.setAttribute('aria-label', '보기 필터');
    const filters = role === 'reviews' ? { all: '전체', due: '오늘의 복습', weak: '취약 개념', pending: '제출 대기' } : role === 'records' ? { all: '전체', learning: '학습 노트', briefings: '일일 브리핑', exams: '시험 대비' } : { all: '전체 자료' };
    const rows = rowsFor(role);
    for (const [key, label] of Object.entries(filters)) { const tab = node('a', `filter-tab${filter === key ? ' active' : ''}`, `${label} ${filterRows(rows, key).length}`); tab.href = roleURL(role, key); if (filter === key) tab.setAttribute('aria-current', 'page'); tabs.append(tab); }
    const search = node('input', 'collection-search'); search.type = 'search'; search.placeholder = '이 컬렉션에서 검색'; search.setAttribute('aria-label', '컬렉션 내 검색');
    controls.append(tabs, search); canvas.append(controls);
    const list = node('div');
    const draw = () => {
      const query = search.value.trim().toLocaleLowerCase();
      let selected = filterRows(rows, filter).filter(doc => `${doc.title} ${JSON.stringify(doc.properties || {})}`.toLocaleLowerCase().includes(query));
      if (!['due', 'weak'].includes(filter)) selected = selected.sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
      list.replaceChildren(selected.length ? documentList(selected, role) : emptyState(query ? '검색 결과가 없습니다' : filter === 'due' ? '기한이 된 복습이 없습니다' : '이 보기에 문서가 없습니다', query ? '제목이나 속성으로 다시 검색해 보세요.' : '다른 보기를 선택하거나 새 페이지를 만들어 보세요.'));
    };
    search.addEventListener('input', draw); draw(); canvas.append(list); $('#main').replaceChildren(canvas);
  }
  function documentList(documents, role) {
    const list = node('div', 'document-list');
    for (const doc of documents) {
      const row = node('a', 'document-row'); row.href = pageURL(doc.id);
      row.append(node('span', 'row-icon', symbols[doc.kind] || symbols.page));
      const title = node('div', 'row-main'); title.append(node('strong', '', doc.title || '제목 없음'));
      const info = [property(doc, '과목'), property(doc, '주차'), property(doc, '개념'), property(doc, '분류')].filter(Boolean).map(text).join(' · ');
      if (info) title.append(node('span', 'row-description', info)); row.append(title);
      const meta = node('div', 'row-meta');
      if (dirty(state.drafts.get(doc.id))) meta.append(node('span', 'badge badge-warm', '초안'));
      if (role === 'reviews') {
        if (isPending(doc)) meta.append(node('span', 'badge badge-blue', '다음 실행 대기'));
        else if (isDue(doc)) meta.append(node('span', 'badge badge-warm', '복습 기한'));
        if (property(doc, '최근평가')) meta.append(node('span', `badge${isWeak(doc) ? ' badge-warm' : ''}`, text(property(doc, '최근평가'))));
        meta.append(node('time', '', dueAt(doc) ? dateLabel(dueAt(doc)) : '복습일 없음'));
      } else meta.append(node('time', '', dateLabel(doc.updated_at)));
      row.append(meta, node('span', 'row-arrow', '↗')); list.append(row);
    }
    return list;
  }

  function renderDocument(draft) {
    const doc = draft.base;
    breadcrumbs({ ...doc, title: draft.title });
    document.title = `${draft.title || '제목 없음'} · SSU 학습`;
    const canvas = node('article', 'canvas document-canvas');
    canvas.append(node('div', 'page-symbol', symbols[doc.kind] || symbols.page));
    const title = node('textarea', 'document-title'); title.id = 'document-title'; title.rows = 2; title.value = draft.title; title.placeholder = '제목 없음'; title.setAttribute('aria-label', '페이지 제목');
    title.addEventListener('input', () => { draft.title = title.value.replace(/\n/g, ' '); changed(); }); canvas.append(title);
    const meta = node('div', 'page-metadata'); meta.append(node('span', '', doc.kind === 'database' ? '컬렉션' : roles[roleOf(doc)] || '개인 페이지'), node('span', 'metadata-divider', '·'), node('time', 'updated-time', `수정 ${dateLabel(doc.updated_at)}`)); canvas.append(meta);
    renderProperties(canvas, draft);
    const editor = node('section', 'editor-section');
    const heading = node('div', 'editor-heading');
    const modes = node('div', 'mode-switch'); modes.setAttribute('aria-label', '본문 표시 방식');
    for (const [key, label] of Object.entries({ read: '읽기', edit: '편집', split: '나란히 보기' })) {
      const mode = button(label, () => setMode(key), 'mode-button'); mode.dataset.mode = key; modes.append(mode);
    }
    heading.append(modes);
    const utilities = node('div', 'editor-utilities'); utilities.append(button('초안 내려받기', () => downloadDraft(draft), 'text-button'));
    utilities.append(button('저장본 보기', async () => {
      try { const { document: latest } = await api(`/api/documents/${doc.id}`); openSavedView(latest); } catch (error) { showBanner(messageFor(error)); }
    }, 'text-button'));
    heading.append(utilities); editor.append(heading);
    const toolbar = node('div', 'format-toolbar'); toolbar.id = 'format-toolbar'; toolbar.setAttribute('aria-label', 'Markdown 서식');
    const formats = [ ['H1', '큰 제목', '# ', '', true], ['H2', '소제목', '## ', '', true], ['B', '굵게', '**', '**'], ['I', '기울임', '*', '*'], ['링크', '링크', '[', '](https://)'], ['목록', '글머리 목록', '- ', '', true], ['인용', '인용', '> ', '', true], ['코드', '코드 블록', '\n```text\n', '\n```\n'], ['수식', '수식 블록', '\n$$\n', '\n$$\n'], ['표', '표 삽입', '\n| 항목 | 내용 |\n| --- | --- |\n| ', ' |  |\n'], ['토글', '접힌 답안', '\n<details>\n<summary>정답과 해설</summary>\n\n', '\n\n</details>\n'], ['콜아웃', '콜아웃', '\n<callout>\n\n', '\n\n</callout>\n'] ];
    for (const [label, description, start, end, lines] of formats) { const control = button(label, () => formatSelection(start, end, lines), 'format-button'); control.title = description; control.setAttribute('aria-label', description); toolbar.append(control); }
    editor.append(toolbar);
    const panes = node('div', 'editor-panes'); panes.id = 'editor-panes';
    const source = node('div', 'source-pane'); source.append(node('label', 'pane-label', '전체 Markdown 원문'));
    const textarea = node('textarea', 'markdown-input'); textarea.id = 'markdown-input'; textarea.value = draft.content; textarea.spellcheck = false; textarea.setAttribute('aria-label', '전체 Markdown 본문'); textarea.placeholder = '여기에 생각을 적어 보세요. Markdown, 수식, 표를 사용할 수 있습니다.';
    textarea.addEventListener('input', () => { draft.content = textarea.value; changed(); schedulePreview(draft.content); });
    textarea.addEventListener('keydown', event => {
      if (event.key === 'Tab') { event.preventDefault(); textarea.setRangeText('  ', textarea.selectionStart, textarea.selectionEnd, 'end'); textarea.dispatchEvent(new Event('input')); }
      if ((event.ctrlKey || event.metaKey) && ['b', 'i'].includes(event.key.toLowerCase())) { event.preventDefault(); formatSelection(event.key.toLowerCase() === 'b' ? '**' : '*', event.key.toLowerCase() === 'b' ? '**' : '*'); }
    });
    source.append(textarea, node('p', 'source-hint', '전체 원문을 편집합니다. 스냅샷 경계는 원문에 보존됩니다. Ctrl/⌘ S로 문서 저장.'));
    const preview = node('div', 'preview-pane'); preview.append(node('div', 'pane-label', '읽기 미리보기'));
    const rich = node('div', 'rich-content'); rich.id = 'rich-preview'; preview.append(rich);
    panes.append(source, preview); editor.append(panes); canvas.append(editor);
    if (roleOf(doc) === 'reviews' && doc.managed_key && !isCollection(doc) && doc.kind !== 'database') renderReview(canvas, draft);
    const childDocs = [...state.documents.values()].filter(child => child.parent_id === doc.id);
    if (childDocs.length) { const children = node('section', 'child-pages'); children.append(node('h2', '', `하위 페이지 ${childDocs.length}`), documentList(childDocs)); canvas.append(children); }
    $('#main').replaceChildren(canvas);
    renderRich(rich, draft.content);
    setMode(state.mode);
  }
  function setMode(mode) {
    state.mode = mode;
    const panes = $('#editor-panes');
    if (!panes) return;
    panes.className = `editor-panes mode-${mode}`;
    $('#format-toolbar').hidden = mode === 'read';
    for (const control of document.querySelectorAll('[data-mode]')) { control.classList.toggle('active', control.dataset.mode === mode); control.setAttribute('aria-pressed', String(control.dataset.mode === mode)); }
    if (mode !== 'edit') renderRich($('#rich-preview'), activeDraft()?.content || '');
  }
  function formatSelection(start, end, lines = false) {
    const area = $('#markdown-input'); if (!area) return;
    if (state.mode === 'read') setMode('split');
    let from = area.selectionStart; const to = area.selectionEnd;
    if (lines) from = area.value.lastIndexOf('\n', from - 1) + 1;
    const selected = area.value.slice(from, to);
    const replacement = lines ? selected.split('\n').map(line => start + line).join('\n') : start + selected + end;
    area.focus(); area.setRangeText(replacement, from, to, 'select');
    if (!lines) area.setSelectionRange(from + start.length, from + start.length + selected.length);
    area.dispatchEvent(new Event('input'));
  }
  function schedulePreview(content) {
    clearTimeout(schedulePreview.timer);
    schedulePreview.timer = setTimeout(() => { const target = $('#rich-preview'); if (target && state.mode !== 'edit' && activeDraft()?.content === content) renderRich(target, content); }, 200);
  }
  function schemaFor(doc, key) {
    const parent = state.documents.get(doc.parent_id);
    for (const schema of [doc.schema, parent?.schema]) {
      if (schema?.properties?.[key] || schema?.[key]) return schema.properties?.[key] || schema[key];
      for (const source of Object.values(schema || {})) if (source?.state?.schema?.[key]) return source.state.schema[key];
    }
    return {};
  }
  function renderProperties(canvas, draft) {
    const entries = Object.entries(draft.base.properties || {});
    if (!entries.length) return;
    const panel = node('details', 'properties-panel'); panel.open = entries.length <= 9;
    const summary = node('summary', 'properties-heading'); summary.append(node('span', '', `속성 ${entries.length}개`), node('span', 'muted', '자동 관리 속성은 읽기 전용')); panel.append(summary);
    const grid = node('div', 'property-grid');
    for (const [key, original] of entries) {
      if (/^date:.*:(end|is_datetime)$/.test(key)) continue;
      const name = key.replace(/^date:(.*):start$/, '$1');
      const row = node('div', 'property-row');
      const label = node('label', 'property-label', name);
      const schema = schemaFor(draft.base, name);
      const readonly = readonlyProperties.has(name) || reviewProperties.has(name) || ['title', 'created_time', 'last_edited_time', 'created_by', 'last_edited_by', 'formula', 'rollup'].includes(schema.type) || typeof original === 'object' && original !== null;
      const value = Object.hasOwn(draft.properties, key) ? draft.properties[key] : original;
      const id = `property-${grid.children.length}`;
      label.htmlFor = id;
      row.append(label);
      if (readonly) {
        const display = node('div', 'property-readonly'); display.id = id;
        const url = typeof value === 'string' && safeURL(value);
        if (url && /^(https?:|#\/|\/assets\/)/.test(url)) { const link = node('a', '', text(value)); link.href = url; secureLink(link); display.append(link); }
        else display.textContent = name === '제출' ? isPending(draft.base) ? '다음 실행 대기' : '미제출' : /^date:/.test(key) || name === '마지막수정' ? dateLabel(value) : ['__YES__', '__NO__'].includes(value) ? value === '__YES__' ? '예' : '아니요' : text(value) || '비어 있음';
        display.title = reviewProperties.has(name) ? '아래 복습 제출 영역에서 직접 입력하세요.' : '자동 관리 또는 원본 구조 보존 속성';
        row.append(display, node('span', 'property-lock', '읽기 전용'));
      } else {
        let input;
        const options = schema.select?.options || schema.options || (name === '분류' ? ['자료버전', '일일브리핑', '시험대비'] : name === '유형' ? ['주차자료', '게시판'] : null);
        if (Array.isArray(options)) {
          input = node('select');
          const values = [...new Set(['', text(value), ...options.map(option => typeof option === 'string' ? option : option.name)].filter(item => item !== undefined))];
          for (const optionValue of values) { const option = node('option', '', optionValue || '비어 있음'); option.value = optionValue; input.append(option); } input.value = text(value);
        } else if (typeof value === 'boolean' || ['__YES__', '__NO__'].includes(value) || schema.type === 'checkbox') { input = node('input'); input.type = 'checkbox'; input.checked = value === true || value === '__YES__'; }
        else { input = node('input'); input.type = typeof value === 'number' || schema.type === 'number' ? 'number' : /^date:/.test(key) || schema.type === 'date' ? 'date' : schema.type === 'url' ? 'url' : 'text'; if (input.type === 'number') input.step = 'any'; input.value = input.type === 'date' ? text(value).slice(0, 10) : text(value); }
        input.id = id;
        input.addEventListener('input', () => {
          let next = input.type === 'checkbox' ? typeof original === 'boolean' ? input.checked : input.checked ? '__YES__' : '__NO__' : input.type === 'number' ? input.value === '' ? null : Number(input.value) : input.value;
          if (equal(next, original)) delete draft.properties[key]; else draft.properties[key] = next;
          changed();
        }); row.append(input);
      }
      grid.append(row);
    }
    panel.append(grid); canvas.append(panel);
  }
  function renderReview(canvas, draft) {
    const section = node('section', 'review-panel'); section.id = 'review-panel';
    section.append(node('div', 'eyebrow', 'ACTIVE RECALL'), node('h2', '', '내 답으로 복습하기'), node('p', 'muted', '먼저 답을 떠올린 뒤 직접 자기 평가를 선택하세요. AI가 대신 답하거나 자동 평가하지 않습니다.'));
    const pending = node('p', 'review-pending'); pending.id = 'review-pending'; pending.textContent = isPending(draft.base) ? '제출됨 · 다음 학습 실행에서 반영됩니다. 아직 채점·일정 갱신이 완료된 상태는 아닙니다.' : '제출 전 · 답안은 이 탭의 초안으로만 보관됩니다.'; section.append(pending);
    const label = node('label', 'field-label', '내 답'); label.htmlFor = 'review-answer';
    const answer = node('textarea', 'review-answer'); answer.id = 'review-answer'; answer.rows = 5; answer.value = draft.answer; answer.placeholder = '내가 이해한 내용을 직접 적어 주세요.';
    answer.addEventListener('input', () => { draft.answer = answer.value; draft.reviewDirty = true; changed(); });
    section.append(label, answer);
    const fieldset = node('fieldset', 'rating-group'); fieldset.append(node('legend', 'field-label', '자기 평가 · 직접 선택'));
    for (const [value, title] of Object.entries(ratings)) {
      const option = node('label', `rating-option rating-${value}`); const input = node('input'); input.type = 'radio'; input.name = 'review-rating'; input.value = value; input.checked = draft.rating === value;
      input.addEventListener('change', () => { draft.rating = value; draft.reviewDirty = true; changed(); }); option.append(input, node('span', '', title)); fieldset.append(option);
    }
    section.append(fieldset);
    const actions = node('div', 'review-actions'); actions.append(node('p', 'muted', '본문 저장과 별개입니다. 제출 버튼을 눌러야 학습 실행에 전달됩니다.'), button(isPending(draft.base) ? '답안 다시 제출' : '복습 답안 제출', submitReview, 'primary-button')); actions.lastChild.id = 'review-submit'; section.append(actions); canvas.append(section);
  }

  function patchFor(draft) {
    const patch = { revision: draft.base.revision };
    if (draft.title !== draft.base.title) patch.title = draft.title;
    if (draft.content !== draft.base.content) patch.content = draft.content;
    const properties = Object.fromEntries(Object.entries(draft.properties).filter(([key, value]) => !equal(value, draft.base.properties?.[key])));
    if (Object.keys(properties).length) patch.properties = properties;
    return patch;
  }
  function acceptSaved(draft, doc, sent) {
    const old = draft.base;
    if (draft.title === old.title || draft.title === sent.title) draft.title = doc.title;
    if (draft.content === old.content || draft.content === sent.content) draft.content = doc.content;
    for (const [key, value] of Object.entries(draft.properties)) if (equal(value, sent.properties?.[key]) || equal(value, doc.properties?.[key])) delete draft.properties[key];
    draft.base = doc; draft.conflict = false; state.documents.set(doc.id, doc);
    persistDrafts(); renderNavigation(); updateStatus();
    if (state.active === doc.id) { const time = $('.updated-time'); if (time) time.textContent = `수정 ${dateLabel(doc.updated_at)}`; breadcrumbs({ ...doc, title: draft.title }); document.title = `${draft.title || '제목 없음'} · SSU 학습`; }
  }
  async function saveDocument(draft = activeDraft()) {
    if (!draft || state.saving) return false;
    if (!dirtyDocument(draft)) return true;
    if (!draft.title.trim()) { showBanner('제목을 입력한 뒤 저장하세요. 초안은 유지됩니다.'); $('#document-title')?.focus(); return false; }
    const sent = patchFor(draft);
    state.saving = true; updateStatus();
    try {
      const { document: doc } = await api(`/api/documents/${draft.base.id}`, 'PATCH', sent);
      acceptSaved(draft, doc, sent);
      notify('이 PC의 로컬 파일에 저장했습니다. OneDrive 동기화는 앱에서 확인하세요.');
      return true;
    } catch (error) {
      if (error.status === 409) { draft.conflict = true; await openConflict(draft); }
      else showBanner(messageFor(error), () => saveDocument(draft), '다시 저장');
      persistDrafts(); return false;
    } finally { state.saving = false; updateStatus(); }
  }
  async function submitReview() {
    const draft = activeDraft();
    if (!draft || state.saving) return;
    if (!Object.hasOwn(ratings, draft.rating)) { showBanner('자기 평가를 직접 선택해야 제출할 수 있습니다.'); $('#review-panel input[type="radio"]')?.focus(); return; }
    if (!draft.answer.trim()) { showBanner('내 답을 입력한 뒤 제출하세요.'); $('#review-answer')?.focus(); return; }
    if (dirtyDocument(draft) && !await saveDocument(draft)) return;
    const sent = { revision: draft.base.revision, rating: draft.rating, answer: draft.answer };
    state.saving = true; updateStatus();
    const control = $('#review-submit'); if (control) control.disabled = true;
    try {
      const { document: doc } = await api(`/api/reviews/${draft.base.id}/submit`, 'POST', sent);
      acceptSaved(draft, doc, {});
      if (draft.answer === sent.answer && draft.rating === sent.rating) draft.reviewDirty = false;
      changed();
      if (state.active === doc.id) { $('#review-pending').textContent = '제출됨 · 다음 학습 실행에서 반영됩니다. 아직 채점·일정 갱신이 완료된 상태는 아닙니다.'; $('#review-submit').textContent = '답안 다시 제출'; }
      notify('복습 답안을 로컬 파일에 제출했습니다. 다음 학습 실행에서 반영됩니다.');
    } catch (error) {
      if (error.status === 409) { draft.conflict = true; await openConflict(draft); }
      else showBanner(messageFor(error));
      persistDrafts();
    } finally { state.saving = false; if (control) control.disabled = false; updateStatus(); }
  }

  function openNew() {
    if (!state.manifest) return;
    const select = $('#new-parent'); select.replaceChildren();
    for (const doc of state.documents.values()) { const option = node('option', '', doc.title || '제목 없음'); option.value = doc.id; select.append(option); }
    const preferred = state.active || state.manifest.collections[state.route?.role] || state.manifest.hub_id;
    if (state.documents.has(preferred)) select.value = preferred;
    $('#new-error').textContent = ''; $('#new-title').value = ''; $('#new-dialog').showModal(); $('#new-title').focus();
  }
  async function createPage(event) {
    event.preventDefault();
    const title = $('#new-title').value.trim(); if (!title) return;
    $('#create-submit').disabled = true; $('#new-error').textContent = '';
    try {
      const { document: doc } = await api('/api/documents', 'POST', { title, content: '', parent_id: $('#new-parent').value || null });
      state.documents.set(doc.id, doc); $('#new-dialog').close(); state.mode = 'edit'; location.hash = pageURL(doc.id); notify('새 페이지를 로컬 파일에 만들었습니다.');
    } catch (error) { $('#new-error').textContent = messageFor(error); }
    finally { $('#create-submit').disabled = false; }
  }
  function downloadDraft(draft) {
    const review = draft.reviewDirty ? `\n\n---\n\n## 미제출 복습 초안\n\n${draft.answer}\n\n자기 평가: ${ratings[draft.rating] || '미선택'}\n` : '';
    const blob = new Blob([`# ${draft.title}\n\n${draft.content}${review}\n\n<!-- 속성 초안: ${JSON.stringify(draft.properties).replace(/-->/g, '-- >')} -->\n`], { type: 'text/markdown;charset=utf-8' });
    const href = URL.createObjectURL(blob); const link = node('a'); link.href = href; link.download = `${draft.title.replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').slice(0, 100) || '학습 초안'}.md`; link.click(); setTimeout(() => URL.revokeObjectURL(href), 1000);
  }
  function openSavedView(doc) {
    let dialog = $('#saved-dialog');
    if (!dialog) { dialog = node('dialog', 'modal saved-modal'); dialog.id = 'saved-dialog'; document.body.append(dialog); }
    dialog.replaceChildren(); const header = node('div', 'modal-heading'); header.append(node('h2', '', `${doc.title} · 저장본`), button('닫기', () => dialog.close())); dialog.append(header, node('p', 'muted', '읽기 전용 비교 화면입니다. 현재 편집 중인 초안은 바뀌지 않습니다.'));
    const content = node('div', 'rich-content'); renderRich(content, doc.content); dialog.append(content); dialog.showModal();
  }
  async function openConflict(draft) {
    try {
      const { document: latest } = await api(`/api/documents/${draft.base.id}`);
      const fields = [];
      if (draft.title !== draft.base.title) fields.push({ key: 'title', label: '제목', mine: draft.title, theirs: latest.title, before: draft.base.title });
      if (draft.content !== draft.base.content) fields.push({ key: 'content', label: '본문', mine: draft.content, theirs: latest.content, before: draft.base.content });
      for (const [key, value] of Object.entries(draft.properties)) fields.push({ key, property: true, label: `속성 · ${key}`, mine: value, theirs: latest.properties?.[key], before: draft.base.properties?.[key] });
      state.conflict = { draft, latest, fields };
      const content = $('#conflict-content'); content.replaceChildren();
      if (!fields.length) content.append(node('p', '', '문서 저장본을 최신으로 바꾸고 미제출 답안은 그대로 유지합니다. 병합 후 답안을 직접 다시 제출하세요.'));
      for (const [index, field] of fields.entries()) {
        const section = node('section', 'conflict-field'); section.append(node('h3', '', field.label));
        const compare = node('div', 'conflict-compare');
        for (const [kind, label, value] of [['mine', '내 초안', field.mine], ['theirs', '최신 저장본', field.theirs]]) {
          const side = node('div'); const choice = node('label', 'conflict-choice'); const radio = node('input'); radio.type = 'radio'; radio.name = `conflict-${index}`; radio.value = kind;
          if (equal(field.theirs, field.before) || equal(field.mine, field.theirs)) radio.checked = kind === 'mine';
          choice.append(radio, node('strong', '', label)); side.append(choice, node('pre', 'conflict-text', text(value))); compare.append(side);
        }
        section.append(compare); content.append(section);
      }
      if (fields.some(field => !equal(field.theirs, field.before) && !equal(field.mine, field.theirs))) content.prepend(node('p', 'conflict-warning', '같은 필드가 양쪽에서 변경되었습니다. 각 항목에서 유지할 내용을 직접 선택하세요. 최신 자동 스냅샷도 본문에 포함될 수 있습니다.'));
      if (!$('#conflict-dialog').open) $('#conflict-dialog').showModal();
    } catch (error) { showBanner(`비교할 저장본을 불러오지 못했습니다. ${messageFor(error)}`, () => openConflict(draft), '다시 비교'); }
  }
  function rebaseConflict() {
    const conflict = state.conflict; if (!conflict) return;
    const choices = conflict.fields.map((field, index) => $(`input[name="conflict-${index}"]:checked`, $('#conflict-content'))?.value);
    if (choices.some(value => !value)) { notify('충돌한 각 항목에서 유지할 내용을 선택하세요.'); return; }
    const { draft, latest, fields } = conflict;
    const properties = {};
    let title = latest.title; let content = latest.content;
    fields.forEach((field, index) => {
      if (choices[index] !== 'mine') return;
      if (field.property) properties[field.key] = field.mine;
      else if (field.key === 'title') title = field.mine;
      else content = field.mine;
    });
    draft.base = latest; draft.title = title; draft.content = content; draft.properties = properties; draft.conflict = false;
    state.documents.set(latest.id, latest); $('#conflict-dialog').close(); state.conflict = null; changed();
    if (state.active === latest.id) { state.mode = 'split'; renderDocument(draft); }
    notify('선택한 내용으로 초안을 병합했습니다. 문서 저장 또는 복습 제출로 확정하세요.');
  }
  async function copyConflict() {
    if (!state.conflict) return;
    const { draft } = state.conflict;
    const control = $('#conflict-copy'); control.disabled = true;
    try {
      const answer = draft.reviewDirty ? `\n\n## 미제출 복습 초안\n\n${draft.answer}\n\n자기 평가: ${ratings[draft.rating] || '미선택'}\n` : '';
      const { document: doc } = await api('/api/documents', 'POST', { title: `${draft.title} · 초안 사본`, content: draft.content + answer, parent_id: draft.base.parent_id, properties: { '초안 원본': `${location.origin}/${pageURL(draft.base.id)}` } });
      state.documents.set(doc.id, doc); $('#conflict-dialog').close(); notify('초안을 별도 페이지에 보관했습니다. 원래 페이지의 초안도 유지됩니다.'); location.hash = pageURL(doc.id);
    } catch (error) { showBanner(messageFor(error)); }
    finally { control.disabled = false; }
  }

  // Only known local documents are rewritten. Other links stay explicit external links.
  function safeURL(raw, image = false) {
    if (typeof raw === 'string' && raw.startsWith('{{') && raw.endsWith('}}')) raw = raw.slice(2, -2);
    if (typeof raw !== 'string' || /[\x00-\x20<>"\\]/.test(raw) || !/^(?:https?:\/\/|mailto:|collection:\/\/|notion:|#|\/assets\/)/i.test(raw)) return null;
    try {
      const url = new URL(raw, location.href);
      if (url.username || url.password) return null;
      if (image) return url.origin === location.origin && /^\/assets\/[A-Za-z0-9._-]+$/.test(url.pathname) ? url.pathname : null;
      if (raw.startsWith('#/page/')) return state.documents.has(raw.slice(7)) ? raw : null;
      if (raw.startsWith('#') && !raw.startsWith('#/')) return raw;
      if (url.origin === location.origin && /^\/assets\/[A-Za-z0-9._-]+$/.test(url.pathname)) return url.pathname + url.hash;
      const notionHost = /(^|\.)(notion\.so|notion\.site|notion\.com)$/.test(url.hostname);
      const localRoute = url.origin === location.origin && /^#\/page\//.test(url.hash);
      if (notionHost || localRoute || url.protocol === 'collection:' || url.protocol === 'notion:') {
        const match = `${url.pathname}${url.hash}${url.protocol === 'collection:' ? url.hostname : ''}`.match(/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32})(?![0-9a-f])/i);
        if (match) {
          const id = match[1].replace(/-/g, '').toLowerCase();
          const doc = [...state.documents.values()].find(item => item.id.replace(/-/g, '').toLowerCase() === id || Object.keys(item.schema || {}).some(key => key.replace(/-/g, '').toLowerCase().endsWith(id)));
          if (doc) return pageURL(doc.id);
        }
      }
      return ['https:', 'http:', 'mailto:'].includes(url.protocol) ? url.href : null;
    } catch { return null; }
  }
  function secureLink(link) {
    const safe = safeURL(link.getAttribute('href'));
    if (!safe) { link.removeAttribute('href'); link.classList.add('unavailable-link'); return; }
    link.setAttribute('href', safe);
    if (safe.startsWith('#')) { link.removeAttribute('target'); link.removeAttribute('rel'); }
    else { link.target = '_blank'; link.rel = 'noopener noreferrer'; link.referrerPolicy = 'no-referrer'; }
  }
  function renderMath(expression, displayMode) {
    try { return katex.renderToString(expression, { displayMode, throwOnError: true, trust: false, strict: 'ignore', maxExpand: 1000, maxSize: 20, output: 'htmlAndMathml' }); }
    catch { return `<code class="math-error" title="수식 문법을 확인하세요">${escape(expression)}</code>`; }
  }
  const md = window.markdownit({ html: true, linkify: false, typographer: false, breaks: true });
  const mathSlots = [];
  function mathPlaceholder(expression, display) {
    const index = mathSlots.push({ expression, display }) - 1;
    return `<span class="ssu-math-placeholder" data-math-slot="${index}"></span>`;
  }
  md.inline.ruler.before('text', 'ssu_math', (parser, silent) => {
    const start = parser.pos;
    const marker = parser.src[start] === '$' ? parser.src.startsWith('$$', start) ? '$$' : '$' : null;
    if (!marker) return false;
    if (marker === '$' && /\s/.test(parser.src[start + 1] || ' ')) return false;
    let end = parser.src.indexOf(marker, start + marker.length);
    while (end !== -1 && parser.src[end - 1] === '\\') end = parser.src.indexOf(marker, end + marker.length);
    if (end < 0 || marker === '$' && (parser.src.slice(start + 1, end).includes('\n') || /\s/.test(parser.src[end - 1]) || /\d/.test(parser.src[end + 1] || ''))) return false;
    if (!silent) {
      const token = parser.push('ssu_math', '', 0);
      let expression = parser.src.slice(start + marker.length, end);
      if (expression.startsWith('`') && expression.endsWith('`')) expression = expression.slice(1, -1);
      token.content = expression; token.meta = { display: marker === '$$' };
    }
    parser.pos = end + marker.length; return true;
  });
  md.inline.ruler.before('text', 'ssu_math_boundary', (parser, silent) => {
    const prefix = parser.src.slice(parser.pos).match(/^[^\\\n`*_\[\]!<&$]+(?=\$)/);
    if (!prefix) return false;
    if (!silent) parser.pending += prefix[0];
    parser.pos += prefix[0].length; return true;
  });
  md.renderer.rules.ssu_math = (tokens, index) => mathPlaceholder(tokens[index].content, tokens[index].meta.display);
  md.block.ruler.before('fence', 'ssu_math_block', (parser, startLine, endLine, silent) => {
    const first = parser.src.slice(parser.bMarks[startLine] + parser.tShift[startLine], parser.eMarks[startLine]).trim();
    const marker = first.startsWith('$$') ? '$$' : first === '\\[' ? '\\[' : null;
    if (!marker || first.length > marker.length && !first.endsWith(marker === '$$' ? '$$' : '\\]')) return false;
    const closer = marker === '$$' ? '$$' : '\\]';
    let end = startLine; let content;
    if (first.length > marker.length && first.endsWith(closer)) content = first.slice(marker.length, -closer.length);
    else {
      for (end = startLine + 1; end < endLine; end++) if (parser.src.slice(parser.bMarks[end] + parser.tShift[end], parser.eMarks[end]).trim() === closer) break;
      if (end === endLine) return false;
      content = parser.src.slice(parser.bMarks[startLine + 1], parser.bMarks[end]);
    }
    if (silent) return true;
    const token = parser.push('ssu_math', '', 0); token.content = content; token.meta = { display: true }; token.block = true; parser.line = end + 1; return true;
  }, { alt: ['paragraph', 'reference', 'blockquote', 'list'] });
  function notionMarkup(raw) {
    const match = raw.match(/^<(page|database|folder|mention-page|mention-database|mention-data-source|mention-agent|mention-user|mention-date|file|pdf|audio|video|embed)\b([^>]*?)(?:\/>|>([\s\S]*?)<\/\1>)/i);
    if (!match) return null;
    const attributes = match[2]; const target = attributes.match(/(?:url|href|id|src)\s*=\s*["']([^"']+)["']/i)?.[1];
    const label = (match[3] || attributes.match(/(?:title|name|date|start)\s*=\s*["']([^"']+)["']/i)?.[1] || (match[1].includes('database') ? '컬렉션' : match[1].includes('date') ? target || '날짜' : ['file', 'pdf', 'audio', 'video', 'embed'].includes(match[1]) ? '첨부 파일 열기' : '연결된 페이지')).replace(/<[^>]*>/g, '');
    let href = target ? safeURL(target) : null;
    if (target && state.documents.has(target)) href = pageURL(target);
    return { length: match[0].length, html: href ? `<a class="notion-reference" href="${escape(href)}">${escape(label)}</a>` : `<span class="notion-reference unavailable-link">${escape(label)}</span>` };
  }
  md.inline.ruler.before('html_inline', 'ssu_notion_reference', (parser, silent) => {
    if (parser.src[parser.pos] !== '<') return false;
    const result = notionMarkup(parser.src.slice(parser.pos)); if (!result) return false;
    if (!silent) { const token = parser.push('html_inline', '', 0); token.content = result.html; }
    parser.pos += result.length; return true;
  });
  md.block.ruler.before('html_block', 'ssu_enhanced_block', (parser, startLine, endLine, silent) => {
    const offset = parser.bMarks[startLine] + parser.tShift[startLine];
    const first = parser.src.slice(offset, parser.eMarks[startLine]).trim();
    if (/^SSU-AUTO\s+\S+(?:\s+\S+)?\s+(BEGIN|END)$/.test(first)) {
      if (silent) return true;
      const token = parser.push('html_block', '', 0); token.content = first.endsWith('BEGIN') ? '<div class="snapshot-boundary" role="separator" aria-label="보존된 학습 스냅샷"><span>학습 스냅샷</span></div>\n' : '<div class="snapshot-end" role="separator" aria-label="스냅샷 끝"></div>\n'; parser.line = startLine + 1; return true;
    }
    const reference = notionMarkup(parser.src.slice(offset));
    if (reference) {
      const consumed = offset + reference.length;
      let line = startLine; while (line < endLine && parser.eMarks[line] < consumed) line++;
      if (parser.src.slice(consumed, parser.eMarks[line]).trim()) return false;
      if (silent) return true;
      const token = parser.push('html_block', '', 0); token.content = `<p>${reference.html}</p>`;
      parser.line = line + 1; return true;
    }
    const opener = first.match(/^<(details|callout|columns|column|synced_block|synced_block_reference|table)(?:\s[^>]*)?>/i);
    if (!opener || (parser.env.ssuDepth || 0) > 24) return false;
    const tag = opener[1].toLowerCase();
    const source = parser.src.slice(offset, parser.bMarks[endLine] ?? parser.src.length);
    const pattern = new RegExp(`<\\/?${tag}\\b[^>]*>`, 'gi');
    let depth = 0; let close = null; let match;
    while ((match = pattern.exec(source))) { depth += /^<\//.test(match[0]) ? -1 : 1; if (depth === 0) { close = { index: match.index, end: pattern.lastIndex }; break; } }
    if (!close) return false;
    let line = startLine; while (line < endLine && parser.eMarks[line] < offset + close.end) line++;
    if (parser.src.slice(offset + close.end, parser.eMarks[line]).trim()) return false;
    if (silent) return true;
    if (tag === 'table') {
      const tableHTML = source.slice(0, close.end).replace(/<(td|th)(\s[^>]*)?>([\s\S]*?)<\/\1>/gi, (whole, cell, attributes, value) => `<${cell}${attributes || ''}>${md.renderInline(value, parser.env)}</${cell}>`);
      const token = parser.push('html_block', '', 0); token.content = tableHTML;
      parser.line = line + 1; return true;
    }
    let body = source.slice(opener[0].length, close.index); let summary = '자세히 보기';
    if (tag === 'details') { const found = body.match(/^\s*<summary(?:\s[^>]*)?>([\s\S]*?)<\/summary>/i); if (found) { summary = found[1]; body = body.slice(found[0].length); } }
    const lines = body.replace(/^\n|\n$/g, '').split('\n');
    const nonempty = lines.filter(value => value.trim());
    if (nonempty.length && nonempty.every(value => /^\t/.test(value))) body = lines.map(value => value.replace(/^\t/, '')).join('\n');
    else if (nonempty.length) { const indent = Math.min(...nonempty.map(value => value.match(/^ */)[0].length)); if (indent) body = lines.map(value => value.slice(indent)).join('\n'); }
    const environment = { ...parser.env, ssuDepth: (parser.env.ssuDepth || 0) + 1 };
    const rendered = md.render(body, environment);
    const token = parser.push('html_block', '', 0);
    token.content = tag === 'details' ? `<details${/\bopen(?:\s|=|>)/i.test(opener[0]) ? ' open' : ''}><summary>${md.renderInline(summary, environment)}</summary><div class="toggle-content">${rendered}</div></details>` : `<div class="${tag === 'callout' ? 'callout' : `notion-${tag}`}">${rendered}</div>`;
    parser.line = line + 1; return true;
  }, { alt: ['paragraph', 'reference', 'blockquote', 'list'] });
  function renderRich(target, source) {
    if (!target) return;
    if (!source.trim()) { target.replaceChildren(node('p', 'empty-document', '아직 본문이 없습니다. 편집 탭에서 첫 문장을 남겨 보세요.')); return; }
    mathSlots.length = 0;
    try {
      const rendered = md.render(source);
      const clean = DOMPurify.sanitize(rendered, { USE_PROFILES: { html: true }, ADD_TAGS: ['details', 'summary'], ADD_ATTR: ['open'], FORBID_TAGS: ['style', 'form', 'input', 'button', 'textarea', 'select', 'iframe', 'object', 'embed', 'script', 'svg', 'math', 'video', 'audio', 'source'], FORBID_ATTR: ['style', 'srcset', 'id', 'name', 'autofocus'], ALLOW_DATA_ATTR: true, RETURN_DOM_FRAGMENT: true });
      const contentClasses = /^(?:ssu-math-placeholder|snapshot-boundary|snapshot-end|notion-reference|notion-columns|notion-column|notion-synced_block|notion-synced_block_reference|toggle-content|callout|unavailable-link|language-[A-Za-z0-9_-]+)$/;
      for (const element of clean.querySelectorAll('[class]')) {
        const classes = [...element.classList].filter(value => contentClasses.test(value));
        if (classes.length) element.className = classes.join(' '); else element.removeAttribute('class');
      }
      for (const link of clean.querySelectorAll('a')) secureLink(link);
      for (const image of clean.querySelectorAll('img')) {
        const src = safeURL(image.getAttribute('src'), true);
        if (!src) { const alt = node('span', 'blocked-image', image.alt ? `[외부 이미지: ${image.alt}]` : '[외부 이미지 · 자동 요청하지 않음]'); image.replaceWith(alt); }
        else { image.src = src; image.loading = 'lazy'; image.removeAttribute('width'); image.removeAttribute('height'); }
      }
      for (const placeholder of clean.querySelectorAll('.ssu-math-placeholder')) {
        const slot = mathSlots[Number(placeholder.dataset.mathSlot)];
        if (!slot) { placeholder.remove(); continue; }
        // Imported HTML is sanitized first; only trust:false KaTeX output may add layout styles.
        placeholder.innerHTML = renderMath(slot.expression, slot.display);
        placeholder.className = slot.display ? 'math-block' : 'math-inline'; placeholder.removeAttribute('data-math-slot');
      }
      for (const table of clean.querySelectorAll('table')) { const wrapper = node('div', 'table-scroll'); table.replaceWith(wrapper); wrapper.append(table); }
      for (const quote of clean.querySelectorAll('blockquote')) { const paragraph = quote.querySelector('p'); if (paragraph && /^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]/i.test(paragraph.textContent)) quote.classList.add('callout'); }
      target.replaceChildren(clean);
    } catch {
      target.replaceChildren(node('p', 'error-text', '미리보기를 해석할 수 없어 전체 원문을 표시합니다. 내용은 변경되지 않았습니다.'), node('pre', 'raw-fallback', source));
    } finally { mathSlots.length = 0; }
  }

  $('#search').addEventListener('input', renderSearch);
  $('#search').addEventListener('keydown', event => { if (event.key === 'Escape') { $('#search').value = ''; renderSearch(); $('#search').blur(); } if (event.key === 'Enter') $('#search-results a')?.click(); });
  $('#new-page').addEventListener('click', openNew); $('#new-sidebar').addEventListener('click', openNew);
  $('#new-form').addEventListener('submit', createPage);
  $('#save').addEventListener('click', () => saveDocument());
  $('#refresh').addEventListener('click', () => bootstrap(false));
  $('#conflict-rebase').addEventListener('click', rebaseConflict); $('#conflict-copy').addEventListener('click', copyConflict);
  $('#about-button').addEventListener('click', () => $('#about-dialog').showModal());
  for (const control of document.querySelectorAll('[data-close]')) control.addEventListener('click', () => document.getElementById(control.dataset.close).close());
  $('#menu-toggle').addEventListener('click', () => { const open = $('#sidebar').classList.toggle('mobile-open'); $('#menu-toggle').setAttribute('aria-expanded', String(open)); });
  document.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); saveDocument(); }
    if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName) && !document.querySelector('dialog[open]')) { event.preventDefault(); $('#search').focus(); $('#sidebar').classList.add('mobile-open'); }
  });
  window.addEventListener('hashchange', renderRoute);
  window.addEventListener('beforeunload', event => { persistDrafts(); if ([...state.drafts.values()].some(dirty)) { event.preventDefault(); event.returnValue = ''; } });
  bootstrap(true);
})();
