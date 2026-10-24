# 品牌字体（可选，不入 git）

把要启用的 `.ttf` / `.otf` 放进本目录即可，程序启动时自动注册并把
第一个 family 前置进全局字体栈（`gui/fonts.py`）。缺文件 = 静默回退
系统字体（Segoe UI / 微软雅黑），不会报错。

推荐：MiSans（小米，免费可商用）Regular / Medium / Semibold 三档。
打包发布时发布脚本会把本目录一并带入安装包。
