"""简单统计函数。"""


def mean(values):
    """返回平均值；空列表返回 0。"""
    if not values:
        return 0
    return sum(values) / len(values)


def median(values):
    """返回中位数；偶数个元素时取中间两个数的平均值。"""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
