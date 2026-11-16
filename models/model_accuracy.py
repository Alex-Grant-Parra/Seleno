from app.db import db


class ModelAccuracySample(db.Model):
    """One body on one date: where DE440 puts it and where each planetary model does.

    Shown as the accuracy table on /about/Astrophysics. Rows are written by
    scripts/model_accuracy.py; rerun it after changing a model. Positions are
    geocentric, geometric (no light time or aberration), J2000 equator and
    equinox, in degrees. Errors are the angle on the sky between the model's
    position and DE440's, in degrees.
    """
    __tablename__ = 'model_accuracy_samples'
    __table_args__ = (db.UniqueConstraint('sample_utc', 'body', name='uq_model_accuracy_sample'),)

    id = db.Column(db.Integer, primary_key=True)
    sample_utc = db.Column(db.String(32), nullable=False)    # ISO 8601, civil time
    body = db.Column(db.String(16), nullable=False)          # lower case: "mercury", "moon"...
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    de440_ra = db.Column(db.Float, nullable=False)
    de440_dec = db.Column(db.Float, nullable=False)

    v1_ra = db.Column(db.Float, nullable=True)
    v1_dec = db.Column(db.Float, nullable=True)
    v1_err = db.Column(db.Float, nullable=True)

    v2_ra = db.Column(db.Float, nullable=True)
    v2_dec = db.Column(db.Float, nullable=True)
    v2_err = db.Column(db.Float, nullable=True)

    v3_ra = db.Column(db.Float, nullable=True)
    v3_dec = db.Column(db.Float, nullable=True)
    v3_err = db.Column(db.Float, nullable=True)

    generated_at = db.Column(db.String(32), nullable=False)
