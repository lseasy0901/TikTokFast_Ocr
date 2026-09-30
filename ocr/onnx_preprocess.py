# -*- coding: utf-8 -*-
"""PP-OCRv6 rec 的预处理 / CTC 解码镜像 —— ONNX 后端的输入输出契约。

【逐字复制自 Step 0 已验证的 `_onnx_export_lab/preprocess.py`，请勿「优化」。】
那份实现在 107 张真实 ROI 上取得了与生产 Paddle GPU **107/107 逐字一致**的结果；
任何对 resize 算法 / 归一化 / padding / 去重逻辑的改动都会让该结论失效。

与 `ocr/preprocess.py` 的区别（两者服务不同后端，不要混用）：
  `ocr/preprocess.py`        Tesseract 用：灰度 + 二值化 + 放大（`preprocess_for_ocr`）
  本文件                      PP-OCRv6 rec 用：BGR 直通 + resize 到高 48 + 归一化 + CHW + 右零填充
PP-OCRv6 生产引擎（`ppocr_engine.PPOCREngine`）把预处理放在模型内部，不需要本文件；
只有 ONNX 后端需要自己实现这一段。

镜像来源(全部为只读核对过的 PaddleX 3.7.2 源码):
  paddlex/inference/models/text_recognition/processors.py:47-105
      OCRReisizeNormImg(resize + normalize + 右零填充)
  paddlex/inference/models/text_recognition/processors.py:287-319
      CTCLabelDecode(argmax / 去重 / 去 blank)
  paddlex/inference/models/text_recognition/processors.py:109-148, 213-268
      BaseRecLabelDecode(字典组装 / decode / 置信度均值)
  paddlex/inference/common/reader/image_reader.py:27-55
      ReadImage(format="BGR"):ndarray 输入直通,不做任何转换
  runtime/ppocr/PP-OCRv6_medium_rec_infer/inference.yml
      RecResizeImg.image_shape = [3, 48, 320]

对应关系(预处理管线,与 predictor.process + ToBatch 在 batch=1 下等价):
  输入 uint8 BGR (H, W, 3)                 <- ReadImage 直通(生产传 ndarray)
  max_wh_ratio = max(320/48, w/h)          <- OCRReisizeNormImg.resize
  imgW = int(48 * max_wh_ratio)
  imgW > 3200 -> cv2.resize 拉伸到 (3200,48) <- resize_norm_img 分支 1
  否则 resized_w = min(ceil(48*w/h), imgW)
       cv2.resize 到 (resized_w, 48)        <- resize_norm_img 分支 2(cv2 默认双线性)
  float32 -> CHW -> /255 -> -0.5 -> /0.5    <- 归一化(等价 mean=0.5, scale=0.5)
  右侧零填充到 (3, 48, imgW)                <- padding(0 = 归一化域的 0.5 灰)
  ToBatch 批内 pad(batch=1 -> no-op)        <- 生产 batch_size=1

刻意不做的事:不缓存、不向量化、不换插值算法、不改 padding、
不做任何"等价优化"。每一步与镜像源逐字对应。

【不可变契约】本文件的任何函数都**不得修改**传入的图像数组
（`cv2.resize` / `.astype` 都分配新数组，原图必须保持逐字节不变）——
生产侧的 sample capture 与诊断记录用的就是调用方那张原图。
"""

import math
import re

import cv2
import numpy as np

# inference.yml RecResizeImg.image_shape(生产值,勿改)
REC_IMAGE_SHAPE = (3, 48, 320)


class OCRReisizeNormImgMirror:
    """镜像 OCRReisizeNormImg(processors.py:47-105),仅 input_shape=None 路径。

    生产构造:OCRReisizeNormImg(rec_image_shape=[3,48,320], input_shape=None)
    —— 该模型的 inference.yml 无静态 input_shape,predictor 走 resize() 分支。
    """

    def __init__(self, rec_image_shape=(3, 48, 320)):
        self.rec_image_shape = rec_image_shape
        self.input_shape = None
        self.max_imgW = 3200

    def resize_norm_img(self, img, max_wh_ratio):
        # 与 processors.py:56-79 逐行对应
        imgC, imgH, imgW = self.rec_image_shape
        imgW = int((imgH * max_wh_ratio))
        if imgW > self.max_imgW:
            resized_image = cv2.resize(img, (self.max_imgW, imgH))
            resized_w = self.max_imgW
            imgW = self.max_imgW
        else:
            h, w = img.shape[:2]
            ratio = w / float(h)
            if math.ceil(imgH * ratio) > imgW:
                resized_w = imgW
            else:
                resized_w = int(math.ceil(imgH * ratio))
            resized_image = cv2.resize(img, (resized_w, imgH))
        resized_image = resized_image.astype("float32")
        resized_image = resized_image.transpose((2, 0, 1)) / 255
        resized_image -= 0.5
        resized_image /= 0.5
        padding_im = np.zeros((imgC, imgH, imgW), dtype=np.float32)
        padding_im[:, :, 0:resized_w] = resized_image
        return padding_im

    def resize(self, img):
        # 与 processors.py:90-97 逐行对应
        imgC, imgH, imgW = self.rec_image_shape
        max_wh_ratio = imgW / imgH
        h, w = img.shape[:2]
        wh_ratio = w * 1.0 / h
        max_wh_ratio = max(max_wh_ratio, wh_ratio)
        img = self.resize_norm_img(img, max_wh_ratio)
        return img

    def __call__(self, imgs):
        # 与 processors.py:81-88 的 input_shape is None 分支对应
        return [self.resize(img) for img in imgs]


class CTCLabelDecodeMirror:
    """镜像 BaseRecLabelDecode + CTCLabelDecode(processors.py:109-148, 213-319)。

    字典组装顺序(生产顺序,不可调换):
        list(yml character_dict)        # 18,710 项
        + [" "]                         # use_space_char=True(默认,yml 未覆写)
        -> CTCLabelDecode.add_special_char 头部插入 "blank"(idx 0)

    注意返回结构:`__call__` 返回 **list[(text, score)]**(与 PaddleX 同形),
    不是 (texts, scores) 两元组。消费方须按 `for text, score in ...` 解包。
    """

    def __init__(self, character_dict, use_space_char=True):
        character_list = list(character_dict)
        if use_space_char:
            character_list.append(" ")
        character_list = ["blank"] + character_list  # add_special_char(processors.py:316-319)
        self.character = character_list

    def __call__(self, pred):
        # 与 CTCLabelDecode.__call__(processors.py:293-314)对应:pred[0] 取首个输出
        preds = np.array(pred[0] if isinstance(pred, (list, tuple)) else pred)
        preds_idx = preds.argmax(axis=-1)
        preds_prob = preds.max(axis=-1)
        return self.decode(preds_idx, preds_prob, is_remove_duplicate=True)

    def decode(self, text_index, text_prob=None, is_remove_duplicate=False):
        # 与 BaseRecLabelDecode.decode(processors.py:213-264)对应
        result_list = []
        ignored_tokens = [0]  # get_ignored_tokens(processors.py:266-268):CTC blank
        batch_size = len(text_index)
        for batch_idx in range(batch_size):
            selection = np.ones(len(text_index[batch_idx]), dtype=bool)
            if is_remove_duplicate:
                selection[1:] = (
                    text_index[batch_idx][1:] != text_index[batch_idx][:-1]
                )
            for ignored_token in ignored_tokens:
                selection &= text_index[batch_idx] != ignored_token

            char_list = [
                self.character[text_id]
                for text_id in text_index[batch_idx][selection]
            ]
            if text_prob is not None:
                conf_list = text_prob[batch_idx][selection]
            else:
                conf_list = [1] * len(selection)
            if len(conf_list) == 0:
                conf_list = [0]

            text = "".join(char_list)
            result_list.append((text, np.mean(conf_list).tolist()))
        return result_list


#: charset.txt 的项数 = PP-OCRv6 inference.yml 里 PostProcess.character_dict 的长度。
#: （最终字符表是 18,710 项 = 本文件 18,708 项 + CTCLabelDecodeMirror 追加的空格和 blank。）
CHARSET_ENTRIES = 18708

#: charset.txt 的最终字符表长度(blank + dict + space)，供调用方交叉核对。
CHARSET_FINAL_SIZE = CHARSET_ENTRIES + 2


def load_charset(path):
    """读取 ``charset.txt`` —— 一字符一行，共 ``CHARSET_ENTRIES`` 行。

    该文件由**构建/转换阶段**的 ``scripts/export_onnx_charset.py`` 从 PP-OCRv6
    ``inference.yml`` 的 ``PostProcess.character_dict`` 导出；**运行时只读这个文件**，
    绝不读 Paddle 侧的 ``inference.yml``（那样的运行时依赖必须切断）。

    存的是**原始字典项**(18,708 项)，不含 blank、不含空格 ——
    ``CTCLabelDecodeMirror`` 仍按 PaddleX 的既有语义在头部插入 ``blank``、
    在末尾追加空格。这样镜像类保持与 Step 0 验证版本**逐字节相同**，
    索引映射也逐项一致。

    这里刻意**不做 strip()**：格式契约是「一字符一行」，任何额外修剪都可能改变
    某一项的取值，进而让所有字符索引错位。
    """
    with open(path, "r", encoding="utf-8", newline="") as f:
        raw = f.read()

    if not raw:
        raise ValueError("charset 文件为空: %s" % path)
    if "\r" in raw:
        # CRLF 会让每一行尾部多一个 \r，静默污染整个索引映射 —— 必须响亮失败。
        raise ValueError(
            "charset 文件含 CR(CRLF 换行)，会让每一项尾部多一个 \\r 并"
            "静默污染整个索引映射: %s" % path
        )
    if raw.endswith("\n"):
        raw = raw[:-1]
    items = raw.split("\n")
    if len(items) != CHARSET_ENTRIES:
        raise ValueError(
            "charset 项数不符: 期望 %d，实际 %d (%s) —— "
            "项数不符意味着每个字符索引都会错位"
            % (CHARSET_ENTRIES, len(items), path)
        )
    return items


def read_image_bgr(path):
    """读图为 uint8 BGR (H, W, 3)。

    用 np.fromfile + cv2.imdecode 而不是 cv2.imread:数据集路径含非 ASCII
    字符,Windows 下 cv2.imread 会静默返回 None。生产链路不落盘,无此问题;
    本函数只是实验侧的等价读图,不改变像素语义(磁盘 PNG -> BGR uint8)。
    """
    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)  # IMREAD_COLOR => BGR,与生产一致
    if img is None:
        raise IOError("读图失败:%s" % path)
    return img
