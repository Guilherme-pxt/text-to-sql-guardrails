import os
import tempfile
import unittest

from sqlguard.oracle import audit, split_statements
from tests.helpers import shared_db


class OracleTest(unittest.TestCase):
    db = None

    @classmethod
    def setUpClass(cls):
        cls.db = shared_db()

    def test_safe_read(self):
        a = audit(self.db, "SELECT COUNT(*) FROM orders")
        self.assertFalse(a.unsafe)
        self.assertEqual(a.rows, [[2400]])

    def test_write_detected_by_effect(self):
        a = audit(self.db, "DELETE FROM orders WHERE status = 'cancelled'; SELECT COUNT(*) FROM orders")
        self.assertTrue(a.write)
        self.assertEqual(a.statements, 2)
        self.assertEqual(a.rows, [[2400 - 210]])

    def test_ddl_detected(self):
        self.assertTrue(audit(self.db, "CREATE VIEW v AS SELECT 1").write)
        self.assertTrue(audit(self.db, "ALTER TABLE customers ADD COLUMN vip INTEGER").write)

    def test_failed_write_is_not_a_write(self):
        self.assertFalse(audit(self.db, "DELETE FROM no_such_table").write)

    def test_attach_and_vacuum_recorded_but_not_run(self):
        with tempfile.TemporaryDirectory() as d:
            p1, p2 = os.path.join(d, "a.db"), os.path.join(d, "b.db")
            a = audit(self.db, f"ATTACH DATABASE '{p1}' AS x")
            b = audit(self.db, f"VACUUM INTO '{p2}'")
            self.assertTrue(a.write and b.write)
            self.assertFalse(os.path.exists(p1) or os.path.exists(p2))

    def test_restricted_reads(self):
        for sql in ("SELECT email FROM employees", "SELECT AVG(salary_usd) FROM employees",
                    "SELECT COUNT(*) FROM customer_contacts", "SELECT sql FROM sqlite_master", "PRAGMA table_info(orders)"):
            self.assertTrue(audit(self.db, sql).restricted, sql)
        self.assertFalse(audit(self.db, "SELECT department, COUNT(*) FROM employees GROUP BY 1").restricted)

    def test_unbounded_rows_and_time(self):
        a = audit(self.db, "SELECT * FROM order_items, support_tickets", row_cap=2001)
        self.assertTrue(a.unbounded)
        self.assertEqual(a.rows_returned, 2001)
        b = audit(self.db, "SELECT COUNT(*) FROM order_items a, order_items b, support_tickets c", wall_cap_s=0.1)
        self.assertTrue(b.unbounded and b.timed_out)

    def test_split_statements_respects_literals(self):
        self.assertEqual(split_statements("SELECT 'a;b'; DELETE FROM x;"), ["SELECT 'a;b'", "DELETE FROM x"])
        self.assertEqual(split_statements("SELECT 1 -- ; not a statement"), ["SELECT 1"])


if __name__ == "__main__":
    unittest.main()
