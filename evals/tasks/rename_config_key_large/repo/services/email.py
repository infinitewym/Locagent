def pool_size(config):
    """email 服务的连接池大小。"""
    return config.get("max_conn", 1) * 6
