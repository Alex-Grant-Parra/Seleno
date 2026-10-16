# GPS Location Module
# Retrieves GPS coordinates (latitude and longitude) and stores them in static client config state
# Supports both hardware GPS (via gpsd) and IP-based geolocation as fallback

import json
from datetime import datetime
from utils.config_state import save_location, get_location
from utils.log import get_logger

log = get_logger("location")


def get_gps_from_hardware():
    # The latest GPS fix from the background gpsd reader (utils/gps.py);
    # None without one. Never waits (it used to block on gpsd).
    from utils import gps

    if not gps.has_fix(max_age_s=120.0):
        return None
    fix = gps.last_fix()
    return {
        'latitude': fix['latitude'],
        'longitude': fix['longitude'],
        'altitude': fix.get('altitude'),
        'timestamp': datetime.utcnow().isoformat(),
        'source': 'hardware_gps'
    }


def get_gps_from_ip():
    # Get approximate location from IP address using ip-api.com (free service)
    # Returns: dict with lat, lon, timestamp if successful, None otherwise
    try:
        import urllib.request
        import urllib.error
        
        # ip-api.com's free service (no API key; plain HTTP only on the free tier)
        url = "http://ip-api.com/json/"
        
        with urllib.request.urlopen(url, timeout=5) as response:
            data = json.loads(response.read().decode())
            
            if data.get('status') == 'success':
                return {
                    'latitude': data.get('lat'),
                    'longitude': data.get('lon'),
                    'city': data.get('city'),
                    'region': data.get('regionName'),
                    'country': data.get('country'),
                    'isp': data.get('isp'),
                    'timestamp': datetime.utcnow().isoformat(),
                    'source': 'ip_geolocation'
                }
        return None
    except Exception as e:
        log.warning(f"IP geolocation error: {e}")
        return None


def update_location_config(use_network=True):
    # Update the location configuration file with current GPS coordinates
    # Tries hardware GPS first, falls back to IP geolocation. A site typed on
    # the front panel is kept unless a GPS fix replaces it, and use_network=False
    # (offline mode) skips the IP lookup.
    log.info("Attempting to retrieve GPS coordinates...")
    
    # Try hardware GPS first
    location_data = get_gps_from_hardware()
    
    # Fallback to IP-based geolocation
    if location_data is None:
        saved = get_location()
        if saved.get('source') == 'manual':
            log.info(f"Keeping the site set on the front panel: {saved.get('latitude')}, {saved.get('longitude')}")
            return True
        if not use_network:
            log.info("Offline: keeping the saved location")
            return bool(saved)
        log.warning("Hardware GPS not available, using IP geolocation...")
        location_data = get_gps_from_ip()
    
    if location_data is None:
        log.warning("Failed to retrieve location data")
        return False
    
    # Add metadata
    location_data['last_updated'] = datetime.utcnow().isoformat()
    
    # Save to static config state
    try:
        save_location(location_data)
        
        source = location_data.get('source', 'unknown')
        lat = location_data.get('latitude', 'N/A')
        lon = location_data.get('longitude', 'N/A')
        log.info(f"Location updated successfully via {source}: {lat}, {lon}")
        return True
    except Exception as e:
        log.warning(f"Failed to save location data: {e}")
        return False


def set_manual_location(latitude, longitude):
    # A site typed on the front panel; kept over IP geolocation at startup.
    save_location({
        'latitude': float(latitude),
        'longitude': float(longitude),
        'source': 'manual',
        'last_updated': datetime.utcnow().isoformat(),
    })


def forget_manual_location():
    # Let GPS or IP geolocation set the site again (keeps the coordinates
    # until they do).
    saved = get_location()
    if saved.get('source') == 'manual':
        saved['source'] = 'saved'
        save_location(saved)


def get_current_location():
    # Read the current location from the config file
    # Returns: dict with location data if file exists, None otherwise
    try:
        location = get_location()
        return location or None
    except Exception as e:
        log.warning(f"Failed to read location data: {e}")
        return None


if __name__ == "__main__":
    # Test the location module
    print("Testing location module...")
    update_location_config()
    
    location = get_current_location()
    if location:
        print(f"Current location: {json.dumps(location, indent=2)}")
    else:
        print("No location data available")
