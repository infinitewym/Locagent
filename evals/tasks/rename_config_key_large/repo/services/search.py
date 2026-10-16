def pool_size(config):
    """search 服务的连接池大小。"""
    return config.get("max_conn", 1) * 9
