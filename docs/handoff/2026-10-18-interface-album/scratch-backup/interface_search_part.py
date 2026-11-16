def extract_friendly_common_name(common_names_field: str) -> str:
    # Extract the first friendly name from commonNames field; skip catalog designations
    if not common_names_field:
        return ''
    parts = [p.strip() for p in common_names_field.split(',')]
    for name in parts:
        name_upper = name.upper()
        # Skip catalog designations (HD, NGC, IC, M followed by number)
        if (name_upper.startswith('HD') or 
            name_upper.startswith('NGC') or 
            name_upper.startswith('IC') or 
            (name_upper.startswith('M') and len(name) > 1 and name[1:].strip().replace(' ', '').isdigit())):
            continue
        # Found a friendly name
        return name
    return ''

def _catalog_search_result(entry):
    """A star_catalog.lookup() entry keyed like the catalogue tables' columns,
    which is what the search result's detail table has always shown."""
    name, record = entry["name"], entry["record"]
    result = {"Name": name, "RA": entry["ra"], "DEC": entry["dec"], "V-Mag": entry["mag"]}
    names_cell = ", ".join(record.get("aliases", ())) or None
    if name.upper().startswith("HD"):
        result["commonNames"] = names_cell
    else:
        result["Common names"] = names_cell
        if record.get("messier"):
            result["Messier"] = record["messier"]
    return result


@interface_bp.route("/search_object", methods=["POST"])
def search_object():
    data = request.json
    search_value = data.get("searchValue", "").strip()

    print(f"Received search query: {search_value}")

    result = None

    searchableCelestials = ["sun", "moon", "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune", "pluto"]
    try:
        norm = search_value.strip()
        if norm.lower() in searchableCelestials:
            search_value = norm.lower()
            now = datetime.utcnow()
            CelestialData = getAllCelestialData(now.year, now.month, now.day, now.hour, now.minute, now.second)

            if search_value in CelestialData:
                formattedData = format_celestial_data(search_value, CelestialData[search_value])
                print(formattedData)
                result = formattedData
            else:
                print("Celestial object not found.")

        else:
            # Designations in any common spelling (HD 48915, NGC224, M31), then
            # names: exact alias first (so "Deneb" doesn't land on Denebola),
            # then a name prefix, then a substring of any alias
            entry = star_catalog.lookup(norm)
            if entry is None:
                print(f"Searching by common name across the catalogue: {norm}")
                designation = star_catalog.find_by_proper_name(norm)
                entry = star_catalog.lookup(designation) if designation else None
            if entry is None:
                print("Object not found")
                return jsonify({"status": "error", "message": "Object not found"})
            result = _catalog_search_result(entry)

    except ValueError as e:
        print(f"Error during search: {e}")
        return jsonify({"status": "error", "message": "Invalid search format"})

    if result:
        # Check if result is a dictionary
        if isinstance(result, dict):
            result_data = result
        else:
            result_data = {column: getattr(result, column) for column in result.__table__.columns.keys()}

        name = result_data.get('Name', "Null")
        ra = float(result_data.get('RA', 0))  # Default to 0 if RA is missing or None
        dec = float(result_data.get('DEC', 0))  # Default to 0 if DEC is missing or None
        mag = result_data.get('V-Mag', 0)  # Default to 0 if V-Mag is missing or None

        # Bayer letter, variable-star ID and displayed name from the catalogue
        try:
            record = star_catalog.star_name_record(name) or {}
        except Exception:
            record = {}

        if record.get('bayer'):
            result_data['bayer'] = record['bayer']
        if record.get('var'):
            result_data['variableId'] = record['var']

        # Extract friendly common name (non-HD variant) if available
        friendly_name = record.get('name')
        if not friendly_name:
            common_names_raw = result_data.get('commonNames', '') or result_data.get('Common names', '')
            friendly_name = extract_friendly_common_name(common_names_raw)
        if friendly_name:
            result_data['friendlyName'] = friendly_name


        # session["selectedObject"] = { # Adds to flask's session
        #     "name": name,
        #     "ra": ra,
        #     "dec": dec,
        #     "mag": mag
        # }
        return jsonify({"status": "success", "data": result_data})
    else:
        print("Object not found")
        return jsonify({"status": "error", "message": "Object not found"})
    


def format_celestial_data(name, data):
    ra_hours = convert.HrMinSecToDegrees(data['ra'][0], data['ra'][1], data['ra'][2])
    ra_degrees = ra_hours * 15  # Convert hours to degrees (360/24h = 15/h)
    
    dec_degrees = convert.HrMinSecToDegrees(data['dec'][0], data['dec'][1], data['dec'][2])
    vmag = data.get("vmag")
    if vmag is None:
        vmag = _CELESTIAL_DEFAULT_VMAGS.get(name.lower(), 30)
    
    formatted = {
        "Name": name.capitalize(),
        "RA": ra_degrees,
        "DEC": dec_degrees,
        "V-Mag": vmag
    }

    if data.get("phase_name"):
        formatted["phase_name"] = data.get("phase_name")
    phase_angle = data.get("phase_angle_deg")
    if phase_angle is not None:
        try:
            formatted["phase_angle_deg"] = float(phase_angle)
        except Exception:
            pass

    moon_illum = data.get("moon_illumination_fraction")
    if moon_illum is not None:
        try:
            formatted["moon_illumination_fraction"] = float(moon_illum)
        except Exception:
            pass

    moon_elong = data.get("moon_elongation_deg")
    if moon_elong is not None:
        try:
            formatted["moon_elongation_deg"] = float(moon_elong)
        except Exception:
            pass

    return formatted

