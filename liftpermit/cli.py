import argparse
import json
from pathlib import Path
from . import __file__ as module_file
from .store import connect,import_file,report,audit

IMPORTS={'plan-import': 'plan', 'asset-import': 'asset', 'work-import': 'work',
         'ticket-import': 'ticket'}
def main(argv=None):
    parser=argparse.ArgumentParser(prog='liftpermit')
    parser.add_argument('command',choices=list(IMPORTS)+['demo','report','audit'])
    parser.add_argument('--db',default='state.sqlite')
    parser.add_argument('--file')
    parser.add_argument('--archive',help='离线票图内容寻址归档目录（ticket-import 必需）')
    args=parser.parse_args(argv)
    db=connect(args.db,archive_dir=args.archive)
    if args.command in IMPORTS:
        if not args.file: parser.error('--file required')
        if args.command=='ticket-import' and not args.archive:
            parser.error('ticket-import requires --archive')
        result=import_file(db,IMPORTS[args.command],args.file)
    elif args.command=='demo':
        root=Path(__file__).resolve().parents[1]/'fixtures'
        result=[]
        for command,file in DEMO:
            imported=import_file(db,IMPORTS[command],root/file)
            result.append({'fixture':file,**imported,
                           'work_state': next((r['state'] for r in report(db)['entities']
                                               if r['subsystem']=='现场许可与作业账'),None)})
        result={'imports':result,'report':report(db),'audit':audit(db),
                'imported_module':str(Path(module_file).resolve())}
    elif args.command=='report': result=report(db)
    else: result=audit(db)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if args.command!='audit' or result['ok'] else 1

DEMO=[('plan-import', 'plan-ok.json'), ('asset-import', 'asset-ok.json'), ('work-import', 'work-ok.json'),
      ('plan-import', 'plan-bad.json'), ('asset-import', 'asset-bad.json'), ('work-import', 'work-bad.json'),
      ('plan-import', 'plan-recover.json'), ('asset-import', 'asset-recover.json'), ('work-import', 'work-recover.json')]
