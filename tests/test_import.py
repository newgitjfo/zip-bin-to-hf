from array import array
import datetime as dt
import io
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile
from import_zip import split_zip,choose_member,validate_url
from hf_catalog import parse_index,merged_index


def block(rows):
    n=len(rows)
    return struct.pack('<i',n)+struct.pack(f'<{n}q',*(r[0] for r in rows))+struct.pack(f'<{n}d',*(r[1] for r in rows))+struct.pack(f'<{n}d',*(r[2] for r in rows))


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.ms=int(dt.datetime(2025,12,31,tzinfo=dt.timezone.utc).timestamp())*1000
    def tearDown(self):self.temp.cleanup()
    def archive(self,data,name='USDJPY_2025.BIN'):
        p=self.root/'source.zip'
        with zipfile.ZipFile(p,'w',zipfile.ZIP_DEFLATED) as z:z.writestr(name,data)
        return p
    def test_cross_day_within_block(self):
        rows=[(self.ms+86399999,150,150.1),(self.ms+86400000,151,151.1),(self.ms+86400000,151.01,151.11)]
        p=self.archive(block(rows));out,r=split_zip(p,'USDJPY',self.root)
        self.assertEqual(r['ticks'],3);self.assertEqual([d['ticks'] for d in r['days']],[1,2])
        self.assertEqual([d['day'] for d in r['days']],['2025-12-31','2026-01-01'])
        data=(out/r['days'][1]['path']).read_bytes()
        self.assertEqual(struct.unpack('<i',data[:4]),(2,))
        self.assertEqual(struct.unpack('<2q',data[4:20]),(self.ms+86400000,)*2)
    def test_multiple_source_blocks(self):
        p=self.archive(block([(self.ms,150,151)])+block([(self.ms+1,151,152)]))
        out,r=split_zip(p,'USDJPY',self.root)
        self.assertEqual(r['source_blocks'],2);self.assertEqual(r['ticks'],2)
    def test_unsorted_rejected(self):
        p=self.archive(block([(self.ms+2,150,151),(self.ms+1,150,151)]))
        with self.assertRaises(ValueError):split_zip(p,'USDJPY',self.root)
        self.assertEqual(list(self.root.glob('split-*')),[])
    def test_truncated_rejected(self):
        p=self.archive(struct.pack('<i',100)+b'bad')
        with self.assertRaises(ValueError):split_zip(p,'USDJPY',self.root)
    def test_nan_rejected(self):
        p=self.archive(block([(self.ms,float('nan'),151)]))
        with self.assertRaises(ValueError):split_zip(p,'USDJPY',self.root)
    def test_wrong_format_rejected(self):
        p=self.archive(b'This is a CSV not a BIN file')
        with self.assertRaises(ValueError):split_zip(p,'USDJPY',self.root)
    def test_multiple_members_need_selection(self):
        p=self.archive(block([(self.ms,150,151)]))
        with zipfile.ZipFile(p,'a') as z:z.writestr('other.BIN',block([(self.ms,150,151)]))
        with self.assertRaises(ValueError):split_zip(p,'USDJPY',self.root)
        out,r=split_zip(p,'USDJPY',self.root,member='other.BIN');self.assertEqual(r['ticks'],1)
    def test_unsafe_member(self):
        p=self.archive(block([(self.ms,150,151)]),'../escape.BIN')
        with self.assertRaises(ValueError):split_zip(p,'USDJPY',self.root)
        self.assertFalse((self.root.parent/'escape.BIN').exists())
    def test_size_cap(self):
        p=self.archive(block([(self.ms,150,151)]))
        with self.assertRaises(ValueError):split_zip(p,'USDJPY',self.root,max_bin_bytes=10)
    def test_unsupported_url(self):
        with self.assertRaises(ValueError):validate_url('file:///etc/passwd')
        with self.assertRaises(ValueError):validate_url('http://example.com/a.zip')
        with self.assertRaises(ValueError):validate_url('https://user:pass@example.com/a.zip')
    def test_catalog_preserved(self):
        s,e=parse_index('BRIDX1\nSYM|USDJPY|Original|0|3\nFILE|old/path.BIN|2018-01-01|28\n')
        text=merged_index(s,e);self.assertIn('Original',text);self.assertIn('old/path.BIN',text)
    def test_unknown_catalog_rejected(self):
        with self.assertRaises(ValueError):parse_index('BRIDX1\nUNKNOWN|data')

if __name__=='__main__':unittest.main()
