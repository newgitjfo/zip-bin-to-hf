"""ZIP URL -> streamed BarReplay bin_v1 yearly file -> UTC daily BINs -> HF."""
import argparse
from array import array
import datetime as dt
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import struct
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from hf_catalog import publish

UTC=dt.timezone.utc
DAY_MS=86400000
COPY=1024*1024


def sha256(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def validate_url(url):
    u=urllib.parse.urlsplit(url)
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.port not in (None,443):
        raise ValueError('Use a public HTTPS download URL without username/password or custom port')
    addresses=socket.getaddrinfo(u.hostname,443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Private, loopback or non-public download host refused')
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        validate_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def download(url,path,limit):
    validate_url(url)
    opener=urllib.request.build_opener(SafeRedirect())
    part=path.with_suffix('.part')
    for attempt in range(3):
        total=0
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'BarReplay-ZipImporter/1.0','Accept-Encoding':'identity'})
            with opener.open(req,timeout=60) as response,part.open('wb') as out:
                if response.status!=200:raise ValueError('Expected HTTP 200 file response')
                length=response.headers.get('Content-Length')
                if length and int(length)>limit:raise ValueError('Download exceeds configured size limit')
                while chunk:=response.read(COPY):
                    total+=len(chunk)
                    if total>limit:raise ValueError('Download exceeds configured size limit')
                    if shutil.disk_usage(path.parent).free<len(chunk)+128*COPY:
                        raise ValueError('Insufficient free disk while downloading')
                    out.write(chunk)
                if length and total!=int(length):raise OSError('Truncated HTTP download')
            if not total:raise ValueError('Empty download')
            if not zipfile.is_zipfile(part):raise ValueError('URL did not return a supported ZIP archive')
            part.replace(path)
            print('ZIP downloaded:',total,'bytes',flush=True)
            return
        except (OSError,urllib.error.URLError):
            part.unlink(missing_ok=True)
            if attempt==2:raise
            print('Network failure; restarting bounded download attempt',attempt+2,flush=True)
            time.sleep(5*(attempt+1))
        except Exception:
            part.unlink(missing_ok=True)
            raise


def choose_member(archive,name,limit):
    candidates=[i for i in archive.infolist() if not i.is_dir() and i.filename.lower().endswith('.bin')]
    if name:
        found=[i for i in candidates if i.filename==name]
        if len(found)!=1:raise ValueError('Requested ZIP member not found or ambiguous')
        item=found[0]
    elif len(candidates)==1:item=candidates[0]
    else:raise ValueError('ZIP must contain exactly one BIN, or set member to its exact path')
    normalized=item.filename.replace('\\','/')
    if normalized.startswith('/') or any(s in ('..','') for s in normalized.split('/')) or ':' in normalized:
        raise ValueError('Unsafe ZIP member path')
    if stat.S_ISLNK(item.external_attr>>16) or item.flag_bits&1:
        raise ValueError('Symlink/encrypted ZIP members are unsupported')
    if item.file_size<4 or item.file_size>limit:raise ValueError('BIN size outside configured limit')
    if item.file_size/max(1,item.compress_size)>1000:raise ValueError('Suspicious ZIP compression ratio')
    return item


def read_column(file,offset,n,code):
    file.seek(offset)
    raw=file.read(n*8)
    if len(raw)!=n*8:raise ValueError('Truncated block column')
    column=array(code);column.frombytes(raw)
    if column.itemsize!=8:raise ValueError('Platform requires 8-byte array columns')
    import sys
    if sys.byteorder!='little':column.byteswap()
    return column


def write_block(file,times,bids,asks,start,stop):
    import sys
    n=stop-start
    if not n:return
    file.write(struct.pack('<i',n))
    for column in (times,bids,asks):
        section=column[start:stop]
        if sys.byteorder!='little':section.byteswap()
        file.write(section.tobytes())


def split_zip(zip_path,symbol,output,member='',max_bin_bytes=6*1024**3):
    if not re.fullmatch('[A-Z0-9]{3,32}',symbol):raise ValueError('Unsafe symbol')
    stage=Path(tempfile.mkdtemp(prefix='split-',dir=output))
    report={'symbol':symbol,'format':'BarReplay bin_v1','timezone':'UTC','ticks':0,'source_blocks':0,'days':[]}
    current=None;out=None;record=None;previous=-1
    max_time=int(dt.datetime.now(UTC).timestamp()*1000)
    try:
        def finish():
            out.close()
            path=stage/record['path']
            record['bytes']=path.stat().st_size;record['sha256']=sha256(path)
            report['days'].append(dict(record))
        with zipfile.ZipFile(zip_path) as z:
            selected=choose_member(z,member,max_bin_bytes)
            report['source_member']=selected.filename
            report['source_uncompressed_bytes']=selected.file_size
            # No extractall: only the selected BIN is decompressed, one block at a time.
            with z.open(selected) as source:
                consumed=0
                while head:=source.read(4):
                    if len(head)!=4:raise ValueError('Truncated BIN header')
                    consumed+=4
                    count=struct.unpack('<i',head)[0]
                    if not 0<count<=20000000:raise ValueError('Not supported bin_v1: invalid count')
                    size=count*24
                    if consumed+size>selected.file_size:raise ValueError('Truncated or incompatible BIN')
                    with tempfile.TemporaryFile(dir=output) as block:
                        remaining=size
                        while remaining:
                            data=source.read(min(COPY,remaining))
                            if not data:raise ValueError('Truncated BIN payload')
                            if shutil.disk_usage(output).free<len(data)+128*COPY:raise ValueError('Insufficient free disk')
                            block.write(data);remaining-=len(data)
                        consumed+=size;report['source_blocks']+=1
                        for offset in range(0,count,100000):
                            n=min(100000,count-offset)
                            times=read_column(block,offset*8,n,'q')
                            bids=read_column(block,count*8+offset*8,n,'d')
                            asks=read_column(block,count*16+offset*8,n,'d')
                            run_start=0
                            for i,(ts,bid,ask) in enumerate(zip(times,bids,asks)):
                                if not 0<ts<=max_time or ts<previous:raise ValueError('Unsorted, invalid or future tick time')
                                if not math.isfinite(bid) or not math.isfinite(ask) or bid<=0 or ask<bid:
                                    raise ValueError('Invalid tick price')
                                day=dt.date.fromordinal(dt.date(1970,1,1).toordinal()+ts//DAY_MS).isoformat()
                                if day!=current:
                                    if out is not None:
                                        write_block(out,times,bids,asks,run_start,i)
                                        finish()
                                    current=day;run_start=i
                                    rel=f'{symbol}/{symbol}_{day}.BIN'
                                    path=stage/rel;path.parent.mkdir(exist_ok=True)
                                    out=path.open('wb')
                                    record={'day':day,'path':rel,'ticks':0,'first_ms':ts}
                                    print('Splitting UTC day',day,flush=True)
                                record['ticks']+=1;record['last_ms']=ts
                                report['ticks']+=1;previous=ts
                            write_block(out,times,bids,asks,run_start,n)
                if consumed!=selected.file_size:raise ValueError('BIN size mismatch')
        if out is not None:finish();out=None
        if not report['ticks']:raise ValueError('No ticks in source BIN')
        (stage/'split_report.json').write_text(json.dumps(report,indent=2))
        return stage,report
    except Exception:
        if out is not None:out.close()
        shutil.rmtree(stage,ignore_errors=True)
        raise


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url',default=os.getenv('ZIP_URL',''))
    p.add_argument('--zip',help='Local ZIP instead of downloading')
    p.add_argument('--symbol',default=os.getenv('SYMBOL','USDJPY'))
    p.add_argument('--member',default=os.getenv('ZIP_MEMBER',''))
    p.add_argument('--repo',default=os.getenv('HF_DATASET','Esmaeil9ss/Tickdata'))
    p.add_argument('--output',default='work')
    p.add_argument('--max-download-gb',type=float,default=3)
    p.add_argument('--max-bin-gb',type=float,default=6)
    p.add_argument('--upload',action='store_true')
    p.add_argument('--confirm-complete-days',action='store_true')
    p.add_argument('--replace-existing',action='store_true')
    p.add_argument('--digits',type=int,default=3)
    args=p.parse_args()
    symbol=args.symbol.strip().upper()
    if not re.fullmatch('[A-Z0-9]{3,32}',symbol):p.error('Invalid symbol')
    if not 0<=args.digits<=12:p.error('digits must be 0..12')
    if not 0<args.max_download_gb<=10 or not 0<args.max_bin_gb<=12:p.error('Invalid size limit')
    if args.upload and not args.confirm_complete_days:p.error('Upload needs confirmation that source contains complete intended days')
    if args.upload and not os.getenv('HF_TOKEN'):p.error('Missing HF_TOKEN Actions Secret')
    root=Path(args.output);root.mkdir(parents=True,exist_ok=True)
    if args.zip:
        zip_path=Path(args.zip)
    elif args.url:
        zip_path=root/'source.zip';download(args.url,zip_path,int(args.max_download_gb*1024**3))
    else:p.error('Provide ZIP_URL / --url or --zip')
    if not zip_path.is_file():p.error('ZIP file not found')
    run,report=split_zip(zip_path,symbol,root,args.member,int(args.max_bin_gb*1024**3))
    reports=Path('reports');reports.mkdir(exist_ok=True)
    (reports/'split_report.json').write_text(json.dumps(report,indent=2))
    if not args.zip:zip_path.unlink(missing_ok=True)
    print('VALIDATED',report['ticks'],'ticks;',len(report['days']),'daily files',flush=True)
    print('Local output:',run)
    if args.upload:
        today=dt.datetime.now(UTC).date().isoformat()
        if any(d['day']>=today for d in report['days']):raise ValueError('Source includes incomplete current UTC day; no upload attempted')
        results=publish(run,report,args.repo,args.digits,args.replace_existing)
        (reports/'upload_report.json').write_text(json.dumps(results,indent=2))
    else:print('Conversion only: Hugging Face was NOT changed')
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as e:
        print('ERROR:',type(e).__name__,'- inspect configuration, limits and source format')
        raise SystemExit(1)
