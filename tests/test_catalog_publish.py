import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace, ModuleType
import unittest
from unittest.mock import patch
from hf_catalog import publish

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.key='USDJPY/USDJPY_2025-01-02.BIN';self.bin=self.root/self.key
        self.bin.parent.mkdir();self.bin.write_bytes(b'validated sample')
        self.sha=hashlib.sha256(self.bin.read_bytes()).hexdigest()
        self.d={'day':'2025-01-02','path':self.key,'bytes':self.bin.stat().st_size,'sha256':self.sha}
        self.report={'symbol':'USDJPY','source_member':'annual.BIN','days':[self.d]}
        self.index=self.root/'index.txt';self.index.write_text('BRIDX1\nSYM|USDJPY|Original|0|3\n')
        self.items=[SimpleNamespace(path='index.txt')];self.commits=[]
        self.api=SimpleNamespace(repo_info=lambda *a,**k:SimpleNamespace(sha='pinned'),
                                 get_paths_info=lambda *a,**k:self.items,create_commit=self.commit)
        self.module=ModuleType('huggingface_hub')
        self.module.HfApi=lambda **k:self.api
        self.module.CommitOperationAdd=lambda **k:SimpleNamespace(**k)
        self.module.hf_hub_download=lambda *a,**k:str(self.index)
        self.env=patch.dict(os.environ,{'HF_TOKEN':'unit-test-placeholder','GITHUB_STEP_SUMMARY':str(self.root/'summary.md')})
        self.env.start();self.mock=patch.dict(sys.modules,{'huggingface_hub':self.module});self.mock.start()
    def tearDown(self):self.mock.stop();self.env.stop();self.temp.cleanup()
    def commit(self,*a,**k):self.commits.append(k);return SimpleNamespace(oid='newcommit')
    def remote(self,sha=None):
        self.items.append(SimpleNamespace(path=self.key,size=self.d['bytes'],lfs={'sha256':sha or self.sha}))
    def test_new_file_symbol_directory_and_atomic_index(self):
        r=publish(self.root,self.report,'owner/dataset',3)
        c=self.commits[0];self.assertEqual(c['parent_commit'],'pinned')
        self.assertIn(self.key,[o.path_in_repo for o in c['operations']])
        self.assertIn('index.txt',[o.path_in_repo for o in c['operations']]);self.assertEqual(r[0]['result'],'uploaded')
    def test_missing_index_repaired_without_reupload(self):
        self.remote();r=publish(self.root,self.report,'owner/dataset',3)
        self.assertEqual(r[0]['result'],'index_repaired')
        self.assertEqual([o.path_in_repo for o in self.commits[0]['operations']],['index.txt'])
        self.assertIn(self.key,self.commits[0]['operations'][0].path_or_fileobj.decode())
    def test_existing_catalog_skip_explicit(self):
        self.remote();self.index.write_text(self.index.read_text()+f"FILE|{self.key}|2025-01-02|{self.d['bytes']}\n")
        r=publish(self.root,self.report,'owner/dataset',3)
        self.assertFalse(self.commits);self.assertEqual(r[0]['result'],'skipped_existing')
        self.assertIn('No new BIN upload',(self.root/'summary.md').read_text())
    def test_different_unindexed_remote_not_silently_registered(self):
        self.remote('0'*64)
        with self.assertRaises(ValueError):publish(self.root,self.report,'owner/dataset',3)
        self.assertFalse(self.commits)
        self.assertEqual(json.loads((self.root/'upload_report.json').read_text())['results'][0]['result'],'needs_replace_or_review')
    def test_explicit_replace_uploads_binary(self):
        self.remote('0'*64)
        r=publish(self.root,self.report,'owner/dataset',3,replace_existing=True)
        self.assertEqual(r[0]['result'],'uploaded');self.assertIn(self.key,[o.path_in_repo for o in self.commits[0]['operations']])
    def test_index_read_failure_no_commit(self):
        def fail(*a,**k):raise OSError('offline')
        self.module.hf_hub_download=fail
        with self.assertRaises(OSError):publish(self.root,self.report,'owner/dataset',3)
        self.assertFalse(self.commits)
    def test_commit_conflict_not_recorded_as_uploaded(self):
        def fail(*a,**k):raise RuntimeError('conflict')
        self.api.create_commit=fail
        with self.assertRaises(RuntimeError):publish(self.root,self.report,'owner/dataset',3)
        data=json.loads((self.root/'upload_report.json').read_text());self.assertEqual(data['results'],[])
    def test_wrong_symbol_folder_rejected(self):
        self.d['path']='OTHER/OTHER_2025-01-02.BIN'
        with self.assertRaises(ValueError):publish(self.root,self.report,'owner/dataset',3)
        self.assertFalse(self.commits)

if __name__=='__main__':unittest.main()
