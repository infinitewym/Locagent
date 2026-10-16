from importlib import import_module

from config_loader import load_config

SERVICES = ["api", "auth", "billing", "cache", "db", "email", "export", "queue", "search", "storage"]

config = load_config()
total = 0
for name in SERVICES:
    size = import_module(f"services.{name}").pool_size(config)
    total += size
    print(f"{name}: {size}")
print(f"total: {total}, retry: {config['max_conn_retry']}")
