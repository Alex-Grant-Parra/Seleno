from app.db import db
import logging

logger = logging.getLogger(__name__)
from sqlalchemy import func

def get_app():
    # Import inside function to avoid circular imports at module import time
    from Server import app
    return app



# Base class for reflection to be inherited by all models
class BaseTable(db.Model):

    __abstract__ = True  # This class is not directly mapped to a table

    @classmethod
    def get_all_fields(cls):
        return {column.name: getattr(cls, column.name) for column in cls.__table__.columns}

    @classmethod
    def reflect_table(cls, app=None):
        # This function used to call get_app() during module import which
        # caused circular imports because Server imports controllers which
        # import models.tables. Defer reflection until Server startup and
        # explicitly pass `app`.
        if app is None:
            app = get_app()
        with app.app_context():
            # perform any reflection or dynamic table adjustments here
            # keep minimal to avoid side-effects at import time
            return

    @classmethod
    def register_model_reflections(cls, app):
        # Explicit entrypoint to register any reflections or dynamic
        # adjustments that require the Flask app context. Call this from
        # Server startup after `db.create_all()`.
        # Example: if there are legacy tables we need to reflect, do it here.
        from app import db
        with app.app_context():
            # Placeholder: no-op for now — keep extensible
            return


    @classmethod
    def __init_subclass__(cls):
        super().__init_subclass__()
        # Do NOT call reflect_table() here to avoid importing the app
        # during module import. Reflection will be registered and run
        # explicitly from Server startup.
        return


# HDSTARtable: Define columns dynamically using reflection
class HDSTARtable(BaseTable):
    __tablename__ = 'HDSTARTable'  # The actual table name in the database
    __table_args__ = {'extend_existing': True}

    Name = db.Column(db.String(255), primary_key=True)
    RA = db.Column(db.Float, nullable=True)
    DEC = db.Column(db.Float, nullable=True)
    V_Mag = db.Column('V-Mag', db.Float, nullable=True)
    commonNames = db.Column(db.Text, nullable=True)

    @staticmethod
    def query_by_name(name):
        print(f"Querying HDSTARtable for name: {name}")
        result = db.session.query(HDSTARtable).filter(HDSTARtable.Name == name).first()
        if result:
            return {
                'Name': result.Name,
                'RA': result.RA,
                'DEC': result.DEC,
                'V-Mag': result.V_Mag,
                'commonNames': result.commonNames,
            }
        return None

    @staticmethod
    def query_by_name_flexible(name_or_hd: str):
        # Query HDSTARtable matching HD name case/space-insensitively, returns dict or None
        try:
            # Normalize: remove spaces and uppercase for comparison
            norm = (name_or_hd or "").strip().replace(" ", "").upper()
            if norm.startswith("HD"):
                result = db.session.query(HDSTARtable).filter(
                    func.replace(func.upper(HDSTARtable.Name), ' ', '') == norm
                ).first()
                if result:
                    return {
                        'Name': result.Name,
                        'RA': result.RA,
                        'DEC': result.DEC,
                        'V-Mag': result.V_Mag,
                        'commonNames': result.commonNames,
                    }
            # Fallback: try exact Name (case-sensitive as stored)
            result = db.session.query(HDSTARtable).filter(HDSTARtable.Name == name_or_hd).first()
            if result:
                return {
                    'Name': result.Name,
                    'RA': result.RA,
                    'DEC': result.DEC,
                    'V-Mag': result.V_Mag,
                    'commonNames': result.commonNames,
                }
        except Exception as e:
            logger.error(f"query_by_name_flexible failed: {e}")
        return None

    @staticmethod
    def query_by_common_name(common_name: str):
        # Query HDSTARtable by commonNames column using case-insensitive substring match
        try:
            result = db.session.query(HDSTARtable).filter(
                HDSTARtable.commonNames.ilike(f"%{common_name}%")
            ).first()
            if result:
                return {
                    'Name': result.Name,
                    'RA': result.RA,
                    'DEC': result.DEC,
                    'V-Mag': result.V_Mag,
                    'commonNames': result.commonNames,
                }
        except Exception as e:
            logger.error(f"query_by_common_name failed: {e}")
        return None


# IndexTable: Define columns dynamically using reflection
class IndexTable(BaseTable):
    __tablename__ = 'IndexTable'
    __table_args__ = {'extend_existing': True}

    Name = db.Column(db.String(255), primary_key=True)
    RA = db.Column(db.Float, nullable=True)
    DEC = db.Column(db.Float, nullable=True)
    V_Mag = db.Column('V-Mag', db.Float, nullable=True)

    @staticmethod
    def query_by_name(name):
        print(f"Querying IndexTable for name: {name}")
        result = db.session.query(IndexTable).filter(IndexTable.Name == name).first()
        if result:
            return {
                'Name': result.Name,
                'RA': result.RA,
                'DEC': result.DEC,
                'V-Mag': result.V_Mag,
            }
        return None


# NGCtable: Define columns dynamically using reflection
class NGCtable(BaseTable):
    __tablename__ = 'NGCtable'
    __table_args__ = {'extend_existing': True}

    Name = db.Column(db.String(255), primary_key=True)
    RA = db.Column(db.Float, nullable=True)
    DEC = db.Column(db.Float, nullable=True)
    V_Mag = db.Column('V-Mag', db.Float, nullable=True)
    Messier = db.Column(db.String(64), nullable=True)
    Common_names = db.Column('Common names', db.Text, nullable=True)

    @staticmethod
    def query_by_name(name):
        print(f"Querying NGCtable for name: {name}")
        result = db.session.query(NGCtable).filter(NGCtable.Name == name).first()
        if result:
            return {
                'Name': result.Name,
                'RA': result.RA,
                'DEC': result.DEC,
                'V-Mag': result.V_Mag,
                'Messier': result.Messier,
                'Common names': result.Common_names,
            }
        return None

    @staticmethod
    def query_by_messier(messier_designation):
        # Query NGCtable by Messier designation (e.g., 'M1', 'M31', 'M104')
        print(f"Querying NGCtable for Messier: {messier_designation}")
        result = db.session.query(NGCtable).filter(NGCtable.Messier == messier_designation).first()
        if result:
            return {
                'Name': result.Name,
                'RA': result.RA,
                'DEC': result.DEC,
                'V-Mag': result.V_Mag,
                'Messier': result.Messier,
                'Common names': result.Common_names,
            }
        return None

    @staticmethod
    def query_by_common_name(common_name):
        # Query NGCtable by common name using case-insensitive exact matching
        print(f"Querying NGCtable for common name: {common_name}")
        result = db.session.query(NGCtable).filter(
            NGCtable.Common_names.ilike(f"%{common_name}%")
        ).first()
        if result:
            return {
                'Name': result.Name,
                'RA': result.RA,
                'DEC': result.DEC,
                'V-Mag': result.V_Mag,
                'Messier': result.Messier,
                'Common names': result.Common_names,
            }
        return None


# Constellation figures for the star map. Each line joins two catalogue stars
# by designation, so the figure always lands exactly on the stars the map draws
# and no coordinates are stored twice. Populated by
# scripts/import_constellations.py.
class ConstellationsTable(BaseTable):
    __tablename__ = 'ConstellationsTable'
    __table_args__ = {'extend_existing': True}

    Abbr = db.Column(db.String(8), primary_key=True)        # IAU abbreviation, e.g. "Ori"
    Name = db.Column(db.String(64), nullable=False)         # e.g. "Orion"
    LabelRA = db.Column(db.Float, nullable=True)            # where to draw the name, degrees
    LabelDEC = db.Column(db.Float, nullable=True)


class ConstellationLinesTable(BaseTable):
    __tablename__ = 'ConstellationLinesTable'
    __table_args__ = (
        db.UniqueConstraint('Constellation', 'StarA', 'StarB', name='uq_constellation_line'),
        {'extend_existing': True},
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Constellation = db.Column(db.String(8), db.ForeignKey('ConstellationsTable.Abbr'),
                              nullable=False, index=True)
    StarA = db.Column(db.String(255), nullable=False)       # catalogue designation, e.g. "HD39801"
    StarB = db.Column(db.String(255), nullable=False)


# Star-map metadata added to HDSTARTable by scripts/import_star_names.py:
#   bayer      - Bayer designation, e.g. "β Per"
#   variableId - variable-star designation, e.g. "Bet Per"
#   magSource  - set only where V-Mag was filled in because the Henry Draper
#                catalogue recorded a placeholder instead of a magnitude; it
#                names the source and keeps the original placeholder, e.g.
#                "IAU-CSN; HD placeholder 30.0", so the change is reversible.
HD_STAR_MAP_COLUMNS = {
    'bayer': 'TEXT',
    'variableId': 'TEXT',
    'magSource': 'TEXT',
}


def ensure_star_catalogue_schema(engine=None):
    """Create the constellation tables and add the star-map columns to
    HDSTARTable if they are missing. Safe to call repeatedly."""
    from sqlalchemy import inspect, text

    engine = engine or db.engine
    ConstellationsTable.__table__.create(bind=engine, checkfirst=True)
    ConstellationLinesTable.__table__.create(bind=engine, checkfirst=True)

    inspector = inspect(engine)
    if 'HDSTARTable' not in inspector.get_table_names():
        return
    existing = {c['name'] for c in inspector.get_columns('HDSTARTable')}
    with engine.begin() as conn:
        for column, column_type in HD_STAR_MAP_COLUMNS.items():
            if column not in existing:
                conn.execute(text(f'ALTER TABLE "HDSTARTable" ADD COLUMN "{column}" {column_type}'))


# PlanetsTable: Define columns dynamically using reflection
class PlanetsTable(BaseTable):
    __tablename__ = 'PlanetsTable'  # The actual table name in the database
    __table_args__ = {'extend_existing': True}

    Name = db.Column(db.String(255), primary_key=True)
    SemiMajorAxis = db.Column(db.Float, nullable=True)
    Eccentricity = db.Column(db.Float, nullable=True)
    Inclination = db.Column(db.Float, nullable=True)
    AscNodeLong = db.Column(db.Float, nullable=True)
    ArgPeri = db.Column(db.Float, nullable=True)
    MeanAnomaly = db.Column(db.Float, nullable=True)
    LongitudeAtEpoch = db.Column(db.Float, nullable=True)
    V_Mag = db.Column('V-Mag', db.Float, nullable=True)

    @staticmethod
    def query_by_name(name):
        logger.debug(f"Querying PlanetsTable for name: {name}")
        result = db.session.query(PlanetsTable).filter(PlanetsTable.Name == name).first()
        if result:
            return {
                'Name': result.Name,
                'SemiMajorAxis': result.SemiMajorAxis,
                'Eccentricity': result.Eccentricity,
                'Inclination': result.Inclination,
                'AscNodeLong': result.AscNodeLong,
                'ArgPeri': result.ArgPeri,
                'MeanAnomaly': result.MeanAnomaly,
                'LongitudeAtEpoch': result.LongitudeAtEpoch,
                'V-Mag': result.V_Mag,
            }
        return None

    @staticmethod
    def load_planets():
        # Load all planets into a dictionary for easy access by name

        planets = db.session.query(PlanetsTable).all()
        return {row.Name.lower(): row for row in planets if getattr(row, 'Name', None)}


# Telescope model: For managing connected telescopes
class Telescope(db.Model):
    __tablename__ = 'telescopes'
    __table_args__ = {'extend_existing': True}
    
    # Explicit column definitions
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    telescope_id = db.Column(db.String(255), unique=True, nullable=False, index=True)
    ip_address = db.Column(db.String(45), nullable=True)  # IPv4 or IPv6
    type = db.Column(db.String(100), nullable=True)
    last_seen = db.Column(db.Float, nullable=True)  # Unix timestamp
    token_hash = db.Column(db.String(64), nullable=True, index=True)
    token_prefix = db.Column(db.String(16), nullable=True)
    token_created_at = db.Column(db.DateTime, nullable=True)
    # Ownership: which user registered this telescope (nullable for legacy/admin-created tokens)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'), nullable=True, index=True)
    # Approval workflow: new user-registered telescopes require admin approval before connecting
    is_approved = db.Column(db.Boolean, default=False, nullable=False)
    # Admin force-disable: keeps record in DB but blocks connections
    is_disabled = db.Column(db.Boolean, default=False, nullable=False)
    # Booking metadata and constraints
    description = db.Column(db.Text, nullable=True)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    specs_json = db.Column(db.Text, nullable=True)
    aperture = db.Column(db.String(128), nullable=True)
    camera = db.Column(db.String(255), nullable=True)
    min_booking_minutes = db.Column(db.Integer, nullable=False, default=30)
    max_booking_minutes = db.Column(db.Integer, nullable=False, default=720)
    allowed_windows_json = db.Column(db.Text, nullable=True)
    
    def __repr__(self):
        return f"<Telescope(id={self.id}, telescope_id='{self.telescope_id}', type='{self.type}')>"

    @property
    def name(self):
        return self.telescope_id

    @property
    def client_type(self):
        return self.type

    @property
    def created_at(self):
        return self.token_created_at
    
    @staticmethod
    def get_all_telescopes():
        # Get all telescopes from the database
        telescopes = db.session.query(Telescope).all()
        return telescopes
    
    @staticmethod
    def get_telescope_by_id(telescope_id):
        # Get a specific telescope by its telescope_id (human-readable name)
        result = db.session.query(Telescope).filter_by(telescope_id=telescope_id).first()
        if result:
            return {
                'id': result.id,
                'telescope_id': result.telescope_id,
                'ip_address': result.ip_address,
                'type': result.type,
                'last_seen': result.last_seen
            }
        return None
    
    @staticmethod
    def is_telescope_online(telescope_id):
        # Check if a telescope is considered online (seen within last 5 minutes)
        import time
        current_time = time.time()
        telescope = db.session.query(Telescope).filter_by(telescope_id=telescope_id).first()
        if telescope and telescope.last_seen:
            # Consider telescope online if seen within last 5 minutes (300 seconds)
            return (current_time - telescope.last_seen) < 300
        return False
    
    @staticmethod
    def add_telescope(telescope_id, ip_address=None, telescope_type=None, last_seen=None):
        # Add a new telescope to the database, returns status dict
        try:
            import time
            if last_seen is None:
                last_seen = time.time()
            
            # Check if telescope already exists
            existing = db.session.query(Telescope).filter_by(telescope_id=telescope_id).first()
            if existing:
                return {"status": "error", "message": f"Telescope with ID '{telescope_id}' already exists"}
            
            # Create new telescope instance
            new_telescope = Telescope(
                telescope_id=telescope_id,
                ip_address=ip_address,
                type=telescope_type or "telescope",
                last_seen=last_seen
            )
            
            db.session.add(new_telescope)
            db.session.commit()
            
            return {"status": "success", "message": f"Telescope '{telescope_id}' added successfully", "id": new_telescope.id}
            
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to add telescope")
            return {"status": "error", "message": "Failed to add telescope"}
    
    @staticmethod
    def remove_telescope(telescope_id):
        # Remove a telescope from the database by telescope ID, returns status dict
        try:
            # Check if telescope exists
            telescope = db.session.query(Telescope).filter_by(telescope_id=telescope_id).first()
            if not telescope:
                return {"status": "error", "message": f"Telescope with ID '{telescope_id}' not found"}
            
            # Delete the telescope
            db.session.delete(telescope)
            db.session.commit()
            
            return {"status": "success", "message": f"Telescope '{telescope_id}' removed successfully"}
            
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to remove telescope")
            return {"status": "error", "message": "Failed to remove telescope"}
    
    @staticmethod
    def update_last_seen(telescope_id, last_seen=None):
        # Update the last seen timestamp for a telescope, returns status dict
        try:
            import time
            if last_seen is None:
                last_seen = time.time()
            
            telescope = db.session.query(Telescope).filter_by(telescope_id=telescope_id).first()
            
            if not telescope:
                return {"status": "error", "message": f"Telescope with ID '{telescope_id}' not found"}
            
            telescope.last_seen = last_seen
            db.session.commit()
            
            return {"status": "success", "message": f"Updated last seen for telescope '{telescope_id}'"}
            
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to update telescope")
            return {"status": "error", "message": "Failed to update telescope"}
    
    @staticmethod
    def update_ip_address(telescope_id, ip_address):
        # Update the IP address for a telescope, returns status dict
        try:
            telescope = db.session.query(Telescope).filter_by(telescope_id=telescope_id).first()
            
            if not telescope:
                return {"status": "error", "message": f"Telescope with ID '{telescope_id}' not found"}
            
            telescope.ip_address = ip_address
            db.session.commit()
            
            return {"status": "success", "message": f"Updated IP address for telescope '{telescope_id}'"}
            
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to update telescope IP")
            return {"status": "error", "message": "Failed to update telescope"}
