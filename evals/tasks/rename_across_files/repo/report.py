import pricing


def summary(orders):
    """orders 是 {名称: 商品列表}，不含税。"""
    lines = []
    for name, items in orders.items():
        lines.append(f"{name}: {pricing.calc_total(items, tax_rate=0.0)}")
    return "\n".join(lines)
