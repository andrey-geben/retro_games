"""Сборка Android APK (WebView-обёртка вокруг нашего веб-плеера).

Использует Android SDK (aapt2, d8, zipalign, apksigner) и JDK, которые уже
могут быть установлены на сервере. Если SDK не найден — сборка недоступна,
страница «Приложения» честно об этом сообщает.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import library

PACKAGE = "com.retrogames.player"
APP_LABEL = "Retro Games"
MIN_SDK = "24"
TARGET_SDK = "34"

JAVA_SOURCE = """package {package};

import android.app.Activity;
import android.os.Bundle;
import android.view.WindowManager;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

public class MainActivity extends Activity {{
    private WebView web;

    @Override
    protected void onCreate(Bundle state) {{
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        web = new WebView(this);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setAllowFileAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        web.setWebViewClient(new WebViewClient());
        setContentView(web);
        web.loadUrl(getString(R.string.app_url));
    }}

    @Override
    public void onBackPressed() {{
        if (web != null && web.canGoBack()) {{
            web.goBack();
        }} else {{
            super.onBackPressed();
        }}
    }}
}}
"""

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="{package}">
    <uses-permission android:name="android.permission.INTERNET" />
    <uses-sdk android:minSdkVersion="{min_sdk}" android:targetSdkVersion="{target_sdk}" />
    <application
        android:label="{label}"
        android:usesCleartextTraffic="true"
        android:theme="@android:style/Theme.NoTitleBar.Fullscreen"
        android:hardwareAccelerated="true">
        <activity android:name=".MainActivity"
            android:exported="true"
            android:configChanges="orientation|screenSize|keyboardHidden"
            android:screenOrientation="sensorLandscape">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
    </application>
</manifest>
"""

STRINGS = """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name">{label}</string>
    <string name="app_url">{url}</string>
</resources>
"""


def _candidate_sdks() -> list[Path]:
    candidates = []
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = os.environ.get(env)
        if value:
            candidates.append(Path(value))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Android" / "Sdk")
    candidates.append(Path.home() / "Android" / "Sdk")
    return candidates


def sdk() -> dict[str, Path] | None:
    for root in _candidate_sdks():
        build_tools = root / "build-tools"
        platforms = root / "platforms"
        if not build_tools.is_dir() or not platforms.is_dir():
            continue
        versions = sorted((p for p in build_tools.iterdir() if p.is_dir()), reverse=True)
        android_jars = sorted((p for p in platforms.iterdir() if (p / "android.jar").is_file()),
                              reverse=True)
        if not versions or not android_jars:
            continue
        tools = versions[0]
        aapt2 = tools / "aapt2.exe" if os.name == "nt" else tools / "aapt2"
        zipalign = tools / "zipalign.exe" if os.name == "nt" else tools / "zipalign"
        apksigner = tools / ("apksigner.bat" if os.name == "nt" else "apksigner")
        d8 = tools / ("d8.bat" if os.name == "nt" else "d8")
        if not all(p.exists() for p in (aapt2, zipalign, apksigner, d8)):
            continue
        return {
            "root": root,
            "android_jar": android_jars[0] / "android.jar",
            "aapt2": aapt2,
            "zipalign": zipalign,
            "apksigner": apksigner,
            "d8": d8,
            "build_tools": tools,
        }
    return None


def _java_tools() -> dict[str, str] | None:
    java_home = os.environ.get("JAVA_HOME")
    candidates = []
    if java_home:
        candidates.append(Path(java_home))
    candidates.extend([
        Path(r"C:\Program Files\Eclipse Adoptium\jdk-17.0.20.101-hotspot"),
        Path(r"C:\Program Files\Android\Android Studio\jbr"),
        Path(r"C:\Program Files\Java"),
    ])
    for root in candidates:
        javac = root / "bin" / ("javac.exe" if os.name == "nt" else "javac")
        keytool = root / "bin" / ("keytool.exe" if os.name == "nt" else "keytool")
        java = root / "bin" / ("java.exe" if os.name == "nt" else "java")
        if javac.exists() and keytool.exists() and java.exists():
            return {"home": str(root), "javac": str(javac), "keytool": str(keytool), "java": str(java)}
    return None


def available() -> bool:
    return sdk() is not None and _java_tools() is not None


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 300) -> None:
    result = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        errors="replace", timeout=timeout,
    )
    if result.returncode != 0:
        tail = "\n".join(result.stdout.strip().splitlines()[-8:])
        raise RuntimeError(f"{Path(cmd[0]).name}: {tail}")


def _keystore(work: Path) -> tuple[Path, str, str]:
    tools = _java_tools()
    assert tools is not None
    folder = library.DATA_DIR / "android"
    folder.mkdir(parents=True, exist_ok=True)
    store = folder / "retro.keystore"
    password_file = folder / "keystore.password"
    if store.is_file() and password_file.is_file():
        return store, password_file.read_text(encoding="utf-8").strip(), "retro"
    import secrets

    password = secrets.token_urlsafe(18)
    _run([
        tools["keytool"], "-genkeypair", "-keystore", str(store),
        "-alias", "retro", "-keyalg", "RSA", "-keysize", "2048",
        "-validity", "10000", "-storepass", password, "-keypass", password,
        "-dname", "CN=Retro Games, O=Retro Games, C=RU",
    ])
    password_file.write_text(password, encoding="utf-8")
    return store, password, "retro"


def build(app_url: str, output: Path) -> Path:
    """Собирает подписанный APK с WebView, открывающим app_url."""
    sdkt = sdk()
    tools = _java_tools()
    if sdkt is None or tools is None:
        raise RuntimeError("Android SDK или JDK не найден на сервере")

    work = Path(tempfile.mkdtemp(prefix="apk-", dir=str(library.DATA_DIR / "tmp")))
    try:
        src = work / "src" / Path(*PACKAGE.split("."))
        res_values = work / "res" / "values"
        src.mkdir(parents=True, exist_ok=True)
        res_values.mkdir(parents=True, exist_ok=True)

        (src / "MainActivity.java").write_text(
            JAVA_SOURCE.format(package=PACKAGE), encoding="utf-8")
        (work / "AndroidManifest.xml").write_text(
            MANIFEST.format(package=PACKAGE, label=APP_LABEL,
                            min_sdk=MIN_SDK, target_sdk=TARGET_SDK),
            encoding="utf-8")
        (res_values / "strings.xml").write_text(
            STRINGS.format(label=APP_LABEL, url=app_url), encoding="utf-8")

        compiled = work / "compiled.zip"
        _run([str(sdkt["aapt2"]), "compile", "--dir", str(work / "res"), "-o", str(compiled)])

        base_apk = work / "base.apk"
        gen = work / "gen"
        _run([
            str(sdkt["aapt2"]), "link", "-o", str(base_apk),
            "-I", str(sdkt["android_jar"]),
            "--manifest", str(work / "AndroidManifest.xml"),
            "--java", str(gen),
            str(compiled),
        ])

        classes = work / "classes"
        classes.mkdir()
        java_files = [str(src / "MainActivity.java")] + [
            str(p) for p in gen.rglob("R.java")
        ]
        _run([tools["javac"], "-source", "8", "-target", "8",
              "-classpath", str(sdkt["android_jar"]), "-d", str(classes)] + java_files)

        dex_dir = work / "dex"
        dex_dir.mkdir()
        class_files = [str(p) for p in classes.rglob("*.class")]
        _run([str(sdkt["d8"]), "--lib", str(sdkt["android_jar"]),
              "--output", str(dex_dir)] + class_files)

        apk = work / "unsigned.apk"
        shutil.copy2(base_apk, apk)
        with zipfile.ZipFile(apk, "a", zipfile.ZIP_DEFLATED) as zf:
            zf.write(dex_dir / "classes.dex", "classes.dex")

        aligned = work / "aligned.apk"
        _run([str(sdkt["zipalign"]), "-f", "4", str(apk), str(aligned)])

        store, password, alias = _keystore(work)
        output.parent.mkdir(parents=True, exist_ok=True)
        _run([
            str(sdkt["apksigner"]), "sign",
            "--ks", str(store), "--ks-pass", f"pass:{password}",
            "--key-pass", f"pass:{password}", "--ks-key-alias", alias,
            "--out", str(output), str(aligned),
        ])
        _run([str(sdkt["apksigner"]), "verify", str(output)])
        return output
    finally:
        shutil.rmtree(work, ignore_errors=True)
