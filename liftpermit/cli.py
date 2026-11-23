import argparse
import json
from pathlib import Path
from . import __file__ as module_file
from .store import connect, import_file, report, audit
from .portal_mock import MockPortal
from .filing import FilingClient, FilingError

IMPORTS = {'plan-import': 'plan', 'asset-import': 'asset', 'work-import': 'work'}
FILING_COMMANDS = [
    'filing-submit', 'filing-reconcile', 'filing-withdraw',
    'batch-submit', 'batch-reconcile', 'batch-withdraw-item',
    'filing-ledger', 'portal-inject',
]


def main(argv=None):
    parser = argparse.ArgumentParser(prog='liftpermit')
    parser.add_argument('command', choices=list(IMPORTS) + ['demo', 'report', 'audit']
                        + FILING_COMMANDS)
    parser.add_argument('--db', default='state.sqlite')
    parser.add_argument('--file')
    parser.add_argument('--portal', help='离线模拟门户状态文件（独立于本地 SQLite）')
    parser.add_argument('--job')
    parser.add_argument('--intent')
    parser.add_argument('--regulatory', help='监管必填字段 JSON 文件')
    parser.add_argument('--jobs', help='夜间批量清单 JSON 文件')
    parser.add_argument('--status')
    parser.add_argument('--route')
    parser.add_argument('--reason')
    parser.add_argument('--fault', action='append', help='门户故障脚本，可重复')
    args = parser.parse_args(argv)
    db = connect(args.db)
    if args.command in IMPORTS:
        if not args.file:
            parser.error('--file required')
        result = import_file(db, IMPORTS[args.command], args.file)
    elif args.command == 'demo':
        root = Path(__file__).resolve().parents[1] / 'fixtures'
        result = []
        for command, file in DEMO:
            imported = import_file(db, IMPORTS[command], root / file)
            result.append({'fixture': file, **imported,
                           'work_state': next((r['state'] for r in report(db)['entities']
                                               if r['subsystem'] == '现场许可与作业账'), None)})
        result = {'imports': result, 'report': report(db), 'audit': audit(db),
                  'imported_module': str(Path(module_file).resolve())}
    elif args.command == 'report':
        result = report(db)
    elif args.command == 'audit':
        result = audit(db)
    else:
        result = _filing_command(parser, args, db)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if args.command != 'audit' or result['ok'] else 1


def _load_json(path, what):
    if not path:
        raise SystemExit(f'{what} required')
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _portal(args):
    if not args.portal:
        raise SystemExit('--portal required（门户状态独立存储，不能与本地库混用）')
    return MockPortal(args.portal, faults=args.fault or [])


def _filing_command(parser, args, db):
    portal = _portal(args)
    client = FilingClient(db, portal)
    if args.command == 'filing-submit':
        if not args.job:
            parser.error('--job required')
        regulatory = _load_json(args.regulatory, '--regulatory')
        intent = client.submit_item(args.job, regulatory)
        return {'intent': _intent_view(intent), 'ledger': client.ledger(job_id=args.job)}
    if args.command == 'filing-reconcile':
        if not args.intent:
            parser.error('--intent required')
        intent = client.reconcile_intent(args.intent)
        return {'intent': _intent_view(intent)}
    if args.command == 'filing-withdraw':
        if not args.intent:
            parser.error('--intent required')
        intent = client.withdraw_intent(args.intent, reason=_range_reason(args))
        return {'intent': _intent_view(intent)}
    if args.command == 'batch-submit':
        jobs = _load_json(args.jobs, '--jobs')
        parent_id, children = client.submit_batch(jobs)
        return {'batch_intent': parent_id, 'child_intents': children,
                'ledger': client.ledger()}
    if args.command == 'batch-reconcile':
        if not args.intent:
            parser.error('--intent required（批次父意图 ID）')
        parent_id = client.reconcile_batch(args.intent)
        return {'batch_intent': parent_id,
                'ledger': client.ledger()}
    if args.command == 'batch-withdraw-item':
        if not args.intent:
            parser.error('--intent required（批次子意图 ID）')
        intent = client.withdraw_batch_item(args.intent, reason=args.range_reason(args))
        return {'intent': _intent_view(intent)}
    if args.command == 'filing-ledger':
        return {'ledger': client.ledger(status=args.status, job_id=args.job,
                                        route=args.route)}
    if args.command == 'portal-inject':
        portal.inject_faults(args.fault or [])
        return {'injected': args.fault or [], 'portal': str(Path(args.portal).resolve())}
    raise FilingError(f'unknown command {args.command}')


def _range_reason(args):
    return args.reason or '方案改版，旧签认不可沿用'


def _intent_view(intent):
    return {k: intent[k] for k in
            ('intent_id', 'job_id', 'route', 'status', 'snapshot_version',
             'payload_digest', 'batch_id', 'item_key', 'parent_intent_id')
            if k in intent}


DEMO = [('plan-import', 'plan-ok.json'), ('asset-import', 'asset-ok.json'),
        ('work-import', 'work-ok.json'),
        ('plan-import', 'plan-bad.json'), ('asset-import', 'asset-bad.json'),
        ('work-import', 'work-bad.json'),
        ('plan-import', 'plan-recover.json'), ('asset-import', 'asset-recover.json'),
        ('work-import', 'work-recover.json')]
