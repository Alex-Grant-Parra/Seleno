import hmac
import json
import os
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlencode

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.db import db
from app.sumup_service import (
    SumUpAPIError,
    current_sumup_mode,
    create_checkout,
    deactivate_checkout,
    get_available_payment_methods,
    is_sumup_feature_enabled,
    resolve_sumup_credentials,
    retrieve_checkout,
)
from models.payment import SumupCheckout, SumupEvent, SumupTransaction


payments_bp = Blueprint('payments', __name__)

# Currency is intentionally fixed for all checkouts in this deployment.
CHECKOUT_CURRENCY = 'GBP'

MAX_HISTORY_LIMIT = 200


def _parse_iso_timestamp(raw):
    if not raw:
        return None
    candidate = str(raw).strip()
    if not candidate:
        return None
    if candidate.endswith('Z'):
        candidate = candidate[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(candidate)
        return parsed.isoformat()
    except Exception:
        return None


def _parse_amount(value):
    try:
        amount = Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('amount must be a valid decimal number with up to 2 decimal places')

    if amount <= Decimal('0.00'):
        raise ValueError('amount must be greater than 0')
    if amount > Decimal('1000000.00'):
        raise ValueError('amount is too large')

    return amount


def _get_client_ip():
    xff = request.headers.get('X-Forwarded-For')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr


def _webhook_secret_is_valid():
    configured = (current_app.config.get('SUMUP_WEBHOOK_SECRET') or os.getenv('SUMUP_WEBHOOK_SECRET') or '').strip()

    if not configured:
        # If no secret configured, treat as disabled but accepted.
        return True

    token = (request.args.get('token') or request.headers.get('X-Sumup-Webhook-Token') or '').strip()
    return hmac.compare_digest(configured, token)


def _build_webhook_return_url():
    app_domain = (current_app.config.get('APP_DOMAIN') or '').strip()
    if app_domain:
        base = f'https://{app_domain}'
    else:
        base = request.url_root.rstrip('/')

    url = f'{base}/payments/sumup/webhook'
    webhook_secret = (current_app.config.get('SUMUP_WEBHOOK_SECRET') or os.getenv('SUMUP_WEBHOOK_SECRET') or '').strip()
    if webhook_secret:
        url = f'{url}?{urlencode({"token": webhook_secret})}'
    return url


def _build_frontend_base_url():
    app_domain = (current_app.config.get('APP_DOMAIN') or '').strip()
    if app_domain:
        return f'https://{app_domain}'
    return request.url_root.rstrip('/')


def _build_checkout_return_url(checkout_reference):
    base = _build_frontend_base_url()
    return f'{base}/payments/return?checkout_reference={checkout_reference}'


def _serialize_checkout(row):
    tx_rows = sorted(row.transactions, key=lambda r: (r.timestamp or datetime.min), reverse=True)
    return {
        'id': row.id,
        'checkoutId': row.checkout_id,
        'checkoutReference': row.checkout_reference,
        'idempotencyKey': row.idempotency_key,
        'status': row.status,
        'amount': float(row.amount) if row.amount is not None else None,
        'currency': row.currency,
        'purpose': row.purpose,
        'merchantCode': row.merchant_code,
        'hostedCheckoutUrl': row.hosted_checkout_url,
        'transactionCode': row.transaction_code,
        'transactionId': row.transaction_id,
        'isTestMode': row.is_test_mode,
        'createdAt': row.created_at.isoformat() if row.created_at else None,
        'updatedAt': row.updated_at.isoformat() if row.updated_at else None,
        'lastSyncedAt': row.last_synced_at.isoformat() if row.last_synced_at else None,
        'transactions': [
            {
                'id': tx.id,
                'sumupTransactionId': tx.sumup_transaction_id,
                'transactionCode': tx.transaction_code,
                'status': tx.status,
                'paymentType': tx.payment_type,
                'amount': float(tx.amount) if tx.amount is not None else None,
                'currency': tx.currency,
                'timestamp': tx.timestamp.isoformat() if tx.timestamp else None,
                'merchantCode': tx.merchant_code,
                'installmentsCount': tx.installments_count,
                'vatAmount': float(tx.vat_amount) if tx.vat_amount is not None else None,
                'tipAmount': float(tx.tip_amount) if tx.tip_amount is not None else None,
                'entryMode': tx.entry_mode,
                'authCode': tx.auth_code,
            }
            for tx in tx_rows
        ],
    }


def _as_bool(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _log_event(**kwargs):
    SumupEvent.log_event(**kwargs)


def _sync_checkout_from_sumup(checkout_id, *, is_test_mode, event_source, actor_ip, event_type=None):
    creds = resolve_sumup_credentials()
    remote = retrieve_checkout(creds['api_key'], checkout_id)
    existing = SumupCheckout.query.filter_by(checkout_id=checkout_id).first()
    row = SumupCheckout.upsert_from_sumup(
        remote,
        user_id=(existing.user_id if existing else None),
        is_test_mode=is_test_mode,
    )
    _log_event(
        checkout_record_id=row.id,
        event_source=event_source,
        event_type=event_type or 'CHECKOUT_SYNCED',
        verification_status='verified',
        remote_ip=actor_ip,
        response_payload=remote,
    )
    return row


def _admin_guard():
    if not current_user.is_authenticated or not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('profile.profile'))
    return None


def _feature_disabled_response():
    return jsonify({'status': 'error', 'message': 'Payments feature is currently disabled.'}), 503


@payments_bp.route('/payments/sumup/checkout', methods=['POST'])
@login_required
def sumup_create_checkout():
    if not is_sumup_feature_enabled():
        return _feature_disabled_response()

    payload = request.get_json(silent=True) or request.form.to_dict(flat=True)
    metadata = payload.get('metadata')

    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            return jsonify({'status': 'error', 'message': 'metadata must be valid JSON when provided as a string'}), 400
    if metadata is not None and not isinstance(metadata, dict):
        return jsonify({'status': 'error', 'message': 'metadata must be an object'}), 400

    try:
        amount = _parse_amount(payload.get('amount'))
    except ValueError as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 400

    currency = CHECKOUT_CURRENCY

    checkout_reference = (payload.get('checkoutReference') or payload.get('checkout_reference') or str(uuid.uuid4())).strip()
    if len(checkout_reference) > 90:
        return jsonify({'status': 'error', 'message': 'checkoutReference must be at most 90 characters'}), 400

    idempotency_key = (payload.get('idempotencyKey') or payload.get('idempotency_key') or '').strip() or None
    if idempotency_key and len(idempotency_key) > 128:
        return jsonify({'status': 'error', 'message': 'idempotencyKey must be at most 128 characters'}), 400

    if idempotency_key:
        existing = SumupCheckout.query.filter_by(idempotency_key=idempotency_key, user_id=current_user.id).first()
        if existing:
            return jsonify({'status': 'success', 'checkout': _serialize_checkout(existing), 'idempotentReplay': True}), 200

    purpose = str(payload.get('purpose') or 'CHECKOUT').strip().upper()
    if purpose not in {'CHECKOUT', 'SETUP_RECURRING_PAYMENT'}:
        return jsonify({'status': 'error', 'message': 'purpose must be CHECKOUT or SETUP_RECURRING_PAYMENT'}), 400

    valid_until = _parse_iso_timestamp(payload.get('validUntil') or payload.get('valid_until'))
    redirect_url = (payload.get('redirectUrl') or payload.get('redirect_url') or '').strip() or None
    description = (payload.get('description') or 'Seleno payment checkout').strip()
    customer_id = (payload.get('customerId') or payload.get('customer_id') or '').strip() or None
    hosted_checkout_enabled = _as_bool(payload.get('hostedCheckout', payload.get('hosted_checkout')), default=True)
    if not hosted_checkout_enabled:
        return jsonify({'status': 'error', 'message': 'This endpoint supports Hosted Checkout only. Set hostedCheckout to true.'}), 400

    if not redirect_url:
        redirect_url = _build_checkout_return_url(checkout_reference)

    try:
        creds = resolve_sumup_credentials()
    except SumUpAPIError as exc:
        return jsonify({'status': 'error', 'message': exc.message}), 500

    request_payload = {
        'checkout_reference': checkout_reference,
        'amount': float(amount),
        'currency': currency,
        'merchant_code': creds['merchant_code'],
        'description': description,
        'purpose': purpose,
        'return_url': _build_webhook_return_url(),
        'hosted_checkout': {'enabled': hosted_checkout_enabled},
    }

    if redirect_url:
        request_payload['redirect_url'] = redirect_url
    if customer_id:
        request_payload['customer_id'] = customer_id
    if valid_until:
        request_payload['valid_until'] = valid_until

    try:
        remote = create_checkout(creds['api_key'], request_payload)
        row = SumupCheckout.upsert_from_sumup(
            remote,
            user_id=current_user.id,
            metadata=metadata,
            request_payload=request_payload,
            is_test_mode=creds['is_test_mode'],
            idempotency_key=idempotency_key,
        )
        _log_event(
            checkout_record_id=row.id,
            event_source='api',
            event_type='CHECKOUT_CREATED',
            verification_status='created',
            remote_ip=_get_client_ip(),
            request_payload=request_payload,
            response_payload=remote,
        )
        db.session.commit()
        return jsonify({'status': 'success', 'checkout': _serialize_checkout(row)}), 201
    except SumUpAPIError as exc:
        db.session.rollback()
        return jsonify({
            'status': 'error',
            'message': 'SumUp checkout creation failed',
            'sumupStatusCode': exc.status_code,
            'sumupError': exc.response_payload,
        }), 502
    except Exception as exc:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(exc)}), 500


@payments_bp.route('/payments/sumup/checkout/<string:checkout_id>', methods=['GET'])
@login_required
def sumup_get_checkout(checkout_id):
    row = SumupCheckout.query.filter_by(checkout_id=checkout_id).first()
    if not row:
        return jsonify({'status': 'error', 'message': 'Checkout not found in local storage'}), 404

    if row.user_id is not None and row.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Not allowed'}), 403

    refresh = str(request.args.get('refresh') or 'false').lower() in {'1', 'true', 'yes'}
    if refresh:
        if not is_sumup_feature_enabled():
            return _feature_disabled_response()
        try:
            creds = resolve_sumup_credentials()
            remote = retrieve_checkout(creds['api_key'], checkout_id)
            row = SumupCheckout.upsert_from_sumup(
                remote,
                user_id=row.user_id,
                is_test_mode=creds['is_test_mode'],
            )
            _log_event(
                checkout_record_id=row.id,
                event_source='sync',
                event_type='CHECKOUT_REFRESHED',
                verification_status='verified',
                remote_ip=_get_client_ip(),
                response_payload=remote,
            )
            db.session.commit()
        except SumUpAPIError as exc:
            db.session.rollback()
            return jsonify({'status': 'error', 'message': 'Unable to refresh from SumUp', 'sumupStatusCode': exc.status_code}), 502

    return jsonify({'status': 'success', 'checkout': _serialize_checkout(row)}), 200


@payments_bp.route('/payments/sumup/checkout/<string:checkout_id>/sync', methods=['POST'])
@login_required
def sumup_sync_checkout(checkout_id):
    if not is_sumup_feature_enabled():
        return _feature_disabled_response()

    row = SumupCheckout.query.filter_by(checkout_id=checkout_id).first()
    if not row:
        return jsonify({'status': 'error', 'message': 'Checkout not found in local storage'}), 404

    if row.user_id is not None and row.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Not allowed'}), 403

    try:
        creds = resolve_sumup_credentials()
        row = _sync_checkout_from_sumup(
            checkout_id,
            is_test_mode=creds['is_test_mode'],
            event_source='sync',
            actor_ip=_get_client_ip(),
            event_type='CHECKOUT_SYNC_REQUEST',
        )
        db.session.commit()
        return jsonify({'status': 'success', 'checkout': _serialize_checkout(row)}), 200
    except SumUpAPIError as exc:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': 'SumUp sync failed', 'sumupStatusCode': exc.status_code}), 502
    except Exception as exc:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(exc)}), 500


@payments_bp.route('/payments/sumup/checkout/<string:checkout_id>/deactivate', methods=['POST'])
@login_required
def sumup_deactivate(checkout_id):
    if not is_sumup_feature_enabled():
        return _feature_disabled_response()

    row = SumupCheckout.query.filter_by(checkout_id=checkout_id).first()
    if not row:
        return jsonify({'status': 'error', 'message': 'Checkout not found in local storage'}), 404

    if row.user_id is not None and row.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Not allowed'}), 403

    try:
        creds = resolve_sumup_credentials()
        remote = deactivate_checkout(creds['api_key'], checkout_id)
        row = SumupCheckout.upsert_from_sumup(
            remote,
            user_id=row.user_id,
            is_test_mode=creds['is_test_mode'],
        )
        _log_event(
            checkout_record_id=row.id,
            event_source='api',
            event_type='CHECKOUT_DEACTIVATED',
            verification_status='deactivated',
            remote_ip=_get_client_ip(),
            response_payload=remote,
        )
        db.session.commit()
        return jsonify({'status': 'success', 'checkout': _serialize_checkout(row)}), 200
    except SumUpAPIError as exc:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': 'SumUp deactivation failed', 'sumupStatusCode': exc.status_code}), 502


@payments_bp.route('/payments/sumup/payment-methods', methods=['GET'])
@login_required
def sumup_payment_methods():
    if not is_sumup_feature_enabled():
        return _feature_disabled_response()

    amount_raw = request.args.get('amount')
    currency = CHECKOUT_CURRENCY

    amount = None
    if amount_raw:
        try:
            amount = float(_parse_amount(amount_raw))
        except ValueError as exc:
            return jsonify({'status': 'error', 'message': str(exc)}), 400

    try:
        creds = resolve_sumup_credentials()
        remote = get_available_payment_methods(
            creds['api_key'],
            creds['merchant_code'],
            amount=amount,
            currency=currency,
        )
        return jsonify({'status': 'success', 'result': remote}), 200
    except SumUpAPIError as exc:
        return jsonify({'status': 'error', 'message': 'Failed to fetch payment methods', 'sumupStatusCode': exc.status_code}), 502


@payments_bp.route('/payments/sumup/history', methods=['GET'])
@login_required
def sumup_history():
    try:
        limit = int(request.args.get('limit', 50))
    except Exception:
        limit = 50
    limit = max(1, min(limit, MAX_HISTORY_LIMIT))

    query = SumupCheckout.query
    if not current_user.is_admin:
        query = query.filter_by(user_id=current_user.id)

    status = (request.args.get('status') or '').strip().upper()
    if status:
        query = query.filter_by(status=status)

    rows = query.order_by(SumupCheckout.created_at.desc()).limit(limit).all()
    return jsonify({'status': 'success', 'count': len(rows), 'checkouts': [_serialize_checkout(row) for row in rows]}), 200


@payments_bp.route('/payments/return', methods=['GET'])
def payment_return_page():
    checkout_id = (request.args.get('checkout_id') or request.args.get('checkoutId') or '').strip()
    checkout_reference = (request.args.get('checkout_reference') or request.args.get('checkoutReference') or '').strip()

    row = None
    if checkout_id:
        row = SumupCheckout.query.filter_by(checkout_id=checkout_id).first()
    if row is None and checkout_reference:
        row = SumupCheckout.query.filter_by(checkout_reference=checkout_reference).first()

    if row is None:
        return render_template(
            'payment_return.html',
            checkout=None,
            checkout_status='UNKNOWN',
            final_message='We could not find this payment locally yet. If you just finished payment, refresh in a few seconds.',
            is_paid=False,
            can_retry=True,
        )

    is_authenticated = bool(getattr(current_user, 'is_authenticated', False))
    is_admin = bool(is_authenticated and getattr(current_user, 'is_admin', False))
    is_owner = bool(is_authenticated and row.user_id is not None and row.user_id == getattr(current_user, 'id', None))

    if row.user_id is not None and not (is_owner or is_admin):
        return render_template(
            'payment_return.html',
            checkout=None,
            checkout_status='FORBIDDEN',
            final_message='This payment does not belong to your account.',
            is_paid=False,
            can_retry=False,
        ), 403

    if is_sumup_feature_enabled():
        try:
            creds = resolve_sumup_credentials()
            remote = retrieve_checkout(creds['api_key'], row.checkout_id)
            row = SumupCheckout.upsert_from_sumup(
                remote,
                user_id=row.user_id,
                is_test_mode=creds['is_test_mode'],
            )
            _log_event(
                checkout_record_id=row.id,
                event_source='sync',
                event_type='CHECKOUT_RETURN_PAGE_SYNC',
                verification_status='verified',
                remote_ip=_get_client_ip(),
                response_payload=remote,
            )
            db.session.commit()
        except Exception:
            db.session.rollback()

    status = (row.status or 'UNKNOWN').upper()
    is_paid = status == 'PAID'
    can_retry = status in {'FAILED', 'EXPIRED', 'PENDING'}

    if is_paid:
        final_message = 'Payment confirmed. Thank you!'
    elif status == 'PENDING':
        final_message = 'Payment is still processing. Please refresh shortly.'
    elif status == 'FAILED':
        final_message = 'Payment failed. Please retry with another method or card.'
    elif status == 'EXPIRED':
        final_message = 'This checkout expired. Please start a new checkout.'
    else:
        final_message = 'Payment state is not final yet. Please refresh in a moment.'

    return render_template(
        'payment_return.html',
        checkout=_serialize_checkout(row),
        checkout_status=status,
        final_message=final_message,
        is_paid=is_paid,
        can_retry=can_retry,
    )


@payments_bp.route('/admin/payments', methods=['GET'])
@login_required
def admin_payments_page():
    guard = _admin_guard()
    if guard:
        return guard

    try:
        page = int(request.args.get('page', 1))
    except Exception:
        page = 1
    page = max(page, 1)

    try:
        page_size = int(request.args.get('page_size', 25))
    except Exception:
        page_size = 25
    page_size = max(10, min(page_size, 200))

    query = SumupCheckout.query
    status_filter = (request.args.get('status') or '').strip().upper()
    mode_filter = (request.args.get('mode') or '').strip().lower()
    ref_query = (request.args.get('q') or '').strip()

    if status_filter:
        query = query.filter(SumupCheckout.status == status_filter)
    if mode_filter == 'test':
        query = query.filter(SumupCheckout.is_test_mode.is_(True))
    elif mode_filter == 'live':
        query = query.filter(SumupCheckout.is_test_mode.is_(False))

    if ref_query:
        like_q = f'%{ref_query}%'
        query = query.filter(
            (SumupCheckout.checkout_reference.ilike(like_q)) |
            (SumupCheckout.checkout_id.ilike(like_q)) |
            (SumupCheckout.transaction_code.ilike(like_q))
        )

    total = query.count()
    rows = query.order_by(SumupCheckout.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    total_pages = max((total + page_size - 1) // page_size, 1)

    return render_template(
        'admin_payments.html',
        checkouts=rows,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        status_filter=status_filter,
        mode_filter=mode_filter,
        q=ref_query,
    )


@payments_bp.route('/admin/payments/<int:checkout_row_id>', methods=['GET'])
@login_required
def admin_payment_detail_page(checkout_row_id):
    guard = _admin_guard()
    if guard:
        return guard

    row = db.session.get(SumupCheckout, checkout_row_id)
    if not row:
        flash('Payment checkout row not found.', 'danger')
        return redirect(url_for('payments.admin_payments_page'))

    event_rows = (
        SumupEvent.query
        .filter(SumupEvent.checkout_record_id == row.id)
        .order_by(SumupEvent.created_at.desc())
        .limit(200)
        .all()
    )
    tx_rows = (
        SumupTransaction.query
        .filter(SumupTransaction.checkout_record_id == row.id)
        .order_by(SumupTransaction.timestamp.desc(), SumupTransaction.id.desc())
        .all()
    )

    return render_template(
        'admin_payment_detail.html',
        checkout=row,
        events=event_rows,
        transactions=tx_rows,
    )


@payments_bp.route('/admin/payments/sandbox', methods=['GET'])
@login_required
def admin_sandbox_payments_page():
    guard = _admin_guard()
    if guard:
        return guard

    mode = 'unknown'
    try:
        mode = current_sumup_mode()
    except Exception:
        pass

    recent = (
        SumupCheckout.query
        .filter(SumupCheckout.is_test_mode.is_(True))
        .order_by(SumupCheckout.created_at.desc())
        .limit(20)
        .all()
    )

    return render_template('admin_payment_sandbox.html', sumup_mode=mode, recent_checkouts=recent)


@payments_bp.route('/admin/payments/sandbox/create', methods=['POST'])
@login_required
def admin_sandbox_create_payment():
    guard = _admin_guard()
    if guard:
        return guard

    if not is_sumup_feature_enabled():
        flash('Payments feature is currently disabled.', 'danger')
        return redirect(url_for('payments.admin_sandbox_payments_page'))

    try:
        mode = current_sumup_mode()
    except SumUpAPIError as exc:
        flash(exc.message, 'danger')
        return redirect(url_for('payments.admin_sandbox_payments_page'))

    if mode != 'sandbox':
        flash("Sandbox lab requires SUMUP_MODE='sandbox' in app/sumup_service.py.", 'warning')
        return redirect(url_for('payments.admin_sandbox_payments_page'))

    amount_raw = (request.form.get('amount') or '').strip()
    currency = CHECKOUT_CURRENCY
    description = (request.form.get('description') or 'Admin sandbox payment test').strip()

    try:
        amount = _parse_amount(amount_raw)
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('payments.admin_sandbox_payments_page'))

    checkout_reference = f"admin-sandbox-{uuid.uuid4()}"
    redirect_url = _build_checkout_return_url(checkout_reference)

    try:
        creds = resolve_sumup_credentials()
        request_payload = {
            'checkout_reference': checkout_reference,
            'amount': float(amount),
            'currency': currency,
            'merchant_code': creds['merchant_code'],
            'description': description,
            'purpose': 'CHECKOUT',
            'return_url': _build_webhook_return_url(),
            'redirect_url': redirect_url,
            'hosted_checkout': {'enabled': True},
        }

        remote = create_checkout(creds['api_key'], request_payload)
        row = SumupCheckout.upsert_from_sumup(
            remote,
            user_id=current_user.id,
            metadata={'adminSandboxLab': True, 'createdByAdminId': current_user.id},
            request_payload=request_payload,
            is_test_mode=True,
            idempotency_key=None,
        )
        _log_event(
            checkout_record_id=row.id,
            event_source='api',
            event_type='ADMIN_SANDBOX_CHECKOUT_CREATED',
            verification_status='created',
            remote_ip=_get_client_ip(),
            request_payload=request_payload,
            response_payload=remote,
        )
        db.session.commit()

        hosted_url = (remote or {}).get('hosted_checkout_url')
        if not hosted_url:
            flash('Checkout created, but SumUp did not return hosted_checkout_url.', 'warning')
            return redirect(url_for('payments.admin_payment_detail_page', checkout_row_id=row.id))

        return redirect(hosted_url)
    except SumUpAPIError as exc:
        db.session.rollback()
        flash(f"SumUp request failed ({exc.status_code or 'n/a'}).", 'danger')
        return redirect(url_for('payments.admin_sandbox_payments_page'))
    except Exception as exc:
        db.session.rollback()
        flash(f'Failed to create sandbox checkout: {exc}', 'danger')
        return redirect(url_for('payments.admin_sandbox_payments_page'))


@payments_bp.route('/payments/sumup/webhook', methods=['POST'])
def sumup_webhook():
    if not is_sumup_feature_enabled():
        # Acknowledge delivery to avoid webhook retry storm while feature is disabled.
        return ('', 204)

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}

    event_type = payload.get('event_type')
    checkout_id = payload.get('id')
    actor_ip = _get_client_ip()

    if not _webhook_secret_is_valid():
        _log_event(
            checkout_record_id=None,
            event_source='webhook',
            event_type=event_type,
            verification_status='rejected',
            remote_ip=actor_ip,
            request_headers=dict(request.headers),
            request_payload=payload,
        )
        db.session.commit()
        return jsonify({'status': 'error', 'message': 'Unauthorized webhook'}), 401

    if not checkout_id:
        _log_event(
            checkout_record_id=None,
            event_source='webhook',
            event_type=event_type,
            verification_status='ignored_missing_checkout_id',
            remote_ip=actor_ip,
            request_headers=dict(request.headers),
            request_payload=payload,
        )
        db.session.commit()
        return ('', 204)

    try:
        creds = resolve_sumup_credentials()
        remote = retrieve_checkout(creds['api_key'], checkout_id)
        existing = SumupCheckout.query.filter_by(checkout_id=checkout_id).first()
        row = SumupCheckout.upsert_from_sumup(
            remote,
            user_id=(existing.user_id if existing else None),
            is_test_mode=creds['is_test_mode'],
        )
        _log_event(
            checkout_record_id=row.id,
            event_source='webhook',
            event_type=event_type,
            verification_status='verified',
            remote_ip=actor_ip,
            request_headers=dict(request.headers),
            request_payload=payload,
            response_payload=remote,
        )
        db.session.commit()
        return ('', 204)
    except SumUpAPIError:
        db.session.rollback()
        return ('', 500)
    except Exception:
        db.session.rollback()
        return ('', 500)
