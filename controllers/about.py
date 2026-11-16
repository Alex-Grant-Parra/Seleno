from datetime import datetime

from flask import Blueprint, render_template

from models.model_accuracy import ModelAccuracySample

# Define the blueprint for about routes
about_bp = Blueprint('about', __name__)

@about_bp.route('/about')
def about():
    return render_template('about.html')

@about_bp.route('/about/licence')
def licence_page():
    return render_template('licence.html')

@about_bp.route('/about/tos')
def tos():
    return render_template('tos.html')

@about_bp.route('/about/privacy')
def privacy():
    return render_template('privacy.html')

def _format_error(deg):
    """Angle on the sky in the most readable unit: arcseconds, arcminutes or degrees."""
    if deg is None:
        return None
    arcsec = deg * 3600.0
    if arcsec < 0.01:
        return '< 0.01″'
    if arcsec < 1:
        return f'{arcsec:.2f}″'
    if arcsec < 60:
        return f'{arcsec:.1f}″'
    if arcsec < 3600:
        return f'{arcsec / 60:.1f}′'
    return f'{deg:.2f}°'


def _accuracy_samples():
    """ModelAccuracySample rows grouped by date, oldest first; empty if the table is missing."""
    try:
        rows = (ModelAccuracySample.query
                .order_by(ModelAccuracySample.sample_utc, ModelAccuracySample.sort_order).all())
    except Exception:
        return []
    samples = {}
    for row in rows:
        when = datetime.strptime(row.sample_utc, '%Y-%m-%dT%H:%M:%SZ')
        sample = samples.setdefault(row.sample_utc, {'key': when.strftime('%Y-%m-%d'), 'when': when, 'rows': []})
        sample['rows'].append({
            'body': row.body,
            'de440': (row.de440_ra, row.de440_dec),
            'models': [
                {'ra': getattr(row, f'{v}_ra'), 'dec': getattr(row, f'{v}_dec'),
                 'err': _format_error(getattr(row, f'{v}_err'))}
                for v in ('v1', 'v2', 'v3')
            ],
        })
    return list(samples.values())


@about_bp.route('/about/Astrophysics')
def astrophysics():
    return render_template('about_astrophysics.html', accuracy_samples=_accuracy_samples())
