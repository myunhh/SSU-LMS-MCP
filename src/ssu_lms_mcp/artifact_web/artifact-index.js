(() => {
  if (location.hash.startsWith('#/')) {
    location.replace(`/study.html${location.hash}`);
    return;
  }
  const list = document.querySelector('#artifact-list');
  const error = document.querySelector('#error');
  const rootPath = document.querySelector('#root-path');
  const count = document.querySelector('#artifact-count');
  const refresh = document.querySelector('#refresh');

  function formatSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let value = bytes;
    let unit = -1;
    do { value /= 1024; unit += 1; } while (value >= 1024 && unit < units.length - 1);
    return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
  }

  function fileURL(path) {
    return `/artifacts/${path.split('/').map(encodeURIComponent).join('/')}`;
  }

  function element(tag, className, text) {
    const value = document.createElement(tag);
    if (className) value.className = className;
    if (text !== undefined) value.textContent = text;
    return value;
  }

  async function load() {
    refresh.disabled = true;
    error.hidden = true;
    list.replaceChildren(element('p', 'empty', '파일 목록을 불러오는 중…'));
    try {
      const response = await fetch('/api/artifacts', { headers: { Accept: 'application/json' }, cache: 'no-store' });
      if (!response.ok) throw new Error('목록을 읽지 못했습니다. 편집기를 다시 시작한 뒤 새로고침해 주세요.');
      const data = await response.json();
      if (!Array.isArray(data.artifacts) || typeof data.root_path !== 'string') throw new Error('저장소 응답 형식이 올바르지 않습니다.');
      rootPath.textContent = data.root_path;
      count.textContent = `${data.artifacts.length}개 파일`;
      if (!data.artifacts.length) {
        list.replaceChildren(element('p', 'empty', '아직 파일이 없습니다. AI 산출물을 이 폴더에 저장하면 링크가 표시됩니다.'));
        return;
      }

      const groups = new Map();
      for (const file of data.artifacts) {
        if (typeof file.path !== 'string' || typeof file.name !== 'string' || !Number.isSafeInteger(file.size)) continue;
        const pieces = file.path.split('/');
        const groupName = pieces.length > 1 ? pieces.slice(0, -1).join(' / ') : '기타';
        if (!groups.has(groupName)) groups.set(groupName, []);
        groups.get(groupName).push(file);
      }
      const fragment = document.createDocumentFragment();
      for (const [groupName, files] of [...groups].sort(([a], [b]) => a.localeCompare(b, 'ko'))) {
        const section = element('section', 'file-group');
        section.append(element('h3', 'group-title', groupName));
        const rows = element('ul', 'file-list');
        for (const file of files) {
          const row = element('li', 'file-row');
          const details = element('div', 'file-details');
          const link = element('a', 'file-link', file.name);
          link.href = fileURL(file.path);
          details.append(link, element('span', 'file-path', file.path));
          row.append(details, element('span', 'file-size', formatSize(file.size)));
          rows.append(row);
        }
        section.append(rows);
        fragment.append(section);
      }
      list.replaceChildren(fragment);
    } catch (cause) {
      list.replaceChildren();
      error.textContent = cause instanceof Error ? cause.message : '파일 목록을 불러오지 못했습니다.';
      error.hidden = false;
    } finally {
      refresh.disabled = false;
    }
  }

  refresh.addEventListener('click', load);
  load();
})();
