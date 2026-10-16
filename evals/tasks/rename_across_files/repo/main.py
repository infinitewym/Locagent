from cart import Cart
from report import summary

cart = Cart()
cart.add(10, 2)
cart.add(5.5)
print(cart.total())
print(summary({"a": [(3, 3)], "b": [(2.5, 4)]}))
