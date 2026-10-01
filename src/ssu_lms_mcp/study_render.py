"""Render untrusted learning data as artifact Markdown, never a page wrapper.

Only this module creates structural Markdown. Citations point at caller-supplied
stable resources; page numbers remain explicit even when the viewer cannot seek.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

_ESCAPE = frozenset('\\*~`$[]<>{}|^#!_+-().:')
_QUERY_KEYS = frozenset({'cid', 'resid', 'id'})
_MATH_COMMANDS = frozenset('frac dfrac tfrac sqrt sum prod int iint iiint lim log ln exp sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh min max sup inf arg det dim gcd mod bmod pmod partial nabla infty cdot times div pm mp le leq ge geq ne neq approx equiv sim simeq propto in notin subset subseteq supset supseteq cup cap emptyset forall exists neg land lor to rightarrow leftarrow leftrightarrow Rightarrow Leftarrow Leftrightarrow mapsto alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu xi pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega mathbb mathbf mathcal mathrm mathit mathsf mathtt boldsymbol text operatorname overline underline hat widehat bar vec dot ddot tilde widetilde left right big Big bigg Bigg begin end matrix pmatrix bmatrix vmatrix Vmatrix cases aligned align split quad qquad displaystyle textstyle limits nolimits underbrace overbrace overset underset binom choose degree circ perp parallel angle lvert rvert vert Vert lbrace rbrace langle rangle lceil rceil lfloor rfloor ldots cdots vdots ddots'.split())
_CODE = re.compile(r'^```(?:[A-Za-z0-9_+.-]*)\n(.*?)\n```[ \t]*(?=\n|$)', re.MULTILINE | re.DOTALL)
_RATINGS = {'understood': '이해함', 'uncertain': '애매함', 'unknown': '모름'}
_STATUSES = {'baseline': '기준선 원본 보관 · AI 학습자료 미생성',
             'published': '신규·개정 학습자료 게시', 'skipped': '건너뜀',
             'review_required': '검토 필요', 'partial': '일부 처리 실패',
             'done': '처리 완료', 'failed': '실패'}


def _plain(value) -> str:
    text = '' if value is None else str(value)
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\u2028', '\n').replace('\u2029', '\n')
    return ''.join(c for c in text if c in '\n\t' or unicodedata.category(c) not in {'Cc', 'Cf', 'Cs'})


def safe_text(value) -> str:
    """One rich-text block: no source-created links, tags, headings or children."""
    text = _plain(value).replace('&', '&amp;').replace('\t', '    ')
    return ''.join('\\' + c if c in _ESCAPE else '<br>' if c == '\n' else c for c in text)


def _safe_url(url) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    decoded = unquote(url)
    if any(c.isspace() or unicodedata.category(c) in {'Cc', 'Cf', 'Cs'} for c in decoded):
        return None
    if any(c in decoded for c in '<>"\\`'):
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme == 'http':
            if (parts.hostname == '127.0.0.1' and parts.username is None and parts.password is None
                    and parts.port is not None and 1024 <= parts.port <= 65535
                    and parts.path == '/' and not parts.query
                    and re.fullmatch(r'/page/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', parts.fragment)):
                return urlunsplit(('http', f'127.0.0.1:{parts.port}', '/', '', parts.fragment))
            return None
        if (parts.scheme != 'https' or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.port not in (None, 443)):
            return None
        host = parts.hostname.encode('idna').decode('ascii')
        if not re.fullmatch(r'[A-Za-z0-9.-]+', host) or '.' not in host:
            return None
        query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        if any(k.lower() not in _QUERY_KEYS or not re.fullmatch(r'[A-Za-z0-9!._-]{1,200}', v)
               for k, v in query):
            return None
        if parts.fragment and not re.fullmatch(r'page=[1-9][0-9]*', parts.fragment):
            return None
        return urlunsplit(('https', host, quote(parts.path, safe='/%:@!$&*+,;=-._~'),
                           parts.query, parts.fragment))
    except (ValueError, UnicodeError):
        return None


def safe_link(label, url) -> str:
    """Link credential-free HTTPS resources or exact local artifact page routes."""
    target = _safe_url(url)
    title = safe_text(label)
    return f'[{title}]({target})' if target else f'{title} (링크 확인 필요)'


def _body(value) -> str:
    """Preserve explicit code as literal, non-executing plain-text code blocks."""
    text = _plain(value)
    result = []
    end = 0
    for match in _CODE.finditer(text):
        code = match[1]
        if '```' in code:
            continue
        if match.start() > end:
            result.append(safe_text(text[end:match.start()].strip('\n')))
        # Never propagate mermaid/HTML or any other executable language metadata.
        result.append('\n```plain text\n' + code + '\n```\n')
        end = match.end()
    if end < len(text):
        result.append(safe_text(text[end:].strip('\n')))
    return '\n'.join(result)


def _pages(pages) -> str:
    numbers = list(dict.fromkeys(p for p in (pages or []) if type(p) is int and p > 0))
    return 'p. ' + ', '.join(map(str, numbers)) if numbers else '쪽 근거 미제공'


def _citation(pages, title, url) -> str:
    return safe_link(f'{title} · {_pages(pages)}', url)


def _references(sources, links) -> str:
    result = []
    for source in sources or []:
        key = source['source_key']
        target = links.get(key)
        if target is None:
            result.append(safe_text(f'출처 연결 확인 필요: {key} · {_pages(source.get("pages"))}'))
        else:
            result.append(_citation(source.get('pages'), target['title'], target['url']))
    return '; '.join(result) or '출처 근거 미제공'


def _toggle(answer, explanation='', references='') -> str:
    children = ['예시 정답', _body(answer)]
    if explanation:
        children.extend(['해설', _body(explanation)])
    if references:
        children.append(references)
    return '<details>\n<summary>정답과 해설</summary>\n' + '\n'.join(
        '\t' + line for child in children for line in child.split('\n')) + '\n</details>'


def _review_state(item) -> list[str]:
    reasons = item.get('review_reasons', [])
    required = item.get('review_required') or bool(reasons)
    lines = ['검토 상태: ' + ('불확실성 있음 · 원문 대조 필요' if required else '추가 검토 사유 보고 없음 · 내용의 정답 보증은 아님')]
    lines.extend('- ' + safe_text(reason) for reason in reasons)
    return lines


def _formula(expression) -> str:
    raw = _plain(expression).strip()
    if raw.startswith('$$') and raw.endswith('$$') and len(raw) > 4:
        raw = raw[2:-2].strip()
    commands = re.findall(r'\\([A-Za-z]+)', raw)
    # This is an injection boundary, not a TeX parser or mathematical validator.
    if (raw and not any(c in raw for c in '$`<>')
            and all(command in _MATH_COMMANDS for command in commands)
            and not re.search(r'\\(?:[^A-Za-z\s{},;!:%_#&|\\]|$)', raw)):
        return '$$\n' + raw + '\n$$'
    return '수식 원문 (안전한 텍스트 표시): ' + safe_text(expression)


def render_package(package, version, uploaded, related) -> str:
    source = version.get('source', {})
    title = source.get('title') or package['title']
    original = uploaded.get('web_url')
    def cite(pages):
        return _citation(pages, title, original or source.get('source_url'))

    lines = ['## ' + safe_text(package['title']),
             'AI 생성 학습자료입니다. 원문 근거와 보충 설명을 구분하고, 중요한 내용은 인용 쪽과 대조하세요.',
             '원본: ' + safe_link(title, original),
             'LMS: ' + safe_link('자료 게시 위치', source.get('source_url')),
             '버전: ' + safe_text(version.get('id', '')),
             '원본 SHA-256: ' + safe_text(package.get('source_sha256', '')),
             '분석 범위: ' + safe_text(_pages(package.get('coverage'))) + ' / 전체 ' + safe_text(package.get('page_count')) + '쪽']
    lines.extend(_review_state(package))
    lines.append('## 핵심 개요')
    for item in package.get('summary', []):
        label = '원문 근거' if item.get('evidence') == 'source' else '보충 설명 · 원문 직접 진술 아님'
        lines.extend([label + ' · ' + cite(item.get('pages')), _body(item['text'])])
    lines.extend(['## 핵심 개념과 직관', '정의는 인용 원문과 대조하고, 직관·예시는 이해를 돕는 AI 보충 설명으로 읽으세요.'])
    for item in package.get('concepts', []):
        lines.extend(['### ' + safe_text(item['name']), cite(item['pages']),
                      '정의: ' + _body(item['definition']), '직관 (보충): ' + _body(item['intuition']),
                      '예시 (보충): ' + _body(item['example'])])
    lines.append('## 수식·변수·조건·풀이')
    if not package.get('formulas'):
        lines.append('제공된 학습 패키지에 수식이 없습니다. 수식을 임의로 추가하지 않았습니다.')
    else:
        lines.append('수식은 표시용입니다. TeX 파싱이나 수학적 정확성을 검증한 결과가 아닙니다. 풀이·예제는 AI 보충 설명입니다.')
    for index, item in enumerate(package.get('formulas', []), 1):
        lines.extend([f'### 수식 {index}', cite(item['pages']), _formula(item['expression']),
                      '변수: ' + _body(item['symbols']), '가정·적용 조건: ' + _body(item['conditions'])])
        for number, step in enumerate(item['steps'], 1):
            lines.extend([f'풀이 단계 {number}', _body(step)])
        lines.extend(['적용 예제 (보충)', _body(item['example'])])
    lines.append('## 흔한 오개념')
    for item in package.get('misconceptions', []):
        lines.extend(['오개념: ' + _body(item['misconception']), '바로잡기: ' + _body(item['correction']), cite(item['pages'])])
    for kind, heading in [('quick', '10분 빠른 학습 가이드'), ('deep', '심화 학습 가이드')]:
        lines.extend(['## ' + heading, 'AI 제안 학습 순서 · 실제 소요 시간은 학습자에 따라 달라집니다.'])
        for number, item in enumerate(package.get('guides', {}).get(kind, []), 1):
            lines.extend([f'### 단계 {number} · ' + safe_text(item['minutes']) + '분', _body(item['step']), cite(item['pages'])])
    lines.extend(['## 회상 퀴즈 · AI 연습문제', '시험 예측이나 교수 강조점이 아닙니다. 먼저 직접 답한 뒤 정답과 해설을 펼치세요.'])
    for index, item in enumerate(package.get('quizzes', []), 1):
        lines.extend([f'### 퀴즈 {index}', '유형: ' + safe_text(item['kind']) + ' · 개념: ' + safe_text(item['concept']),
                      _body(item['prompt']), cite(item['pages']), _toggle(item['answer'], item['explanation'], cite(item['pages']))])
    lines.append('## 과목별 응용 · AI 연습문제')
    for index, item in enumerate(package.get('practices', []), 1):
        lines.extend([f'### 응용 {index}', _body(item['task']), cite(item['pages']), _toggle(item['solution'], references=cite(item['pages']))])
    lines.append('## 선수 개념과 연결 자료')
    related_by_key = {item['key']: item for item in related}
    for item in package.get('prerequisites', []):
        lines.extend(['### ' + safe_text(item['concept']), _body(item['why']), cite(item['pages'])])
        for key in item['related_keys']:
            target = related_by_key.get(key)
            lines.append(safe_link(target['title'], target.get('artifact_url')) if target else '관련 자료 연결 확인 필요: ' + safe_text(key))
    lines.append('## 이전 버전과 달라진 점')
    changes = package.get('changes', {})
    lines.extend('- ' + safe_text(text) for text in changes.get('summary', []))
    for field, label in [('added_pages', '추가 (현재 버전)'), ('changed_pages', '변경 (현재 버전)')]:
        pages = changes.get(field, [])
        if pages:
            lines.append(label + ': ' + cite(pages))
    if changes.get('removed_pages'):
        lines.append('삭제 (이전 버전 쪽 번호, 현재 원본 링크 아님): ' + safe_text(_pages(changes['removed_pages'])))
    return '\n\n'.join(lines)


def render_exam(exam, source_links: dict) -> str:
    lines = ['## ' + safe_text(exam['title']), 'AI 연습문제 모음입니다. 실제 시험 예측이나 교수 강조점이 아닙니다.',
             '## 요청 범위']
    scope = exam.get('scope', {})
    for key, label in [('title', '요청 제목'), ('course_ids', '과목 ID'), ('source_keys', '자료 키')]:
        if key in scope:
            value = scope[key]
            lines.append(label + ': ' + safe_text(', '.join(map(str, value)) if isinstance(value, list) else value))
    lines.extend(_review_state(exam))
    lines.extend(['## 출처', _references(exam.get('sources'), source_links),
                  '## 개념 비교 · AI 종합 설명', '<table header-row="true">',
                  '\t<tr><td>비교 주제</td><td>설명</td><td>원문 근거</td></tr>'])
    for item in exam.get('comparisons', []):
        cells = [safe_text(item['topic']), safe_text(item['explanation']), _references(item['sources'], source_links)]
        lines.append('\t<tr>' + ''.join('<td>' + cell + '</td>' for cell in cells) + '</tr>')
    lines.extend(['</table>', '## 취약 개념과 복습 근거'])
    if not exam.get('weak_topics'):
        lines.append('제공된 복습 기록에 따른 취약 개념 항목이 없습니다. 숙달 여부를 자동 판정한 것은 아닙니다.')
    for item in exam.get('weak_topics', []):
        lines.extend(['### ' + safe_text(item['topic']), _body(item['reason']),
                      '연결 복습 키: ' + safe_text(', '.join(item['review_keys'])), _references(item['sources'], source_links)])
    lines.extend(['## 문제 · AI 연습문제', '답을 먼저 작성하고 각 문제의 정답과 해설을 펼치세요.'])
    answers = {item['question_id']: item for item in exam.get('answers', [])}
    for index, question in enumerate(exam.get('questions', []), 1):
        lines.extend([f'### 문제 {index}', '유형: ' + safe_text(question['kind']) + ' · 주제: ' + safe_text(question['topic']),
                      _body(question['prompt']), _references(question['sources'], source_links)])
        answer = answers.get(question['id'])
        if answer:
            lines.append(_toggle(answer['answer'], answer['explanation'], _references(answer['sources'], source_links)))
        else:
            lines.append('연결 정답 미제공 · 검토 필요')
    return '\n\n'.join(lines)


def render_briefing(report) -> str:
    failures = report.get('failures', [])
    partial = (report.get('status') == 'partial' or bool(failures)
               or any(row.get('status') in {'partial', 'failed', 'review_required'} for row in report.get('materials', []))
               or any(row.get('status') == 'partial' for row in report.get('courses', [])))
    lines = ['## 일일 학습 브리핑 · ' + safe_text(report.get('date', '')),
             '실행 상태: ' + ('일부 미완료 · 아래 실패·검토 항목 확인' if partial else _STATUSES.get(report.get('status'), '실행 상태 미제공')),
             '## 처리 집계']
    labels = {'materials': '처리 대상 자료', 'baseline': '기준선 원본 보관 (AI 학습자료 미생성)',
              'published': '신규·개정 학습자료 게시', 'skipped': '건너뜀', 'review_required': '검토 필요',
              'failures': '실패 항목', 'due_reviews': '복습 기한 도래'}
    for key, label in labels.items():
        if key in report.get('summary', {}):
            lines.append(label + ': ' + safe_text(report['summary'][key]))
    lines.append('기준선 보관은 AI 분석 완료가 아닙니다. 검토 필요 수는 다른 상태와 겹칠 수 있습니다.')
    lines.append('## 자료별 결과')
    for item in report.get('materials', []):
        status = _STATUSES.get(item.get('status'), '상태 확인 필요')
        if item.get('review_required') and item.get('status') != 'review_required':
            status += ' · 내용 검토 필요'
        title = safe_link(item.get('title', item.get('key', '자료')), item['artifact_url']) if item.get('artifact_url') else safe_text(item.get('title', item.get('key', '자료')))
        lines.extend(['### ' + title, '과목 ID: ' + safe_text(item.get('course_id')) + ' · ' + status])
        if item.get('reason'):
            lines.append('사유: ' + safe_text(item['reason']))
    lines.append('## 과목별 처리 범위')
    for item in report.get('courses', []):
        lines.append('- 과목 ' + safe_text(item['course_id']) + ': ' + _STATUSES.get(item.get('status'), '상태 확인 필요'))
    lines.append('## 실패·재확인 항목')
    for failure in failures:
        fields = [(key, failure[key]) for key in ('course_id', 'key', 'partition', 'stage', 'error_type') if key in failure]
        lines.append('- ' + ' · '.join(safe_text(key) + ': ' + safe_text(value) for key, value in fields))
        if failure.get('error_type') == 'onedrive_name_conflict':
            lines.append('  같은 이름의 OneDrive 파일과 내용이 달라 전송을 중단했습니다. 기존 파일은 그대로 보존했으며, 덮어쓰기·자동 이름 변경·다른 폴더 저장을 하지 않았습니다. 저장 대상을 확인하세요.')
    if not failures:
        lines.append('이번 보고에 기록된 실패 항목이 없습니다.')
    lines.append('## 기한 도래 복습과 취약 개념')
    for review in report.get('due', []):
        rating = _RATINGS.get(review.get('rating'), '평가 전')
        lines.extend(['### ' + safe_text(review.get('concept', '복습')) + ' · ' + rating,
                      _body(review.get('prompt', '')), '복습 키: ' + safe_text(review.get('key', '')),
                      '기한: ' + safe_text(review.get('due_at', ''))])
        if review.get('artifact_url'):
            lines.append(safe_link('복습 카드', review['artifact_url']))
    if not report.get('due'):
        lines.append('이번 보고에 포함된 기한 도래 복습이 없습니다. 모든 개념을 숙달했다는 뜻은 아닙니다.')
    notice = report.get('notification', {})
    accepted = notice.get('accepted')
    lines.extend(['## 알림 요청', '운영체제 요청 ' + ('수락' if accepted is True else '미수락' if accepted is False else '결과 미확인') + ' · 화면 표시 여부는 확인하지 않았습니다.'])
    return '\n\n'.join(lines)


def render_review(review, source_url) -> str:
    lines = ['## 복습 질문 · AI 연습문제', _body(review['prompt']),
             '개념: ' + safe_text(review.get('concept', '')),
             _citation(review.get('pages'), '학습 자료', source_url),
             '시험 예측 문제가 아닙니다. 먼저 스스로 답하고, 필요한 경우 아래 예시 정답을 펼쳐 비교하세요.',
             _toggle(review['answer'], review.get('explanation', ''), _citation(review.get('pages'), '학습 자료', source_url)),
             '## 내 복습 기록 제출',
             '카드 속성 평가를 이해함 / 애매함 / 모름 중 하나로 선택하고 내답에 직접 답을 적은 뒤 제출을 체크하세요. 다음 일일 실행에서 반영합니다. AI가 학습자 대신 답하거나 자동 채점하지 않습니다.',
             'CLI에서도 ssu-study review의 --key, --rating, --answer 옵션으로 직접 기록할 수 있습니다. 평가는 understood / uncertain / unknown 중 선택하세요.',
             '복습 키: ' + safe_text(review['key'])]
    return '\n\n'.join(lines)
