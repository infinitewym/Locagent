from .settings.defaults import REQUEST_TIMEOUT, RETRY_TIMES


class Client:
    def __init__(self, base_url, timeout=REQUEST_TIMEOUT):
        self.base_url = base_url
        self.timeout = timeout
        self.retries = RETRY_TIMES
