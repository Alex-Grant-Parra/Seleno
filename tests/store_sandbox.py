"""The store against the real SumUp sandbox: orders are placed through the store's own
pages and paid with SumUp's test cards, including the 3-D Secure step.

    venv/bin/python tests/store_sandbox.py            # about 20 s, needs internet
    venv/bin/python tests/store_sandbox.py --serve    # serve the store to click through by hand

Needs SUMUP_TEST_API_KEY and SUMUP_TEST_MERCHANT_CODE in instance/.env (a sandbox
merchant from https://me.sumup.com/settings/developer?tab=sandboxes). No money moves.
Like the other tests it uses a throwaway database, not instance/Data.db, and the live
key is never loaded.

How a card is paid here: the script hands the card to SumUp's "process checkout" API
(what the hosted page does), then follows the 3-D Secure simulator's auto-submitting
forms that a browser would run. SumUp cannot reach a webhook on 127.0.0.1, so the
script posts the same two-field notice SumUp sends. The server ignores what a notice
says and asks SumUp for the checkout's real status either way.

Scenarios (from https://developer.sumup.com/online-payments/testing/):
    card 4200 0000 0000 0091, any future expiry, any CVV   -> paid
    the same card on an order totalling 11.00              -> declined, then retried
By hand, the hosted page also takes 4200 0000 0000 0042 for a 3-D Secure challenge."""
import html
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PORT = 18095
GOOD_CARD = "4200000000000091"

from dotenv import dotenv_values  # noqa: E402

_env = dotenv_values(os.path.join(os.path.dirname(HERE), "instance", ".env"))
for _key in ("SUMUP_TEST_API_KEY", "SUMUP_TEST_MERCHANT_CODE"):
    os.environ[_key] = (_env.get(_key) or "").strip()
os.environ["SUMUP_MODE"] = "sandbox"
os.environ["STORE_DELIVERY"] = "on"         # the declined case needs the 0.50 delivery to reach 11.00
for _key in ("SUMUP_API_KEY", "SUMUP_MERCHANT_CODE"):       # never the live account
    os.environ.pop(_key, None)

import requests  # noqa: E402

import test_store  # noqa: E402  (registers every blueprint on the harness app)
from harness import app, db, login, make_user  # noqa: E402
from flask import redirect, request, session  # noqa: E402
from app import store_service, sumup_service  # noqa: E402
from models.store import StoreOrder, StoreProduct, StoreShippingZone  # noqa: E402
from models.user import User  # noqa: E402

app.config["APP_DOMAIN"] = ""              # links point back at this machine, not the real site
MAIL = []
store_service._deliver = lambda app_, to, subject, body: MAIL.append((to, subject, body))
ADMIN_ID = None


def seed():
    global ADMIN_ID
    with app.app_context():
        ADMIN_ID = make_user("sandbox_admin", "Administrator").id
        db.session.add_all([
            StoreProduct(slug="tracking-upgrade-kit", name="Tracking upgrade kit (sandbox sample)",
                         summary="Made-up product for testing payments.", price_pence=1150,
                         description="A pretend kit.\nIt exists only in this throwaway test database.",
                         stock=5, max_per_order=3, is_active=True),
            StoreProduct(slug="declined-kit", name="Kit that SumUp declines (sandbox sample)",
                         summary="With delivery the order is 11.00, which the sandbox always refuses.",
                         price_pence=1050, stock=5, is_active=True, sort_order=1),
            StoreShippingZone(label="United Kingdom", price_pence=50, is_active=True),
        ])
        db.session.commit()


def pay_with_card(checkout_id, number=GOOD_CARD):
    """Pay a checkout as SumUp's hosted page would, and wait for SumUp's verdict."""
    creds = sumup_service.resolve_sumup_credentials()
    step = sumup_service._sumup_request("PUT", f"/v0.1/checkouts/{checkout_id}", creds["api_key"], body={
        "payment_type": "card",
        "card": {"name": "Sandbox Tester", "number": number, "expiry_month": "12", "expiry_year": "30", "cvv": "123"},
    })
    next_step = step.get("next_step")
    if next_step:
        web = requests.Session()
        pre = next_step.get("pre_action")
        if pre:
            web.request(pre["method"], pre["url"], data=pre.get("payload") or {}, timeout=30)
        page = web.request(next_step["method"], next_step["url"], data=next_step.get("payload") or {}, timeout=30)
        for _ in range(8):                  # the simulator's auto-submitting forms
            form = re.search(r"<form[^>]*>", page.text, re.I)
            action = form and re.search(r'action="([^"]+)"', form.group(0))
            if not action:
                break
            method = (re.search(r'method="(\w+)"', form.group(0)) or [None, "GET"])[1].upper()
            fields = {html.unescape(n): html.unescape(v)
                      for n, v in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', page.text, re.I)}
            page = web.request(method, html.unescape(action.group(1)), data=fields, timeout=30, allow_redirects=False)
            while page.is_redirect and not page.headers["Location"].startswith("http://localhost"):
                page = web.get(requests.compat.urljoin(page.url, page.headers["Location"]), timeout=30,
                               allow_redirects=False)
            if page.is_redirect:            # on its way back to the store
                break
    for _ in range(15):
        remote = sumup_service.retrieve_checkout(creds["api_key"], checkout_id)
        if remote.get("status") != "PENDING":
            return remote
        time.sleep(1)
    return remote


def run():
    failures = []

    def check(label, ok, detail=""):
        print(f"  {'ok  ' if ok else 'FAIL'} {label}{f' ({detail})' if detail and not ok else ''}")
        if not ok:
            failures.append(label)

    with app.app_context():
        c = app.test_client()
        login(c, db.session.get(User, ADMIN_ID))
        zone = StoreShippingZone.query.one()

        def place(slug):
            product = StoreProduct.query.filter_by(slug=slug).one()
            c.post("/store/basket/add", data={"product_id": product.id, "quantity": 1})
            response = c.post("/store/checkout", data=dict(test_store.DETAILS, zone_id=zone.id))
            order = StoreOrder.query.order_by(StoreOrder.id.desc()).first()
            return product, order, response.headers.get("Location", "")

        def page_heading(order):
            page = c.get(f"/store/order/{order.order_number}?key={order.access_token}").get_data(as_text=True)
            return " ".join(re.findall(r"<h1>(.*?)</h1>", page))

        def refresh(order):
            db.session.expire_all()
            return db.session.get(StoreOrder, order.id)

        print("Paid order")
        kit, order, location = place("tracking-upgrade-kit")
        check("checkout opens SumUp's hosted page", location.startswith("https://checkout.sumup.com/pay/"), location)
        check("order is a test order for £12.00", order.is_test_mode and order.total_pence == 1200)
        remote = pay_with_card(order.checkout_id)
        check("SumUp says PAID", remote.get("status") == "PAID", remote.get("status"))
        check("webhook accepted", c.post("/payments/sumup/webhook", json={
            "event_type": "CHECKOUT_STATUS_CHANGED", "id": order.checkout_id}).status_code == 204)
        order = refresh(order)
        check("order marked paid with SumUp's transaction code",
              order.status == "PAID" and order.transaction_code == remote.get("transaction_code"), order.status)
        check("stock went down by one", db.session.get(StoreProduct, kit.id).stock == 4)
        check("customer emailed once", [m[1] for m in MAIL] == [f"[TEST] Your SELENO order {order.order_number}"],
              str([m[1] for m in MAIL]))
        check("order page says paid", "your order is paid" in page_heading(order))

        print("Declined order")
        kit, order, _ = place("declined-kit")
        check("order total is the declining 11.00", order.total_pence == 1100)
        first_checkout = order.checkout_id
        remote = pay_with_card(order.checkout_id)
        check("SumUp says FAILED", remote.get("status") == "FAILED", remote.get("status"))
        check("order page says it did not go through", "did not go through" in page_heading(order))
        order = refresh(order)
        check("order marked failed, stock untouched", order.status == "FAILED"
              and db.session.get(StoreProduct, kit.id).stock == 5)
        check("no email for a failed payment", len(MAIL) == 1)
        response = c.post(f"/store/order/{order.order_number}/pay", data={"key": order.access_token})
        order = refresh(order)
        check("retry opens a new hosted page", order.checkout_id != first_checkout and
              response.headers.get("Location", "").startswith("https://checkout.sumup.com/pay/"))
        check("retry is attempt 2", order.payment_attempts == 2 and order.status == "PENDING")

    print("PASSED" if not failures else f"FAILED: {len(failures)} check(s)")
    return 1 if failures else 0


@app.route("/sandbox-login")
def sandbox_login():
    session["_user_id"] = str(ADMIN_ID)
    session["_fresh"] = True
    return redirect(request.args.get("next") or "/store")


if __name__ == "__main__":
    if not (os.environ["SUMUP_TEST_API_KEY"] and os.environ["SUMUP_TEST_MERCHANT_CODE"]):
        sys.exit("SUMUP_TEST_API_KEY and SUMUP_TEST_MERCHANT_CODE are not set in instance/.env")
    seed()
    if "--serve" in sys.argv:
        from werkzeug.serving import run_simple
        print(f"Open http://127.0.0.1:{PORT}/sandbox-login (signed in as a test admin). Ctrl-C to stop.")
        run_simple("127.0.0.1", PORT, app, threaded=True)
    else:
        sys.exit(run())
