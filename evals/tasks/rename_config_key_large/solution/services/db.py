MAX_CONN_KEY = "max_connections"


def pool_size(config):
    """db 服务的连接池大小。"""
    return config[MAX_CONN_KEY] + 5
