import tempfile
import unittest
from pathlib import Path

from todo.store import TodoStore


class TodoStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "todo.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_add_and_reload(self):
        TodoStore(self.path).add("buy milk")
        self.assertEqual(TodoStore(self.path).items[0]["title"], "buy milk")

    def test_done(self):
        store = TodoStore(self.path)
        item = store.add("write report")
        store.done(item["id"])
        self.assertEqual(store.pending(), [])

    def test_done_unknown_id(self):
        with self.assertRaises(KeyError):
            TodoStore(self.path).done(99)

    def test_priority_order(self):
        store = TodoStore(self.path)
        store.add("a", "low")
        store.add("b", "high")
        store.add("c")
        self.assertEqual([item["title"] for item in store.pending()], ["b", "c", "a"])

    def test_invalid_priority(self):
        with self.assertRaises(ValueError):
            TodoStore(self.path).add("x", "urgent")


if __name__ == "__main__":
    unittest.main()
