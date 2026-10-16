def pool_size(config):
    """search 服务的连接池大小。"""
    return config.get("max_connections", 1) * 9
