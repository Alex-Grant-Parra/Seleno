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


class Project(db.Model):
    """A long integration built up over many nights. The browser keeps running totals
    (weighted sums, weights, coverage and the rejection limits) and the server stores
    them as FITS in camera_photos/<owner_id>/.projects/<id>/: the committed
    generation in current/, an upload in progress in staging/, and a from-scratch
    rebuild's checkpoint in rebuild/ (so it can resume)."""
    __tablename__ = 'projects'

    id = db.Column(db.String(32), primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    target = db.Column(db.String(255), nullable=True)
    source = db.Column(db.String(8), nullable=False, default='raw')        # raw|jpeg
    width = db.Column(db.Integer, nullable=True)
    height = db.Column(db.Integer, nullable=True)
    settings_json = db.Column(db.Text, nullable=True)       # fixed for the project's life
    reference_json = db.Column(db.Text, nullable=True)      # reference frame: name, stars, level and spread
    frames_json = db.Column(db.Text, nullable=True)         # names of the frames in the totals
    seen_json = db.Column(db.Text, nullable=True)           # every frame considered (in, or left out), never re-judged
    nights_json = db.Column(db.Text, nullable=True)         # [{night, frames, exposure_s, added_at}]
    frames_total = db.Column(db.Integer, nullable=False, default=0)
    exposure_total_s = db.Column(db.Float, nullable=False, default=0.0)
    generation = db.Column(db.Integer, nullable=False, default=0)          # 0: no totals yet
    rebuild_json = db.Column(db.Text, nullable=True)        # {done: [names], total, settings} while rebuilding
    stack_id = db.Column(db.String(32), nullable=True)      # the latest result, saved as a stack
    bytes_total = db.Column(db.BigInteger, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self, full=False):
        base = f"/album/projects/{self.id}"
        out = {
            'id': self.id,
            'name': self.name,
            'target': self.target,
            'source': self.source,
            'width': self.width,
            'height': self.height,
            'frames_total': self.frames_total,
            'exposure_total_s': self.exposure_total_s,
            'generation': self.generation,
            'nights': _loads(self.nights_json, []),
            'rebuilding': _loads(self.rebuild_json),
            'stack_id': self.stack_id,
            'bytes_total': self.bytes_total,
            'files_url': f"{base}/current",
            'rebuild_url': f"{base}/rebuild",
            'created_at': _iso(self.created_at),
            'updated_at': _iso(self.updated_at),
        }
        out['settings'] = _loads(self.settings_json, {})
        if full:
            out.update(reference=_loads(self.reference_json), frames=_loads(self.frames_json, []),
                       seen=_loads(self.seen_json, []))
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
    preset = db.Column(db.String(16), nullable=False)          # moon|planet|bright|faint|star
    size_arcmin = db.Column(db.Float, nullable=True)
    blurb = db.Column(db.Text, nullable=True)
    season = db.Column(db.String(32), nullable=True)
    wiki = db.Column(db.String(255), nullable=True)            # Wikipedia article URL
    sort_order = db.Column(db.Integer, nullable=False, default=100)
    enabled = db.Column(db.Boolean, nullable=False, default=True)


def N_(text):
    """Marks text for translation without translating it here (the targets API translates it per visitor)."""
    return text


# Seed rows for featured_targets: (designation, name, kind, preset, size arcmin, blurb)
# Inserted when missing; rows already there (perhaps edited) are left alone.
FEATURED_SEED = [
    ("Moon", N_("The Moon"), N_("our Moon"), "moon", 31, N_("Craters and maria in sharp detail; best near the quarters, when shadows are long.")),
    ("Jupiter", N_("Jupiter"), N_("planet"), "planet", 0.7, N_("Cloud belts and the four big moons.")),
    ("Saturn", N_("Saturn"), N_("planet"), "planet", 0.3, N_("The rings, even in small telescopes.")),
    ("Mars", N_("Mars"), N_("planet"), "planet", 0.3, N_("Small and bright; shows most near opposition.")),
    ("Venus", N_("Venus"), N_("planet"), "planet", 0.5, N_("A dazzling crescent or gibbous disc.")),
    ("M42", N_("Orion Nebula"), N_("emission nebula"), "bright", 85, N_("The brightest nebula in the sky: glowing gas and newborn stars.")),
    ("M31", N_("Andromeda Galaxy"), N_("spiral galaxy"), "faint", 180, N_("The nearest big galaxy, so large it fills the frame.")),
    ("M45", N_("Pleiades"), N_("open cluster"), "bright", 110, N_("Blue stars wrapped in faint reflection nebula.")),
    ("M13", N_("Great Hercules Cluster"), N_("globular cluster"), "bright", 20, N_("Hundreds of thousands of stars in a tight ball.")),
    ("M27", N_("Dumbbell Nebula"), N_("planetary nebula"), "bright", 8, N_("A bright, colourful dying star's shell.")),
    ("M57", N_("Ring Nebula"), N_("planetary nebula"), "bright", 1.4, N_("A small, bright smoke ring.")),
    ("M51", N_("Whirlpool Galaxy"), N_("spiral galaxy"), "faint", 11, N_("A face-on spiral pulling on a smaller companion.")),
    ("M81", N_("Bode's Galaxy"), N_("spiral galaxy"), "faint", 27, N_("A bright spiral; M82 sits next to it.")),
    ("M82", N_("Cigar Galaxy"), N_("starburst galaxy"), "faint", 11, N_("An edge-on galaxy with red plumes.")),
    ("M101", N_("Pinwheel Galaxy"), N_("spiral galaxy"), "faint", 29, N_("A big, faint face-on spiral: needs a long total exposure.")),
    ("M33", N_("Triangulum Galaxy"), N_("spiral galaxy"), "faint", 70, N_("A large, faint spiral with pink star-forming regions.")),
    ("M8", N_("Lagoon Nebula"), N_("emission nebula"), "bright", 90, N_("A bright summer nebula low in the south.")),
    ("M16", N_("Eagle Nebula"), N_("emission nebula"), "faint", 35, N_("Home of the Pillars of Creation.")),
    ("M17", N_("Omega Nebula"), N_("emission nebula"), "bright", 11, N_("A bright swan-shaped cloud.")),
    ("M20", N_("Trifid Nebula"), N_("emission and reflection nebula"), "faint", 28, N_("Red and blue nebula split by dark lanes.")),
    ("M1", N_("Crab Nebula"), N_("supernova remnant"), "faint", 7, N_("The remains of a star seen exploding in 1054.")),
    ("M104", N_("Sombrero Galaxy"), N_("spiral galaxy"), "faint", 9, N_("An edge-on galaxy with a dark dust lane.")),
    ("NGC7000", N_("North America Nebula"), N_("emission nebula"), "faint", 120, N_("A huge nebula shaped like the continent; wide fields only.")),
    ("NGC869", N_("Double Cluster"), N_("open clusters"), "bright", 60, N_("Two rich star clusters side by side.")),
    ("M3", "M3", N_("globular cluster"), "bright", 18, N_("A bright, rich globular cluster.")),
    ("M92", "M92", N_("globular cluster"), "bright", 14, N_("A compact globular, often overlooked next to M13.")),
    ("M44", N_("Beehive Cluster"), N_("open cluster"), "bright", 95, N_("A big, bright cluster for wide fields.")),
    ("M35", "M35", N_("open cluster"), "bright", 28, N_("A rich cluster in Gemini.")),
    ("M64", N_("Black Eye Galaxy"), N_("spiral galaxy"), "faint", 10, N_("A dark dust band across a bright core.")),
    ("M97", N_("Owl Nebula"), N_("planetary nebula"), "faint", 3.4, N_("A round, faint nebula with two dark 'eyes'.")),
    ("Sirius", N_("Sirius"), N_("star"), "star", None, N_("The brightest star in the night sky, flashing colours when low.")),
    ("Albireo", N_("Albireo"), N_("double star"), "star", 0.6, N_("A gold and a blue star side by side: the classic colour double.")),
    ("Mizar", N_("Mizar and Alcor"), N_("multiple star"), "star", 12, N_("A naked-eye pair in the Plough's handle; the telescope splits Mizar too.")),
    ("Betelgeuse", N_("Betelgeuse"), N_("red supergiant"), "star", None, N_("A huge orange-red star marking Orion's shoulder.")),
    ("Vega", N_("Vega"), N_("star"), "star", None, N_("A brilliant blue-white star high on summer evenings.")),
    ("M22", "M22", N_("globular cluster"), "bright", 32, N_("A big, bright globular low in Sagittarius.")),
    ("M15", "M15", N_("globular cluster"), "bright", 18, N_("A dense globular with a very bright core.")),
    ("M11", N_("Wild Duck Cluster"), N_("open cluster"), "bright", 14, N_("A rich cluster packed like a flock of birds.")),
    ("M7", N_("Ptolemy Cluster"), N_("open cluster"), "bright", 80, N_("A big, bright cluster: a southern summer target.")),
    ("M78", "M78", N_("reflection nebula"), "faint", 8, N_("A small blue glow in Orion.")),
    ("NGC253", N_("Sculptor Galaxy"), N_("spiral galaxy"), "faint", 27, N_("A bright, dusty spiral seen nearly edge-on.")),
    ("NGC7293", N_("Helix Nebula"), N_("planetary nebula"), "faint", 16, N_("A large, faint 'eye' of gas: one of the nearest planetary nebulae.")),
    ("NGC6960", N_("Western Veil Nebula"), N_("supernova remnant"), "faint", 70, N_("Fine filaments of an old supernova, around 52 Cygni.")),
    ("NGC6992", N_("Eastern Veil Nebula"), N_("supernova remnant"), "faint", 60, N_("The brighter half of the Veil: twisted ropes of gas.")),
]


# Each featured target's Wikipedia article, by designation
FEATURED_WIKI = {
    "Moon": "Moon", "Jupiter": "Jupiter", "Saturn": "Saturn", "Mars": "Mars", "Venus": "Venus",
    "M42": "Orion_Nebula", "M31": "Andromeda_Galaxy", "M45": "Pleiades", "M13": "Messier_13",
    "M27": "Dumbbell_Nebula", "M57": "Ring_Nebula", "M51": "Whirlpool_Galaxy", "M81": "Messier_81",
    "M82": "Messier_82", "M101": "Pinwheel_Galaxy", "M33": "Triangulum_Galaxy", "M8": "Lagoon_Nebula",
    "M16": "Eagle_Nebula", "M17": "Omega_Nebula", "M20": "Trifid_Nebula", "M1": "Crab_Nebula",
    "M104": "Sombrero_Galaxy", "NGC7000": "North_America_Nebula", "NGC869": "Double_Cluster",
    "M3": "Messier_3", "M92": "Messier_92", "M44": "Beehive_Cluster", "M35": "Messier_35",
    "M64": "Black_Eye_Galaxy", "M97": "Owl_Nebula",
    "Sirius": "Sirius", "Albireo": "Albireo", "Mizar": "Mizar_and_Alcor", "Betelgeuse": "Betelgeuse",
    "Vega": "Vega", "M22": "Messier_22", "M15": "Messier_15", "M11": "Wild_Duck_Cluster",
    "M7": "Messier_7", "M78": "Messier_78", "NGC253": "Sculptor_Galaxy", "NGC7293": "Helix_Nebula",
    "NGC6960": "Veil_Nebula", "NGC6992": "Veil_Nebula",
}


def wiki_url(designation):
    article = FEATURED_WIKI.get(designation)
    return f"https://en.wikipedia.org/wiki/{article}" if article else None


def ensure_featured_columns():
    """Columns added to featured_targets after it first shipped."""
    from sqlalchemy import inspect, text
    inspector = inspect(db.engine)
    if 'featured_targets' not in inspector.get_table_names():
        return
    have = {c['name'] for c in inspector.get_columns('featured_targets')}
    with db.engine.begin() as conn:
        if 'wiki' not in have:
            conn.execute(text("ALTER TABLE featured_targets ADD COLUMN wiki VARCHAR(255)"))


def ensure_featured_targets():
    """Insert any missing featured targets, and fill in Wikipedia links that are
    blank (idempotent)."""
    ensure_featured_columns()
    rows = {row.designation: row for row in FeaturedTarget.query.all()}
    added = 0
    for order, (designation, name, kind, preset, size, blurb) in enumerate(FEATURED_SEED):
        row = rows.get(designation)
        if row is not None:
            if not row.wiki and wiki_url(designation):
                row.wiki = wiki_url(designation)
            continue
        db.session.add(FeaturedTarget(designation=designation, name=name, kind=kind, preset=preset,
                                      size_arcmin=size, blurb=blurb, wiki=wiki_url(designation), sort_order=order))
        added += 1
    db.session.commit()
    return added

