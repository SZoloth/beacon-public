import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import db, briefs, followups, server


class StepOwnerTest(unittest.TestCase):
    def test_a_step_is_beacons_or_the_owners_and_old_rows_still_read(self):
        self.assertEqual(followups.who({"who": "beacon"}), "beacon")
        self.assertEqual(followups.who({"who": "owner"}), "owner")
        self.assertEqual(followups.who({"who": "an-older-first-name"}), "owner")


class FollowupsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        p = patch.object(db, 'DB_PATH', self.temp.name + '/test.db')
        p.start()
        self.addCleanup(p.stop)
        db.init_db()
        db.seed_workstreams([{'slug': 'travel', 'name': 'Travel'}], kind='area')
        db.seed_workstreams([{'slug': 'proj', 'name': 'Project'}])
        with sqlite3.connect(db.DB_PATH) as conn:
            for i, slug, status in [(1, 'travel', 'queued'), (2, 'proj', 'queued'), (3, 'travel', 'adopted')]:
                conn.execute("INSERT INTO items(id,source,external_id,title,status,synced_at) VALUES(?,'x',?,'Flights',?,'2026-10-10')", (i, str(i), status))
                conn.execute('INSERT INTO item_workstreams(item_id,workstream_id,confidence) SELECT ?,id,0.9 FROM workstreams WHERE slug=?', (i, slug))
        self.answer = dict(summary='Check fares.', why_now=None, next_steps=[dict(text='Ask Dad.', who='owner')],
                           drafts=[dict(kind='text', to='Dad', body='What dates work?')], sources=['/context.md'])

    def test_keep_queues_once_without_backfill_or_project_do(self):
        db.update_item_status(1, 'adopted')
        db.update_item_status(1, 'adopted')
        db.update_item_status(2, 'reviewed')
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute('SELECT item_id, status FROM briefs').fetchall(), [(1, 'skipped')])

    def test_new_keeps_run_before_failed_retries(self):
        from lib import followups
        db.update_item_status(1, 'adopted')
        with patch.object(followups, 'context', return_value='context'):
            followups.generate(agent=lambda *_: ({'summary': 'bad'}, 0))
            with sqlite3.connect(db.DB_PATH) as conn:
                conn.execute("INSERT INTO items(id,source,external_id,title,status,synced_at) VALUES(4,'x','4','Fares','queued','2026-10-11')")
                conn.execute("INSERT INTO item_workstreams(item_id,workstream_id,confidence) SELECT 4,id,0.9 FROM workstreams WHERE slug='travel'")
            db.update_item_status(4, 'adopted')
            seen = []
            followups.generate(limit=1, agent=lambda prompt, cwd: (seen.append(prompt), (self.answer, 0))[1])
        self.assertIn('Fares', seen[0])

    def test_roundtrip_and_no_repeat(self):
        from lib import followups
        db.update_item_status(1, 'adopted')
        with patch.object(followups, 'context', return_value='Thanksgiving; check TrueBlue'):
            agent = lambda prompt, cwd: (self.answer, 0)
            self.assertEqual(followups.generate(agent=agent)['ready'], 1)
            self.assertEqual(followups.generate(agent=agent)['ready'], 0)
        status, plans = server.route('GET', '/briefs', {})
        self.assertEqual(status, 200)
        self.assertEqual(plans[0]['briefs'][0]['follow_up'], self.answer)
        self.assertEqual(briefs.list_plans(), plans)

    def test_malformed_and_dry_run(self):
        from lib import followups
        db.update_item_status(1, 'adopted')
        with patch.object(followups, 'context', return_value='context') as ctx:
            self.assertEqual(followups.generate(dry_run=True)['pending'], 1)
            ctx.assert_not_called()
            self.assertEqual(followups.generate(agent=lambda *_: ({'summary': 'bad'}, 0))['failed'], 1)
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute('SELECT status FROM briefs').fetchone()[0], 'failed')

    def test_generate_passes_preview_takeaway_and_no_x_fetch_rule_to_agent(self):
        from lib import followups
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute(
                "UPDATE items SET body='dream sweater rotation https://t.co/abc', "
                "url='https://x.com/user/status/1', "
                "raw_json=? WHERE id=1",
                (json.dumps({
                    "text": "dream sweater rotation",
                    "media": [{"type": "photo", "url": "https://pbs.twimg.com/media/1.jpg"}],
                    "quotedTweet": {"author": {"username": "stylist"}, "text": "Merino crewnecks"},
                }),),
            )
            conn.execute("INSERT INTO item_gists VALUES(1,'Four photos of a sweater rotation','link only','do','Style shelf','2026-10-10')")
        db.update_item_status(1, 'adopted')
        seen = []
        with patch.object(followups, 'context', return_value='context'):
            followups.generate(agent=lambda prompt, cwd: (seen.append(prompt), (self.answer, 0))[1])
        self.assertIn('Four photos of a sweater rotation', seen[0])
        self.assertIn('Merino crewnecks', seen[0])
        self.assertIn('[1 image/video attachment(s)]', seen[0])
        self.assertIn('Do not WebFetch x.com, twitter.com, or t.co URLs', seen[0])


class AreaAgentBoundaryTest(unittest.TestCase):
    """The area agent is handed personal context in its prompt. It gets no way to read anything else locally."""

    def test_the_agent_has_web_tools_only_and_no_local_file_access(self):
        from lib import followups
        with patch.object(briefs, 'sonnet', return_value=({}, 0)) as sonnet:
            followups.run_agent('prompt', '/scratch')
        tools = set(sonnet.call_args.kwargs['tools'].split(','))
        self.assertEqual(tools, {'WebFetch', 'WebSearch'})
        self.assertEqual(sonnet.call_args.kwargs['cwd'], '/scratch')

    def test_it_runs_in_an_empty_throwaway_folder_not_the_home_directory(self):
        import os
        from lib import followups
        seen = {}

        def agent(prompt, cwd):
            seen.update(cwd=cwd, listing=os.listdir(cwd), exists=Path(cwd).is_dir())
            return {'summary': 'x'}, 0  # fails validation; the folder must still be cleaned up

        db_dir = tempfile.TemporaryDirectory()
        self.addCleanup(db_dir.cleanup)
        with patch.object(db, 'DB_PATH', db_dir.name + '/test.db'):
            db.init_db()
            db.seed_workstreams([{'slug': 'travel', 'name': 'Travel'}], kind='area')
            with sqlite3.connect(db.DB_PATH) as conn:
                conn.execute("INSERT INTO items(id,source,external_id,title,status,synced_at) VALUES(1,'x','1','Flights','adopted','2026-10-10')")
                conn.execute('INSERT INTO item_workstreams(item_id,workstream_id,confidence) SELECT 1,id,0.9 FROM workstreams WHERE slug=?', ('travel',))
                conn.execute("INSERT INTO briefs(item_id,workstream,status,note,created_at) VALUES(1,'travel','skipped',?,'2026-10-10')", (followups.PENDING,))
            with patch.object(followups, 'context', return_value='Thanksgiving'):
                followups.generate(agent=agent)
        self.assertTrue(seen['exists'])
        self.assertEqual(seen['listing'], [])
        self.assertNotEqual(Path(seen['cwd']).resolve(), Path.home().resolve())
        self.assertFalse(Path(seen['cwd']).exists())


class FollowupClaimTest(FollowupsTest):
    """Audit B09: a follow-up is saved only if the kept save and its brief didn't change during the run."""

    def test_dismissed_while_running_stays_dismissed(self):
        from lib import followups
        db.update_item_status(1, 'adopted')

        def agent(prompt, cwd):
            briefs.set_status(1, 'travel', 'dismissed')
            return self.answer, 0

        with patch.object(followups, 'context', return_value='context'):
            result = followups.generate(agent=agent)
        self.assertEqual((result['ready'], result['stale']), (0, 1))
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute('SELECT status FROM briefs WHERE item_id=1').fetchone()[0], 'dismissed')

    def test_a_second_run_started_meanwhile_skips_the_claimed_save(self):
        from lib import followups
        db.update_item_status(1, 'adopted')
        inner, calls = {}, []

        def agent(prompt, cwd):
            calls.append(prompt)
            if 'result' not in inner:
                inner['result'] = followups.generate(agent=agent)
            return self.answer, 0

        with patch.object(followups, 'context', return_value='context'):
            self.assertEqual(followups.generate(agent=agent)['ready'], 1)
        self.assertEqual(inner['result']['ready'], 0)
        self.assertEqual(len(calls), 1)
