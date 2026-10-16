import unittest

from expense import add_record, by_name, total


class ExpenseTest(unittest.TestCase):
    def test_add_record(self):
        records = []
        self.assertIs(add_record(records, "lunch", 20), records)
        self.assertEqual(len(records), 1)

    def test_invalid_amount(self):
        with self.assertRaises(ValueError):
            add_record([], "x", 0)
        with self.assertRaises(ValueError):
            add_record([], "x", -1)

    def test_total_and_by_name(self):
        records = []
        add_record(records, "lunch", 20)
        add_record(records, "taxi", 15)
        add_record(records, "lunch", 10)
        self.assertEqual(total(records), 45)
        self.assertEqual(by_name(records, "lunch"), 30)


if __name__ == "__main__":
    unittest.main()
