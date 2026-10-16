"""价格计算。"""


def compute_total(items, tax_rate=0.1):
    """items 是 (单价, 数量) 列表，返回含税总价，保留两位小数。"""
    subtotal = sum(price * qty for price, qty in items)
    return round(subtotal * (1 + tax_rate), 2)
