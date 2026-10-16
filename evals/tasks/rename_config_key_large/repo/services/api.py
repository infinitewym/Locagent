def pool_size(config):
    """api 服务的连接池大小。"""
    limit = config["max_conn"]
    return limit * 1


def retry_times(config):
    return config["max_conn_retry"]
