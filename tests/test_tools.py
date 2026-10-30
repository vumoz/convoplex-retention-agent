import json

from convoplex.purchase import confirm_purchase
from convoplex.tools import check_stock, create_offer, get_delivery_options, get_price, place_order


def _confirm(shop, *, qty, offer_id=None, product_id=1, customer_id=1):
    confirm_purchase(
        shop,
        customer_id=customer_id,
        retailer_id=1,
        product_id=product_id,
        qty=qty,
        offer_id=offer_id,
    )


def test_price_and_stock_match_the_shelf(shop):
    price = get_price(shop, product="Mama Gold Rice 50kg", retailer_id=1)
    assert price["ok"] is True
    assert price["price_ngn"] == 78000
    assert price["currency"] == "NGN"
    assert price["in_stock"] is True
    stock = check_stock(shop, product="rice 50kg", retailer_id=1)
    assert stock["ok"] is True
    assert stock["matches"][0]["stock_qty"] == 20


def test_offer_clamps_policy_and_order_debits_stock(shop):
    rejected = create_offer(shop, customer_id=1, product_id=1, discount_percent=40)
    assert rejected["ok"] is False
    offer = create_offer(shop, customer_id=1, product_id=1, discount_percent=10)
    assert offer["ok"] is True
    assert offer["list_price_ngn"] == 78000
    assert offer["offer_price_ngn"] == 70200
    shop.execute("UPDATE offers SET status = 'sent' WHERE id = ?", (offer["offer_id"],))
    _confirm(shop, qty=2, offer_id=offer["offer_id"])
    order = place_order(shop, customer_id=1, product_id=1, qty=2, offer_id=offer["offer_id"])
    assert order["ok"] is True
    assert order["total_ngn"] == 140400
    assert order["channel"] == "whatsapp_sim"
    left = shop.execute("SELECT stock_qty FROM products WHERE id = 1").fetchone()["stock_qty"]
    assert left == 18
    status = shop.execute("SELECT status FROM offers WHERE id = ?", (offer["offer_id"],)).fetchone()["status"]
    assert status == "accepted"
    again = place_order(shop, customer_id=1, product_id=1, qty=1, offer_id=offer["offer_id"])
    assert again["ok"] is False


def test_out_of_stock_and_cross_shop_product(shop):
    empty = create_offer(shop, customer_id=1, product="spaghetti", discount_percent=10)
    assert empty["ok"] is False
    shop.execute(
        """
        INSERT INTO retailers (name, city, area, category_focus, phone)
        VALUES ('Other Shop', 'Abuja', 'Garki', 'provisions', '+2348000001999')
        """
    )
    shop.execute(
        """
        INSERT INTO products (retailer_id, sku, name, category, unit, price_ngn, stock_qty)
        VALUES (2, 'RICE-50', 'Mama Gold Rice 50kg', 'provisions', 'bag', 80000, 5)
        """
    )
    foreign = shop.execute("SELECT id FROM products WHERE retailer_id = 2").fetchone()["id"]
    blocked = create_offer(shop, customer_id=1, product_id=foreign, discount_percent=10)
    assert blocked["ok"] is False


def test_place_order_caps_at_stock_and_max_per_order(shop):
    _confirm(shop, qty=100)
    capped = place_order(shop, customer_id=1, product_id=1, qty=100)
    assert capped["ok"] is True
    assert capped["requested_qty"] == 100
    assert capped["qty"] == 10
    assert capped["capped"] is True
    left = shop.execute("SELECT stock_qty FROM products WHERE id = 1").fetchone()["stock_qty"]
    assert left == 10

    shop.execute("UPDATE products SET stock_qty = 20 WHERE id = 1")
    _confirm(shop, qty=15)
    fifteen = place_order(shop, customer_id=1, product_id=1, qty=15)
    assert fifteen["qty"] == 10
    assert fifteen["capped"] is True

    shop.execute("UPDATE products SET stock_qty = 3 WHERE id = 1")
    _confirm(shop, qty=8)
    thin = place_order(shop, customer_id=1, product_id=1, qty=8)
    assert thin["ok"] is True
    assert thin["qty"] == 3
    assert thin["capped"] is True
    assert shop.execute("SELECT stock_qty FROM products WHERE id = 1").fetchone()["stock_qty"] == 0

    empty = place_order(shop, customer_id=1, product_id=1, qty=1)
    assert empty["ok"] is False

    shop.execute("UPDATE products SET stock_qty = 20 WHERE id = 1")
    _confirm(shop, qty=2)
    exact = place_order(shop, customer_id=1, product_id=1, qty=2)
    assert exact["ok"] is True
    assert exact["qty"] == 2
    assert exact["capped"] is False
    assert shop.execute("SELECT stock_qty FROM products WHERE id = 1").fetchone()["stock_qty"] == 18


def test_declined_offer_is_charged_when_place_order_omits_offer_id(shop):
    offer = create_offer(shop, customer_id=1, product_id=1, discount_percent=10)
    shop.execute("UPDATE offers SET status = 'declined' WHERE id = ?", (offer["offer_id"],))
    _confirm(shop, qty=2, offer_id=offer["offer_id"])
    order = place_order(shop, customer_id=1, product_id=1, qty=2)
    assert order["ok"] is True
    assert order["offer_id"] == offer["offer_id"]
    assert order["unit_price_ngn"] == 70200
    assert order["qty"] == 2
    assert order["total_ngn"] == 140400
    status = shop.execute("SELECT status FROM offers WHERE id = ?", (offer["offer_id"],)).fetchone()["status"]
    assert status == "accepted"


def test_delivery_options_are_synthetic_windows(shop):
    shop.execute(
        """
        INSERT INTO customers (retailer_id, full_name, phone, city, area, language, archetype)
        VALUES (1, 'Amaka Eze', '+2348030001199', 'Abuja', 'Wuse', 'en', 'lapsed')
        """
    )
    same = get_delivery_options(shop, retailer_id=1, customer_id=1)
    assert same["ok"] is True
    assert same["synthetic"] is True
    methods = {item["method"]: item for item in same["options"]}
    assert methods["pickup"]["window"] == "same day"
    assert methods["pickup"]["fee_ngn"] == 0
    assert methods["delivery"]["available"] is True
    assert methods["delivery"]["window"] == "1 to 2 days"
    assert methods["delivery"]["fee_ngn"] == 2500
    assert "friday" not in json.dumps(same).lower()
    away = get_delivery_options(shop, retailer_id=1, customer_id=4)
    away_delivery = next(item for item in away["options"] if item["method"] == "delivery")
    assert away_delivery["available"] is False
