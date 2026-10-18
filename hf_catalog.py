"""Safe batched binary+audit+catalog commits against a pinned HF revision."""
import hashlib
import json
from pathlib import Path


def digest(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def parse_index(text):
    lines=text.lstrip('\ufeff').splitlines()
    if not lines or lines[0]!='BRIDX1':raise ValueError('Unexpected index format')
    symbols={};entries={}
    for line in lines[1:]:
        if not line.strip():continue
        parts=line.split('|')
        if parts[0]=='SYM' and len(parts)==5:
            if parts[1] in symbols:raise ValueError('Duplicate SYM')
            symbols[parts[1]]=line
        elif parts[0]=='FILE' and len(parts)==4:
            if parts[1] in entries or int(parts[3])<=0:raise ValueError('Duplicate/invalid FILE')
            entries[parts[1]]=line
        else:raise ValueError('Unknown index row; refusing rewrite')
    return symbols,entries


def merged_index(symbols,entries):
    lines=['BRIDX1'];placed=set()
    for symbol in sorted(symbols):
        lines.append(symbols[symbol])
        for path in sorted(entries):
            if path.split('/')[0]==symbol:
                lines.append(entries[path]);placed.add(path)
    lines.extend(entries[path] for path in sorted(entries) if path not in placed)
    return '\n'.join(lines)+'\n'


def publish(root,report,repo,digits,replace_existing=False,batch_size=30):
    from huggingface_hub import HfApi,CommitOperationAdd,hf_hub_download
    import os
    token=os.environ['HF_TOKEN'];api=HfApi(token=token)
    symbol=report['symbol'];results=[]
    for offset in range(0,len(report['days']),batch_size):
        batch=report['days'][offset:offset+batch_size]
        revision=api.repo_info(repo,repo_type='dataset').sha
        info=api.get_paths_info(repo,paths=['index.txt']+[d['path'] for d in batch],repo_type='dataset',revision=revision)
        remote={item.path:item for item in info}
        if 'index.txt' not in remote:raise ValueError('Target index.txt must already exist')
        path=hf_hub_download(repo,'index.txt',repo_type='dataset',revision=revision,token=token)
        symbols,entries=parse_index(Path(path).read_text('utf-8-sig'))
        operations=[];committed=[]
        for d in batch:
            file=root/d['path']
            if file.stat().st_size!=d['bytes'] or digest(file)!=d['sha256']:raise ValueError('Local BIN altered')
            if d['path'] in remote and not replace_existing:
                state='skipped_existing'
                if d['path'] not in entries:state+='; missing index entry needs manual review'
                results.append({'path':d['path'],'result':state})
                print(state,d['path'],flush=True);continue
            if d['path'] in entries and d['path'] not in remote and not replace_existing:
                raise ValueError('Index points to missing remote file; review explicit replacement')
            operations.append(CommitOperationAdd(path_in_repo=d['path'],path_or_fileobj=str(file)))
            audit=dict(d,source='ZIP annual BIN',source_member=report['source_member'],
                       source_completeness='user-confirmed; not independently provable from timestamps')
            operations.append(CommitOperationAdd(path_in_repo=f'audit/zip/{symbol}/{d["day"]}.json',path_or_fileobj=json.dumps(audit).encode()))
            entries[d['path']]=f"FILE|{d['path']}|{d['day']}|{d['bytes']}"
            committed.append(d)
        if operations:
            symbols.setdefault(symbol,f'SYM|{symbol}|{symbol}|0|{digits}')
            operations.append(CommitOperationAdd(path_in_repo='index.txt',path_or_fileobj=merged_index(symbols,entries).encode()))
            commit=api.create_commit(repo,repo_type='dataset',operations=operations,parent_commit=revision,
                                     commit_message=f'Import ZIP daily BIN ticks: {symbol}')
            results.extend({'path':d['path'],'result':'uploaded','commit':commit.oid} for d in committed)
            print('COMMITTED',len(committed),'daily BIN files',flush=True)
        (root/'upload_report.json').write_text(json.dumps(results,indent=2))
    return results
