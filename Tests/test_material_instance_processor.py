"""Offline business-rule and failure-path regressions, no UE asset writes."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Content' / 'Python'))
from datasmith_batch_tools import material_instance_processor as p


def tex(role='BaseColor', mid='107', fid='001', name='Bridge_Asphalt_01', category='Asphalt', prefix=None):
    return f"{prefix or p.DEFAULT_SOURCE}/{category}/{mid}_{category}_UV_Unknown/{role}_{mid}_{name}_{fid}"


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.config = p.options()

    def plan(self, *paths, exists=None):
        return p.build_plans(paths, self.config, exists)

    def test_requested_path(self):
        rows, _, _ = self.plan(tex())
        self.assertEqual(rows[0].target, '/Game/Materials/GeneratedInstances/Asphalt/107_Asphalt/107_Bridge_Asphalt_01_001')

    def test_content_browser_virtual_path(self):
        self.assertEqual(p.game_directory('/All/Game/Textures'), p.DEFAULT_SOURCE)
        self.assertEqual(p.game_directory('/All/Game'), '/Game')

    def test_virtual_path_slashes_and_whitespace(self):
        self.assertEqual(p.game_directory('  \\All\\Game\\Textures\\  '), p.DEFAULT_SOURCE)

    def test_virtual_options_normalized_for_both_directories(self):
        config = p.options(source='/All/Game/Textures', output='/All/Game/Materials/GeneratedInstances')
        self.assertEqual(config['source'], p.DEFAULT_SOURCE)
        self.assertEqual(config['output'], p.DEFAULT_OUTPUT)

    def test_virtual_paths_do_not_allow_other_mounts_or_traversal(self):
        for path in ('/All/Engine/Textures', '/All/Plugin/Textures', '/All/GameExtra', '/All/Game/../Engine', '/All'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                p.game_directory(path)

    def test_invalid_directory_reports_actual_selection(self):
        with self.assertRaisesRegex(ValueError, '/All/Engine/Textures'):
            p.game_directory('/All/Engine/Textures')

    def test_basecolorao_creates_instance(self):
        rows, _, _ = self.plan(tex('BaseColorAO', '040', name='Rock_Surface_A', category='Stone'))
        self.assertEqual(rows[0].target, '/Game/Materials/GeneratedInstances/Stone/040_Stone/040_Rock_Surface_A_001')

    def test_each_base_shared_other_maps(self):
        paths = [tex(fid=f'{i:03}') for i in range(1, 5)] + [tex('Normal', fid='005'), tex('PackedRGBA', fid='006')]
        rows, _, _ = self.plan(*paths)
        self.assertEqual(len(rows), 4)
        self.assertEqual({r.normal for r in rows}, {tex('Normal', fid='005')})
        self.assertTrue(all(r.metal == r.rough == tex('PackedRGBA', fid='006') for r in rows))

    def test_mixed_base_prefixes_both_create(self):
        rows, _, _ = self.plan(tex(), tex('BaseColorAO', fid='002'))
        self.assertEqual(len(rows), 2)

    def test_base_prefix_collision(self):
        rows, _, _ = self.plan(tex(), tex('BaseColorAO'))
        self.assertTrue(all(r.status == 'conflict' for r in rows))

    def test_numeric_file_sort(self):
        rows, _, _ = self.plan(tex(), tex('Normal', fid='1000'), tex('Normal', fid='999'))
        self.assertEqual(rows[0].normal, tex('Normal', fid='999'))

    def test_independent_and_packed_pooled(self):
        rows, _, _ = self.plan(tex(), tex('PackedRGBA', fid='005'), tex('Metallic', fid='003'), tex('Roughness', fid='006'))
        self.assertEqual(rows[0].metal, tex('Metallic', fid='003'))
        self.assertEqual(rows[0].rough, tex('PackedRGBA', fid='005'))

    def test_channelm_supports_both(self):
        rows, _, _ = self.plan(tex(), tex('ChannelM', fid='002'))
        self.assertEqual(rows[0].metal, rows[0].rough)
        self.assertTrue(rows[0].metal)

    def test_tie_uses_path_deterministically(self):
        a, b = tex('PackedRGBA', fid='002'), tex('ChannelM', fid='002')
        rows, _, _ = self.plan(tex(), a, b)
        self.assertEqual(rows[0].metal, min(a, b, key=str.casefold))
        rows2, _, _ = self.plan(b, a, tex())
        self.assertEqual(rows, rows2)

    def test_missing_maps_use_fallback(self):
        rows, _, _ = self.plan(tex())
        self.assertEqual((rows[0].normal, rows[0].metal, rows[0].rough), ('', '', ''))
        self.assertEqual(len(rows[0].notes), 3)

    def test_no_base_group_skipped(self):
        rows, issues, _ = self.plan(tex('Normal'))
        self.assertFalse(rows)
        self.assertIn('没有 BaseColor', issues[0]['reason'])

    def test_future_category_and_long_id(self):
        rows, _, _ = self.plan(tex(mid='1007', category='New_Category'))
        self.assertEqual(rows[0].category, 'New_Category')
        self.assertIn('/New_Category/1007_New_Category/', rows[0].target)

    def test_custom_output_root_keeps_both_folder_levels(self):
        config = p.options(output='/Game/Library/Materials')
        rows, _, _ = p.build_plans([tex()], config)
        self.assertEqual(rows[0].target, '/Game/Library/Materials/Asphalt/107_Asphalt/107_Bridge_Asphalt_01_001')

    def test_categories_have_separate_parent_folders(self):
        rows, _, _ = self.plan(tex(), tex(mid='002', category='Brick'))
        self.assertEqual({r.target[len(self.config['output'])+1:].split('/')[0] for r in rows}, {'Asphalt', 'Brick'})

    def test_bad_name_and_directory_mismatch(self):
        rows, issues, _ = self.plan(tex().replace('/BaseColor_107_', '/BaseColor_108_'), tex().replace('_001', '_01'))
        self.assertFalse(rows)
        self.assertEqual(len(issues), 2)

    def test_same_id_different_categories_invalid(self):
        rows, issues, _ = self.plan(tex(), tex(category='Wood'))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.status == 'invalid' for r in rows))

    def test_exact_roles_unsupported_ignored(self):
        rows, issues, _ = self.plan(tex('BaseColorAO'), tex('AmbientOcclusion', fid='002'))
        self.assertEqual(len(rows), 1)
        self.assertIn('忽略未支持用途', issues[0]['reason'])

    def test_existing_instance_skip_other_conflict(self):
        self.assertEqual(self.plan(tex(), exists=lambda _: 'instance')[0][0].status, 'existing')
        self.assertEqual(self.plan(tex(), exists=lambda _: 'other')[0][0].status, 'conflict')

    def test_deduplicate_paths(self):
        rows, _, summary = self.plan(tex(), tex() + '.' + tex().rsplit('/', 1)[-1])
        self.assertEqual(len(rows), 1)
        self.assertEqual(summary['textures'], 1)

    def test_path_boundaries(self):
        with self.assertRaises(ValueError):
            p.parse_texture(tex(prefix=p.DEFAULT_SOURCE + '_Other'), p.DEFAULT_SOURCE)

    def test_invalid_options(self):
        for kw in ({'source': 'D:/Texture'}, {'output': '/Engine/Materials'}, {'rough': float('nan')}, {'metal': 2}):
            with self.assertRaises(ValueError):
                p.options(**kw)

    def test_journal_and_cancel_preserve_success(self):
        rows, _, _ = self.plan(tex(), tex(fid='002'))
        with tempfile.TemporaryDirectory() as temp, patch.object(p.storage, 'candidate_directories', return_value=[temp]):
            batch = p.begin_batch(self.config, rows, [])
            rows[0].status = 'created'
            report = p.finish_batch(batch, cancelled=True)
            saved = json.loads(Path(report).read_text(encoding='utf-8'))
            self.assertEqual(saved['summary'], {'created': 1, 'cancelled': 1})
            self.assertEqual(saved['state'], 'cancelled')
            self.assertTrue(Path(report).with_suffix('.csv').is_file())


class Texture:
    def __init__(self, path, srgb=True, compression='color'):
        self.path = path
        self.values = dict(srgb=srgb, compression_settings=compression, virtual_texture_streaming=False)

    def get_editor_property(self, name): return self.values[name]
    def get_path_name(self): return self.path


class CreationTests(unittest.TestCase):
    def setUp(self):
        self.config = p.options()
        self.plan = p.build_plans([tex()], self.config)[0][0]
        self.assets = {tex(): Texture(tex())}
        self.events = []
        self.saved = True
        self.writes = {}
        self.switches = {}
        self.scalars = {}
        self.fake = SimpleNamespace(Texture2D=Texture, TextureCompressionSettings=SimpleNamespace(TC_NORMALMAP='normal'))
        self.fake.EditorAssetLibrary = SimpleNamespace(
            does_asset_exist=lambda path: path in self.assets,
            find_asset_data=lambda path: SimpleNamespace(asset_class_path=SimpleNamespace(asset_name='MaterialInstanceConstant')),
            load_asset=lambda path: self.assets.get(path), does_directory_exist=lambda _: True,
            save_loaded_asset=lambda *a, **kw: self.saved,
        )
        def create(name, folder, *args):
            self.events.append('create')
            asset = SimpleNamespace()
            self.assets[folder + '/' + name] = asset
            return asset
        def set_texture(instance, name, asset):
            self.writes[name] = asset
            return True
        def set_switch(instance, name, value, **kw):
            self.switches[name] = value
            return True
        def set_scalar(instance, name, value):
            self.scalars[name] = value
            return False
        self.fake.MaterialEditingLibrary = SimpleNamespace(
            set_material_instance_parent=lambda *a: None,
            set_material_instance_texture_parameter_value=set_texture,
            set_material_instance_static_switch_parameter_value=set_switch,
            set_material_instance_scalar_parameter_value=set_scalar,
            get_material_instance_scalar_parameter_value=lambda i, n: self.scalars[n],
            update_material_instance=lambda *a: None,
            get_material_instance_texture_parameter_value=lambda i, n: self.writes[n],
            get_material_instance_static_switch_parameter_value=lambda i, n: self.switches[n],
        )
        self.fake.AssetToolsHelpers = SimpleNamespace(get_asset_tools=lambda: SimpleNamespace(create_asset=create))
        self.fake.MaterialInstanceConstant = object
        self.fake.MaterialInstanceConstantFactoryNew = lambda: None
        self.patcher = patch.object(p, 'ue', return_value=self.fake)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_create_with_fallback_switches(self):
        p.create_one(self.plan, self.config, object())
        self.assertEqual(self.plan.status, 'created')
        self.assertEqual(self.writes['base_color'], self.assets[tex()])
        self.assertTrue(all(not self.switches[name] for name in p.SWITCHES.values()))

    def test_new_existing_asset_not_overwritten(self):
        self.assets[self.plan.target] = object()
        p.create_one(self.plan, self.config, object())
        self.assertEqual(self.plan.status, 'existing')
        self.assertFalse(self.events)

    def test_create_receives_recursive_category_folder(self):
        made=[]
        self.fake.EditorAssetLibrary.does_directory_exist=lambda _: False
        self.fake.EditorAssetLibrary.make_directory=lambda folder: made.append(folder) or True
        p.create_one(self.plan, self.config, object())
        self.assertEqual(made, ['/Game/Materials/GeneratedInstances/Asphalt/107_Asphalt'])
        self.assertIn(self.plan.target,self.assets)
        self.assertNotIn('/Game/Materials/GeneratedInstances/107_Bridge_Asphalt_01_001',self.assets)

    def test_engine_false_return_does_not_mean_failure(self):
        original = self.fake.MaterialEditingLibrary.set_material_instance_texture_parameter_value
        def assign(*args):
            original(*args)
            return False
        self.fake.MaterialEditingLibrary.set_material_instance_texture_parameter_value = assign
        p.create_one(self.plan, self.config, object())
        self.assertEqual(self.plan.status, 'created')

    def test_readback_failure_reported(self):
        self.fake.MaterialEditingLibrary.get_material_instance_texture_parameter_value = lambda *args: None
        p.create_one(self.plan, self.config, object())
        self.assertEqual(self.plan.status, 'failed')
        self.assertIn('参数核对失败', self.plan.message)

    def test_missing_source_no_partial_asset(self):
        self.assets.clear()
        p.create_one(self.plan, self.config, object())
        self.assertEqual(self.plan.status, 'failed')
        self.assertFalse(self.events)

    def test_save_failure_not_success(self):
        self.saved = False
        p.create_one(self.plan, self.config, object())
        self.assertEqual(self.plan.status, 'failed')
        self.assertIn('保存失败', self.plan.message)

    def test_srgb_data_automatic_branch_and_shared_texture(self):
        data = tex('PackedRGBA', fid='002')
        self.assets[data] = Texture(data, srgb=True)
        self.plan.metal = self.plan.rough = data
        p.create_one(self.plan, self.config, object())
        self.assertEqual(self.plan.status, 'created')
        self.assertIs(self.writes['Metal'], self.writes['Roughness'])
        self.assertTrue(self.switches[p.SRGB_SWITCHES['metal']])
        self.assertTrue(self.assets[data].get_editor_property('srgb'))

    def test_linear_data_automatic_branch(self):
        data = tex('Metallic', fid='002')
        self.assets[data] = Texture(data, srgb=False)
        self.plan.metal = data
        p.create_one(self.plan, self.config, object())
        self.assertEqual(self.plan.status, 'created')
        self.assertFalse(self.switches[p.SRGB_SWITCHES['metal']])

    def test_normal_compressed_data_rejected_before_creation(self):
        data = tex('PackedRGBA', fid='002')
        self.assets[data] = Texture(data, srgb=False, compression='normal')
        self.plan.metal = data
        p.create_one(self.plan, self.config, object())
        self.assertEqual(self.plan.status, 'failed')
        self.assertFalse(self.events)


if __name__ == '__main__':
    unittest.main()
