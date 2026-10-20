import datetime as dt
import io
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
import warnings
import zipfile
import import_collection as app
from hf_catalog import publish


def block(rows):
    n=len(rows)
    return struct.pack('<i',n)+struct.pack(f'<{n}q',*(r[0] for r in rows))+struct.pack(f'<{n}d',*(r[1] for r in rows))+struct.pack(f'<{n}d',*(r[2] for r in rows))


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.ms=int(dt.datetime(2025,1,1,tzinfo=dt.timezone.utc).timestamp())*1000
        self.one=block([(self.ms,150.,151.)]);self.two=block([(self.ms+86400000,151.,152.)])
    def tearDown(self):self.tmp.cleanup()
    def archive(self,entries):
        p=self.root/'source.zip'
        with zipfile.ZipFile(p,'w') as z:
            for name,data in entries:z.writestr(name,data)
        return p
    def split(self,entries,**kwargs):
        return app.split_zip(self.archive(entries),'USDJPY',self.root,**kwargs)
    def test_annual(self):
        out,r=self.split([('USDJPY_2025.BIN',self.one+self.two)])
        self.assertEqual([d['day'] for d in r['days']],['2025-01-01','2025-01-02'])
    def test_many_daily_nested_and_unordered(self):
        out,r=self.split([('nested/USDJPY_2025-01-02.bin',self.two),('USDJPY_2025-01-01.BIN',self.one),('readme.txt',b'ignore')])
        self.assertEqual(r['ticks'],2);self.assertEqual(r['source_ticks'],2)
        self.assertEqual([d['day'] for d in r['days']],['2025-01-01','2025-01-02'])
        self.assertEqual((out/r['days'][0]['path']).read_bytes(),self.one)
    def test_single_daily(self):
        out,r=self.split([('USDJPY_2025-01-01.BIN',self.one)])
        self.assertEqual(r['members'][0]['kind'],'daily')
    def test_mixed_disjoint(self):
        out,r=self.split([('annual.BIN',self.one),('USDJPY_2025-01-02.BIN',self.two)])
        self.assertEqual(len(r['days']),2)
    def test_identical_overlap_uploaded_once(self):
        out,r=self.split([('annual.BIN',self.one+self.two),('USDJPY_2025-01-01.BIN',self.one)])
        self.assertEqual(r['ticks'],2);self.assertEqual(r['source_ticks'],3)
        self.assertEqual(len(r['identical_duplicates']),1)
        self.assertEqual(len(r['days'][0]['source_members']),2)
    def test_conflicting_overlap_cleanup(self):
        with self.assertRaisesRegex(ValueError,'Conflicting'):
            self.split([('a.BIN',self.one),('b.BIN',block([(self.ms,140.,141.)]))])
        self.assertFalse(list(self.root.glob('collection-*')));self.assertFalse(list(self.root.glob('split-*')))
    def test_partial_same_day_not_concatenated(self):
        with self.assertRaisesRegex(ValueError,'Conflicting'):
            self.split([('a.BIN',self.one),('b.BIN',block([(self.ms+1000,150.,151.)]))])
    def test_member_selects_exactly_one(self):
        out,r=self.split([('a.BIN',self.one),('bad.BIN',b'bad')],member='a.BIN')
        self.assertEqual(r['ticks'],1);self.assertEqual(r['source_members'],['a.BIN'])
    def test_member_missing(self):
        with self.assertRaises(ValueError):self.split([('a.BIN',self.one)],member='missing.BIN')
    def test_duplicate_zip_names(self):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with self.assertRaisesRegex(ValueError,'Duplicate'):self.split([('a.BIN',self.one),('a.BIN',self.one)])
    def test_no_bin(self):
        with self.assertRaises(ValueError):self.split([('a.txt',b'hello')])
    def test_total_size_cap(self):
        with self.assertRaisesRegex(ValueError,'Combined'):
            self.split([('a.BIN',self.one),('b.BIN',self.two)],max_bin_bytes=40)
    def test_daily_date_mismatch(self):
        with self.assertRaisesRegex(ValueError,'date'):
            self.split([('USDJPY_2025-01-02.BIN',self.one)])
    def test_named_daily_contains_two_days(self):
        with self.assertRaisesRegex(ValueError,'date'):
            self.split([('USDJPY_2025-01-01.BIN',self.one+self.two)])
    def test_wrong_daily_symbol(self):
        with self.assertRaisesRegex(ValueError,'different symbol'):
            self.split([('EURUSD_2025-01-01.BIN',self.one)])
    def test_unsafe_path(self):
        with self.assertRaises(ValueError):self.split([('../a.BIN',self.one)])
    def test_symlink(self):
        p=self.root/'source.zip';i=zipfile.ZipInfo('a.BIN');i.create_system=3;i.external_attr=(stat.S_IFLNK|0o777)<<16
        with zipfile.ZipFile(p,'w') as z:z.writestr(i,self.one)
        with self.assertRaises(ValueError):app.split_zip(p,'USDJPY',self.root)
    def test_same_ms_ticks_preserved(self):
        ticks=block([(self.ms,150.,151.),(self.ms,150.1,151.1)])
        out,r=self.split([('a.BIN',ticks)]);self.assertEqual(r['ticks'],2)
        self.assertEqual((out/r['days'][0]['path']).read_bytes(),ticks)
    def test_last_bad_member_prevents_any_upload(self):
        p=self.archive([('a.BIN',self.one),('z.BIN',b'invalid')])
        with patch.object(app.legacy,'publish') as send,patch.dict(os.environ,{'HF_TOKEN':'offline-placeholder'}),patch.object(sys,'argv',['import_collection.py','--zip',str(p),'--symbol','USDJPY','--output',str(self.root/'work'),'--upload','--confirm-complete-days']):
            with self.assertRaises(ValueError):app.main()
            send.assert_not_called()
    def test_mocked_upload_has_symbol_index_and_provenance(self):
        out,r=self.split([('USDJPY_2025-01-01.BIN',self.one),('USDJPY_2025-01-02.BIN',self.two)])
        index=self.root/'index.txt';index.write_text('BRIDX1\nSYM|USDJPY|Original|0|3\n')
        commits=[]
        api=SimpleNamespace(repo_info=lambda *a,**k:SimpleNamespace(sha='old'),get_paths_info=lambda *a,**k:[SimpleNamespace(path='index.txt')],create_commit=lambda *a,**k:commits.append(k) or SimpleNamespace(oid='new'))
        module=ModuleType('huggingface_hub');module.HfApi=lambda **k:api
        module.CommitOperationAdd=lambda **k:SimpleNamespace(**k)
        module.hf_hub_download=lambda *a,**k:str(index)
        with patch.dict(sys.modules,{'huggingface_hub':module}),patch.dict(os.environ,{'HF_TOKEN':'offline-placeholder'}):
            results=publish(out,r,'owner/data',3)
        self.assertEqual(len(results),2);self.assertEqual(commits[0]['parent_commit'],'old')
        ops={o.path_in_repo:o for o in commits[0]['operations']}
        self.assertIn('USDJPY/USDJPY_2025-01-01.BIN',ops);self.assertIn('index.txt',ops)
        self.assertIn('Original',ops['index.txt'].path_or_fileobj.decode())
        audit=json.loads(ops['audit/zip/USDJPY/2025-01-01.json'].path_or_fileobj)
        self.assertEqual(audit['source_member'],'USDJPY_2025-01-01.BIN')

if __name__=='__main__':unittest.main()
