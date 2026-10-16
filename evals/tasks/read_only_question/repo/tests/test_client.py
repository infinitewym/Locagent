from app.client import Client


def test_custom_timeout():
    assert Client("http://example.com", timeout=5).timeout == 5
