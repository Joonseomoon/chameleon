"""Dev 3 tests: validation, selection, repair, fallback and resize() orchestration (plan tasks T3.2, T3.4, T3.5).
Run: .venv/bin/python -m unittest discover -s tests -p test_resize.py -v"""
import copy
import json
import math
import unittest
from pathlib import Path
from unittest.mock import patch

import engine.resize as r
from engine.main import self_check

ROOT = Path(__file__).resolve().parents[1]
GRID = [(160, 600), (300, 250), (320, 50), (728, 90), (970, 90), (1080, 1350), (1080, 1920), (1200, 628),
        (1280, 720), (1500, 500), (1600, 400), (2480, 3508), (1080, 1080)]


def sample(level):
    return json.loads(next((ROOT / 'input').glob(f'*level{level}_*.json')).read_text())


def hard(source, out):
    an = r.analyze_source(r._sanitize(source))
    w, h = out['canvas']['width'], out['canvas']['height']
    return r._hard_checks(an, r.FittedCandidate(None, {e['id']: e for e in out['elements']}, {}, {}), w, h)


def quiet(*args):
    with patch.object(r, '_log'):
        return r.resize(*args)


class ResizeTests(unittest.TestCase):
    def test_every_sample_and_size_passes_the_hard_checks(self):
        for level in (1, 2, 3):
            t = sample(level)
            for w, h in GRID:
                before = json.dumps(t['source'], sort_keys=True)
                out = quiet(t['source'], {'width': w, 'height': h})
                self.assertEqual(json.dumps(t['source'], sort_keys=True), before)
                self.assertEqual(out['canvas'], {'width': w, 'height': h})
                self.assertEqual([e['id'] for e in out['elements']], [e['id'] for e in t['source']['elements']])
                self.assertEqual(hard(t['source'], out), [], (level, w, h))
                self.assertEqual(self_check(t['source'], out), [], (level, w, h))
                json.dumps(out, allow_nan=False)

    def test_deterministic(self):
        t = sample(3)
        self.assertEqual(json.dumps(quiet(t['source'], t['target_canvas'])), json.dumps(quiet(t['source'], t['target_canvas'])))

    def test_selection_on_the_samples(self):
        """Pins the current choices so weight changes are deliberate: L1 keeps its composition, L2 reflows into lanes."""
        logs = []
        with patch.object(r, '_log', logs.append):
            for level in (1, 2):
                t = sample(level)
                r.resize(t['source'], t['target_canvas'])
        self.assertIn('selected anchor', logs[0])
        self.assertIn('selected lanes:3:0|1|2,3', logs[1])

    def test_never_raises(self):
        src = sample(2)['source']
        weird = copy.deepcopy(src)
        weird['elements'][3]['width'] = float('nan')
        weird['elements'][4]['font_size'] = 'big'
        cases = [(weird, {'width': 728, 'height': 90}),
                 ({'canvas': {'width': 100, 'height': 100}, 'elements': []}, {'width': 300, 'height': 250}),
                 (src, {'width': 1, 'height': 1}),
                 (src, {'width': 728.4, 'height': 90.2})]
        for s, t in cases:
            out = quiet(s, t)
            self.assertEqual(len(out['elements']), len(s['elements']))
            self.assertTrue(all(math.isfinite(e[k]) for e in out['elements'] for k in ('x', 'y', 'width', 'height')))

    def test_internal_crash_falls_back_with_a_log_line(self):
        t = sample(2)
        logs = []
        with patch.object(r, 'generate_candidates', side_effect=RuntimeError('boom')), patch.object(r, '_log', logs.append):
            out = r.resize(t['source'], t['target_canvas'])
        self.assertTrue(any('FALLBACK' in m and 'boom' in m for m in logs))
        self.assertEqual(hard(t['source'], out), [])

    def test_hard_checks_catch_overlap_and_stretch(self):
        t = sample(2)
        an = r.analyze_source(t['source'])
        cand = r.generate_candidates(an, t['target_canvas'])[0]
        fitted = r.fit_candidate(an, cand, t['target_canvas'])
        self.assertEqual(r.validate(an, fitted, t['target_canvas']).hard, [])
        bad = copy.deepcopy(fitted)
        bad.elements['subhead'].update(x=bad.elements['headline']['x'], y=bad.elements['headline']['y'])
        self.assertTrue(any(h.startswith('H8') for h in r.validate(an, bad, t['target_canvas']).hard))
        bad = copy.deepcopy(fitted)
        bad.elements['product']['width'] *= 1.5
        self.assertTrue(any(h.startswith('H5') for h in r.validate(an, bad, t['target_canvas']).hard))

    def test_select_prefers_valid_then_score_then_order(self):
        ok_low, ok_high, broken = r.Report([], {}, 0.5), r.Report([], {}, 0.8), r.Report(['H8 x'], {}, 0.99)
        self.assertIs(r._select([('a', ok_low), ('b', broken), ('c', ok_high)])[0], 'c')
        self.assertIs(r._select([('a', ok_high), ('b', ok_high)])[0], 'a')
        self.assertIsNone(r._select([('b', broken)]))


if __name__ == '__main__':
    unittest.main()
