# -*- coding: utf-8 -*-
"""Huya 媒体请求头接入生产链测试（Phase 2.4）

覆盖：
    1.  StreamCandidate 默认没有 headers
    2.  Huya candidate 包含正确 headers
    3.  Douyin candidate 不包含 Huya headers
    4.  FFmpegReader headers=None 不改变原命令
    5.  FFmpegReader 能正确生成 User-Agent
    6.  FFmpegReader 能正确生成 Referer / Origin
    7.  Resolver -> HuyaAdapter -> StreamInfo -> selected candidate
    8.  MainWindow -> FFmpegReader headers 传递
    9.  不出现平台特判

    10/11（抖音 resolver、frameReady）由各自测试文件覆盖，不在此重复。

不联网：Huya 走 fixture，Douyin 走桩页面。
不打印 AntiCode / wsSecret / token / cookie / 完整鉴权 URL
（User-Agent / Referer / Origin 是公开常量，允许显示）。
"""

import logging
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ----------------------------------------------------------------------
# 测试环境隔离 —— 必须在导入任何项目模块之前完成
#
# [8] 会构造真实的 MainWindow，而它会创建 LicenseManager / FirstRunStore /
# StreamHistory：三者分别读写 %APPDATA%（授权）与 %LOCALAPPDATA%（偏好、
# 历史、更新状态）。不隔离就会读本机真实的授权与偏好状态；在干净机器上
# 还会把源码树根的 legacy stream_history.json 迁移进真实数据目录。
#
# 两个变量都**直接赋值**，刻意不用 os.environ.setdefault()：Windows 上
# LOCALAPPDATA 本来就已经存在，setdefault 是空操作，隔离会静默失效。
# ----------------------------------------------------------------------
_TEMP_ROOT = tempfile.mkdtemp(prefix="dlv-huyaheaders-")
_TEMP_APPDATA = os.path.join(_TEMP_ROOT, "appdata")
_TEMP_LOCALAPPDATA = os.path.join(_TEMP_ROOT, "localappdata")
os.makedirs(_TEMP_APPDATA, exist_ok=True)
os.makedirs(_TEMP_LOCALAPPDATA, exist_ok=True)

#: 覆盖前的原值；进程退出时精确还原（原先不存在就删掉，不留空壳变量）
_SAVED_ENV = {key: os.environ.get(key) for key in ("APPDATA", "LOCALAPPDATA")}
os.environ["APPDATA"] = _TEMP_APPDATA
os.environ["LOCALAPPDATA"] = _TEMP_LOCALAPPDATA

logging.disable(logging.CRITICAL)

from stream.adapters.douyin import DouyinStream  # noqa: E402
from stream.adapters.huya import (  # noqa: E402
    HUYA_MEDIA_HEADERS, HUYA_ORIGIN, HUYA_USER_AGENT, HuyaAdapter,
    build_candidates, locate_room_data, parse_stream_object,
)
from stream.ffmpeg_reader import FFmpegReader  # noqa: E402
from stream.models import StreamCandidate, StreamInfo  # noqa: E402
from stream.resolver import StreamResolver  # noqa: E402
from stream.video_info import VideoInfo  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "tests", "fixtures", "huya_dank1ng.html")
HUYA_URL = "https://www.huya.com/dank1ng"
DOUYIN_URL = "https://live.douyin.com/123456789"

_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"   {detail}" if detail and not cond else ""))


def under(path, parent) -> bool:
    """path 是否位于 parent 之内（绝对路径比较；不同盘符视为不包含）。"""
    if not parent:
        return False
    try:
        return os.path.commonpath([os.path.abspath(str(path)),
                                   os.path.abspath(str(parent))]) \
            == os.path.abspath(str(parent))
    except ValueError:
        return False


def _walk_files(root):
    """递归列出 root 下的全部文件路径。"""
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            found.append(os.path.join(dirpath, name))
    return found


# ----------------------------------------------------------------------
def t_default_headers():
    print("\n[1] StreamCandidate 默认没有 headers")
    c = StreamCandidate(url="http://x/y.flv", protocol="flv")
    check("默认 http_headers == {}", c.http_headers == {}, str(c.http_headers))
    check("默认不是 None（调用方可直接 .items()）", c.http_headers is not None)
    check("默认是独立字典（不是共享可变对象）",
          StreamCandidate(url="a", protocol="flv").http_headers
          is not StreamCandidate(url="b", protocol="flv").http_headers)
    # 既有构造方式必须继续可用
    check("位置参数构造仍兼容",
          StreamCandidate("u", "flv", "HD1", 800, 1280, 720, 60.0,
                          {"cdn": "AL"}).metadata == {"cdn": "AL"})
    check("metadata 与 http_headers 互不影响",
          StreamCandidate(url="u", protocol="flv",
                          metadata={"cdn": "AL"}).http_headers == {})


# ----------------------------------------------------------------------
def huya_candidates():
    html = open(FIXTURE, encoding="utf-8").read()
    return build_candidates(locate_room_data(parse_stream_object(html))["gameStreamInfoList"])


def t_huya_headers():
    print("\n[2] Huya candidate 包含正确 headers")
    cands = huya_candidates()
    check("候选非空", len(cands) == 3, str(len(cands)))
    for c in cands:
        cdn = c.metadata.get("cdn")
        check(f"{cdn} http_headers 非空", bool(c.http_headers))
        check(f"{cdn} User-Agent 正确",
              c.http_headers.get("User-Agent") == HUYA_USER_AGENT,
              str(c.http_headers.get("User-Agent")))
        check(f"{cdn} Referer 正确",
              c.http_headers.get("Referer") == HUYA_ORIGIN)
        check(f"{cdn} Origin 正确",
              c.http_headers.get("Origin") == HUYA_ORIGIN)
        check(f"{cdn} 字段集合 = {sorted(HUYA_MEDIA_HEADERS)}",
              set(c.http_headers) == set(HUYA_MEDIA_HEADERS))
    check("UA 带 HYSDK( 前缀（决定性变量）",
          HUYA_USER_AGENT.startswith("HYSDK("), HUYA_USER_AGENT)
    check("UA 不含 ffmpeg 默认 Lavf", "Lavf" not in HUYA_USER_AGENT)
    check("三个候选各持独立副本",
          cands[0].http_headers is not cands[1].http_headers)
    cands[0].http_headers["X-Test"] = "1"
    check("改一个候选不影响其他候选",
          "X-Test" not in cands[1].http_headers)
    check("也不污染模块级常量", "X-Test" not in HUYA_MEDIA_HEADERS)


# ----------------------------------------------------------------------
def t_douyin_headers():
    print("\n[3] Douyin 不产出候选 → 不可能混入 Huya headers")
    ds = DouyinStream(DOUYIN_URL)
    info = ds.get_stream_info()
    # RELEASE-02-B5-CLEANUP：抖音适配器不再枚举任何流候选（旧 `_uhd` 转码档
    # 候选整体移除），唯一取流来源是裸流页面直取。因此"抖音候选是否混入
    # 虎牙 headers"这一风险在结构上已不存在 —— 这里断言该结构。
    check("抖音适配器不产出候选（结构上无 headers 污染面）",
          info.candidates == [], str(info.candidates))
    check("抖音 selected 恒为 None", info.selected is None)
    check("DouyinStream 不再有旧页面解析入口",
          not hasattr(ds, "_extract_from_pace_f")
          and not hasattr(ds, "_fetch_room_page"))


# ----------------------------------------------------------------------
#: 测试用的显式运行时路径（不依赖隔离环境里是否装了用户级 FFmpeg 7）
_FAKE_EXE = r"D:\fake\runtime\ffmpeg7\ffmpeg.exe"


def _pump_until(pred, timeout=5.0):
    """驱动 Qt 事件循环直到 pred 成立（后台 worker 经 Signal 回 GUI 需要它）。"""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if app is not None:
            app.processEvents()
        if pred():
            if app is not None:
                app.processEvents()
            return True
        time.sleep(0.005)
    return bool(pred())


def cmd_for(**kw):
    kw.setdefault("exe_path", _FAKE_EXE)
    r = FFmpegReader("http://example.com/a.flv", width=1280, height=720, **kw)
    return r._build_ffmpeg_cmd()


def t_ffmpeg_cmd():
    print("\n[4] FFmpegReader headers=None 不改变原命令")
    base = cmd_for(use_cuda=True)
    check("默认（不传 http_headers）与显式 None 一致",
          cmd_for(use_cuda=True, http_headers=None) == base)
    check("空字典与 None 一致",
          cmd_for(use_cuda=True, http_headers={}) == base)
    check("无 headers 时命令里没有 -user_agent",
          "-user_agent" not in base)
    check("无 headers 时命令里没有 -headers",
          "-headers" not in base)
    check("仍保留低延迟参数（不含 -thread_queue_size：ffmpeg>=7 会拒绝）",
          all(x in base for x in ("-fflags", "nobuffer+flush_packets+genpts", "-flags",
                                  "low_delay", "-probesize", "-analyzeduration"))
          and "-thread_queue_size" not in base,
          str(base[:20]))
    check("use_cuda 行为不变", "-hwaccel" in base and "cuda" in base)
    check("输出侧参数未被破坏",
          base[base.index("-i") + 2:][:4] == ["-an", "-sn", "-avioflags", "direct"])

    print("\n[5] FFmpegReader 能正确生成 User-Agent")
    cmd = cmd_for(use_cuda=True, http_headers=HUYA_MEDIA_HEADERS)
    check("包含 -user_agent", "-user_agent" in cmd)
    check("UA 值完全一致",
          cmd[cmd.index("-user_agent") + 1] == HUYA_USER_AGENT)
    check("UA 不在 -headers 里重复出现",
          "User-Agent" not in cmd[cmd.index("-headers") + 1])

    print("\n[6] FFmpegReader 能正确生成 Referer / Origin")
    blob = cmd[cmd.index("-headers") + 1]
    check("-headers 含 Referer", f"Referer: {HUYA_ORIGIN}\r\n" in blob, repr(blob))
    check("-headers 含 Origin", f"Origin: {HUYA_ORIGIN}\r\n" in blob, repr(blob))
    check("多 header 用 CRLF 分隔", blob.count("\r\n") == 2, repr(blob))

    print("   输入/输出选项位置（必须属于输入流）")
    i_pos = cmd.index("-i")
    check("-user_agent 在 -i 之前", cmd.index("-user_agent") < i_pos)
    check("-headers 在 -i 之前", cmd.index("-headers") < i_pos)
    # 注入块必须完整、连续地贴在 -i 前面，中间不能夹进任何输出侧参数
    injected = cmd[i_pos - 4:i_pos]
    check("注入块紧邻 -i 且结构正确",
          injected[:2] == ["-user_agent", HUYA_USER_AGENT]
          and injected[2] == "-headers"
          and injected[3].startswith("Referer:"),
          str(injected))

    # 大小写不敏感
    lower = cmd_for(use_cuda=True, http_headers={"user-agent": "HYSDK(x)"})
    check("user-agent 小写也走原生选项", "-user_agent" in lower)
    check("小写时不重复放进 -headers", "-headers" not in lower)
    # 只有非 UA header
    only_ref = cmd_for(use_cuda=True, http_headers={"Referer": "https://a/"})
    check("只有 Referer 时不产生 -user_agent", "-user_agent" not in only_ref)
    check("只有 Referer 时产生 -headers", "-headers" in only_ref)


# ----------------------------------------------------------------------
class _StubAdapter(HuyaAdapter):
    def __init__(self):
        self._html = open(FIXTURE, encoding="utf-8").read()

    def _fetch_room_page(self, url):
        return self._html

    def _detect_video_resolution(self, stream_url):
        return VideoInfo(1280, 720, "yuv420p", 60.0, "h264")


def t_resolver_chain():
    print("\n[7] Resolver -> HuyaAdapter -> StreamInfo -> selected candidate")
    r = StreamResolver()
    check("registry 为虎牙 URL 选中 HuyaAdapter",
          isinstance(r.registry.get_adapter(HUYA_URL), HuyaAdapter))
    info = _StubAdapter().resolve(HUYA_URL)
    check("StreamInfo.platform == 'huya'", info.platform == "huya")
    check("selected 是候选对象", info.selected is info.candidates[0])
    check("selected 带 headers", bool(info.selected.http_headers))
    check("selected UA 正确",
          info.selected.http_headers.get("User-Agent") == HUYA_USER_AGENT)
    # StreamInfo 自身不应重复承载请求头，避免出现两套来源
    check("StreamInfo 自身没有 http_headers 字段（唯一来源是 candidate）",
          not hasattr(info, "http_headers"))


# ----------------------------------------------------------------------
def t_mainwindow_wiring():
    print("\n[8] MainWindow -> FFmpegReader headers 传递")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import gui.main_window as mw
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    captured = {}

    class _SpyReader:
        # 签名必须与 _start_playback 的真实构造调用一致（含音频第二路参数与
        # RELEASE-02-B5 加入的 exe_path），否则构造即 TypeError → 全部尝试同步
        # 失败 → 断言全部失真（2026-10-01、2026-10-13 各出现过一次）。
        def __init__(self, url, width=None, height=None, use_cuda=True,
                     http_headers=None, timing=None,
                     audio_enabled=False, audio_pcm_callback=None,
                     audio_sample_rate=None, audio_channels=None,
                     protocol=None, exe_path=None):
            captured.update(url=url, width=width, height=height,
                            use_cuda=use_cuda, http_headers=http_headers,
                            timing=timing, exe_path=exe_path)
            self.width, self.height = width, height
            self.error = None
            self.frames_skipped = 0
            self.first_frame_ms = None

        def start(self):
            captured["started"] = True

        def stop(self):
            pass

        def pipe_backlog_stats(self):
            return {}

        def is_running(self):
            return True

    real = mw.FFmpegReader
    mw.FFmpegReader = _SpyReader
    # RELEASE-02-B5-CLEANUP：会话先过 FFmpeg7 运行时校验。隔离环境里没有
    # 用户级运行时，这里桩成"即时可用"，避免真实子进程探测。
    from utils import ffmpeg7_runtime as _rt
    _saved = (_rt.status, _rt.resolved_exe)
    _Status = _rt.Ffmpeg7Status
    _rt.status = lambda refresh=False: _Status(True, _rt.REASON_OK,
                                               path=_FAKE_EXE)
    _rt.resolved_exe = lambda: _FAKE_EXE
    try:
        win = mw.MainWindow()
        # 压掉"首次进入直播间询问备注"的模态弹窗：record_success 会触发
        # set_remark_callback 注册的回调，默认实现弹模态对话框，offscreen
        # 测试环境会永久阻塞（该功能晚于本测试出现；2026-10-01 修复）。
        win._history.set_remark_callback(lambda url, room_id: None)

        # --- 隔离自证：这个真实 MainWindow 只能落在临时目录里 -------------
        # 只改环境变量是不够的 —— 若某个对象在 import 期就把真实路径缓存了
        # 下来，环境变量改了也会被绕过。所以这里查的是**已经构造出来的对象**。
        import utils.first_run as first_run
        import utils.license_store as license_store
        import utils.stream_history as stream_history

        print("   （临时根: %s）" % _TEMP_ROOT)
        # MainWindow 的历史服务属性是 _history（HistoryService），
        # 仓库落盘路径在内部的 _repo 上（service/repository 两层结构）。
        for label, path, tmproot in (
                ("StreamHistory", win._history._repo._file_path,
                 _TEMP_LOCALAPPDATA),
                ("FirstRunStore", win._first_run_store.path, _TEMP_LOCALAPPDATA),
                ("LicenseStore", win._license_manager.store.path,
                 _TEMP_APPDATA)):
            check(f"[隔离] 已构造的 {label} 落在临时目录",
                  under(path, tmproot), str(path))
        for label, path, real_root in (
                ("StreamHistory", win._history._repo._file_path,
                 _SAVED_ENV.get("LOCALAPPDATA")),
                ("FirstRunStore", win._first_run_store.path,
                 _SAVED_ENV.get("LOCALAPPDATA")),
                ("LicenseStore", win._license_manager.store.path,
                 _SAVED_ENV.get("APPDATA"))):
            if real_root:
                check(f"[隔离] {label} 不在真实用户目录下",
                      not under(path, real_root), str(path))
        check("[隔离] 历史目录解析指向临时 LOCALAPPDATA",
              stream_history.default_data_dir()
              == os.path.join(_TEMP_LOCALAPPDATA, stream_history.APP_DIR_NAME),
              stream_history.default_data_dir())
        check("[隔离] 偏好路径解析指向临时 LOCALAPPDATA",
              under(first_run.default_prefs_path(), _TEMP_LOCALAPPDATA),
              first_run.default_prefs_path())
        check("[隔离] 授权目录解析指向临时 APPDATA",
              license_store.default_data_dir()
              == os.path.join(_TEMP_APPDATA, license_store.APP_DIR_NAME),
              license_store.default_data_dir())

        # 会话中真实写盘一次，确认落点也只在临时目录里
        # （HistoryService 的写入口是 record_success："只有成功进入直播间才写"）
        win._history.record_success("https://www.huya.com/隔离自证", "")
        moved = [p for p in _walk_files(_TEMP_ROOT)
                 if os.path.basename(p) == "stream_history.json"]
        check("[隔离] 运行时写出的 stream_history.json 在临时目录内",
              bool(moved) and all(under(p, _TEMP_ROOT) for p in moved),
              str(moved))

        cand = huya_candidates()[0]
        info = StreamInfo(platform="huya", room_id="dank1ng",
                          candidates=[cand], selected=cand,
                          width=1280, height=720)
        win._handle_connect_success(info)
        # RELEASE-02-B5-CLEANUP：所有平台都先过一次 FFmpeg7 运行时校验（后台
        # 线程），结果经 Qt Signal 回到 GUI 线程后才启动第一个候选。
        _pump_until(lambda: win._bare_worker is None)
        check("FFmpegReader 被调用", captured.get("started"))
        check("http_headers 传到了 FFmpegReader",
              captured.get("http_headers") is not None
              and captured["http_headers"].get("User-Agent") == HUYA_USER_AGENT,
              str(captured.get("http_headers")))
        check("headers 与 candidate 上的一致",
              captured.get("http_headers") == cand.http_headers)
        check("url/宽高未被改变",
              captured.get("url") == cand.url
              and (captured.get("width"), captured.get("height")) == (1280, 720))
        # RELEASE-02-B5-CLEANUP：所有平台都用外部 FFmpeg 7 运行时
        check("运行时路径传给了 FFmpegReader",
              captured.get("exe_path") == _FAKE_EXE,
              repr(captured.get("exe_path")))
        # Phase 3.2：启动分段计时对象由 MainWindow 创建并交给 reader
        check("启动计时对象已传给 FFmpegReader",
              captured.get("timing") is not None,
              repr(captured.get("timing")))
        win._disconnect()

        # 无 headers 的候选（虎牙平台）应传 {} / None —— 不产生任何注入
        captured.clear()
        plain = StreamCandidate(url="http://d/x.flv", protocol="flv")
        info2 = StreamInfo(platform="huya", room_id="1", candidates=[plain],
                           selected=plain, width=1280, height=720)
        win._handle_connect_success(info2)
        _pump_until(lambda: win._bare_worker is None)
        # 语义要求是"传下去后不产生任何注入"。默认值是 {} 而不是 None，
        # FFmpegReader 对两者一视同仁（见 [4]），这里断言真正重要的那件事。
        check("无 headers 的候选不产生注入",
              not captured.get("http_headers"),
              str(captured.get("http_headers")))
        check("无 headers 的候选命令不含 -user_agent/-headers",
              not FFmpegReader("u", width=1, height=1, exe_path=_FAKE_EXE,
                               http_headers=captured.get("http_headers")
                               )._build_ffmpeg_cmd().count("-user_agent"))
        win._disconnect()

        # selected 为 None 不应崩
        captured.clear()
        info3 = StreamInfo(platform="douyin", room_id="1", candidates=[],
                           selected=None, width=1280, height=720)
        try:
            win._handle_connect_success(info3)
            check("selected 为 None 不抛异常", True)
            check("selected 为 None 时 headers 为 None",
                  captured.get("http_headers") is None)
        except Exception as e:
            check("selected 为 None 不抛异常", False, f"{type(e).__name__}: {e}")
    finally:
        mw.FFmpegReader = real
        _rt.status, _rt.resolved_exe = _saved
        app.processEvents()


# ----------------------------------------------------------------------
def t_no_platform_specialcase():
    print("\n[9] 不出现平台特判")
    root = os.path.dirname(os.path.abspath(__file__))

    def read(rel):
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            return f.read()

    gui_src = read("gui/main_window.py")
    reader_src = read("stream/ffmpeg_reader.py")
    for name, src in (("gui/main_window.py", gui_src),
                      ("stream/ffmpeg_reader.py", reader_src)):
        check(f"{name} 不含 'huya'（大小写不敏感）",
              "huya" not in src.lower())
        check(f"{name} 不含 'HYSDK'", "HYSDK" not in src)
        check(f"{name} 不按平台名分支",
              'platform == "huya"' not in src
              and "platform == 'huya'" not in src)

    # 核心层只认 http_headers 这个通用字段。
    # Phase 2.5 起候选由 failover 逐个尝试，请求头必须**跟着当前候选**走
    # （候选 B 的请求头不能取自候选 A），因此字段读取点是 candidate 而不是
    # info.selected；这依然是通用字段，依然不涉及任何平台名/平台分支。
    check("main_window 用的是通用字段 candidate.http_headers",
          "candidate.http_headers" in gui_src)
    check("ffmpeg_reader 用通用参数名 http_headers",
          "http_headers" in reader_src)
    # 只有虎牙适配器写 headers
    for rel in ("stream/adapters/huya.py", "stream/adapters/douyin.py"):
        src = read(rel)
        has = "http_headers" in src
        check(f"{rel} {'设置' if rel.endswith('huya.py') else '不设置'} headers",
              has if rel.endswith("huya.py") else not has)


def main():
    print("=" * 60)
    print("Phase 2.4 Huya 请求头生产修复 测试（不联网）")
    print("=" * 60)
    for fn in (t_default_headers, t_huya_headers, t_douyin_headers,
               t_ffmpeg_cmd, t_resolver_chain, t_mainwindow_wiring,
               t_no_platform_specialcase):
        fn()

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 60)
    print(f"RESULT: {passed}/{total} passed")
    for name, ok, detail in _results:
        if not ok:
            print(f"  FAILED: {name} {detail}")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        # 还原环境变量并清掉临时目录（本进程内其它测试可能接着跑）
        for _key, _value in _SAVED_ENV.items():
            if _value is None:
                os.environ.pop(_key, None)
            else:
                os.environ[_key] = _value
        shutil.rmtree(_TEMP_ROOT, ignore_errors=True)
