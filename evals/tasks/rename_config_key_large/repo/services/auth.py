MAX_CONN_KEY = "max_conn"


def pool_size(config):
    """auth 服务的连接池大小。"""
    return config[MAX_CONN_KEY] + 2
