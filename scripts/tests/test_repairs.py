"""Regression tests for state, data integrity, and output consistency; all data is synthetic."""
import contextlib
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import render_outputs as ro
import state_io as st
import tikhub_client as tc
import trip_support as support
import validate_trip as vt


def sample():
    return json.loads((ROOT / 'assets/examples/sample_trip.json').read_text(encoding='utf-8'))


def item(t, key):
    return next(i for d in t['itinerary']['days'] for i in d['items'] if i['item_id'] == key)


def audit(t):
    support.preflight(t)
    a = vt.Audit(t)
    vt.semantic_checks(t, a)
    return a


def silent(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args)


class Files(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.path = self.base / 'trip.json'
        self.trip = sample()
        self.write(self.trip)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, t, path=None):
        (path or self.path).write_text(json.dumps(t, ensure_ascii=False), encoding='utf-8')

    def render(self):
        self.assertEqual(silent(ro.main, [str(self.path)]), 0)
        return json.loads((self.base / 'manifest.v1.json').read_text(encoding='utf-8'))

    def save_args(self, **kwargs):
        args = dict(trip=str(self.path), affected=None, summary='修改第二天', from_file=None)
        args.update(kwargs)
        return SimpleNamespace(**args)

    def test_save_rejects_secret_before_any_snapshot(self):
        self.trip['itinerary']['sources_note'] = 'TIKHUB_API_KEY=SYNTHETIC_NOT_A_REAL_KEY'
        self.write(self.trip)
        with self.assertRaises(ValueError):
            st.cmd_save(self.save_args())
        self.assertFalse((self.base / 'versions').exists())

    def test_save_summary_scanned_before_snapshot(self):
        candidate = self.base / 'edited.json'
        self.write(self.trip, candidate)
        with self.assertRaises(ValueError):
            st.cmd_save(self.save_args(from_file=str(candidate), summary='TIKHUB_API_KEY=SYNTHETIC_NOT_A_REAL_KEY'))
        self.assertFalse((self.base / 'versions').exists())

    def test_in_place_save_without_baseline_refused(self):
        self.trip['itinerary']['summary']['tagline'] = 'already edited'
        self.write(self.trip)
        with self.assertRaises(ValueError):
            st.cmd_save(self.save_args())
        self.assertFalse((self.base / 'versions').exists())

    def test_render_checkpoint_preserves_original_in_place_edit(self):
        self.render()
        original = copy.deepcopy(self.trip)
        self.trip['itinerary']['summary']['tagline'] = 'edited'
        self.write(self.trip)
        self.assertEqual(silent(st.cmd_save, self.save_args()), 0)
        snap = json.loads((self.base / 'versions/trip.v1.json').read_text(encoding='utf-8'))
        self.assertEqual(snap, original)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['plan_version'], 2)

    def test_save_from_candidate_preserves_original_without_prior_render(self):
        original = copy.deepcopy(self.trip)
        self.trip['itinerary']['summary']['tagline'] = 'new copy'
        candidate = self.base / 'edited.json'
        self.write(self.trip, candidate)
        self.assertEqual(silent(st.cmd_save, self.save_args(from_file=str(candidate))), 0)
        self.assertEqual(json.loads((self.base / 'versions/trip.v1.json').read_text(encoding='utf-8')), original)

    def test_restore_preflight_precedes_snapshot(self):
        support.checkpoint(str(self.base), self.trip)
        self.trip['plan_version'] = 2
        self.write(self.trip)
        old = sample()
        old['request']['transport']['notes'] = 'Bearer SYNTHETIC_INVALID_TOKEN_12345'
        self.write(old, self.base / 'versions/trip.v1.json')
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            st.cmd_restore(SimpleNamespace(trip_dir=str(self.base), v='1'))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.base / 'versions/trip.v2.json').exists())

    def test_restore_invalidates_facts_and_keeps_history(self):
        support.checkpoint(str(self.base), self.trip)
        self.trip['plan_version'] = 2
        self.trip['changelog'].append({'version':2,'at':self.trip['generated_at'],'summary':'second','affected_ids':[]})
        self.write(self.trip)
        self.assertEqual(silent(st.cmd_restore, SimpleNamespace(trip_dir=str(self.base), v='1')), 0)
        t = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(t['plan_version'], 3)
        self.assertEqual([x['version'] for x in t['changelog']], [1,2,3])
        self.assertEqual(t['status'], 'draft')
        self.assertIsNone(t['checked_at'])
        self.assertFalse(any(f['status'] == 'verified' for f in t['facts']))

    def test_stale_outputs_rejected_even_same_version(self):
        self.render()
        self.trip['itinerary']['days'][0]['items'][0]['description'] += ' changed'
        a = vt.Audit(self.trip)
        vt.check_outputs(self.trip, str(self.path), None, a)
        self.assertGreater(a.summary()['blocking'], 0)

    def test_original_outputs_pass(self):
        self.render()
        a = vt.Audit(self.trip)
        vt.check_outputs(self.trip, str(self.path), None, a)
        self.assertEqual(a.summary()['blocking'], 0, a.issues)

    def test_forged_coverage_and_file_hash_cannot_hide_missing_text(self):
        self.trip['request']['special_needs'] = ['MUST_VISIBLE_REQUIREMENT']
        self.write(self.trip)
        man = self.render()
        desc = next(f for f in man['files'] if f['kind'] == 'html')
        file = self.base / desc['path']
        file.write_text(file.read_text(encoding='utf-8').replace('MUST_VISIBLE_REQUIREMENT', 'REMOVED'), encoding='utf-8')
        desc['sha256'] = hashlib.sha256(file.read_bytes()).hexdigest()
        desc['bytes'] = file.stat().st_size
        self.write(man, self.base / 'manifest.v1.json')
        a = vt.Audit(self.trip)
        vt.check_outputs(self.trip, str(self.path), None, a)
        self.assertGreater(a.summary()['blocking'], 0)

    def test_manifest_path_escape_rejected_before_read(self):
        man = self.render()
        man['files'][0]['path'] = '../outside.md'
        self.write(man, self.base / 'manifest.v1.json')
        a = vt.Audit(self.trip)
        with patch.object(vt, 'sha256_file', side_effect=AssertionError('must not read')):
            vt.check_outputs(self.trip, str(self.path), None, a)
        self.assertGreater(a.summary()['blocking'], 0)

    def test_renderer_preflight_rejects_secrets_and_path_traversal(self):
        for mutation in ('secret', 'path'):
            t = sample()
            if mutation == 'secret':
                t['request']['transport']['notes'] = 'Bearer SYNTHETIC_INVALID_TOKEN_12345'
            else:
                t['trip_id'] = '../../escape'
            self.write(t)
            self.assertNotEqual(silent(ro.main, [str(self.path)]), 0)
            self.assertFalse((self.base / 'outputs').exists())
            self.assertFalse((self.base / 'versions').exists())

    def test_renderer_force_cannot_replace_history(self):
        self.render()
        before = (self.base / 'versions/trip.v1.json').read_bytes()
        self.trip['itinerary']['summary']['tagline'] = 'changed'
        self.write(self.trip)
        self.assertNotEqual(silent(ro.main, [str(self.path), '--force']), 0)
        self.assertEqual((self.base / 'versions/trip.v1.json').read_bytes(), before)

    def test_failed_output_write_does_not_leave_completion_manifest(self):
        self.render()
        with patch.object(ro, 'atomic_text', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                silent(ro.main, [str(self.path), '--force'])
        self.assertFalse((self.base / 'manifest.v1.json').exists())

    def test_initialize_without_fake_dates(self):
        t = st.skeleton('sample-new', None, None)
        support.preflight(t)
        self.assertEqual(t['itinerary']['days'], [])
        self.assertNotIn('1970', json.dumps(t))
        self.write(t)
        self.assertEqual(silent(ro.main, [str(self.path)]), 0)

    def test_output_overrides_cannot_destroy_source_or_history(self):
        before = self.path.read_bytes()
        for target in [self.path, self.base / 'versions' / 'manifest.old.json', self.base / 'audit.v1.json']:
            self.assertNotEqual(silent(ro.main, [str(self.path), '--manifest', str(target)]), 0)
        for target in [self.path, self.base / 'versions' / 'audit.old.json', self.base / 'manifest.v1.json']:
            self.assertNotEqual(silent(vt.main, [str(self.path), '--out', str(target)]), 0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.base / 'versions').exists())
        self.assertFalse((self.base / 'outputs').exists())


class ContentAndValidation(unittest.TestCase):
    def test_special_needs_optional_and_na_visible_in_both(self):
        t = sample()
        t['request']['special_needs'] = ['needs<safe>text']
        t['request']['interests']['optional'] = ['optional & experience']
        t['itinerary']['checklist'][0]['status'] = 'na'
        c = ro.Ctx(t)
        md, html = ro.render_md(c), ro.render_html(c)
        text = ro.html_to_text(html)
        for v in ['needs<safe>text', 'optional & experience', '不适用']:
            self.assertIn(v, md)
            self.assertIn(v, text)
        self.assertIn(' disabled', html)
        self.assertEqual(ro.coverage(text, c.required_strings())['missing'], [])

    def test_unknown_budget_never_called_established(self):
        c = ro.Ctx(sample())
        for content in [ro.render_md(c), ro.html_to_text(ro.render_html(c))]:
            self.assertIn('含未知费用时尚不能确认', content)
        a = audit(sample())
        self.assertTrue(any(x['severity'] == 'conditional' and '未知类别' in x['evidence'] for x in a.issues))

    def test_per_person_requires_explicit_count(self):
        t = sample()
        t['itinerary']['budget']['hard_limit'] = None
        t['request']['budget'].update(mode='per_person', amount_max=1000)
        self.assertIsNone(support.budget_limit(t))
        t['request']['budget']['budget_persons'] = 2
        self.assertEqual(support.budget_limit(t), 2000)
        t['request']['budget']['currency'] = 'USD'
        self.assertIsNone(support.budget_limit(t))

    def test_failed_booking_with_missing_opening_is_blocking(self):
        for windows in ([], [{'days':[1], 'dates':None, 'open':'09:00','close':'17:00','last_entry':None}]):
            t = sample()
            next(p for p in t['places'] if p['place_id']=='place-museum')['opening_windows'] = windows
            item(t, 'item-d1-museum')['booking_status'] = 'failed'
            self.assertTrue(any(x['severity']=='blocking' and '预约失败仍在主行程' in x['evidence'] for x in audit(t).issues))

    def test_cross_day_overlap_and_offset_equivalence(self):
        t = sample()
        overnight = copy.deepcopy(t['itinerary']['days'][0]['items'][-1])
        overnight.update(item_id='overnight',kind='rest',place_id=None,
                         planned_start='2026-10-10T23:00:00+08:00',planned_end='2026-10-11T01:00:00Z')
        t['itinerary']['days'][0]['items'].append(overnight)
        self.assertTrue(any('跨日时间重叠' in i['evidence'] for i in audit(t).issues))
        overnight['planned_end']='2026-10-11T00:00:00Z'  # 08:00 +08; touching is valid.
        self.assertFalse(any('跨日时间重叠' in i['evidence'] for i in audit(t).issues))

    def test_duplicate_ids_and_nonfinite_amount_rejected(self):
        t = sample()
        t['places'].append(copy.deepcopy(t['places'][0]))
        self.assertTrue(any('重复 place_id' in i['evidence'] for i in audit(t).issues))
        t = sample()
        t['request']['budget']['amount_max'] = float('nan')
        with self.assertRaises(ValueError):
            support.preflight(t)

    def test_malformed_root_and_secret_do_not_write_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'trip.json'
            for data in ([], {'token':'SYNTHETIC_RAW_TOKEN_VALUE'}):
                p.write_text(json.dumps(data),encoding='utf-8')
                self.assertEqual(silent(vt.main,[str(p)]),2)
                self.assertEqual([x.name for x in Path(tmp).iterdir()],['trip.json'])


class TikHubRepairs(unittest.TestCase):
    def test_cli_dry_run_does_not_read_token_environment(self):
        original = os.environ.get
        def guarded(key, *args):
            self.assertNotEqual(key, 'TEST_TIKHUB_TOKEN')
            return original(key, *args)
        with patch.object(os.environ, 'get', side_effect=guarded):
            self.assertEqual(silent(tc.main, ['search', '测试', '--dry-run', '--token-env', 'TEST_TIKHUB_TOKEN']), 0)

    def test_cli_result_cannot_overwrite_budget_or_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = str(Path(folder) / 'usage.json')
            for out in (ledger, ledger + '.lock'):
                with self.assertRaises(SystemExit):
                    silent(tc.main, ['search', '测试', '--usage-file', ledger, '--out', out])
            self.assertEqual(list(Path(folder).iterdir()), [])

    def transport(self, data):
        calls=[]
        def send(url, headers):
            calls.append(url)
            return 200, json.dumps(data)
        return send,calls

    def test_missing_list_stops_adapter_but_empty_list_valid(self):
        for method, args in [('search',('k',)),('comments',('n',)),('sub_comments',('n','c'))]:
            send,calls=self.transport({'code':200,'data':{'data':{'renamed':[]}}})
            c=tc.Client('SYNTHETIC_TOKEN',tc.Budget(5),transport=send)
            with self.assertRaises(tc.StructureError):
                getattr(c,method)(*args)
            with self.assertRaises(tc.TikHubError):
                c.search('again')
            self.assertEqual(len(calls),1)
        send,_=self.transport({'code':200,'data':{'data':{'items':[]}}})
        self.assertEqual(tc.Client('SYNTHETIC_TOKEN',tc.Budget(5),transport=send).search('k')['items'],[])

    def test_business_auth_failure_stops_shared_budget(self):
        for data in ({'code':401}, {'code':200,'data':{'code':403}},
                     {'code':200,'data':{'success':False,'msg':'余额不足'}}):
            send,calls=self.transport(data)
            b=tc.Budget(5)
            c=tc.Client('SYNTHETIC_TOKEN',b,transport=send)
            with self.assertRaises(tc.AuthError):
                c.search('k')
            with self.assertRaises(tc.TikHubError):
                tc.Client('SYNTHETIC_TOKEN',b,transport=send).search('k')
            self.assertEqual(len(calls),1)

    def test_content_service_error_phrase_is_not_api_error(self):
        send,_=self.transport({'code':200,'data':{'data':{'items':[{'note_id':'n1','desc':'旅行时应用出现服务异常'}]}}})
        c=tc.Client('SYNTHETIC_TOKEN',tc.Budget(5),transport=send)
        self.assertEqual(len(c.search('k')['items']),1)

    def test_budget_survives_process_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'usage.json'
            code="import sys; sys.path.insert(0,sys.argv[1]); from tikhub_client import PersistentBudget; b=PersistentBudget(2,sys.argv[2]); b.reserve(); b.close()"
            for _ in range(2):
                r=subprocess.run([sys.executable,'-X','utf8','-B','-c',code,str(ROOT/'scripts'),str(path)],capture_output=True)
                self.assertEqual(r.returncode,0,r.stderr)
            b=tc.PersistentBudget(2,str(path))
            try:
                with self.assertRaises(tc.BudgetExceeded):
                    b.reserve()
                self.assertEqual(b.used,2)
            finally:
                b.close()

    def test_ledger_lock_limit_and_corruption_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=str(Path(tmp)/'usage.json')
            b=tc.PersistentBudget(2,p)
            try:
                with self.assertRaises(tc.TikHubError):
                    tc.PersistentBudget(2,p)
            finally:
                b.close()
            with self.assertRaises(ValueError):
                tc.PersistentBudget(3,p)
            Path(p).write_text('broken',encoding='utf-8')
            with self.assertRaises(ValueError):
                tc.PersistentBudget(2,p)
            self.assertFalse(Path(p+'.lock').exists())

    def test_response_metadata_differentiates_mock_and_dry_run(self):
        send,_=self.transport({'code':200,'data':{'data':{'items':[]}}})
        c=tc.Client('SYNTHETIC_TOKEN',tc.Budget(5),transport=send)
        self.assertEqual(c.search('k')['source_meta']['response_mode'],'mock')
        c=tc.Client(None,tc.Budget(5),dry_run=True,log=lambda _:None)
        self.assertEqual(c.search('k')['source_meta']['response_mode'],'dry_run')

    def test_timeout_counted_and_never_retried(self):
        calls=[]
        def send(*args):
            calls.append(1)
            raise TimeoutError('timed out')
        b=tc.Budget(5)
        with self.assertRaises(tc.TikHubError):
            tc.Client('SYNTHETIC_TOKEN',b,transport=send).search('k')
        self.assertEqual(len(calls),1)
        self.assertEqual(b.unknown_cost,1)

    def test_plain_http_rejected_without_transport(self):
        send,calls=self.transport({})
        with patch.object(tc,'BASE_URL','http://api.tikhub.io'):
            with self.assertRaises(tc.TikHubError):
                tc.Client('SYNTHETIC_TOKEN',tc.Budget(5),transport=send).search('k')
        self.assertEqual(calls,[])


if __name__=='__main__':
    unittest.main()
