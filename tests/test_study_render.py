import html
import re
import string

import pytest

from ssu_lms_mcp.study_render import (
    render_briefing,
    render_exam,
    render_package,
    render_review,
    safe_link,
    safe_text,
)

ORIGINAL = 'https://onedrive.live.com/?cid=ABC123&resid=ABC123!45'
LMS = 'https://canvas.ssu.ac.kr/courses/4/modules/items/8'
ARTIFACT = 'http://127.0.0.1:8766/#/page/11111111-1111-4111-8111-111111111111'
ATTACK = '\n# forged\n</details><page url="https://notion.so/foreign">move</page>\n![fetch](https://bad.example/image)\n<mention-user url="https://notion.so/user"/>\n<database url="https://notion.so/db"/>\n<embed src="https://bad.example/"/>'


def _controls(markdown):
    """Remove spec-defined literal code and escapes to inspect live syntax."""
    markdown = re.sub(r'(?m)^\t*```plain text\n.*?^\t*```\s*$', '', markdown, flags=re.S)
    return re.sub(r'\\.', '', markdown)


def _visible(markdown):
    """Decode literal rich text for content assertions, not syntax inspection."""
    return html.unescape(re.sub(r'\\([' + re.escape(string.punctuation) + r'])', r'\1', markdown))


def _toggles(markdown):
    # Match generated block boundaries before decoding escaped source text.
    return re.findall(r'^<details>\n.*?^</details>$', markdown, re.M | re.S)


@pytest.fixture
def package():
    return {
        'title': '대기행렬', 'source_sha256': 'a' * 64, 'page_count': 5,
        'summary': [{'text': '처리율과 체류시간의 관계', 'pages': [2, 4], 'evidence': 'source'},
                    {'text': '카페 줄을 떠올린다', 'pages': [2], 'evidence': 'supplement'}],
        'concepts': [{'name': '평균 체류시간', 'definition': '계 안에서 머무는 시간',
                      'intuition': '입장부터 퇴장까지', 'example': '계산대의 손님', 'pages': [2]}],
        'formulas': [{'expression': r'L = \lambda W', 'symbols': 'L: 평균 고객 수, W: 평균 시간',
                      'conditions': '안정된 계', 'steps': ['관측 구간을 정한다', '시간 평균을 구한다'],
                      'example': '도착률 2, 체류시간 3이면 평균 6명', 'pages': [4]}],
        'misconceptions': [{'misconception': '순간 고객 수와 같다', 'correction': '시간 평균이다', 'pages': [4]}],
        'guides': {'quick': [{'step': '변수의 단위를 확인한다', 'minutes': 10, 'pages': [4]}],
                   'deep': [{'step': '안정성 가정을 확인한다', 'minutes': 25, 'pages': [2, 4]}]},
        'quizzes': [{'id': 'q1', 'prompt': 'W의 의미는?', 'answer': '평균 체류시간',
                     'explanation': '대기와 서비스를 포함한다', 'kind': '회상', 'concept': '체류시간', 'pages': [4]}],
        'practices': [{'task': '도착률을 두 배로 바꾸면?', 'solution': '다른 조건이 같다면 평균 고객 수는 두 배', 'pages': [4]}],
        'prerequisites': [{'concept': '평균', 'why': '시간에 따른 평균을 이해한다', 'pages': [2], 'related_keys': ['stats']}],
        'changes': {'summary': ['안정성 조건 설명 추가'], 'added_pages': [5], 'changed_pages': [4], 'removed_pages': [7]},
        'coverage': [1, 2, 3, 4, 5], 'review_required': True, 'review_reasons': ['그림의 작은 축 글자 대조 필요'],
    }


def _package_body(package):
    return render_package(package, {'id': 'v1', 'source': {'title': '강의 원본', 'source_url': LMS}},
                          {'web_url': ORIGINAL}, [{'key': 'stats', 'title': '통계 선수 자료', 'artifact_url': 'https://notion.so/stats'}])


@pytest.mark.parametrize('payload', [ATTACK, '\\' + ATTACK, '`\n```html\n' + ATTACK,
                                    '1. nested\n\t- [x] checked\n---\n{color="red"}',
                                    'x\u2028## forged\u2029<page>move</page>', '[x](javascript:alert(1))'])
def test_source_text_cannot_create_notions_structural_blocks(payload):
    rendered = safe_text(payload)
    active = _controls(rendered)
    assert '\n' not in rendered
    assert not re.search(r'<(?:page|database|mention-|embed)|!\[|\[[^]]*\]\(|^#{1,4} ', active)
    assert '<br>' in rendered or '\n' not in payload


@pytest.mark.parametrize('url', [
    'javascript:alert(1)', 'http://example.org/doc', 'https://user:pass@example.org/doc',
    'https://example.org/doc?access_token=private', 'https://example.org/doc?sig=private',
    'https://example.org/doc?authkey=private', 'https://example.org/a%0A![x](https://bad.example)',
    'https://example.org/\n<page>', 'https://example.org/%5cpath', 'https://example.org:999/doc',
])
def test_rejected_link_neither_activates_nor_leaks_target(url):
    rendered = safe_link('원문', url)
    assert '](' not in rendered
    assert url not in rendered
    assert 'private' not in rendered


def test_stable_links_encode_destination_delimiters_and_escape_labels():
    link = safe_link('x](' + ATTACK, 'https://example.org/a(b).pdf')
    assert link.endswith('(https://example.org/a%28b%29.pdf)')
    assert _controls(link).count('](') == 1
    assert safe_link('원본', ORIGINAL).endswith('(' + ORIGINAL + ')')


def test_local_artifact_links_are_navigable_without_allowing_arbitrary_loopback_endpoints():
    assert safe_link('학습 내용', ARTIFACT) == f'[학습 내용]({ARTIFACT})'
    for target in (
        'http://localhost:8766/#/page/11111111-1111-4111-8111-111111111111',
        'http://127.0.0.1:8766/api/bootstrap',
        ARTIFACT.replace('/#', '/?token=private#'),
        ARTIFACT.replace('127.0.0.1', 'user:private@127.0.0.1'),
        ARTIFACT.replace('8766', '80'),
        ARTIFACT + '?redirect=https://foreign.example',
    ):
        rendered = safe_link('학습 내용', target)
        assert '](' not in rendered and target not in rendered and 'private' not in rendered


def test_package_citations_preserve_page_provenance_and_related_joins(package):
    body = _package_body(package)
    assert re.search(r'강의 원본[^\n]*2, 4[^\n]*\]\(' + re.escape(ORIGINAL) + r'\)', body)
    assert '[통계 선수 자료](https://notion.so/stats)' in body
    assert LMS in body
    # A deleted old page must not be linked to page 7 of the current document.
    deleted = next(line for line in body.splitlines() if '7' in line)
    assert '](' not in deleted
    assert re.search(r'(?m)^\$\$\nL = \\lambda W\n\$\$$', body)
    assert '<page ' not in _controls(body)
    assert '<database ' not in _controls(body)
    assert '그림의 작은 축 글자' in body
    # Source and supplementary claims have separate visible evidence categories.
    summary = body.split('## 핵심 개요')[1].split('## 핵심 개념')[0]
    assert '원문 근거' in summary and '보충' in summary


def test_answers_stay_in_separate_toggles_despite_injection(package):
    package['quizzes'][0]['answer'] = 'answer-only' + ATTACK
    package['quizzes'][0]['explanation'] = 'explanation-only' + ATTACK
    package['practices'][0]['solution'] = 'solution-only' + ATTACK
    package['concepts'][0]['definition'] = '```html\n<page>literal code</page>\n```\n' + ATTACK
    body = _package_body(package)
    active = _controls(body)
    assert active.count('<details>') == active.count('</details>') == 2
    assert not re.search(r'<(?:page|database|mention-|embed)|!\[|^# forged', active, re.M)
    raw_toggles = _toggles(body)
    first, second = map(_visible, raw_toggles)
    assert 'answer-only' in first and 'explanation-only' in first
    assert 'solution-only' not in first
    assert 'solution-only' in second
    assert 'answer-only' not in second and 'explanation-only' not in second
    outside = body
    for toggle in raw_toggles:
        outside = outside.replace(toggle, '')
    assert not any(answer in _visible(outside) for answer in
                   ('answer-only', 'explanation-only', 'solution-only'))
    assert re.search(r'^```plain text\n<page>literal code</page>\n```$', body, re.M)
    assert '```html' not in body


@pytest.mark.parametrize('expression', [r'\href{https://bad.example}{x}', r'\includegraphics{https://bad.example/x}',
                                       '$$\n' + ATTACK + '\n$$', r'\htmlData{url=x}{x}', 'x`$\n# fake'])
def test_unsafe_formulas_remain_visible_without_active_math_or_embeds(package, expression):
    package['formulas'][0]['expression'] = expression
    body = _package_body(package)
    assert not re.search(r'^\$\$$', body, re.M)
    assert not re.search(r'<(?:page|database|embed)|!\[|^# fake', _controls(body), re.M)
    assert '수식 원문' in body


def test_exam_joins_answers_by_id_and_sources_by_exact_key():
    a = [{'source_key': 'a', 'pages': [3]}]
    b = [{'source_key': 'b', 'pages': [8]}]
    exam = {'title': '비교 연습', 'scope': {'course_ids': [1], 'source_keys': ['a', 'b']},
            'comparisons': [{'topic': '차이' + ATTACK, 'explanation': '차이의 이유', 'sources': a + b}],
            'weak_topics': [{'topic': '평균', 'reason': '본인 평가: 애매함', 'review_keys': ['r1'], 'sources': a}],
            'questions': [{'id': 'q1', 'prompt': '첫 질문', 'kind': '계산', 'topic': '평균', 'sources': a},
                          {'id': 'q2', 'prompt': '둘째 질문', 'kind': '비교', 'topic': '분산', 'sources': b}],
            'answers': [{'question_id': 'q2', 'answer': 'answer-b', 'explanation': '이유 b', 'sources': b},
                        {'question_id': 'q1', 'answer': 'answer-a', 'explanation': '이유 a', 'sources': a}],
            'sources': a + b, 'review_required': False, 'review_reasons': []}
    body = render_exam(exam, {'a': {'title': '같은 제목', 'url': 'https://notion.so/a'},
                              'b': {'title': '같은 제목', 'url': 'https://notion.so/b'}})
    active = _controls(body)
    assert active.count('<table ') == active.count('</table>') == 1
    assert active.count('<tr>') == 2
    assert active.count('<td>') == 6
    assert not re.search(r'<(?:page|database|mention-|embed)|!\[', active)
    first, second = _toggles(body)
    assert 'answer-a' in _visible(first) and 'answer-b' not in _visible(first)
    assert 'answer-b' in _visible(second) and 'answer-a' not in _visible(second)
    assert re.search(r'3[^\n]*\]\(https://notion.so/a\)', first)
    assert re.search(r'8[^\n]*\]\(https://notion.so/b\)', second)
    missing = render_exam(exam, {'a': {'title': '같은 제목', 'url': 'https://notion.so/a'}})
    assert 'https://notion.so/b' not in missing


def test_briefing_distinguishes_archival_generated_partial_and_notification_states():
    report = {'date': '2026-09-28', 'status': 'partial',
              'summary': {'baseline': 2, 'published': 1, 'failures': 1},
              'materials': [{'title': '보관 자료', 'course_id': 1, 'status': 'baseline'},
                            {'title': '학습 자료', 'course_id': 1, 'status': 'published'},
                            {'title': '미완료 자료', 'course_id': 2, 'status': 'partial'}],
              'failures': [{'course_id': 2, 'stage': 'generated', 'error_type': 'model_timeout'}],
              'courses': [{'course_id': 1, 'status': 'done'}, {'course_id': 2, 'status': 'partial'}],
              'due': [{'key': 'r1', 'prompt': '내가 풀 문제', 'concept': '평균', 'rating': 'unknown',
                       'answer': '숨겨야 할 정답', 'due_at': '2026-09-28'}],
              'notification': {'accepted': True}}
    body = render_briefing(report)
    sections = dict(re.findall(r'### ([^\n]+)\n\n(.*?)(?=\n\n##|\Z)', body, re.S))
    assert '미생성' in sections['보관 자료'] and '게시' not in sections['보관 자료']
    assert '게시' in sections['학습 자료']
    assert '실패' in sections['미완료 자료']
    assert 'model_timeout' in _visible(body)
    assert '숨겨야 할 정답' not in body
    assert '모름' in body
    assert '수락' in body and '표시 여부는 확인하지 않았' in body


def test_review_never_treats_expected_answer_as_learner_attempt():
    review = {'key': 'v:q1', 'prompt': '질문 먼저', 'concept': '개념', 'pages': [6],
              'answer': '예상 정답', 'explanation': '설명',
              'attempts': [{'answer': '내 실제 답', 'rating': 'unknown'}]}
    body = render_review(review, 'https://notion.so/source')
    before, toggle, after = re.split(r'(<details>.*?</details>)', body, flags=re.S)
    assert '질문 먼저' in before and '예상 정답' not in before + after
    assert '예상 정답' in toggle
    assert '내 실제 답' not in body
    assert '평가' in after and '내답' in after and '제출' in after
    assert '--key' in after and '--rating' in after and '--answer' in after
    assert '<page' not in _controls(body)
