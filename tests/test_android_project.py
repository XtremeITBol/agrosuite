"""Verificación estática del proyecto Android.

Este entorno no tiene el SDK de Android, así que no se puede compilar. Pero
las fallas de compilación más frecuentes en Android son estáticas y se pueden
detectar sin compilador:

  * una referencia `R.algo.nombre` a un recurso que no existe,
  * un apóstrofo o un `&` sin escapar dentro de un string (error clásico de
    aapt en proyectos en español),
  * un `%` suelto en un string que se pasa por `getString(id, args)`,
  * una actividad declarada en el manifiesto que no corresponde a ninguna clase,
  * el `network_security_config` sin la confianza en CA de usuario, que es
    justamente lo que hace funcionar el certificado de la finca.

Cada una de estas, si pasa, rompe la compilación o la app en el teléfono.
"""
from __future__ import annotations

import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ANDROID = Path(__file__).resolve().parent.parent / "android"
RES = ANDROID / "app" / "src" / "main" / "res"
JAVA = ANDROID / "app" / "src" / "main" / "java" / "bo" / "agrosuite" / "campo"
MANIFEST = ANDROID / "app" / "src" / "main" / "AndroidManifest.xml"

ANDROID_NS = "{http://schemas.android.com/apk/res/android}"


def java_sources() -> list[Path]:
    return sorted(JAVA.glob("*.java"))


def declared_resources() -> dict[str, set[str]]:
    """Todos los recursos que el proyecto define, por tipo."""
    res: dict[str, set[str]] = {
        "id": set(), "string": set(), "layout": set(), "color": set(),
        "mipmap": set(), "drawable": set(), "style": set(), "xml": set(),
    }
    for layout in (RES / "layout").glob("*.xml"):
        res["layout"].add(layout.stem)
        for el in ET.parse(layout).iter():
            raw = el.get(f"{ANDROID_NS}id")
            if raw:
                res["id"].add(raw.split("/")[-1])
    for values in RES.rglob("values*/*.xml"):
        for el in ET.parse(values).getroot():
            name = el.get("name")
            if not name:
                continue
            if el.tag in res:
                res[el.tag].add(name)
    for folder, kind in (("mipmap", "mipmap"), ("drawable", "drawable"), ("xml", "xml")):
        for d in RES.glob(f"{folder}*"):
            for f in d.iterdir():
                if f.is_file():
                    res[kind].add(f.stem)
    return res


class TestAndroidProject(unittest.TestCase):

    def test_project_layout(self):
        for p in ["build.gradle", "settings.gradle", "gradle.properties",
                  "app/build.gradle", "app/src/main/AndroidManifest.xml"]:
            with self.subTest(file=p):
                self.assertTrue((ANDROID / p).exists(), f"falta android/{p}")
        self.assertTrue(java_sources(), "no hay fuentes Java")

    def test_all_xml_parses(self):
        for p in ANDROID.rglob("*.xml"):
            with self.subTest(file=str(p.relative_to(ANDROID))):
                ET.parse(p)

    def test_r_references_exist(self):
        """Una referencia R.* a un recurso inexistente no compila."""
        res = declared_resources()
        pattern = re.compile(r"\bR\.(\w+)\.(\w+)\b")
        missing = []
        for src in java_sources():
            for kind, name in pattern.findall(src.read_text(encoding="utf-8")):
                if kind not in res:
                    continue
                if name not in res[kind]:
                    missing.append(f"{src.name}: R.{kind}.{name}")
        self.assertEqual(missing, [], f"recursos referenciados que no existen: {missing}")

    def test_manifest_references_real_resources(self):
        res = declared_resources()
        root = ET.parse(MANIFEST).getroot()
        app = root.find("application")
        checks = {
            f"{ANDROID_NS}icon": "mipmap", f"{ANDROID_NS}roundIcon": "mipmap",
            f"{ANDROID_NS}label": "string", f"{ANDROID_NS}theme": "style",
            f"{ANDROID_NS}networkSecurityConfig": "xml",
        }
        for attr, kind in checks.items():
            val = app.get(attr)
            if val and val.startswith("@"):
                name = val.split("/")[-1]
                with self.subTest(attr=attr):
                    self.assertIn(name, res[kind],
                                  f"el manifiesto apunta a @{kind}/{name}, que no existe")

    def test_manifest_activities_have_classes(self):
        root = ET.parse(MANIFEST).getroot()
        classes = {p.stem for p in java_sources()}
        for act in root.find("application").findall("activity"):
            name = act.get(f"{ANDROID_NS}name", "")
            with self.subTest(activity=name):
                self.assertTrue(name.startswith("."), f"'{name}' debería ser relativo")
                self.assertIn(name[1:], classes,
                              f"el manifiesto declara {name} sin clase Java correspondiente")

    def test_launcher_activity_declared(self):
        xml = MANIFEST.read_text(encoding="utf-8")
        self.assertIn("android.intent.category.LAUNCHER", xml,
                      "sin actividad de lanzamiento la app no aparece en el teléfono")

    def test_required_permissions(self):
        xml = MANIFEST.read_text(encoding="utf-8")
        for perm in ["INTERNET", "ACCESS_FINE_LOCATION", "CAMERA"]:
            with self.subTest(permission=perm):
                self.assertIn(f"android.permission.{perm}", xml)

    def test_strings_escaped_for_aapt(self):
        """Apóstrofos y ampersands sin escapar rompen aapt. Clásico en español."""
        problems = []
        for values in RES.rglob("values*/*.xml"):
            raw = values.read_text(encoding="utf-8")
            for el in ET.parse(values).getroot():
                if el.tag != "string" or not el.text:
                    continue
                # ET ya resolvió las entidades; se revisa el texto crudo.
                m = re.search(rf'<string name="{re.escape(el.get("name"))}">(.*?)</string>',
                              raw, re.S)
                if not m:
                    continue
                body = m.group(1)
                for ch in ("'", '"'):
                    if re.search(rf"(?<!\\){re.escape(ch)}", body):
                        problems.append(f"{values.name}/{el.get('name')}: {ch} sin escapar")
                if re.search(r"&(?!amp;|lt;|gt;|quot;|apos;|#)", body):
                    problems.append(f"{values.name}/{el.get('name')}: & sin escapar")
        self.assertEqual(problems, [], f"strings que romperían aapt: {problems}")

    def test_format_placeholders_match_usage(self):
        """getString(id, a, b) debe coincidir con los %1$s del recurso."""
        strings = {}
        for values in RES.rglob("values/*.xml"):
            for el in ET.parse(values).getroot():
                if el.tag == "string" and el.text:
                    strings[el.get("name")] = el.text

        call = re.compile(r"getString\(\s*R\.string\.(\w+)\s*((?:,[^;]*?)?)\)")
        problems = []
        for src in java_sources():
            for name, args in call.findall(src.read_text(encoding="utf-8")):
                if name not in strings:
                    continue
                n_ph = len(set(re.findall(r"%(\d)\$", strings[name])))
                n_args = 0 if not args.strip() else args.count(",")
                if n_ph != n_args:
                    problems.append(
                        f"{src.name}: R.string.{name} tiene {n_ph} marcador(es) "
                        f"pero se pasan {n_args} argumento(s)")
        self.assertEqual(problems, [], str(problems))

    def test_strings_with_placeholders_are_well_formed(self):
        """Un % suelto en un string formateado lanza IllegalFormatException."""
        problems = []
        for values in RES.rglob("values/*.xml"):
            for el in ET.parse(values).getroot():
                if el.tag != "string" or not el.text:
                    continue
                if "%" in el.text:
                    # todos los % deben ser parte de un marcador posicional
                    leftovers = re.sub(r"%\d\$[sd]", "", el.text)
                    if "%" in leftovers:
                        problems.append(f"{el.get('name')}: % sin formato posicional")
        self.assertEqual(problems, [], str(problems))

    def test_trusts_user_installed_ca(self):
        """Sin esto el WebView ignora la CA de la finca y la app no conecta.

        Es la razón principal de que el APK exista, así que se verifica
        explícitamente en lugar de asumirlo.
        """
        cfg = RES / "xml" / "network_security_config.xml"
        self.assertTrue(cfg.exists(), "falta network_security_config.xml")
        root = ET.parse(cfg).getroot()
        srcs = {c.get("src") for c in root.iter("certificates")}
        self.assertIn("user", srcs,
                      "el WebView ignorará la CA instalada por el usuario")
        self.assertIn("system", srcs, "se perdió la confianza en las CA del sistema")
        self.assertIn("networkSecurityConfig", MANIFEST.read_text(encoding="utf-8"),
                      "el manifiesto no referencia la configuración de red")

    def test_never_bypasses_certificate_validation(self):
        """Aceptar cualquier certificado convertiría el HTTPS en decorativo."""
        for src in java_sources():
            body = src.read_text(encoding="utf-8")
            with self.subTest(file=src.name):
                self.assertNotIn("handler.proceed()", body,
                                 f"{src.name} acepta certificados inválidos")
                self.assertNotIn("setHostnameVerifier", body)
                self.assertNotIn("ALLOW_ALL_HOSTNAME_VERIFIER", body)

    def test_webview_enables_offline_storage(self):
        """IndexedDB y DOM storage son la cola sin señal del monitoreo."""
        body = (JAVA / "MainActivity.java").read_text(encoding="utf-8")
        for needed in ["setJavaScriptEnabled(true)", "setDomStorageEnabled(true)",
                       "setGeolocationEnabled(true)", "onShowFileChooser"]:
            with self.subTest(setting=needed):
                self.assertIn(needed, body)

    def test_gradle_config_is_coherent(self):
        app = (ANDROID / "app" / "build.gradle").read_text(encoding="utf-8")
        root = (ANDROID / "build.gradle").read_text(encoding="utf-8")
        self.assertIn("namespace 'bo.agrosuite.campo'", app)
        self.assertIn("applicationId \"bo.agrosuite.campo\"", app)
        # network_security_config existe desde API 24
        m = re.search(r"minSdk\s+(\d+)", app)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 24,
                                "minSdk menor a 24 no soporta network_security_config")
        self.assertIn("com.android.application", root)
        # signingConfigs debe declararse antes de que buildTypes lo use. La
        # comparación va sobre el código, no sobre los comentarios, que
        # mencionan ambos bloques y darían un falso positivo.
        code = re.sub(r"//[^\n]*|/\*.*?\*/", "", app, flags=re.S)
        self.assertLess(code.index("signingConfigs {"), code.index("buildTypes {"),
                        "signingConfigs debe declararse antes de buildTypes: "
                        "Gradle evalúa el DSL en orden")

    def test_launcher_icons_present_in_every_density(self):
        for d in ("mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi"):
            folder = RES / f"mipmap-{d}"
            with self.subTest(density=d):
                self.assertTrue((folder / "ic_launcher.png").exists())
                self.assertTrue((folder / "ic_launcher_round.png").exists())
                self.assertTrue((folder / "ic_launcher.png").read_bytes().startswith(b"\x89PNG"))

    def test_adaptive_icon_references_existing_drawables(self):
        res = declared_resources()
        adaptive = RES / "mipmap-anydpi-v26" / "ic_launcher.xml"
        self.assertTrue(adaptive.exists())
        for el in ET.parse(adaptive).getroot():
            ref = el.get(f"{ANDROID_NS}drawable")
            if ref and ref.startswith("@"):
                kind, name = ref[1:].split("/")
                with self.subTest(ref=ref):
                    self.assertIn(name, res[kind], f"{ref} no existe")

    def test_java_braces_balanced(self):
        """Chequeo grosero pero efectivo de truncamiento accidental."""
        for src in java_sources():
            body = re.sub(r'"(?:\\.|[^"\\])*"', '""', src.read_text(encoding="utf-8"))
            body = re.sub(r"//[^\n]*|/\*.*?\*/", "", body, flags=re.S)
            with self.subTest(file=src.name):
                self.assertEqual(body.count("{"), body.count("}"),
                                 f"{src.name}: llaves desbalanceadas")
                self.assertEqual(body.count("("), body.count(")"),
                                 f"{src.name}: paréntesis desbalanceados")

    def test_workflows_exist_for_both_platforms(self):
        wf = ANDROID.parent / ".github" / "workflows"
        for f in ("build-android.yml", "build-windows.yml"):
            with self.subTest(workflow=f):
                self.assertTrue((wf / f).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
