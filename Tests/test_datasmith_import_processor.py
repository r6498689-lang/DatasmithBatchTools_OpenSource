import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Content/Python'))
sys.modules['unreal'] = types.ModuleType('unreal')
from datasmith_batch_tools import datasmith_import_processor as p


class Backend:
    def __init__(self):
        self.events = []
        self.paths = set()
        self.import_fail = set()
        self.save_fail = set()
        self.saves = 0
        self.time = 0.0
        self.low = False
    def finish_compilation(self): self.events.append(('compile', None))
    def cleanup(self): self.events.append(('cleanup', None))
    def memory_status(self):
        return {'available_physical_gb':0.1 if self.low else 10.0,'available_virtual_gb':0.1 if self.low else 20.0,
                'used_physical_gb':1.0,'used_virtual_gb':2.0}
    def occupied(self, path): return path.casefold() in self.paths
    def import_file(self, item, root, settings):
        self.events.append(('import', item.name))
        self.paths.add(item.target.casefold())
        if item.name in self.import_fail: raise RuntimeError('import boom')
        return {'scene': item.target+'/'+item.name, 'actors': 2}
    def save_all(self, root):
        self.saves += 1
        self.events.append(('save', self.saves))
        if self.saves in self.save_fail: raise RuntimeError('save boom')
        return root+'/Map'


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.backend = Backend()
    def tearDown(self): self.temp.cleanup()
    def file(self, name):
        file = self.root/name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('<DatasmithUnrealScene/>')
        return str(file)
    def session(self, names=('A.udatasmith','B.udatasmith')):
        rows = p.build_queue([self.file(n) for n in names], '/Game/Out', self.backend.occupied)
        session = p.BatchSession(rows, '/Game/Out', 'settings', self.backend, clock=lambda:self.backend.time)
        session.journal=lambda:None
        return session
    def drain(self, session):
        for _ in range(30):
            self.backend.time += 5.1
            session.step()
            if session.done:return
        self.fail('queue did not finish')
    def test_strict_import_save_order(self):
        session=self.session();self.drain(session)
        self.assertEqual(self.backend.events,[('import','A'),('compile',None),('save',1),('cleanup',None),('import','B'),('compile',None),('save',2),('cleanup',None)])
        self.assertTrue(all(i.status=='completed' for i in session.items))
    def test_failure_still_saves_then_continues(self):
        self.backend.import_fail.add('A')
        session=self.session();self.drain(session)
        self.assertEqual(self.backend.events,[('import','A'),('compile',None),('save',1),('cleanup',None),('import','B'),('compile',None),('save',2),('cleanup',None)])
        self.assertEqual([i.status for i in session.items],['import_failed','completed'])
    def test_save_failure_is_visible_and_continues(self):
        self.backend.save_fail.add(1)
        session=self.session();self.drain(session)
        self.assertEqual([i.status for i in session.items],['save_failed','completed'])
        self.assertEqual(session.items[0].save_error,'save boom')
    def test_both_failures_retained(self):
        self.backend.import_fail.add('A');self.backend.save_fail.add(1)
        session=self.session();self.drain(session)
        self.assertIn('import boom', session.items[0].message)
        self.assertIn('save boom', session.items[0].message)
    def test_cancel_between_import_and_save_must_save(self):
        session=self.session();session.step();session.step(cancel=True);session.step(cancel=True);session.step(cancel=True)
        self.assertEqual(self.backend.events,[('import','A'),('compile',None),('save',1),('cleanup',None)])
        session.step()
        self.assertTrue(session.done and session.cancelled)
        self.assertEqual(session.items[1].status,'cancelled')
    def test_cancel_before_import_does_not_save(self):
        session=self.session();session.step(cancel=True)
        self.assertEqual(self.backend.events,[])
        self.assertTrue(all(i.status=='cancelled' for i in session.items))
    def test_duplicate_names_and_existing_folder(self):
        self.backend.paths.add('/game/out/a')
        session=self.session(('one/A.udatasmith','two/A.udatasmith','three/a.udatasmith'))
        self.assertEqual([i.name for i in session.items],['A_1','A_2','a_3'])
        self.drain(session)
        self.assertEqual([e[1] for e in self.backend.events if e[0]=='import'],['A_1','A_2','a_3'])
    def test_late_collision_avoids_reserved_queue_names(self):
        session=self.session(('one/A.udatasmith','two/A.udatasmith'))
        self.backend.paths.add('/game/out/a')
        self.drain(session)
        self.assertEqual([i.name for i in session.items],['A_2','A_1'])
    def test_deleted_source_fails_then_continues(self):
        session=self.session();Path(session.items[0].source).unlink();self.drain(session)
        self.assertEqual([i.status for i in session.items],['import_failed','completed'])
        self.assertEqual(self.backend.saves,2)
    def test_invalid_file_does_not_block_valid_file(self):
        session=self.session(('A.txt','B.udatasmith'));self.drain(session)
        self.assertEqual([i.status for i in session.items],['invalid','completed'])
        self.assertEqual(self.backend.events,[('import','B'),('compile',None),('save',1),('cleanup',None)])
    def test_unicode_and_virtual_game_path(self):
        self.assertEqual(p.game_directory('/All/Game/导入/建筑/'),'/Game/导入/建筑')
        self.assertEqual(p.safe_name('建筑 A?'),'建筑_A_')
    def test_reject_bad_roots(self):
        for bad in ['/Game','/Engine/Foo','C:/Assets','/Game/A/../B','/Game/A//B','/Game/A.B']:
            with self.subTest(bad=bad), self.assertRaises(ValueError):p.game_directory(bad)
    def test_report_records_options_statuses_and_errors(self):
        session=self.session();self.backend.save_fail.add(1);self.drain(session)
        with patch.object(p.storage,'candidate_directories',return_value=[str(self.root/'reports')]):
            path=Path(session.write_report());report=json.loads(path.read_text(encoding='utf-8'))
            self.assertTrue(report['done'])
            self.assertEqual(report['counts'],{'save_failed':1,'completed':1})
            self.assertEqual(report['files'][0]['save_error'],'save boom')
            self.assertTrue(path.with_suffix('.csv').read_bytes().startswith(b'\xef\xbb\xbf'))
    def test_done_session_is_idempotent(self):
        session=self.session();self.drain(session);events=list(self.backend.events)
        session.step();session.step(cancel=True)
        self.assertEqual(events,self.backend.events)

    def test_low_memory_warns_but_does_not_pause(self):
        self.backend.low=True
        session=self.session();self.drain(session)
        self.assertTrue(session.done)
        self.assertTrue(all(i.status=='completed' for i in session.items))
        self.assertTrue(all('低内存预警' in i.memory_warning for i in session.items))

    def test_cooldown_requires_full_five_seconds(self):
        session=self.session()
        for _ in range(4):session.step()
        self.assertEqual(session.phase,'cooldown')
        self.backend.time=4.99;session.step()
        self.assertEqual(len([e for e in self.backend.events if e[0]=='import']),1)
        self.backend.time=5;session.step();session.step()
        self.assertEqual(len([e for e in self.backend.events if e[0]=='import']),2)

    def test_journal_written_before_native_import(self):
        session=self.session()
        snapshots=[]
        session.journal=lambda:snapshots.append((session.items[session.index].status,list(self.backend.events)))
        session.step()
        self.assertEqual(snapshots,[('importing',[])])

    def test_journal_failure_does_not_start_native_import(self):
        session=self.session()
        def fail():raise OSError('disk full')
        session.journal=fail
        with self.assertRaises(OSError):session.step()
        self.assertEqual(self.backend.events,[])
        self.assertTrue(session.done)

    def test_cleanup_error_records_failure_and_continues(self):
        session=self.session()
        self.backend.cleanup=lambda:(_ for _ in ()).throw(RuntimeError('cleanup boom'))
        self.drain(session)
        self.assertTrue(session.done)
        self.assertTrue(all(i.status=='maintenance_failed' for i in session.items))

    def test_cancel_cooldown_stops_without_another_import(self):
        session=self.session()
        for _ in range(4):session.step()
        session.step(cancel=True)
        self.assertTrue(session.done and session.cancelled)
        self.assertEqual(session.items[1].status,'cancelled')

    def save_backend(self, package='/Temp/Untitled_1.Untitled'):
        world=types.SimpleNamespace(get_path_name=lambda:package)
        backend=p.UnrealBackend.__new__(p.UnrealBackend)
        backend.world=world;backend.check_world=lambda:None;backend.occupied=lambda path:False
        return backend

    def test_first_map_save_failure_still_saves_content(self):
        backend=self.save_backend()
        api=types.SimpleNamespace(save_map=lambda *args:False,save_dirty_packages=lambda *args:True,
                                  get_dirty_map_packages=lambda:[],get_dirty_content_packages=lambda:[])
        with patch.object(p.unreal,'EditorLoadingAndSavingUtils',api,create=True), patch.object(api,'save_dirty_packages',wraps=api.save_dirty_packages) as save:
            with self.assertRaisesRegex(RuntimeError,'首次保存失败'):backend.save_all('/Game/Out')
            save.assert_called_once_with(True,True)

    def test_saved_map_keeps_its_path(self):
        backend=self.save_backend('/Game/Existing/Map.Map')
        api=types.SimpleNamespace(save_map=lambda *args:False,save_dirty_packages=lambda *args:True,
                                  get_dirty_map_packages=lambda:[],get_dirty_content_packages=lambda:[])
        with patch.object(p.unreal,'EditorLoadingAndSavingUtils',api,create=True), patch.object(api,'save_map',wraps=api.save_map) as save:
            self.assertEqual(backend.save_all('/Game/Out'),'/Game/Existing/Map')
            save.assert_not_called()

    def test_remaining_dirty_package_is_a_save_failure(self):
        backend=self.save_backend('/Game/Existing/Map.Map')
        api=types.SimpleNamespace(save_dirty_packages=lambda *args:True,get_dirty_map_packages=lambda:[],
             get_dirty_content_packages=lambda:[types.SimpleNamespace(get_path_name=lambda:'/Game/Locked')])
        with patch.object(p.unreal,'EditorLoadingAndSavingUtils',api,create=True):
            with self.assertRaisesRegex(RuntimeError,'/Game/Locked'):backend.save_all('/Game/Out')

    def test_old_native_module_is_rejected_before_import(self):
        old = types.SimpleNamespace(release_temporary_resources=lambda:None)
        with patch.object(p.unreal,'DatasmithBatchImportLibrary',old,create=True), patch.object(
                p.unreal,'DatasmithImportOptions',lambda:None,create=True):
            with self.assertRaisesRegex(RuntimeError,'重启'):
                p.UnrealBackend()


if __name__=='__main__':unittest.main()
