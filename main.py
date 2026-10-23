"""大肥鱼唱歌插件：点一首歌，本地分离人声、RVC 换音色、混回伴奏，发一条语音。

唱歌全程在后台跑，前台聊天不受影响：
  - 命令和模型工具都只负责“接单”，立刻回一句，然后扔给后台任务
  - 后台一次只跑一首，后面的排队并明确告诉用户排上了
  - 子进程降优先级，避免分离/换声时把聊天的 CPU 抢光
  - RVC 目录自动找，设置页只留跟唱歌有关的选项
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

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        paths = self._paths()
        logger.info(f"[dafeiyu_sing] RVC={paths['rvc_root']} ffmpeg={paths['ffmpeg']}")
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

    # ------------------------------------------------------------------ 命令
    @filter.command("唱歌", alias={"唱一首", "点唱"})
    async def sing_command(self, event: AstrMessageEvent):
        song = event.message_str.partition(" ")[2].strip()
        if not song:
            yield event.plain_result("要唱哪首？比如：唱歌 起风了")
            return
        text = self._accept(event, song)
        yield event.plain_result(text)

    @filter.command("唱歌音色", alias={"唱歌模型"})
    async def voice_command(self, event: AstrMessageEvent):
        model = self._get("model", "auto")
        yield event.plain_result(f"当前唱歌音色：{model}（填 auto 会按歌曲语种自动挑）")

    @filter.command("唱歌状态", alias={"唱歌队列"})
    async def status_command(self, event: AstrMessageEvent):
        if self._busy_with:
            yield event.plain_result(f"正在唱《{self._busy_with}》，排队的还有 {self._queue.qsize()} 首。")
        else:
            yield event.plain_result("现在没在唱，随时可以点。")

    # ------------------------------------------------------------------ 工具
    @filter.llm_tool()
    async def sing_song(self, event: AstrMessageEvent, song: str = ""):
        """
        用本机 RVC 唱一小段歌，后台合成后发语音。用户让你唱歌、点歌时调用；调用后立刻用文字回一句，不要等。
        Args:
            song(string): 歌名，比如 起风了
        """
        song = (song or "").strip()
        if not song:
            return "没说要唱哪首"
        text = self._accept(event, song)
        try:
            await event.send(event.plain_result(text))
        except Exception:  # noqa: BLE001
            pass
        return text

    # ------------------------------------------------------------------ 接单
    def _accept(self, event: AstrMessageEvent, song: str) -> str:
        umo = getattr(event, "unified_msg_origin", "") or ""
        cooldown = int(self._get("cooldown_seconds", 30) or 0)
        now = time.time()
        if cooldown and now - self._last.get(umo, 0) < cooldown:
            wait = int(cooldown - (now - self._last.get(umo, 0)))
            return f"刚唱过，歇 {wait} 秒再来。"
        self._last[umo] = now

        queued = (not self._queue.empty()) or bool(self._busy_with)
        self._queue.put_nowait((event, song, umo))
        if queued:
            return f"好，《{song}》排上了，前面还有 {self._queue.qsize() - 1} 首，唱完就发。"
        return f"好，去准备《{song}》了，大概半分钟到一分钟，前台照常聊。"

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
            await self._send(event, "没找到 RVC 目录，插件没法唱。")
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
            await self._send(event, "伴奏流程没启动起来。")
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
        try:
            await event.send(event.plain_result(text))
        except Exception:  # noqa: BLE001
            pass
