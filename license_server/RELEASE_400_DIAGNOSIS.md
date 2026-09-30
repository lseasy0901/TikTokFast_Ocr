# RELEASE_400_DIAGNOSIS.md —— POST /updates/releases 400 定位与修复

日期：2026-09-30
方法：只读审计本地 `license_server/app/` 源码 + 管理端 `publish_update.bat` → 定位后实施最小修复
结论先行：**detail 一直都在 HTTP 响应体里，只是发布脚本没把它打出来**。按 §3 的重放命令 30 秒可拿到真实原因；§1 给出按概率排序的候选。
**修复已实施（见文末 §5），服务器部署后生效。**

---

## 0. 管理端 payload 实际长什么样（publish_update.bat 反推）

bat 强制发布的是 **delta 包**，payload 全部由本地文件推导：

```json
{
  "version": "2.0.1",
  "channel": "stable",
  "package_filename": "LiveLensDesktop-2.0.1-stable-delta.nupkg",   ← $i.Name，且本地强制 == 该模式
  "package_share_url": "https://…/s/…",
  "release_notes": "…",
  "package_sha1":   "<本地 delta 文件的 SHA1，大写 40 位>",
  "package_sha256": "<本地 delta 文件的 SHA256，大写 64 位>",
  "package_size":   <本地 delta 文件字节数>
}
```

→ 交接文档里"可能2 空格/路径、可能3 SHA 格式、可能4 域名不合法"**已被 bat 本地预检排除**（本地校验正则与服务端一致或更严）。

## 1. 400 候选（按概率排序，全部有源码行号依据）

| # | 服务端消息（detail） | 触发条件 | 概率 |
|---|---|---|---|
| 1 | `发布前解析失败: 分享中未找到文件: LiveLensDesktop-<ver>-stable-delta.nupkg`（release_service.py:197 ← resolver:378 的 404→400） | 飞盘分享里没有与 `package_filename` **逐字符相等**的 `fileName`。典型：管理员往飞盘传的是 **full 包**（302MB 那个）而不是 delta 包；或上传时文件被改名；或大小写不一致（匹配是大小写敏感的 `==`） | **最高** |
| 2 | `package_share_url 非法: 分享链接必须形如 https://share.feijipan.com/s/<id>`（resolver:190，`_SHARE_PATH_RE = ^/s/[^/?]+$`） | **粘贴的链接带尾部斜杠** `/s/abc123/`。bat 的 PowerShell 正则 `/s/.+` 是通过的（大小写不敏感、允许尾斜杠），服务端要求单段路径——**两端校验强度不一致**，这是真实的坑 | 高 |
| 3 | `package_size 与分享中的文件大小不一致（申报 X，实际 Y）`（release_service.py:213） | 飞盘上的同名文件与本地 delta 字节数不同（重新构建后重传、传错文件但名字相同） | 中 |
| 4 | `发布前解析失败: 获取文件信息失败: <上游msg>`（resolver:348 返回 code=400 → HTTP 400） | 分享失效/需要提取码/上游风控——**这类上游错误也落 400**，不是 502。比交接文档假设的"解析失败=502"范围更宽 | 中 |
| 5 | `发布前解析失败: 文件列表为空，可能链接已失效`（resolver:356，404→400） | 分享被删/过期 | 低 |
| 6 | `该渠道已存在版本 X 的 release` | 重复发布（注意这是 **409**，日志里应显示 409 不是 400） | 排除 |

补充：`解析结果与 package_filename 不一致`（:207）实际上不可达——matched 分支保证 `fileName == package_filename` 才返回。

## 2. 为什么 journalctl 里什么都看不到（交接文档 §9 的答案）

- resolver 的关键诊断日志 `分享中未找到指定文件: %s`、`小飞机解析成功: file_name=… file_size=…` 全部是 **logger.info**；
- 服务端**没有任何 logging 级别配置**（app/ 内无 basicConfig/dictConfig/setLevel），uvicorn 默认 root=WARNING；
- → info 全部被吞，日志里只剩 uvicorn access line 的 400。**这不是没有日志，是级别把日志吞了。**

## 3. 拿真实错误的两条路（按速度排序）

### 路 A：零改动，30 秒（推荐先做）

detail 一直在 HTTP 响应体里（`HTTPException(detail=str(e))`）。bat 的 catch 只打了 `$_.Exception.Message`，PS 5.1 不含 body——所以管理员看不到。

bat 每次发布都把**完整 payload** 写在 `%TEMP%\livelens_publish_*.json`（LL_TMP_JSON）。直接重放：

```bash
# 在管理员机器上（先设置 LIVELENS_ADMIN_KEY 环境变量）
curl -s -X POST https://<服务器>/api/v1/updates/releases \
  -H "X-API-Key: %LIVELENS_ADMIN_KEY%" \
  -H "Content-Type: application/json" \
  --data @"%TEMP%\livelens_publish_xxx.json"
```

响应就是 `{"detail": "<真实原因>"}`，对号入座 §1 表格即可。

### 路 B：服务端日志补丁（交接文档 §9 的方案，精确到行）

`app/main.py` 发布接口的 except 块加一行（约 :160 附近）：

```python
    except release_service.ReleaseError as e:
        logger.error("release publish failed: %s | payload_version=%s filename=%s",
                     str(e), getattr(payload, "version", None),
                     getattr(payload, "package_filename", None))
        raise HTTPException(status_code=e.status, detail=str(e))
```

（日志**不含**临时直链，符合 resolver 的脱敏设计。）

同时把 resolver 两行关键 info 提为 warning（feijipan_resolver.py:376、:475），不改其它：

```python
logger.warning("分享中未找到指定文件: %s", filename)      # 原 info
logger.warning("小飞机解析成功: file_name=%s file_size=%s", ...)  # 原 info
```

改完 `systemctl restart livelens-license`，重新发布一次，`journalctl -u livelens-license -f` 直接看原因。

### 顺手核验飞盘分享内容的独立命令（服务器上跑，不依赖发布请求）

```bash
cd /opt/livelens/license_server && python3 - <<'EOF'
import asyncio, json
from app.services import feijipan_resolver as r

share = "https://share.feijipan.com/s/xxxx"      # ← 换成管理员实际用的链接
async def main():
    try:
        out = await r.resolve(share, "", "LiveLensDesktop-2.0.1-stable-delta.nupkg")
        print("MATCH:", out["file_name"], out["file_size"])
    except r.FeijipanResolveError as e:
        print("RESOLVE FAIL:", e)
    # 打印分享里的真实文件名清单（直链不入库/不打日志，这里只打印名字）
asyncio.run(main())
EOF
```

## 4. 定位后的修复方向（拿到 detail 之后再做，不在本次）

- 候选 1/3 → 运维动作：把正确命名的 delta 包传上飞盘，重新发布（服务端无改动）
- 候选 2 → 两端校验对齐：要么 bat 正则收紧到 `/s/[^/?]+$` 并 trim 尾斜杠，要么服务端放宽容忍尾斜杠（strip 后再校验）——**二选一，不要都改**
- 结构性问题（与本次 400 无关，记录备查）：上游 API 的部分业务失败被映射为 400，语义上更像 502/503，会让"参数错"和"上游错"混在一个状态码里，排障时全靠 detail 文案区分

---

## 5. 已实施的修复（2026-09-30，最小改动，不动发布/匹配语义）

| # | 文件 | 修复 | 对应候选 |
|---|---|---|---|
| F1 | `app/main.py`（发布接口 except） | ReleaseError 时 `logger.error("release publish failed (status=…): … \| version/channel/filename")` —— 排障不再只看到一行 400 | 全部 |
| F2 | `app/services/feijipan_resolver.py` | 两处关键日志 info→warning（"分享中未找到指定文件" / "小飞机解析成功"），默认日志级别下可见 | 候选 1/3/4 |
| F3 | `app/services/feijipan_resolver.py` `validate_share_url` | 路径 rstrip("/") 后再匹配 `_SHARE_PATH_RE`：**尾部斜杠容忍**（与 bat 本地正则对齐）；多段路径 `/s/a/b` 仍拒绝 | 候选 2 |
| F4 | `publish_update.bat`（:207 catch） | 打印 `[DETAIL] +$_.ErrorDetails.Message`（服务端响应体），管理员当场看到真实原因 | 全部 |

未改动：发布流程顺序、精确文件名匹配语义、API 契约、hash/size 校验规则。

## 6. 修复验证

`license_server/test_update_release.py`：**78/78 通过**（新增 [17] 段 8 项断言）：
- 尾斜杠链接通过 `validate_share_url`；多段路径仍拒绝（单元层 ×3）
- 带尾斜杠 URL 的完整发布 → 200 且入库（API 层，走真实 validate_share_url）
- 同版本重复发布 → 409，且服务端日志含 "release publish failed" 与失败原因（F1 生效证明）

回归说明：
- `test_feijipan_resolver.py` 96/96 通过（URL 校验改动无回归）
- `test_phase_7_2_5.py` 1/10、`test_phase_7_2_6.py` 8/8 —— 7_2_5 的失败在**干净树（stash 本 diff 后）完全一致**，属既有问题（susi SignedLicense 环境依赖），与本修复无关；7_2_6 曾在批量顺序跑中出现一次失败，干净树与带改动同顺序重跑均通过，判定为测试间状态污染的暂态

## 7. 部署清单（服务器 /opt/livelens/license_server）

1. 同步 2 个服务端文件：`app/main.py`、`app/services/feijipan_resolver.py`
2. `systemctl restart livelens-license`
3. 管理员机器更新 `publish_update.bat`（透出 [DETAIL]）
4. 重新发布：若 detail 显示"分享中未找到文件"→ 把正确命名的 delta 包传上飞盘再发；若"大小不一致"→ 核对上传文件与本地是否同一构建

## 8. 实战修复记录（2026-09-30，2.1.0 真实发布）

重放拿到 detail：`发布前解析失败: 文件列表为空，可能链接已失效`（候选 5）。

本地用 resolver 对真实分享 `…/s/covzYBmB` 直接诊断：
- **分享现在正常**（04:23 失败时刚创建未生效，`updTime=2026-09-30 03:14:41`）
- 文件名与 payload 完全一致：`LiveLensDesktop-2.1.0-stable-delta.nupkg`
- **新发现（修复前必踩的第二个 400）**：飞盘 `fileSize` 是 **KB 向下取整**，不是字节
  （15,106,417B → 报告 14,752；原始结构里没有字节级字段）

据此追加修复 F5：
- `app/services/release_service.py` size 交叉校验接受两种口径（精确字节 / KB 取整）；
  **落库改用申报的真实字节**（旧逻辑会把飞盘 KB 值落库，feed Size 差 1024 倍，
  客户端更新进度/大小显示全错——这是审计之外的既有 bug）。
- 原报告"候选 3"的触发条件修正为：申报字节与 KB 口径**都不**匹配（真传错文件）。

测试：`test_update_release.py` **82/82**（新增 [18] 段：KB 口径 → 200 且落库真实字节、
精确字节口径 → 200、真不一致 → 400）；`test_feijipan_resolver.py` 96/96。

## 9. 现在就可以重发

服务端修复部署（main.py / release_service.py / feijipan_resolver.py + restart）后，
**同一命令重发 2.1.0 即可成功**——分享、文件名、大小口径现在全部对齐。
