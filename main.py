"""大肥鱼唱歌插件：点一首歌，本地分离人声、RVC 换音色、混回伴奏，发一条语音。

对外只做三件事：/唱歌 歌名、/唱歌音色、给模型一个 sing_song 工具。
重活全在 sing_pipeline.py，跑在本机，不联网付费、不要 GPU 也能慢慢跑。
"""

from __future__ import annotations

import asyncio
import json
import os
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


class DafeiyuSingPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = config
        self.out_dir = PLUGIN_DIR / "output"
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._last: dict[str, float] = {}
        self._running_for: str = ""

    # ------------------------------------------------------------------ 配置
    def _get(self, key: str, default=None):
        try:
            value = self.cfg.get(key, default)
        except Exception:  # noqa: BLE001
            value = default
        return default if value is None or value == "" else value

    def _paths(self) -> dict:
        rvc_root = str(self._get("rvc_root", "")).strip()
        root = str(self._get("qqbot_root", "")).strip()
        if not root and rvc_root:
            root = str(Path(rvc_root).resolve().parent)
        ffmpeg = str(self._get("ffmpeg", "")).strip()
        return {
            "rvc_root": rvc_root,
            "rvc_pkg": str(self._get("rvc_pkg", "")).strip(),
            "ffmpeg": ffmpeg,
            "rvc_script": str(RVC_CONVERT),
            "work_dir": str(self.out_dir / "work"),
            "qqbot_root": root,
        }

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._cleanup)

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
        async for result in self._begin(event, song):
            yield result

    @filter.command("唱歌音色", alias={"唱歌模型"})
    async def voice_command(self, event: AstrMessageEvent):
        model = self._get("model", "auto")
        yield event.plain_result(
            f"当前唱歌音色：{model}。可用音色看插件配置里的 model，填 auto 会按歌曲语种自动挑。"
        )

    # ------------------------------------------------------------------ 工具
    @filter.llm_tool()
    async def sing_song(self, event: AstrMessageEvent, song: str = ""):
        """
        用本机 RVC 唱一小段歌并发语音。用户让你唱歌、点歌时调用。
        Args:
            song(string): 歌名，比如 起风了
        """
        song = (song or "").strip()
        if not song:
            return "没说要唱哪首"
        async for _ in self._begin(event, song):
            pass
        return "已经开始唱了，几十秒后会把语音发过来。"

    # ------------------------------------------------------------------ 核心
    async def _begin(self, event: AstrMessageEvent, song: str):
        umo = getattr(event, "unified_msg_origin", "") or ""
        cooldown = int(self._get("cooldown_seconds", 30) or 0)
        now = time.time()
        if cooldown and now - self._last.get(umo, 0) < cooldown:
            wait = int(cooldown - (now - self._last.get(umo, 0)))
            yield event.plain_result(f"刚唱过，歇 {wait} 秒再来。")
            return

        if self._lock.locked():
            yield event.plain_result("上一首还在唱，这首排队，轮到我就发。")
        else:
            yield event.plain_result(f"好，去找《{song}》的伴奏了，大概半分钟到一分钟。")

        self._last[umo] = now
        task = asyncio.create_task(self._run(event, song, umo))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, event: AstrMessageEvent, song: str, umo: str) -> None:
        async with self._lock:
            started = time.time()
            out = self.out_dir / f"sing_{int(started * 1000)}.mp3"
            env = os.environ.copy()
            paths = self._paths()
            env["QQBOT_ROOT"] = paths.get("qqbot_root") or str(PLUGIN_DIR)
            env["DAFEIYU_SING_PATHS"] = json.dumps(paths, ensure_ascii=False)

            cmd = [
                sys.executable, str(PIPELINE),
                "--song", song,
                "--out", str(out),
                "--max-seconds", str(int(self._get("max_seconds", 30) or 30)),
                "--pitch", str(int(self._get("pitch", 0) or 0)),
            ]
            model = str(self._get("model", "auto")).strip()
            if model and model != "auto":
                cmd += ["--model", f"{model}.pth", "--index", f"assets/indices/{model}.index"]

            timeout = int(self._get("timeout_seconds", 900) or 900)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    env=env,
                    cwd=str(PLUGIN_DIR),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.communicate()
                    await self._fail(event, song, f"超过 {timeout} 秒还没唱完")
                    return
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[dafeiyu_sing] 启动失败: {exc}")
                await self._fail(event, song, "伴奏流程没启动起来")
                return

            text = (raw or b"").decode("utf-8", "ignore")
            if proc.returncode != 0 or not out.exists():
                logger.warning(f"[dafeiyu_sing] 唱歌失败: {text[-1200:]}")
                await self._fail(event, song, "这首没唱成，换一首试试")
                return

            try:
                await event.send(event.chain_result([Record.fromFileSystem(str(out))]))
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[dafeiyu_sing] 发送失败: {exc}")
                return
            logger.info(f"[dafeiyu_sing] 唱完《{song}》用时 {time.time() - started:.0f}s -> {out.name}")

    async def _fail(self, event: AstrMessageEvent, song: str, reason: str) -> None:
        try:
            await event.send(event.plain_result(f"《{song}》没唱成：{reason}"))
        except Exception:  # noqa: BLE001
            pass
