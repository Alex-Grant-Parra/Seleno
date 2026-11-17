"""The store's rules: who may order, the basket, turning a basket into an order,
taking payment through SumUp Hosted Checkout, and deciding when an order is paid.

An order is only ever marked paid from SumUp's own answer about a checkout (fetched
server-side with the API key), never from what a browser or a webhook body says, and
only when the amount and currency SumUp took match the order's total."""
import os
import re
import threading
import time
from datetime import datetime
from decimal import Decimal

from flask import current_app, session
from flask_babel import gettext as _
from sqlalchemy import case, update

from app.db import db
from app.sumup_service import (
    SumUpAPIError,
    create_checkout,
    current_sumup_mode,
    deactivate_checkout,
    is_sumup_feature_enabled,
    resolve_sumup_credentials,
    retrieve_checkout,
)
from models.payment import SumupCheckout, SumupEvent
from models.store import (
    AWAITING_PAYMENT,
    StoreOrder,
    StoreOrderItem,
    StoreProduct,
    StoreShippingZone,
    format_pence,
)
from utility.emailer import send_email

CURRENCY = 'GBP'
BASKET_KEY = 'store_basket'
MAX_BASKET_LINES = 20
MAX_ORDER_PENCE = 2_000_000          # £20,000: far above any basket of kits
ORDERS_PER_WINDOW = 6                # new orders one address may start...
ORDER_WINDOW_SECONDS = 600           # ...in ten minutes

_REFERENCE = re.compile(r'^(SEL-[A-Z0-9]{8})-\d+$')
_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


class StoreError(Exception):
    """Something the customer (or admin) should be told in plain words."""


# --- Who may order ---------------------------------------------------------

def store_mode():
    """'live', 'sandbox', or 'off' when payments are disabled or misconfigured."""
    if not is_sumup_feature_enabled():
        return 'off'
    try:
        return current_sumup_mode()
    except SumUpAPIError:
        return 'off'


def can_order(user):
    """Live: everyone. Sandbox: admins only, so the public never places a pretend
    order. Off: nobody."""
    mode = store_mode()
    if mode == 'live':
        return True
    if mode == 'sandbox':
        return bool(getattr(user, 'is_authenticated', False) and getattr(user, 'is_admin', False))
    return False


# --- Basket (kept in the session cookie as {product id: quantity}) ---------

def _raw_basket():
    basket = session.get(BASKET_KEY)
    return basket if isinstance(basket, dict) else {}


def basket_count():
    try:
        return sum(int(q) for q in _raw_basket().values())
    except (TypeError, ValueError):
        return 0


def basket_set(product, quantity):
    """Put `quantity` of a product in the basket (0 removes it). Returns the
    quantity actually held, after the stock and per-order limits."""
    basket = dict(_raw_basket())
    key = str(product.id)
    quantity = max(0, min(int(quantity), product.most_orderable))
    if quantity == 0 or not product.is_active:
        basket.pop(key, None)
        quantity = 0
    else:
        if key not in basket and len(basket) >= MAX_BASKET_LINES:
            raise StoreError(_('Your basket is full.'))
        basket[key] = quantity
    session[BASKET_KEY] = basket
    session.modified = True
    return quantity


def basket_clear():
    session.pop(BASKET_KEY, None)


def basket_lines():
    """The basket as [{product, quantity, line_pence}], and whether anything had
    to be dropped or reduced since the customer last looked (sold out, withdrawn)."""
    basket = _raw_basket()
    lines, cleaned, adjusted = [], {}, False
    ids = [int(k) for k in basket if str(k).isdigit()]
    products = {p.id: p for p in StoreProduct.query.filter(StoreProduct.id.in_(ids)).all()} if ids else {}
    for key, raw_quantity in basket.items():
        product = products.get(int(key)) if str(key).isdigit() else None
        try:
            wanted = int(raw_quantity)
        except (TypeError, ValueError):
            wanted = 0
        if product is None or not product.is_active or wanted < 1:
            adjusted = True
            continue
        quantity = min(wanted, product.most_orderable)
        if quantity != wanted:
            adjusted = True
        if quantity < 1:
            continue
        cleaned[str(product.id)] = quantity
        lines.append({'product': product, 'quantity': quantity, 'line_pence': product.price_pence * quantity})
    if cleaned != basket:
        session[BASKET_KEY] = cleaned
        session.modified = True
    lines.sort(key=lambda line: (line['product'].sort_order, line['product'].name))
    return lines, adjusted


# Delivery options on/off. While off, checkout doesn't ask the customer to choose
# delivery and charges nothing for it (the address is still collected, to post the
# kit). STORE_DELIVERY=on/off in instance/.env overrides this.
DELIVERY_ENABLED = False


def delivery_enabled():
    value = (os.getenv('STORE_DELIVERY') or '').strip().lower()
    if value:
        return value in ('1', 'true', 'yes', 'on')
    return DELIVERY_ENABLED


class _NoDeliveryCharge:
    """Stands in for a delivery option while delivery options are switched off."""
    id = None
    label = 'not charged'
    price_pence = 0
    is_active = True


NO_DELIVERY = _NoDeliveryCharge()


def active_zones():
    return (StoreShippingZone.query.filter_by(is_active=True)
            .order_by(StoreShippingZone.sort_order, StoreShippingZone.label).all())


# --- Orders ----------------------------------------------------------------

_recent_orders = {}
_recent_lock = threading.Lock()


def _within_order_rate(ip):
    now = time.time()
    with _recent_lock:
        stamps = [t for t in _recent_orders.get(ip, []) if now - t < ORDER_WINDOW_SECONDS]
        allowed = len(stamps) < ORDERS_PER_WINDOW
        if allowed:
            stamps.append(now)
        _recent_orders[ip] = stamps
        if len(_recent_orders) > 5000:
            for key in [k for k, v in _recent_orders.items() if not v or now - v[-1] > ORDER_WINDOW_SECONDS]:
                _recent_orders.pop(key, None)
        return allowed


FIELD_LIMITS = {
    'email': 254, 'full_name': 120, 'phone': 40, 'address_line1': 120, 'address_line2': 120,
    'city': 80, 'region': 80, 'postcode': 20, 'country': 80, 'customer_note': 500,
}
REQUIRED_FIELDS = ('email', 'full_name', 'address_line1', 'city', 'postcode', 'country')


def clean_details(form):
    """The checkout form as a dict of trimmed strings, plus {field: problem}."""
    details, errors = {}, {}
    for field, limit in FIELD_LIMITS.items():
        value = ' '.join((form.get(field) or '').split()) if field != 'customer_note' else (form.get(field) or '').strip()
        if len(value) > limit:
            errors[field] = 'too_long'
        details[field] = value
    for field in REQUIRED_FIELDS:
        if not details[field]:
            errors[field] = 'required'
    if details['email'] and not _EMAIL.match(details['email']):
        errors['email'] = 'invalid'
    return details, errors


def create_order(lines, zone, details, user, ip=None):
    """A PENDING order from the basket. Prices are read from the database here; the
    browser never supplies an amount."""
    if not lines:
        raise StoreError(_('Your basket is empty.'))
    if zone is None or not zone.is_active:
        raise StoreError(_('Choose a delivery option.'))
    subtotal = sum(line['line_pence'] for line in lines)
    total = subtotal + zone.price_pence
    if total <= 0 or total > MAX_ORDER_PENCE:
        raise StoreError(_('This order total cannot be paid online. Please contact support.'))
    if ip and not _within_order_rate(ip):
        raise StoreError(_('Too many orders started from this connection. Please wait a few minutes.'))

    mode = store_mode()
    signed_in = bool(getattr(user, 'is_authenticated', False))
    order = StoreOrder(
        order_number=StoreOrder.new_order_number(),
        access_token=os.urandom(24).hex(),
        user_id=user.id if signed_in else None,
        email=details['email'],
        full_name=details['full_name'],
        phone=details.get('phone') or None,
        address_line1=details['address_line1'],
        address_line2=details.get('address_line2') or None,
        city=details['city'],
        region=details.get('region') or None,
        postcode=details['postcode'],
        country=details['country'],
        customer_note=details.get('customer_note') or None,
        shipping_label=zone.label,
        shipping_pence=zone.price_pence,
        subtotal_pence=subtotal,
        total_pence=total,
        currency=CURRENCY,
        status='PENDING',
        is_test_mode=(mode != 'live'),
    )
    for line in lines:
        order.items.append(StoreOrderItem(
            product_id=line['product'].id,
            product_name=line['product'].name,
            unit_price_pence=line['product'].price_pence,
            quantity=line['quantity'],
        ))
    db.session.add(order)
    db.session.commit()
    return order


def order_for_reference(checkout_reference):
    match = _REFERENCE.match(checkout_reference or '')
    if not match:
        return None
    return StoreOrder.query.filter_by(order_number=match.group(1)).first()


def _note(order, text):
    stamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M')
    order.admin_note = f'{order.admin_note}\n' if order.admin_note else ''
    order.admin_note += f'[{stamp} UTC] {text}'


def _checkout_pence(row):
    return int((Decimal(str(row.amount)) * 100).to_integral_value())


def apply_checkout(order, row):
    """Bring an order in line with one of its SumUp checkouts. Returns True when
    this call is the one that marked it paid (so stock and emails happen once,
    even if the webhook and the customer's return land together)."""
    status = (row.status or '').upper()

    if status == 'PAID':
        if order.stock_applied:
            return False
        if _checkout_pence(row) != order.total_pence or (row.currency or '').upper() != order.currency:
            if row.checkout_id in (order.admin_note or ''):
                return False
            _note(order, f'SumUp checkout {row.checkout_id} is PAID for {row.amount} {row.currency}, '
                         f'which does not match this order ({format_pence(order.total_pence, order.currency)}). '
                         'Not marked paid; check it in SumUp.')
            return False
        was_cancelled = order.status == 'CANCELLED'
        if order.status not in AWAITING_PAYMENT and not was_cancelled:
            return False
        # One statement decides the winner between two requests arriving together
        claimed = db.session.execute(
            update(StoreOrder)
            .where(StoreOrder.id == order.id, StoreOrder.stock_applied.is_(False))
            .values(stock_applied=True, status='PAID', paid_at=datetime.utcnow(),
                    checkout_id=row.checkout_id, transaction_code=row.transaction_code)
        ).rowcount
        if claimed != 1:
            return False
        db.session.refresh(order)
        if was_cancelled:
            _note(order, 'Payment arrived after this order was cancelled. Refund it in SumUp or send it.')
        for item in order.items:
            product = db.session.get(StoreProduct, item.product_id) if item.product_id else None
            if product is None or product.stock is None:
                continue
            if product.stock < item.quantity:
                _note(order, f'Oversold: {item.quantity} x {item.product_name} ordered with {product.stock} left.')
            db.session.execute(
                update(StoreProduct).where(StoreProduct.id == product.id).values(
                    stock=case((StoreProduct.stock >= item.quantity, StoreProduct.stock - item.quantity), else_=0))
            )
        return True

    if order.status in AWAITING_PAYMENT and row.checkout_id == order.checkout_id \
            and status in ('PENDING', 'FAILED', 'EXPIRED'):
        order.status = status
    return False


def checkout_changed(row):
    """Called whenever a SumUp checkout has just been re-read (webhook, sync).
    Updates the order it belongs to, if it is a store checkout."""
    order = order_for_reference(row.checkout_reference)
    if order is None:
        return None
    newly_paid = apply_checkout(order, row)
    db.session.commit()
    if newly_paid:
        send_order_emails(order)
    return order


def sync_order(order):
    """Ask SumUp about this order's newest checkout and apply the answer."""
    if not order.checkout_id:
        return order
    creds = resolve_sumup_credentials(is_test_mode=order.is_test_mode)
    remote = retrieve_checkout(creds['api_key'], order.checkout_id)
    row = SumupCheckout.upsert_from_sumup(remote, user_id=order.user_id, is_test_mode=order.is_test_mode)
    SumupEvent.log_event(checkout_record_id=row.id, event_source='sync', event_type='STORE_ORDER_SYNC',
                         verification_status='verified', response_payload=remote)
    db.session.commit()
    checkout_changed(row)
    return order


def retire_checkout(order):
    """Close the order's open SumUp checkout so it can no longer be paid."""
    if not order.checkout_id:
        return
    try:
        creds = resolve_sumup_credentials(is_test_mode=order.is_test_mode)
        remote = deactivate_checkout(creds['api_key'], order.checkout_id)
        if (remote or {}).get('id'):
            SumupCheckout.upsert_from_sumup(remote, user_id=order.user_id, is_test_mode=order.is_test_mode)
            db.session.commit()
    except (SumUpAPIError, ValueError):
        # Already paid, expired or gone: nothing left to close
        db.session.rollback()


def start_payment(order, *, order_url, webhook_url, ip=None):
    """A fresh SumUp Hosted Checkout for the order; returns the page to send the
    customer to, or None if the order turned out to be paid already."""
    if not order.awaiting_payment:
        return None
    creds = resolve_sumup_credentials()
    if creds['is_test_mode'] != order.is_test_mode:
        raise StoreError(_('This order was started in a different payment mode. Please place it again.'))

    if order.checkout_id:
        # A second attempt: make sure the first didn't go through, then close it
        try:
            sync_order(order)
        except SumUpAPIError:
            db.session.rollback()
        if not order.awaiting_payment:
            return None
        retire_checkout(order)

    order.payment_attempts = (order.payment_attempts or 0) + 1
    payload = {
        'checkout_reference': f'{order.order_number}-{order.payment_attempts}',
        'amount': float(Decimal(order.total_pence) / 100),
        'currency': order.currency,
        'merchant_code': creds['merchant_code'],
        'description': f'SELENO order {order.order_number}',
        'purpose': 'CHECKOUT',
        'return_url': webhook_url,
        'redirect_url': order_url,
        'hosted_checkout': {'enabled': True},
    }
    remote = create_checkout(creds['api_key'], payload)
    hosted_url = (remote or {}).get('hosted_checkout_url')
    if not hosted_url:
        db.session.rollback()
        raise StoreError(_('The payment page could not be opened. Please try again.'))
    row = SumupCheckout.upsert_from_sumup(
        remote, user_id=order.user_id, metadata={'storeOrder': order.order_number},
        request_payload=payload, is_test_mode=creds['is_test_mode'])
    SumupEvent.log_event(checkout_record_id=row.id, event_source='api', event_type='STORE_CHECKOUT_CREATED',
                         verification_status='created', remote_ip=ip, request_payload=payload,
                         response_payload=remote)
    order.checkout_id = row.checkout_id
    order.status = 'PENDING'
    db.session.commit()
    return hosted_url


# --- Emails ----------------------------------------------------------------

def _deliver(app, recipients, subject, body):
    """Send in the background: SumUp counts a slow webhook answer as a failure."""
    def run():
        try:
            send_email(app, 'support', recipients, subject, body)
        except Exception:
            app.logger.exception('Store email to %d recipient(s) failed: %s', len(recipients), subject)
    threading.Thread(target=run, daemon=True).start()


def order_summary_text(order):
    lines = [f'  {item.quantity} x {item.product_name}  {format_pence(item.line_pence, order.currency)}'
             for item in order.items]
    lines.append(f'  Delivery ({order.shipping_label})  {format_pence(order.shipping_pence, order.currency)}')
    lines.append(f'  Total  {format_pence(order.total_pence, order.currency)}')
    return '\n'.join(lines)


def send_order_emails(order):
    """The customer's confirmation and a heads-up to the shop, once per order."""
    if order.confirmation_sent:
        return
    order.confirmation_sent = True
    db.session.commit()

    app = current_app._get_current_object()
    domain = (app.config.get('APP_DOMAIN') or '').strip()
    link = f'https://{domain}/store/order/{order.order_number}?key={order.access_token}' if domain else ''
    test = '[TEST] ' if order.is_test_mode else ''
    summary = order_summary_text(order)
    address = '\n'.join(f'  {line}' for line in order.address_lines)

    customer_body = (
        f'Hello {order.full_name},\n\n'
        f'Thank you for your order. Your payment has been received.\n\n'
        f'Order {order.order_number}\n{summary}\n\n'
        f'Delivering to:\n{address}\n\n'
        + (f'You can check on your order here:\n{link}\n\n' if link else '')
        + 'We will email you again when it is on its way. Reply to this email if anything looks wrong.\n\n'
          'SELENO\n'
    )
    if order.is_test_mode:
        customer_body = 'THIS IS A TEST ORDER. No money was taken and nothing will be sent.\n\n' + customer_body
    _deliver(app, [order.email], f'{test}Your SELENO order {order.order_number}', customer_body)

    shop = (os.getenv('STORE_NOTIFY_EMAIL') or app.config.get('MAIL_SUPPORT_SENDER') or '').strip()
    if shop:
        shop_body = (
            f'New paid order {order.order_number} ({format_pence(order.total_pence, order.currency)}).\n\n'
            f'{summary}\n\nSend to:\n{address}\n\nEmail: {order.email}\n'
            + (f'Phone: {order.phone}\n' if order.phone else '')
            + (f'Note: {order.customer_note}\n' if order.customer_note else '')
            + (f'\nhttps://{domain}/admin/store/orders/{order.id}\n' if domain else '')
        )
        _deliver(app, [shop], f'{test}New store order {order.order_number}', shop_body)


def send_shipped_email(order):
    app = current_app._get_current_object()
    test = '[TEST] ' if order.is_test_mode else ''
    body = (
        f'Hello {order.full_name},\n\n'
        f'Your order {order.order_number} is on its way.\n'
        + (f'Tracking reference: {order.tracking_reference}\n' if order.tracking_reference else '')
        + f'\n{order_summary_text(order)}\n\nSELENO\n'
    )
    _deliver(app, [order.email], f'{test}Your SELENO order {order.order_number} has been sent', body)
