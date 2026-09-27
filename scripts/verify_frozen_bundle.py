# -*- coding: utf-8 -*-
"""
冻结发行包完整性校验（Phase 1.1 —— 打包完整性）

为什么需要它
    构建闸门（douyin_viewer.spec 的 require_build_inputs）只回答
    「构建机上有没有」。它回答不了「有没有真的进包」：

      * PP-OCRv6 模型是纯数据，没有任何 import 指向它，datas 写漏了
        PyInstaller 只警告不报错；
      * paddle/libs 下运行期用 LoadLibrary 打开的 DLL 不在 PE 导入表里，
        PyInstaller 的依赖分析完全看不见；
      * nvidia-* wheel 的 CUDA 库靠 os.add_dll_directory 拼路径找，
        既不是 Python 模块也不是静态依赖。

    实测事故（1.1.1 内部包）：构建机上一切正常，发行包里既没有
    PP-OCRv6 模型也没有任何 CUDA DLL —— 客户机上 OCR 一次都跑不起来。
    模型缺失的后果由 ocr.ppocr_engine 呈现为
    "内置 OCR 组件缺失或损坏，请重新安装 LiveLens。"

它做什么
    1. 按与 douyin_viewer.spec 相同的规则，算出「应该进包的文件集合」
       （独立于 spec 重新推导一次，因此 spec 漏掉一整类文件时这里能发现）
    2. 在 frozen 产物里逐个核对存在性与字节数（必须等于构建机的源文件）
    3. PP-OCRv6 模型按 scripts/ppocr_model_manifest.json 做 SHA-256 校验
       —— 与 scripts/verify_ppocr_model.py 用的是同一份期望值
    4. 报告同一文件重复出现在多个目录（已知的 datas / BINARY 双收）

它不做什么
    不做 CUDA 裁剪判断、不推断「哪些 DLL 可以不装」、不修改任何文件。
    清单里的「必需」来自 Phase 1 审计的**实测加载证据**；是否可裁剪属于
    后续阶段，本脚本刻意不参与那个判断。

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
    "verify_bundle",
    "default_dist_dir",
]

#: 随包模型的位置，必须与 ocr.ppocr_engine.PPOCRModelLocator 一致。
MODEL_SUBDIR = os.path.join("runtime", "ppocr")
MODEL_DIR_NAME = "PP-OCRv6_medium_rec_infer"
MODEL_PARAMS = "inference.pdiparams"

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

#: paddle/libs 的目标目录（paddle 自己 glob 这个目录）。
PADDLE_LIBS_DEST = os.path.join("paddle", "libs")

#: PyInstaller 的 PE 依赖分析**已经能收到**的 paddle/libs DLL：
#: libpaddle.pyd 静态导入 phi.dll，phi.dll 静态导入
#: common.dll / mkldnn.dll / libiomp5md.dll。它们由 BINARY TOC 落到
#: paddle/libs/，同样必须存在 —— 但不由 datas 声明（重复目标名会让
#: COLLECT 收到重复条目）。
PADDLE_LIBS_STATIC = ("phi.dll", "common.dll", "mkldnn.dll", "libiomp5md.dll")

#: mkldnn.dll 静态导入、但不在 paddle/libs 里的 MSVC OpenMP 运行时。
MSVC_OPENMP_DLL = "vcomp140.dll"

#: PaddleX 用 importlib.metadata.version() 判断「装了没有」的运行时依赖
#: （paddlex/utils/deps.py:get_dep_version）。冻结包里没有 dist-info 时，
#: version() 抛 PackageNotFoundError，paddlex 判定依赖缺失并拒绝构造识别器
#: —— 而它的模块其实就在包里。实测报错：
#:   `TextRecRunnerPredictor` is not ready for use ... not available: python-bidi
#: 集合来自把 is_dep_available 打桩后跑一遍真实生产路径的记录；paddlepaddle
#: 也在查询序列里，但它走 find_spec 分支，不需要元数据。
PADDLE_METADATA_DISTS = ("python-bidi", "pypdfium2", "opencv-contrib-python")


class VerifyError(RuntimeError):
    """冻结产物不完整。消息里逐条列出缺什么。"""


# ----------------------------------------------------------------------
# 构建环境侧：算出「应该进包的文件」
# ----------------------------------------------------------------------
def build_env_package_dirs():
    """构建解释器里可能承载第三方包的目录（去重保序）。

    与 douyin_viewer.spec 用的是同一套定位方式：sysconfig 的 purelib/platlib
    优先，再用 importlib 的包定位兜底（namespace package 也能命中）。
    """
    dirs = []

    def _add(path):
        if path and os.path.isdir(path) and path not in dirs:
            dirs.append(path)

    try:
        import sysconfig
        paths = sysconfig.get_paths()
        for key in ("purelib", "platlib"):
            _add(paths.get(key))
    except Exception:  # noqa: BLE001 - 定位失败交给下面的 importlib 兜底
        pass

    import importlib.util
    for module in ("paddle", "nvidia"):
        try:
            found = importlib.util.find_spec(module)
        except Exception:  # noqa: BLE001
            found = None
        for location in list(getattr(found, "submodule_search_locations", None) or []):
            _add(os.path.dirname(location))
    return dirs


def _find_under_build_env(leaf, is_file=False):
    """在构建环境的包目录下找 leaf，返回第一个存在的绝对路径。"""
    probe = os.path.isfile if is_file else os.path.isdir
    for base in build_env_package_dirs():
        candidate = os.path.join(base, leaf)
        if probe(candidate):
            return candidate
    return None


def nvidia_dlls(nvidia_dir):
    """nvidia-* wheel 里的全部 DLL，保持 wheel 内的原相对路径。

    目标路径是硬要求：ocr.ppocr_engine._ensure_cuda_dlls() 在
    site.getsitepackages() 下 glob "nvidia/cu13/bin/*"，而冻结环境里
    site.getsitepackages() 的根**就是** sys._MEIPASS（已用冻结探针实测），
    所以文件必须落在 _internal/nvidia/<原相对路径>。
    """
    if not nvidia_dir:
        return []
    parent = os.path.dirname(nvidia_dir)
    found = []
    for root, _dirs, files in os.walk(nvidia_dir):
        dest = os.path.relpath(root, parent)
        for name in sorted(files):
            if name.lower().endswith(".dll"):
                found.append((os.path.join(root, name), dest))
    return sorted(found)


def paddle_libs(paddle_libs_dir):
    """paddle/libs 下所有运行期 LoadLibrary 打开的 DLL（含 PE 已收的那几个）。

    返回全部，交给调用方按 PADDLE_LIBS_STATIC 分流：spec 只为「PE 看不见的
    那些」写 datas，校验则要求**全部**都在包里。
    """
    if not paddle_libs_dir:
        return []
    return [
        (os.path.join(paddle_libs_dir, name), PADDLE_LIBS_DEST)
        for name in sorted(os.listdir(paddle_libs_dir))
        if name.lower().endswith(".dll")
    ]


def find_dist_metadata(dist_names):
    """按发行名定位 dist-info 目录。

    发行名与目录名不是一回事（python-bidi -> python_bidi-0.6.11.dist-info），
    所以读每个 METADATA 的 Name 字段配对，不拼字符串。
    返回 (目录列表, 已找到的规范化发行名集合)。
    """
    wanted = {name.lower().replace("_", "-") for name in dist_names}
    found = {}
    for base in build_env_package_dirs():
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base)):
            if not entry.endswith(".dist-info"):
                continue
            meta = os.path.join(base, entry, "METADATA")
            if not os.path.isfile(meta):
                continue
            try:
                with open(meta, "r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        if line.startswith("Name:"):
                            key = line.split(":", 1)[1].strip().lower().replace("_", "-")
                            if key in wanted:
                                found.setdefault(key, os.path.join(base, entry))
                            break
            except OSError:
                continue
    return [found[name] for name in sorted(found)], set(found)


def find_msvc_openmp():
    """定位 vcomp140.dll（mkldnn.dll 的静态依赖，MSVC OpenMP 运行时）。

    优先用构建环境里已有的副本（随 PySide6 / shiboken6 一起装的，
    与 app 同源）；系统副本只是兜底 —— 干净 Windows 机器上没有它。
    """
    for leaf in (os.path.join("PySide6", MSVC_OPENMP_DLL),
                 os.path.join("shiboken6", MSVC_OPENMP_DLL)):
        found = _find_under_build_env(leaf, is_file=True)
        if found:
            return found
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    fallback = os.path.join(system_root, "System32", MSVC_OPENMP_DLL)
    return fallback if os.path.isfile(fallback) else None


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

    # --- 1. PP-OCRv6 模型（期望值来自进 Git 的 manifest） -----------------
    model_dir = os.path.join(project_root, MODEL_SUBDIR, MODEL_DIR_NAME)
    manifest_path = os.path.join(project_root, "scripts", "ppocr_model_manifest.json")
    manifest_files = {}
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as handle:
                manifest_files = json.load(handle).get("files", {})
        except Exception as exc:  # noqa: BLE001
            warnings.append("manifest 无法读取：%s（%s）" % (manifest_path, exc))
    else:
        warnings.append("manifest 不存在，模型只能按字节数核对：%s" % manifest_path)

    if manifest_files:
        for name in sorted(manifest_files):
            entries.append(Entry(
                os.path.join(MODEL_SUBDIR, MODEL_DIR_NAME, name),
                os.path.join(model_dir, name),
                "PP-OCRv6 模型",
                sha256=manifest_files[name].get("sha256"),
            ))
    else:
        for name in (MODEL_PARAMS, "inference.json", "inference.yml"):
            entries.append(Entry(
                os.path.join(MODEL_SUBDIR, MODEL_DIR_NAME, name),
                os.path.join(model_dir, name),
                "PP-OCRv6 模型",
            ))

    # --- 2. FFmpeg / ffprobe 及其伴生闭包 ---------------------------------
    ffmpeg_dir = os.path.join(project_root, "runtime", "ffmpeg")
    if os.path.isdir(ffmpeg_dir):
        for name in sorted(os.listdir(ffmpeg_dir)):
            source = os.path.join(ffmpeg_dir, name)
            if not os.path.isfile(source):
                continue
            if not (name.lower().endswith(".dll") or name.lower().endswith(".exe")):
                continue
            entries.append(Entry(os.path.join("runtime", "ffmpeg", name), source,
                                 "FFmpeg 运行时"))
    else:
        warnings.append("构建环境没有 %s，跳过 FFmpeg 闭包校验" % ffmpeg_dir)

    # --- 3. Paddle 运行时 --------------------------------------------------
    libs_dir = _find_under_build_env(os.path.join("paddle", "libs"))
    if libs_dir:
        for source, dest in paddle_libs(libs_dir):
            entries.append(Entry(os.path.join(dest, os.path.basename(source)),
                                 source, "Paddle 运行时"))
        libpaddle = os.path.join(os.path.dirname(libs_dir), "base", "libpaddle.pyd")
        if os.path.isfile(libpaddle):
            entries.append(Entry(os.path.join("paddle", "base", "libpaddle.pyd"),
                                 libpaddle, "Paddle 运行时"))
    else:
        warnings.append("构建环境没有 paddle/libs，跳过 Paddle 运行时校验")

    # --- 4. CUDA 闭包 ------------------------------------------------------
    nvidia_dir = _find_under_build_env("nvidia")
    if nvidia_dir:
        for source, dest in nvidia_dlls(nvidia_dir):
            entries.append(Entry(os.path.join(dest, os.path.basename(source)),
                                 source, "CUDA 运行时"))
    else:
        warnings.append("构建环境没有 nvidia(轮子)，跳过 CUDA 闭包校验")

    # --- 5. vcomp140（mkldnn.dll 的静态依赖） ------------------------------
    openmp = find_msvc_openmp()
    if openmp:
        entries.append(Entry(MSVC_OPENMP_DLL, openmp, "MSVC OpenMP 运行时"))
    else:
        warnings.append("构建环境找不到 %s" % MSVC_OPENMP_DLL)

    # --- 6. PaddleX 依赖检查用的发行元数据 ---------------------------------
    meta_dirs, meta_found = find_dist_metadata(PADDLE_METADATA_DISTS)
    for directory in meta_dirs:
        dist_info = os.path.basename(directory)
        for root, _dirs, files in os.walk(directory):
            rel = os.path.relpath(root, directory)
            for name in sorted(files):
                inside = name if rel == os.curdir else os.path.join(rel, name)
                entries.append(Entry(os.path.join(dist_info, inside),
                                     os.path.join(root, name), "PaddleX 依赖元数据"))
    absent = [n for n in PADDLE_METADATA_DISTS
              if n.lower().replace("_", "-") not in meta_found]
    if absent:
        warnings.append("构建环境缺少 dist-info：%s" % ", ".join(absent))

    # --- 7. 其它随包资产 ---------------------------------------------------
    for relpath, label in (
        (os.path.join("license_public_key.pem",), "许可证公钥"),
        (os.path.join("config.yaml",), "配置文件"),
        (os.path.join("susi_helper", "target", "release", "susi_helper.exe"),
         "susi_helper"),
    ):
        entries.append(Entry(relpath, os.path.join(project_root, relpath), label))

    return entries, warnings


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

    # 同一目标路径只核对一次（paddle/libs 由两处推导，可能重名）。
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
        ok("必需文件全部到位（%d 项，含模型 SHA-256）" % len(present))

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
