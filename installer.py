"""RVC 环境安装：跟着唱歌插件走，不再单独一个插件。

官方仓库源码 + 独立 venv + PyTorch(cu128) + hubert/rmvpe 权重。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import requests

RVC_ZIP = "https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/archive/refs/tags/2.3.260718.zip"
HF = "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main"
RVC_WEIGHTS = {"hubert_base.pt": f"{HF}/hubert_base.pt", "rmvpe.pt": f"{HF}/rmvpe.pt"}


class RvcInstaller:
    def __init__(self, root: Path, plugin_dir: Path, log):
        self.root = root
        self.pkg = root / "pkg" / "RVC-official"
        self.log_file = Path(plugin_dir) / "install.log"
        self.state_file = Path(plugin_dir) / "install_state.json"
        self._log = log
        self._state = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"state": "idle"}

    def _save(self) -> None:
        self.state_file.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")

    def log(self, text: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {text}"
        self._log(line)
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def ready(self) -> bool:
        for py in self.root.glob("pkg/*/runtime/python.exe"):
            if (py.parent.parent / "assets" / "weights").is_dir():
                return True
        return False

    def status(self) -> dict:
        return {"ready": self.ready(), "state": self._state.get("state", "idle"), "path": str(self.root)}

    def start(self) -> dict:
        if self._state.get("state") == "running":
            return {"ok": False, "message": "已经在装了"}
        self._state["state"] = "running"
        self._save()
        threading.Thread(target=self._run, daemon=True).start()
        return {"ok": True, "message": "开始安装 RVC，日志在插件目录 install.log"}

    def _run(self) -> None:
        try:
            self._install()
            self._state["state"] = "done"
            self.log("RVC 安装完成")
        except Exception as exc:  # noqa: BLE001
            self._state["state"] = "failed"
            self.log(f"RVC 安装失败：{exc}")
        finally:
            self._save()

    def _download(self, url: str, out: Path) -> None:
        self.log(f"下载 {url}")
        out.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with out.open("wb") as f:
                for chunk in r.iter_content(1024 * 512):
                    if chunk:
                        f.write(chunk)

    def _install(self) -> None:
        self.pkg.mkdir(parents=True, exist_ok=True)
        zip_path = self.pkg / "rvc.zip"
        self._download(RVC_ZIP, zip_path)
        self.log("解压 RVC")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(self.pkg)
        inner = next((d for d in self.pkg.iterdir() if d.is_dir() and (d / "requirements.txt").is_file()), None)
        if inner is None:
            raise RuntimeError("压缩包里没有 requirements.txt")
        for item in inner.iterdir():
            shutil.move(str(item), str(self.pkg / item.name))
        inner.rmdir()

        venv = self.pkg / "runtime"
        py = venv / "Scripts" / "python.exe"
        if not py.is_file():
            self.log("创建虚拟环境")
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        self.log("装 PyTorch cu128")
        subprocess.run([str(py), "-m", "pip", "install", "torch", "torchaudio",
                        "--index-url", "https://download.pytorch.org/whl/cu128"], check=True)
        self.log("装 RVC 依赖")
        subprocess.run([str(py), "-m", "pip", "install", "-r", str(self.pkg / "requirements.txt")], check=True)

        assets = self.pkg / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        for name, url in RVC_WEIGHTS.items():
            target = assets / name
            if not target.exists():
                self._download(url, target)
        (assets / "weights").mkdir(exist_ok=True)
        (assets / "indices").mkdir(exist_ok=True)
