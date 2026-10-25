import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from market_data.release import build_candidate
from market_data.transport import fetch_release, validate_download_url

ROOT = Path(__file__).resolve().parents[1]


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.release = build_candidate(root=ROOT, dataset_name='csi300-demo',
            as_of='2026-10-22', revision=1, output_dir=self.root/'release')
        self.raw = (self.release/'release-manifest.json').read_bytes()
        self.pin = hashlib.sha256(self.raw).hexdigest()
        self.dataset_id = json.loads(self.raw)['datasets'][0]['dataset_id']
        self.tag = self.release.name
        self.base = 'https://github.com/example/market/releases/download/'+self.tag+'/'
        self.metadata = {'tag_name':self.tag, 'draft':False, 'prerelease':False,
            'immutable':True, 'assets':[{'name':p.name,'browser_download_url':self.base+p.name,
                'size':p.stat().st_size} for p in self.release.iterdir()]}
        self.calls = []

    def read(self, url, limit):
        self.calls.append(url)
        if url.startswith('https://api.github.com/'):
            return json.dumps(self.metadata).encode()
        return (self.release/url.rsplit('/',1)[-1]).read_bytes()

    def fetch(self, **options):
        return fetch_release(repository=options.pop('repository','example/market'),
            release_id=self.tag, expected_manifest_sha256=options.pop('pin',self.pin),
            expected_dataset_id=self.dataset_id, cache_dir=self.root/'cache',
            download=options.pop('download',self.read), **options)

    def test_fixed_release_installs_idempotently(self):
        target = self.fetch(allow_synthetic=True)
        self.assertEqual(target,self.fetch(allow_synthetic=True))
        self.assertEqual(len(self.calls),10)
        self.assertEqual(json.loads((target/'manifest.json').read_text())['market'],'A')

    def test_synthetic_is_rejected_by_default(self):
        with self.assertRaisesRegex(ValueError,'synthetic'):
            self.fetch()
        self.assertFalse((self.root/'cache').exists())

    def test_wrong_pin_preserves_previous_pointer(self):
        cache=self.root/'cache';cache.mkdir();(cache/'current.json').write_text('previous')
        with self.assertRaisesRegex(ValueError,'manifest.*pin'):
            self.fetch(pin='0'*64,allow_synthetic=True)
        self.assertEqual((cache/'current.json').read_text(),'previous')
        self.assertEqual(len(self.calls),2)

    def test_draft_mutable_prerelease_and_wrong_tag_are_rejected(self):
        for key,value in [('draft',True),('prerelease',True),('immutable',False),('tag_name','latest')]:
            with self.subTest(key=key):
                old=self.metadata[key];self.metadata[key]=value
                with self.assertRaises(ValueError):self.fetch(allow_synthetic=True)
                self.metadata[key]=old
        self.assertFalse((self.root/'cache').exists())

    def test_cross_repository_asset_is_rejected(self):
        self.metadata['assets'][0]['browser_download_url']=self.base.replace('example/market','other/market')+self.metadata['assets'][0]['name']
        with self.assertRaisesRegex(ValueError,'asset URL'):self.fetch(allow_synthetic=True)

    def test_unexpected_duplicate_and_missing_assets_are_rejected(self):
        original=list(self.metadata['assets'])
        for assets in [original+[{'name':'private.txt','size':3,'browser_download_url':self.base+'private.txt'}], original+[original[0]],original[:-1]]:
            self.metadata['assets']=assets
            with self.assertRaises(ValueError):self.fetch(allow_synthetic=True)
        self.assertFalse((self.root/'cache').exists())

    def test_oversized_or_truncated_response_is_rejected(self):
        for bad in (b'',b'x'*(1024*1024+1)):
            def read(url,limit):
                return bad if url==self.base+'release-manifest.json' else self.read(url,limit)
            with self.assertRaises(ValueError):self.fetch(download=read,allow_synthetic=True)
        self.assertFalse((self.root/'cache').exists())

    def test_invalid_repository_or_pins_do_not_reach_network(self):
        for repo in ['../private','user/repo/extra','user/repo?token=secret']:
            with self.assertRaises(ValueError):self.fetch(repository=repo,allow_synthetic=True)
        with self.assertRaises(ValueError):self.fetch(pin='invalid',allow_synthetic=True)
        self.assertEqual(self.calls,[])

    def test_redirect_destination_must_be_trusted_https(self):
        for url in ['http://github.com/file','https://github.com.evil.invalid/file',
                    'https://user:password'+'@github.com/file','https://github.com:444/file']:
            with self.assertRaises(ValueError):validate_download_url(url)
        validate_download_url('https://release-assets.githubusercontent.com/file?signature=example')

    def test_http_reader_bounds_actual_response_and_checks_redirect(self):
        from market_data.transport import read_https
        class Response:
            headers={}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def geturl(self):return 'https://release-assets.githubusercontent.com/file'
            def read(self,limit):return b'ab'
        class Opener:
            def open(self,*args,**kwargs):return Response()
        with patch('market_data.transport.build_opener',return_value=Opener()):
            with self.assertRaisesRegex(ValueError,'size limit'):read_https(self.base+'file',1)
            with patch.object(Response,'geturl',return_value='http://github.com/file'):
                with self.assertRaisesRegex(ValueError,'HTTPS'):read_https(self.base+'file',10)

    def test_http_reader_uses_json_accept_for_github_api(self):
        from market_data.transport import read_https

        class Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def geturl(self):
                return url

            def read(self, limit):
                return b'{}'

        seen = []

        class Opener:
            def open(self, request, timeout):
                seen.append((url, request.get_header('Accept')))
                return Response()

        url = 'https://api.github.com/repos/example/market/releases/latest'
        with patch('market_data.transport.build_opener', return_value=Opener()):
            self.assertEqual(read_https(url, 10), b'{}')
        self.assertEqual(seen, [(url, 'application/vnd.github+json')])

    def test_non_public_real_manifest_is_rejected_before_remaining_downloads(self):
        m=json.loads(self.raw);m['data_class']='normalized_price';m['publication_eligible']=False
        raw=json.dumps(m).encode();(self.release/'release-manifest.json').write_bytes(raw)
        self.pin=hashlib.sha256(raw).hexdigest()
        for a in self.metadata['assets']:
            if a['name']=='release-manifest.json':a['size']=len(raw)
        with self.assertRaisesRegex(ValueError,'public consumption'):self.fetch()
        self.assertEqual(len(self.calls),2)
        self.assertFalse((self.root/'cache').exists())
