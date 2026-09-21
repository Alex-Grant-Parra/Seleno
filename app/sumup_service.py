import json
import os
from urllib import error, parse, request


# Easy in-code toggles:
# - SUMUP_FEATURE_ENABLED: turn SumUp integration on/off quickly.
# - SUMUP_MODE: choose exactly one mode for credential selection.
#   Accepted values: 'live' or 'sandbox'.
SUMUP_FEATURE_ENABLED = True
SUMUP_MODE = 'sandbox'


class SumUpAPIError(Exception):
    def __init__(self, message, status_code=None, response_payload=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_payload = response_payload


def is_sumup_feature_enabled():
    return bool(SUMUP_FEATURE_ENABLED)


def _resolve_mode():
    mode = (SUMUP_MODE or '').strip().lower()
    if mode not in {'live', 'sandbox'}:
        raise SumUpAPIError("Invalid SUMUP_MODE. Use 'live' or 'sandbox'.")
    return mode


def current_sumup_mode():
    return _resolve_mode()


def resolve_sumup_credentials():
    if not is_sumup_feature_enabled():
        raise SumUpAPIError('SumUp feature is disabled in code toggle.')

    mode = _resolve_mode()
    if mode == 'sandbox':
        api_key = (os.getenv('SUMUP_TEST_API_KEY') or '').strip()
        merchant_code = (os.getenv('SUMUP_TEST_MERCHANT_CODE') or '').strip()
        is_test_mode = True
    else:
        api_key = (os.getenv('SUMUP_API_KEY') or '').strip()
        merchant_code = (os.getenv('SUMUP_MERCHANT_CODE') or '').strip()
        is_test_mode = False

    if not api_key or not merchant_code:
        raise SumUpAPIError('SumUp credentials are missing for the selected mode.')

    return {
        'api_key': api_key,
        'merchant_code': merchant_code,
        'is_test_mode': is_test_mode,
    }


def _sumup_request(method, path, api_key, body=None, query=None, timeout=15):
    base_url = (os.getenv('SUMUP_API_BASE_URL') or 'https://api.sumup.com').strip().rstrip('/')
    target = f"{base_url}/{path.lstrip('/')}"

    if query:
        query_string = parse.urlencode(query, doseq=True)
        target = f"{target}?{query_string}"

    headers = {
        'Authorization': f'Bearer {api_key}',
        'Accept': 'application/json',
    }

    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode('utf-8')

    req = request.Request(target, data=data, headers=headers, method=method.upper())

    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8')
            if not raw:
                return {}
            return json.loads(raw)
    except error.HTTPError as exc:
        raw = ''
        payload = None
        try:
            raw = exc.read().decode('utf-8', errors='ignore')
            payload = json.loads(raw) if raw else None
        except Exception:
            payload = {'raw': raw} if raw else None
        raise SumUpAPIError(
            message='SumUp API request failed.',
            status_code=exc.code,
            response_payload=payload,
        )
    except error.URLError as exc:
        raise SumUpAPIError(f'SumUp network error: {exc.reason}')


def create_checkout(api_key, payload):
    return _sumup_request('POST', '/v0.1/checkouts', api_key, body=payload)


def retrieve_checkout(api_key, checkout_id):
    return _sumup_request('GET', f'/v0.1/checkouts/{checkout_id}', api_key)


def deactivate_checkout(api_key, checkout_id):
    return _sumup_request('DELETE', f'/v0.1/checkouts/{checkout_id}', api_key)


def get_available_payment_methods(api_key, merchant_code, amount=None, currency=None):
    query = {}
    if amount is not None:
        query['amount'] = amount
    if currency:
        query['currency'] = currency
    return _sumup_request('GET', f'/v0.1/merchants/{merchant_code}/payment-methods', api_key, query=query)
