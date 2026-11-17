"""The store: /store for customers, /admin/store for running it.

Customers browse, fill a basket (kept in their session), give a delivery address and
pay on SumUp's hosted page. They come back to /store/order/<number>, which asks SumUp
what happened. Guests reach their order through the key in that link.

While SumUp is in sandbox mode only admins can check out, and every order is marked
as a test. The rules live in app/store_service.py."""
import hmac
import os
import time
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request,
                   send_from_directory, url_for)
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app import store_service
from app.db import db
from app.store_service import StoreError
from app.sumup_service import SumUpAPIError
from models.store import (ORDER_STATUSES, StoreOrder, StoreOrderItem, StoreProduct, StoreShippingZone,
                          format_pence)
from security.client_ip import client_ip

store_bp = Blueprint('store', __name__)

IMAGE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'instance', 'uploads', 'store'))
IMAGE_TYPES = {'.png', '.jpg', '.jpeg', '.webp'}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


@store_bp.app_template_filter('money')
def money_filter(pence, currency='GBP'):
    return format_pence(pence, currency)


_nav = {'at': 0.0, 'show': False}


@store_bp.app_context_processor
def store_nav():
    """The Store link appears in the menu once there is something to sell."""
    now = time.time()
    if now - _nav['at'] > 30:
        try:
            _nav['show'] = db.session.query(StoreProduct.id).filter_by(is_active=True).first() is not None
        except Exception:
            db.session.rollback()
            _nav['show'] = False
        _nav['at'] = now
    return {'store_in_nav': _nav['show']}


def _state():
    return {
        'mode': store_service.store_mode(),
        'can_order': store_service.can_order(current_user),
        'basket_count': store_service.basket_count(),
        'delivery_on': store_service.delivery_enabled(),
    }


def _products():
    return (StoreProduct.query.filter_by(is_active=True)
            .order_by(StoreProduct.sort_order, StoreProduct.name).all())


def _base_url():
    domain = (current_app.config.get('APP_DOMAIN') or '').strip()
    return f'https://{domain}' if domain else request.url_root.rstrip('/')


def _order_url(order):
    return f'{_base_url()}/store/order/{order.order_number}?key={order.access_token}'


def _webhook_url():
    from controllers.payments import _build_webhook_return_url
    return _build_webhook_return_url()


def _find_order(order_number):
    """The order, if this visitor may see it: they hold its key, placed it while
    signed in, or are an admin. Anything else looks like it doesn't exist."""
    order = StoreOrder.query.filter_by(order_number=(order_number or '').upper()).first()
    if order is None:
        abort(404)
    key = (request.values.get('key') or '').strip()
    if key and hmac.compare_digest(key.encode(), order.access_token.encode()):
        return order
    if current_user.is_authenticated and (current_user.is_admin or order.user_id == current_user.id):
        return order
    abort(404)


# --- Customers -------------------------------------------------------------

@store_bp.route('/store')
def store_home():
    return render_template('store.html', products=_products(), store=_state())


@store_bp.route('/store/product/<slug>')
def store_product(slug):
    product = StoreProduct.query.filter_by(slug=slug).first()
    if product is None or not (product.is_active or (current_user.is_authenticated and current_user.is_admin)):
        abort(404)
    return render_template('store_product.html', product=product, store=_state())


@store_bp.route('/store/image/<path:filename>')
def store_image(filename):
    return send_from_directory(IMAGE_DIR, filename, max_age=86400)


@store_bp.route('/store/basket')
def store_basket():
    lines, adjusted = store_service.basket_lines()
    if adjusted:
        flash(_('Your basket was updated: something in it is no longer available in that quantity.'), 'warning')
    return render_template('store_basket.html', lines=lines, store=_state(),
                           subtotal=sum(line['line_pence'] for line in lines))


@store_bp.route('/store/basket/add', methods=['POST'])
def store_basket_add():
    product = db.session.get(StoreProduct, request.form.get('product_id', type=int) or 0)
    if product is None or not product.is_active:
        abort(404)
    wanted = max(1, request.form.get('quantity', type=int) or 1)
    already = 0
    try:
        already = int((store_service._raw_basket() or {}).get(str(product.id), 0))
    except (TypeError, ValueError):
        pass
    try:
        held = store_service.basket_set(product, already + wanted)
    except StoreError as exc:
        flash(str(exc), 'warning')
        return redirect(url_for('store.store_basket'))
    if held == 0:
        flash(_('%(name)s is sold out.', name=product.name), 'warning')
    elif held < already + wanted:
        flash(_('Only %(count)s of %(name)s can be ordered at once.', count=held, name=product.name), 'warning')
    else:
        flash(_('%(name)s added to your basket.', name=product.name), 'success')
    return redirect(url_for('store.store_basket'))


@store_bp.route('/store/basket/update', methods=['POST'])
def store_basket_update():
    for key, value in request.form.items():
        if not key.startswith('quantity_'):
            continue
        product = db.session.get(StoreProduct, int(key[9:])) if key[9:].isdigit() else None
        if product is None:
            continue
        try:
            quantity = int(value)
        except (TypeError, ValueError):
            continue
        if request.form.get('remove') == str(product.id):
            quantity = 0
        try:
            store_service.basket_set(product, quantity)
        except StoreError as exc:
            flash(str(exc), 'warning')
    return redirect(url_for('store.store_basket'))


@store_bp.route('/store/checkout', methods=['GET', 'POST'])
def store_checkout():
    state = _state()
    lines, adjusted = store_service.basket_lines()
    if not lines:
        flash(_('Your basket is empty.'), 'info')
        return redirect(url_for('store.store_home'))
    if adjusted:
        flash(_('Your basket was updated: something in it is no longer available in that quantity.'), 'warning')
        return redirect(url_for('store.store_basket'))
    if not state['can_order']:
        flash(_('Ordering is not open yet.'), 'info')
        return redirect(url_for('store.store_basket'))

    delivery_on = state['delivery_on']
    zones = store_service.active_zones() if delivery_on else []
    subtotal = sum(line['line_pence'] for line in lines)
    details, errors = {}, {}
    zone = None if delivery_on else store_service.NO_DELIVERY

    if request.method == 'POST':
        details, errors = store_service.clean_details(request.form)
        if delivery_on:
            zone = next((z for z in zones if z.id == request.form.get('zone_id', type=int)), None)
            if zone is None:
                errors['zone_id'] = 'required'
        if not request.form.get('accept_terms'):
            errors['accept_terms'] = 'required'
        if not errors:
            try:
                order = store_service.create_order(lines, zone, details, current_user, ip=client_ip())
            except StoreError as exc:
                flash(str(exc), 'danger')
            else:
                store_service.basket_clear()
                try:
                    hosted_url = store_service.start_payment(
                        order, order_url=_order_url(order), webhook_url=_webhook_url(), ip=client_ip())
                except (StoreError, SumUpAPIError) as exc:
                    db.session.rollback()
                    current_app.logger.warning('Store payment could not start for %s: %s (%s)', order.order_number,
                                               getattr(exc, 'message', exc), getattr(exc, 'status_code', ''))
                    flash(_('Your order is saved, but the payment page could not be opened. Please try again.'), 'danger')
                    hosted_url = None
                return redirect(hosted_url or url_for('store.store_order', order_number=order.order_number,
                                                      key=order.access_token))
    elif current_user.is_authenticated:
        details['email'] = current_user.get_email() or ''

    if delivery_on and not zones:
        flash(_('Delivery is not set up yet, so orders cannot be placed.'), 'warning')
    return render_template('store_checkout.html', lines=lines, zones=zones, subtotal=subtotal, store=state,
                           details=details, errors=errors, chosen_zone=zone or (zones[0] if len(zones) == 1 else None))


@store_bp.route('/store/order/<order_number>')
def store_order(order_number):
    order = _find_order(order_number)
    sync_failed = False
    if order.awaiting_payment and order.checkout_id:
        # Coming back from SumUp: ask what happened rather than trust the redirect
        try:
            store_service.sync_order(order)
        except (SumUpAPIError, ValueError):
            db.session.rollback()
            sync_failed = True
    return render_template('store_order.html', order=order, store=_state(), sync_failed=sync_failed,
                           key=(request.args.get('key') or ''))


@store_bp.route('/store/order/<order_number>/pay', methods=['POST'])
def store_order_pay(order_number):
    order = _find_order(order_number)
    back = url_for('store.store_order', order_number=order.order_number, key=order.access_token)
    if not store_service.can_order(current_user):
        flash(_('Ordering is not open yet.'), 'info')
        return redirect(back)
    try:
        hosted_url = store_service.start_payment(
            order, order_url=_order_url(order), webhook_url=_webhook_url(), ip=client_ip())
    except StoreError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
        return redirect(back)
    except SumUpAPIError as exc:
        db.session.rollback()
        current_app.logger.warning('Store payment could not start for %s: %s (%s)', order.order_number,
                                   exc.message, exc.status_code)
        flash(_('The payment page could not be opened. Please try again.'), 'danger')
        return redirect(back)
    return redirect(hosted_url or back)


# --- Admin -----------------------------------------------------------------

def _admin_guard():
    if not current_user.is_authenticated or not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('profile.profile'))
    return None


def _parse_pounds(text, allow_zero=False):
    try:
        pounds = Decimal((text or '').strip().lstrip('£'))
    except InvalidOperation:
        raise ValueError('Enter a price like 49.99.')
    pence = pounds * 100
    if pence != pence.to_integral_value():
        raise ValueError('Prices can have at most two decimal places.')
    pence = int(pence)
    if pence < 0 or (pence == 0 and not allow_zero) or pence > store_service.MAX_ORDER_PENCE:
        raise ValueError('Enter a price above zero.' if not allow_zero else 'Enter a price of zero or more.')
    return pence


def _save_image(upload):
    extension = os.path.splitext(upload.filename or '')[1].lower()
    if extension not in IMAGE_TYPES:
        raise ValueError('Pictures must be PNG, JPEG or WebP.')
    data = upload.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError('Pictures can be at most 8 MB.')
    signatures = {'.png': (b'\x89PNG',), '.jpg': (b'\xff\xd8',), '.jpeg': (b'\xff\xd8',), '.webp': (b'RIFF',)}
    if not data.startswith(signatures[extension]):
        raise ValueError('That file is not the kind of picture its name says.')
    os.makedirs(IMAGE_DIR, exist_ok=True)
    filename = f'{uuid.uuid4().hex}{extension}'
    with open(os.path.join(IMAGE_DIR, filename), 'wb') as fh:
        fh.write(data)
    return filename


def _delete_image(filename):
    if filename:
        try:
            os.remove(os.path.join(IMAGE_DIR, os.path.basename(filename)))
        except OSError:
            pass


@store_bp.route('/admin/store')
@login_required
def admin_store():
    guard = _admin_guard()
    if guard:
        return guard
    status = (request.args.get('status') or '').upper()
    orders = StoreOrder.query
    if status in ORDER_STATUSES:
        orders = orders.filter_by(status=status)
    counts = dict(db.session.query(StoreOrder.status, db.func.count(StoreOrder.id)).group_by(StoreOrder.status).all())
    return render_template(
        'admin_store.html',
        mode=store_service.store_mode(),
        products=StoreProduct.query.order_by(StoreProduct.sort_order, StoreProduct.name).all(),
        zones=StoreShippingZone.query.order_by(StoreShippingZone.sort_order, StoreShippingZone.label).all(),
        orders=orders.order_by(StoreOrder.created_at.desc()).limit(100).all(),
        delivery_on=store_service.delivery_enabled(),
        counts=counts, status_filter=status, statuses=ORDER_STATUSES,
    )


@store_bp.route('/admin/store/product/new', methods=['GET', 'POST'])
@store_bp.route('/admin/store/product/<int:product_id>', methods=['GET', 'POST'])
@login_required
def admin_store_product(product_id=None):
    guard = _admin_guard()
    if guard:
        return guard
    product = db.session.get(StoreProduct, product_id) if product_id else None
    if product_id and product is None:
        abort(404)

    form = request.form if request.method == 'POST' else None
    if form is not None:
        try:
            name = ' '.join((form.get('name') or '').split())
            if not name or len(name) > 120:
                raise ValueError('Give the product a name (up to 120 characters).')
            price_pence = _parse_pounds(form.get('price'))
            stock_text = (form.get('stock') or '').strip()
            stock = None if stock_text == '' else int(stock_text)
            if stock is not None and stock < 0:
                raise ValueError('Stock cannot be negative.')
            max_per_order = int(form.get('max_per_order') or 5)
            if not 1 <= max_per_order <= 100:
                raise ValueError('The most per order must be between 1 and 100.')
            sort_order = int(form.get('sort_order') or 0)
            new_image = None
            upload = request.files.get('image')
            if upload and upload.filename:
                new_image = _save_image(upload)
        except ValueError as exc:
            message = str(exc)
            flash('Enter whole numbers for stock, the most per order and position.'
                  if message.startswith('invalid literal') else message, 'danger')
        else:
            if product is None:
                product = StoreProduct(slug=StoreProduct.unique_slug(name))
                db.session.add(product)
            product.name = name
            product.summary = (form.get('summary') or '').strip()[:240] or None
            product.description = (form.get('description') or '').strip() or None
            product.price_pence = price_pence
            product.stock = stock
            product.max_per_order = max_per_order
            product.sort_order = sort_order
            product.is_active = bool(form.get('is_active'))
            if new_image or form.get('remove_image'):
                _delete_image(product.image_filename)
                product.image_filename = new_image
            db.session.commit()
            _nav['at'] = 0.0
            flash(f'Saved {product.name}.', 'success')
            return redirect(url_for('store.admin_store'))
    return render_template('admin_store_product.html', product=product, form=form)


@store_bp.route('/admin/store/product/<int:product_id>/delete', methods=['POST'])
@login_required
def admin_store_product_delete(product_id):
    guard = _admin_guard()
    if guard:
        return guard
    product = db.session.get(StoreProduct, product_id)
    if product is None:
        abort(404)
    # Orders keep their own copy of the name and price, so past orders still read correctly
    StoreOrderItem.query.filter_by(product_id=product.id).update({'product_id': None})
    _delete_image(product.image_filename)
    db.session.delete(product)
    db.session.commit()
    _nav['at'] = 0.0
    flash(f'Deleted {product.name}.', 'success')
    return redirect(url_for('store.admin_store'))


@store_bp.route('/admin/store/shipping', methods=['POST'])
@login_required
def admin_store_shipping():
    guard = _admin_guard()
    if guard:
        return guard
    zone_id = request.form.get('zone_id', type=int)
    zone = db.session.get(StoreShippingZone, zone_id) if zone_id else None
    if zone_id and zone is None:
        abort(404)
    if zone is not None and request.form.get('delete'):
        db.session.delete(zone)
        db.session.commit()
        flash(f'Removed delivery option {zone.label}.', 'success')
        return redirect(url_for('store.admin_store'))
    try:
        label = ' '.join((request.form.get('label') or '').split())
        if not label or len(label) > 120:
            raise ValueError('Give the delivery option a name, such as "United Kingdom".')
        price_pence = _parse_pounds(request.form.get('price'), allow_zero=True)
        sort_order = int(request.form.get('sort_order') or 0)
    except ValueError as exc:
        message = str(exc)
        flash('Position must be a whole number.' if message.startswith('invalid literal') else message, 'danger')
        return redirect(url_for('store.admin_store'))
    if zone is None:
        zone = StoreShippingZone()
        db.session.add(zone)
        zone.is_active = True
    else:
        zone.is_active = bool(request.form.get('is_active'))
    zone.label, zone.price_pence, zone.sort_order = label, price_pence, sort_order
    db.session.commit()
    flash(f'Saved delivery option {zone.label}.', 'success')
    return redirect(url_for('store.admin_store'))


@store_bp.route('/admin/store/orders/<int:order_id>')
@login_required
def admin_store_order(order_id):
    guard = _admin_guard()
    if guard:
        return guard
    order = db.session.get(StoreOrder, order_id)
    if order is None:
        abort(404)
    from models.payment import SumupCheckout
    checkouts = (SumupCheckout.query
                 .filter(SumupCheckout.checkout_reference.like(f'{order.order_number}-%'))
                 .order_by(SumupCheckout.created_at.desc()).all())
    return render_template('admin_store_order.html', order=order, checkouts=checkouts)


@store_bp.route('/admin/store/orders/<int:order_id>/action', methods=['POST'])
@login_required
def admin_store_order_action(order_id):
    guard = _admin_guard()
    if guard:
        return guard
    order = db.session.get(StoreOrder, order_id)
    if order is None:
        abort(404)
    action = request.form.get('action')
    back = redirect(url_for('store.admin_store_order', order_id=order.id))

    if action == 'sync':
        try:
            store_service.sync_order(order)
            flash(f'SumUp says this order is {order.status}.', 'info')
        except (SumUpAPIError, ValueError) as exc:
            db.session.rollback()
            flash(f"Could not reach SumUp ({getattr(exc, 'status_code', None) or 'no answer'}).", 'danger')
    elif action == 'ship' and order.status in ('PAID', 'SHIPPED'):
        order.tracking_reference = (request.form.get('tracking_reference') or '').strip()[:120] or None
        first_time = order.status == 'PAID'
        order.status = 'SHIPPED'
        order.shipped_at = order.shipped_at or datetime.utcnow()
        db.session.commit()
        if request.form.get('notify') and first_time:
            store_service.send_shipped_email(order)
        flash('Marked as sent.' + (' The customer has been emailed.' if request.form.get('notify') and first_time else ''), 'success')
    elif action == 'cancel' and order.awaiting_payment:
        # Check nothing was paid in the meantime, then close the payment page
        try:
            store_service.sync_order(order)
        except (SumUpAPIError, ValueError):
            db.session.rollback()
        if order.awaiting_payment:
            store_service.retire_checkout(order)
            order.status = 'CANCELLED'
            db.session.commit()
            flash('Order cancelled. Its payment page has been closed.', 'success')
        else:
            flash(f'Not cancelled: SumUp says this order is {order.status}.', 'warning')
    elif action == 'refunded' and order.status in ('PAID', 'SHIPPED'):
        order.status = 'REFUNDED'
        store_service._note(order, f'Marked refunded by {current_user.username}.')
        db.session.commit()
        flash('Marked as refunded. This only records it: send the money back from your SumUp dashboard.', 'warning')
    elif action == 'note':
        order.admin_note = (request.form.get('admin_note') or '').strip() or None
        db.session.commit()
        flash('Note saved.', 'success')
    else:
        flash('That cannot be done to an order in this state.', 'warning')
    return back
