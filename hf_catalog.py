"""Publish daily BINs in SYMBOL/ and repair verified missing catalog entries."""
import hashlib
import json
import os
import re
from pathlib import Path


def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()


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


def remote_matches(item,local,sha,repo,revision,token,download):
    if getattr(item,'size',None)!=local.stat().st_size:return False
    lfs=getattr(item,'lfs',None)
    remote_sha=lfs.get('sha256') if isinstance(lfs,dict) else getattr(lfs,'sha256',None)
    if remote_sha:return remote_sha==sha
    downloaded=download(repo,item.path,repo_type='dataset',revision=revision,token=token)
    return digest(Path(downloaded))==sha


def write_status(root,repo,symbol,results,error=None,summary=False):
    payload={'repo':repo,'symbol':symbol,'results':results}
    if error:payload['error_type']=error
    (root/'upload_report.json').write_text(json.dumps(payload,indent=2))
    if not summary:return
    counts={name:sum(r['result']==name for r in results) for name in
            ('uploaded','index_repaired','skipped_existing','already_unchanged','needs_replace_or_review')}
    print('HF RESULT',json.dumps(counts),flush=True)
    filename=os.getenv('GITHUB_STEP_SUMMARY')
    if filename:
        with open(filename,'a') as f:
            f.write(f'\n## Hugging Face import\nDataset: `{repo}`\n\nDaily directory: `{symbol}/`\n\n')
            for name,value in counts.items():f.write(f'- {name}: **{value}**\n')
            if error:f.write(f'\nStopped: {error}. Check upload_report.json and logs.\n')
            if counts['uploaded']==0 and (counts['skipped_existing'] or counts['already_unchanged']):
                f.write('\nNo new BIN upload: existing files were skipped or all submitted contents already matched.\n')


def publish(root,report,repo,digits,replace_existing=False,batch_size=30):
    from huggingface_hub import HfApi,CommitOperationAdd,hf_hub_download
    token=os.environ['HF_TOKEN'];api=HfApi(token=token)
    root=Path(root);symbol=report['symbol'];results=[];error=None
    if not re.fullmatch('[A-Z0-9]{3,32}',symbol):raise ValueError('Unsafe symbol')
    print('HF TARGET',repo,'DAILY DIRECTORY',symbol+'/',flush=True)
    try:
        for offset in range(0,len(report['days']),batch_size):
            batch=report['days'][offset:offset+batch_size]
            revision=api.repo_info(repo,repo_type='dataset').sha
            info=api.get_paths_info(repo,paths=['index.txt']+[d['path'] for d in batch],repo_type='dataset',revision=revision)
            remote={item.path:item for item in info}
            if 'index.txt' not in remote:raise ValueError('Target index.txt must already exist')
            path=hf_hub_download(repo,'index.txt',repo_type='dataset',revision=revision,token=token)
            symbols,entries=parse_index(Path(path).read_text('utf-8-sig'))
            operations=[];pending=[];changed=False
            for d in batch:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',d['day']) or d['path']!=f"{symbol}/{symbol}_{d['day']}.BIN":
                    raise ValueError('Daily path is not in its symbol directory')
                file=root/d['path']
                if file.stat().st_size!=d['bytes'] or digest(file)!=d['sha256']:raise ValueError('Local BIN altered')
                row=f"FILE|{d['path']}|{d['day']}|{d['bytes']}"
                if d['path'] in remote and not replace_existing:
                    if entries.get(d['path'])==row and symbol in symbols:
                        results.append({'path':d['path'],'result':'skipped_existing'})
                        print('SKIPPED existing BIN and catalog entry:',d['path'],flush=True)
                        continue
                    if remote_matches(remote[d['path']],file,d['sha256'],repo,revision,token,hf_hub_download):
                        entries[d['path']]=row
                        symbols.setdefault(symbol,f'SYM|{symbol}|{symbol}|0|{digits}')
                        changed=True;pending.append({'path':d['path'],'result':'index_repaired'})
                        print('INDEX REPAIR queued:',d['path'],flush=True)
                    else:
                        results.append({'path':d['path'],'result':'needs_replace_or_review'})
                        print('WARNING: existing remote differs and catalog needs repair:',d['path'],flush=True)
                    continue
                if d['path'] in entries and d['path'] not in remote and not replace_existing:
                    raise ValueError('Index references missing remote file; enable replacement only after review')
                operations.append(CommitOperationAdd(path_in_repo=d['path'],path_or_fileobj=str(file)))
                audit=dict(d,source=report.get('source_format','ZIP BIN'),
                           source_member=d.get('source_member',report['source_member']),
                           source_members=d.get('source_members',[report['source_member']]),
                           source_completeness='user-confirmed; not independently provable from timestamps')
                operations.append(CommitOperationAdd(path_in_repo=f"audit/zip/{symbol}/{d['day']}.json",path_or_fileobj=json.dumps(audit).encode()))
                entries[d['path']]=row
                symbols.setdefault(symbol,f'SYM|{symbol}|{symbol}|0|{digits}')
                changed=True;pending.append({'path':d['path'],'result':'uploaded'})
            if changed:
                operations.append(CommitOperationAdd(path_in_repo='index.txt',path_or_fileobj=merged_index(symbols,entries).encode()))
                commit=api.create_commit(repo,repo_type='dataset',operations=operations,parent_commit=revision,
                                         commit_message=f'Import daily BINs and repair catalog: {symbol}')
                if commit.oid == revision:
                    results.extend(dict(item,result='already_unchanged',commit=commit.oid) for item in pending)
                    print('NO CHANGE: remote BINs/catalog already match; no new commit:',len(pending),flush=True)
                else:
                    results.extend(dict(item,commit=commit.oid) for item in pending)
                    print('COMMITTED daily BIN/catalog changes:',len(pending),flush=True)
            write_status(root,repo,symbol,results)
        if any(r['result']=='needs_replace_or_review' for r in results):
            raise ValueError('Existing different BINs need review or explicit replace_existing')
        return results
    except Exception as e:
        error=type(e).__name__
        raise
    finally:
        write_status(root,repo,symbol,results,error,summary=True)
