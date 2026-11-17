"""The store: baskets, checkout, and when an order counts as paid. SumUp is a fake
here (FakeSumUp below), so nothing leaves the machine; tests/store_sandbox.py is the
one that talks to the real SumUp sandbox."""
import importlib
import os
import unittest

from harness import REPO, app, db, make_user, login

from flask_wtf import CSRFProtect

app.config["WTF_CSRF_ENABLED"] = False
if "csrf" not in app.extensions:
    CSRFProtect(app)   # for csrf_token() in the templates

# base.html links to pages all over the site, so every blueprint has to exist
for _name in sorted(os.listdir(os.path.join(REPO, "controllers"))):
    if _name.endswith(".py") and _name != "__init__.py":
        _module = importlib.import_module(f"controllers.{_name[:-3]}")
        _bp = getattr(_module, f"{_name[:-3]}_bp", None)
        if _bp is not None and _bp.name not in app.blueprints:
            app.register_blueprint(_bp)
from models.user import user_bp  # noqa: E402
if user_bp.name not in app.blueprints:
    app.register_blueprint(user_bp)
import models.trusted_device, models.client_release, models.contact, models.contact_thread  # noqa: E402,F401

import controllers.payments as payments_mod  # noqa: E402
import controllers.store as store_mod  # noqa: E402
from app import page_toggles, store_service, sumup_service  # noqa: E402
from app.sumup_service import SumUpAPIError  # noqa: E402
from models.payment import SumupCheckout, SumupEvent, SumupTransaction  # noqa: E402
from models.store import StoreOrder, StoreOrderItem, StoreProduct, StoreShippingZone, format_pence  # noqa: E402

with app.app_context():
    db.create_all()


class FakeSumUp:
    """Enough of SumUp's checkout API to stand in for it: create, read, close."""

    def __init__(self):
        self.checkouts = {}
        self.created = []
        self.reads = 0
        self.down = False

    def creds(self, is_test_mode=None):
        if is_test_mode is None:
            is_test_mode = sumup_service.current_sumup_mode() != 'live'
        return {'api_key': 'key', 'merchant_code': 'MTEST', 'is_test_mode': bool(is_test_mode)}

    def create(self, api_key, payload):
        if self.down:
            raise SumUpAPIError('SumUp API request failed.', status_code=503)
        checkout_id = f'chk-{len(self.checkouts) + 1:04d}-{payload["checkout_reference"]}'
        remote = dict(payload, id=checkout_id, status='PENDING', transactions=[],
                      hosted_checkout_url=f'https://checkout.sumup.test/pay/{checkout_id}')
        self.checkouts[checkout_id] = remote
        self.created.append(payload)
        return dict(remote)

    def retrieve(self, api_key, checkout_id):
        self.reads += 1
        if self.down:
            raise SumUpAPIError('SumUp API request failed.', status_code=503)
        if checkout_id not in self.checkouts:
            raise SumUpAPIError('SumUp API request failed.', status_code=404)
        return dict(self.checkouts[checkout_id])

    def deactivate(self, api_key, checkout_id):
        remote = self.checkouts[checkout_id]
        if remote['status'] == 'PAID':
            raise SumUpAPIError('SumUp API request failed.', status_code=409)
        remote['status'] = 'EXPIRED'
        return dict(remote)

    def settle(self, checkout_id, status='PAID', amount=None):
        """What the customer did on SumUp's page."""
        remote = self.checkouts[checkout_id]
        remote['status'] = status
        if amount is not None:
            remote['amount'] = amount
        if status == 'PAID':
            remote['transaction_code'] = 'TTEST' + checkout_id[4:8]
            remote['transaction_id'] = 'tx-' + checkout_id
            remote['transactions'] = [{'id': 'tx-' + checkout_id, 'transaction_code': remote['transaction_code'],
                                       'status': 'SUCCESSFUL', 'amount': remote['amount'], 'currency': 'GBP'}]


DETAILS = {
    'email': 'buyer@example.test', 'full_name': 'Bea Buyer', 'address_line1': '1 Orbit Way',
    'city': 'Leeds', 'postcode': 'LS1 1AA', 'country': 'United Kingdom', 'accept_terms': '1',
}


class Store(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        cls.admin = make_user("store_admin", "Administrator")
        cls.member = make_user("store_member")
        cls.other = make_user("store_other")

    @classmethod
    def tearDownClass(cls):
        os.environ.pop('SUMUP_MODE', None)
        os.environ.pop('STORE_DELIVERY', None)
        cls.ctx.pop()

    def setUp(self):
        self.sumup = FakeSumUp()
        self.mail = []
        self._saved = []
        for module, name, value in (
            (store_service, 'create_checkout', self.sumup.create),
            (store_service, 'retrieve_checkout', self.sumup.retrieve),
            (store_service, 'deactivate_checkout', self.sumup.deactivate),
            (store_service, 'resolve_sumup_credentials', self.sumup.creds),
            (payments_mod, 'retrieve_checkout', self.sumup.retrieve),
            (payments_mod, 'resolve_sumup_credentials', self.sumup.creds),
            (store_service, '_deliver', lambda app_, to, subject, body: self.mail.append((to, subject, body))),
        ):
            self._saved.append((module, name, getattr(module, name)))
            setattr(module, name, value)
        store_service._recent_orders.clear()
        store_mod._nav['at'] = 0.0
        os.environ['SUMUP_MODE'] = 'live'
        os.environ['STORE_DELIVERY'] = 'on'
        self.kit = StoreProduct(slug='tracking-kit', name='Tracking Kit', summary='Motors and a board',
                                price_pence=14950, stock=3, max_per_order=2, is_active=True)
        self.cable = StoreProduct(slug='cable', name='Cable', price_pence=799, stock=None, is_active=True)
        self.uk = StoreShippingZone(label='United Kingdom', price_pence=450, is_active=True)
        db.session.add_all([self.kit, self.cable, self.uk])
        db.session.commit()

    def tearDown(self):
        for module, name, value in self._saved:
            setattr(module, name, value)
        db.session.rollback()
        models = (SumupEvent, SumupTransaction, SumupCheckout, StoreOrderItem, StoreOrder,
                  StoreShippingZone, StoreProduct)
        for model in models:
            model.query.delete()
        db.session.commit()
        for row in list(db.session.identity_map.values()):
            if isinstance(row, models):      # ids are reused by the next test
                db.session.expunge(row)

    # -- helpers --
    def client(self, user=None):
        c = app.test_client()
        if user is not None:
            login(c, user)
        return c

    def add(self, c, product, quantity=1):
        return c.post('/store/basket/add', data={'product_id': product.id, 'quantity': quantity})

    def checkout(self, c, **overrides):
        data = dict(DETAILS, zone_id=self.uk.id)
        data.update(overrides)
        return c.post('/store/checkout', data={k: v for k, v in data.items() if v is not None})

    def place(self, c=None, quantity=1):
        """An order for `quantity` kits, up to the redirect to SumUp's page."""
        c = c or self.client()
        self.add(c, self.kit, quantity)
        response = self.checkout(c)
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:300])
        order = StoreOrder.query.order_by(StoreOrder.id.desc()).first()
        self.assertEqual(response.headers['Location'], f'https://checkout.sumup.test/pay/{order.checkout_id}')
        return c, order

    def webhook(self, checkout_id):
        return self.client().post('/payments/sumup/webhook',
                                  json={'event_type': 'CHECKOUT_STATUS_CHANGED', 'id': checkout_id})

    def order_page(self, c, order, key=True):
        return c.get(f'/store/order/{order.order_number}' + (f'?key={order.access_token}' if key else ''))

    def fresh(self, order):
        db.session.expire_all()
        return db.session.get(StoreOrder, order.id)

    # -- the shop window --
    def test_empty_store_says_kits_are_coming(self):
        StoreProduct.query.delete()
        db.session.commit()
        page = self.client().get('/store').get_data(as_text=True)
        self.assertIn('Upgrade kits are on the way', page)
        self.assertNotIn('href="/store">Store</a>', page)       # no menu link with nothing to sell

    def test_products_are_listed_and_hidden_ones_are_not(self):
        self.cable.is_active = False
        db.session.commit()
        c = self.client()
        page = c.get('/store').get_data(as_text=True)
        self.assertIn('Tracking Kit', page)
        self.assertIn('£149.50', page)
        self.assertNotIn('Cable', page)
        self.assertIn('href="/store">Store</a>', page)
        self.assertEqual(c.get('/store/product/cable').status_code, 404)
        self.assertEqual(self.client(self.admin).get('/store/product/cable').status_code, 200)

    def test_money_is_written_in_pounds_and_pence(self):
        self.assertEqual(format_pence(14950), '£149.50')
        self.assertEqual(format_pence(5), '£0.05')
        self.assertEqual(format_pence(123456789), '£1,234,567.89')

    # -- baskets --
    def test_basket_respects_stock_and_the_per_order_limit(self):
        c = self.client()
        self.add(c, self.kit, 5)                      # most per order is 2
        page = c.get('/store/basket').get_data(as_text=True)
        self.assertIn('value="2"', page)
        self.assertIn('£299.00', page)
        self.kit.stock = 1                            # someone else bought one meanwhile
        db.session.commit()
        page = c.get('/store/basket').get_data(as_text=True)
        self.assertIn('value="1"', page)
        self.assertIn('no longer available in that quantity', page)

    def test_sold_out_and_hidden_products_cannot_be_added(self):
        self.kit.stock = 0
        db.session.commit()
        c = self.client()
        self.add(c, self.kit)
        self.assertIn('Your basket is empty', c.get('/store/basket').get_data(as_text=True))
        self.cable.is_active = False
        db.session.commit()
        self.assertEqual(self.add(c, self.cable).status_code, 404)

    def test_basket_update_and_remove(self):
        c = self.client()
        self.add(c, self.kit)
        self.add(c, self.cable, 2)
        c.post('/store/basket/update', data={f'quantity_{self.kit.id}': '2', f'quantity_{self.cable.id}': '2',
                                           'remove': str(self.cable.id)})
        page = c.get('/store/basket').get_data(as_text=True)
        self.assertIn('£299.00', page)
        self.assertNotIn('/store/product/cable', page)

    # -- who may order --
    def test_sandbox_mode_lets_only_admins_check_out(self):
        os.environ['SUMUP_MODE'] = 'sandbox'
        for user in (None, self.member):
            c = self.client(user)
            self.add(c, self.kit)
            self.assertIn('Ordering is not open yet', c.get('/store/basket').get_data(as_text=True))
            self.assertEqual(self.checkout(c).headers['Location'], '/store/basket')
        self.assertEqual(StoreOrder.query.count(), 0)

        c, order = self.place(self.client(self.admin))
        self.assertTrue(order.is_test_mode)
        self.sumup.settle(order.checkout_id)
        page = self.order_page(c, order).get_data(as_text=True)
        self.assertIn('Test order', page)
        self.assertIn('your order is paid', page)
        self.assertTrue(self.mail[0][1].startswith('[TEST] '))

    def test_payments_switched_off_closes_the_checkout(self):
        saved = sumup_service.SUMUP_FEATURE_ENABLED
        sumup_service.SUMUP_FEATURE_ENABLED = False
        try:
            c = self.client(self.admin)
            self.add(c, self.kit)
            self.assertEqual(self.checkout(c).headers['Location'], '/store/basket')
        finally:
            sumup_service.SUMUP_FEATURE_ENABLED = saved

    # -- checkout --
    def test_guest_order_is_priced_by_the_server(self):
        c = self.client()
        self.add(c, self.kit, 2)
        self.add(c, self.cable)
        response = self.checkout(c, amount='0.01', total_pence='1', price='0.01')    # ignored
        self.assertEqual(response.status_code, 302)
        order = StoreOrder.query.one()
        self.assertEqual((order.subtotal_pence, order.shipping_pence, order.total_pence), (30699, 450, 31149))
        self.assertEqual(order.status, 'PENDING')
        self.assertFalse(order.is_test_mode)
        self.assertIsNone(order.user_id)
        sent = self.sumup.created[0]
        self.assertEqual(sent['amount'], 311.49)
        self.assertEqual(sent['currency'], 'GBP')
        self.assertEqual(sent['checkout_reference'], f'{order.order_number}-1')
        self.assertEqual(sent['redirect_url'],
                         f'https://example.test/store/order/{order.order_number}?key={order.access_token}')
        self.assertEqual(sent['return_url'], 'https://example.test/payments/sumup/webhook')
        self.assertEqual(sent['hosted_checkout'], {'enabled': True})
        self.assertIn('Your basket is empty', c.get('/store/basket').get_data(as_text=True))

    def test_personal_details_are_encrypted_in_the_database(self):
        _, order = self.place()
        raw = db.session.execute(db.text('SELECT email, full_name, address_line1, postcode FROM store_orders')).one()
        for value, plain in zip(raw, ('buyer@example.test', 'Bea Buyer', '1 Orbit Way', 'LS1 1AA')):
            self.assertNotIn(plain, value)
        self.assertEqual(order.email, 'buyer@example.test')

    def test_missing_details_are_asked_for_again(self):
        c = self.client()
        self.add(c, self.kit)
        for override in ({'email': 'nope'}, {'postcode': ''}, {'accept_terms': None}, {'zone_id': 9999},
                         {'full_name': 'x' * 121}):
            response = self.checkout(c, **override)
            self.assertEqual(response.status_code, 200, override)
        self.assertEqual(StoreOrder.query.count(), 0)
        self.assertEqual(self.sumup.created, [])
        self.assertIn('value="Bea Buyer"', self.checkout(c, email='').get_data(as_text=True))   # nothing retyped

    def test_checkout_needs_a_basket_and_a_delivery_option(self):
        c = self.client()
        self.assertEqual(c.get('/store/checkout').headers['Location'], '/store')
        self.add(c, self.kit)
        self.uk.is_active = False
        db.session.commit()
        self.assertIn('Delivery is not set up yet', c.get('/store/checkout').get_data(as_text=True))
        self.assertEqual(self.checkout(c).status_code, 200)
        self.assertEqual(StoreOrder.query.count(), 0)

    def test_with_delivery_switched_off_orders_need_no_delivery_option(self):
        os.environ['STORE_DELIVERY'] = 'off'
        self.uk.is_active = False             # no delivery option offered at all
        db.session.commit()
        c = self.client()
        self.add(c, self.kit)
        self.assertIn('No charge', c.get('/store/basket').get_data(as_text=True))
        page = c.get('/store/checkout').get_data(as_text=True)
        self.assertNotIn('Delivery is not set up yet', page)
        self.assertNotIn('type="radio" name="zone_id"', page)
        self.assertIn('Delivery address', page)
        response = self.checkout(c, zone_id=None)
        self.assertEqual(response.status_code, 302)
        order = StoreOrder.query.one()
        self.assertEqual((order.shipping_pence, order.total_pence, order.shipping_label), (0, 14950, 'not charged'))
        self.assertEqual(self.sumup.created[0]['amount'], 149.50)
        self.assertEqual(order.postcode, 'LS1 1AA')

    def test_signed_in_customer_sees_their_email_and_owns_the_order(self):
        c = self.client(self.member)
        self.add(c, self.kit)
        self.assertIn('value="store_member@example.test"', c.get('/store/checkout').get_data(as_text=True))
        self.checkout(c)
        order = StoreOrder.query.one()
        self.assertEqual(order.user_id, self.member.id)
        self.assertEqual(self.order_page(c, order, key=False).status_code, 200)

    def test_orders_from_one_address_are_rate_limited(self):
        c = self.client()
        for _ in range(store_service.ORDERS_PER_WINDOW):
            self.add(c, self.cable)
            self.assertEqual(self.checkout(c).status_code, 302)
        self.add(c, self.cable)
        response = self.checkout(c)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Too many orders', response.get_data(as_text=True))

    def test_sumup_being_down_keeps_the_order_for_later(self):
        self.sumup.down = True
        c = self.client()
        self.add(c, self.kit)
        response = self.checkout(c)
        order = StoreOrder.query.one()
        self.assertIn(f'/store/order/{order.order_number}?key=', response.headers['Location'])
        self.assertIn('could not be opened', c.get(response.headers['Location']).get_data(as_text=True))
        self.sumup.down = False
        response = c.post(f'/store/order/{order.order_number}/pay', data={'key': order.access_token})
        self.assertTrue(response.headers['Location'].startswith('https://checkout.sumup.test/pay/'))

    # -- getting paid --
    def test_returning_without_paying_leaves_the_order_waiting(self):
        c, order = self.place()
        page = self.order_page(c, order).get_data(as_text=True)
        self.assertIn('Waiting for payment', page)
        self.assertIn('Pay now', page)
        self.assertEqual(self.fresh(order).status, 'PENDING')
        self.assertEqual(self.mail, [])
        self.assertEqual(db.session.get(StoreProduct, self.kit.id).stock, 3)

    def test_paying_marks_the_order_paid_once(self):
        c, order = self.place(quantity=2)
        self.sumup.settle(order.checkout_id)
        self.assertEqual(self.webhook(order.checkout_id).status_code, 204)
        order = self.fresh(order)
        self.assertEqual(order.status, 'PAID')
        self.assertIsNotNone(order.paid_at)
        self.assertTrue(order.transaction_code.startswith('TTEST'))
        self.assertEqual(db.session.get(StoreProduct, self.kit.id).stock, 1)
        self.assertEqual([m[0] for m in self.mail], [['buyer@example.test']])   # no shop address in the tests
        self.assertIn(order.order_number, self.mail[0][1])
        self.assertIn('2 x Tracking Kit', self.mail[0][2])
        self.assertIn('£303.50', self.mail[0][2])

        # SumUp retries, and the customer lands back on the page: nothing happens twice
        self.webhook(order.checkout_id)
        page = self.order_page(c, order).get_data(as_text=True)
        self.assertIn('your order is paid', page)
        self.assertNotIn('Pay now', page)
        self.assertEqual(db.session.get(StoreProduct, self.kit.id).stock, 1)
        self.assertEqual(len(self.mail), 1)

    def test_the_return_page_alone_is_enough_when_the_webhook_never_arrives(self):
        c, order = self.place()
        self.sumup.settle(order.checkout_id)
        self.assertIn('your order is paid', self.order_page(c, order).get_data(as_text=True))
        self.assertEqual(self.fresh(order).status, 'PAID')
        self.assertEqual(len(self.mail), 1)

    def test_shop_is_told_about_a_paid_order(self):
        app.config['MAIL_SUPPORT_SENDER'] = 'shop@example.test'
        try:
            _, order = self.place()
            self.sumup.settle(order.checkout_id)
            self.webhook(order.checkout_id)
        finally:
            app.config.pop('MAIL_SUPPORT_SENDER')
        self.assertEqual([m[0] for m in self.mail], [['buyer@example.test'], ['shop@example.test']])
        self.assertIn('1 Orbit Way', self.mail[1][2])

    def test_a_webhook_claiming_paid_is_not_believed(self):
        _, order = self.place()
        # The body can say anything; SumUp itself still says PENDING
        response = self.client().post('/payments/sumup/webhook', json={
            'event_type': 'CHECKOUT_STATUS_CHANGED', 'id': order.checkout_id, 'status': 'PAID'})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.fresh(order).status, 'PENDING')

    def test_webhook_for_a_checkout_we_never_made_is_ignored_without_calling_sumup(self):
        self.assertEqual(self.webhook('not-ours').status_code, 204)
        self.assertEqual(self.sumup.reads, 0)

    def test_webhook_answers_500_when_sumup_cannot_be_reached_so_it_is_retried(self):
        _, order = self.place()
        self.sumup.settle(order.checkout_id)
        self.sumup.down = True
        self.assertEqual(self.webhook(order.checkout_id).status_code, 500)
        self.assertEqual(self.fresh(order).status, 'PENDING')
        self.sumup.down = False
        self.assertEqual(self.webhook(order.checkout_id).status_code, 204)
        self.assertEqual(self.fresh(order).status, 'PAID')

    def test_a_payment_for_the_wrong_amount_does_not_pay_the_order(self):
        _, order = self.place()
        self.sumup.settle(order.checkout_id, amount=1.00)
        self.webhook(order.checkout_id)
        self.webhook(order.checkout_id)
        order = self.fresh(order)
        self.assertEqual(order.status, 'PENDING')
        self.assertEqual(order.admin_note.count('does not match this order'), 1)
        self.assertEqual(self.mail, [])
        self.assertEqual(db.session.get(StoreProduct, self.kit.id).stock, 3)

    def test_failed_payment_can_be_tried_again(self):
        c, order = self.place()
        first = order.checkout_id
        self.sumup.settle(first, 'FAILED')
        page = self.order_page(c, order).get_data(as_text=True)
        self.assertIn('did not go through', page)
        self.assertIn('Try the payment again', page)
        self.assertEqual(self.fresh(order).status, 'FAILED')

        response = c.post(f'/store/order/{order.order_number}/pay', data={'key': order.access_token})
        order = self.fresh(order)
        self.assertNotEqual(order.checkout_id, first)
        self.assertEqual(response.headers['Location'], f'https://checkout.sumup.test/pay/{order.checkout_id}')
        self.assertEqual(self.sumup.created[-1]['checkout_reference'], f'{order.order_number}-2')
        self.assertEqual(self.sumup.checkouts[first]['status'], 'EXPIRED')      # the first page is closed
        self.assertEqual(order.status, 'PENDING')

        self.sumup.settle(order.checkout_id)
        self.webhook(order.checkout_id)
        self.assertEqual(self.fresh(order).status, 'PAID')

    def test_retrying_an_order_that_was_paid_meanwhile_does_not_charge_twice(self):
        c, order = self.place()
        self.sumup.settle(order.checkout_id)       # paid in another tab; no webhook yet
        response = c.post(f'/store/order/{order.order_number}/pay', data={'key': order.access_token})
        self.assertIn(f'/store/order/{order.order_number}', response.headers['Location'])
        self.assertEqual(len(self.sumup.created), 1)
        self.assertEqual(self.fresh(order).status, 'PAID')

    def test_last_one_sold_twice_is_flagged(self):
        self.kit.stock = 1
        db.session.commit()
        _, first = self.place()
        _, second = self.place()
        for order in (first, second):
            self.sumup.settle(order.checkout_id)
            self.webhook(order.checkout_id)
        self.assertEqual(db.session.get(StoreProduct, self.kit.id).stock, 0)
        self.assertIsNone(self.fresh(first).admin_note)
        self.assertIn('Oversold', self.fresh(second).admin_note)

    def test_changing_a_price_later_does_not_change_an_order(self):
        _, order = self.place()
        self.kit.price_pence = 99999
        self.kit.name = 'Renamed'
        db.session.commit()
        order = self.fresh(order)
        self.assertEqual(order.total_pence, 15400)
        self.assertEqual(order.items[0].product_name, 'Tracking Kit')

    # -- who can see an order --
    def test_an_order_is_only_shown_to_its_key_its_owner_or_an_admin(self):
        _, order = self.place(self.client(self.member))
        number = order.order_number
        self.assertEqual(self.client().get(f'/store/order/{number}').status_code, 404)
        self.assertEqual(self.client().get(f'/store/order/{number}?key=wrong').status_code, 404)
        self.assertEqual(self.client(self.other).get(f'/store/order/{number}').status_code, 404)
        self.assertEqual(self.client().post(f'/store/order/{number}/pay').status_code, 404)
        self.assertEqual(self.client().get(f'/store/order/{number}?key={order.access_token}').status_code, 200)
        self.assertEqual(self.client(self.member).get(f'/store/order/{number}').status_code, 200)
        self.assertEqual(self.client(self.admin).get(f'/store/order/{number}').status_code, 200)
        self.assertEqual(self.client().get('/store/order/SEL-NOSUCH00?key=x').status_code, 404)

    # -- running the shop --
    def test_admin_pages_are_for_admins(self):
        for path in ('/admin/store', '/admin/store/product/new', f'/admin/store/product/{self.kit.id}'):
            self.assertEqual(self.client(self.member).get(path).status_code, 302, path)
            self.assertEqual(self.client(self.admin).get(path).status_code, 200, path)
        self.assertEqual(self.client().get('/admin/store').status_code, 401)
        response = self.client(self.member).post('/admin/store/product/new', data={'name': 'Free stuff', 'price': '0.01'})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(StoreProduct.query.filter_by(name='Free stuff').first())

    def test_home_page_has_a_store_card_instead_of_support(self):
        page = self.client().get('/home').get_data(as_text=True)
        self.assertIn('<a class="home-card" href="/store">', page)
        self.assertNotIn('<a class="home-card" href="/support">', page)
        self.assertIn('href="/support">Support</a>', page)       # still in the footer

    def test_manage_store_button_is_for_admins_only(self):
        self.assertIn('href="/admin/store"', self.client(self.admin).get('/store').get_data(as_text=True))
        for user in (None, self.member):
            self.assertNotIn('Manage store', self.client(user).get('/store').get_data(as_text=True))
        StoreProduct.query.delete()
        db.session.commit()
        self.assertIn('Manage store', self.client(self.admin).get('/store').get_data(as_text=True))

    def test_admin_adds_and_edits_a_product(self):
        c = self.client(self.admin)
        c.post('/admin/store/product/new', data={'name': 'Focuser Kit', 'price': '£89.00', 'stock': '',
                                                 'max_per_order': '3', 'summary': 'Sharper stars', 'is_active': '1'})
        product = StoreProduct.query.filter_by(slug='focuser-kit').one()
        self.assertEqual((product.price_pence, product.stock, product.max_per_order, product.is_active),
                         (8900, None, 3, True))
        c.post(f'/admin/store/product/{product.id}', data={'name': 'Focuser Kit', 'price': '95', 'stock': '4'})
        product = db.session.get(StoreProduct, product.id)
        self.assertEqual((product.price_pence, product.stock, product.is_active), (9500, 4, False))
        for bad in ({'price': '0'}, {'price': 'ten'}, {'price': '1.999'}, {'stock': '-1'}, {'name': ''}):
            data = {'name': 'Focuser Kit', 'price': '95', 'stock': '4'}
            data.update(bad)
            self.assertEqual(c.post(f'/admin/store/product/{product.id}', data=data).status_code, 200, bad)
        self.assertEqual(db.session.get(StoreProduct, product.id).price_pence, 9500)

    def test_deleting_a_product_keeps_past_orders_readable(self):
        c, order = self.place()
        self.client(self.admin).post(f'/admin/store/product/{self.kit.id}/delete')
        self.assertIsNone(db.session.get(StoreProduct, self.kit.id))
        self.assertIn('Tracking Kit', self.order_page(c, order).get_data(as_text=True))

    def test_admin_manages_delivery_options(self):
        c = self.client(self.admin)
        c.post('/admin/store/shipping', data={'label': 'Europe', 'price': '12.50'})
        zone = StoreShippingZone.query.filter_by(label='Europe').one()
        self.assertEqual((zone.price_pence, zone.is_active), (1250, True))
        c.post('/admin/store/shipping', data={'zone_id': zone.id, 'label': 'Europe (tracked)', 'price': '0'})
        zone = db.session.get(StoreShippingZone, zone.id)
        self.assertEqual((zone.label, zone.price_pence, zone.is_active), ('Europe (tracked)', 0, False))
        c.post('/admin/store/shipping', data={'zone_id': zone.id, 'delete': '1'})
        self.assertIsNone(db.session.get(StoreShippingZone, zone.id))

    def test_admin_sends_an_order_and_the_customer_is_told(self):
        _, order = self.place()
        c = self.client(self.admin)
        action = f'/admin/store/orders/{order.id}/action'
        c.post(action, data={'action': 'ship', 'notify': '1'})
        self.assertEqual(self.fresh(order).status, 'PENDING')           # not paid: can't be sent

        self.sumup.settle(order.checkout_id)
        c.post(action, data={'action': 'sync'})
        self.assertEqual(self.fresh(order).status, 'PAID')
        c.post(action, data={'action': 'ship', 'tracking_reference': 'RM123', 'notify': '1'})
        order = self.fresh(order)
        self.assertEqual((order.status, order.tracking_reference), ('SHIPPED', 'RM123'))
        self.assertIn('has been sent', self.mail[-1][1])
        self.assertIn('RM123', self.mail[-1][2])
        page = self.client().get(f'/store/order/{order.order_number}?key={order.access_token}').get_data(as_text=True)
        self.assertIn('on its way', page)
        self.assertIn('RM123', page)
        self.assertIn(order.order_number, c.get(f'/admin/store/orders/{order.id}').get_data(as_text=True))
        self.assertIn(order.order_number, c.get('/admin/store?status=SHIPPED').get_data(as_text=True))

    def test_cancelling_closes_the_payment_page(self):
        _, order = self.place()
        c = self.client(self.admin)
        c.post(f'/admin/store/orders/{order.id}/action', data={'action': 'cancel'})
        self.assertEqual(self.fresh(order).status, 'CANCELLED')
        self.assertEqual(self.sumup.checkouts[order.checkout_id]['status'], 'EXPIRED')

    def test_cancel_is_refused_if_the_customer_has_just_paid(self):
        _, order = self.place()
        self.sumup.settle(order.checkout_id)
        self.client(self.admin).post(f'/admin/store/orders/{order.id}/action', data={'action': 'cancel'})
        self.assertEqual(self.fresh(order).status, 'PAID')

    def test_refund_is_only_a_record(self):
        _, order = self.place()
        self.sumup.settle(order.checkout_id)
        self.webhook(order.checkout_id)
        self.client(self.admin).post(f'/admin/store/orders/{order.id}/action', data={'action': 'refunded'})
        order = self.fresh(order)
        self.assertEqual(order.status, 'REFUNDED')
        self.assertIn('Marked refunded by store_admin', order.admin_note)

    def test_order_page_links_to_a_payment_support_ticket(self):
        c, order = self.place()
        page = self.order_page(c, order).get_data(as_text=True)
        self.assertIn('/contact?type=payment&amp;title=Order+' + order.order_number, page)
        form = c.get(f'/contact?type=payment&title=Order+{order.order_number}').get_data(as_text=True)
        self.assertIn('<option value="💳 Payment or Order Issue" selected>', form)
        self.assertIn(f'value="Order {order.order_number}"', form)
        plain = c.get('/contact').get_data(as_text=True)
        self.assertIn('<option value="💳 Payment or Order Issue" >', plain)
        self.assertIn('disabled selected>Select a type', plain)

    # -- the rest of the payments plumbing --
    def test_webhook_stays_reachable_while_payments_pages_are_switched_off(self):
        self.assertFalse(page_toggles.is_page_disabled('/payments/sumup/webhook'))
        states = page_toggles._states()
        if states.get('/payments') is False:
            self.assertTrue(page_toggles.is_page_disabled('/payments/return'))

    def test_mode_comes_from_the_environment_and_picks_the_matching_key(self):
        saved = {k: os.environ.get(k) for k in ('SUMUP_TEST_API_KEY', 'SUMUP_TEST_MERCHANT_CODE',
                                                'SUMUP_API_KEY', 'SUMUP_MERCHANT_CODE')}
        os.environ.update(SUMUP_TEST_API_KEY='test-key', SUMUP_TEST_MERCHANT_CODE='MT',
                          SUMUP_API_KEY='live-key', SUMUP_MERCHANT_CODE='ML')
        try:
            os.environ['SUMUP_MODE'] = 'sandbox'
            self.assertEqual(sumup_service.resolve_sumup_credentials()['api_key'], 'test-key')
            self.assertEqual(sumup_service.resolve_sumup_credentials(is_test_mode=False)['api_key'], 'live-key')
            os.environ['SUMUP_MODE'] = 'live'
            self.assertEqual(sumup_service.resolve_sumup_credentials()['api_key'], 'live-key')
            self.assertEqual(sumup_service.resolve_sumup_credentials(is_test_mode=True)['api_key'], 'test-key')
            os.environ['SUMUP_MODE'] = 'nonsense'
            self.assertEqual(store_service.store_mode(), 'off')
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
