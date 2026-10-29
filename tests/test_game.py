import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app import COOKIE, create_app, new_game


class GameTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "test.sqlite3")
        self.app = create_app({"TESTING": True, "DATABASE": self.path})
        self.client = self.app.test_client()
        data = self.client.get('/api/game').get_json()
        self.game, self.csrf = data['game'], data['csrf']

    def tearDown(self):
        self.directory.cleanup()

    def post(self, cells=None, route=None, **kwargs):
        body = {'game_id': self.game['id'], 'revision': self.game['revision']}
        if cells is not None:
            body.update(cells=cells, bin=route)
        body.update(kwargs)
        return self.client.post('/api/refine', json=body, headers={'X-CSRF-Token': self.csrf})

    def groups(self):
        groups = {}
        for cell in self.game['cells']:
            if 'signal' in cell:
                signal = cell['signal']
                groups.setdefault(signal['cluster'], {'cells': [], 'bin': signal['route']})['cells'].append(cell['id'])
        return list(groups.values())

    def internal_state(self):
        with closing(sqlite3.connect(self.path)) as db:
            return json.loads(db.execute('SELECT state FROM operators WHERE id=?',
                              (self.client.get_cookie(COOKIE).value,)).fetchone()[0])

    def test_page_and_static_assets(self):
        page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'number-field', page.data)
        for asset in ('style.css', 'game.js', 'mark.svg'):
            response = self.client.get('/static/' + asset)
            self.assertEqual(response.status_code, 200)
            response.close()
        self.assertIn("frame-ancestors 'none'", page.headers['Content-Security-Policy'])

    def test_puzzle_has_twenty_disjoint_groups_and_balanced_routes(self):
        for _ in range(100):
            state = new_game()
            members = [cell for group in state['groups'] for cell in group['members']]
            self.assertEqual(len(members), len(set(members)))
            self.assertEqual(len(members), 80)
            self.assertTrue(all(0 <= cell < 240 for cell in members))
            for route in range(1, 6):
                self.assertEqual({g['temper'] for g in state['groups'] if g['route'] == route}, {'WO', 'FC', 'DR', 'MA'})

    def test_valid_group_and_duplicate_cannot_increase_progress(self):
        group = self.groups()[0]
        response = self.post(group['cells'], group['bin'])
        self.assertTrue(response.get_json()['accepted'])
        self.game = response.get_json()['game']
        self.assertEqual(self.game['progress'], 5)
        duplicate = self.post(group['cells'], group['bin']).get_json()
        self.assertFalse(duplicate['accepted'])
        self.assertEqual(duplicate['game']['progress'], 5)

    def test_wrong_route_and_mixed_cluster_are_rejected(self):
        first, second = self.groups()[:2]
        response = self.post(first['cells'], first['bin'] % 5 + 1).get_json()
        self.assertFalse(response['accepted'])
        self.game = response['game']
        mixed = self.post(first['cells'][:2] + second['cells'][:2], first['bin']).get_json()
        self.assertFalse(mixed['accepted'])
        self.assertEqual(mixed['game']['progress'], 0)
        self.assertEqual(mixed['game']['mistakes'], 2)

    def test_invalid_payloads_do_not_mutate_state(self):
        initial = self.internal_state()
        for cells, route in [([], 1), ([0] * 4, 1), ([0, 1, 2, 999], 1),
                             ([True, 1, 2, 3], 1), ([0, 1, 2, 3], True),
                             ([0, 1, 2, 3], 6), ('1234', 1), ([0, 1, 2, 3], '1')]:
            self.assertEqual(self.post(cells, route).status_code, 400)
        for body in ([], None, 'text'):
            self.assertEqual(self.client.post('/api/refine', data=json.dumps(body),
                             content_type='application/json').status_code, 400)
        self.assertEqual(self.internal_state(), initial)

    def test_client_supplied_answers_cannot_forge_success(self):
        response = self.post([0, 1, 2, 3], 1, scary=[0, 1, 2, 3], progress=100, refined=list(range(20)))
        self.assertLessEqual(response.get_json()['game']['progress'], 5)
        self.assertFalse(response.get_json()['game']['complete'])

    def test_csrf_origin_and_content_type(self):
        payload = {'game_id': self.game['id'], 'revision': 0}
        for token in ('', 'wrong', 'é'):
            response = self.client.post('/api/reset', json=payload, headers={'X-CSRF-Token': token})
            self.assertEqual(response.status_code, 403)
        response = self.client.post('/api/reset', json=payload,
                                   headers={'X-CSRF-Token': self.csrf, 'Origin': 'https://example.org'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post('/api/reset', data='hello').status_code, 415)
        self.assertEqual(self.client.post('/api/reset', data='{' ,content_type='application/json').status_code, 400)

    def test_oversize_request_returns_json(self):
        response = self.client.post('/api/refine', json={'padding': 'x' * 5000})
        self.assertEqual(response.status_code, 413)
        self.assertIn('error', response.get_json())

    def test_cookie_is_private_and_api_not_cached(self):
        response = self.client.get('/api/game')
        cookie = response.headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        secure = create_app({'TESTING': True, 'DATABASE': self.path, 'COOKIE_SECURE': True})
        self.assertIn('Secure;', secure.test_client().get('/api/game').headers['Set-Cookie'])

    def test_progress_survives_server_restart(self):
        group = self.groups()[0]
        self.post(group['cells'], group['bin'])
        restarted = create_app({'TESTING': True, 'DATABASE': self.path}).test_client()
        restarted.set_cookie(COOKIE, self.client.get_cookie(COOKIE).value)
        result = restarted.get('/api/game').get_json()
        self.assertEqual(result['game']['progress'], 5)
        self.assertEqual(result['game']['id'], self.game['id'])
        self.assertEqual(result['csrf'], self.csrf)

    def test_operators_are_isolated(self):
        other = self.app.test_client()
        data = other.get('/api/game').get_json()
        self.assertNotEqual(data['game']['id'], self.game['id'])
        group = self.groups()[0]
        self.post(group['cells'], group['bin'])
        self.assertEqual(other.get('/api/game').get_json()['game']['progress'], 0)
        self.assertEqual(other.post('/api/reset', json={'game_id': data['game']['id'], 'revision': 0},
                         headers={'X-CSRF-Token': self.csrf}).status_code, 403)

    def test_stale_and_parallel_submissions_cannot_duplicate_progress(self):
        group = self.groups()[0]
        operator = self.client.get_cookie(COOKIE).value
        def submit(_):
            client = self.app.test_client()
            client.set_cookie(COOKIE, operator)
            return client.post('/api/refine', json={'game_id': self.game['id'], 'revision': 0,
                               'cells': group['cells'], 'bin': group['bin']},
                               headers={'X-CSRF-Token': self.csrf}).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(submit, range(2)))
        self.assertEqual(sorted(statuses), [200, 409])
        self.assertEqual(self.internal_state()['revision'], 1)
        self.assertEqual(len(self.internal_state()['refined']), 1)

    def test_reset_invalidates_old_file_and_keeps_session(self):
        old_id = self.game['id']
        result = self.client.post('/api/reset', json={'game_id': old_id, 'revision': 0},
                                  headers={'X-CSRF-Token': self.csrf}).get_json()
        self.assertNotEqual(result['game']['id'], old_id)
        self.assertEqual(result['game']['progress'], 0)
        stale = self.post(self.groups()[0]['cells'], self.groups()[0]['bin'])
        self.assertEqual(stale.status_code, 409)

    def test_completion_and_new_assignment(self):
        groups = self.groups()
        for group in groups:
            result = self.post(group['cells'], group['bin']).get_json()
            self.assertTrue(result['accepted'])
            self.game = result['game']
        self.assertEqual(self.game['progress'], 100)
        self.assertTrue(self.game['complete'])
        self.assertIsNotNone(self.game['finished'])
        self.assertTrue(all(bin['count'] == 4 and all(bin['tempers'].values()) for bin in self.game['bins']))
        self.assertEqual(self.post(groups[0]['cells'], groups[0]['bin']).status_code, 409)
        self.assertFalse(any('signal' in cell for cell in self.game['cells']))

    def test_missing_session_does_not_create_state_on_write(self):
        client = self.app.test_client()
        response = client.post('/api/reset', json={'game_id': 'fake', 'revision': 0})
        self.assertEqual(response.status_code, 401)


if __name__ == '__main__':
    unittest.main()
