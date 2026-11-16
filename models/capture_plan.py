import json
from datetime import datetime

from app.db import db

# A plan in one of these states is still on the telescope
ACTIVE_PLAN_STATES = ('starting', 'running', 'paused', 'waiting')
FINAL_PLAN_STATES = ('done', 'stopped', 'failed')


class CapturePlan(db.Model):
    """A capture plan sent to a telescope, and how far it got."""
    __tablename__ = 'capture_plans'

    id = db.Column(db.String(64), primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    started_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'), nullable=True)
    telescope_id = db.Column(db.Integer, db.ForeignKey('telescopes.id', ondelete='CASCADE'), nullable=False, index=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('telescope_bookings.id', ondelete='SET NULL'), nullable=True)

    name = db.Column(db.String(255), nullable=False)
    plan_json = db.Column(db.Text, nullable=False)
    state = db.Column(db.String(16), nullable=False, default='starting', index=True)
    end_by = db.Column(db.DateTime, nullable=True, index=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    last_status_json = db.Column(db.Text, nullable=True)
    frames_done = db.Column(db.Integer, nullable=True)
    # Seconds per frame beyond exposure and interval, measured once the plan finished
    overhead_s = db.Column(db.Float, nullable=True)
    stop_requested_at = db.Column(db.DateTime, nullable=True)

    @property
    def plan(self):
        try:
            return json.loads(self.plan_json or '{}')
        except ValueError:
            return {}

    @property
    def last_status(self):
        try:
            return json.loads(self.last_status_json) if self.last_status_json else None
        except ValueError:
            return None

    def to_dict(self):
        plan = self.plan
        return {
            'id': self.id,
            'name': self.name,
            'state': self.state,
            'owner_id': self.owner_id,
            'booking_id': self.booking_id,
            'end_by': self.end_by.isoformat() + 'Z' if self.end_by else None,
            'created_at': self.created_at.isoformat() + 'Z' if self.created_at else None,
            'started_at': self.started_at.isoformat() + 'Z' if self.started_at else None,
            'finished_at': self.finished_at.isoformat() + 'Z' if self.finished_at else None,
            'steps': plan.get('steps', []),
            'last_status': self.last_status,
            'frames_done': self.frames_done,
        }


class SavedCapturePlan(db.Model):
    """A plan a user saved in the builder to run again."""
    __tablename__ = 'saved_capture_plans'

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    # {"steps": [...], "on_error": ..., "upload": ..., "park_at_end": ...}
    plan_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        try:
            plan = json.loads(self.plan_json or '{}')
        except ValueError:
            plan = {}
        return {
            'id': self.id,
            'name': self.name,
            'plan': plan,
            'updated_at': self.updated_at.isoformat() + 'Z' if self.updated_at else None,
        }
