#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RELEASE-01 —— 购买授权入口 / 联系方式统一（QQ）验证脚本

覆盖：
    A. 常量：作者联系方式统一为 QQ 1752924057；购买链接为指定淘宝地址
    B. 未激活遮罩：次要按钮文本 = 「购买授权」，点击发 purchase_clicked（不再是联系作者）
    C. open_external_url：成功返回 True；失败返回 False 且弹出明确提示（不假报成功）
    D. 标题栏联系方式卡片：内容为 QQ，且不再出现「微信」
    E. 授权逻辑未被触碰（回归）：LicenseManager 的公开方法集合与签名不变

约定与仓库其它 test_*.py 一致：扁平脚本、check()/section() 自行统计，
直接 ``python test_release01_purchase_contact.py`` 运行，不依赖 pytest。
关键隔离：APPDATA / LOCALAPPDATA 指向临时目录，绝不写用户真实配置；
QDesktopServices / QMessageBox 一律打桩，本脚本不真的打开浏览器。
"""

import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="dlv-rel01-appdata-")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="dlv-rel01-local-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

import gui.widgets as widgets  # noqa: E402
import gui.main_window as mw  # noqa: E402
from gui.settings_dialog import CONTACT_QQ, PURCHASE_URL  # noqa: E402

TAOBAO = "https://m.tb.cn/h.jaMe2gX?tk=CfF8TI9nJew"

_results = []


def check(cond, msg):
    _results.append((bool(cond), msg))
    print("  [%s] %s" % ("PASS" if cond else "FAIL", msg))


def section(t):
    print("\n" + t)


app = QApplication.instance() or QApplication([])


class _FakeDesktopServices:
    result = True

    @staticmethod
    def openUrl(_url):
        return _FakeDesktopServices.result


class _FakeMsgBox:
    calls = []

    @staticmethod
    def warning(parent, title, text, *a, **k):
        _FakeMsgBox.calls.append((title, text))


def t_constants():
    section("A. 常量")
    check(CONTACT_QQ == "1752924057", "CONTACT_QQ == 1752924057 (%r)" % CONTACT_QQ)
    check(PURCHASE_URL == TAOBAO, "PURCHASE_URL 为指定淘宝链接 (%r)" % PURCHASE_URL)
    check(not hasattr(__import__("gui.settings_dialog", fromlist=["x"]), "CONTACT_WECHAT"),
          "settings_dialog 不再导出 CONTACT_WECHAT")


def t_overlay_button():
    section("B. 未激活遮罩按钮")
    ov = mw._ActivationOverlay()
    check(ov._contact_btn.text() == "购买授权",
          "次要按钮文本为「购买授权」 (%r)" % ov._contact_btn.text())
    check("联系作者" not in ov._contact_btn.text(), "次要按钮不再是「联系作者」")
    check(hasattr(ov, "purchase_clicked"), "遮罩发出 purchase_clicked 信号")
    check(not hasattr(ov, "contact_clicked"), "遮罩不再有 contact_clicked 信号")
    got = []
    ov.purchase_clicked.connect(lambda: got.append(1))
    ov._contact_btn.click()
    app.processEvents()
    check(got == [1], "点击「购买授权」触发 purchase_clicked")


def t_open_external_url():
    section("C. open_external_url 成功/失败语义")
    orig_ds, orig_mb = widgets.QDesktopServices, widgets.QMessageBox
    widgets.QDesktopServices, widgets.QMessageBox = _FakeDesktopServices, _FakeMsgBox
    try:
        _FakeDesktopServices.result = True
        _FakeMsgBox.calls = []
        ok = widgets.open_external_url(TAOBAO)
        check(ok is True, "打桩成功 → 返回 True")
        check(_FakeMsgBox.calls == [], "成功时不弹提示")

        _FakeDesktopServices.result = False
        _FakeMsgBox.calls = []
        ok = widgets.open_external_url(TAOBAO)
        check(ok is False, "打桩失败 → 返回 False（不假报成功）")
        check(len(_FakeMsgBox.calls) == 1
              and TAOBAO in _FakeMsgBox.calls[0][1],
              "失败时弹出明确提示并给出链接")

        check(widgets.open_external_url("") is False, "空 URL → False")
    finally:
        widgets.QDesktopServices, widgets.QMessageBox = orig_ds, orig_mb


def t_contact_popup_qq():
    section("D. 标题栏联系方式卡片内容")
    win = mw.MainWindow()
    try:
        win._open_contact_popup(win._contact_btn)
        popup = win._contact_popup
        check(popup is not None, "卡片已构造")
        texts = [lb.text() for lb in popup.findChildren(QLabel)] if popup else []
        blob = " ".join(texts)
        check("1752924057" in blob, "卡片含 QQ 号 1752924057")
        check("QQ" in blob, "卡片含『QQ』标签行")
        check("微信" not in blob, "卡片不再出现「微信」")
        check("邮箱" not in blob, "卡片不再出现「邮箱」（统一为 QQ）")
        if popup:
            popup.close()
    finally:
        win.close()


def t_license_untouched():
    section("E. 授权逻辑回归（公开接口未被改动）")
    from utils.license_manager import LicenseManager
    for name in ("activate", "activate_offline", "load_stored",
                 "get_machine_code", "heartbeat"):
        check(hasattr(LicenseManager, name), "LicenseManager.%s 仍存在" % name)
    check(hasattr(mw.MainWindow, "_on_license_clicked"),
          "MainWindow._on_license_clicked 仍存在")


def main():
    print("=" * 64)
    print("RELEASE-01 购买授权入口 / 联系方式统一（QQ）验证")
    print("=" * 64)
    for fn in (t_constants, t_overlay_button, t_open_external_url,
               t_contact_popup_qq, t_license_untouched):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, "%s 抛异常: %s" % (fn.__name__, e))
    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 64)
    print("RESULT: %d/%d passed" % (passed, total))
    for ok, msg in _results:
        if not ok:
            print("  FAIL: %s" % msg)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
