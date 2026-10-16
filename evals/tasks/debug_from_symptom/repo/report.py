from inventory.parser import load_items
from inventory.summary import total_value

items = load_items("data/items.csv")
print(f"商品数: {len(items)}")
print(f"总价: {total_value(items):.2f}")
