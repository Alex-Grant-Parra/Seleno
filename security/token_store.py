import hashlib
import json
import os
import secrets
from datetime import datetime
from sqlalchemy import inspect, text

from app.db import db


def _get_telescope_model():
    from models.tables import Telescope
    return Telescope


def token_hash(raw_token: str) -> str:
    # Tokens are 256-bit random values (secrets.token_urlsafe(32)), not user-chosen
    # passwords, so a fast deterministic hash is appropriate: brute force is infeasible
    # and a deterministic digest is needed for the indexed token_hash lookup.
    return hashlib.sha256((raw_token or '').encode('utf-8')).hexdigest()


def _parse_created(value):
    if not value:
        return datetime.utcnow()
    try:
        if isinstance(value, str) and value.endswith('Z'):
            value = value[:-1]
        return datetime.fromisoformat(value)
    except Exception:
        return datetime.utcnow()


def create_token_record(name: str, client_type: str = 'observer', **kwargs):
    raw_token = secrets.token_urlsafe(32)
    rec = upsert_raw_token_record(
        raw_token,
        name=name,
        client_type=client_type,
    )
    return raw_token, rec


def upsert_raw_token_record(raw_token: str, *, name: str, client_type: str = 'observer', **kwargs):
    Telescope = _get_telescope_model()
    token_digest = token_hash(raw_token)
    token_name = (name or 'Unknown').strip() or 'Unknown'
    token_type = (client_type or 'observer').strip() or 'observer'

    rec = db.session.query(Telescope).filter_by(token_hash=token_digest).first()
    if rec is None:
        rec = db.session.query(Telescope).filter_by(telescope_id=token_name).first()

    if rec is None:
        rec = Telescope(
            telescope_id=token_name,
            type=token_type,
            last_seen=None,
        )
        db.session.add(rec)

    rec.telescope_id = token_name
    rec.type = token_type
    rec.token_hash = token_digest
    rec.token_prefix = str(raw_token)[:12]
    rec.token_created_at = rec.token_created_at or datetime.utcnow()

    db.session.commit()
    return rec


def list_tokens(include_revoked=False):
    Telescope = _get_telescope_model()
    query = Telescope.query.filter(Telescope.token_hash.isnot(None))
    return query.order_by(Telescope.token_created_at.desc()).all()


def get_token_by_id(token_id):
    Telescope = _get_telescope_model()
    try:
        token_id = int(token_id)
    except Exception:
        return None
    row = db.session.get(Telescope, token_id)
    if row and row.token_hash:
        return row
    return None


def revoke_token_by_id(token_id):
    rec = get_token_by_id(token_id)
    if not rec:
        return None
    # Capture name before deletion so we can disconnect active clients
    telescope_name = getattr(rec, 'telescope_id', None)

    # Delete the entire telescope record instead of just clearing the token
    db.session.delete(rec)
    db.session.commit()

    # Best-effort: disconnect any active websocket client with this telescope name
    try:
        from app.WebsocketServer import disconnect_client_by_id
        if telescope_name:
            try:
                disconnect_client_by_id(telescope_name, reason='token_revoked')
            except Exception:
                pass
            try:
                disconnect_client_by_id(f"{telescope_name}_liveview", reason='token_revoked')
            except Exception:
                pass
    except Exception:
        # If websocket helper isn't available or fails, ignore silently
        pass

    return rec


def verify_token(raw_token: str, *, required_scope=None, client_ip=None, client_id=None):
    Telescope = _get_telescope_model()
    if not raw_token:
        return None, 'missing_token'

    digest = token_hash(raw_token)
    rec = Telescope.query.filter_by(token_hash=digest).first()
    if not rec:
        return None, 'unknown_token'

    if not rec.is_approved:
        return None, 'pending_approval'

    if rec.is_disabled:
        return None, 'telescope_disabled'

    return rec, None


def migrate_json_tokens_to_db(tokens_file='security/api_tokens.json'):
    # Best-effort one-way import from legacy JSON token file into DB
    Telescope = _get_telescope_model()
    ensure_telescope_token_columns()

    if not os.path.exists(tokens_file):
        return {'imported': 0, 'skipped': 0}

    imported = 0
    skipped = 0

    try:
        with open(tokens_file, 'r') as f:
            tokens = json.load(f)
    except Exception:
        return {'imported': 0, 'skipped': 0}

    for raw_token, info in (tokens or {}).items():
        digest = token_hash(raw_token)
        existing = Telescope.query.filter_by(token_hash=digest).first()
        if existing:
            skipped += 1
            continue

        name = (info or {}).get('name') or 'Unknown'
        client_type = (info or {}).get('client_type') or 'observer'

        rec = db.session.query(Telescope).filter_by(telescope_id=name).first()
        if rec is None:
            rec = Telescope(
                telescope_id=name,
                type=client_type,
                last_seen=None,
            )
            db.session.add(rec)

        rec.type = client_type
        rec.token_hash = digest
        rec.token_prefix = str(raw_token)[:12]
        rec.token_created_at = _parse_created((info or {}).get('created'))
        imported += 1

    if imported > 0:
        db.session.commit()

    return {'imported': imported, 'skipped': skipped}


def ensure_telescope_token_columns():
    # Ensure token-related columns exist in telescopes table
    engine = db.engine
    inspector = inspect(engine)

    if 'telescopes' not in inspector.get_table_names():
        return

    existing_cols = {c['name'] for c in inspector.get_columns('telescopes')}

    required_cols = {
        'token_hash': 'VARCHAR(64)',
        'token_prefix': 'VARCHAR(16)',
        'token_created_at': 'DATETIME',
    }

    with engine.begin() as conn:
        for col_name, col_type in required_cols.items():
            if col_name not in existing_cols:
                conn.execute(text(f"ALTER TABLE telescopes ADD COLUMN {col_name} {col_type}"))

        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_telescopes_token_hash ON telescopes(token_hash)"))
        except Exception:
            pass

        # Best-effort cleanup of previously added advanced token columns
        removable_cols = [
            'token_expires_at',
            'token_revoked',
            'token_revoked_at',
            'token_last_used_at',
            'allowed_ips_json',
            'allowed_client_ids_json',
            'scopes_json',
        ]
        for col_name in removable_cols:
            try:
                if col_name in existing_cols:
                    conn.execute(text(f"ALTER TABLE telescopes DROP COLUMN {col_name}"))
            except Exception:
                pass


def migrate_api_token_table_to_telescopes(drop_source_table=True):
    # Migrate legacy api_token rows into telescopes table and optionally drop source table
    Telescope = _get_telescope_model()
    ensure_telescope_token_columns()

    engine = db.engine
    inspector = inspect(engine)
    if 'api_token' not in inspector.get_table_names():
        return {'imported': 0, 'skipped': 0, 'dropped': False}

    imported = 0
    skipped = 0

    with engine.begin() as conn:
        rows = conn.execute(text("SELECT token_hash, token_prefix, name, client_type, created_at FROM api_token")).mappings().all()

    for row in rows:
        digest = row.get('token_hash')
        if not digest:
            skipped += 1
            continue

        existing_by_hash = db.session.query(Telescope).filter_by(token_hash=digest).first()
        if existing_by_hash:
            skipped += 1
            continue

        name = row.get('name') or 'Unknown'
        rec = db.session.query(Telescope).filter_by(telescope_id=name).first()
        if rec is None:
            rec = Telescope(telescope_id=name, type=row.get('client_type') or 'observer', last_seen=None)
            db.session.add(rec)

        rec.type = row.get('client_type') or rec.type
        rec.token_hash = digest
        rec.token_prefix = row.get('token_prefix')
        rec.token_created_at = _parse_created(row.get('created_at'))
        imported += 1

    if imported > 0:
        db.session.commit()

    dropped = False
    if drop_source_table:
        try:
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE IF EXISTS api_token"))
            dropped = True
        except Exception:
            dropped = False

    return {'imported': imported, 'skipped': skipped, 'dropped': dropped}
