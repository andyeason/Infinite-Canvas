import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


class CanvasLogCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.assets = self.root / "assets"
        self.generated = self.assets / "output"
        self.inputs = self.assets / "input"
        self.legacy = self.root / "output"
        self.data = self.root / "data"
        self.canvases = self.data / "canvases"
        self.conversations = self.data / "conversations"
        self.previews = self.data / "media_previews"
        for path in (self.generated, self.inputs, self.legacy, self.canvases, self.conversations, self.previews):
            path.mkdir(parents=True, exist_ok=True)
        self.history = self.root / "history.json"
        self.global_config = self.root / "global_config.json"
        self.asset_library = self.data / "asset_library.json"
        self.history.write_text("[]", encoding="utf-8")
        self.global_config.write_text("{}", encoding="utf-8")
        self.patches = [
            patch.object(main, "ASSETS_DIR", str(self.assets)),
            patch.object(main, "OUTPUT_OUTPUT_DIR", str(self.generated)),
            patch.object(main, "OUTPUT_DIR", str(self.legacy)),
            patch.object(main, "DATA_DIR", str(self.data)),
            patch.object(main, "CANVAS_DIR", str(self.canvases)),
            patch.object(main, "CONVERSATION_DIR", str(self.conversations)),
            patch.object(main, "MEDIA_PREVIEW_DIR", str(self.previews)),
            patch.object(main, "HISTORY_FILE", str(self.history)),
            patch.object(main, "GLOBAL_CONFIG_FILE", str(self.global_config)),
            patch.object(main, "ASSET_LIBRARY_PATH", str(self.asset_library)),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def write_canvas(self, canvas_id, logs, nodes=None, updated_at=0):
        value = {
            "id": canvas_id,
            "title": "test",
            "logs": logs,
            "nodes": nodes or [],
            "connections": [],
            "viewport": {"x": 0, "y": 0, "scale": 1},
            "updated_at": updated_at,
        }
        (self.canvases / f"{canvas_id}.json").write_text(json.dumps(value), encoding="utf-8")

    def generated_file(self, name="result.png", content=b"image"):
        path = self.generated / name
        path.write_bytes(content)
        return path, f"/assets/output/{name}"

    def test_collects_nested_local_media_only(self):
        value = {
            "items": [
                {"url": "/assets/output/a.png"},
                "https://example.com/remote.png",
                {"nested": "/output/b.png?x=1"},
            ]
        }
        self.assertEqual(
            main.collect_local_media_urls(value),
            ["/assets/output/a.png", "/output/b.png?x=1"],
        )

    def test_generated_path_rejects_input_files(self):
        generated_path, generated_url = self.generated_file()
        input_path = self.inputs / "reference.png"
        input_path.write_bytes(b"input")
        self.assertEqual(main.generated_media_path_from_url(generated_url), str(generated_path.resolve()))
        self.assertIsNone(main.generated_media_path_from_url("/assets/input/reference.png"))

    def test_output_url_resolves_only_to_output_mount(self):
        generated_collision = self.generated / "same.png"
        legacy_output = self.legacy / "same.png"
        generated_collision.write_bytes(b"generated")
        legacy_output.write_bytes(b"mounted-output")

        self.assertEqual(main.generated_media_path_from_url("/output/same.png"), str(legacy_output.resolve()))

    async def test_record_only_keeps_media(self):
        path, url = self.generated_file()
        self.write_canvas("record_only", [{"id": "log-1", "outputs": [url]}])

        result = await main.delete_canvas_log(
            "record_only",
            main.DeleteCanvasLogRequest(log_id="log-1", delete_unreferenced_media=False),
        )

        self.assertTrue(path.exists())
        self.assertEqual(result["removed_files"], [])
        self.assertEqual(result["canvas"]["logs"], [])

    async def test_cleanup_keeps_media_referenced_by_a_node(self):
        path, url = self.generated_file()
        self.write_canvas(
            "referenced",
            [{"id": "log-1", "outputs": [{"url": url}]}],
            nodes=[{"id": "node-1", "generatedOutputs": [url]}],
        )

        result = await main.delete_canvas_log(
            "referenced",
            main.DeleteCanvasLogRequest(log_id="log-1", delete_unreferenced_media=True),
        )

        self.assertTrue(path.exists())
        self.assertEqual(result["removed_files"], [])
        self.assertEqual(result["skipped_referenced"], [path.name])

    async def test_cleanup_deletes_unreferenced_media_and_preview(self):
        path, url = self.generated_file()
        preview = Path(main.media_preview_cache_paths(str(path), 256)[0])
        preview.write_bytes(b"preview")
        self.write_canvas("unreferenced", [{"id": "log-1", "outputs": [url]}])

        result = await main.delete_canvas_log(
            "unreferenced",
            main.DeleteCanvasLogRequest(log_id="log-1", delete_unreferenced_media=True),
        )

        self.assertFalse(path.exists())
        self.assertFalse(preview.exists())
        self.assertEqual(result["removed_files"], [path.name])
        self.assertEqual(result["removed_previews"], 1)

    async def test_cleanup_preserves_media_when_json_is_unreadable(self):
        path, url = self.generated_file()
        (self.canvases / "being-written.json").write_text("{", encoding="utf-8")
        self.write_canvas("unreadable_owner", [{"id": "log-1", "outputs": [url]}])

        result = await main.delete_canvas_log(
            "unreadable_owner",
            main.DeleteCanvasLogRequest(log_id="log-1", delete_unreferenced_media=True),
        )

        self.assertTrue(path.exists())
        self.assertEqual(result["skipped_referenced"], [path.name])

    async def test_stale_delete_is_rejected_without_changing_canvas(self):
        path, url = self.generated_file()
        self.write_canvas("stale", [{"id": "log-1", "outputs": [url]}], updated_at=200)

        with self.assertRaises(main.HTTPException) as caught:
            await main.delete_canvas_log(
                "stale",
                main.DeleteCanvasLogRequest(
                    log_id="log-1",
                    delete_unreferenced_media=True,
                    base_updated_at=100,
                ),
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(path.exists())
        stored = json.loads((self.canvases / "stale.json").read_text(encoding="utf-8"))
        self.assertEqual([item["id"] for item in stored["logs"]], ["log-1"])

    async def test_saved_version_advances_when_clock_is_unchanged(self):
        _, url = self.generated_file()
        self.write_canvas("monotonic", [{"id": "log-1", "outputs": [url]}], updated_at=200)

        with patch.object(main, "now_ms", return_value=200):
            result = await main.delete_canvas_log(
                "monotonic",
                main.DeleteCanvasLogRequest(log_id="log-1", base_updated_at=200),
            )

        self.assertEqual(result["canvas"]["updated_at"], 201)


if __name__ == "__main__":
    unittest.main()
