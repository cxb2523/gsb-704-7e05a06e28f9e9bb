"""Read-only query preflight: Workspace.plan shares admission rules with query."""
import json
import unittest

from agent_workspace import Workspace, WorkspaceError

ROWS = {
    'tasks': [
        {'id': 't1', 'name': '写周报', 'projectId': 'p1', 'assignee': 'mate', 'date': '2026-11-03',
         'duration': 30, 'status': 'doing', 'priority': 'P1'},
        {'id': 't2', 'name': '修复缺陷', 'projectId': 'p1', 'assignee': 'owner', 'date': '2026-11-04',
         'duration': 60, 'status': 'todo', 'priority': 'P2'},
    ],
    'projects': [{'id': 'p1', 'name': '项目甲', 'owner': 'owner', 'status': 'active'}],
    'people': [{'id': 'u1', 'displayName': '小周', 'role': 'dev', 'active': 1}],
}


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.loaded = []

        def load_rows(table):
            self.loaded.append(table)
            return [dict(row) for row in ROWS.get(table, [])]

        self.workspace = Workspace(load_rows)

    def plan(self, args):
        return self.workspace.plan(args)

    def test_plan_describes_a_normal_query_without_touching_data(self):
        result = self.plan({'sql': 'SELECT t.name, p.name AS project, count(*) AS n FROM tasks t '
                                   'JOIN projects p ON t.projectId = p.id WHERE t.date = ? GROUP BY t.name, p.name',
                            'params': ['2026-11-03'], 'offset': 0, 'limit': 10})
        self.assertTrue(result['readOnly'])
        self.assertTrue(result['compiled'])
        self.assertEqual(result['tables'], ['projects', 'tasks'])
        self.assertEqual(result['columns'], ['name', 'project', 'n'])
        self.assertEqual(result['columnCount'], 3)
        self.assertFalse(result['columnCountLimited'])
        self.assertFalse(result['columnNameLengthLimited'])
        self.assertNotIn('rows', result)
        self.assertNotIn('写周报', json.dumps(result, ensure_ascii=False), 'plan 不得把业务数据放进返回内容')
        self.assertEqual(self.loaded, [], 'plan 不应加载任何业务行')

    def test_plan_supports_ctes_and_never_calls_the_row_loader(self):
        result = self.plan({'sql': 'WITH x AS (SELECT * FROM tasks WHERE duration > ?) SELECT count(*) AS n FROM x',
                            'params': [10]})
        self.assertEqual(result['tables'], ['tasks'])
        self.assertEqual(result['columns'], ['n'])
        self.assertEqual(self.loaded, [])
        workspace = Workspace(lambda table: (_ for _ in ()).throw(AssertionError('plan 不得读取业务行')))
        self.assertEqual(workspace.plan({'sql': 'SELECT id, name FROM tasks ORDER BY id'})['tables'], ['tasks'])

    def test_plan_rejects_writes_and_escape_attempts_like_query(self):
        attacks = ["UPDATE tasks SET name='被篡改'", 'DELETE FROM tasks', 'DROP TABLE tasks',
                   "ATTACH DATABASE '/tmp/should-not-be-created.sqlite3' AS stolen", 'PRAGMA database_list',
                   "INSERT INTO tasks (id) VALUES ('x')", 'CREATE TEMP TABLE x AS SELECT * FROM tasks',
                   'SELECT 1; DELETE FROM tasks', 'WITH x AS (SELECT * FROM tasks) DELETE FROM tasks',
                   "SELECT load_extension('evil')", "SELECT readfile('/etc/passwd')", 'SELECT randomblob(1000000000)']
        for sql in attacks:
            with self.subTest(sql=sql):
                with self.assertRaises(WorkspaceError) as ctx:
                    self.plan({'sql': sql})
                self.assertNotIn('列', str(ctx.exception), '非只读/未授权错误应能与列形状错误区分')
                with self.assertRaises(WorkspaceError):
                    self.workspace.query({'sql': sql})
        self.assertEqual(self.loaded, [])

    def test_plan_rejects_unauthorized_tables_like_query(self):
        for sql in ('SELECT * FROM settings', 'SELECT * FROM users', 'SELECT * FROM sqlite_master',
                    'SELECT * FROM tasks t JOIN secret s ON t.id = s.id'):
            with self.subTest(sql=sql):
                with self.assertRaises(WorkspaceError) as ctx:
                    self.plan({'sql': sql})
                self.assertIn('只读查询未完成', str(ctx.exception))
                with self.assertRaises(WorkspaceError):
                    self.workspace.query({'sql': sql})
        self.assertEqual(self.loaded, [])

    def test_plan_rejects_duplicate_column_names_like_query(self):
        with self.assertRaises(WorkspaceError) as ctx:
            self.plan({'sql': 'SELECT 1 AS n, 2 AS n'})
        self.assertIn('列名重复', str(ctx.exception))
        with self.assertRaises(WorkspaceError):
            self.workspace.query({'sql': 'SELECT 1 AS n, 2 AS n'})

    def test_plan_rejects_more_than_twenty_columns_like_query(self):
        sql = 'SELECT ' + ','.join(f'{i} AS c{i}' for i in range(21))
        with self.assertRaises(WorkspaceError) as ctx:
            self.plan({'sql': sql})
        self.assertIn('最多选择20列', str(ctx.exception))
        with self.assertRaises(WorkspaceError):
            self.workspace.query({'sql': sql})
        result = self.plan({'sql': 'SELECT ' + ','.join(f'{i} AS c{i}' for i in range(20))})
        self.assertEqual(result['columnCount'], 20)
        self.assertFalse(result['columnCountLimited'])

    def test_plan_rejects_overlong_column_names_like_query(self):
        sql = 'SELECT 1 AS ' + 'a' * 65
        with self.assertRaises(WorkspaceError) as ctx:
            self.plan({'sql': sql})
        self.assertIn('不超过64字符的列名', str(ctx.exception))
        with self.assertRaises(WorkspaceError):
            self.workspace.query({'sql': sql})
        result = self.plan({'sql': 'SELECT 1 AS ' + 'a' * 64})
        self.assertEqual(result['columns'], ['a' * 64])
        self.assertFalse(result['columnNameLengthLimited'])

    def test_plan_validates_offset_and_limit_like_query(self):
        for bad in ({'offset': -1}, {'offset': 1_000_001}, {'offset': 1.5}, {'offset': '0'},
                    {'limit': 0}, {'limit': 101}, {'limit': -1}, {'limit': True}, {'limit': 1.5}):
            with self.subTest(bad=bad):
                args = {'sql': 'SELECT 1 AS n', **bad}
                with self.assertRaises(WorkspaceError) as ctx:
                    self.plan(args)
                self.assertIn('offset/limit', str(ctx.exception))
                with self.assertRaises(WorkspaceError):
                    self.workspace.query(args)
        self.assertEqual(self.loaded, [])

    def test_plan_validates_params_and_sql_shape_like_query(self):
        for params in ('not-a-list', [None] * 101, [{'x': 1}], [object()], 5):
            with self.subTest(params=params):
                with self.assertRaises(WorkspaceError) as ctx:
                    self.plan({'sql': 'SELECT ? AS n', 'params': params})
                self.assertIn('params', str(ctx.exception))
                with self.assertRaises(WorkspaceError):
                    self.workspace.query({'sql': 'SELECT ? AS n', 'params': params})
        # 参数数量与占位符不匹配属于编译期失败，plan 与 query 一致拒绝
        for params in ([1, 2], []):
            with self.subTest(params=params):
                with self.assertRaises(WorkspaceError):
                    self.plan({'sql': 'SELECT ? AS n', 'params': params})
                with self.assertRaises(WorkspaceError):
                    self.workspace.query({'sql': 'SELECT ? AS n', 'params': params})
        for sql in ('', '   ', 'SELECT ' + '1,' * 3000 + '1', 123):
            with self.subTest(sql=str(sql)[:20]):
                with self.assertRaises(WorkspaceError):
                    self.plan({'sql': sql})
                with self.assertRaises(WorkspaceError):
                    self.workspace.query({'sql': sql})
        self.assertEqual(self.loaded, [])

    def test_plan_and_query_agree_on_admission(self):
        statements = [
            {'sql': 'SELECT id, name FROM tasks ORDER BY id'},
            {'sql': 'SELECT count(*) AS n, sum(duration) AS total FROM tasks'},
            {'sql': 'WITH x AS (SELECT * FROM tasks) SELECT count(*) AS n FROM x'},
            {'sql': 'SELECT t.name, p.name AS project FROM tasks t JOIN projects p ON t.projectId = p.id'},
            {'sql': "SELECT json_extract(data, '$.name') AS n FROM tasks"},
            {'sql': 'SELECT name FROM tasks WHERE date = ?', 'params': ['2026-11-03']},
            {'sql': 'SELECT 1 AS n, 2 AS n'},
            {'sql': 'SELECT * FROM settings'},
            {'sql': "UPDATE tasks SET name='x'"},
            {'sql': 'SELECT ' + ','.join(f'{i} AS c{i}' for i in range(30))},
            {'sql': 'SELECT 1 AS ' + 'b' * 100},
            {'sql': 'SELECT ? AS n', 'params': [1, 2, 3]},
            {'sql': 'WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x) SELECT sum(n) AS n FROM x'},
        ]
        for args in statements:
            with self.subTest(sql=args['sql'][:40]):
                try:
                    planned = self.plan(args)
                except WorkspaceError as exc:
                    planned = exc
                try:
                    queried = self.workspace.query(args)
                except WorkspaceError as exc:
                    queried = exc
                self.assertEqual(isinstance(planned, WorkspaceError), isinstance(queried, WorkspaceError),
                                 'plan 与 query 的准入结论必须一致')
                if isinstance(planned, WorkspaceError):
                    self.assertEqual(str(planned), str(queried))
                else:
                    self.assertEqual(queried['columns'], planned['columns'])

    def test_plan_does_not_change_state_or_later_query_results(self):
        before = self.workspace.query({'sql': 'SELECT id, name FROM tasks ORDER BY id'})
        self.plan({'sql': 'SELECT id, name FROM tasks ORDER BY id'})
        for sql in ("DELETE FROM tasks", "INSERT INTO tasks (id) VALUES ('ghost')", 'DROP TABLE tasks'):
            with self.assertRaises(WorkspaceError):
                self.plan({'sql': sql})
        after = self.workspace.query({'sql': 'SELECT id, name FROM tasks ORDER BY id'})
        self.assertEqual(before, after)
        self.assertEqual([row['name'] for row in after['rows']], ['写周报', '修复缺陷'])


if __name__ == '__main__':
    unittest.main()
