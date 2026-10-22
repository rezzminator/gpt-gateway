"""Catalog producer shapes, including omitted serde defaults and invalid forms."""
import dataclasses
import unittest

from gpt_gateway.chatgpt import catalog


class CatalogTest(unittest.TestCase):
    def entry(self, **fields):
        return dict(slug='gpt-example', context_window=272000,
                    supported_reasoning_levels=[{'effort': 'low', 'description': 'Quick'},
                                                {'effort': 'xhigh', 'description': 'Deep'}],
                    default_reasoning_level='low', default_verbosity='low', **fields)

    def test_backend_and_cached_envelope(self):
        entry = self.entry(service_tiers=[{'id': 'priority', 'name': 'Fast', 'description': 'Faster'}])
        for envelope in ({'models': [entry]}, {'models': [entry], 'etag': 'synthetic', 'identity': {}}):
            with self.subTest(envelope=envelope):
                self.assertEqual(catalog.parse(envelope), {'gpt-example': catalog.Model(
                    'gpt-example', 272000, ('low', 'xhigh'), 'low', 'low', 'auto', ('priority',))})

    def test_backward_compatible_model_capability_defaults(self):
        for model in (catalog.Model('gpt-example', 272000, (), None, None, None, ()),
                      catalog.parse({'models': [self.entry()]})['gpt-example']):
            with self.subTest(model=model):
                self.assertIs(getattr(model, 'supports_reasoning_effort_updates', None), False)
                self.assertIs(getattr(model, 'support_verbosity', None), False)
                self.assertEqual(getattr(model, 'effective_context_window_percent', None), 95)
                self.assertEqual(getattr(model, 'effective_context_window', None), 258400)
                self.assertEqual(model.context_window, 272000)

    def test_explicit_capabilities_and_effective_context_window(self):
        entry = self.entry(supports_reasoning_effort_updates=True, support_verbosity=True,
                           effective_context_window_percent=80)
        entry['context_window'] = 101
        model = catalog.parse({'models': [entry]})['gpt-example']
        self.assertIs(getattr(model, 'supports_reasoning_effort_updates', None), True)
        self.assertIs(getattr(model, 'support_verbosity', None), True)
        self.assertEqual(getattr(model, 'effective_context_window_percent', None), 80)
        self.assertEqual(getattr(model, 'effective_context_window', None), 80)
        self.assertEqual(model.context_window, 101)
        entry['context_window'] = None
        model = catalog.parse({'models': [entry]})['gpt-example']
        self.assertIsNone(getattr(model, 'effective_context_window', 'missing'))

    def test_service_tier_resolver(self):
        # Codex normalizes the fast alias to priority before filtering advertised tiers.
        for tiers, requested, expected in (
                (('priority',), 'fast', 'priority'), (('priority',), 'priority', 'priority'),
                (('fast',), 'fast', None), (('fast', 'priority'), 'fast', 'priority'),
                (('future',), 'future', 'future'), (('priority',), 'unsupported', None),
                (('default', 'priority'), 'default', None), (('priority',), None, None),
                ((), 'fast', None), ((), 'priority', None), ((), 'flex', 'flex'),
                (('priority',), 'flex', 'flex')):
            with self.subTest(tiers=tiers, requested=requested):
                entry = self.entry(service_tiers=[{'id': tier} for tier in tiers],
                                   default_service_tier='priority')
                model = catalog.parse({'models': [entry]})['gpt-example']
                self.assertEqual(model.service_tiers, tiers)
                resolver = getattr(model, 'service_tier_for_request', None)
                self.assertTrue(callable(resolver), 'model must resolve request service tiers')
                self.assertEqual(resolver(requested), expected)
        entry = self.entry(additional_speed_tiers=['fast'])
        model = catalog.parse({'models': [entry]})['gpt-example']
        resolver = getattr(model, 'service_tier_for_request', None)
        self.assertTrue(callable(resolver), 'model must resolve request service tiers')
        self.assertIsNone(resolver('fast'), 'picker-only speed tiers must not authorize wire tiers')

    def test_capability_metadata_validated_at_entry(self):
        for field, values in (
                ('supports_reasoning_effort_updates', (None, 0, 1, 'true', [], {})),
                ('support_verbosity', (None, 0, 1, 'false', [], {})),
                ('effective_context_window_percent', (None, True, False, 0, -1, 101, 95.0, '95'))):
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, field):
                        catalog.parse({'models': [self.entry(**{field: value})]})

    def test_summary_flags(self):
        entry = self.entry(supports_reasoning_summaries=False)
        self.assertIsNone(catalog.parse({'models': [entry]})['gpt-example'].reasoning_summary)
        entry = self.entry(supports_reasoning_summaries=True, supports_reasoning_summary_parameter=False)
        self.assertIsNone(catalog.parse({'models': [entry]})['gpt-example'].reasoning_summary)

    def test_default_reasoning_summary_is_sent_unless_none(self):
        # codex-rs/core/src/client.rs:880-882: the request names a summary only when the resolved one is not none.
        for fields, expected in (({}, 'auto'), ({'default_reasoning_summary': 'none'}, None),
                                 ({'default_reasoning_summary': 'concise'}, 'concise'),
                                 ({'default_reasoning_summary': 'detailed', 'supports_reasoning_summary_parameter': False},
                                  None)):
            with self.subTest(fields=fields):
                self.assertEqual(catalog.parse({'models': [self.entry(**fields)]})['gpt-example'].reasoning_summary,
                                 expected)
        for bad in ('loud', '', 7, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                catalog.parse({'models': [self.entry(default_reasoning_summary=bad)]})

    def test_nullable_defaults_and_future_effort(self):
        entry = self.entry()
        entry.update(context_window=None, default_reasoning_level=None, default_verbosity=None,
                     supported_reasoning_levels=[{'effort': 'future', 'description': 'New'}])
        result = catalog.parse({'models': [entry]})
        self.assertIn('gpt-example', result)
        self.assertEqual(result['gpt-example'].reasoning_levels, ('future',))
        self.assertIsNone(result['gpt-example'].context_window)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result['gpt-example'].slug = 'other'

    def test_unknown_forms_raise_with_field(self):
        for field, value in [('context_window', 'big'), ('context_window', True),
                             ('supported_reasoning_levels', ['high']), ('supported_reasoning_levels', [{}]),
                             ('service_tiers', ['priority']), ('service_tiers', [{'name': 'Fast'}]),
                             ('supports_reasoning_summary_parameter', None),
                             ('default_reasoning_level', 5), ('default_verbosity', {}), ('slug', None)]:
            with self.subTest(field=field, value=value):
                entry = self.entry()
                entry[field] = value
                with self.assertRaisesRegex(ValueError, field):
                    catalog.parse({'models': [entry]})
        for bad in (None, [], {}, {'models': {}}, {'models': [False]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                catalog.parse(bad)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            catalog.parse({'models': [self.entry(), self.entry()]})


class CatalogStoreTest(unittest.TestCase):
    def setUp(self):
        import os
        from types import SimpleNamespace
        from unittest.mock import Mock
        from tests import jail, fakes
        self.path = os.path.join(jail.scratch(self), "models.json")
        self.knobs = SimpleNamespace(models_path=self.path)
        self.client, self.store = Mock(), Mock()
        self.client.models.return_value = fakes.CHATGPT_MODELS
        self.raw = fakes.CHATGPT_MODELS
        self.events = []
        self.record = lambda kind, **fields: self.events.append((kind, fields))

    def test_lazy_ttl_disk_fallback_and_atomic_permissions(self):
        import os
        import stat
        from unittest.mock import patch
        from tests import jail
        with patch("gpt_gateway.clock.monotonic", return_value=100) as timer:
            cache = catalog.Catalog(self.knobs, self.client, self.store, self.record)
            self.client.models.assert_not_called()
            self.assertIsNone(cache.count())
            self.assertIn("gpt-test", cache.models())
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
            self.assertEqual(self.client.models.call_count, 1)
            timer.return_value = 399
            cache.models()
            self.assertEqual(self.client.models.call_count, 1)
            timer.return_value = 400
            self.client.models.side_effect = ValueError("invented unavailable")
            self.assertIn("gpt-test", cache.models())
            self.assertTrue(jail.wait_for(lambda: cache.pending is None), "refresh never completed")
            timer.return_value = 459
            cache.models()
            self.assertEqual(self.client.models.call_count, 2)
            timer.return_value = 460
            cache.models()
            self.assertTrue(jail.wait_for(lambda: cache.pending is None), "retry never completed")
            self.assertEqual(self.client.models.call_count, 3)
            self.assertEqual(self.events, [("catalog_refresh_failed", {"cause": "invented unavailable"})] * 2)
            loaded = catalog.Catalog(self.knobs, self.client, self.store, self.record)
            self.assertEqual(loaded.count(), 1)
            self.assertIn("gpt-test", loaded.models())
            self.assertEqual(self.client.models.call_count, 3)

    def test_cached_models_return_during_one_refresh(self):
        import concurrent.futures
        import threading
        from unittest.mock import patch
        from tests import jail

        entered, release = threading.Event(), threading.Event()
        replacement = {"models": [dict(self.raw["models"][0], slug="gpt-new")]}

        def slow_models(_credentials):
            entered.set()
            if not release.wait(3):
                raise AssertionError("fake refresh was not released")
            return replacement

        with patch("gpt_gateway.clock.monotonic", return_value=100) as timer:
            cache = catalog.Catalog(self.knobs, self.client, self.store, self.record)
            self.assertIn("gpt-test", cache.models())
            timer.return_value = 400
            self.client.models.side_effect = slow_models
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                first = pool.submit(cache.models)
                try:
                    self.assertTrue(entered.wait(1), "fake refresh never started")
                    other = [pool.submit(cache.models) for _ in range(5)]
                    done, _ = concurrent.futures.wait([first] + other, timeout=0.5)
                    self.assertEqual(len(done), 6, "cached models blocked on the refresh")
                    for result in done:
                        self.assertIn("gpt-test", result.result())
                    self.assertEqual(self.client.models.call_count, 2)
                finally:
                    release.set()
            self.assertTrue(jail.wait_for(lambda: cache.pending is None), "refresh never completed")
            self.assertIn("gpt-new", cache.models())
            self.assertEqual(self.client.models.call_count, 2)

    def test_single_flight_and_no_copy_failure(self):
        import concurrent.futures
        from unittest.mock import patch
        with patch("gpt_gateway.clock.monotonic", return_value=100):
            cache = catalog.Catalog(self.knobs, self.client, self.store, self.record)
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda _: cache.models(), range(6)))
            self.assertEqual(len(results), 6)
            self.assertEqual(self.client.models.call_count, 1)
        self.knobs.models_path += ".missing"
        self.client.models.side_effect = ValueError("invented failure")
        cache = catalog.Catalog(self.knobs, self.client, self.store, self.record)
        with self.assertRaisesRegex(catalog.CatalogUnavailable, "invented failure"):
            cache.models()

    def test_timeout_retains_single_flight_and_waits_before_retry(self):
        import queue
        from unittest.mock import Mock, patch
        with patch("gpt_gateway.clock.monotonic", return_value=100) as timer:
            cache = catalog.Catalog(self.knobs, self.client, self.store, self.record)
            cache.pending = Mock()
            cache.pending.get.side_effect = queue.Empty
            with self.assertRaisesRegex(catalog.CatalogUnavailable, "10 seconds"):
                cache.models()
            cache.pending.get.assert_called_once_with(timeout=10)
            timer.return_value = 159
            with self.assertRaises(catalog.CatalogUnavailable):
                cache.models()
            self.assertEqual(cache.pending.get.call_count, 1)
            self.client.models.assert_not_called()
