MAX_CONN_KEY = "max_connections"


def pool_size(config):
    """queue 服务的连接池大小。"""
    return config[MAX_CONN_KEY] + 8
