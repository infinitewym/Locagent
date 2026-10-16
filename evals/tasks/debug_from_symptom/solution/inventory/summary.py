"""库存汇总。"""


def total_value(items):
    """所有商品的 单价 x 数量 之和，保留两位小数。"""
    total = 0
    for item in items:
        total += item["price"] * item["qty"]
    return round(total, 2)
