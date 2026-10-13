# -*- coding: utf-8 -*-
"""
冻结发行包完整性校验（Phase 1.1 —— 打包完整性）

为什么需要它
    构建闸门（douyin_viewer.spec 的 require_build_inputs）只回答
    「构建机上有没有」。它回答不了「有没有真的进包」：

      * ONNX 模型与 charset.txt 是纯数据，没有任何 import 指向它们，
        datas 写漏了 PyInstaller 只警告不报错；
      * ONNX Runtime 的原生库（onnxruntime.dll / DirectML.dll）由
        hook-onnxruntime 从构建环境收，漏收时构建照样成功；
      * 反过来，「已经退役的 Paddle/CUDA 运行时还在不在包里」也是
        「必需文件都在」发现不了的 —— COLLECT 不清理 dist/，复用旧产物树
        会让 _runtime/ppocr/、paddle/、nvidia/ 一路跟着走。

    实测事故（1.1.1 内部包，Paddle 时代）：构建机上一切正常，发行包里既没有
    模型也没有任何 CUDA DLL —— 客户机上 OCR 一次都跑不起来。OCR 组件缺失由
    ocr.onnx_engine / ocr.ppocr_engine 呈现为
    "内置 OCR 组件缺失或损坏，请重新安装 LiveLens。"

它做什么
    1. 按与 douyin_viewer.spec 相同的规则，算出「应该进包的文件集合」
       （独立于 spec 重新推导一次，因此 spec 漏掉一整类文件时这里能发现）
    2. 在 frozen 产物里逐个核对存在性与字节数（必须等于构建机的源文件）
    3. ONNX 产物（模型 + charset.txt）按 scripts/onnx_model_manifest.json 做
       SHA-256 校验 —— 与 douyin_viewer.spec 的闸门、
       scripts/verify_onnx_model.py 用的是同一份期望值
    4. **反向**核对：runtime/ppocr、paddle/、nvidia/ 一律不允许出现
    5. 报告同一文件重复出现在多个目录（已知的 datas / BINARY 双收）

它不做什么
    不推断「哪些 DLL 可以不装」、不修改任何文件。
    是否可裁剪属于后续阶段，本脚本刻意不参与那个判断。

用法
    python scripts/verify_frozen_bundle.py [onedir 目录]
    # 默认 <项目根>/dist/DouyinLowLatencyViewer

    scripts/velopack_release.py 在 PyInstaller 之后调用 verify_bundle()，
    所以「包里缺文件」会让发布链在 vpk pack 之前就失败。
"""

import hashlib
import json
import os
import sys

__all__ = [
    "VerifyError",
    "expected_payload",
    "audio_payload_failures",
    "verify_bundle",
    "default_dist_dir",
]

#: 随包 ONNX 产物的位置，必须与 ocr.onnx_engine.ONNXModelLocator 一致。
ONNX_SUBDIR = os.path.join("runtime", "onnx")
ONNX_MODEL_NAME = "PP-OCRv6_medium_rec_fp32.onnx"
ONNX_CHARSET_NAME = "charset.txt"

#: ONNX Runtime 的原生库所在的包目录与其必需的 DLL。
#: 它们由 PyInstaller 的 hook-onnxruntime
#: （collect_dynamic_libs("onnxruntime")）从构建环境的 onnxruntime/capi 收进包。
#: PyInstaller 不会因为漏收而中止构建 —— 只会打出一个 import 就失败的包 ——
#: 所以打包后必须显式核对这两个名字（CPUExecutionProvider 编在
#: onnxruntime.dll 里，DirectML 是独立的 DirectML.dll）。
ORT_PACKAGE_DIR = "onnxruntime"
ORT_REQUIRED_DLLS = ("onnxruntime.dll", "DirectML.dll")

#: 包里必须存在的 ONNX Runtime 原生库（相对 _internal）。
#: 它们由 hook-onnxruntime 收集，不是 spec 的 datas —— 漏收时构建照常成功，
#: 只会打出一个 `import onnxruntime` 就崩的包，所以要在**包内**直接核对。
ORT_BUNDLE_DLLS = tuple(
    os.path.join(ORT_PACKAGE_DIR, "capi", name) for name in ORT_REQUIRED_DLLS
)

#: 音频（QtMultimedia）产物：AudioService（v0.1.0 新增）的冻结包依赖。
#: 与 ORT 一样由 hiddenimport + hook 收集、插件由 spec 的 collect_qt_plugins
#: 收集——都不是 datas，漏收时构建照样成功，必须逐项断言"包里真的有"。
#: 项目只 import 了 QtMultimedia（QAudioSink/QMediaDevices），**不依赖**
#: QtMultimediaWidgets —— 刻意不要求它，避免为无用模块增大包体。
QTMULTIMEDIA_PYD = os.path.join("PySide6", "QtMultimedia.pyd")
QTMULTIMEDIA_QT_DLL = os.path.join("PySide6", "Qt6Multimedia.dll")
MULTIMEDIA_PLUGIN_DIR = os.path.join("PySide6", "plugins", "multimedia")
#: Windows 后端插件：至少要有一个在包里，否则 QAudioSink 打不开任何设备。
MULTIMEDIA_BACKEND_PLUGINS = ("windowsmediaplugin.dll", "ffmpegmediaplugin.dll")

#: 退役的 Paddle/CUDA 运行时：这些路径**不允许**出现在包里。
#: 迁移后它们只可能因为两类缺陷出现，两类都必须拦住：
#:   * 复用了旧的 dist/（COLLECT 不清理，残留会一直跟着）；
#:   * 导入图里还有漏网的 `import paddle`，datas 删了也照样把运行时拖进来。
FORBIDDEN_PATHS = (
    (os.path.join("runtime", "ppocr"), "旧 Paddle OCR 模型目录"),
    ("paddle", "Paddle 运行时"),
    ("nvidia", "NVIDIA CUDA 运行时"),
)

#: ffprobe.exe 的 av*/sw* 伴生库（与 douyin_viewer.spec 的 _FFPROBE_DLL_NAMES 一致）。
FFPROBE_DLL_NAMES = (
    "avformat-59.dll",
    "avcodec-59.dll",
    "avdevice-59.dll",
    "avfilter-8.dll",
    "avutil-57.dll",
    "swresample-4.dll",
    "swscale-6.dll",
)



class VerifyError(RuntimeError):
    """冻结产物不完整。消息里逐条列出缺什么。"""




class Entry(object):
    """一个「必须出现在包里的文件」：包内相对路径 + 构建机源文件 + 说明。"""

    __slots__ = ("relpath", "source", "label", "sha256")

    def __init__(self, relpath, source, label, sha256=None):
        self.relpath = relpath
        self.source = source
        self.label = label
        self.sha256 = sha256


def expected_payload(project_root):
    """算出冻结产物里必须存在的文件集合。

    返回 (entries, warnings)。warnings 用于「构建环境本身不完整」这类
    无法判定为包缺陷的情况（例如这台机器没装 GPU 依赖）；此时仍然会把
    能判定的部分全部检查掉。
    """
    entries = []
    warnings = []

    # --- 1. ONNX OCR 产物（期望值来自进 Git 的 manifest） -----------------
    # 与 douyin_viewer.spec 的 SHA-256 闸门读同一份 manifest，因此「构建机上的
    # 产物 == manifest 记录的产物」由同一处期望值判定。
    # charset.txt 与模型同等重要：它的项数或顺序错了不会报错，只会把每个字符
    # 索引静默解错。
    manifest_path = os.path.join(project_root, "scripts", "onnx_model_manifest.json")
    manifest_files = {}
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as handle:
                manifest_files = json.load(handle).get("files", {})
        except Exception as exc:  # noqa: BLE001
            warnings.append("manifest 无法读取：%s（%s）" % (manifest_path, exc))
    else:
        warnings.append("manifest 不存在，ONNX 产物只能按字节数核对：%s" % manifest_path)

    onnx_dir = os.path.join(project_root, ONNX_SUBDIR)
    if manifest_files:
        for name in sorted(manifest_files):
            entries.append(Entry(
                os.path.join(ONNX_SUBDIR, name),
                os.path.join(onnx_dir, name),
                "ONNX OCR 产物",
                sha256=manifest_files[name].get("sha256"),
            ))
    else:
        for name in (ONNX_MODEL_NAME, ONNX_CHARSET_NAME):
            entries.append(Entry(
                os.path.join(ONNX_SUBDIR, name),
                os.path.join(onnx_dir, name),
                "ONNX OCR 产物",
            ))

    # --- 2. ffprobe 及其伴生闭包 -------------------------------------------
    # RELEASE-02-B5-CLEANUP：runtime/ffmpeg/ 下只剩 ffprobe.exe 与 av*/sw* 闭包
    # （分辨率探测回退用）。播放运行时是外部 FFmpeg 7，由 utils/ffmpeg7_runtime
    # 定位，**不随包** —— 这里不应再期望 ffmpeg.exe。
    ffprobe_dir = os.path.join(project_root, "runtime", "ffmpeg")
    if os.path.isdir(ffprobe_dir):
        for name in sorted(os.listdir(ffprobe_dir)):
            source = os.path.join(ffprobe_dir, name)
            if not os.path.isfile(source):
                continue
            if not (name.lower().endswith(".dll") or name.lower().endswith(".exe")):
                continue
            entries.append(Entry(os.path.join("runtime", "ffmpeg", name), source,
                                 "ffprobe 运行时"))
    else:
        warnings.append("构建环境没有 %s，跳过 ffprobe 闭包校验" % ffprobe_dir)

    # --- 2b. FFmpeg 7 播放运行时（RELEASE-02 2.2.0，随包分发）---------------
    # RELEASE-02 起 FFmpeg 7 随安装包分发，让流媒体开箱即用。
    # utils/ffmpeg7_runtime.candidate_paths() 在 <MEIPASS>/runtime/ffmpeg7/ 与
    # <exe_dir>/runtime/ffmpeg7/ 探测 ffmpeg.exe。与 ffprobe 闭包同理：该目录
    # 下的每个 .exe/.dll 都必须真的进包（漏一个，ffmpeg.exe 起不来，播放直接失败）。
    #
    # 注意：该目录**不含** GPL v3 许可文本 —— 这是 RELEASE-02 交付负责人的书面
    # 风险承担下的刻意决定，逐字记录在 RELEASE-02-2.2.0_FINAL_DELIVERY_REPORT.md。
    # 本脚本只核对二进制是否进包，不做许可合规断言。
    ffmpeg7_dir = os.path.join(project_root, "runtime", "ffmpeg7")
    if os.path.isdir(ffmpeg7_dir):
        for name in sorted(os.listdir(ffmpeg7_dir)):
            source = os.path.join(ffmpeg7_dir, name)
            if not os.path.isfile(source):
                continue
            if not (name.lower().endswith(".dll") or name.lower().endswith(".exe")):
                continue
            entries.append(Entry(os.path.join("runtime", "ffmpeg7", name), source,
                                 "FFmpeg 7 播放运行时"))
    else:
        warnings.append("构建环境没有 %s，跳过 FFmpeg 7 运行时校验" % ffmpeg7_dir)

    # ONNX Runtime 的原生库不在这里推导：它们由 PyInstaller 的 hook-onnxruntime
    # 从**构建环境的 site-packages** 收进包，与源码树无关，所以「构建机上的源
    # 文件」这种对照没有意义。它们的核对放在 verify_bundle() 里直接查包内路径
    # （见 ORT_BUNDLE_DLLS）—— 那才是真正要回答的问题：包里有没有。

    # --- 7. 其它随包资产 ---------------------------------------------------
    for relpath, label in (
        (os.path.join("license_public_key.pem",), "许可证公钥"),
        (os.path.join("config.yaml",), "配置文件"),
        (os.path.join("susi_helper", "target", "release", "susi_helper.exe"),
         "susi_helper"),
    ):
        entries.append(Entry(relpath, os.path.join(project_root, relpath), label))

    # --- 8. 音频（QtMultimedia）产物 ---------------------------------------
    # 与 ORT 同理：由 hiddenimport + hook + spec 的 collect_qt_plugins 收集，
    # 不是 datas —— 漏收时构建照样成功。构建环境里存在的才进期望集（字节数
    # 逐一对照）；构建环境本身缺失则打 WARN，由 audio_payload_failures() 的
    # "包内必须存在" 断言兜底（它无条件核对 pyd / Qt DLL / 后端插件）。
    try:
        import importlib.util as _ilu
        _pyside_spec = _ilu.find_spec("PySide6")
        if _pyside_spec is None or not _pyside_spec.submodule_search_locations:
            raise ImportError("PySide6 not importable in build interpreter")
        pyside_dir = list(_pyside_spec.submodule_search_locations)[0]

        for name in ("QtMultimedia.pyd", "Qt6Multimedia.dll"):
            source = os.path.join(pyside_dir, name)
            if os.path.isfile(source):
                entries.append(Entry(os.path.join("PySide6", name), source,
                                     "QtMultimedia 产物"))
            else:
                warnings.append("构建环境缺少 %s（冻结包音频将不可用）" % source)

        plugin_src_dir = os.path.join(pyside_dir, "plugins", "multimedia")
        if os.path.isdir(plugin_src_dir):
            for name in sorted(os.listdir(plugin_src_dir)):
                if name.lower().endswith(".dll"):
                    entries.append(Entry(
                        os.path.join(MULTIMEDIA_PLUGIN_DIR, name),
                        os.path.join(plugin_src_dir, name),
                        "Qt Multimedia 插件"))
        else:
            warnings.append("构建环境没有 %s，跳过多媒体插件字节数校验"
                            % plugin_src_dir)
    except Exception as exc:  # noqa: BLE001 - 构建环境残缺降级为警告
        warnings.append("无法定位构建环境 PySide6，跳过音频产物字节数校验：%s" % exc)

    return entries, warnings


def audio_payload_failures(internal_dir):
    """QtMultimedia 音频产物核对（相对 _internal）。返回失败项列表。

    无条件核对（不依赖构建环境推导）：
        1. PySide6/QtMultimedia.pyd        （hiddenimport 收集的 Python 绑定）
        2. PySide6/Qt6Multimedia.dll       （Qt 多媒体运行库）
        3. plugins/multimedia/ 至少一个 Windows 后端插件
    """
    failures = []
    for relpath in (QTMULTIMEDIA_PYD, QTMULTIMEDIA_QT_DLL):
        if not os.path.isfile(os.path.join(internal_dir, relpath)):
            failures.append("缺失 QtMultimedia 产物: _internal/%s" % relpath)
    plugins_present = [
        name for name in MULTIMEDIA_BACKEND_PLUGINS
        if os.path.isfile(os.path.join(internal_dir, MULTIMEDIA_PLUGIN_DIR, name))
    ]
    if not plugins_present:
        failures.append(
            "缺失 Qt Multimedia Windows 后端插件: _internal/%s/（需要 %s 之一）"
            % (MULTIMEDIA_PLUGIN_DIR, " 或 ".join(MULTIMEDIA_BACKEND_PLUGINS)))
    return failures


# ----------------------------------------------------------------------
# 产物侧：核对
# ----------------------------------------------------------------------
def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_onedir(path):
    """接受 onedir 目录，也接受它的父目录（dt/dist 两种给法都行）。"""
    if os.path.isdir(os.path.join(path, "_internal")):
        return path
    nested = os.path.join(path, "DouyinLowLatencyViewer")
    if os.path.isdir(os.path.join(nested, "_internal")):
        return nested
    return path


def duplicate_report(internal_dir, limit=15):
    """同一 (文件名, 字节数) 出现在多个目录 —— 已知的 datas/BINARY 双收。"""
    seen = {}
    for root, _dirs, files in os.walk(internal_dir):
        for name in files:
            full = os.path.join(root, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            seen.setdefault((name.lower(), size), []).append(full)

    groups = []
    for (name, size), paths in seen.items():
        if len(paths) > 1:
            groups.append((size * (len(paths) - 1), name, size, paths))
    groups.sort(reverse=True)
    return groups[:limit]


def verify_bundle(dist_dir, project_root=None, stream=sys.stdout):
    """核对冻结产物，返回失败项列表（空列表表示通过）。

    失败项同时打印到 stream；调用方负责决定是否中止（发布链会中止）。
    """
    project_root = project_root or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    onedir = resolve_onedir(dist_dir)
    internal_dir = os.path.join(onedir, "_internal")

    failures = []

    def fail(message):
        failures.append(message)
        stream.write("  [FAIL] %s\n" % message)

    def ok(message):
        stream.write("  [PASS] %s\n" % message)

    stream.write("冻结产物完整性校验\n")
    stream.write("  产物目录: %s\n" % onedir)

    if not os.path.isdir(internal_dir):
        fail("产物目录不存在或不完整（找不到 %s）" % internal_dir)
        return failures

    entries, warnings = expected_payload(project_root)
    for warning in warnings:
        stream.write("  [WARN] %s\n" % warning)

    # 同一目标路径只核对一次（同一文件可能由两处推导）。
    by_relpath = {}
    for entry in entries:
        by_relpath.setdefault(os.path.normcase(entry.relpath), entry)

    missing, wrong_size, wrong_hash, present = [], [], [], []
    for entry in by_relpath.values():
        target = os.path.join(internal_dir, entry.relpath)
        if not os.path.isfile(target):
            missing.append(entry)
            continue
        # 0 字节一律当作"没打进包"—— 但只对**带期望摘要**的文件成立（模型权重：
        # 0 字节占位是历史上真实发生过的假供应）。dist-info 里的 REQUESTED 这类
        # 标记文件本来就是空的，对它套用这条规则会误报。
        if entry.sha256 and os.path.getsize(target) == 0:
            missing.append(entry)
            continue
        try:
            source_size = os.path.getsize(entry.source)
        except OSError:
            source_size = None
        if source_size is not None and os.path.getsize(target) != source_size:
            wrong_size.append(entry)
            continue
        if entry.sha256:
            if sha256_of(target) != entry.sha256:
                wrong_hash.append(entry)
                continue
        present.append(entry)

    for entry in missing:
        fail("缺失 %s: %s" % (entry.label, entry.relpath))
    for entry in wrong_size:
        fail("字节数不符 %s: %s（包内 %d，构建机 %d）"
             % (entry.label, entry.relpath,
                os.path.getsize(os.path.join(internal_dir, entry.relpath)),
                os.path.getsize(entry.source)))
    for entry in wrong_hash:
        fail("SHA-256 不符 %s: %s" % (entry.label, entry.relpath))

    if not failures:
        ok("必需文件全部到位（%d 项，含 ONNX 产物 SHA-256）" % len(present))

    # --- 已退役的 Paddle/CUDA 运行时必须**不存在** -------------------------
    # 「必需文件都在」发现不了这一类缺陷，而它正好是最可能发生的：COLLECT
    # 不清理 dist/，复用旧产物树会让 _internal/runtime/ppocr/、paddle/、
    # nvidia/ 一直跟着走；导入图里漏网的 `import paddle` 也会把它们拖回来。
    # 两者都会让包体悄悄涨回 2.5 GB。
    stale_found = False
    for relpath, label in FORBIDDEN_PATHS:
        target = os.path.join(internal_dir, relpath)
        if os.path.exists(target):
            stale_found = True
            fail("包里出现了已退役的 %s: _internal/%s" % (label, relpath))
    if not stale_found:
        ok("已退役的 Paddle/CUDA 运行时均不在包内（%d 项）" % len(FORBIDDEN_PATHS))

    # --- ONNX Runtime 原生库必须真的在包里 ---------------------------------
    # 它们由 hook-onnxruntime 从构建环境收集，不是 spec 的 datas：漏收时构建
    # 不会失败，只会打出一个 import onnxruntime 就崩的包。
    ort_missing = [
        rel for rel in ORT_BUNDLE_DLLS
        if not os.path.isfile(os.path.join(internal_dir, rel))
    ]
    for rel in ort_missing:
        fail("缺失 ONNX Runtime 原生库: _internal/%s" % rel)
    if not ort_missing:
        ok("ONNX Runtime 原生库到位（%d 项，含 DirectML）" % len(ORT_BUNDLE_DLLS))

    # --- QtMultimedia 音频产物必须真的在包里 -------------------------------
    # 与 ORT 同理：由 hiddenimport + hook + collect_qt_plugins 收集，漏收时
    # 构建不会失败，只会打出一个没有声音的包（v0.1.0 音频功能整段不可用）。
    audio_failures = audio_payload_failures(internal_dir)
    for message in audio_failures:
        fail(message)
    if not audio_failures:
        ok("QtMultimedia 音频产物到位（pyd + Qt6Multimedia.dll + Windows 后端插件）")

    # 已知的 datas/BINARY 双收：只报告，不作为失败项（不影响可运行性）。
    duplicates = duplicate_report(internal_dir)
    if duplicates:
        waste = sum(item[0] for item in duplicates)
        stream.write("  [INFO] 重复文件 %d 组，约 %.1f MiB（datas 与 BINARY 各收一份）\n"
                     % (len(duplicates), waste / 1048576.0))
        for _waste, name, size, paths in duplicates[:5]:
            stream.write("         %-40s x%d (%s)\n" % (name, len(paths), paths[0]))

    return failures


def default_dist_dir(project_root=None):
    project_root = project_root or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(project_root, "dist", "DouyinLowLatencyViewer")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dist_dir = argv[0] if argv else default_dist_dir()
    failures = verify_bundle(dist_dir)
    print()
    if failures:
        print("失败 %d 项：包不完整，不能发布。" % len(failures))
        return 1
    print("通过：冻结产物包含全部必需运行时文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
