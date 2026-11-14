"""レンダリング・祝日・保存処理の回帰テスト（実機GUIを除く）。"""
import datetime as dt
import tempfile
import unicodedata
import unittest
from unittest import mock
from pathlib import Path

import fitz
import store_calendar as mod


class CalendarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.renderer = mod.CalendarRenderer()

    @classmethod
    def tearDownClass(cls):
        cls.renderer.close()

    def test_six_cards_landscape_a4(self):
        with self.renderer.make_document(2026, 11, [], mod.DEFAULT_SETTINGS) as doc:
            self.assertEqual(len(doc), 1)
            self.assertAlmostEqual(doc[0].rect.width, mod.PAGE_W)
            self.assertAlmostEqual(doc[0].rect.height, mod.PAGE_H)
            self.assertEqual(len(doc[0].search_for('お休みのお知らせ')), 6)
            self.assertEqual(len(mod.crop_mark_segments()), 6)

    def test_holiday_names_appear_in_each_card(self):
        with self.renderer.make_document(2026, 11, [], mod.DEFAULT_SETTINGS) as doc:
            self.assertEqual(len(doc[0].search_for('文化の日')), 6)
            self.assertEqual(len(doc[0].search_for('勤労感謝の日')), 6)

    def test_one_digit_month(self):
        with self.renderer.make_document(2027, 2, [], mod.DEFAULT_SETTINGS) as doc:
            self.assertEqual(len(doc[0].search_for('お休みのお知らせ')), 6)
            self.assertEqual(unicodedata.normalize('NFKC', doc[0].get_text()).count('建国記念の日'), 6)

    def test_single_card_is_one_sixth(self):
        with self.renderer.make_document(2026, 11, [], mod.DEFAULT_SETTINGS,
                                         single_card=True) as doc:
            self.assertAlmostEqual(doc[0].rect.width, mod.CELL_W, places=3)
            self.assertAlmostEqual(doc[0].rect.height, mod.CELL_H, places=3)

    def test_position_adjustment_and_crop_marks(self):
        """Column/row offsets move cards AND the six cut marks by their midpoints."""
        shift = dict(mod.DEFAULT_SETTINGS)
        shift['position_mm'] = {'columns': [3.0, 1.0, -2.0],
                                'rows': [1.0, -1.0]}
        pt = mod.PT_PER_MM
        x_orig, y_orig = mod.card_origin(0, 0)
        x_new, y_new = mod.card_origin(0, 0, shift)
        self.assertAlmostEqual(x_new-x_orig, 3.0*pt, places=4)
        self.assertAlmostEqual(y_new-y_orig, 1.0*pt, places=4)
        self.assertAlmostEqual(mod.card_origin(2, 1, shift)[0] -
                               mod.card_origin(2, 1)[0], -2*pt, places=4)
        marks = mod.crop_mark_segments(shift)
        self.assertEqual(len(marks), 6)
        self.assertAlmostEqual(marks[0][0][0], mod.CELL_W + 2*pt, places=4)
        self.assertAlmostEqual(marks[2][0][0], 2*mod.CELL_W - .5*pt, places=4)
        # The two shifted row centers still straddle the middle at exactly 105mm.
        self.assertAlmostEqual(marks[-1][0][1], mod.CELL_H, places=4)
        with self.renderer.make_document(2026, 11, [], shift) as doc:
            self.assertEqual(len(doc[0].search_for('お休みのお知らせ')), 6)

    def test_holiday_number_baseline_equal_to_other_dates(self):
        """Holiday labels are below the date; dates have identical text baselines."""
        with self.renderer.make_document(2026, 11, [], mod.DEFAULT_SETTINGS,
                                         single_card=True) as doc:
            positions = {}
            for block in doc[0].get_text('dict')['blocks']:
                for line in block.get('lines', []):
                    for span in line.get('spans', []):
                        if span['text'] in ('2', '3', '4'):
                            positions[span['text']] = span['origin'][1]
            self.assertEqual(set(positions), {'2', '3', '4'})
            self.assertAlmostEqual(positions['2'], positions['3'], places=2)
            self.assertAlmostEqual(positions['3'], positions['4'], places=2)

    def test_position_limits_and_bad_values(self):
        values = {'position_mm': {'columns': [100, 'x', -100],
                                  'rows': [float('inf'), -100]}}
        self.assertEqual(mod.position_values(values),
                         ([6.0, 0.0, -6.0], [0.0, -3.0]))

    def test_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'closed_days.json'
            data={'2026-11':[2,3,4], '2027-02':[8,20]}
            mod.save_json(f, data)
            self.assertEqual(mod.load_json(f, {}), data)

    def test_holiday_csv_parser(self):
        sample='日付,名称\n2026/11/3,文化の日\n2026/11/23,勤労感謝の日\n'
        found = mod.parse_holiday_csv(sample.encode('cp932'), min_records=2)
        self.assertEqual(found['2026-11-03'], '文化の日')

    def test_color_and_opacity(self):
        settings=dict(mod.DEFAULT_SETTINGS)
        rgb=mod.color_for_logo(settings)
        self.assertEqual(len(rgb), 3)
        self.assertTrue(all(0<=c<=1 for c in rgb))
        settings['closure_opacity']=150
        self.assertEqual(mod.opacity(settings,'closure_opacity',38),1)


    def test_v111_swatch_matches_label_height(self):
        # The legend mark must be roughly as tall as the 8.4 pt caption.
        with self.renderer.make_document(2026, 11, [], mod.DEFAULT_SETTINGS,
                                         single_card=True) as doc:
            target = [d for d in doc[0].get_drawings()
                      if d.get('fill') is not None
                      and max(abs(a-b) for a,b in zip(d['fill'], mod.PINK)) < 1e-5
                      and abs(d.get('width', 0) - 0.65) < 1e-4]
            self.assertTrue(target, '休業日凡例の色付き枠が見つかりません')
            self.assertAlmostEqual(target[-1]['rect'].height, 7.4, places=2)

    def test_cut_guides_disabled(self):
        settings = {**mod.DEFAULT_SETTINGS, 'cut_guides': False}
        with self.renderer.make_document(2026, 11, [], settings) as plain:
            with self.renderer.make_document(2026, 11, [], mod.DEFAULT_SETTINGS) as cut:
                # 6 extra short marks appear only when ON.
                self.assertEqual(len(cut[0].get_drawings()) -
                                 len(plain[0].get_drawings()), 6)

    def test_data_directory_copy_no_overwrite(self):
        """Copy on change, persist preference, reload at startup, reject collision."""
        original = (mod.DATA, mod.DATA_IS_FALLBACK, mod.DATA_DIR_WARNING)
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            old = root / 'old'
            old.mkdir()
            mod.save_json(old / 'closed_days.json', {'2028-01': [1, 13, 25]})
            mod.save_json(old / 'settings.json', {'notice': 'first'})
            (old / 'syukujitsu.csv').write_text('cached', encoding='utf-8')
            preference = root / 'localappdata' / 'installation.json'
            try:
                with mock.patch.object(mod, 'DATA_LOCATION_FILE', preference):
                    mod._activate_data_dir(old)
                    dest = root / 'new'
                    mod.switch_data_directory(dest, {'notice': 'updated'})
                    self.assertEqual(mod.DATA, dest)
                    self.assertEqual(mod.load_json(mod.CLOSURES_FILE, {}),
                                     {'2028-01': [1, 13, 25]})
                    self.assertEqual(mod.load_json(old / 'settings.json', {})['notice'], 'first')
                    self.assertEqual(mod.load_json(dest / 'settings.json', {})['notice'], 'updated')
                    self.assertEqual((dest / 'syukujitsu.csv').read_text(), 'cached')
                    self.assertEqual(mod.choose_data_dir()[0], dest)
                    self.assertTrue(preference.is_file())
                    other = root / 'other'
                    other.mkdir()
                    mod.save_json(other / 'closed_days.json', {'2028-01': []})
                    with self.assertRaises(FileExistsError):
                        mod.switch_data_directory(other, {'notice': 'new'})
                    self.assertEqual(mod.DATA, dest)
                    self.assertEqual(mod.load_json(other / 'closed_days.json', {}),
                                     {'2028-01': []})
            finally:
                mod._activate_data_dir(original[0])
                mod.DATA_IS_FALLBACK = original[1]
                mod.DATA_DIR_WARNING = original[2]

    def test_data_directory_use_existing(self):
        original = (mod.DATA, mod.DATA_IS_FALLBACK, mod.DATA_DIR_WARNING)
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            a = root / 'a'
            b = root / 'b'
            a.mkdir()
            b.mkdir()
            mod.save_json(a / 'settings.json', {'notice': 'A'})
            mod.save_json(a / 'closed_days.json', {'2026-11': [2]})
            mod.save_json(b / 'settings.json', {'notice': 'B'})
            mod.save_json(b / 'closed_days.json', {'2026-11': [3]})
            try:
                with mock.patch.object(mod, 'DATA_LOCATION_FILE', root / 'setting.json'):
                    mod._activate_data_dir(a)
                    mod.switch_data_directory(b, use_existing=True)
                    self.assertEqual(mod.load_json(mod.SETTINGS_FILE, {})['notice'], 'B')
                    self.assertEqual(mod.load_json(mod.CLOSURES_FILE, {}), {'2026-11': [3]})
                    self.assertEqual(mod.load_json(a / 'closed_days.json', {}), {'2026-11': [2]})
            finally:
                mod._activate_data_dir(original[0])
                mod.DATA_IS_FALLBACK = original[1]
                mod.DATA_DIR_WARNING = original[2]


if __name__ == '__main__':
    unittest.main()
