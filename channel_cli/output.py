"""Human-readable terminal output; JSON remains an explicit integration format."""
import json
import shutil
import sys
import unicodedata

STATES = {"queued": "대기", "running": "실행 중", "done": "완료", "failed": "실패",
          "interrupted": "중단", "cancelled": "취소"}


def width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in text)


def plain(text):
    # Keep terminal control sequences in messages from changing the display.
    return ''.join(c if c in '\n\t' or not unicodedata.category(c).startswith('C') else '' for c in str(text))


def clipped(text, maximum):
    text = ' '.join(plain(text).split())
    if width(text) <= maximum:
        return text
    out = ''
    for c in text:
        if width(out + c) > maximum - 1:
            break
        out += c
    return out + '…'


def table(headers, rows):
    sizes = [max(width(str(row[i])) for row in [headers, *rows]) for i in range(len(headers))]
    def line(row):
        return '  '.join(str(cell) + ' ' * (sizes[i] - width(str(cell))) for i, cell in enumerate(row)).rstrip()
    return '\n'.join([line(headers), line(['─' * s for s in sizes]), *(line(row) for row in rows)])


def render(value, command, verbose=False):
    if isinstance(value, dict) and 'error' in value and 'id' not in value:
        return '오류: ' + plain(value['error'])
    if command == 'agents':
        rows = [[a['id'], '설치됨' if a['installed'] else '없음',
                 ', '.join('@' + alias for alias in a['aliases']) or '—'] for a in value]
        found = sum(a['installed'] for a in value)
        result = f'에이전트 {found}개 설치됨\n\n' + table(['에이전트', '설치', '별칭'], rows)
        if verbose:
            result += '\n\n' + '\n'.join(f"{a['id']}\n  {a['path'] or 'PATH에서 찾지 못함'}" for a in value)
        return result + '\n\n인증·사용 한도는 확인하지 않았습니다.'
    if command == 'jobs':
        if not value:
            return '표시할 작업이 없습니다.'
        max_text = max(12, shutil.get_terminal_size((100, 24)).columns - 48)
        rows = [[j['id'][:8], STATES[j['state']], '@' + j['agent'],
                 clipped(f"{j['channel']}/{j['room']}", 18), clipped(j['prompt'], max_text)] for j in value]
        return table(['작업', '상태', '에이전트', '방', '메시지'], rows)
    if command == 'events':
        if not value:
            return '새 이벤트가 없습니다.'
        return '\n\n'.join(f"#{e['seq']} · {STATES.get(e['type'], e['type'])}\n" + render(e['payload'], 'result') for e in value)
    job = value
    label = {'send': '접수', 'retry': '재접수', 'cancel': '취소'}.get(command, STATES[job['state']])
    head = f"{label} · @{job['agent']} · {plain(job['channel'])}/{plain(job['room'])} · {job['id'][:8]}"
    if command in ('send', 'retry', 'cancel'):
        return head + '\n  ' + clipped(job['prompt'], max(20, shutil.get_terminal_size((100, 24)).columns - 4))
    if job.get('answer'):
        return head + '\n\n' + plain(job['answer'])
    if job.get('error'):
        return head + '\n\n' + plain(job['error'])
    return head + '\n\n' + plain(job['prompt'])


def emit(value, command, json_mode=False, verbose=False):
    if json_mode:
        print(json.dumps(value, ensure_ascii=False), flush=True)
    else:
        error = isinstance(value, dict) and 'error' in value and 'id' not in value
        print(render(value, command, verbose), file=sys.stderr if error else sys.stdout, flush=True)
