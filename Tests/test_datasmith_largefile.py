import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_datasmith_import_processor import Backend, p


class LargeBackend(Backend):
    def __init__(self):
        super().__init__()
        self.released = []
        self.bad_bindings = set()
        self.bad_textures = set()
        self.bad_geometry = set()
        self.stage_fail = set()
        self.late_fail = set()

    def import_stage(self, item, root, settings):
        self.events.append((item.stage, item.name))
        if (item.name, item.stage) in self.stage_fail:
            raise RuntimeError('stage failed')
        self.paths.add(item.target.casefold())
        return {'scene': item.target+'/'+item.name+'.'+item.name, 'actors': 0}

    def release_source_cache(self, root):
        self.released.append(root)

    def binding_mesh_ids(self, item):
        return [] if item.name in self.bad_geometry else ['mesh1', 'mesh2']

    def texture_issues(self, item):
        return ['missing texture'] if item.name in self.bad_textures else []

    def bind_mesh(self, item, mesh_id):
        self.events.append(('bind', item.name + ':' + mesh_id))
        return {'checked': 2, 'repaired': 2, 'unassigned': 1,
                'issues': ['missing material'] if item.name in self.bad_bindings else []}

    def check_mesh(self, item, mesh_id):
        self.events.append(('recheck', item.name + ':' + mesh_id))
        return {'checked':2, 'repaired':0, 'unassigned':1,
                'issues':['binding changed after repair'] if item.name in self.late_fail else []}


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = LargeBackend()
        self.items = []
        files = []
        for name in ['A', 'B']:
            path = Path(self.temp.name)/(name+'.udatasmith')
            path.write_text('<DatasmithUnrealScene/>')
            files.append(str(path))
        self.items = p.build_queue(files, '/Game/Out', self.backend.occupied)
        self.session = p.BatchSession(self.items, '/Game/Out', 'settings', self.backend,
                                      clock=lambda: self.backend.time, mode='large')
        self.session.journal = lambda: None

    def tearDown(self):
        self.temp.cleanup()

    def drain(self):
        for _ in range(150):
            self.backend.time += 5.1
            self.session.step()
            if self.session.done:
                return
        self.fail('queue did not finish')

    def advance_to(self, phase):
        for _ in range(60):
            if self.session.phase == phase:
                return
            self.backend.time += 5.1
            self.session.step()
        self.fail(phase)

    def test_all_stages_finish_before_next_file(self):
        self.drain()
        stages = [e for e in self.backend.events if e[0] in ('geometry', 'materials', 'bind')]
        self.assertEqual(stages, [('geometry','A'),('materials','A'),('bind','A:mesh1'),('bind','A:mesh2'),
                                  ('geometry','B'),('materials','B'),('bind','B:mesh1'),('bind','B:mesh2')])
        self.assertTrue(all(i.status == 'completed' and i.validation_completed for i in self.items))
        self.assertEqual(self.backend.saves, 8)
        self.assertEqual(self.items[0].checked_slots, 4)
        self.assertEqual(self.items[0].repaired_slots, 4)

    def test_material_phase_continues_same_output_without_suffix(self):
        self.drain()
        self.assertEqual([i.name for i in self.items], ['A','B'])

    def test_missing_material_is_incomplete_and_next_file_continues(self):
        self.backend.bad_bindings.add('A')
        self.drain()
        self.assertEqual([i.status for i in self.items], ['incomplete','completed'])
        self.assertIn('missing material', self.items[0].message)

    def test_missing_texture_is_not_success(self):
        self.backend.bad_textures.add('A')
        self.drain()
        self.assertEqual(self.items[0].status, 'incomplete')

    def test_no_source_meshes_cannot_pass(self):
        self.backend.bad_geometry.add('A')
        self.drain()
        self.assertEqual(self.items[0].status, 'incomplete')

    def test_geometry_failure_saves_but_does_not_import_materials(self):
        self.backend.stage_fail.add(('A','geometry'))
        self.drain()
        self.assertEqual(self.items[0].status, 'import_failed')
        self.assertNotIn(('materials','A'), self.backend.events)
        self.assertEqual(self.items[1].status, 'completed')

    def test_material_stage_failure_is_not_success(self):
        self.backend.stage_fail.add(('A','materials'))
        self.drain()
        self.assertEqual(self.items[0].status, 'import_failed')
        self.assertFalse(self.items[0].validation_completed)

    def test_save_failure_skips_registry_release(self):
        self.backend.save_fail.add(1)
        self.drain()
        self.assertEqual(self.items[0].status, 'save_failed')
        self.assertNotIn('/Game/Out/A', self.backend.released)

    def test_cancel_after_geometry_saves_then_marks_partial(self):
        self.session.step()
        for _ in range(3):
            self.session.step(cancel=True)
        self.assertTrue(self.session.done and self.session.cancelled)
        self.assertEqual([i.status for i in self.items], ['incomplete','cancelled'])
        self.assertEqual(self.backend.saves, 1)

    def test_cancel_between_stages_does_not_show_success(self):
        self.advance_to('cooldown')
        self.session.step(cancel=True)
        self.assertEqual([i.status for i in self.items], ['incomplete','cancelled'])

    def test_cancel_during_validation_drains_final_save(self):
        self.advance_to('validate')
        self.session.step(cancel=True)
        for _ in range(3):
            self.session.step(cancel=True)
        self.assertTrue(self.session.done)
        self.assertEqual([i.status for i in self.items], ['incomplete','cancelled'])

    def test_low_memory_never_pauses(self):
        self.backend.low = True
        self.drain()
        self.assertTrue(all(i.status == 'completed' and i.memory_warning for i in self.items))

    def test_final_recheck_failure_cannot_show_success(self):
        self.backend.late_fail.add('A')
        self.drain()
        self.assertEqual([i.status for i in self.items], ['incomplete','completed'])

    def test_changed_source_between_phases_is_rejected(self):
        self.advance_to('cooldown')
        Path(self.items[0].source).write_text('changed datasmith')
        self.drain()
        self.assertEqual(self.items[0].status, 'import_failed')
        self.assertNotIn(('materials','A'), self.backend.events)

    def test_journal_records_each_import_stage(self):
        journal = []
        self.session.journal = lambda: journal.append((self.session.index, self.items[self.session.index].stage))
        self.drain()
        self.assertIn((0,'geometry'), journal)
        self.assertIn((0,'materials'), journal)

    def test_report_persists_mode_validation_and_all_issues(self):
        self.backend.bad_bindings.add('A')
        self.drain()
        with patch.object(p.storage,'candidate_directories',return_value=[self.temp.name]):
            report = json.loads(Path(self.session.write_report()).read_text(encoding='utf-8'))
        self.assertEqual(report['import_mode'], 'large')
        self.assertEqual(report['files'][0]['binding_issues'], ['missing material','missing material'])

    def test_normal_mode_also_releases_saved_source_cache(self):
        self.session.mode = 'normal'
        self.drain()
        self.assertEqual(self.backend.released, ['/Game/Out/A','/Game/Out/B'])

    def chunk_backend(self):
        original = self.backend.import_stage
        def sliced(item, root, settings):
            result = original(item, root, settings)
            if item.stage == 'geometry':
                result.update(geometry_index=min(item.geometry_index+24, 50), geometry_total=50)
            return result
        self.backend.import_stage = sliced

    def test_geometry_slices_save_and_clean_before_next_slice(self):
        self.chunk_backend()
        self.drain()
        self.assertEqual([i.name for i in self.items], ['A', 'B'])
        self.assertTrue(all(i.geometry_index == 50 and i.geometry_total == 50 and i.status == 'completed' for i in self.items))
        events = self.backend.events
        geometry = [i for i, e in enumerate(events) if e == ('geometry', 'A')]
        self.assertEqual(len(geometry), 3)
        for start, end in zip(geometry, geometry[1:]):
            between = [e[0] for e in events[start+1:end]]
            self.assertEqual(between, ['compile', 'save', 'cleanup'])
        self.assertEqual(self.backend.saves, 12)

    def test_texture_slices_save_and_clean_before_materials(self):
        original = self.backend.import_stage
        def sliced(item, root, settings):
            result = original(item, root, settings)
            if item.stage == 'textures':
                result.update(texture_index=min(item.texture_index+8, 17), texture_total=17)
            return result
        self.backend.import_stage = sliced
        self.drain()
        self.assertEqual([i.name for i in self.items], ['A','B'])
        self.assertTrue(all(i.texture_index == 17 and i.status == 'completed' for i in self.items))
        events = self.backend.events
        slices = [i for i,e in enumerate(events) if e == ('textures','A')]
        self.assertEqual(len(slices), 3)
        for start, end in zip(slices, slices[1:]):
            self.assertEqual([e[0] for e in events[start+1:end]], ['compile','save','cleanup'])
        self.assertGreater(events.index(('materials','A')), slices[-1])

    def test_texture_failure_skips_materials_and_continues(self):
        self.backend.stage_fail.add(('A','textures'))
        self.drain()
        self.assertEqual([i.status for i in self.items], ['import_failed','completed'])
        self.assertNotIn(('materials','A'), self.backend.events)

    def test_cancel_between_geometry_slices_retains_partial_and_stops(self):
        self.chunk_backend()
        self.advance_to('cooldown')
        self.session.step(cancel=True)
        self.assertEqual([i.status for i in self.items], ['incomplete', 'cancelled'])
        self.assertEqual([e for e in self.backend.events if e[0] == 'geometry'], [('geometry','A')])

    def test_source_changed_between_geometry_slices_stops_current_file(self):
        self.chunk_backend()
        self.advance_to('cooldown')
        Path(self.items[0].source).write_text('changed datasmith')
        self.drain()
        self.assertEqual([i.status for i in self.items], ['import_failed', 'completed'])
        self.assertNotIn(('materials','A'), self.backend.events)

    def test_stalled_geometry_cursor_is_not_success(self):
        original = self.backend.import_stage
        def stalled(item, root, settings):
            result = original(item, root, settings)
            if item.stage == 'geometry':
                result.update(geometry_index=0, geometry_total=50)
            return result
        self.backend.import_stage = stalled
        self.drain()
        self.assertTrue(all(i.status == 'import_failed' for i in self.items))
