import unittest
from types import SimpleNamespace
from tests import test_catalog_publish as fixture
from hf_catalog import publish

class NoOpCommitTests(unittest.TestCase):
    def test_existing_head_is_not_a_new_upload(self):
        case=fixture.Tests()
        case.setUp()
        try:
            case.api.create_commit=lambda *a,**k:SimpleNamespace(oid='pinned')
            result=publish(case.root,case.report,'owner/dataset',3)
            self.assertEqual(result[0]['result'],'already_unchanged')
            self.assertNotIn('uploaded',[item['result'] for item in result])
        finally:
            case.tearDown()

if __name__=='__main__':unittest.main()
