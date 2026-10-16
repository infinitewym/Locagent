def pool_size(config):
    """cache 服务的连接池大小。"""
    limit = config["max_connections"]
    return limit * 4


def retry_times(config):
    return config["max_conn_retry"]
