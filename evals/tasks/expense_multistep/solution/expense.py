def add_record(records, name, amount):
    if amount <= 0:
        raise ValueError("amount must be positive")
    records.append({"name": name, "amount": amount})
    return records


def total(records):
    return sum(record["amount"] for record in records)


def by_name(records, name):
    return sum(record["amount"] for record in records if record["name"] == name)
