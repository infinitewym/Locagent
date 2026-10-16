"""待办事项存储：保存在一个 JSON 文件里。"""

import json
from pathlib import Path

PRIORITIES = ("high", "medium", "low")


class TodoStore:
    def __init__(self, path):
        self.path = Path(path)
        self.items = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []

    def add(self, title, priority="medium"):
        if priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}")
        item = {"id": len(self.items) + 1, "title": title, "done": False, "priority": priority}
        self.items.append(item)
        self.save()
        return item

    def done(self, item_id):
        for item in self.items:
            if item["id"] == item_id:
                item["done"] = True
                self.save()
                return item
        raise KeyError(item_id)

    def pending(self):
        items = [item for item in self.items if not item["done"]]
        return sorted(items, key=lambda item: (PRIORITIES.index(item.get("priority", "medium")), item["id"]))

    def save(self):
        self.path.write_text(json.dumps(self.items, ensure_ascii=False, indent=2), encoding="utf-8")
