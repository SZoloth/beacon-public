import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from lib import save_review, useful
from test_deck import seed_database, SINCE, UNTIL, TASK


class SaveReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'invented.db'
        self.context = Path(self.temp.name) / 'context.json'
        seed_database(self.db)
        self.now = datetime.now(timezone.utc)
        self.raw = {'version': 1, 'task': TASK, 'conditions': [
            {'id': 'backup', 'project': 'invented-reader', 'owner': 'Synthetic owner', 'source_ref': 'synthetic:condition',
             'observed_at': self.now.isoformat(), 'expires_at': (self.now + timedelta(days=1)).isoformat(), 'when': 'you need a local SQLite backup and integrity check.'}],
            'efforts': [{'id': 'ticket', 'project': 'invented-reader', 'owner': 'Synthetic owner', 'source_ref': 'synthetic:ticket',
                        'observed_at': self.now.isoformat(), 'expires_at': (self.now + timedelta(days=1)).isoformat(),
                        'summary': 'Implement local SQLite backups using Connection.backup and verify integrity.'}]}
        self.context.write_text(json.dumps(self.raw))
        self.before = self.db.read_bytes()

    def run_review(self, **options):
        return save_review.generate(1, self.db, self.context, clock=lambda: self.now, **options)

    def response(self, request):
        answers = {}
        for key, q in request['questions'].items():
            answers[key] = {'type': 'noul', 'noul': .95} if q['type'] == 'noul' else {
                'type': 'choice', 'choice': 'duplicate', 'confidence': .95,
                'probabilities': {k: 1 if k == 'duplicate' else 0 for k in q['criteria']}}
        return {'model': useful.MODEL, 'usage': {'input_tokens': 10, 'output_tokens': 10}, 'answers': answers}

    def test_local_preview_and_cli_are_read_only(self):
        with patch.object(useful, 'call_typesafe', side_effect=AssertionError('unexpected upload')):
            self.assertEqual(self.run_review()['future_use']['state'], 'hold')
            request = self.run_review(provider='typesafe', preview=True)['request']
            self.assertNotIn('synthetic:ticket', json.dumps(request))
            self.assertNotIn('Synthetic owner', json.dumps(request))
        cli = subprocess.run([sys.executable, 'beacon.py', 'consider', '1', '--db', str(self.db), '--context', str(self.context), '--html'], capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertIn('Hold: no supported authored condition.', cli.stdout)
        self.assertEqual(self.db.read_bytes(), self.before)
        deck = subprocess.run([sys.executable, 'beacon.py', 'deck', TASK, '--db', str(self.db), '--since', SINCE, '--until', UNTIL, '--html'], capture_output=True, text=True)
        self.assertEqual(deck.returncode, 0, deck.stderr)
        self.assertEqual(deck.stdout.count('<article>'), 5)
        self.assertIn('1 more eligible', deck.stdout)
        self.assertEqual(self.db.read_bytes(), self.before)

    def test_supported_authored_template_and_collision_never_authorize_actions(self):
        with patch.object(useful, 'call_typesafe', side_effect=lambda r, **_: self.response(r)):
            result = self.run_review(provider='typesafe', allow_upload=True)
        self.assertEqual(result['future_use']['text'], 'You will want this when ' + self.raw['conditions'][0]['when'])
        self.assertEqual(result['future_use']['currently_met'], 'not_evaluated')
        self.assertFalse(result['future_use']['routing_allowed'])
        self.assertEqual(result['collisions'][0]['relationship'], 'duplicate')
        self.assertFalse(result['collisions'][0]['merge_allowed'])
        self.assertEqual(self.db.read_bytes(), self.before)

    def test_stale_future_duplicate_ambiguous_and_invalid_context(self):
        raw = copy.deepcopy(self.raw)
        raw['efforts'][0]['observed_at'] = (self.now + timedelta(hours=1)).isoformat()
        context = save_review.context_packet(raw, self.now)
        self.assertEqual(len(context['efforts']), 0)
        self.assertEqual(context['excluded'][0]['reason'], 'stale_or_future_snapshot')
        raw['conditions'].append(copy.deepcopy(raw['conditions'][0]))
        with self.assertRaisesRegex(ValueError, 'unique'):
            save_review.context_packet(raw, self.now)
        raw['conditions'][1]['id'] = 'different'
        context = save_review.context_packet(raw, self.now)
        item = self.run_review()['item']
        request = save_review.build_request(item, context)
        result = save_review.interpret(item, context, request, self.response(request))
        self.assertEqual(result['future_use']['state'], 'hold')
        raw['efforts'][0]['account_secret'] = 'not allowed'
        with self.assertRaisesRegex(ValueError, 'fields'):
            save_review.context_packet(raw, self.now)

    def test_source_context_or_expiry_change_during_model_discards_result(self):
        for change in ('source', 'context', 'expiry'):
            with self.subTest(change=change):
                self.context.write_text(json.dumps(self.raw))
                times = iter([self.now, self.now + timedelta(days=2) if change == 'expiry' else self.now])
                def remote(request, **kwargs):
                    if change == 'source':
                        import sqlite3
                        with sqlite3.connect(self.db) as conn:
                            conn.execute("UPDATE items SET body=body || ' changed outside excerpt' WHERE id=1")
                    if change == 'context':
                        self.context.write_text(json.dumps({**self.raw, 'task': 'Different task'}))
                    return self.response(request)
                with patch.object(useful, 'call_typesafe', side_effect=remote), self.assertRaisesRegex(ValueError, 'changed/expired'):
                    save_review.generate(1, self.db, self.context, provider='typesafe', allow_upload=True, clock=lambda: next(times))

    def test_post_call_context_check_keeps_the_read_bound(self):
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('Unbounded context read')), patch.object(useful, 'call_typesafe', side_effect=lambda r, **_: self.response(r)):
            self.assertEqual(self.run_review(provider='typesafe', allow_upload=True)['future_use']['state'], 'proposed')

    def test_html_is_escaped_and_invalid_output_options_do_not_upload(self):
        result = self.run_review()
        result['item']['title'] = '<script>bad()</script>'
        result['item']['url'] = 'javascript:bad()'
        rendered = save_review.render_html(result)
        self.assertNotIn('<script>', rendered)
        self.assertNotIn('javascript:', rendered)
        import beacon
        with patch.object(sys, 'argv', ['beacon', 'deck', TASK, '--db', str(self.db), '--since', SINCE, '--until', UNTIL,
                                       '--provider', 'typesafe', '--allow-upload', '--json', '--html']), patch.object(useful, 'call_typesafe', side_effect=AssertionError('unexpected upload')), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            beacon.main()


if __name__ == '__main__':
    unittest.main()
