"""大肥鱼唱歌插件：点一首歌，本地分离人声、RVC 换音色、混回伴奏，发一条语音。

只暴露一个模型工具 sing_song：接单、排队、后台合成、发语音。
不往聊天里塞固定话术——要不要接、怎么回，交给模型按人格自己说。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Record

PLUGIN_DIR = Path(__file__).resolve().parent
PIPELINE = PLUGIN_DIR / "sing_pipeline.py"
RVC_CONVERT = PLUGIN_DIR / "rvc_convert.py"
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
MODEL_CN = {
    "luotianyi": "洛天依",
    "miku": "初音未来",
    "tomori": "高松灯",
    "uzuki": "卯月",
    "guanguanV1": "关关",
    "keruanV1": "可软",
    "kikiV1": "琪琪",
    "youzhanv2-xi": "有沾 v2",
}


def _detect_root() -> Path | None:
    """从插件位置往上找机器人根目录：AstrBot/data/plugins/x -> AstrBot 的上一级。"""
    for parent in PLUGIN_DIR.parents:
        if parent.name.lower() == "astrbot":
            return parent.parent
    return None


def _detect_rvc() -> Path | None:
    env_root = os.environ.get("RVC_ROOT")
    if env_root and Path(env_root).is_dir():
        return Path(env_root)
    root = _detect_root()
    candidates = []
    if root:
        candidates.append(root / "rvc")
        candidates += [p for p in root.glob("rvc*") if p.is_dir()]
    for parent in PLUGIN_DIR.parents:
        candidates.append(parent / "rvc")
    for cand in candidates:
        if (cand / "pkg").is_dir():
            return cand
    return None


def _detect_ffmpeg(root: Path | None) -> Path | None:
    env_ff = os.environ.get("FFMPEG")
    if env_ff and Path(env_ff).is_file():
        return Path(env_ff)
    if root:
        for rel in ("bin/ffmpeg.exe", "ffmpeg.exe", "ffmpeg/bin/ffmpeg.exe"):
            cand = root / rel
            if cand.is_file():
                return cand
    found = shutil.which("ffmpeg")
    return Path(found) if found else None


class DafeiyuSingPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = config
        self.out_dir = PLUGIN_DIR / "output"
        self._queue: asyncio.Queue[tuple[AstrMessageEvent, str, str]] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._busy_with: str = ""
        self._last: dict[str, float] = {}

    # ------------------------------------------------------------------ 配置
    def _get(self, key: str, default=None):
        try:
            value = self.cfg.get(key, default)
        except Exception:  # noqa: BLE001
            value = default
        return default if value is None or value == "" else value

    def _paths(self) -> dict:
        root = _detect_root()
        rvc_root = _detect_rvc()
        ffmpeg = _detect_ffmpeg(root)
        pkg = ""
        if rvc_root:
            pkgs = sorted(rvc_root.glob("pkg/*/runtime/python.exe"))
            if pkgs:
                pkg = str(pkgs[0].parent.parent)
        return {
            "rvc_root": str(rvc_root or ""),
            "rvc_pkg": pkg,
            "ffmpeg": str(ffmpeg or ""),
            "rvc_script": str(RVC_CONVERT),
            "work_dir": str(self.out_dir / "work"),
            "qqbot_root": str(root or ""),
        }

    # ------------------------------------------------------------------ 音色
    def _scan_models(self) -> list[str]:
        """列出本机 RVC 里已有的音色名，不下载任何东西。"""
        names: set[str] = set()
        root = _detect_rvc()
        if not root:
            return []
        for pattern in ("pkg/*/assets/weights/*.pth", "models/*/*.pth", "assets/weights/*.pth"):
            for pth in root.glob(pattern):
                if pth.stem and not pth.stem.startswith("."):
                    names.add(pth.stem)
        return sorted(names)

    def _sync_schema_models(self) -> None:
        """把发现的音色写进设置页的下拉选项，省得用户手填名字。"""
        schema_path = PLUGIN_DIR / "_conf_schema.json"
        if not schema_path.exists():
            return
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        field = schema.get("model")
        if not isinstance(field, dict):
            return
        models = self._scan_models()
        options = ["auto"] + models
        labels = ["自动（按语种）"] + [f"{MODEL_CN[m]}（{m}）" if m in MODEL_CN else m for m in models]
        field["options"] = options
        field["labels"] = labels
        field["hint"] = "从本机 RVC 音色中选择；auto 按歌曲语种自动选择。"
        try:
            if json.loads(schema_path.read_text(encoding="utf-8-sig")) == schema:
                return
        except (OSError, ValueError):
            pass
        schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        paths = self._paths()
        models = self._scan_models()
        self._sync_schema_models()
        logger.info(f"[dafeiyu_sing] RVC={paths['rvc_root']} ffmpeg={paths['ffmpeg']} 音色={models}")
        await asyncio.to_thread(self._cleanup)
        self._worker = asyncio.create_task(self._worker_loop())

    async def terminate(self):
        if self._worker:
            self._worker.cancel()

    def _cleanup(self) -> None:
        keep_hours = int(self._get("out_keep_hours", 24) or 24)
        cutoff = time.time() - keep_hours * 3600
        for path in self.out_dir.glob("sing_*.mp3"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ 工具
    @filter.llm_tool()
    async def sing_song(self, event: AstrMessageEvent, song: str = ""):
        """
        用本机 RVC 唱一小段歌，后台合成后把语音发出来。用户让你唱歌、点歌时调用。
        调用只负责接单，不会替你说话；接单结果（排第几、要不要等冷却）会返回给你，
        你按自己的人格决定怎么回，不要复述这段说明。
        Args:
            song(string): 歌名，比如 起风了
        """
        song = (song or "").strip()
        if not song:
            return "用户没说歌名，需要先问唱哪首。"

        umo = getattr(event, "unified_msg_origin", "") or ""
        cooldown = int(self._get("cooldown_seconds", 30) or 0)
        now = time.time()
        if cooldown and now - self._last.get(umo, 0) < cooldown:
            wait = int(cooldown - (now - self._last.get(umo, 0)))
            return f"还在冷却，{wait} 秒后才能再点，先别答应他。"

        self._last[umo] = now
        queued = (not self._queue.empty()) or bool(self._busy_with)
        self._queue.put_nowait((event, song, umo))
        if queued:
            return f"《{song}》已排进后台，前面还有 {self._queue.qsize() - 1} 首，要到时会自动发语音。"
        return f"《{song}》已经接单，后台开始合成，几十秒后会自动把语音发出来。"

    # ------------------------------------------------------------------ 后台
    async def _worker_loop(self) -> None:
        while True:
            try:
                event, song, umo = await self._queue.get()
            except asyncio.CancelledError:
                return
            self._busy_with = song
            try:
                await self._run(event, song, umo)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[dafeiyu_sing] 后台任务出错: {exc}")
            finally:
                self._busy_with = ""
                self._queue.task_done()

    async def _run(self, event: AstrMessageEvent, song: str, umo: str) -> None:
        started = time.time()
        out = self.out_dir / f"sing_{int(started * 1000)}.mp3"
        paths = self._paths()
        if not paths["rvc_root"]:
            await self._send(event, f"《{song}》没唱成，本机没找到 RVC。")
            return
        env = os.environ.copy()
        env["QQBOT_ROOT"] = paths.get("qqbot_root") or str(PLUGIN_DIR)
        env["DAFEIYU_SING_PATHS"] = json.dumps(paths, ensure_ascii=False)
        env.setdefault("OMP_NUM_THREADS", "4")
        env.setdefault("MKL_NUM_THREADS", "4")

        cmd = [
            sys.executable, str(PIPELINE),
            "--song", song,
            "--out", str(out),
            "--max-seconds", str(int(self._get("max_seconds", 30) or 30)),
            "--pitch", str(int(self._get("pitch", 0) or 0)),
            "--pitch-mode", str(self._get("pitch_mode", "auto")),
            "--f0-method", str(self._get("f0_method", "rmvpe")),
            "--index-rate", str(float(self._get("index_rate", 0.55))),
            "--protect", str(float(self._get("protect", 0.45))),
        ]
        model = str(self._get("model", "auto")).strip()
        if model and model != "auto":
            if model not in self._scan_models():
                logger.warning(f"[dafeiyu_sing] 音色 {model} 不存在，回退 auto")
                model = "auto"
            else:
                cmd += ["--model", f"{model}.pth", "--index", f"assets/indices/{model}.index"]

        timeout = int(self._get("timeout_seconds", 900) or 900)
        kwargs = {"env": env, "cwd": str(PLUGIN_DIR),
                  "stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.STDOUT}
        if os.name == "nt":
            kwargs["creationflags"] = BELOW_NORMAL_PRIORITY_CLASS
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
            try:
                raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                await self._send(event, f"《{song}》唱太久了，先放弃。")
                return
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[dafeiyu_sing] 启动失败: {exc}")
            await self._send(event, f"《{song}》的伴奏流程没启动起来。")
            return

        text = (raw or b"").decode("utf-8", "ignore")
        if proc.returncode != 0 or not out.exists():
            logger.warning(f"[dafeiyu_sing] 唱歌失败: {text[-1200:]}")
            await self._send(event, f"《{song}》没唱成，换一首试试。")
            return
        try:
            await event.send(event.chain_result([Record.fromFileSystem(str(out))]))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[dafeiyu_sing] 发送失败: {exc}")
            return
        logger.info(f"[dafeiyu_sing] 唱完《{song}》用时 {time.time() - started:.0f}s -> {out.name}")

    async def _send(self, event: AstrMessageEvent, text: str) -> None:
        """只用于失败提示；正常接单和进度都不发固定话术。"""
        try:
            await event.send(event.plain_result(text))
        except Exception:  # noqa: BLE001
            pass

