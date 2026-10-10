"""Validate backend catalog metadata used by the pure request translator.

Derived from openai/codex, changed (Apache-2.0); see THIRD_PARTY_NOTICES.md.
"""
import json
import os
import queue
import tempfile
import threading

from .. import clock
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class Model:
    slug: str
    context_window: Optional[int]
    reasoning_levels: Tuple[str, ...]
    default_reasoning: Optional[str]
    default_verbosity: Optional[str]
    reasoning_summary: Optional[str]  # the summary a request names; None sends none
    responses_lite: bool
    service_tiers: Tuple[str, ...]


# codex-rs/protocol/src/openai_models.rs:195-202,228-232,406-489,846-851.
def parse(models_json) -> dict:
    """Read a decoded /codex/models response or Codex cache envelope.

    Unknown representations of consumed fields raise ValueError with the field
    name, so the caller can report the error and retain its last good catalog.
    Unrelated metadata is intentionally ignored. Optional Rust fields stay None.
    """
    if not isinstance(models_json, dict) or not isinstance(models_json.get('models'), list):
        raise ValueError('catalog.models must be an array in an object')
    models = {}
    for entry in models_json['models']:
        if not isinstance(entry, dict):
            raise ValueError('catalog.models entry must be an object')
        slug = _catalog_string(entry.get('slug'), 'slug')
        if slug in models:
            raise ValueError('duplicate catalog slug: ' + slug)
        window = entry.get('context_window')
        if window is not None and (type(window) is not int or window <= 0):
            raise ValueError('context_window must be a positive integer or null')
        levels = _catalog_options(entry.get('supported_reasoning_levels'), 'supported_reasoning_levels', 'effort')
        tiers = _catalog_options(entry.get('service_tiers', []), 'service_tiers', 'id')
        default = _catalog_string(entry.get('default_reasoning_level'), 'default_reasoning_level', nullable=True)
        verbosity = _catalog_string(entry.get('default_verbosity'), 'default_verbosity', nullable=True)
        summaries = _catalog_bool(entry, 'supports_reasoning_summaries', True)
        summary_parameter = _catalog_bool(entry, 'supports_reasoning_summary_parameter', True)
        summary = _catalog_string(entry.get('default_reasoning_summary', 'auto'), 'default_reasoning_summary')
        if summary not in _SUMMARIES:
            raise ValueError('default_reasoning_summary must be one of ' + ', '.join(_SUMMARIES))
        # The bridge's family fallback applies only when the catalog omits its flag.
        lite = _catalog_bool(entry, 'use_responses_lite', slug.rsplit('-', 1)[-1] in ('sol', 'terra', 'luna', 'astra'))
        sent = summary if summaries and summary_parameter and summary != 'none' else None
        models[slug] = Model(slug, window, levels, default, verbosity, sent, lite, tiers)
    return models


# codex-rs/protocol/src/config_types.rs:65-72 ReasoningSummary; serde default Auto.
_SUMMARIES = ('auto', 'concise', 'detailed', 'none')


# codex-rs/protocol/src/openai_models.rs:195-202,228-232.
def _catalog_options(value, field, member):
    if not isinstance(value, list):
        raise ValueError(field + ' must be an array of objects')
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(field + ' entry must be an object')
        result.append(_catalog_string(item.get(member), field + '.' + member))
    return tuple(result)


# codex-rs/protocol/src/openai_models.rs:406-489.
def _catalog_string(value, field, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(field + ' must be a nonempty string' + (' or null' if nullable else ''))
    return value


# codex-rs/protocol/src/openai_models.rs:440-446,483-485 (serde defaults).
def _catalog_bool(entry, field, default):
    value = entry.get(field, default)
    if type(value) is not bool:
        raise ValueError(field + ' must be a boolean')
    return value


class CatalogUnavailable(Exception):
    """No valid catalog has been read; the message is a safe operational cause."""


class Catalog:
    """Disk-first catalog, refreshed lazily, with one bounded refresh in flight."""

    def __init__(self, knobs, client, store, log):
        self.path, self.client, self.store, self.log = knobs.models_path, client, store, log
        self.lock = threading.Lock()
        self.copy = None
        self.cause = "no cached catalog"
        self.failure = None
        self.next_refresh = 0
        self.pending = None
        try:
            with open(self.path, encoding="utf-8") as source:
                self.copy = parse(json.load(source))
            age = max(0, clock.now() - os.stat(self.path).st_mtime)
            self.next_refresh = clock.monotonic() + max(0, 300 - age)
        except (OSError, ValueError) as error:
            self.cause = str(error)

    def count(self):
        """Health never fetches or waits on the refresh lock."""
        copy = self.copy
        return None if copy is None else len(copy)

    def _fetch(self, result):
        try:
            result.put((self.client.models(self.store.credentials()), None))
        except Exception as error:
            result.put((None, error))
        finally:
            self.client.drop()

    def _save(self, raw):
        directory = os.path.dirname(self.path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".chatgpt-models-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                json.dump(raw, target, ensure_ascii=False, separators=(",", ":"))
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    # codex-rs/models-manager/src/manager.rs:34-35: 300-second TTL; local scheduling is lazy.
    def models(self):
        with self.lock:
            if clock.monotonic() >= self.next_refresh:
                try:
                    if self.pending is None:
                        self.pending = queue.Queue(1)
                        threading.Thread(target=self._fetch, args=(self.pending,), daemon=True).start()
                    try:
                        raw, error = self.pending.get(timeout=10)
                    except queue.Empty:
                        raise CatalogUnavailable("catalog refresh timed out after 10 seconds") from None
                    self.pending = None
                    if error is not None:
                        raise error
                    models = parse(raw)
                    self._save(raw)
                    self.copy = models
                    self.next_refresh = clock.monotonic() + 300
                except Exception as error:
                    self.cause, self.failure = str(error), error
                    self.log("    gpt catalog refresh failed: " + self.cause)
                    self.next_refresh = clock.monotonic() + 60
            if self.copy is None:
                raise CatalogUnavailable(self.cause) from self.failure
            return self.copy
