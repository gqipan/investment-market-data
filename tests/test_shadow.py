import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from market_data.consumer import stage_release
from market_data.release import build_candidate, dataset_identity
from market_data.shadow import compare_shadow

ROOT = Path(__file__).resolve().parents[1]


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        release=build_candidate(root=ROOT,dataset_name='csi300-demo',as_of='2026-10-22',revision=1,output_dir=self.root/'releases')
        raw=(release/'release-manifest.json').read_bytes();header=json.loads(raw)
        self.dataset=stage_release(release_dir=release,expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
            expected_dataset_id=header['datasets'][0]['dataset_id'],cache_dir=self.root/'cache',allow_synthetic=True)
        # Construct a test-only shape for real-data validation, never publish these fixtures.
        self.update(data_class='normalized_price',synthetic=False,source='test_reference')

    def update(self,**fields):
        path=self.dataset/'manifest.json';m=json.loads(path.read_text());m.update(fields)
        m['dataset_id']=dataset_identity(m);path.write_text(json.dumps(m))
        self.pin=hashlib.sha256(path.read_bytes()).hexdigest()

    def compare(self,**options):
        return compare_shadow(candidate=self.dataset,baseline=self.dataset,
            candidate_manifest_sha256=options.pop('pin',self.pin),baseline_manifest_sha256=self.pin,
            expected_session=options.pop('expected_session','2026-10-22'),**options)

    def test_short_fixture_does_not_qualify_for_ma_history(self):
        result=self.compare()
        self.assertFalse(result['comparison_passed'])
        self.assertIn('insufficient_history',result['reasons'])
        self.assertFalse(result['promotion_allowed'])

    def test_equal_compatible_data_reports_comparison_only(self):
        result=self.compare(minimum_sessions=5)
        self.assertTrue(result['comparison_passed'])
        self.assertEqual(result['overlap_sessions'],5)
        self.assertTrue(result['same_dataset'])
        self.assertFalse(result['promotion_allowed'])

    def test_stale_data_fails(self):
        result=self.compare(minimum_sessions=5,expected_session='2026-10-23')
        self.assertFalse(result['comparison_passed'])
        self.assertIn('unexpected_last_session',result['reasons'])

    def test_wrong_manifest_pin_and_damaged_bars_fail(self):
        with self.assertRaisesRegex(ValueError,'manifest.*pin'):self.compare(pin='0'*64)
        with (self.dataset/'bars.csv').open('a') as f:f.write('corrupt\n')
        with self.assertRaises(ValueError):self.compare()

    def test_synthetic_data_is_rejected(self):
        self.update(data_class='synthetic_fixture',synthetic=True)
        with self.assertRaisesRegex(ValueError,'synthetic'):self.compare()

    def test_invalid_thresholds_fail(self):
        for value in [-1,float('nan'),float('inf')]:
            with self.assertRaises(ValueError):self.compare(relative_tolerance=value)
        with self.assertRaises(ValueError):self.compare(minimum_sessions=0)

    def test_malformed_root_or_missing_units_are_clean_input_errors(self):
        path=self.dataset/'manifest.json'
        original=path.read_bytes()
        for value in [[],None,{},dict(json.loads(original),volume_unit=None),
                      dict(json.loads(original),currency='USD'),dict(json.loads(original),symbol='invalid')]:
            with self.subTest(value=value):
                if isinstance(value,dict) and value:
                    value['dataset_id']=dataset_identity(value)
                path.write_text(json.dumps(value));self.pin=hashlib.sha256(path.read_bytes()).hexdigest()
                with self.assertRaises(ValueError):self.compare()
        path.write_bytes(original)

    def test_different_units_or_ohlcv_do_not_pass(self):
        import shutil
        other=self.root/'baseline';shutil.copytree(self.dataset,other)
        def check():
            return compare_shadow(candidate=self.dataset,baseline=other,candidate_manifest_sha256=self.pin,
                baseline_manifest_sha256=hashlib.sha256((other/'manifest.json').read_bytes()).hexdigest(),
                expected_session='2026-10-22',minimum_sessions=5)
        m=json.loads((other/'manifest.json').read_text());m['volume_unit']='incompatible'
        m['dataset_id']=dataset_identity(m);(other/'manifest.json').write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError,'volume_unit'):check()
        m['volume_unit']=json.loads((self.dataset/'manifest.json').read_text())['volume_unit']
        bars=other/'bars.csv';lines=bars.read_text().splitlines();parts=lines[1].split(',');parts[-1]=str(float(parts[-1])+1);lines[1]=','.join(parts);bars.write_text('\n'.join(lines)+'\n')
        m['sha256']=hashlib.sha256(bars.read_bytes()).hexdigest();m['dataset_id']=dataset_identity(m)
        (other/'manifest.json').write_text(json.dumps(m))
        result=check();self.assertFalse(result['comparison_passed']);self.assertEqual(result['different_sessions'],1)
