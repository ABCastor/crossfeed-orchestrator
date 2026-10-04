"""Correction and unit oracles independently derived from public documents."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import random
import re
import tempfile
import unittest

from bench.check import check_task
from bench.families import extraction


class ExtractionFamilyTests(unittest.TestCase):
    def test_references_nulls_schema_and_seeded_inputs(self):
        count = 0
        primitives = {str: 'string', int: 'integer', bool: 'boolean', type(None): 'null'}
        with tempfile.TemporaryDirectory(prefix='extraction-family-') as temporary:
            root = Path(temporary)
            empty = root / 'empty.txt'
            empty.write_text(' \n\t', encoding='utf-8')
            for seed in (1, 2):
                for template in range(4):
                    for tier in extraction.TIERS:
                        with self.subTest(seed=seed, template=template, tier=tier):
                            source_seed = '%s:%s:%s' % (seed, template, tier)
                            fixture = extraction.make(random.Random(source_seed), template, tier)
                            self.assertEqual(fixture, extraction.make(random.Random(source_seed), template, tier))
                            prompt, workspace, check, reference, reply = fixture
                            self.assertEqual(workspace, reference)
                            task = root / ('%s-%s-%s' % (seed, template, tier))
                            (task / 'workspace').mkdir(parents=True)
                            (task / 'workspace' / 'document.txt').write_text(workspace['document.txt'], encoding='utf-8')
                            (task / 'check.json').write_text(json.dumps(check), encoding='utf-8')
                            reply_path = task / 'reply.txt'
                            reply_path.write_text(reply, encoding='utf-8')
                            self.assertTrue(check_task(task, task / 'workspace', reply_path)['pass'])
                            self.assertFalse(check_task(task, task / 'workspace', empty)['pass'])
                            schema, unused = json.JSONDecoder().raw_decode(prompt.split('JSON Schema: ', 1)[1])
                            self.assertEqual(schema['type'], 'object')
                            self.assertIs(schema['additionalProperties'], True)
                            self.assertEqual(set(schema['required']), set(check['expected']))
                            self.assertEqual(len(schema['required']), len(check['expected']))
                            self.assertEqual(set(schema['properties']), set(check['expected']))
                            for field, value in check['expected'].items():
                                declaration = schema['properties'][field]
                                if type(value) is list:
                                    self.assertEqual(declaration, {'type': 'array', 'items': {'type': 'string'}})
                                    self.assertTrue(all(type(item) is str for item in value))
                                else:
                                    self.assertEqual(declaration['type'], primitives[type(value)])
                            different = extraction.make(random.Random('different:' + source_seed), template, tier)
                            self.assertNotEqual(workspace, different[1])
                            count += 1
        self.assertEqual(count, 32)

    def expert(self, template):
        fixture = extraction.make(random.Random(17), template, 'expert')
        return fixture[1]['document.txt'], fixture[2]['expected']

    def test_order_reset_and_field_scoped_void_golden(self):
        document, expected = self.expert(0)
        self.assertIn('R4: reset quantity to 2 cases', document)
        self.assertIn('each case contains 5 cartridges', document)
        self.assertIn('R5: short-pick 1 cartridges', document)
        self.assertIn('VOID the R3 price correction only', document)
        self.assertIn('R7: replace restored price with USD 42.78', document)
        self.assertEqual(expected['quantity'], 2 * 5 - 1)
        self.assertEqual(expected['unit_price_cents'], 4278)
        self.assertEqual(expected['tags'], ['priority', 'reusable'])
        self.assertIs(expected['expedited'], True)
        self.assertIsNone(expected['shipping'])

    def test_manifest_latest_signed_revision_and_half_up_oracle(self):
        document, expected = self.expert(1)
        pattern = re.compile(r'^(?:Manifest|SIGNED inspection) r(\d+) shipment=(\S+) package=(\S+) (.*)$')
        records = {}
        for line in document.splitlines():
            match = pattern.match(line)
            if not match or match[2] != expected['shipment_id']:
                continue
            revision, package, fields = int(match[1]), match[3], match[4]
            if package not in records or revision > records[package][0]:
                records[package] = (revision, fields)
        gross_total = net_total = surviving = 0
        codes = set()
        for revision, fields in records.values():
            if fields.startswith('VOID'):
                continue
            code = re.search(r'code=(\S+)', fields)[1]
            weight, unit = re.search(r'gross=([\d.]+) (kg|g)', fields).groups()
            tare = int(re.search(r'tare=(\d+) g', fields)[1])
            gross = int((Decimal(weight) * (1000 if unit == 'kg' else 1)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            gross_total += gross
            net_total += gross - tare
            surviving += 1
            codes.add(code)
        self.assertEqual((gross_total, net_total, surviving), (57490, 53376, 10))
        self.assertEqual((expected['gross_grams'], expected['net_grams'], expected['package_count']),
                         (gross_total, net_total, surviving))
        self.assertEqual(expected['item_codes'], sorted(codes))
        self.assertEqual(int((Decimal('3.5255') * 1000).quantize(Decimal('1'), rounding=ROUND_HALF_UP)), 3526)

    def test_experiment_calibration_and_volume_conversion_golden(self):
        document, expected = self.expert(2)
        self.assertIn('R6: replace concentration with RAW result 318 micrograms/mL', document)
        self.assertIn('calibration C4: gain=3/2, blank=14 mg/L', document)
        self.assertIn('withdraw 4.243 mL; instrument dead volume=51 microliters', document)
        self.assertIn('measured gross mass=3.638 g; tare=653 mg', document)
        concentration = Decimal(318) * Decimal('1.5') - Decimal(14)
        volume = int(Decimal('4.243') * 1000) - 51
        self.assertEqual(expected['mass_mg'], int(Decimal('3.638') * 1000) - 653)
        self.assertEqual(expected['concentration_ng_per_ml'], int(concentration * 1000))
        self.assertEqual(expected['volume_ul'], volume)
        self.assertEqual(expected['dose_ng'], int(concentration * volume))
        self.assertEqual(expected['dose_ng'], 1940896)

    def test_incident_revision_scope_offsets_and_interval_union_oracle(self):
        document, expected = self.expert(3)
        pattern = re.compile(r'^Commander accepted r(\d+) incident=(\S+) window=(\S+) (.*)$')
        records = {}
        for line in document.splitlines():
            match = pattern.match(line)
            if not match or match[2] != expected['incident_id']:
                continue
            revision, key, fields = int(match[1]), match[3], match[4]
            if key not in records or revision > records[key][0]:
                records[key] = (revision, fields)
        intervals = []
        for revision, fields in records.values():
            if fields.startswith('VOID') or 'kind=customer-impact' not in fields:
                continue
            start, end = re.search(r'start=(\S+) end=(\S+)', fields).groups()
            intervals.append((datetime.fromisoformat(start).astimezone(timezone.utc),
                              datetime.fromisoformat(end).astimezone(timezone.utc)))
        merged = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        duration = sum(int((end - start).total_seconds()) for start, end in merged)
        self.assertEqual(duration, 5001)
        self.assertEqual(expected['downtime_seconds'], duration)
        self.assertEqual(expected['started_utc'], merged[0][0].strftime('%Y-%m-%dT%H:%M:%SZ'))
        self.assertEqual(expected['resolved_utc'], merged[-1][1].strftime('%Y-%m-%dT%H:%M:%SZ'))
        self.assertGreater(int((merged[-1][1] - merged[0][0]).total_seconds()), duration)
        self.assertIsNone(expected['root_cause'])


if __name__ == '__main__':
    unittest.main()
