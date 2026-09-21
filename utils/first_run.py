# -*- coding: utf-8 -*-
"""
首次启动状态 / 轻量用户偏好

存放位置：
    Windows: %LOCALAPPDATA%/LiveLens/prefs.json
    其它:    ~/.local/share/LiveLens/prefs.json

    与 stream_history.json 同目录 —— 这里**刻意复用**
    :func:`utils.stream_history.default_data_dir`，因为两者的基础目录
    （LOCALAPPDATA）与目录名（LiveLens）完全一致，属于同一类「本机 GUI 状态」。
    这与 stream_history 不复用 license_store.default_data_dir() 的情况不同：
    那里基础目录和目录名都不一样，是有意的独立配置。

    用 LOCALAPPDATA 而不是漫游的 APPDATA：「本机是否还弹引导」是机器本地状态，
    跟随漫游配置同步到别的机器上会让用户在第二台机器上看不到引导。

    与 utils/runtime_paths.py 无关：那套 sys._MEIPASS 逻辑是给打包可执行文件
    定位用的，用它来存用户数据会把文件写进安装目录。

数据结构：
{
    "version": 1,
    "show_guide_on_start": true
}

    字段用**正向**语义（show_guide_on_start 而不是 guide_seen），这样
    「字段缺失 / 文件损坏」自然退回「显示引导」，而不是「静默跳过引导」——
    新用户看不到引导比老用户多看一次引导更糟。

容错读取与静默写入失败是本模块的硬要求：
    读：文件缺失、JSON 损坏、类型不对、版本更高，一律按默认值处理，绝不抛异常。
    写：失败只记日志。一个存不下的偏好不值得让程序起不来（与
    utils/license_store.py 的「写失败即 raise」刻意不同 —— 那里失败意味着
    授权状态不可信，这里失败只意味着下次还会弹一次引导）。
"""

import json
import logging
import os
import threading

from utils.stream_history import default_data_dir

logger = logging.getLogger("DouyinLowLatencyViewer.utils.first_run")

#: 文件名。刻意叫 prefs.json 而不是 first_run.json：后续阶段要再加轻量用户
#: 偏好时应当扩展这个文件，而不是在旁边再造一个 settings.json。
PREFS_FILENAME = "prefs.json"
SCHEMA_VERSION = 1

KEY_SHOW_GUIDE = "show_guide_on_start"

#: 默认值：显示引导。缺失 / 损坏 / 类型不对都落回这里。
_DEFAULT_SHOW_GUIDE = True


def default_prefs_path() -> str:
    """返回 prefs.json 的完整路径（不创建任何东西）。"""
    return os.path.join(default_data_dir(), PREFS_FILENAME)


class FirstRunStore:
    """首次启动状态的本地存储：容错读取 + 合并式原子写入 + 线程安全。

    :param data_dir: 覆盖存储目录（测试注入用）；None 时取
        :func:`utils.stream_history.default_data_dir`。
    """

    def __init__(self, data_dir: str | None = None):
        self.data_dir = data_dir or default_data_dir()
        self.path = os.path.join(self.data_dir, PREFS_FILENAME)
        self._lock = threading.RLock()
        self._load()

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def _load(self) -> None:
        """容错读取。任何异常都收敛为「按默认值处理」。

        刻意不创建目录也不创建文件：读取发生在 MainWindow 构造期，
        首次启动时目录还不存在，只读磁盘环境下 makedirs 会抛异常。
        """
        data: dict = {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
            else:
                logger.warning("prefs.json 不是 JSON 对象，按默认值处理: %s", self.path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            # ValueError 覆盖 json.JSONDecodeError；不删文件，交给用户/后续修复
            logger.warning("prefs.json 读取失败，按默认值处理: %s", exc)
        self._data = data

    def load(self) -> dict:
        """返回当前记录的副本（诊断 / 测试用）。"""
        with self._lock:
            return dict(self._data)

    def should_show_guide(self) -> bool:
        """启动时是否应该显示使用引导。

        只有显式写入的 JSON 布尔 ``false`` 才算「不要显示」：字段缺失、
        类型不对（例如被手工改成字符串 "no"）都按默认的 True 处理。
        刻意用 ``is False`` 而不是真值判断 —— 与
        utils/license_manager.py 的容错判断风格一致。
        """
        with self._lock:
            return self._data.get(KEY_SHOW_GUIDE) is not False

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def set_should_show_guide(self, show: bool) -> None:
        """合并写入 show_guide_on_start 并原子落盘。

        合并而不是整条覆盖：磁盘上已有的未知字段会被保留，后续阶段往这个
        文件里加 key 时不会被这里清掉（utils/license_store.save() 是全量覆盖，
        那里不能照抄）。
        """
        with self._lock:
            record = dict(self._data)
            record["version"] = SCHEMA_VERSION
            record[KEY_SHOW_GUIDE] = bool(show)
            self._write(record)
            self._data = record

    def _write(self, record: dict) -> None:
        """原子写盘。失败只记日志，不抛。

        先写临时文件再 os.replace，保证进程中断时不会留下半个文件。
        """
        tmp_path = self.path + ".tmp"
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        except OSError as exc:
            # 清理残留临时文件，但不向上抛：偏好写不进去不该影响启动
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            logger.warning("prefs.json 保存失败（忽略）: %s", exc)
            return
        logger.info("prefs.json 已保存: %s", self.path)
