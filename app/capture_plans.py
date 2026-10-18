"""Capture plans: a list of steps the Pi runs in order (go to, brackets, lights,
darks, video...). The server builds and checks the plan, sends it in one
startCapturePlan command, and follows its progress from status messages.

The plan shape is checked against PLAN_SCHEMA below, a JSON-Schema-style
description that `validate_plan` enforces (unknown fields are refused).
"""
import json
import logging
import math
import secrets
import threading
import time
from datetime import datetime, timedelta

from app.db import db
from app.telescopeLink import shutter_seconds

logger = logging.getLogger(__name__)

MAX_PLAN_BYTES = 256 * 1024
MAX_STEPS = 50
MAX_FRAMES = 2000
MAX_EXPOSURE_S = 900
MAX_VIDEO_S = 600
MAX_TIMELAPSE_S = 12 * 3600
MAX_WAIT_S = 12 * 3600
DEFAULT_OVERHEAD_S = 3.0      # per frame, until finished plans teach us better
OWNER_PLAN_MAX_HOURS = 12     # end_by for a plan started outside any booking

# Rough times for steps that take no frames
GOTO_S = 45
CENTRE_S = 60
DITHER_S = 8
SNAPSHOT_OVERHEAD_S = 0.5

DEFAULT_SIZES = {
    'jpeg': 8 * 1024 * 1024,
    'raw': 25 * 1024 * 1024,
    'snapshot': 300 * 1024,
    'video_per_s': 2 * 1024 * 1024,
}

PLAN_STATES = ('running', 'paused', 'waiting', 'done', 'stopped', 'failed')


# Field types: (python types, extra check)
def _number(minimum=None, maximum=None, exclusive_min=False):
    return {'type': 'number', 'minimum': minimum, 'maximum': maximum, 'exclusiveMinimum': exclusive_min}


def _integer(minimum=None, maximum=None):
    return {'type': 'integer', 'minimum': minimum, 'maximum': maximum}


def _string(max_length=120, enum=None):
    return {'type': 'string', 'maxLength': max_length, 'enum': enum}


_CAMERA_FIELDS = {
    'iso': {'type': ['string', 'integer']},
    'aperture': {'type': ['string', 'number']},
    'shutter': _string(32),
    'exposure_s': _number(0, MAX_EXPOSURE_S, exclusive_min=True),
    'white_balance': _string(64),
    'image_format': _string(64),
    'exposure_compensation': {'type': ['string', 'number']},
    'label': _string(120),
}

_COUNT = _integer(1, MAX_FRAMES)

PLAN_SCHEMA = {
    'plan': {
        'required': ['name', 'steps'],
        'properties': {
            'name': _string(120),
            'on_error': _string(16, ['skip_step', 'stop']),
            'upload': _string(16, ['jpeg_first', 'all', 'none']),
            'park_at_end': {'type': 'boolean'},
            'steps': {'type': 'array', 'minItems': 1, 'maxItems': MAX_STEPS},
        },
    },
    'steps': {
        'light': {
            'required': ['count'],
            'camera': True,
            'properties': {
                'count': _COUNT,
                'interval_s': _number(0, 3600),
                'dither_every': _integer(0, MAX_FRAMES),
                'dither_arcmin': _number(0, 60),
                'dither_settle_s': _number(0, 120),
            },
        },
        'bracket': {
            'required': ['exposures_s'],
            'camera': True,
            'properties': {
                'exposures_s': {'type': 'array', 'minItems': 1, 'maxItems': 12,
                                'items': _number(0, MAX_EXPOSURE_S, exclusive_min=True)},
                'repeat': _integer(1, 100),
            },
        },
        'timelapse': {
            'required': ['every_s'],
            'camera': True,
            'properties': {
                'every_s': _number(1, 3600),
                'duration_s': _number(1, MAX_TIMELAPSE_S),
                'count': _COUNT,
            },
        },
        'snapshot': {
            'required': ['count'],
            'camera': True,
            'properties': {
                'count': _COUNT,
                'interval_s': _number(0, 3600),
            },
        },
        'video': {
            'required': ['duration_s'],
            'camera': True,
            'properties': {
                'duration_s': _number(1, MAX_VIDEO_S),
                'quality': _string(32),
            },
        },
        'dark': {'required': ['count'], 'camera': True, 'properties': {'count': _COUNT}},
        'flat': {
            'required': ['count'],
            'camera': True,
            'properties': {
                'count': _COUNT,
                # A number, or "auto" once the Pi supports it
                'exposure_s': {'type': ['number', 'string'], 'minimum': 0, 'maximum': MAX_EXPOSURE_S,
                               'exclusiveMinimum': True, 'enum': ['auto']},
            },
        },
        'bias': {'required': ['count'], 'camera': True, 'properties': {'count': _COUNT}},
        'goto': {
            'required': [],
            'properties': {
                'target': _string(120),
                'ra': _number(0, 360),
                'dec': _number(-90, 90),
                'name': _string(120),
                'centre': {'type': 'boolean'},
            },
        },
        'wait': {
            'required': [],
            'properties': {
                'until': _string(40),
                'until_alt_deg': _number(-10, 90),
                'seconds': _number(0, MAX_WAIT_S),
            },
        },
    },
}

STEP_TYPES = tuple(PLAN_SCHEMA['steps'].keys())
SHOOTING_STEPS = ('light', 'bracket', 'timelapse', 'dark', 'flat', 'bias')

# Step setting -> getCameraChoices key
CAMERA_CHOICE_KEYS = {
    'iso': 'iso',
    'shutter': 'shutterSpeed',
    'aperture': 'aperture',
    'white_balance': 'whiteBalance',
    'image_format': 'imageFormat',
    'exposure_compensation': 'exposureCompensation',
}


def _type_ok(value, types):
    types = types if isinstance(types, list) else [types]
    for name in types:
        if name == 'string' and isinstance(value, str):
            return True
        if name == 'boolean' and isinstance(value, bool):
            return True
        if name == 'integer' and isinstance(value, int) and not isinstance(value, bool):
            return True
        if name == 'number' and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return True
        if name == 'array' and isinstance(value, list):
            return True
    return False


def _check_field(where, value, spec, errors):
    # A string is checked against `enum` when there is one, so a field can take
    # either a number or a fixed word (flat exposure_s: a number or "auto")
    enum = spec.get('enum')
    if isinstance(value, str) and enum is not None:
        if value not in enum:
            errors.append(f"{where} must be one of {', '.join(enum)}")
        return
    types = spec.get('type')
    if types and not _type_ok(value, types):
        errors.append(f"{where} has the wrong type")
        return
    if isinstance(value, str):
        if spec.get('maxLength') and len(value) > spec['maxLength']:
            errors.append(f"{where} is too long")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        low, high = spec.get('minimum'), spec.get('maximum')
        if low is not None and (value < low or (spec.get('exclusiveMinimum') and value <= low)):
            errors.append(f"{where} must be {'more than' if spec.get('exclusiveMinimum') else 'at least'} {low:g}")
        if high is not None and value > high:
            errors.append(f"{where} must be at most {high:g}")
    elif isinstance(value, list):
        if spec.get('minItems') and len(value) < spec['minItems']:
            errors.append(f"{where} needs at least {spec['minItems']} item(s)")
        if spec.get('maxItems') and len(value) > spec['maxItems']:
            errors.append(f"{where} allows at most {spec['maxItems']} items")
        if spec.get('items'):
            for i, item in enumerate(value):
                _check_field(f"{where}[{i}]", item, spec['items'], errors)


def validate_plan(plan):
    """Errors in the plan's shape and limits; an empty list when it is fine."""
    errors = []
    if not isinstance(plan, dict):
        return ["The plan must be an object"]

    plan_spec = PLAN_SCHEMA['plan']
    for key in plan_spec['required']:
        if key not in plan:
            errors.append(f"The plan needs {key}")
    for key, value in plan.items():
        spec = plan_spec['properties'].get(key)
        if spec is None:
            errors.append(f"Unknown plan field: {key}")
        else:
            _check_field(key, value, spec, errors)
    if errors:
        return errors

    for index, step in enumerate(plan['steps']):
        where = f"Step {index + 1}"
        if not isinstance(step, dict):
            errors.append(f"{where} must be an object")
            continue
        kind = step.get('type')
        step_spec = PLAN_SCHEMA['steps'].get(kind)
        if step_spec is None:
            errors.append(f"{where} has an unknown type: {kind}")
            continue
        properties = dict(_CAMERA_FIELDS) if step_spec.get('camera') else {}
        properties.update(step_spec['properties'])
        for key in step_spec['required']:
            if key not in step:
                errors.append(f"{where} ({kind}) needs {key}")
        for key, value in step.items():
            if key == 'type':
                continue
            spec = properties.get(key)
            if spec is None:
                errors.append(f"{where} ({kind}) has an unknown field: {key}")
            else:
                _check_field(f"{where} {key}", value, spec, errors)

        if kind == 'goto':
            has_coords = 'ra' in step or 'dec' in step
            if not step.get('target') and not has_coords:
                errors.append(f"{where} (goto) needs a target, or ra and dec")
            if has_coords and not ('ra' in step and 'dec' in step):
                errors.append(f"{where} (goto) needs both ra and dec")
        elif kind == 'wait':
            given = [key for key in ('until', 'until_alt_deg', 'seconds') if key in step]
            if len(given) != 1:
                errors.append(f"{where} (wait) needs exactly one of until, until_alt_deg or seconds")
            elif 'until' in step and _parse_utc(step['until']) is None:
                errors.append(f"{where} (wait) until must be an ISO 8601 UTC time")
        elif kind == 'timelapse' and 'duration_s' in step and 'count' in step:
            errors.append(f"{where} (timelapse) takes duration_s or count, not both")

    if errors:
        return errors

    frames = estimate_plan(plan)['frames']
    if frames > MAX_FRAMES:
        errors.append(f"The plan takes {frames} frames; the limit is {MAX_FRAMES}")
    return errors


def check_camera_settings(plan, choices):
    # Settings the camera doesn't offer, according to getCameraChoices
    errors = []
    if not isinstance(choices, dict):
        return errors
    for index, step in enumerate(plan.get('steps', [])):
        for field, key in CAMERA_CHOICE_KEYS.items():
            if field not in step:
                continue
            offered = choices.get(key)
            if isinstance(offered, list) and offered and str(step[field]) not in {str(c) for c in offered}:
                errors.append(f"Step {index + 1}: the camera has no {field.replace('_', ' ')} {step[field]}")
    return errors


def _parse_utc(text):
    if not isinstance(text, str):
        return None
    try:
        value = datetime.fromisoformat(text.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if value.tzinfo is not None:
        value = (value - value.utcoffset()).replace(tzinfo=None)
    return value


def _exposure(step, default=1.0):
    value = step.get('exposure_s')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return shutter_seconds(step.get('shutter')) or default


def _frame_bytes(step, sizes):
    fmt = str(step.get('image_format') or 'jpeg').lower()
    raw = 'raw' in fmt or any(ext in fmt for ext in ('cr2', 'cr3', 'nef', 'arw', 'raf', 'dng'))
    jpeg = 'jpeg' in fmt or 'jpg' in fmt or 'fine' in fmt or not raw
    return (sizes['jpeg'] if jpeg else 0) + (sizes['raw'] if raw else 0)


def estimate_plan(plan, overhead_s=DEFAULT_OVERHEAD_S, sizes=None, start=None):
    """Time, frames and storage for a plan. A wait for an altitude can't be timed
    and counts as nothing; such steps are marked `unknown`."""
    sizes = {**DEFAULT_SIZES, **(sizes or {})}
    start = start or datetime.utcnow()
    total_s = 0.0
    total_frames = 0
    total_bytes = 0
    steps = []

    for step in plan.get('steps', []) if isinstance(plan, dict) else []:
        if not isinstance(step, dict):
            continue
        kind = step.get('type')
        seconds, frames, size, unknown = 0.0, 0, 0, False

        if kind in ('light', 'dark', 'flat'):
            frames = int(step.get('count') or 0)
            exposure = _exposure(step, 1.0)
            interval = float(step.get('interval_s') or 0)
            seconds = frames * (exposure + interval + overhead_s)
            every = int(step.get('dither_every') or 0)
            if kind == 'light' and every > 0:
                seconds += (frames // every) * DITHER_S
            size = frames * _frame_bytes(step, sizes)
        elif kind == 'bias':
            frames = int(step.get('count') or 0)
            seconds = frames * overhead_s
            size = frames * _frame_bytes(step, sizes)
        elif kind == 'bracket':
            exposures = [float(e) for e in step.get('exposures_s') or [] if isinstance(e, (int, float))]
            repeat = int(step.get('repeat') or 1)
            frames = repeat * len(exposures)
            seconds = repeat * sum(e + overhead_s for e in exposures)
            size = frames * _frame_bytes(step, sizes)
        elif kind == 'timelapse':
            every = float(step.get('every_s') or 1)
            per_frame = max(every, _exposure(step, 0.0) + overhead_s)
            if step.get('duration_s'):
                seconds = float(step['duration_s'])
                frames = max(1, int(seconds // per_frame))
            else:
                frames = int(step.get('count') or 0)
                seconds = frames * per_frame
            size = frames * _frame_bytes(step, sizes)
        elif kind == 'snapshot':
            frames = int(step.get('count') or 0)
            seconds = frames * (float(step.get('interval_s') or 0) + SNAPSHOT_OVERHEAD_S)
            size = frames * sizes['snapshot']
        elif kind == 'video':
            frames = 1
            seconds = float(step.get('duration_s') or 0) + overhead_s
            size = int(float(step.get('duration_s') or 0) * sizes['video_per_s'])
        elif kind == 'goto':
            seconds = GOTO_S + (CENTRE_S if step.get('centre') else 0)
        elif kind == 'wait':
            if 'seconds' in step:
                seconds = float(step.get('seconds') or 0)
            elif 'until' in step:
                until = _parse_utc(step.get('until'))
                if until:
                    seconds = max(0.0, (until - (start + timedelta(seconds=total_s))).total_seconds())
            else:
                unknown = True

        total_s += seconds
        total_frames += frames
        total_bytes += size
        steps.append({'type': kind, 'seconds': round(seconds, 1), 'frames': frames, 'bytes': int(size), 'unknown': unknown})

    return {
        'seconds': round(total_s, 1),
        'frames': total_frames,
        'bytes': int(total_bytes),
        'overhead_s': overhead_s,
        'unknown_steps': sum(1 for s in steps if s['unknown']),
        'steps': steps,
    }


def learned_overhead(telescope_row):
    # Average overhead per frame from this telescope's last few finished plans
    from models.capture_plan import CapturePlan
    rows = (
        CapturePlan.query.filter(CapturePlan.telescope_id == telescope_row.id, CapturePlan.overhead_s.isnot(None))
        .order_by(CapturePlan.finished_at.desc())
        .limit(5)
        .all()
    )
    values = [r.overhead_s for r in rows if r.overhead_s]
    return round(sum(values) / len(values), 2) if values else DEFAULT_OVERHEAD_S


def recent_frame_sizes(owner_id):
    # Average file sizes from the user's recent uploads, by kind
    from sqlalchemy import func
    from models.photo import Photo
    sizes = dict(DEFAULT_SIZES)
    for kind in ('jpeg', 'raw'):
        recent = (
            db.session.query(Photo.bytes)
            .filter(Photo.owner_id == owner_id, Photo.kind == kind, Photo.bytes.isnot(None))
            .order_by(Photo.uploaded_at.desc())
            .limit(50)
            .subquery()
        )
        average = db.session.query(func.avg(recent.c.bytes)).scalar()
        if average:
            sizes[kind] = int(average)
    return sizes


def _new_plan_id():
    from models.capture_plan import CapturePlan
    stamp = datetime.utcnow().strftime('%Y%m%d')
    while True:
        plan_id = f"plan_{stamp}_{secrets.token_hex(2)}"
        if db.session.get(CapturePlan, plan_id) is None:
            return plan_id


def _iso_z(value):
    return value.replace(microsecond=0).isoformat() + 'Z' if value else None


def prepare_plan(user, telescope_row, plan_input):
    """Check who may start the plan and fill in id, owner and end_by.

    Returns (plan, context, errors). During an approved booking the booker owns
    the photos and the plan ends with the booking; otherwise the telescope's
    owner or an admin starts it for themselves, and it ends before the next
    booking or after 12 hours."""
    from app.telescope_access import approved_booking, is_telescope_owner, next_booking_start, utc_now

    errors = validate_plan(plan_input)
    if errors:
        return None, None, errors

    now = utc_now()
    booking = approved_booking(telescope_row, at=now)
    may_manage = getattr(user, 'is_admin', False) or is_telescope_owner(user, telescope_row)

    if booking is not None:
        if booking.requester_user_id != user.id and not may_manage:
            return None, None, ["Only the booking's holder, the telescope's owner or an admin can start a plan"]
        owner_id = booking.requester_user_id
        end_by = booking.end_utc
    else:
        if not may_manage:
            return None, None, ["You need a booking that covers now to start a plan"]
        owner_id = user.id
        end_by = now + timedelta(hours=OWNER_PLAN_MAX_HOURS)
        upcoming = next_booking_start(telescope_row, after=now)
        if upcoming and upcoming < end_by:
            end_by = upcoming

    estimate = estimate_plan(plan_input, overhead_s=learned_overhead(telescope_row),
                             sizes=recent_frame_sizes(owner_id), start=now)
    remaining = (end_by - now).total_seconds()
    if estimate['seconds'] > remaining:
        errors.append(
            f"The plan needs about {_duration(estimate['seconds'])} but must end by "
            f"{_iso_z(end_by)} ({_duration(max(0, remaining))} from now)"
        )
        return None, None, errors

    plan = {
        'id': _new_plan_id(),
        'name': plan_input['name'],
        'owner': str(owner_id),
        'end_by': _iso_z(end_by),
        'on_error': plan_input.get('on_error', 'skip_step'),
        'upload': plan_input.get('upload', 'jpeg_first'),
        'park_at_end': bool(plan_input.get('park_at_end', False)),
        'steps': plan_input['steps'],
    }
    if len(json.dumps(plan)) > MAX_PLAN_BYTES:
        return None, None, [f"The plan is larger than {MAX_PLAN_BYTES // 1024} KB"]

    context = {'owner_id': owner_id, 'booking_id': booking.id if booking else None,
               'end_by': end_by, 'estimate': estimate}
    return plan, context, []


def _duration(seconds):
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


def plan_status_from_status(status):
    # The capture plan part of a status message, if it carries one
    if not isinstance(status, dict):
        return None
    for candidate in (status.get('plan'), (status.get('photos') or {}).get('plan') if isinstance(status.get('photos'), dict) else None):
        if isinstance(candidate, dict) and (candidate.get('id') or candidate.get('plan')):
            return candidate
    return None


def record_plan_status(telescope_name, plan_status):
    """Store the Pi's view of a plan. A telescope can only update its own plans."""
    from models.capture_plan import CapturePlan, FINAL_PLAN_STATES
    from models.tables import Telescope

    if not isinstance(plan_status, dict):
        return None
    plan_id = plan_status.get('id') or plan_status.get('plan')
    if not isinstance(plan_id, str):
        return None
    row = db.session.get(CapturePlan, plan_id)
    telescope = db.session.query(Telescope).filter_by(telescope_id=telescope_name).first()
    if row is None or telescope is None or row.telescope_id != telescope.id:
        return None

    encoded = json.dumps(plan_status)
    if len(encoded) <= 64 * 1024:
        row.last_status_json = encoded

    state = plan_status.get('state')
    if state in PLAN_STATES and row.state not in FINAL_PLAN_STATES:
        row.state = state
        if state in FINAL_PLAN_STATES:
            row.finished_at = datetime.utcnow()

    frames = plan_status.get('frames_done', plan_status.get('frames'))
    if isinstance(frames, int) and not isinstance(frames, bool):
        row.frames_done = frames

    if row.state == 'done' and row.overhead_s is None:
        row.overhead_s = _measured_overhead(row)
    db.session.commit()
    return row


def _measured_overhead(row):
    # Only plans without waits or covered steps, whose time is otherwise unknowable
    plan = row.plan
    kinds = {step.get('type') for step in plan.get('steps', []) if isinstance(step, dict)}
    if kinds & {'wait', 'dark', 'bias'} or not row.started_at or not row.finished_at:
        return None
    nominal = estimate_plan(plan, overhead_s=0)
    frames = row.frames_done or nominal['frames']
    if not frames:
        return None
    elapsed = (row.finished_at - row.started_at).total_seconds()
    overhead = (elapsed - nominal['seconds']) / frames
    return round(min(max(overhead, 0.5), 30.0), 2)


def catch_up_after_reconnect(client_id, telescope_name):
    # Ask once about the latest unfinished plan on this telescope
    from app.WebsocketServer import ClientError, client_manager
    from models.capture_plan import ACTIVE_PLAN_STATES, CapturePlan
    from models.tables import Telescope

    telescope = db.session.query(Telescope).filter_by(telescope_id=telescope_name).first()
    if telescope is None:
        return
    row = (
        CapturePlan.query.filter(CapturePlan.telescope_id == telescope.id, CapturePlan.state.in_(ACTIVE_PLAN_STATES))
        .order_by(CapturePlan.created_at.desc())
        .first()
    )
    if row is None:
        return
    try:
        status = client_manager.command(client_id, "getCapturePlanStatus", [row.id], timeout=10)
    except ClientError as e:
        logger.info("Could not catch up on plan %s: %s", row.id, e)
        return
    if isinstance(status, dict) and not status.get('error'):
        status.setdefault('id', row.id)
        record_plan_status(telescope_name, status)


def stop_overdue_plans():
    """Backup for the Pi's own end_by: stop plans whose booking has ended."""
    from app.WebsocketServer import ClientError, client_manager
    from models.booking import TelescopeBooking
    from models.capture_plan import ACTIVE_PLAN_STATES, CapturePlan
    from models.tables import Telescope

    now = datetime.utcnow()
    rows = CapturePlan.query.filter(CapturePlan.state.in_(ACTIVE_PLAN_STATES), CapturePlan.stop_requested_at.is_(None)).all()
    for row in rows:
        overdue = row.end_by is not None and row.end_by <= now
        if row.booking_id and not overdue:
            booking = db.session.get(TelescopeBooking, row.booking_id)
            overdue = booking is None or booking.status != 'reserved' or booking.end_utc <= now
        if not overdue:
            continue
        telescope = db.session.get(Telescope, row.telescope_id)
        if telescope is None:
            continue
        try:
            client_manager.command(telescope.telescope_id, "stopCapturePlan", timeout=5)
            row.stop_requested_at = now
            print(f"[CapturePlan] Stopped {row.id} on {telescope.telescope_id}: its booking ended")
        except ClientError as e:
            # Offline: the Pi stops itself at end_by; try again next round
            logger.info("Could not stop plan %s: %s", row.id, e)
    db.session.commit()


def start_plan_watchdog(app, interval_s=30):
    def run():
        while True:
            time.sleep(interval_s)
            try:
                with app.app_context():
                    stop_overdue_plans()
            except Exception:
                logger.exception("Capture plan watchdog failed")

    threading.Thread(target=run, name="capture-plan-watchdog", daemon=True).start()


# Presets in the builder's Add menu
PRESETS = [
    {
        'key': 'moon_bracket',
        'name': 'Moon bracket',
        'plan': {'name': 'Moon bracket', 'steps': [
            {'type': 'goto', 'target': 'Moon'},
            {'type': 'bracket', 'exposures_s': [0.001, 0.004, 0.016], 'iso': 100, 'repeat': 3},
        ]},
    },
    {
        'key': 'planet_video',
        'name': 'Planet video',
        'plan': {'name': 'Jupiter video', 'steps': [
            {'type': 'goto', 'target': 'Jupiter'},
            {'type': 'video', 'duration_s': 60, 'quality': 'original'},
        ]},
    },
    {
        'key': 'deep_sky',
        'name': 'Deep sky with calibration',
        'plan': {'name': 'M42 with calibration', 'steps': [
            {'type': 'goto', 'name': 'M42', 'ra': 83.82, 'dec': -5.39, 'centre': True},
            {'type': 'light', 'count': 20, 'exposure_s': 30, 'iso': 800, 'dither_every': 5, 'dither_arcmin': 2},
            {'type': 'dark', 'count': 20, 'exposure_s': 30, 'iso': 800},
            {'type': 'bias', 'count': 30},
        ]},
    },
    {
        'key': 'timelapse',
        'name': 'Timelapse',
        'plan': {'name': 'Timelapse', 'steps': [
            {'type': 'timelapse', 'every_s': 10, 'duration_s': 1800, 'exposure_s': 2},
        ]},
    },
]
