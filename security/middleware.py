from flask import Flask, request, jsonify, abort
import logging
from datetime import datetime, timedelta
from typing import Dict, Any
import json
from functools import wraps
from flask_login import current_user
from pathlib import Path
from .ip_blacklist import get_blacklist
from .config import REQUEST_LOGGING_CONFIG
from models.logging import RequestLog, SecurityLog

class SecurityMiddleware:
    def __init__(self, app: Flask = None, log_file: str = None):
        # Initialize security middleware
        self.app = app
        self.blacklist = get_blacklist()
        
        # Rate limiting tracking: {ip: [timestamps]}
        self.request_history = {}
        self.suspicious_request_counts = {}
        
        # Setup security logging
        if log_file is None:
            log_file = Path(__file__).parent / 'logs' / 'security.log'
        
        self.log_file = Path(log_file)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Configure security logger
        self.security_logger = logging.getLogger('security')
        self.security_logger.setLevel(logging.INFO)
        
        # Security events now persist to DB; avoid binding file handlers here.
        if not self.security_logger.handlers:
            self.security_logger.addHandler(logging.NullHandler())
        
        # Setup request logging if enabled
        self.request_logger = None
        if REQUEST_LOGGING_CONFIG.get('enabled', False):
            self._setup_request_logger()
        
        if app is not None:
            self.init_app(app)
    
    def init_app(self, app: Flask):
        # Initialize middleware with Flask app
        self.app = app
        
        # Register before_request handler
        app.before_request(self._before_request)
        
        # Register security routes
        self._register_security_routes()
        
        self.security_logger.info("Security middleware initialized")
    
    def _setup_request_logger(self):
        # Request logging now persists to the request_logs DB table.
        self.request_logger = True

        try:
            RequestLog.save_request({
                'timestamp': datetime.now().isoformat(),
                'client_ip': 'system',
                'method': 'INIT',
                'path': '/security/request-logging',
                'url': 'internal://request-logging',
                'query_string': '',
                'remote_addr': 'local',
                'scheme': 'internal',
                'headers': {'User-Agent': 'security-middleware-init'}
            })
        except Exception as e:
            self.security_logger.error(f"Failed to initialize request logging: {e}")
        
        self.security_logger.info("Request logging initialized (database table: request_logs)")
    
    def _log_request(self):
        # Log every request with true client IP and details
        if not self.request_logger:
            return
            
        # Check if we should exclude this path
        exclude_paths = REQUEST_LOGGING_CONFIG.get('exclude_paths', [])
        for exclude_path in exclude_paths:
            if request.path.startswith(exclude_path):
                return
        
        client_ip = self._get_client_ip()
        
        # Prepare request data
        request_data = {
            'timestamp': datetime.now().isoformat(),
            'client_ip': client_ip,
            'method': request.method,
            'path': request.path,
            'url': request.url,
            'query_string': request.query_string.decode('utf-8') if request.query_string else '',
            'remote_addr': request.remote_addr,  # This will be 127.0.0.1 from Caddy
            'scheme': request.scheme,
            'headers': {}
        }
        
        # Add specified headers
        include_headers = REQUEST_LOGGING_CONFIG.get('include_headers', [])
        for header_name in include_headers:
            header_value = request.headers.get(header_name)
            if header_value:
                request_data['headers'][header_name] = header_value
        
        # Persist request logs to DB table
        try:
            RequestLog.save_request(request_data)
        except Exception as e:
            self.security_logger.error(f"Error writing request log: {e}")
    
    def _get_client_ip(self) -> str:
        # Get the real client IP address
        # Check for various forwarded headers (for reverse proxy scenarios)
        forwarded_headers = [
            'CF-Connecting-IP',  # Cloudflare
            'X-Forwarded-For',   # Standard proxy header
            'X-Real-IP',         # Nginx
            'X-Client-IP',       # Alternative
        ]
        
        for header in forwarded_headers:
            ip = request.headers.get(header)
            if ip:
                # Take the first IP if comma-separated
                return ip.split(',')[0].strip()
        
        # Fallback to remote_addr
        return request.remote_addr or 'unknown'
    
    def _log_security_event(self, event_type: str, details: Dict[str, Any]):
        # Log security events
        client_ip = self._get_client_ip()
        
        log_entry = {
            'timestamp': datetime.now().isoformat(),
            'event_type': event_type,
            'client_ip': client_ip,
            'user_agent': request.headers.get('User-Agent', 'unknown'),
            'method': request.method,
            'url': request.url,
            'path': request.path,
            'details': details
        }
        
        # Persist security event to DB and keep logger call for diagnostics.
        SecurityLog.save_event(log_entry, level='INFO')
        self.security_logger.info(json.dumps(log_entry))
        
        return log_entry
    
    def _is_suspicious_request(self) -> Dict[str, Any]:
        # Check whether the request looks suspicious
        suspicious_indicators = {}
        
        # Check for common attack patterns in URL
        suspicious_paths = [
            'cgi-bin', 'phpunit', 'wp-admin', 'wp-content', '.php',
            'eval-stdin', 'shell', '/bin/', 'cmd.exe', 'powershell',
            '../', '..\\', '%2e%2e', 'etc/passwd', 'boot.ini',
            'proc/self', 'windows/system32'
        ]
        
        path_lower = request.path.lower()
        query_lower = request.query_string.decode('utf-8', errors='ignore').lower()
        
        for pattern in suspicious_paths:
            if pattern in path_lower or pattern in query_lower:
                suspicious_indicators['suspicious_path'] = pattern
                break
        
        # Check for config/secret file probes (high indicator of scanning)
        config_file_patterns = [
            '.env', 'docker-compose', 'config.', 'secrets.', 'credentials.',
            '.sql', 'backup.', '.bak', '.old', '.save', '.tmp',
            'wp-config', 'settings.', '.swp', '~', '.git'
        ]
        
        for pattern in config_file_patterns:
            if pattern in path_lower:
                suspicious_indicators['config_file_probe'] = pattern
                break
        
        # Check for suspicious headers
        user_agent = request.headers.get('User-Agent', '').lower()
        suspicious_agents = [
            'sqlmap', 'nikto', 'nmap', 'masscan', 'zap', 'burp',
            'crawler', 'bot', 'spider', 'scanner', 'exploit'
        ]
        
        for agent in suspicious_agents:
            if agent in user_agent:
                suspicious_indicators['suspicious_user_agent'] = agent
                break
        
        # Check for unusual methods
        if request.method not in ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD']:
            suspicious_indicators['unusual_method'] = request.method
        
        # Check for suspicious content types
        content_type = request.headers.get('Content-Type', '').lower()
        if 'php' in content_type or 'eval' in content_type:
            suspicious_indicators['suspicious_content_type'] = content_type
        
        return suspicious_indicators
    
    def _check_rate_limit(self, client_ip: str, is_suspicious: bool = False, is_aggressive: bool = False) -> bool:
        # Check whether client has exceeded rate limit; return True when blocked
        from .config import RATE_LIMITS, RATE_LIMIT_CONFIG
        
        now = datetime.now()
        window = timedelta(seconds=RATE_LIMIT_CONFIG.get('tracking_window', 60))
        
        # Determine limit based on request type
        if is_aggressive:
            limit = RATE_LIMITS.get('aggressive_attack', 5)
        elif is_suspicious:
            limit = RATE_LIMITS.get('suspicious', 15)
        else:
            limit = RATE_LIMITS.get('default', 120)  # Generous for normal users
        
        # Initialize tracking for this IP
        if client_ip not in self.request_history:
            self.request_history[client_ip] = []
        
        # Remove old entries outside the time window
        self.request_history[client_ip] = [
            ts for ts in self.request_history[client_ip]
            if now - ts < window
        ]
        
        # Check if limit exceeded
        if len(self.request_history[client_ip]) >= limit:
            return True  # Rate limit exceeded
        
        # Record this request
        self.request_history[client_ip].append(now)
        return False  # OK to proceed
    
    def _before_request(self):
        # Handle request before processing
        # Get client IP early so can remove some processing for blacklisted
        # clients and avoid doing extra work (like heavy request logging).
        client_ip = self._get_client_ip()

        # Skip security checks for localhost in development
        if client_ip in ['127.0.0.1', '::1', 'localhost'] and self.app.debug:
            return

        # Check if IP is blacklisted and immediately abort to reduce load
        if self.blacklist.is_ip_blacklisted(client_ip):
            self._log_security_event('blocked_request', {
                'reason': 'ip_blacklisted',
                'blocked_ip': client_ip
            })

            abort(403)

        # Only log requests after we've cleared the blacklist check so that
        # blocked IPs do not generate full request logs.
        self._log_request()
        
        # Check for suspicious activity
        suspicious_indicators = self._is_suspicious_request()
        is_aggressive = False  # Track if this is an aggressive attack pattern
        
        if suspicious_indicators:
            self._log_security_event('suspicious_request', suspicious_indicators)
            
            # Track suspicious requests from this IP
            if client_ip not in self.suspicious_request_counts:
                self.suspicious_request_counts[client_ip] = {'count': 0, 'timestamp': datetime.now()}
            
            # Reset count if it's been over an hour since last suspicious request
            from .config import RATE_LIMIT_CONFIG
            decay_time = timedelta(seconds=RATE_LIMIT_CONFIG.get('suspicious_decay_time', 3600))
            if datetime.now() - self.suspicious_request_counts[client_ip]['timestamp'] > decay_time:
                self.suspicious_request_counts[client_ip]['count'] = 0
            
            self.suspicious_request_counts[client_ip]['count'] += 1
            self.suspicious_request_counts[client_ip]['timestamp'] = datetime.now()
            
            high_risk_indicators = ['suspicious_path', 'unusual_method']
            if any(indicator in suspicious_indicators for indicator in high_risk_indicators):
                # Check if this is part of a scanning/enumeration attack
                if self.suspicious_request_counts[client_ip]['count'] >= 3:
                    # Auto-blacklist aggressive attackers after 3+ suspicious requests
                    self.blacklist.add_manual_ip(client_ip)
                    is_aggressive = True
                    
                    self._log_security_event('auto_blacklisted', {
                        'reason': 'aggressive_attack_pattern',
                        'suspicious_count': self.suspicious_request_counts[client_ip]['count'],
                        'indicators': suspicious_indicators
                    })
                    
                    abort(403)
            
            # Rate limit suspicious requests more aggressively
            if self._check_rate_limit(client_ip, is_suspicious=True, is_aggressive=is_aggressive):
                self._log_security_event('rate_limit_exceeded', {
                    'reason': 'suspicious_request_rate',
                    'client_ip': client_ip,
                    'request_count': len(self.request_history.get(client_ip, []))
                })
                abort(429)  # Too Many Requests
        else:
            # Normal rate limiting for legitimate requests - now re-enabled!
            if self._check_rate_limit(client_ip, is_suspicious=False):
                self._log_security_event('rate_limit_exceeded', {
                    'reason': 'normal_request_rate',
                    'client_ip': client_ip,
                    'request_count': len(self.request_history.get(client_ip, []))
                })
                abort(429)  # Too Many Requests
    
    
    def _register_security_routes(self):
        # Register security management routes (admin only)

        def admin_only(view):
            @wraps(view)
            def wrapped(*args, **kwargs):
                if not current_user.is_authenticated or not getattr(current_user, 'is_admin', False):
                    return jsonify({'error': 'Admin access required'}), 403
                return view(*args, **kwargs)
            return wrapped

        @self.app.route('/admin/security/status')
        @admin_only
        def security_status():
            # Return security system status
            stats = self.blacklist.get_stats()
            return jsonify({
                'status': 'active',
                'blacklist_stats': stats,
                'blacklist_file': stats.get('blacklist_file'),
                'log_file': str(self.log_file)
            })
        
        @self.app.route('/admin/security/blacklist/add', methods=['POST'])
        @admin_only
        def add_to_blacklist():
            # Manually add an IP to blacklist
            data = request.get_json()
            if not data or 'ip' not in data:
                return jsonify({'error': 'IP address required'}), 400
            
            ip = data['ip']
            if self.blacklist.add_manual_ip(ip):
                self._log_security_event('manual_blacklist_add', {'ip': ip})
                return jsonify({'message': f'Added {ip} to blacklist'})
            else:
                return jsonify({'error': 'Invalid IP address'}), 400
        
        @self.app.route('/admin/security/blacklist/remove', methods=['POST'])
        @admin_only
        def remove_from_blacklist():
            # Remove an IP from blacklist
            data = request.get_json()
            if not data or 'ip' not in data:
                return jsonify({'error': 'IP address required'}), 400
            
            ip = data['ip']
            if self.blacklist.remove_ip(ip):
                self._log_security_event('manual_blacklist_remove', {'ip': ip})
                return jsonify({'message': f'Removed {ip} from blacklist'})
            else:
                return jsonify({'error': 'IP not found in blacklist'}), 404
        
        @self.app.route('/admin/security/logs')
        @admin_only
        def get_security_logs():
            # Return recent security logs
            try:
                rows = SecurityLog.query.order_by(SecurityLog.id.desc()).limit(100).all()
                logs = []
                for row in reversed(rows):
                    if row.payload_json:
                        try:
                            logs.append(json.loads(row.payload_json))
                            continue
                        except Exception:
                            pass
                    logs.append({
                        'timestamp': row.event_timestamp,
                        'event_type': row.event_type,
                        'client_ip': row.client_ip,
                        'method': row.method,
                        'path': row.path,
                        'url': row.url,
                    })

                return jsonify({'logs': logs})
            except Exception:
                self.security_logger.exception('Failed to load security logs')
                return jsonify({'error': 'Failed to load security logs'}), 500

# Error handlers for security responses
def register_security_error_handlers(app: Flask):
    # Register custom error handlers for security responses
    
    @app.errorhandler(403)
    def forbidden(e):
        # Return custom 403 response
        return jsonify({
            'error': 'Access forbidden',
            'message': 'Your request was blocked for security reasons'
        }), 403
    
    @app.errorhandler(429)
    def too_many_requests(e):
        # Return custom 429 response
        return jsonify({
            'error': 'Too many requests',
            'message': 'Rate limit exceeded'
        }), 429