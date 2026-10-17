"""Dev 2 fitting tests. Run with: .venv/bin/python -m unittest discover -s tests -p test_fitting.py"""
import copy
import json
import math
import unittest
from pathlib import Path
from unittest.mock import patch

import engine.resize as r

ROOT = Path(__file__).resolve().parents[1]


def sample(level):
    return json.loads(next((ROOT / 'input').glob(f'*level{level}_*.json')).read_text())


def text(**changes):
    el = dict(id='t', type='text', x=0, y=0, width=400, height=100,
              content='Chase the Light', font_family='Solstice Serif', font_size=60,
              font_weight='bold', color='rgba(255,255,255,1)')
    el.update(changes)
    return el


class FittingTests(unittest.TestCase):
    def test_text_cap_and_impossible_word(self):
        el = text()
        fit = r._fit_text(el, r.Box(0, 0, 400, 100), 60)
        self.assertTrue(fit.fits)
        self.assertLess(fit.font_size, 60)
        self.assertLessEqual(fit.text_w, 400)
        self.assertLessEqual(fit.text_h, 100)
        self.assertEqual(r._fit_text(el, r.Box(0, 0, 800, 200), 60).font_size, 60)
        self.assertFalse(r._fit_text(text(content='W'*100), r.Box(0, 0, 1, 1), 60).fits)

    def test_wrap_matches_converter_and_mixed_styles(self):
        from renderer.converter.convert import _append_text
        from collections import defaultdict
        el = text(content='Hello bold world\nNext line', letter_spacing=2,
                  style_runs=[dict(start=6, end=9, font_weight='regular')])
        converted = defaultdict(list)
        _append_text(converted, el, r._font_manager())
        lines, width, height = r._measure_text(el, 60, 400)
        self.assertEqual(lines, converted['text'][0].count('\n')+1)
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)
        self.assertLessEqual(r._fit_text(dict(el, max_lines=1), r.Box(0, 0, 400, 100), 60).fits, False)

    def test_letter_spacing_and_fractional_renderer_size(self):
        el = text(content='WWW')
        a = r._measure_text(el, 30.1, 1000)
        b = r._measure_text(el, 30.9, 1000)
        self.assertEqual(a, b)  # one line: drawing truncates size to integer
        spaced = r._measure_text(dict(el, letter_spacing=4), 30.1, 1000)
        self.assertAlmostEqual(spaced[1] - a[1], 8)
        colored = dict(el, letter_spacing=4, style_runs=[dict(start=1, end=1, color='red')])
        self.assertAlmostEqual(r._measure_text(colored, 30.1, 1000)[1], a[1])

    def test_pillow_fallback(self):
        with patch.object(r, '_font_manager', return_value=None):
            result = r._measure_text(text(), 24, 200)
            self.assertTrue(all(math.isfinite(v) for v in result))
            self.assertGreater(result[1], 0)
            self.assertTrue(r._fit_text(text(), r.Box(0, 0, 200, 100), 24).fits)

    def test_image_alignment(self):
        slot = r.Box(100, 50, 300, 200)
        box = r._fit_image(dict(width=400, height=600), slot, ('right', 'bottom'))
        self.assertAlmostEqual(box.w / box.h, 2/3)
        self.assertAlmostEqual(box.right, slot.right)
        self.assertAlmostEqual(box.bottom, slot.bottom)

    def test_rotated_group_stays_in_slot(self):
        t = sample(3)
        an = r.analyze_source(t['source'])
        u = next(u for u in an.units.values() if u.rotation)
        slot = r.Box(20, 10, 250, 70)
        els = r._fit_unit(an, u, slot, ('center', 'middle'), 1)
        for i, el in els.items():
            self.assertTrue(slot.contains(r._rotated_aabb(el), .001))
            self.assertEqual(el.get('rotation'), an.elements[i].get('rotation'))
            if el['type'] == 'text':
                self.assertTrue(r._text_report(el, r._element_box(el), el['font_size']).fits)

    def test_samples_preserve_content_bounds_and_reports(self):
        for level in (1, 2, 3):
            t = sample(level)
            original = copy.deepcopy(t['source'])
            an = r.analyze_source(t['source'])
            canvas = r.Box(0, 0, t['target_canvas']['width'], t['target_canvas']['height'])
            candidates = r.generate_candidates(an, t['target_canvas'])
            for cand in candidates:
                fit = r.fit_candidate(an, cand, t['target_canvas'])
                self.assertEqual(list(fit.elements), an.order)
                self.assertFalse(any(n.startswith('FIT ERROR') for n in fit.notes), fit.notes)
                self.assertEqual(set(fit.used), set(an.units))
                for i, el in fit.elements.items():
                    self.assertTrue(canvas.contains(r._rotated_aabb(el), .5), (level, cand.name, i))
                    for key, value in an.elements[i].items():
                        if key not in ('x', 'y', 'width', 'height', 'font_size'):
                            self.assertEqual(el[key], value)
                    if el['type'] == 'text':
                        self.assertEqual(fit.text[i].font_size, el['font_size'])
                        if not fit.text[i].fits:
                            self.assertIn('Text overflow: '+i, fit.notes)
                texts = [i for i in an.order if an.elements[i]['type'] == 'text']
                for i in texts:
                    for j in texts:
                        if an.elements[i]['font_size'] > an.elements[j]['font_size']:
                            self.assertGreaterEqual(fit.elements[i]['font_size']+1e-6, fit.elements[j]['font_size'])
                if level == 3:
                    self.assertEqual(len({fit.elements[f'badge{i}_label']['font_size'] for i in (1, 2, 3)}), 1)
            self.assertEqual(t['source'], original)

    def test_malformed_candidate_reports_failure_without_dropping_elements(self):
        t = sample(2)
        an = r.analyze_source(t['source'])
        cand = r.Candidate('broken', {}, {}, {})
        result = r.fit_candidate(an, cand, t['target_canvas'])
        self.assertEqual(set(result.elements), set(an.order))
        self.assertTrue(any(n.startswith('FIT ERROR') for n in result.notes))

    def test_group_label_remains_inside_shape(self):
        t = sample(2)
        an = r.analyze_source(t['source'])
        u = next(u for u in an.units.values() if 'badge_label' in u.member_ids)
        els = r._fit_unit(an, u, r.Box(100, 50, 300, 100), ('center', 'middle'), 1)
        self.assertTrue(r._element_box(els['badge_bg']).contains(r._element_box(els['badge_label'])))


if __name__ == '__main__':
    unittest.main()
