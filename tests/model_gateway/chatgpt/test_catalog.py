"""Catalog producer shapes, including omitted serde defaults and invalid forms."""
import dataclasses
import unittest

from model_gateway.chatgpt import catalog


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
                    'gpt-example', 272000, ('low', 'xhigh'), 'low', 'low', 'auto', False, ('priority',))})

    def test_lite_fallback_and_explicit_false_and_summary_flags(self):
        for slug in ('gpt-6.1-sol', 'gpt-5.6-terra', 'gpt-6-luna', 'gpt-6-astra', 'gpt-5.5'):
            for override in (None, False, True):
                entry = self.entry(supports_reasoning_summaries=False)
                entry['slug'] = slug
                if override is not None:
                    entry['use_responses_lite'] = override
                result = catalog.parse({'models': [entry]})
                self.assertIn(slug, result)
                model = result[slug]
                self.assertEqual(model.responses_lite, slug != 'gpt-5.5' if override is None else override)
                self.assertIsNone(model.reasoning_summary)
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
                             ('use_responses_lite', 'false'), ('supports_reasoning_summary_parameter', None),
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
        self.lines = []

    def test_lazy_ttl_disk_fallback_and_atomic_permissions(self):
        import os
        import stat
        from unittest.mock import patch
        with patch("model_gateway.clock.monotonic", return_value=100) as timer:
            cache = catalog.Catalog(self.knobs, self.client, self.store, self.lines.append)
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
            timer.return_value = 459
            cache.models()
            self.assertEqual(self.client.models.call_count, 2)
            timer.return_value = 460
            cache.models()
            self.assertEqual(self.client.models.call_count, 3)
            self.assertIn("gpt catalog refresh failed: invented unavailable", self.lines[0])
            loaded = catalog.Catalog(self.knobs, self.client, self.store, self.lines.append)
            self.assertEqual(loaded.count(), 1)
            self.assertIn("gpt-test", loaded.models())
            self.assertEqual(self.client.models.call_count, 3)

    def test_single_flight_and_no_copy_failure(self):
        import concurrent.futures
        from unittest.mock import patch
        with patch("model_gateway.clock.monotonic", return_value=100):
            cache = catalog.Catalog(self.knobs, self.client, self.store, self.lines.append)
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda _: cache.models(), range(6)))
            self.assertEqual(len(results), 6)
            self.assertEqual(self.client.models.call_count, 1)
        self.knobs.models_path += ".missing"
        self.client.models.side_effect = ValueError("invented failure")
        cache = catalog.Catalog(self.knobs, self.client, self.store, self.lines.append)
        with self.assertRaisesRegex(catalog.CatalogUnavailable, "invented failure"):
            cache.models()

    def test_timeout_retains_single_flight_and_waits_before_retry(self):
        import queue
        from unittest.mock import Mock, patch
        with patch("model_gateway.clock.monotonic", return_value=100) as timer:
            cache = catalog.Catalog(self.knobs, self.client, self.store, self.lines.append)
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
