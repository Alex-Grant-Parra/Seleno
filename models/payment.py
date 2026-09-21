import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy import UniqueConstraint, inspect, text

from app.db import db


def _parse_iso_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None

    candidate = value.strip()
    if candidate.endswith('Z'):
        candidate = candidate[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(candidate)
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except Exception:
        return None


def _to_decimal(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


class SumupCheckout(db.Model):
    __tablename__ = 'sumup_checkouts'

    id = db.Column(db.Integer, primary_key=True)
    checkout_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    checkout_reference = db.Column(db.String(90), unique=True, nullable=False, index=True)
    idempotency_key = db.Column(db.String(128), nullable=True, index=True)

    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'), nullable=True, index=True)

    merchant_code = db.Column(db.String(32), nullable=False, index=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    currency = db.Column(db.String(3), nullable=False, index=True)
    purpose = db.Column(db.String(32), nullable=False, default='CHECKOUT')
    status = db.Column(db.String(32), nullable=False, default='PENDING', index=True)

    description = db.Column(db.Text, nullable=True)
    customer_id = db.Column(db.String(128), nullable=True)
    redirect_url = db.Column(db.Text, nullable=True)
    return_url = db.Column(db.Text, nullable=True)
    hosted_checkout_url = db.Column(db.Text, nullable=True)

    transaction_code = db.Column(db.String(64), nullable=True, index=True)
    transaction_id = db.Column(db.String(64), nullable=True, index=True)

    is_test_mode = db.Column(db.Boolean, nullable=False, default=False)

    valid_until = db.Column(db.DateTime, nullable=True, index=True)
    sumup_created_at = db.Column(db.DateTime, nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=True, index=True)

    request_payload_json = db.Column(db.Text, nullable=True)
    response_payload_json = db.Column(db.Text, nullable=True)
    metadata_json = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    transactions = db.relationship('SumupTransaction', backref='checkout', lazy='select', cascade='all, delete-orphan')
    events = db.relationship('SumupEvent', backref='checkout', lazy='select', cascade='all, delete-orphan')

    @classmethod
    def upsert_from_sumup(cls, payload, user_id=None, metadata=None, request_payload=None, is_test_mode=False, idempotency_key=None):
        checkout_id = (payload or {}).get('id')
        checkout_reference = (payload or {}).get('checkout_reference')
        if not checkout_id or not checkout_reference:
            raise ValueError('SumUp payload missing id or checkout_reference')

        row = cls.query.filter_by(checkout_id=checkout_id).first()
        if row is None:
            row = cls.query.filter_by(checkout_reference=checkout_reference).first()

        if row is None:
            row = cls(
                checkout_id=checkout_id,
                checkout_reference=checkout_reference,
                user_id=user_id,
            )
            db.session.add(row)

        row.checkout_id = checkout_id
        row.checkout_reference = checkout_reference
        row.idempotency_key = row.idempotency_key or idempotency_key
        row.user_id = row.user_id if row.user_id is not None else user_id

        row.merchant_code = (payload.get('merchant_code') or row.merchant_code or '').strip()
        row.amount = _to_decimal(payload.get('amount')) or row.amount
        row.currency = (payload.get('currency') or row.currency or '').strip().upper()
        row.purpose = (payload.get('purpose') or row.purpose or 'CHECKOUT').strip().upper()
        row.status = (payload.get('status') or row.status or 'PENDING').strip().upper()

        row.description = payload.get('description')
        row.customer_id = payload.get('customer_id')
        row.redirect_url = payload.get('redirect_url')
        row.return_url = payload.get('return_url')
        row.hosted_checkout_url = payload.get('hosted_checkout_url')

        row.transaction_code = payload.get('transaction_code')
        row.transaction_id = payload.get('transaction_id')

        row.valid_until = _parse_iso_datetime(payload.get('valid_until'))
        row.sumup_created_at = _parse_iso_datetime(payload.get('date')) or row.sumup_created_at
        row.last_synced_at = datetime.utcnow()
        row.is_test_mode = bool(is_test_mode)

        if request_payload is not None:
            row.request_payload_json = json.dumps(request_payload)
        row.response_payload_json = json.dumps(payload)
        if metadata is not None:
            row.metadata_json = json.dumps(metadata)

        SumupTransaction.sync_for_checkout(row, payload.get('transactions') or [])
        return row


class SumupTransaction(db.Model):
    __tablename__ = 'sumup_transactions'
    __table_args__ = (
        UniqueConstraint('checkout_record_id', 'sumup_transaction_id', name='uq_sumup_checkout_transaction'),
    )

    id = db.Column(db.Integer, primary_key=True)
    checkout_record_id = db.Column(db.Integer, db.ForeignKey('sumup_checkouts.id', ondelete='CASCADE'), nullable=False, index=True)

    sumup_transaction_id = db.Column(db.String(64), nullable=False, index=True)
    transaction_code = db.Column(db.String(64), nullable=True, index=True)
    status = db.Column(db.String(32), nullable=True, index=True)
    payment_type = db.Column(db.String(32), nullable=True, index=True)

    amount = db.Column(db.Numeric(12, 2), nullable=True)
    currency = db.Column(db.String(3), nullable=True)
    timestamp = db.Column(db.DateTime, nullable=True, index=True)
    merchant_code = db.Column(db.String(32), nullable=True, index=True)

    installments_count = db.Column(db.Integer, nullable=True)
    vat_amount = db.Column(db.Numeric(12, 2), nullable=True)
    tip_amount = db.Column(db.Numeric(12, 2), nullable=True)
    entry_mode = db.Column(db.String(64), nullable=True)
    auth_code = db.Column(db.String(64), nullable=True)

    raw_payload_json = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    @classmethod
    def sync_for_checkout(cls, checkout_row, transactions):
        for tx in transactions:
            tx_id = (tx or {}).get('id')
            if not tx_id:
                continue

            row = cls.query.filter_by(
                checkout_record_id=checkout_row.id,
                sumup_transaction_id=tx_id,
            ).first()

            if row is None:
                row = cls(
                    checkout_record_id=checkout_row.id,
                    sumup_transaction_id=tx_id,
                )
                db.session.add(row)

            row.transaction_code = tx.get('transaction_code')
            row.status = tx.get('status')
            row.payment_type = tx.get('payment_type')
            row.amount = _to_decimal(tx.get('amount'))
            row.currency = tx.get('currency')
            row.timestamp = _parse_iso_datetime(tx.get('timestamp'))
            row.merchant_code = tx.get('merchant_code')

            row.installments_count = tx.get('installments_count')
            row.vat_amount = _to_decimal(tx.get('vat_amount'))
            row.tip_amount = _to_decimal(tx.get('tip_amount'))
            row.entry_mode = tx.get('entry_mode')
            row.auth_code = tx.get('auth_code')
            row.raw_payload_json = json.dumps(tx)


class SumupEvent(db.Model):
    __tablename__ = 'sumup_events'

    id = db.Column(db.Integer, primary_key=True)
    checkout_record_id = db.Column(db.Integer, db.ForeignKey('sumup_checkouts.id', ondelete='CASCADE'), nullable=True, index=True)

    event_source = db.Column(db.String(32), nullable=False, index=True)  # api|webhook|sync
    event_type = db.Column(db.String(128), nullable=True, index=True)
    verification_status = db.Column(db.String(32), nullable=True, index=True)

    remote_ip = db.Column(db.String(64), nullable=True, index=True)
    request_headers_json = db.Column(db.Text, nullable=True)
    request_payload_json = db.Column(db.Text, nullable=True)
    response_payload_json = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    @classmethod
    def log_event(
        cls,
        *,
        checkout_record_id,
        event_source,
        event_type=None,
        verification_status=None,
        remote_ip=None,
        request_headers=None,
        request_payload=None,
        response_payload=None,
    ):
        row = cls(
            checkout_record_id=checkout_record_id,
            event_source=event_source,
            event_type=event_type,
            verification_status=verification_status,
            remote_ip=remote_ip,
            request_headers_json=json.dumps(request_headers) if request_headers is not None else None,
            request_payload_json=json.dumps(request_payload) if request_payload is not None else None,
            response_payload_json=json.dumps(response_payload) if response_payload is not None else None,
        )
        db.session.add(row)
        return row


def ensure_sumup_payment_indexes():
    engine = db.engine
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    required = {'sumup_checkouts', 'sumup_transactions', 'sumup_events'}
    if not required.issubset(tables):
        return

    with engine.begin() as conn:
        conn.execute(text('CREATE INDEX IF NOT EXISTS idx_sumup_checkouts_status ON sumup_checkouts(status)'))
        conn.execute(text('CREATE INDEX IF NOT EXISTS idx_sumup_checkouts_reference ON sumup_checkouts(checkout_reference)'))
        conn.execute(text('CREATE INDEX IF NOT EXISTS idx_sumup_transactions_status ON sumup_transactions(status)'))
        conn.execute(text('CREATE INDEX IF NOT EXISTS idx_sumup_events_source ON sumup_events(event_source)'))
