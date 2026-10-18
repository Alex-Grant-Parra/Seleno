"""The album's own records: an index of every file a user has (whatever route it
arrived by), the user's flags and the quality numbers the browser measured, the
stacks the browser made, and the targets Basic mode suggests."""
import json
from datetime import datetime

from app.db import db

FLAGS = ("pick", "reject")
# Frame types a user can set; uploads may also carry snapshot/bracket/timelapse/video/test
USER_FRAME_TYPES = ("light", "dark", "flat", "darkflat", "bias", "snapshot", "test", "processed", "other")


def _iso(value):
    return value.isoformat() + "Z" if value else None


def _loads(text_value, default=None):
    if not text_value:
        return default
    try:
        return json.loads(text_value)
    except ValueError:
        return default


class FrameMeta(db.Model):
    """One file in camera_photos/<owner_id>/: what its EXIF says (cached, refreshed when
    the file changes), what the user set, and what the browser measured."""
    __tablename__ = 'frame_meta'
    __table_args__ = (db.UniqueConstraint('owner_id', 'filename', name='uq_frame_meta_owner_file'),)

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)

    # The file as last scanned; a different size or mtime means scan it again
    file_size = db.Column(db.BigInteger, nullable=True)
    file_mtime = db.Column(db.Float, nullable=True)
    kind = db.Column(db.String(16), nullable=True)                 # jpeg|raw|video|image
    raw_filename = db.Column(db.String(255), nullable=True)        # the RAW that belongs with a JPEG

    captured_at = db.Column(db.DateTime, nullable=True, index=True)
    exposure_s = db.Column(db.Float, nullable=True)
    iso = db.Column(db.String(32), nullable=True)
    camera = db.Column(db.String(128), nullable=True)
    width = db.Column(db.Integer, nullable=True)
    height = db.Column(db.Integer, nullable=True)
    # A JPEG's target and notes live in its EXIF and are mirrored here; other files
    # keep them here only
    target = db.Column(db.String(255), nullable=True)
    notes = db.Column(db.Text, nullable=True)

    # Set by the user: frame type (overrides the upload's), pick/reject, stars 0-5
    frame_type = db.Column(db.String(16), nullable=True)
    flag = db.Column(db.String(8), nullable=True)
    rating = db.Column(db.Integer, nullable=True)

    # Measured in the browser (stars, FWHM, HFR, eccentricity, background, noise...)
    metrics_json = db.Column(db.Text, nullable=True)
    metrics_source = db.Column(db.String(8), nullable=True)        # raw|jpeg
    measured_at = db.Column(db.DateTime, nullable=True)

    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def metrics(self):
        return _loads(self.metrics_json)


class StackResult(db.Model):
    """A stack (or lucky-imaging result) made in the browser and saved to the album.
    Files live in camera_photos/<owner_id>/.stacks/<id>/: master.fits (linear, 32-bit),
    preview.f16 (the editor's linear preview), auto.jpg and thumb.jpg."""
    __tablename__ = 'stacks'

    id = db.Column(db.String(32), primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    target = db.Column(db.String(255), nullable=True)
    kind = db.Column(db.String(16), nullable=False, default='stack')    # stack|planetary|single
    state = db.Column(db.String(16), nullable=False, default='uploading')  # uploading|ready

    width = db.Column(db.Integer, nullable=True)
    height = db.Column(db.Integer, nullable=True)
    preview_width = db.Column(db.Integer, nullable=True)
    preview_height = db.Column(db.Integer, nullable=True)
    frames_used = db.Column(db.Integer, nullable=True)
    frames_total = db.Column(db.Integer, nullable=True)
    total_exposure_s = db.Column(db.Float, nullable=True)
    linear = db.Column(db.Boolean, nullable=False, default=True)

    settings_json = db.Column(db.Text, nullable=True)      # what it was stacked with
    report_json = db.Column(db.Text, nullable=True)        # per-frame results, warnings, crop
    edit_json = db.Column(db.Text, nullable=True)          # the editor's saved settings

    master_bytes = db.Column(db.BigInteger, nullable=True)       # expected size of master.fits
    master_received = db.Column(db.BigInteger, nullable=False, default=0)
    bytes_total = db.Column(db.BigInteger, nullable=True)        # everything on disk, once ready

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self, full=False):
        base = f"/album/stacks/{self.id}"
        out = {
            'id': self.id,
            'name': self.name,
            'target': self.target,
            'kind': self.kind,
            'state': self.state,
            'width': self.width,
            'height': self.height,
            'preview_width': self.preview_width,
            'preview_height': self.preview_height,
            'frames_used': self.frames_used,
            'frames_total': self.frames_total,
            'total_exposure_s': self.total_exposure_s,
            'linear': self.linear,
            'has_master': bool(self.master_bytes) and self.master_received >= (self.master_bytes or 0),
            'master_bytes': self.master_bytes,
            'bytes_total': self.bytes_total,
            'created_at': _iso(self.created_at),
            'updated_at': _iso(self.updated_at),
            'thumb_url': f"{base}/thumb.jpg",
            'auto_url': f"{base}/auto.jpg",
            'preview_url': f"{base}/preview.f16",
            'master_url': f"{base}/master.fits",
            'edited': bool(self.edit_json),
        }
        if full:
            out.update(settings=_loads(self.settings_json, {}), report=_loads(self.report_json, {}),
                       edit=_loads(self.edit_json))
        return out


class FeaturedTarget(db.Model):
    """Objects Basic mode offers as targets, with what kind of exposure suits them.
    Coordinates come from the catalogue (or, for the Moon and planets, are worked out
    for now), so a row only needs the designation."""
    __tablename__ = 'featured_targets'

    id = db.Column(db.Integer, primary_key=True)
    designation = db.Column(db.String(32), nullable=False, unique=True)
    name = db.Column(db.String(120), nullable=False)
    kind = db.Column(db.String(64), nullable=True)            # "emission nebula", "spiral galaxy"...
    preset = db.Column(db.String(16), nullable=False)          # moon|planet|bright|faint
    size_arcmin = db.Column(db.Float, nullable=True)
    blurb = db.Column(db.Text, nullable=True)
    season = db.Column(db.String(32), nullable=True)
    sort_order = db.Column(db.Integer, nullable=False, default=100)
    enabled = db.Column(db.Boolean, nullable=False, default=True)


# Seed rows for featured_targets: (designation, name, kind, preset, size arcmin, blurb)
# Inserted when missing; rows already there (perhaps edited) are left alone.
FEATURED_SEED = [
    ("Moon", "The Moon", "our Moon", "moon", 31, "Craters and maria in sharp detail; best near the quarters, when shadows are long."),
    ("Jupiter", "Jupiter", "planet", "planet", 0.7, "Cloud belts and the four big moons."),
    ("Saturn", "Saturn", "planet", "planet", 0.3, "The rings, even in small telescopes."),
    ("Mars", "Mars", "planet", "planet", 0.3, "Small and bright; shows most near opposition."),
    ("Venus", "Venus", "planet", "planet", 0.5, "A dazzling crescent or gibbous disc."),
    ("M42", "Orion Nebula", "emission nebula", "bright", 85, "The brightest nebula in the sky: glowing gas and newborn stars."),
    ("M31", "Andromeda Galaxy", "spiral galaxy", "faint", 180, "The nearest big galaxy, so large it fills the frame."),
    ("M45", "Pleiades", "open cluster", "bright", 110, "Blue stars wrapped in faint reflection nebula."),
    ("M13", "Great Hercules Cluster", "globular cluster", "bright", 20, "Hundreds of thousands of stars in a tight ball."),
    ("M27", "Dumbbell Nebula", "planetary nebula", "bright", 8, "A bright, colourful dying star's shell."),
    ("M57", "Ring Nebula", "planetary nebula", "bright", 1.4, "A small, bright smoke ring."),
    ("M51", "Whirlpool Galaxy", "spiral galaxy", "faint", 11, "A face-on spiral pulling on a smaller companion."),
    ("M81", "Bode's Galaxy", "spiral galaxy", "faint", 27, "A bright spiral; M82 sits next to it."),
    ("M82", "Cigar Galaxy", "starburst galaxy", "faint", 11, "An edge-on galaxy with red plumes."),
    ("M101", "Pinwheel Galaxy", "spiral galaxy", "faint", 29, "A big, faint face-on spiral: needs a long total exposure."),
    ("M33", "Triangulum Galaxy", "spiral galaxy", "faint", 70, "A large, faint spiral with pink star-forming regions."),
    ("M8", "Lagoon Nebula", "emission nebula", "bright", 90, "A bright summer nebula low in the south."),
    ("M16", "Eagle Nebula", "emission nebula", "faint", 35, "Home of the Pillars of Creation."),
    ("M17", "Omega Nebula", "emission nebula", "bright", 11, "A bright swan-shaped cloud."),
    ("M20", "Trifid Nebula", "emission and reflection nebula", "faint", 28, "Red and blue nebula split by dark lanes."),
    ("M1", "Crab Nebula", "supernova remnant", "faint", 7, "The remains of a star seen exploding in 1054."),
    ("M104", "Sombrero Galaxy", "spiral galaxy", "faint", 9, "An edge-on galaxy with a dark dust lane."),
    ("NGC7000", "North America Nebula", "emission nebula", "faint", 120, "A huge nebula shaped like the continent; wide fields only."),
    ("NGC869", "Double Cluster", "open clusters", "bright", 60, "Two rich star clusters side by side."),
    ("M3", "M3", "globular cluster", "bright", 18, "A bright, rich globular cluster."),
    ("M92", "M92", "globular cluster", "bright", 14, "A compact globular, often overlooked next to M13."),
    ("M44", "Beehive Cluster", "open cluster", "bright", 95, "A big, bright cluster for wide fields."),
    ("M35", "M35", "open cluster", "bright", 28, "A rich cluster in Gemini."),
    ("M64", "Black Eye Galaxy", "spiral galaxy", "faint", 10, "A dark dust band across a bright core."),
    ("M97", "Owl Nebula", "planetary nebula", "faint", 3.4, "A round, faint nebula with two dark 'eyes'."),
]


def ensure_featured_targets():
    """Insert any missing featured targets (idempotent)."""
    existing = {row.designation for row in FeaturedTarget.query.with_entities(FeaturedTarget.designation)}
    added = 0
    for order, (designation, name, kind, preset, size, blurb) in enumerate(FEATURED_SEED):
        if designation in existing:
            continue
        db.session.add(FeaturedTarget(designation=designation, name=name, kind=kind, preset=preset,
                                      size_arcmin=size, blurb=blurb, sort_order=order))
        added += 1
    if added:
        db.session.commit()
    return added

