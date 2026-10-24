#!/usr/bin/env bash
# ============================================================
# LiveLens Admin 2.0 — 生产重启 + 冒烟（P3 批准后执行；单次重启）
# 冒烟：3 枚测试 KEY —— K1 首次激活 / K2 续费 / K3 UNUSED 吊销。
# 明文 KEY 仅存于本进程变量与一次性响应，不落盘、不回显完整值。
# 用法：ADMIN_PASSWORD=<ADMIN_API_KEY> bash prod_smoke.sh
# ============================================================
set -euo pipefail

: "${ADMIN_PASSWORD:?需要 ADMIN_PASSWORD=<ADMIN_API_KEY>（经隧道本机填写，不回显）}"
BASE=http://127.0.0.1:8000
PY=/opt/livelens/venv/bin/python
SMOKE_DB_TAG="smoke-$(date +%Y%m%d%H%M%S)"

# 1. 单次重启
systemctl restart livelens-license
sleep 2
systemctl is-active livelens-license

# 2. 只读冒烟
for p in "/" "/admin/login" "/admin2/login"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE$p")
  [ "$code" = "200" ] || { echo "FAIL $p -> $code"; exit 1; }
  echo "PASS GET $p -> 200"
done
code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/admin2/api/overview")
[ "$code" = "401" ] || { echo "FAIL 未认证 API 应 401，实际 $code"; exit 1; }
echo "PASS 未认证 /admin2/api -> 401"

# 3. 管理写操作（3 枚测试 KEY 全链路）
"$PY" - "$BASE" "$ADMIN_PASSWORD" "$SMOKE_DB_TAG" <<'EOF'
import re, sys, json, urllib.request, http.cookiejar
base, pwd, tag = sys.argv[1], sys.argv[2], sys.argv[3]
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def req(path, data=None, headers=None, method=None):
    r = urllib.request.Request(base + path, data=data, headers=headers or {}, method=method)
    with op.open(r) as resp:
        return resp.status, resp.read().decode()

_, body = req("/admin2/login")
csrf = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)
import urllib.parse
req("/admin2/login", urllib.parse.urlencode(
    {"username": "admin", "password": pwd, "csrf_token": csrf}).encode())
_, page = req("/admin2/keys")
csrf2 = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
H = {"Content-Type": "application/json", "X-CSRF-Token": csrf2}

def post(path, payload):
    return req(path, json.dumps(payload).encode(), H)

# 3 枚月卡：K1 首次激活 / K2 续费 / K3 吊销
_, gen = post("/admin2/api/keys/generate", {"duration_days": 30, "quantity": 3})
keys = json.loads(gen)["plaintext_keys"]
dev = f"DV-{tag}"
s, act1 = req("/api/v1/licenses/activate", json.dumps(
    {"license_key": keys[0], "device_id": dev}).encode(), {"Content-Type": "application/json"})
assert s == 200, act1
s, act2 = req("/api/v1/licenses/activate", json.dumps(
    {"license_key": keys[1], "device_id": dev}).encode(), {"Content-Type": "application/json"})
assert s == 200, act2
print("PASS 首次激活 + 续费（设备", dev, "）")

s, lk = post("/admin2/api/keys/lookup", {"license_key": keys[1]})
_lk = json.loads(lk)
assert _lk["status"] == "redeemed", lk
assert _lk["device"] and _lk["device"]["device_id"] == dev, lk
print("PASS KEY 反查命中（已兑换，绑定", dev, "）")

# K1 重复兑换必须拒绝（一次性）
s, dup = req("/api/v1/licenses/activate", json.dumps(
    {"license_key": keys[0], "device_id": dev}).encode(), {"Content-Type": "application/json"})
assert s == 400 and "already_redeemed" in dup, dup
print("PASS K1 重复兑换拒绝（already_redeemed）")

# K3 吊销前显式验证 UNUSED
s, lk3 = post("/admin2/api/keys/lookup", {"license_key": keys[2]})
_lk3 = json.loads(lk3)
assert _lk3["status"] == "unused", lk3
k3_id = _lk3["license"]["id"]
print("PASS K3 吊销前状态 UNUSED（#%d）" % k3_id)

# 不存在的卡密
s, lk4 = post("/admin2/api/keys/lookup", {"license_key": "zz" * 21})
assert json.loads(lk4)["status"] == "not_found", lk4
print("PASS KEY 反查未命中有明确状态")

s, rv = post("/admin2/api/keys/revoke", {"license_id": k3_id})
assert s == 200, rv
print("PASS UNUSED 吊销（#%d）" % k3_id)
s, rv2 = post("/admin2/api/keys/revoke", {"license_id": k3_id})
assert s == 409, rv2
print("PASS 重复吊销拒绝（409）")

# CSV 导出格式检查（兑换流水：BOM + 表头 + 数据行）
import io as _io
s, csvraw = req("/admin2/api/export/csv?scope=redemptions&page_size=500")
assert s == 200 and csvraw.startswith("﻿"), "CSV 缺 BOM"
_lines = csvraw.lstrip("﻿").strip().splitlines()
assert _lines[0].startswith("兑换时间"), _lines[0]
assert len(_lines) >= 3 and any("首次激活" in l for l in _lines), "CSV 缺数据行"
print("PASS CSV 导出格式（BOM+表头+%d 行）" % (len(_lines) - 1))

# 事件对账：backfill + live == REDEEMED
import sqlite3
con = sqlite3.connect("file:/var/lib/livelens/license_server.db?mode=ro", uri=True)
q = lambda s: con.execute(s).fetchone()[0]
n_ev = q("SELECT COUNT(*) FROM redemption_events")
n_rd = q("SELECT COUNT(*) FROM licenses WHERE state='REDEEMED' AND redeemed_at IS NOT NULL")
assert n_ev == n_rd, f"对账不一致: events={n_ev} redeemed={n_rd}"
print(f"PASS 事件对账：{n_ev} == {n_rd}")

# 兑换流水标签：本设备应有 首次激活 + 续费(有效期累加) 两条
_, flow = req(f"/admin2/api/redemptions?q={dev}&page_size=10")
items = json.loads(flow)["items"]
labels = sorted(i["result"] for i in items)
assert labels == ["有效期累加", "新建授权"], labels
print("PASS 流水标签：", labels)
EOF

# 4. 日志检查
sleep 1
n=$(journalctl -u livelens-license --since "-3 min" 2>/dev/null | grep -ci traceback || true)
[ "$n" = "0" ] || { echo "FAIL 日志出现 $n 个 Traceback"; exit 1; }
echo "PASS 无 Traceback"
echo "冒烟全部通过。测试卡密状态：2 已兑换 + 1 已吊销（无遗留未使用卡）。"
