"""The store: products, delivery options and orders.

Money is whole pence. An order keeps its own copy of each line's name and price, so
later edits to a product never change what a customer was charged. Names, emails and
addresses are encrypted at rest like the rest of the site's personal data."""
import re
import secrets
from datetime import datetime

from app.db import db
from utility.encryption import EncryptedString

# What an order can be. A payment only ever moves PENDING/FAILED/EXPIRED to PAID;
# the rest are set by an admin.
ORDER_STATUSES = ('PENDING', 'PAID', 'FAILED', 'EXPIRED', 'SHIPPED', 'CANCELLED', 'REFUNDED')
AWAITING_PAYMENT = ('PENDING', 'FAILED', 'EXPIRED')

# No 0/O or 1/I, so an order number reads back cleanly over the phone or in an email
_ORDER_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'


def slugify(text):
    slug = re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')
    return slug[:80] or 'item'


def format_pence(pence, currency='GBP'):
    symbol = {'GBP': '£', 'EUR': '€', 'USD': '$'}.get(currency, '')
    pence = int(pence or 0)
    text = f'{symbol}{abs(pence) // 100:,}.{abs(pence) % 100:02d}'
    if not symbol:
        text = f'{text} {currency}'
    return f'-{text}' if pence < 0 else text


class StoreProduct(db.Model):
    __tablename__ = 'store_products'

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    summary = db.Column(db.String(240), nullable=True)
    description = db.Column(db.Text, nullable=True)
    price_pence = db.Column(db.Integer, nullable=False)
    # None = not tracked (made to order); 0 = sold out
    stock = db.Column(db.Integer, nullable=True)
    max_per_order = db.Column(db.Integer, nullable=False, default=5)
    image_filename = db.Column(db.String(120), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=False, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def in_stock(self):
        return self.stock is None or self.stock > 0

    @property
    def most_orderable(self):
        """The largest quantity one order may hold."""
        limit = max(1, self.max_per_order or 1)
        return limit if self.stock is None else max(0, min(limit, self.stock))

    @classmethod
    def unique_slug(cls, name, ignore_id=None):
        base = slugify(name)
        slug, n = base, 1
        while True:
            clash = cls.query.filter_by(slug=slug).first()
            if clash is None or clash.id == ignore_id:
                return slug
            n += 1
            slug = f'{base[:76]}-{n}'


class StoreShippingZone(db.Model):
    """A delivery option the customer picks at checkout, e.g. "United Kingdom"."""
    __tablename__ = 'store_shipping_zones'

    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(120), nullable=False)
    price_pence = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)


class StoreOrder(db.Model):
    __tablename__ = 'store_orders'

    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(24), unique=True, nullable=False, index=True)
    # Lets a guest open their order page from the link they were given
    access_token = db.Column(db.String(64), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'), nullable=True, index=True)

    email = db.Column(EncryptedString(254), nullable=False)
    full_name = db.Column(EncryptedString(120), nullable=False)
    phone = db.Column(EncryptedString(40), nullable=True)
    address_line1 = db.Column(EncryptedString(120), nullable=False)
    address_line2 = db.Column(EncryptedString(120), nullable=True)
    city = db.Column(EncryptedString(80), nullable=False)
    region = db.Column(EncryptedString(80), nullable=True)
    postcode = db.Column(EncryptedString(20), nullable=False)
    country = db.Column(EncryptedString(80), nullable=False)
    customer_note = db.Column(EncryptedString(500), nullable=True)

    shipping_label = db.Column(db.String(120), nullable=False)
    shipping_pence = db.Column(db.Integer, nullable=False, default=0)
    subtotal_pence = db.Column(db.Integer, nullable=False)
    total_pence = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(3), nullable=False, default='GBP')

    status = db.Column(db.String(16), nullable=False, default='PENDING', index=True)
    is_test_mode = db.Column(db.Boolean, nullable=False, default=False, index=True)

    # The newest SumUp checkout for this order. Each payment attempt gets its own,
    # referenced "<order number>-<attempt>".
    payment_attempts = db.Column(db.Integer, nullable=False, default=0)
    checkout_id = db.Column(db.String(64), nullable=True, index=True)
    transaction_code = db.Column(db.String(64), nullable=True)

    stock_applied = db.Column(db.Boolean, nullable=False, default=False)
    confirmation_sent = db.Column(db.Boolean, nullable=False, default=False)
    tracking_reference = db.Column(db.String(120), nullable=True)
    admin_note = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    paid_at = db.Column(db.DateTime, nullable=True)
    shipped_at = db.Column(db.DateTime, nullable=True)

    items = db.relationship('StoreOrderItem', backref='order', lazy='select', cascade='all, delete-orphan')

    @staticmethod
    def new_order_number():
        while True:
            number = 'SEL-' + ''.join(secrets.choice(_ORDER_ALPHABET) for _ in range(8))
            if StoreOrder.query.filter_by(order_number=number).first() is None:
                return number

    @property
    def awaiting_payment(self):
        return self.status in AWAITING_PAYMENT

    @property
    def is_paid(self):
        return self.status in ('PAID', 'SHIPPED')

    @property
    def address_lines(self):
        parts = [self.full_name, self.address_line1, self.address_line2, self.city,
                 self.region, self.postcode, self.country]
        return [p for p in parts if p]


class StoreOrderItem(db.Model):
    __tablename__ = 'store_order_items'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('store_orders.id', ondelete='CASCADE'), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey('store_products.id', ondelete='SET NULL'), nullable=True)
    product_name = db.Column(db.String(120), nullable=False)
    unit_price_pence = db.Column(db.Integer, nullable=False)
    quantity = db.Column(db.Integer, nullable=False)

    @property
    def line_pence(self):
        return self.unit_price_pence * self.quantity
