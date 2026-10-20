"""Offline regressions; fake UE objects, real atomic journal files in a temp dir."""
import importlib
import os
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Content" / "Python"))
fake = types.ModuleType("unreal")
events, assets = [], {}


class Texture:
    def __init__(self, name="T_BaseColor"):
        self.name = name
        self.values = {"max_texture_size": 0, "lod_bias": 0, "adjust_brightness": 1.0, "adjust_hue": 0.0, "adjust_saturation": 1.0, "adjust_vibrance": 0.0, "compression_settings": "TC_DEFAULT", "srgb": True, "virtual_texture_streaming": False, "lod_group": "TEXTUREGROUP_WORLD"}
        self.identity = "source-1"

    def get_name(self): return self.name
    def get_path_name(self): return "/Game/Test/" + self.name + "." + self.name
    def get_editor_property(self, name): return self.values[name]


class Native:
    @staticmethod
    def get_texture_source_size(asset): return types.SimpleNamespace(x=4096, y=2048)
    @staticmethod
    def get_texture_source_identity(asset): return asset.identity
    @staticmethod
    def texture_has_effective_mips(asset): return True
    @staticmethod
    def apply_texture_properties(asset, values):
        events.append("modify")
        asset.values.update(values)
        return ""


fake.Texture2D = Texture
fake.DatasmithTextureBatchLibrary = Native
fake.log_warning = fake.log_error = fake.log = lambda *args: None
fake.EditorAssetLibrary = types.SimpleNamespace(load_asset=lambda path: assets.get(path), save_loaded_asset=lambda asset, **kw: True)
sys.modules["unreal"] = fake
p = importlib.import_module("datasmith_batch_tools.texture_processor")


class Tests(unittest.TestCase):
    def setUp(self):
        events.clear()
        assets.clear()
        self.asset = Texture()
        assets[self.asset.get_path_name()] = self.asset
        self.record = p.inspect_texture(self.asset)
        self.options = {"resolution_enabled": True, "resolution_mode": "max_size", "max_texture_size": 2048, "lod_bias": 1, "colors": {}}
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("DMA_TEXTURE_TEST_DIR"))
        self.paths = patch.object(p.storage, "candidate_directories", return_value=[self.temp.name])
        self.paths.start()
        self.original_write = p.write_batch

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def test_maximum_clears_existing_bias(self):
        self.record.values["lod_bias"] = 2
        plan = p.plan_record(self.record, self.options)
        self.assertEqual(plan["after"], {"max_texture_size": 2048, "lod_bias": 0})
        self.assertEqual(p.estimated_size(self.record, plan["after"]), (2048, 1024))

    def test_bias_clears_maximum(self):
        self.record.values["max_texture_size"] = 512
        self.options["resolution_mode"] = "lod_bias"
        self.assertEqual(p.plan_record(self.record, self.options)["after"], {"lod_bias": 1, "max_texture_size": 0})

    def test_no_mip_skips_bias_without_changing_maximum(self):
        record = replace(self.record, has_mips=False)
        self.options["resolution_mode"] = "lod_bias"
        self.assertFalse(p.plan_record(record, self.options)["after"])

    def test_color_requires_manual_confirmation(self):
        self.options.update(resolution_enabled=False, colors={"adjust_brightness": 0.7})
        self.assertFalse(p.plan_record(self.record, self.options)["after"])
        self.assertEqual(p.plan_record(self.record, self.options, True)["after"], {"adjust_brightness": 0.7})

    def test_data_does_not_allow_color(self):
        record = replace(self.record, kind="data")
        self.options["colors"] = {"adjust_hue": 45.0}
        self.assertNotIn("adjust_hue", p.plan_record(record, self.options, True)["after"])

    def test_unknown_can_be_confirmed(self):
        self.options.update(resolution_enabled=False, colors={"adjust_vibrance": 0.5})
        self.assertIn("adjust_vibrance", p.plan_record(replace(self.record, kind="unknown"), self.options, True)["after"])

    def test_missing_source_skips_all(self):
        self.assertFalse(p.plan_record(replace(self.record, editable=False), self.options, True)["after"])

    def test_name_and_compression_safety(self):
        for name, compression in (("T_ORM", "TC_DEFAULT"), ("T_BaseColor", "TC_NORMALMAP"), ("T_Normal", "TC_DEFAULT"), ("T_BaseColor", "TC_HDR")):
            self.assertEqual(p.classify(name, [], compression, True)[0], "data")
        self.assertEqual(p.classify("Tex1", ["BaseColor"], "TC_DEFAULT", True)[0], "basecolor")
        self.assertEqual(p.classify("Tex1", [], "TC_DEFAULT", False)[0], "unknown")

    def test_invalid_options(self):
        for key, value in (("adjust_brightness", -1), ("adjust_hue", float("nan")), ("adjust_vibrance", 2), ("adjust_saturation", float("inf")), ("unexpected", 1)):
            options = dict(self.options, colors={key: value})
            with self.assertRaises(ValueError): p.validate_options(options)

    def test_inactive_options_rejected(self):
        with self.assertRaises(ValueError): p.validate_options(dict(self.options, resolution_enabled=False))

    def test_write_ahead_and_restore(self):
        batch = p.new_batch(self.options)
        def tracked_write(value):
            events.append("journal:" + (value["entries"][-1]["status"] if value["entries"] else "empty"))
            return self.original_write(value)
        with patch.object(p, "write_batch", side_effect=tracked_write):
            entry = p.process_one(batch, self.record, self.options)
        self.assertEqual(entry["status"], "applied")
        self.assertLess(events.index("journal:prepared"), events.index("modify"))
        restore = p.new_batch({}, "restore", batch["batch_id"])
        self.assertEqual(p.restore_one(restore, entry)["status"], "restored")
        self.assertEqual(self.asset.values["max_texture_size"], 0)
        self.assertEqual(p.restore_one(restore, entry)["status"], "skipped")

    def test_repeated_absolute_apply_is_noop(self):
        batch = p.new_batch(self.options)
        p.process_one(batch, self.record, self.options)
        updated = p.inspect_texture(self.asset)
        self.assertFalse(p.plan_record(updated, self.options)["after"])

    def test_drift_since_scan_skipped(self):
        self.asset.values["adjust_saturation"] = 0.6
        batch = p.new_batch(self.options)
        self.assertEqual(p.process_one(batch, self.record, self.options)["status"], "skipped")
        self.assertNotIn("modify", events)

    def test_restore_drift_keeps_new_value(self):
        batch = p.new_batch(self.options)
        entry = p.process_one(batch, self.record, self.options)
        self.asset.values["max_texture_size"] = 512
        restore = p.new_batch({}, "restore", batch["batch_id"])
        result = p.restore_one(restore, entry)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.asset.values["max_texture_size"], 512)

    def test_restore_ignores_unrelated_property_drift(self):
        batch = p.new_batch(self.options)
        entry = p.process_one(batch, self.record, self.options)
        self.asset.values["adjust_hue"] = 35.0
        restore = p.new_batch({}, "restore", batch["batch_id"])
        self.assertEqual(p.restore_one(restore, entry)["status"], "restored")
        self.assertEqual(self.asset.values["adjust_hue"], 35.0)

    def test_changed_source_not_restored(self):
        batch = p.new_batch(self.options)
        entry = p.process_one(batch, self.record, self.options)
        self.asset.identity = "new-source"
        restore = p.new_batch({}, "restore", batch["batch_id"])
        self.assertEqual(p.restore_one(restore, entry)["status"], "skipped")

    def test_failed_journal_blocks_mutation(self):
        batch = p.new_batch(self.options)
        with patch.object(p, "write_batch", side_effect=OSError("disk full")):
            with self.assertRaises(p.RecoveryWriteError): p.process_one(batch, self.record, self.options)
        self.assertNotIn("modify", events)

    def test_save_failure_rolls_back(self):
        batch = p.new_batch(self.options)
        with patch.object(fake.EditorAssetLibrary, "save_loaded_asset", side_effect=[False, True]):
            entry = p.process_one(batch, self.record, self.options)
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(self.asset.values["max_texture_size"], 0)

    def test_history_and_cancelled_report(self):
        batch = p.new_batch(self.options)
        p.process_one(batch, self.record, self.options)
        report = p.finish_batch(batch, cancelled=True)
        self.assertTrue(os.path.isfile(report))
        self.assertTrue(os.path.isfile(report[:-5] + ".csv"))
        self.assertEqual(p.list_history()[0]["status"], "cancelled")

    def test_bad_history_id_rejected(self):
        with self.assertRaises(ValueError): p._history_name("../../asset")

    def test_selected_textures_are_deduplicated(self):
        steps = list(p.scan_items("selected_textures", selected_assets=[self.asset, self.asset, object()]))
        self.assertEqual(len([step for step in steps if "record" in step]), 1)

    def test_selected_models_include_children_and_deduplicate(self):
        material = types.SimpleNamespace(get_path_name=lambda: "/Game/Test/M", get_name=lambda: "M")
        component = types.SimpleNamespace(get_num_materials=lambda: 2, get_material=lambda index: material)
        child = types.SimpleNamespace(get_path_name=lambda: "Child", get_attached_actors=lambda: [], get_components_by_class=lambda cls: [component], get_actor_label=lambda: "Child")
        root = types.SimpleNamespace(get_path_name=lambda: "Root", get_attached_actors=lambda: [child], get_components_by_class=lambda cls: [], get_actor_label=lambda: "Root")
        with patch.object(fake, "EditorActorSubsystem", object, create=True), patch.object(fake, "MeshComponent", object, create=True), patch.object(fake, "MaterialInstanceConstant", type("MI", (), {}), create=True), patch.object(fake, "get_editor_subsystem", return_value=object(), create=True), patch.object(Native, "get_material_texture_assets", return_value=[self.asset], create=True):
            steps = list(p.scan_items("selected_models", selected_actors=[root, child]))
        records = [step["record"] for step in steps if "record" in step]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].materials, ["/Game/Test/M"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
