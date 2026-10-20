import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'Content/Python'))
fake=types.ModuleType('unreal')
fake.log=fake.log_error=lambda *args:None
callbacks={}
fake.register_slate_post_tick_callback=lambda cb: callbacks.setdefault(1,cb) and 1
fake.unregister_slate_post_tick_callback=lambda handle:callbacks.pop(handle,None)
fake.DatasmithImportOptions=lambda:object()
fake.EditorDialog=types.SimpleNamespace(show_object_details_view=lambda *args:True)
fake.DatasmithBatchImportLibrary=types.SimpleNamespace(show_options=lambda *args:True)
sys.modules['unreal']=fake
from PySide6.QtWidgets import QApplication,QMessageBox
from datasmith_batch_tools import datasmith_import_ui as ui
from test_datasmith_import_processor import Backend

app=QApplication.instance() or QApplication([])


class Host:
    def __init__(self):self._scan_active=self._scan_cancel_requested=self._closing=False
    def _begin_scan(self,*args):self._scan_active=True;self._scan_cancel_requested=False
    def _end_scan(self):self._scan_active=False
    def _scan_progress(self,*args):pass
    def _save_state(self):pass


class Tests(unittest.TestCase):
    def setUp(self):
        callbacks.clear();self.host=Host();self.backend=Backend()
        self.page=ui.DatasmithImportPage(self.host)
        self.temp=tempfile.TemporaryDirectory()
        self.patches=[patch.object(ui.processor,'UnrealBackend',return_value=self.backend),
                      patch.object(ui.processor.storage,'candidate_directories',return_value=[self.temp.name]),
                      patch.object(QMessageBox,'warning',return_value=None),
                      patch.object(ui.processor.time,'monotonic',side_effect=lambda:self.backend.time)]
        for p in self.patches:p.start()
        for n in ['A','B']:
            f=Path(self.temp.name)/(n+'.udatasmith');f.write_text('<DatasmithUnrealScene/>');self.page.files.append(str(f))
    def tearDown(self):
        self.page.stop(cancelled=True);self.page.close();self.page.deleteLater();app.processEvents()
        for p in self.patches:p.stop()
        self.temp.cleanup()
    def test_start_shows_options_once_and_imports_on_ticks(self):
        with patch.object(fake.DatasmithBatchImportLibrary,'show_options',return_value=True) as show:
            self.page.start();self.assertEqual(show.call_count,1)
        self.assertEqual(self.backend.events,[])
        for _ in range(12):
            self.backend.time+=5.1;self.page._tick()
        self.assertEqual(self.backend.events,[('import','A'),('compile',None),('save',1),('cleanup',None),('import','B'),('compile',None),('save',2),('cleanup',None)])
        self.assertFalse(self.host._scan_active);self.assertFalse(callbacks)
    def test_cancel_options_has_no_import_and_releases_lock(self):
        with patch.object(fake.DatasmithBatchImportLibrary,'show_options',return_value=False):self.page.start()
        self.assertEqual(self.backend.events,[]);self.assertFalse(callbacks);self.assertFalse(self.host._scan_active)
    def test_close_between_import_and_save_saves_then_cancels(self):
        self.page.start();self.page._tick();self.page.stop(cancelled=True)
        self.assertEqual(self.backend.events,[('import','A'),('compile',None),('save',1),('cleanup',None)])
        self.assertEqual(self.page.items[1].status,'cancelled');self.assertFalse(callbacks)
    def test_cancel_requested_inside_sync_import_still_saves(self):
        original=self.backend.import_file
        def action(*args):
            self.host._scan_cancel_requested=True
            return original(*args)
        self.backend.import_file=action
        self.page.start();self.page._tick()
        self.assertEqual(self.backend.events,[('import','A'),('compile',None),('save',1),('cleanup',None)])
        self.assertIsNone(self.page.session);self.assertFalse(callbacks)
    def test_registration_failure_releases_lock(self):
        with patch.object(fake,'register_slate_post_tick_callback',side_effect=RuntimeError('register failed')):self.page.start()
        self.assertFalse(self.host._scan_active);self.assertIsNone(self.page.session)
        self.assertEqual(self.backend.events,[])
    def test_other_task_blocks_start(self):
        self.host._scan_active=True;self.page.start()
        self.assertIsNone(self.page.session);self.assertEqual(self.backend.events,[])

    def test_mode_buttons_are_equal_exclusive_and_labeled(self):
        normal, large = self.page.mode_buttons['normal'], self.page.mode_buttons['large']
        self.assertEqual(normal.size(), large.size())
        self.assertTrue(normal.isChecked());self.assertFalse(large.isChecked())
        large.click()
        self.assertTrue(large.isChecked());self.assertFalse(normal.isChecked())
        self.assertEqual(self.page.mode_label.text(), '当前模式：大文件模式')
        self.assertEqual(self.page.save_state()['mode'], 'large')

    def test_mode_restored_and_locked_during_import(self):
        self.page.restore_state({'mode':'large'})
        self.assertEqual(self.page.mode, 'large')
        self.page.start()
        self.page.set_mode('normal')
        self.assertEqual(self.page.mode, 'large')
        self.assertEqual(self.page.session.mode, 'large')

    def test_unknown_saved_mode_defaults_to_normal(self):
        self.page.restore_state({'mode':'unknown'})
        self.assertEqual(self.page.mode, 'normal')
    def test_reentrant_tick_does_not_import_twice(self):
        original=self.backend.import_file
        def action(*args):self.page._tick();return original(*args)
        self.backend.import_file=action;self.page.start();self.page._tick()
        self.assertEqual(self.backend.events,[('import','A')])
    def test_restore_state_and_virtual_selected_directory(self):
        self.page.restore_state({'output':'/Game/Restored','last_folder':'C:/Data'})
        self.assertEqual(self.page.save_state(),{'output':'/Game/Restored','last_folder':'C:/Data','mode':'normal'})
        fake.EditorUtilityLibrary=types.SimpleNamespace(get_selected_folder_paths=lambda:['/All/Game/Imported'])
        self.page._pick_folder();self.assertEqual(self.page.output.text(),'/Game/Imported')
    def test_duplicate_prompt_is_nonmodal_and_updates_names(self):
        self.page.files.append(self.page.files[0]);self.page.start()
        self.assertFalse(self.page.rename_dialog.isModal())
        self.assertIn('A_1',self.page.rename_text.toPlainText())
    def test_move_changes_actual_queue_order(self):
        self.page._populate();self.page.table.selectRow(1);self.page.move_selected(-1);self.page.start()
        self.page._tick();self.assertEqual(self.backend.events,[('import','B')])
    def test_failure_rows_continue_and_cleanup(self):
        self.backend.import_fail.add('A');self.page.start()
        for _ in range(12):
            self.backend.time+=5.1;self.page._tick()
        self.assertEqual([i.status for i in self.page.items],['import_failed','completed'])
        self.assertFalse(callbacks);self.assertFalse(self.host._scan_active)

    def test_low_memory_is_shown_before_native_call_and_queue_continues(self):
        self.backend.low=True
        original=self.backend.import_file
        def action(*args):
            self.assertIn('低内存预警',self.page.memory_label.text())
            return original(*args)
        self.backend.import_file=action
        self.page.start()
        for _ in range(12):
            self.backend.time+=5.1;self.page._tick()
        self.assertEqual([i.status for i in self.page.items],['completed','completed'])
        self.assertIsNone(self.page.session)


if __name__=='__main__':unittest.main()
