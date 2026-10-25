"""Small, redacted JSONL audit trail independent of uvicorn's console log."""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app_paths import DATA_DIR

PATH = DATA_DIR / '.local-capture' / 'auth-rush.jsonl'
_logger = logging.getLogger('court.audit')
_logger.propagate = False
_logger.setLevel(logging.INFO)


def redact(value, *secrets):
    text = str(value or '')
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), '[redacted]')
    text = re.sub(r'(?i)(token|authorization|tel|phone)(["\x27]?\s*[:=]\s*["\x27]?)[^\s&,}"\x27]+', r'\1\2[redacted]', text)
    text = re.sub(r'(?i)Bearer\s+\S+', 'Bearer [redacted]', text)
    return re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '[phone]', text)[:500]


def record(event, *, token='', **fields):
    # Only explicit diagnostic fields; never serialize a snapshot or HTTP payload.
    allowed = {'phase', 'previous_phase', 'message', 'school_msg', 'auth_fail',
               'error_kind', 'ok', 'userid', 'auth_revision', 'source', 'run_id',
               'date', 'http_status', 'code', 'stale', 'persist_error',
               'groundId', 'groundName', 'gymId', 'gymName',
               'startDate', 'endDate', 'target_date', 'offset_days',
               'config_dates', 'blocks_sample', 'block_count', 'slot_step_min',
               'path', 'sync_at', 'auth_fail_at', 'ttl_sec', 'via',
               'action', 'reason', 'interval_sec',
               'startTime', 'endTime', 'method', 'response_ok',
               'sport_id', 'vid_preview', 'vid_age_ms', 'elapsed_ms', 'queue_ms',
               'sent', 'uncertain', 'dry_run', 'court', 'round', 'target_windows',
               'queries', 'query_errors', 'matches', 'rounds', 'content_type', 'response_bytes', 'timing',
               'stage', 'recheck_queries', 'recheck_errors', 'vid_errors', 'order_sends',
               'not_sent', 'captcha_type', 'captcha_length', 'failure_stage', 'json_error',
               'discarded', 'valid_count', 'backoff_attempt', 'retry_after_sec',
               'backoff_sec', 'effective_wait_sec', 'vid_attempt', 'vid_source',
               'scan_timeout_ms', 'clock_offset_ms', 'clock_uncertainty_ms'}
    from models.auth import token_preview
    entry = {'at': datetime.now(timezone(timedelta(hours=8))).isoformat(), 'event': event,
             'token_preview': token_preview(token)}
    entry.update({k: redact(v, token) if isinstance(v, str) else v
                  for k, v in fields.items() if k in allowed and v is not None})
    try:
        if not _logger.handlers:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(message)s'))
            _logger.addHandler(handler)
        _logger.info(json.dumps(entry, ensure_ascii=False))
    except OSError:
        logging.getLogger('court').warning('Audit file unavailable')
