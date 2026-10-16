import unittest

from config_loader import load_config
from services import api, auth, cache


class ConfigTest(unittest.TestCase):
    def test_load(self):
        config = load_config()
        self.assertEqual(config["max_conn"], 8)
        self.assertEqual(config["max_conn_retry"], 3)

    def test_pool_sizes(self):
        config = {"max_conn": 2, "max_conn_retry": 1}
        self.assertEqual(api.pool_size(config), 2)
        self.assertEqual(auth.pool_size(config), 4)
        self.assertEqual(cache.pool_size(config), 8)


if __name__ == "__main__":
    unittest.main()
