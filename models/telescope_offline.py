from datetime import datetime

from app.db import db
from utility.encryption import EncryptedString


class TelescopeOffline(db.Model):
    """What a telescope needs so the seleno.org app can talk to it directly on
    its offline hotspot (app/offline_access.py): its HTTPS name
    (<slug>.tel.<domain>, pointing at the hotspot's address), the secret users'
    offline passes are signed with, and the certificate for that name.

    One row per telescope, made the first time it asks for its credentials or
    a user asks for a pass."""
    __tablename__ = 'telescope_offline'

    telescope_row_id = db.Column(db.Integer, db.ForeignKey('telescopes.id', ondelete='CASCADE'), primary_key=True)
    slug = db.Column(db.String(63), nullable=False, unique=True)
    secret = db.Column(EncryptedString(64), nullable=False)
    cert_pem = db.Column(db.Text, nullable=True)
    key_pem = db.Column(EncryptedString(), nullable=True)
    cert_expires = db.Column(db.DateTime, nullable=True)
    dns_published = db.Column(db.Boolean, nullable=False, default=False)
    last_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


def _drop_with_telescope(mapper, connection, target):
    # SQLite here doesn't enforce ON DELETE CASCADE, and telescope ids get reused
    connection.execute(TelescopeOffline.__table__.delete().where(TelescopeOffline.telescope_row_id == target.id))


def _register_cleanup():
    from sqlalchemy import event

    from models.tables import Telescope

    if not event.contains(Telescope, "after_delete", _drop_with_telescope):
        event.listen(Telescope, "after_delete", _drop_with_telescope)


try:
    _register_cleanup()
except Exception:  # models.tables imports lazily; app/offline_access.py registers it again
    pass
