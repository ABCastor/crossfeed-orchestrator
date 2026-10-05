"""Check web motion bindings and sample the independent README loop at 100 ms."""
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / 'scripts/console-assets/console.css').read_text()
LOOP = (ROOT / 'docs/readme-header.svg').read_text()


def frames(name, property=None, css=LOOP):
    body = re.search(r'@keyframes ' + name + r'\{(.*?)\n\}', css, re.S).group(1)
    result = {}
    pattern = r'opacity:([\d.]+)' if property == 'opacity' else r'(?:rotate\(|translateY\(|stroke-dashoffset:)(-?[\d.]+)'
    for selectors, declaration in re.findall(r'([^{}]+)\{([^{}]+)\}', body):
        match = re.search(pattern, declaration)
        if not match:
            continue
        value = float(match.group(1))
        for percent in re.findall(r'([\d.]+)%', selectors):
            result[float(percent)] = value
    return sorted(result.items())


def value_at(name, percent, property=None, css=LOOP):
    points = frames(name, property, css)
    for (start, a), (end, b) in zip(points, points[1:]):
        if start <= percent <= end:
            return a + (b - a) * (percent - start) / (end - start)
    raise ValueError(percent)


class MarkSequenceTests(unittest.TestCase):
    def test_svg_geometry_equalises_then_resets_independently_every_100ms(self):
        svg = ET.fromstring((ROOT / 'scripts/console-assets/crossfeed.svg').read_text())
        groups = {node.get('class'): node for node in svg.iter() if node.get('class') in ('cf-l', 'cf-r')}
        # The SVG's real level paths, rather than assuming equal displacements mean equal levels.
        base = {side: float(re.search(r'M[\d.]+ (-?[\d.]+)', list(group)[1].get('d')).group(1))
                for side, group in groups.items()}
        previous = None
        transfer = reset = 0
        for step in range(81):
            percent = step * 1.25
            left = base['cf-l'] + value_at('cf-left', percent)
            right = base['cf-r'] + value_at('cf-right', percent)
            valve = value_at('cf-valve', percent)
            pipe = value_at('cf-pipe', percent)
            visible = value_at('cf-pipe', percent, 'opacity') > .01
            if previous and abs(right - previous[1]) > 1e-8:
                if right < previous[1]:
                    self.assertEqual(valve, 90, step)
                    self.assertEqual(pipe, 0, step)
                    self.assertTrue(visible, step)
                    self.assertLessEqual(left, right + 1e-8, step)
                    transfer += 1
                else:
                    self.assertEqual(valve, 0, step)
                    self.assertFalse(visible, step)
                    self.assertLess(left, previous[0], step)  # left refills as right is used
                    reset += 1
            if 40 <= percent <= 55:
                self.assertAlmostEqual(left, right, msg=f'levels must stop equal at {step/10}s')
            previous = left, right
        self.assertGreater(transfer, 0)
        self.assertGreater(reset, 0)
        self.assertEqual(value_at('cf-left', 0), value_at('cf-left', 100))
        self.assertEqual(value_at('cf-right', 0), value_at('cf-right', 100))

    def test_order_no_return_travel_and_pause(self):
        self.assertEqual(value_at('cf-valve', 10), 90)
        self.assertEqual(value_at('cf-pipe', 10), 1)
        self.assertGreater(value_at('cf-pipe', 15), 0)
        self.assertEqual(value_at('cf-right', 15), 0)
        self.assertEqual(value_at('cf-pipe', 20), 0)
        self.assertEqual(value_at('cf-right', 20), 0)
        self.assertLess(value_at('cf-right', 21.25), 0)
        self.assertEqual(value_at('cf-valve', 45), 0)
        self.assertEqual(value_at('cf-pipe', 45, 'opacity'), 0)
        self.assertEqual(value_at('cf-right', 55), -7)
        self.assertGreater(value_at('cf-right', 56.25), -7)
        self.assertEqual(value_at('cf-right', 80), 0)
        for step in range(81):
            percent = step * 1.25
            self.assertGreaterEqual(value_at('cf-pipe', percent), 0)
            if step and value_at('cf-pipe', percent, 'opacity') > .01:
                self.assertLessEqual(value_at('cf-pipe', percent), value_at('cf-pipe', percent-1.25))
        self.assertNotIn('@keyframes cf-', CSS)
        self.assertNotIn('linear infinite paused', CSS)
        for name in ('valve', 'pipe', 'left', 'right', 'flow'):
            self.assertIn(f'var(--cf-{name},', CSS)
        self.assertIn('.product-mark[data-mark-active]', CSS)
        self.assertNotIn('.product-mark:is(:hover,:focus-visible)', CSS)
        self.assertIn('@media(prefers-reduced-motion:reduce)', CSS)

    def test_readme_loops_independently_in_plain_self_themed_svg(self):
        header = (ROOT / 'docs/readme-header.svg').read_text()
        ET.fromstring(header)
        for name in ('cf-valve', 'cf-pipe', 'cf-left', 'cf-right'):
            self.assertEqual(frames(name)[0][0], 0)
            self.assertEqual(frames(name)[-1][0], 100)
            self.assertIn(f'animation:{name} 8s linear infinite', header)
            self.assertNotIn(f'animation:{name} 8s linear infinite paused', header)
        self.assertEqual(value_at('cf-pipe', 0, 'opacity'), 1)
        self.assertEqual(value_at('cf-pipe', 100, 'opacity'), 0)
        self.assertIn('prefers-reduced-motion:reduce', header)
        self.assertIn('prefers-color-scheme:dark', header)
        readme = (ROOT / 'README.md').read_text()
        self.assertIn('<img src="docs/readme-header.svg"', readme)
        self.assertRegex(readme, r'<a href="https://abcastor.com"><img src="docs/castor-footer.svg"')
        self.assertNotIn('<picture>', readme)


if __name__ == '__main__':
    unittest.main()
