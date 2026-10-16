def pool_size(config):
    """export 服务的连接池大小。"""
    limit = config["max_conn"]
    return limit * 7


def retry_times(config):
    return config["max_conn_retry"]
