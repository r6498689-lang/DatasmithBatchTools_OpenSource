"""Real Qt widgets with a small UE task bridge; no user assets are loaded."""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Content' / 'Python'))
fake = types.ModuleType('unreal')
callbacks = {}
fake.log = fake.log_error = lambda *args: None
def register(callback):
    callbacks[1] = callback
    return 1
fake.register_slate_post_tick_callback = register
fake.unregister_slate_post_tick_callback = lambda handle: callbacks.pop(handle, None)
sys.modules['unreal'] = fake
from PySide6.QtWidgets import QApplication, QMessageBox
from datasmith_batch_tools import material_instance_ui as ui, material_instance_processor as p

app = QApplication.instance() or QApplication([])


class Host:
    def __init__(self):
        self._scan_active = self._scan_cancel_requested = self._closing = False
        self.saves = 0
    def _save_state(self): self.saves += 1
    def _begin_scan(self, title): self._scan_active = True; self._scan_cancel_requested = False
    def _end_scan(self): self._scan_active = False
    def _scan_progress(self, *args): return not self._scan_cancel_requested


class Tests(unittest.TestCase):
    def setUp(self):
        callbacks.clear()
        self.host = Host()
        self.page = ui.MaterialInstancePage(self.host)
        self.temp = tempfile.TemporaryDirectory()
        self.storage_patch = patch.object(p.storage, 'candidate_directories', return_value=[self.temp.name])
        self.storage_patch.start()
        self.questions = patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes)
        self.questions.start()
        self.warnings = patch.object(QMessageBox, 'warning', return_value=None)
        self.warnings.start()

    def tearDown(self):
        self.page.stop(cancelled=True)
        self.page.deleteLater()
        app.processEvents()
        self.storage_patch.stop()
        self.questions.stop()
        self.warnings.stop()
        self.temp.cleanup()

    def prepare(self):
        self.page.scanned_config = p.options()
        self.page.plans = [p.InstancePlan('001', 'Wood', f'/Game/Base_{i}', f'/Game/Out_{i}') for i in range(3)]
        self.page._populate()

    def start_creation(self, action):
        self.prepare()
        with patch.object(p, 'validate_parent', return_value=object()), patch.object(p, 'create_one', side_effect=action):
            self.page.create()
            return self.page._tick()

    def test_change_settings_invalidates_preview(self):
        self.prepare()
        self.page.create_button.setEnabled(True)
        self.page.source.setText('/Game/NewTextures')
        self.assertIsNone(self.page.scanned_config)
        self.assertFalse(self.page.create_button.isEnabled())

    def test_read_virtual_folder_from_asset_view(self):
        library = types.SimpleNamespace(get_selected_folder_paths=lambda: ['/All/Game/TestInput'])
        with patch.object(fake, 'EditorUtilityLibrary', library, create=True):
            self.page._pick_folder(self.page.source)
        self.assertEqual(self.page.source.text(), '/Game/TestInput')
        self.assertEqual(self.host.saves, 1)

    def test_read_virtual_folder_from_path_tree(self):
        library = types.SimpleNamespace(get_selected_folder_paths=lambda: [], get_selected_path_view_folder_paths=lambda: ['/All/Game/Materials/GeneratedInstances'])
        self.page.output.setText('/Game/Other')
        with patch.object(fake, 'EditorUtilityLibrary', library, create=True):
            self.page._pick_folder(self.page.output)
        self.assertEqual(self.page.output.text(), p.DEFAULT_OUTPUT)

    def test_invalid_selected_folder_leaves_input_unchanged(self):
        original = self.page.source.text()
        library = types.SimpleNamespace(get_selected_folder_paths=lambda: ['/All/Engine/Textures'])
        with patch.object(fake, 'EditorUtilityLibrary', library, create=True):
            self.page._pick_folder(self.page.source)
        self.assertEqual(self.page.source.text(), original)

    def test_restore_does_not_save_or_scan(self):
        self.page.restore_state(dict(source='/Game/Test', metal=0.4))
        self.assertEqual(self.page.source.text(), '/Game/Test')
        self.assertAlmostEqual(self.page.metal_default.value(), 0.4)
        self.assertEqual(self.host.saves, 0)

    def test_target_column_shows_both_category_levels(self):
        config=p.options()
        path='/Game/Textures/Asphalt/107_Asphalt_UV_Unknown/BaseColor_107_Bridge_Asphalt_01_001'
        self.page.plans=p.build_plans([path],config)[0]
        self.page.scanned_config=config
        self.page._populate()
        cell=self.page.table.item(0,6)
        self.assertEqual(cell.text(),'Asphalt/107_Asphalt/107_Bridge_Asphalt_01_001')
        self.assertEqual(cell.toolTip(),self.page.plans[0].target)

    def test_task_mutex(self):
        self.host._scan_active = True
        with patch.object(p, 'scan_textures') as scan:
            self.page.scan()
            scan.assert_not_called()
        self.assertIsNone(self.page.job)

    def test_cancel_after_one_keeps_success_and_cleans_callback(self):
        def action(plan, *args):
            plan.status = 'created'
            self.host._scan_cancel_requested = True
        self.start_creation(action)
        self.assertEqual([x.status for x in self.page.plans], ['created', 'cancelled', 'cancelled'])
        self.assertFalse(self.host._scan_active)
        self.assertFalse(callbacks)
        self.assertIsNone(self.page.job)

    def test_close_during_tick_defers_cleanup_until_item_finishes(self):
        def action(plan, *args):
            self.host._closing = True
            self.page.stop(cancelled=True)
            self.assertIsNotNone(self.page.job)
            plan.status = 'created'
        self.start_creation(action)
        self.assertEqual(self.page.plans[0].status, 'created')
        self.assertIsNone(self.page.job)
        self.assertFalse(callbacks)

    def test_callback_registration_failure_releases_lock(self):
        with patch.object(fake, 'register_slate_post_tick_callback', side_effect=RuntimeError('registration failed')):
            self.page.scan()
        self.assertFalse(self.host._scan_active)
        self.assertIsNone(self.page.job)
        self.assertFalse(self.page.create_button.isEnabled())

    def test_cancelled_scan_cannot_create_from_partial_result(self):
        self.page._start(dict(kind='scan', iterator=(x for x in [])), 'scan')
        self.host._scan_cancel_requested = True
        self.page._tick()
        self.assertIsNone(self.page.scanned_config)
        self.assertFalse(self.page.create_button.isEnabled())

    def test_completed_scan_enables_creation(self):
        self.prepare()
        plans = self.page.plans
        item = dict(done=3, total=3, plans=plans, issues=[], summary=dict(textures=3, groups=1))
        iterator = (x for x in [item])
        with patch.object(p, 'scan_textures', return_value=iterator):
            self.page.scan()
        self.page._tick()
        self.assertTrue(self.page.create_button.isEnabled())
        self.assertEqual(self.page.table.rowCount(), 3)
        self.assertFalse(callbacks)


if __name__ == '__main__':
    unittest.main()
