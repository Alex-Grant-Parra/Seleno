# Security configuration for telescope project

# IP Blacklist Configuration
BLACKLIST_CONFIG = {
    'update_interval': 3600,  # Update every hour (3600 seconds)
    'blacklist_file': 'security/blacklist.txt', 
    'log_file': 'security/logs/security.log',
    'enable_auto_blacklist': True,  # Automatically blacklist aggressive attackers
    'aggressive_threshold': 3,  # Number of suspicious requests before auto-blacklist
}

# Threat Intelligence Sources
# You can enable/disable sources by setting 'enabled': False
THREAT_SOURCES = {
    'spamhaus_drop': {
        'enabled': True,
        'description': 'Spamhaus DROP list - known bad networks'
    },
    'spamhaus_edrop': {
        'enabled': True,
        'description': 'Spamhaus EDROP list - extended bad networks'
    },
    'emergingthreats': {
        'enabled': True,
        'description': 'Emerging Threats IP list'
    },
    'blocklist_de': {
        'enabled': True,
        'description': 'Blocklist.de - failed login attempts and compromised hosts'
    },
    'cinsscore': {
        'enabled': True,
        'description': 'CINS Score bad guys list'
    },
    'greensnow': {
        'enabled': True,
        'description': 'GreenSnow blacklist'
    }
}

# Suspicious request patterns
SUSPICIOUS_PATTERNS = {
    'paths': [
        'cgi-bin', 'phpunit', 'wp-admin', 'wp-content', '.php',
        'eval-stdin', 'shell', '/bin/', 'cmd.exe', 'powershell',
        '../', '..\\', '%2e%2e', 'etc/passwd', 'boot.ini',
        'proc/self', 'windows/system32'
    ],
    'user_agents': [
        'sqlmap', 'nikto', 'nmap', 'masscan', 'zap', 'burp',
        'scanner', 'exploit', 'nessus'
    ]
}

# Rate limit cleanup configuration
RATE_LIMIT_CONFIG = {
    'cleanup_interval': 300,  # Clean old entries every 5 minutes
    'tracking_window': 60,    # Track requests in 1-minute windows
    'suspicious_decay_time': 3600,  # Reset suspicious count after 1 hour of no suspicious requests
}

# Rate limiting (requests per IP per minute)
# Generous limits for normal users to avoid false positives
# Stricter limits for clearly malicious behavior
RATE_LIMITS = {
    'default': 120,         # 120 requests per minute for normal users (2 per second)
    'suspicious': 15,       # 15 requests per minute for suspicious IPs
    'aggressive_attack': 5, # 5 requests per minute after multiple malicious indicators
    'blocked': 0            # 0 requests for blocked IPs
}

# Paths left out of the normal per-minute limit. The world map's weather layers
# read many small pieces of data (dozens per layer, more while scrubbing the
# forecast slider), which would trip the limit. Suspicious requests are still limited.
RATE_LIMIT_EXEMPT_PATHS = [
    '/map/weather/',
]

# Security logging
LOGGING_CONFIG = {
    'log_blocked_requests': True,
    'log_suspicious_requests': True,
    'log_successful_requests': False,  # Only log security events
    'max_log_size_mb': 10,  # Rotate logs when they exceed 10MB
    'backup_count': 5       # Keep 5 backup log files
}

# Request logging - logs ALL requests with true client IPs
REQUEST_LOGGING_CONFIG = {
    'enabled': True,
    'storage': 'database',
    'table_name': 'request_logs',
    'log_format': 'json',  
    'exclude_paths': [     # Don't log these paths to reduce noise
        '/static/',
        '/map/weather/',   # the world map reads weather data in many small pieces
        '/favicon.ico',
        '/robots.txt'
    ],
    'include_headers': [   # Additional headers to log
        'User-Agent',
        'Referer', 
        'Accept-Language',
        'X-Forwarded-For',
        'X-Real-IP'
    ]
}