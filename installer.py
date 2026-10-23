"""RVC 环境安装：完全照 GPT-SoVITS 安装器的做法来。

官方仓库源码 + 独立 Python 3.12 虚拟环境 + 官方 cu128 两段式依赖 + 官方 assets 权重。
官方仓库自带 requirments_cu128_py312.txt，其中写明：
  1) torch==2.7.1+cu128 / torchaudio==2.7.1+cu128
  2) pip install -r requirments_cu128_py312.txt
权重按官方 README 的 assets 结构放：hubert_base/ 三个文件、rmvpe/rmvpe.pt。
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
TORCH_INDEX = "https://mirrors.nju.edu.cn/pytorch/whl/cu128"
PYPI_INDEX = "https://mirrors.pku.edu.cn/pypi/simple"
ASSETS = {
    "hubert_base/config.json": f"{HF}/hubert_base/config.json",
    "hubert_base/preprocessor_config.json": f"{HF}/hubert_base/preprocessor_config.json",
    "hubert_base/pytorch_model.bin": f"{HF}/hubert_base/pytorch_model.bin",
    "rmvpe/rmvpe.pt": f"{HF}/rmvpe/rmvpe.pt",
}


class RvcInstaller:
    def __init__(self, root: Path, plugin_dir: Path, log):
        self.root = root
        self.pkg = root / "pkg" / "RVC-official"
        self.log_file = Path(plugin_dir) / "install.log"
        self.state_file = Path(plugin_dir) / "install_state.json"
        self._log = log
        self._state = self._load()

    # ------------------------------------------------------------------ 状态
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

    def _runtime(self) -> Path | None:
        py = self.pkg / "runtime" / "python.exe"
        return py if py.is_file() else None

    def _existing(self) -> Path | None:
        if self.ready_at(self.pkg):
            return self.pkg
        for cand in self.root.glob("pkg/*"):
            if cand.is_dir() and self.ready_at(cand):
                return cand
        return None

    @staticmethod
    def ready_at(pkg: Path) -> bool:
        return (
            (pkg / "runtime" / "python.exe").is_file()
            and (pkg / "configs" / "config.py").is_file()
            and (pkg / "assets" / "hubert_base" / "pytorch_model.bin").is_file()
            and (pkg / "assets" / "rmvpe" / "rmvpe.pt").is_file()
        )

    def ready(self) -> bool:
        return self._existing() is not None

    def status(self) -> dict:
        found = self._existing() or self.pkg
        return {
            "ready": self.ready(),
            "state": self._state.get("state", "idle"),
            "path": str(found),
            "python": str(found / "runtime" / "python.exe") if self.ready() else "",
        }

    def start(self) -> dict:
        if self._state.get("state") == "running":
            return {"ok": False, "message": "已经在装了"}
        self._state["state"] = "running"
        self._save()
        threading.Thread(target=self._run, daemon=True).start()
        return {"ok": True, "message": "开始安装 RVC，看日志进度"}

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

    # ------------------------------------------------------------------ 下载
    def _download(self, url: str, out: Path) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.is_file() and out.stat().st_size > 1024 * 1024:
            self.log(f"已存在，跳过 {out.name}")
            return
        self.log(f"下载 {out.name} …")
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with out.open("wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        done += len(chunk)
                        if total and done % (200 * 1024 * 1024) < 1024 * 1024:
                            self.log(f"  {done // 1048576}MB / {total // 1048576}MB")

    # ------------------------------------------------------------------ 安装
    def _install(self) -> None:
        self.pkg.mkdir(parents=True, exist_ok=True)
        zip_path = self.pkg / "rvc.zip"
        self._download(RVC_ZIP, zip_path)
        self.log("解压官方源码")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(self.pkg)
        inner = next((d for d in self.pkg.iterdir() if d.is_dir() and (d / "configs" / "config.py").is_file()), None)
        if inner is None:
            raise RuntimeError("压缩包里没找到 configs/config.py")
        for item in inner.iterdir():
            shutil.move(str(item), str(self.pkg / item.name))
        inner.rmdir()

        req = self.pkg / "requirments_cu128_py312.txt"
        if not req.is_file():
            raise RuntimeError("官方仓库里没有 requirments_cu128_py312.txt")

        py = self._runtime()
        if py is None:
            self.log(f"创建虚拟环境（用 {sys.version.split()[0]}，官方要求 3.12）")
            subprocess.run([sys.executable, "-m", "venv", str(self.pkg / "runtime")], check=True)
            py = self._runtime()
        if py is None:
            raise RuntimeError("虚拟环境创建失败")

        self.log("安装 PyTorch 2.7.1 + cu128")
        subprocess.run(
            [str(py), "-m", "pip", "install", "torch==2.7.1+cu128", "torchaudio==2.7.1+cu128",
             "--index-url", TORCH_INDEX, "--extra-index-url", PYPI_INDEX],
            check=True,
        )
        self.log("安装官方依赖 requirments_cu128_py312.txt")
        subprocess.run([str(py), "-m", "pip", "install", "-r", str(req)], check=True)

        assets = self.pkg / "assets"
        for name, url in ASSETS.items():
            self._download(url, assets / name)
        for d in ("weights", "indices"):
            (assets / d).mkdir(parents=True, exist_ok=True)
        (self.pkg / "logs").mkdir(parents=True, exist_ok=True)

        self.log("自检：导入 configs.config")
        subprocess.run(
            [str(py), "-c", "import configs.config; print('config ok')"],
            cwd=str(self.pkg), check=True,
        )
        self.log("RVC 自检通过")
