from __future__ import annotations

import os
from pathlib import Path
import unittest

try:
    from streamlit.testing.v1 import AppTest
except ImportError:  # Permite ejecutar las pruebas puras sin instalar la UI.
    if os.getenv("CI"):
        raise
    AppTest = None


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(AppTest is None, "Streamlit no está instalado en este entorno")
class StreamlitSmokeTests(unittest.TestCase):
    def _entrypoint(self):
        app = AppTest.from_file(ROOT / "home.py", default_timeout=15).run()
        self.assertEqual(list(app.exception), [])
        return app

    def test_home_and_registered_navigation_render_without_exceptions(self) -> None:
        home = self._entrypoint()
        self.assertTrue(home.title)
        self.assertEqual(home.title[0].value, "Encuentra precedentes en las actas")

        registered_pages = (
            "pages/1_Explorador.py",
            "pages/2_Analista_IA.py",
            "pages/3_Administracion.py",
            "pages/4_Integridad.py",
            "pages/5_Catalogo.py",
            "pages/7_Comparar.py",
        )
        for page_path in registered_pages:
            with self.subTest(page=page_path):
                app = self._entrypoint()
                app.switch_page(page_path).run(timeout=15)
                self.assertEqual(list(app.exception), [])


if __name__ == "__main__":
    unittest.main()
