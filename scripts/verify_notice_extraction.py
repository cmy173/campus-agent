"""Opt-in public verification of notice extraction against one real college notice.

Credential-free public pages only, into a temporary database with a synthetic local owner.
Never reads the user's real library. Attachments are listed but never downloaded or parsed,
so any field that only exists in an attachment is reported as unread, not guessed.

Usage:
    python scripts/verify_notice_extraction.py --keyword 游泳 --output docs/notice-extraction.json
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campus_agent import notice_analysis, notice_contract, notice_model
from campus_agent.campus import CampusManager
from campus_agent.campus_college_sources import CKC
from campus_agent.connector_contracts import CollegeNoticesQuery, UserContext
from campus_agent.core import TZ
from campus_agent.parser_worker import parse
from campus_agent.services import extract_tasks
from campus_agent.storage import Store

# Fields a human must confirm against the notice text. Keywords and date/time shapes only
# select candidate lines for review; they never produce an answer.
CHECKLIST = {
    'registration_deadline': ('报名/提交截止', ('报名', '截止', '截至', '逾期')),
    'activity_time': ('活动或测试起止时间', ('测试时间', '活动时间', '时间', '开始', '结束', '-')),
    'assembly': ('集合/检录时间', ('集合', '检录', '签到', '热身')),
    'location': ('地点', ('地点', '校区', '馆', '场地', '池')),
    'audience': ('适用对象', ('对象', '参加', '年级', '新生', '学生')),
    'attachment': ('附件要求', ('附件', '见表', '详见')),
}
# A label often sits in its own paragraph while the value sits in the next one.
DATE_SHAPE = re.compile(r'\d{4}\s*年|\d{1,2}\s*月\s*\d{1,2}\s*日|\d{1,2}:\d{2}|\d{1,2}：\d{2}')


def lines(body):
    return [line for line in body.splitlines() if line.strip()]


def candidate_lines(body, keywords, limit=10):
    found, seen = [], set()
    for number, line in enumerate(lines(body), 1):
        if any(keyword in line for keyword in keywords) or DATE_SHAPE.search(line):
            if number not in seen:
                seen.add(number)
                found.append({'line': number, 'text': line,
                              'matched': 'keyword+date' if DATE_SHAPE.search(line) and
                              any(keyword in line for keyword in keywords)
                              else 'date-shape' if DATE_SHAPE.search(line) else 'keyword'})
    return found[:limit]


def old_rule_results(chunks):
    """The behaviour before this change: per-chunk sentence extraction."""
    results = []
    for chunk in chunks:
        for task in extract_tasks(chunk['text']):
            results.append({'locator': chunk['locator'], 'title': task['title'][:120],
                            'due': task['due'], 'raw_date': task['raw_date']})
    return results


def main():
    parser = argparse.ArgumentParser(description='Verify notice extraction on a real public notice')
    parser.add_argument('--keyword', default='游泳', help='Title or body keyword selecting the notice')
    parser.add_argument('--limit', type=int, default=5)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--use-model', action='store_true',
                        help='Also run one real provider call when DEEPSEEK_API_KEY is configured')
    args = parser.parse_args()

    with TemporaryDirectory(prefix='notice-extraction-verification-') as directory:
        store = Store(Path(directory) / 'campus.sqlite3')
        with store.connect() as db:
            db.execute('INSERT INTO users VALUES(?,?,?)', ('verification', 'synthetic-local-user', 'unused'))
        manager = CampusManager(store)
        owner = 'verification'
        context = UserContext(owner)
        manager.repository.write(owner, {'profile': {'data': {'college': CKC.college},
            'fetched_at': datetime.now(TZ).isoformat(), 'stale': False}})
        result = manager.service.sync('college_notices', CollegeNoticesQuery(limit=args.limit), context)
        records = result.records
        chosen = next((row for row in records if args.keyword in row['title'] or args.keyword in row['body']), None)
        report = {
            'verified_at': datetime.now(TZ).isoformat(),
            'verification_kind': 'real credential-free public pages; synthetic profile and local owner',
            'network_policy': os.environ.get('CAMPUS_PUBLIC_NETWORK_POLICY', 'system'),
            'listing': CKC.listing,
            'synced_records': [{'id': row['id'], 'title': row['title'], 'source': row['source'],
                                'published_at': row['published_at'],
                                'body_characters': len(row['body']),
                                'attachments': [a['name'] for a in row['attachments']]} for row in records],
            'sync_error': result.error.model_dump(mode='json') if result.error else None,
            'matched_keyword': args.keyword,
        }
        if chosen is None:
            report['status'] = 'notice_not_in_current_public_window'
            report['note'] = ('当前公开列表首页没有匹配该关键词的通知；连接器只读取首页有限窗口，'
                              '不提供历史分页。请换一个仍在首页的通知，或直接核对官方原文。')
            manager.close()
            emit(report, args.output)
            return 1

        # Rebuild the document exactly as the import route does: title line plus the official body.
        content = (chosen['title'] + '\n\n' + chosen['body']).encode('utf-8')
        parsed = parse(content, '.txt')
        chunks = [{'id': f'chunk-{position}', 'ref': notice_contract.paragraph_ref(position),
                   'locator': chunk['locator'], 'text': chunk['text']}
                  for position, chunk in enumerate(parsed['chunks'])]
        attachments = notice_analysis.attachment_list(chosen['attachments'])
        body_sha = chosen['body_sha256']
        rules_payload, rules_tasks = notice_analysis.rules_result(chunks, attachments, body_sha)

        report |= {
            'status': 'ok',
            'notice': {'id': chosen['id'], 'title': chosen['title'], 'source': chosen['source'],
                       'published_at': chosen['published_at'], 'college': chosen['college'],
                       'body_sha256': body_sha, 'body_characters': len(chosen['body']),
                       'paragraph_count': len(chunks),
                       'attachments': [{'ref': a['ref'], 'name': a['name'], 'url': a['url'],
                                        'parsed': False} for a in attachments],
                       'has_unread_content': chosen['has_unread_content'],
                       'body': lines(chosen['body'])},
            'model_input_budget': {'max_chars': notice_contract.MAX_INPUT_CHARS,
                                   'prompt_version': notice_contract.PROMPT_VERSION,
                                   'paragraph_refs': [c['ref'] for c in chunks],
                                   'paragraph_locators': [c['locator'] for c in chunks],
                                   'coverage': notice_contract.build_input(
                                       chosen['title'], {'record_id': chosen['id'],
                                       'college': chosen['college'], 'published_at': chosen['published_at'],
                                       'has_unread_content': chosen['has_unread_content'],
                                       'attachments': attachments}, chunks)['coverage']},
            'old_rule_extraction': old_rule_results(chunks),
            'new_rule_extraction': {'coverage_note': rules_payload['coverage_note'],
                                    'open_questions': rules_payload['open_questions'],
                                    'candidates': [{'title': t['title'][:120], 'due': t['due'],
                                                    'raw_date': t['raw_date'],
                                                    'evidence': t['evidence'][0]['quote'][:120]}
                                                   for t in rules_tasks]},
            'manual_checklist': [
                {'field': field, 'label': label,
                 'candidate_source_lines': candidate_lines(chosen['body'], keywords),
                 'attachment_only_possible': bool(attachments) and field in
                     ('location', 'activity_time', 'assembly', 'audience'),
                 'human_verdict': None, 'human_note': ''}
                for field, (label, keywords) in CHECKLIST.items()],
        }

        configured = notice_model.configuration()
        report['model'] = {'provider': configured['provider'], 'provider_label': configured['provider_label'],
                           'model': configured['model'], 'endpoint': configured['endpoint'],
                           'prompt_version': configured['prompt_version'],
                           'configured': configured['configured'], 'sends': configured['sends']}
        if args.use_model and configured['configured']:
            model_input = notice_contract.build_input(chosen['title'], {
                'record_id': chosen['id'], 'college': chosen['college'],
                'published_at': chosen['published_at'], 'has_unread_content': chosen['has_unread_content'],
                'attachments': attachments}, chunks)
            sent = json.dumps(model_input, ensure_ascii=False)
            started = time.monotonic()
            try:
                raw = notice_model.extractor().extract(model_input)
                payload, corrected = notice_contract.validate(
                    raw['content'], {c['ref']: c['text'] for c in chunks},
                    {a['ref'] for a in attachments})
                report['model_call'] = {
                    'verified': True, 'latency_ms': round((time.monotonic() - started) * 1000),
                    'usage': raw['usage'], 'finish_reason': raw['finish_reason'],
                    'input_characters': len(sent),
                    'input_contains_attachment_url': any(a['url'] in sent for a in attachments),
                    'input_contains_origin': chosen['source'] in sent,
                    'evidence_corrected': corrected,
                    'events': payload['events'], 'open_questions': payload['open_questions'],
                    'coverage': payload['coverage'],
                    'candidates': [{'title': t['title'], 'due': t['due'], 'raw_date': t['raw_date'],
                                    'time_kind': t['time_kind'], 'time_precision': t['time_precision'],
                                    'location': t['location'], 'needs_attachment': t['needs_attachment'],
                                    'derived_note': t['derived_note'],
                                    'evidence': [e['quote'][:120] for e in t['evidence']]}
                                   for t in notice_analysis.tasks_from_payload(
                                       payload, {c['ref']: c for c in chunks}, attachments,
                                       notice_analysis.analysis_key(body_sha, 'deepseek',
                                                                    notice_contract.PROMPT_VERSION,
                                                                    configured['model']))],
                    'note': '模型输出不是标准答案；每项仍需按 manual_checklist 对照原文人工核对。'}
            except Exception as exc:  # reported by code only, never with the key or body
                report['model_call'] = {'verified': False,
                                        'error': getattr(exc, 'code', type(exc).__name__),
                                        'message': getattr(exc, 'message', '请求失败'),
                                        'latency_ms': round((time.monotonic() - started) * 1000)}
        else:
            report['model_call'] = {'verified': False, 'error': 'NOT_RUN',
                'message': ('未配置 DEEPSEEK_API_KEY，或未使用 --use-model；真实模型调用未验证。'
                            '模拟模型响应已在自动化测试中覆盖。')}
        report['not_verified'] = [
            'attachment and image contents (never downloaded or parsed)',
            'live campus login or profile refresh',
            'other colleges', 'historical pagination beyond the first public page',
            'deadline accuracy on a labelled real corpus',
            'other network environments',
        ] + ([] if report['model_call'].get('verified') else ['a real provider call'])
        manager.close()
    emit(report, args.output)
    return 0 if report['status'] == 'ok' else 1


def emit(report, output):
    text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('verified_at', 'status', 'matched_keyword')
                      if key in report}, ensure_ascii=True))
    print('report keys: ' + ', '.join(report))


if __name__ == '__main__':
    raise SystemExit(main())
