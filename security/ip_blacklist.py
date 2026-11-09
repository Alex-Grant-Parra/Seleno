# IP blacklist manager for Telescope project
# Fetches malicious IPs from multiple threat intelligence sources
# and provides blocking functionality for Flask applications.

import bisect
import requests
import ipaddress
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Set, List, Dict
from pathlib import Path
import os

class IPBlacklist:
    def __init__(self, blacklist_file='blacklist.txt', update_interval=3600):
        # Initialize the IP blacklist manager
        self.blacklist_file = Path(blacklist_file)
        self.update_interval = update_interval
        self.blacklisted_ips = set()
        # Pre-parsed copy of blacklisted_ips for fast lookups (see _rebuild_index)
        self._index = (set(), {4: ([], []), 6: ([], [])})
        self.last_update = None
        self.logger = logging.getLogger(__name__)
        self._lock = threading.Lock()
        
        # Threat intelligence sources (free/public sources)
        self.sources = {
            'spamhaus_drop': {
                'url': 'https://www.spamhaus.org/drop/drop.txt',
                'parser': self._parse_spamhaus_drop
            },
            'spamhaus_edrop': {
                'url': 'https://www.spamhaus.org/drop/edrop.txt',
                'parser': self._parse_spamhaus_drop
            },
            'emergingthreats': {
                'url': 'https://rules.emergingthreats.net/fwrules/emerging-Block-IPs.txt',
                'parser': self._parse_simple_list
            },
            'blocklist_de': {
                'url': 'https://lists.blocklist.de/lists/all.txt',
                'parser': self._parse_simple_list
            },
            'cinsscore': {
                'url': 'http://cinsscore.com/list/ci-badguys.txt',
                'parser': self._parse_simple_list
            },
            'greensnow': {
                'url': 'https://blocklist.greensnow.co/greensnow.txt',
                'parser': self._parse_simple_list
            }
        }
        
        # Load existing blacklist
        self._load_blacklist()
        
        # Start background update thread
        self._start_background_updater()
    
    def _parse_spamhaus_drop(self, content: str) -> Set[str]:
        # Parse Spamhaus DROP/EDROP format
        ips = set()
        for line in content.split('\n'):
            line = line.strip()
            if line and not line.startswith(';') and not line.startswith('#'):
                # Extract CIDR block (first part before semicolon)
                if ';' in line:
                    cidr = line.split(';')[0].strip()
                    if self._is_valid_cidr(cidr):
                        ips.add(cidr)
        return ips
    
    def _parse_simple_list(self, content: str) -> Set[str]:
        # Parse simple IP list format
        ips = set()
        for line in content.split('\n'):
            line = line.strip()
            if line and not line.startswith('#') and not line.startswith(';'):
                if self._is_valid_ip_or_cidr(line):
                    ips.add(line)
        return ips
    
    def _is_valid_ip_or_cidr(self, ip_str: str) -> bool:
        # Check whether a string is a valid IP address or CIDR block
        try:
            ipaddress.ip_network(ip_str, strict=False)
            return True
        except ValueError:
            return False
    
    def _is_valid_cidr(self, cidr_str: str) -> bool:
        # Check whether a string is a valid CIDR block
        try:
            ipaddress.ip_network(cidr_str, strict=False)
            return '/' in cidr_str
        except ValueError:
            return False
    
    def _fetch_from_source(self, source_name: str, source_config: Dict) -> Set[str]:
        # Fetch IPs from a single source
        try:
            self.logger.info(f"Fetching IPs from {source_name}")
            response = requests.get(
                source_config['url'], 
                timeout=30,
                headers={'User-Agent': 'Telescope-Security-Bot/1.0'}
            )
            response.raise_for_status()
            
            ips = source_config['parser'](response.text)
            self.logger.info(f"Fetched {len(ips)} IPs from {source_name}")
            return ips
            
        except Exception as e:
            self.logger.error(f"Failed to fetch from {source_name}: {str(e)}")
            return set()
    
    def update_blacklist(self) -> bool:
        # Update blacklist from all configured sources
        self.logger.info("Starting blacklist update")
        new_ips = set()
        
        for source_name, source_config in self.sources.items():
            source_ips = self._fetch_from_source(source_name, source_config)
            new_ips.update(source_ips)
            time.sleep(1)
        
        with self._lock:
            self.blacklisted_ips = new_ips
            self.last_update = datetime.now()
            self._rebuild_index()
        
        self._save_blacklist()
        self.logger.info(f"Updated blacklist with {len(new_ips)} IPs/ranges")
        return True
    
    def _rebuild_index(self):
        # Parse the list once so each request is a set lookup plus a binary
        # search, instead of re-parsing all ~40k entries on every request.
        # Single IPs go in a set; CIDR ranges become merged, sorted
        # (start, end) integer spans per IP version.
        exact = set()
        spans = {4: [], 6: []}
        for item in list(self.blacklisted_ips):
            try:
                if '/' in item:
                    network = ipaddress.ip_network(item, strict=False)
                    spans[network.version].append((int(network.network_address), int(network.broadcast_address)))
                else:
                    exact.add(ipaddress.ip_address(item))
            except ValueError:
                continue
        ranges = {}
        for version, items in spans.items():
            items.sort()
            merged = []
            for start, end in items:
                if merged and start <= merged[-1][1] + 1:
                    if end > merged[-1][1]:
                        merged[-1] = (merged[-1][0], end)
                else:
                    merged.append((start, end))
            ranges[version] = ([start for start, _ in merged], [end for _, end in merged])
        self._index = (exact, ranges)  # swapped in one go, so readers never see half of it

    def is_ip_blacklisted(self, ip: str) -> bool:
        # Check whether an IP address is blacklisted
        try:
            client_ip = ipaddress.ip_address(ip)
        except ValueError:
            # Invalid IP address
            return False
        exact, ranges = self._index
        if client_ip in exact:
            return True
        starts, ends = ranges[client_ip.version]
        value = int(client_ip)
        i = bisect.bisect_right(starts, value) - 1
        return i >= 0 and value <= ends[i]
    
    def add_manual_ip(self, ip: str) -> bool:
        # Manually add an IP to the blacklist
        if self._is_valid_ip_or_cidr(ip):
            with self._lock:
                self.blacklisted_ips.add(ip)
                self._rebuild_index()
            self._save_blacklist()
            self.logger.info(f"Manually added {ip} to blacklist")
            return True
        return False
    
    def remove_ip(self, ip: str) -> bool:
        # Remove an IP from the blacklist
        with self._lock:
            if ip in self.blacklisted_ips:
                self.blacklisted_ips.remove(ip)
                self._rebuild_index()
                self._save_blacklist()
                self.logger.info(f"Removed {ip} from blacklist")
                return True
        return False
    
    def get_stats(self) -> Dict:
        # Return blacklist statistics
        with self._lock:
            return {
                'total_entries': len(self.blacklisted_ips),
                'last_update': self.last_update.isoformat() if self.last_update else None,
                'sources': list(self.sources.keys()),
                'blacklist_file': str(self.blacklist_file)
            }
    
    def _save_blacklist(self):
        # Save blacklist to text file
        try:
            # Ensure directory exists
            self.blacklist_file.parent.mkdir(parents=True, exist_ok=True)
            
            with open(self.blacklist_file, 'w') as f:
                # Write header with update info
                f.write(f"# Telescope Project IP Blacklist\n")
                f.write(f"# Last updated: {datetime.now().isoformat()}\n")
                f.write(f"# Total entries: {len(self.blacklisted_ips)}\n")
                f.write(f"# Sources: {', '.join(self.sources.keys())}\n")
                f.write(f"#\n")
                f.write(f"# Format: One IP address or CIDR range per line\n")
                f.write(f"# Lines starting with # are comments\n")
                f.write(f"#\n\n")
                
                # Write all IPs, sorted for easier reading
                sorted_ips = sorted(self.blacklisted_ips)
                for ip in sorted_ips:
                    f.write(f"{ip}\n")
                
            self.logger.info(f"Saved {len(self.blacklisted_ips)} IPs to {self.blacklist_file}")
                
        except Exception as e:
            self.logger.error(f"Failed to save blacklist: {str(e)}")
    
    def _load_blacklist(self):
        # Load blacklist from text file
        try:
            if self.blacklist_file.exists():
                with open(self.blacklist_file, 'r') as f:
                    loaded_ips = set()
                    for line in f:
                        line = line.strip()
                        # Skip empty lines and comments
                        if line and not line.startswith('#'):
                            if self._is_valid_ip_or_cidr(line):
                                loaded_ips.add(line)
                            else:
                                self.logger.warning(f"Invalid IP/CIDR in blacklist file: {line}")
                
                self.blacklisted_ips = loaded_ips
                self._rebuild_index()
                self.logger.info(f"Loaded {len(self.blacklisted_ips)} IPs from {self.blacklist_file}")
                
                with open(self.blacklist_file, 'r') as f:
                    for line in f:
                        if line.startswith('# Last updated:'):
                            try:
                                date_str = line.split('Last updated: ')[1].strip()
                                self.last_update = datetime.fromisoformat(date_str)
                                break
                            except (IndexError, ValueError):
                                pass
            else:
                # No blacklist file, do initial update
                self.logger.info("No blacklist file found, performing initial update")
                self.update_blacklist()
                
        except Exception as e:
            self.logger.error(f"Failed to load blacklist: {str(e)}")
            # Fallback to manual bad IPs
            self.blacklisted_ips = {'38.211.193.130', '38.211.193.0/24'}
            self._rebuild_index()
            self._save_blacklist()
    
    def _start_background_updater(self):
        # Start background thread for periodic updates
        def updater():
            while True:
                try:
                    if (self.last_update is None or 
                        datetime.now() - self.last_update > timedelta(seconds=self.update_interval)):
                        self.update_blacklist()
                    time.sleep(300)  # Check every 5 minutes
                except Exception as e:
                    self.logger.error(f"Background updater error: {str(e)}")
                    time.sleep(300)
        
        updater_thread = threading.Thread(target=updater, daemon=True)
        updater_thread.start()
        self.logger.info("Started background blacklist updater")

# Global instance
_blacklist_instance = None

def get_blacklist():
    # Return the global blacklist instance
    global _blacklist_instance
    if _blacklist_instance is None:
        # Store blacklist.txt in the security directory
        blacklist_file = Path(__file__).parent / 'blacklist.txt'
        _blacklist_instance = IPBlacklist(blacklist_file=blacklist_file)
    return _blacklist_instance
