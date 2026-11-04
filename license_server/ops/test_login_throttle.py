# -*- coding: utf-8 -*-
"""admin2 登录锁定实测(服务器上运行):5 次错误后第 6 次 429。"""
import re
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

cj = CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def req(path, data=None):
    r = urllib.request.Request("http://127.0.0.1:8000" + path, data=data)
    try:
        with op.open(r) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


_, body = req("/admin2/login")
m = re.search(r'name="csrf_token" value="([^"]+)"', body)
csrf = m.group(1)
for i in range(1, 8):
    code, page = req("/admin2/login", urllib.parse.urlencode({
        "username": "admin", "password": "wrong-%d" % i,
        "csrf_token": csrf}).encode())
    extra = "(已锁定)" if code == 429 else ""
    print("第%d次错误登录 -> %d %s" % (i, code, extra))
