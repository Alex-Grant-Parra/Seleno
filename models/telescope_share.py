from datetime import datetime

from app.db import db


class TelescopeShare(db.Model):
    """Another user the owner has given standing access to their telescope.

    They may use it at any time as the owner would, without a booking, but they
    can't manage its bookings, change its booking settings, remove it, or share it
    with anyone else: that stays with the owner (Telescope.user_id)."""
    __tablename__ = 'telescope_shares'
    __table_args__ = (db.UniqueConstraint('telescope_id', 'user_id', name='uq_telescope_share_user'),)

    id = db.Column(db.Integer, primary_key=True)
    telescope_id = db.Column(db.Integer, db.ForeignKey('telescopes.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    granted_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship('User', foreign_keys=[user_id], lazy='joined')


def _drop_shares(column):
    # SQLite here doesn't enforce ON DELETE CASCADE and reuses ids, so a share left
    # behind could open a later telescope or account to the wrong person
    def listener(mapper, connection, target):
        connection.execute(TelescopeShare.__table__.delete().where(column == target.id))
    return listener


def _register_cleanup():
    from sqlalchemy import event
    from models.tables import Telescope
    from models.user import User
    event.listen(Telescope, 'before_delete', _drop_shares(TelescopeShare.__table__.c.telescope_id))
    event.listen(User, 'before_delete', _drop_shares(TelescopeShare.__table__.c.user_id))


_register_cleanup()
