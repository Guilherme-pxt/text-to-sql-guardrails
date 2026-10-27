import unittest

from sqlguard.guards import GuardConfig, allowlist_check, normalize, run_guarded, strip_comments, wrap_limit
from tests.helpers import shared_db


class AllowlistTest(unittest.TestCase):
    def test_plain_select_and_cte_pass(self):
        self.assertIsNone(allowlist_check("SELECT COUNT(*) FROM orders;"))
        self.assertIsNone(allowlist_check("WITH x AS (SELECT 1 AS a) SELECT a FROM x"))
        self.assertIsNone(allowlist_check("select replace(name, 'a', 'b') from products"))

    def test_rejects_writes_ddl_pragma_attach(self):
        for sql in ("DELETE FROM orders", "UPDATE products SET list_price_usd = 0", "DROP TABLE orders",
                    "PRAGMA table_info(orders)", "ATTACH DATABASE 'x.db' AS x", "INSERT INTO orders VALUES (1)",
                    "REPLACE INTO orders VALUES (1)", "VACUUM"):
            self.assertIsNotNone(allowlist_check(sql), sql)

    def test_rejects_second_statement_but_not_semicolon_in_string(self):
        self.assertEqual(allowlist_check("SELECT 1; DELETE FROM orders"), "more than one statement")
        self.assertIsNone(allowlist_check("SELECT 'a; DROP TABLE x' AS s FROM orders"))

    def test_rejects_recursive_and_hidden_keywords(self):
        self.assertIn("RECURSIVE", allowlist_check(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT * FROM n"))
        self.assertIsNotNone(allowlist_check("SELECT * FROM (SELECT 1) WHERE 1 = 1 /* ok */ ; DROP TABLE x"))

    def test_comments_are_stripped_but_strings_kept(self):
        self.assertEqual(normalize("SELECT 1 -- note\n;"), "SELECT 1")
        self.assertIn("'--not a comment'", strip_comments("SELECT '--not a comment' /* x */"))
        self.assertTrue(wrap_limit("SELECT 1;", 10).endswith("LIMIT 11"))


class GuardedExecutionTest(unittest.TestCase):
    db = None

    @classmethod
    def setUpClass(cls):
        cls.db = shared_db()

    def test_ok_query_returns_rows(self):
        r = run_guarded(self.db, "SELECT COUNT(*) FROM orders")
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.rows, [[2400]])

    def test_restricted_column_blocked_everywhere(self):
        for sql in ("SELECT email FROM employees",
                    "SELECT COUNT(*) FROM employees WHERE salary_usd > 100000",
                    "SELECT department, AVG(salary_usd) FROM employees GROUP BY department",
                    "SELECT * FROM employees",
                    "SELECT c.company_name FROM customers c JOIN customer_contacts cc ON cc.customer_id = c.customer_id"):
            r = run_guarded(self.db, sql)
            self.assertEqual((r.status, r.guard), ("blocked", "authz_columns"), sql)

    def test_non_restricted_employee_columns_allowed(self):
        r = run_guarded(self.db, "SELECT department, COUNT(*) FROM employees GROUP BY department")
        self.assertEqual(r.status, "ok")
        self.assertEqual(len(r.rows), 6)

    def test_internal_tables_blocked(self):
        r = run_guarded(self.db, "SELECT name FROM sqlite_master")
        self.assertEqual(r.guard, "authz_columns")

    def test_forced_limit_truncates(self):
        r = run_guarded(self.db, "SELECT * FROM order_items", GuardConfig(row_limit=50))
        self.assertTrue(r.truncated)
        self.assertEqual(len(r.rows), 50)

    def test_timeout_interrupts(self):
        cfg = GuardConfig(timeout_s=0.05, force_limit=False)
        r = run_guarded(self.db, "SELECT COUNT(*) FROM order_items a, order_items b, support_tickets c", cfg)
        self.assertEqual((r.status, r.guard), ("blocked", "timeout"))

    def test_without_allowlist_the_authorizer_still_blocks_writes(self):
        r = run_guarded(self.db, "DELETE FROM orders", GuardConfig(allowlist=False, force_limit=False))
        self.assertEqual(r.status, "blocked")
        self.assertEqual(r.guard, "authz_actions")

    def test_leave_one_out_column_policy_lets_pii_through(self):
        r = run_guarded(self.db, "SELECT email FROM employees", GuardConfig().without("authz_columns"))
        self.assertEqual(r.status, "ok")
        self.assertEqual(len(r.rows), 60)

    def test_function_allowlist(self):
        r = run_guarded(self.db, "SELECT sqlite_version()")
        self.assertEqual(r.guard, "authz_actions")
        self.assertEqual(run_guarded(self.db, "SELECT strftime('%Y', ordered_on), COUNT(*) FROM orders GROUP BY 1")
                         .status, "ok")

    def test_source_file_never_written(self):
        before = self.db.read_bytes()
        cfg = GuardConfig(allowlist=False, authz_actions=False, force_limit=False)  # only read-only left
        r = run_guarded(self.db, "DELETE FROM orders", cfg)
        self.assertEqual(r.status, "blocked")
        self.assertEqual(r.guard, "readonly")
        cfg = GuardConfig(allowlist=False, authz_actions=False, force_limit=False, readonly=False)
        self.assertEqual(run_guarded(self.db, "DELETE FROM orders", cfg).status, "ok")  # ran on a copy
        self.assertEqual(self.db.read_bytes(), before)


class AttackSuiteTest(unittest.TestCase):
    def test_all_guards_stop_every_attack_and_no_guards_stop_none(self):
        from sqlguard.attacks import ATTACKS, run_suite
        cfg = GuardConfig(timeout_s=0.05, harness_wall_s=0.2)
        out = run_suite(shared_db(), cfg, {"wall_cap_s": 0.3, "row_cap": 10001, "unbounded_rows": 1000,
                                            "unbounded_s": 0.1})
        self.assertEqual(out["unsafe_totals"]["all guards"], 0)
        self.assertGreaterEqual(out["unsafe_totals"]["no guards"], len(ATTACKS) - 2)
        self.assertEqual(out["unsafe_totals"]["minus_authz_columns"], 7)
        self.assertEqual(out["unsafe_totals"]["minus_force_limit"], 2)


if __name__ == "__main__":
    unittest.main()
