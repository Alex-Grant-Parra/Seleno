import json
from datetime import datetime

from app.db import db


class Photo(db.Model):
    """A photo or video a telescope uploaded, with where it pointed and which capture
    plan step took it. Files live in camera_photos/<owner_id>/<filename>."""
    __tablename__ = 'photos'
    __table_args__ = (
        db.Index('ix_photos_owner_sha256', 'owner_id', 'sha256'),
        db.Index('ix_photos_owner_plan', 'owner_id', 'plan_id'),
    )

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    telescope_id = db.Column(db.Integer, db.ForeignKey('telescopes.id', ondelete='SET NULL'), nullable=True, index=True)

    filename = db.Column(db.String(255), nullable=False)
    path = db.Column(db.Text, nullable=False)
    # SHA-256 of the file as uploaded (JPEGs are rewritten afterwards to carry the target)
    sha256 = db.Column(db.String(64), nullable=False)
    bytes = db.Column(db.BigInteger, nullable=True)

    kind = db.Column(db.String(16), nullable=False, default='jpeg')        # jpeg|raw|video
    frame_type = db.Column(db.String(16), nullable=True, index=True)      # light|dark|flat|bias|snapshot|bracket|timelapse
    label = db.Column(db.String(255), nullable=True)

    captured_at = db.Column(db.DateTime, nullable=True, index=True)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    exposure_s = db.Column(db.Float, nullable=True)
    iso = db.Column(db.String(32), nullable=True)

    target = db.Column(db.String(255), nullable=True)
    ra = db.Column(db.Float, nullable=True)
    dec = db.Column(db.Float, nullable=True)
    site_lat = db.Column(db.Float, nullable=True)
    site_lon = db.Column(db.Float, nullable=True)

    plan_id = db.Column(db.String(64), nullable=True)
    step = db.Column(db.Integer, nullable=True)
    frame = db.Column(db.Integer, nullable=True)
    pair_id = db.Column(db.String(64), nullable=True)   # joins a RAW to its JPEG

    def to_dict(self):
        return {
            'id': self.id,
            'filename': self.filename,
            'kind': self.kind,
            'frame_type': self.frame_type,
            'label': self.label,
            'bytes': self.bytes,
            'captured_at': self.captured_at.isoformat() + 'Z' if self.captured_at else None,
            'uploaded_at': self.uploaded_at.isoformat() + 'Z' if self.uploaded_at else None,
            'exposure_s': self.exposure_s,
            'iso': self.iso,
            'target': self.target,
            'ra': self.ra,
            'dec': self.dec,
            'plan_id': self.plan_id,
            'step': self.step,
            'frame': self.frame,
            'pair_id': self.pair_id,
        }


class TelescopeUpload(db.Model):
    """A resumable (tus) upload in progress. Data goes to instance/uploads/partial/<id>.part."""
    __tablename__ = 'uploads'

    id = db.Column(db.String(32), primary_key=True)
    telescope_id = db.Column(db.Integer, db.ForeignKey('telescopes.id', ondelete='CASCADE'), nullable=False, index=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    upload_length = db.Column(db.BigInteger, nullable=False)
    upload_offset = db.Column(db.BigInteger, nullable=False, default=0)
    metadata_json = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    photo_id = db.Column(db.Integer, db.ForeignKey('photos.id', ondelete='SET NULL'), nullable=True)

    @property
    def details(self):
        try:
            return json.loads(self.metadata_json or '{}')
        except ValueError:
            return {}
