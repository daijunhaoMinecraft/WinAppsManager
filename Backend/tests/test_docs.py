from pathlib import Path
import sys
import unittest

from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.main import create_app


class DocsTests(unittest.TestCase):
    """``/docs`` 必须使用仓库内自带的 Swagger UI 资源，不依赖外部 CDN。"""

    def setUp(self) -> None:
        self.api = create_app()
        self.client = TestClient(self.api)

    def tearDown(self) -> None:
        self.client.close()

    def test_docs_page_uses_local_swagger_assets(self) -> None:
        response = self.client.get("/docs")
        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertIn("/static/swagger/swagger-ui-bundle.js", body)
        self.assertIn("/static/swagger/swagger-ui.css", body)
        self.assertNotIn("cdn.jsdelivr.net", body)

    def test_swagger_assets_are_served(self) -> None:
        js = self.client.get("/static/swagger/swagger-ui-bundle.js")
        self.assertEqual(js.status_code, 200)
        content_type = js.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        self.assertIn(content_type, {"application/javascript", "text/javascript"})
        self.assertGreater(len(js.content), 100_000)

        css = self.client.get("/static/swagger/swagger-ui.css")
        self.assertEqual(css.status_code, 200)
        self.assertIn("text/css", css.headers.get("content-type", ""))

    def test_redoc_is_disabled(self) -> None:
        self.assertEqual(self.client.get("/redoc").status_code, 404)


if __name__ == "__main__":
    unittest.main()
