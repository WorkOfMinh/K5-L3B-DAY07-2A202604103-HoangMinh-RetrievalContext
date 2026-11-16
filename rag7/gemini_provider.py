"""Gemini REST transport with pacing, durable response cache and explicit failures.

Only production runtime calls HTTPS. Unit tests inject a synthetic HTTP transport.
There is no provider fallback, paid-tier upgrade or model substitution.
"""
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, error
from .common import ROOT, object_hash, read_json, require, save_json

SUPPORTED_MODELS = {'gemini-3.8-flash', 'gemini-3.5-flash-lite'}


class ProviderFailure(RuntimeError):
    pass


class SessionPaused(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def redact(message, secrets=()):
    text = str(message)
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[REDACTED]')
    return re.sub(r'AIza[\w-]{20,}', '[REDACTED_GEMINI_KEY]', text)


def read_credentials():
    # Read only this project's .env; never export it or include its hash in outputs.
    from dotenv import dotenv_values
    values = dotenv_values(ROOT / '.env')
    key = os.environ.get('GEMINI_API_KEY') or values.get('GEMINI_API_KEY') or ''
    model = os.environ.get('GEMINI_MODEL') or values.get('GEMINI_MODEL') or 'gemini-3.5-flash-lite'
    require(key and key != 'your_actual_key_here', 'Set GEMINI_API_KEY in the Day 07 root .env file.')
    require(model in SUPPORTED_MODELS, 'Unsupported GEMINI_MODEL. Supported explicit profiles: gemini-3.5-flash-lite, gemini-3.8-flash. Match config/agent.json and use a new experiment for a model change.')
    return key, model


def append_jsonl(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')


def send_https(endpoint, key, payload, timeout):
    # Key is a header, not a URL/query parameter that might enter logs.
    req = request.Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                          headers={'Content-Type': 'application/json', 'x-goog-api-key': key}, method='POST')
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read().decode('utf-8')
    except error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode('utf-8', errors='replace')
    except (error.URLError, TimeoutError, OSError) as exc:
        raise ProviderFailure('Gemini network failure: ' + redact(exc, [key])) from None


def retry_delay(headers, body, attempt):
    values = [min(60.0, 10.0 * (2 ** attempt))]
    for name, value in headers.items():
        if name.casefold() == 'retry-after':
            try:
                values.append(float(value))
            except (ValueError, TypeError):
                pass
    for detail in body.get('error', {}).get('details', []):
        delay = detail.get('retryDelay', '')
        if isinstance(delay, str) and re.fullmatch(r'\d+(\.\d+)?s', delay):
            values.append(float(delay[:-1]))
    return max(values)


class GeminiClient:
    def __init__(self, key, config, folder, *, min_interval=15, max_calls=20,
                 timeout_seconds=180, transport=send_https, sleep=time.sleep, clock=time.monotonic):
        require(0 <= min_interval <= 60 and max_calls >= 1, 'Invalid pacing/request budget.')
        require(isinstance(timeout_seconds,(int,float)) and math.isfinite(timeout_seconds)
                and 10 <= timeout_seconds <= 600, 'API timeout must be 10-600 seconds.')
        require(config['model'] in SUPPORTED_MODELS and config['endpoint'] ==
                'https://generativelanguage.googleapis.com/v1beta/models/'+config['model']+':generateContent',
                'Unexpected Gemini endpoint/model.')
        self.key = key
        self.config = config
        self.folder = Path(folder)
        self.cache = self.folder / 'api_cache'
        self.cache.mkdir(parents=True, exist_ok=True)
        self.ledger = self.folder / 'api_attempts.jsonl'
        self.transport, self.sleep, self.clock = transport, sleep, clock
        self.interval, self.max_calls = min_interval, max_calls
        self.timeout_seconds = timeout_seconds
        self.new_calls, self.cache_hits, self.pacing_seconds = 0, 0, 0.0
        self.last_started = None
        self.version_path = self.folder / 'resolved_model.json'
        self.version = read_json(self.version_path)['model_version'] if self.version_path.exists() else None

    def wait(self, seconds):
        require(0 <= seconds <= 60, 'Long wait requires stopping and resuming later.')
        if seconds:
            print(f'API pacing: waiting {seconds:.1f}s.', flush=True)
            started = self.clock()
            self.sleep(seconds)
            self.pacing_seconds += self.clock() - started

    def generate(self, payload, *, namespace):
        identity = object_hash({'model': self.config['model'], 'namespace': namespace, 'request': payload})
        path = self.cache / (identity + '.json')
        if path.exists():
            cached = read_json(path)
            require(cached['request_sha256'] == object_hash(payload), 'API checkpoint request mismatch.')
            require(cached['record_sha256'] == object_hash({k:v for k,v in cached.items() if k != 'record_sha256'}),
                    'API checkpoint changed; stop and investigate.')
            self._check_version(cached['response'])
            self.cache_hits += 1
            return cached, True
        for attempt in range(3):
            if self.new_calls >= self.max_calls:
                raise SessionPaused('Session API request budget reached. Run the same command again to resume.')
            if self.last_started is not None:
                self.wait(max(0.0, self.interval - (self.clock() - self.last_started)))
            self.new_calls += 1
            self.last_started = self.clock()
            started = self.clock()
            append_jsonl(self.ledger, {'event':'attempt_started', 'at':utc_now(), 'request_id':identity,
                'namespace':namespace, 'request_sha256':object_hash(payload), 'attempt':attempt + 1,
                'socket_timeout_seconds':self.timeout_seconds})
            print(f'Calling Gemini (socket timeout {self.timeout_seconds:g}s; HTTP attempt {self.new_calls}/{self.max_calls}).',flush=True)
            try:
                status, headers, text = self.transport(self.config['endpoint'], self.key, payload, self.timeout_seconds)
            except Exception as exc:
                message = redact(exc,[self.key])
                if isinstance(exc,TimeoutError) or 'timed out' in message.casefold():
                    message += (' The configured socket timeout was '
                        f'{self.timeout_seconds:g}s. Provider processing/usage is unknown; no automatic duplicate request was sent. '
                        'Retry manually with --api-timeout 180 (or 300) and a new --experiment name when changing timeout/input settings.')
                append_jsonl(self.ledger, {'event':'attempt_failed', 'at':utc_now(), 'request_id':identity,
                    'error':message, 'network_seconds':self.clock()-started,
                    'socket_timeout_seconds':self.timeout_seconds,
                    'usage':'unknown; request may have reached provider'})
                raise ProviderFailure(message) from None
            elapsed = self.clock() - started
            try:
                body = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                body = {'error': {'message':'Non-JSON HTTP response'}}
            if not isinstance(body, dict):
                body = {'error': {'message':'Unexpected HTTP response shape'}}
            if 200 <= status < 300 and isinstance(body, dict) and 'error' not in body:
                record = {'request_id':identity, 'request_sha256':object_hash(payload), 'request':payload,
                    'response':body, 'network_seconds':elapsed, 'created_at':utc_now()}
                record['record_sha256'] = object_hash(record)
                # Save before parsing the model action; interrupted workflows can replay this response.
                save_json(path, record)
                append_jsonl(self.ledger, {'event':'attempt_succeeded', 'at':utc_now(), 'request_id':identity,
                    'network_seconds':elapsed, 'usage_metadata':body.get('usageMetadata'),
                    'response_id':body.get('responseId'), 'model_version':body.get('modelVersion')})
                self._check_version(body)
                return record, False
            message = redact(body.get('error', {}).get('message', 'Unknown API failure'), [self.key])
            append_jsonl(self.ledger, {'event':'attempt_failed', 'at':utc_now(), 'request_id':identity,
                'http_status':status, 'error':message, 'network_seconds':elapsed,
                'usage_metadata':body.get('usageMetadata')})
            if status == 429 and re.search(r'per.?day|daily|limit:\s*0', json.dumps(body), re.I):
                raise SessionPaused('Gemini daily/zero quota: ' + message + ' Check AI Studio limits; resume later.')
            if status == 429 or status in (500, 502, 503, 504):
                delay = retry_delay(headers, body, attempt)
                if delay > 60 or attempt == 2:
                    raise SessionPaused(f'Gemini HTTP {status}: {message}. Check quota/service and resume later.')
                print(f'Gemini HTTP {status}; retry {attempt + 1}/2.', flush=True)
                self.wait(delay)
                continue
            raise ProviderFailure(f'Gemini HTTP {status}: {message}')
        raise ProviderFailure('Unreachable retry state.')

    def _check_version(self, response):
        version = response.get('modelVersion')
        require(isinstance(version, str) and bool(version), 'Gemini response lacks modelVersion; cannot verify experiment provenance.')
        if self.version is None:
            self.version = version
            save_json(self.version_path, {'requested_model':self.config['model'], 'model_version':version})
        require(self.version == version, 'Resolved Gemini model version changed. Do not mix versions in this experiment.')
