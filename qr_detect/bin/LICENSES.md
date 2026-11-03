# qr_detect/bin 第三方组件许可

| 文件 | 来源 | 许可 | 使用方式 |
|---|---|---|---|
| zbar-0.dll | ZBar (http://zbar.sourceforge.net/) | LGPL-2.1 | 动态链接,未修改 |
| iconv-2.dll | libiconv (GNU) | LGPL-2.1 | 动态链接,未修改 |
| qrencode.dll | libqrencode (https://fukuchi.org/works/qrencode/) | LGPL-2.1 | 动态链接,未修改 |

三个库均以**动态链接**方式使用且未修改其源码,LGPL-2.1 允许如此与专有软件
组合分发;本目录同时保留以上声明以满足 LGPL 的许可信息随附要求。
用户如需,可自行以相同接口的替代构建替换这些 DLL。
