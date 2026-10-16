"""读取库存数据。"""

import csv


def parse_price(text):
    """把价格文本转成数字，支持千分位逗号，例如 "1,200.50" -> 1200.5。"""
    return float(text.replace(",", ""))


def load_items(path):
    items = []
    with open(path, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            items.append({"name": row["name"], "price": parse_price(row["price"]), "qty": int(row["qty"])})
    return items
