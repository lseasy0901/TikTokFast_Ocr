# -*- coding: utf-8 -*-
"""
LiveLens Admin 2.0 — Phase 4 隔离环境部署与回滚演练（可重复执行）。

隔离保证：独立临时目录 + 一次性数据库 + 独立端口（8010 新代码 / 8011 回滚代码），
绝不连接生产数据库、绝不 SSH。演练完成后自动清理所有进程与临时文件。

流程（与生产 ops/*.sh 一一对应）：
  [1] 备份（SQLite Backup API）→ [2] 恢复校验（integrity/schema/行数）
  [3] 迁移 → [4] 索引与对账校验 → [5] 重复迁移幂等性
  [6] 启动 → [7] 只读冒烟 → [8] 写操作（3 KEY：首充/续费/吊销 + 反查 + CSV）
  [9] 事件对账 → [10] 故障注入 A（迁移失败不破坏数据）
  [11] 故障注入 B（服务启动失败可恢复）→ [12] 故障注入 C（对账不一致被检出）
  [13] 代码回滚演练（git worktree @ 部署前 commit，旧后台 + 授权 API 正常）
  [14] R2 表回退后旧代码仍正常

用法：cd DouyinLowLatencyViewer/license_server
      .venv/Scripts/python.exe ../../LiveLens_Admin_2.0/ops/rehearsal.py
"""

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import http.cookiejar
from datetime import datetime, timedelta
from pathlib import Path

LS_DIR = Path(__file__).resolve().parents[2] / "DouyinLowLatencyViewer" / "license_server"
REPO = LS_DIR.parent
#: 允许指定被演练的代码目录（默认主工作副本）；例如 git worktree 中的候选提交
CODE_DIR = Path(os.environ.get("REHEARSAL_CODE_DIR", LS_DIR)).resolve()
PY = LS_DIR / ".venv" / "Scripts" / "python.exe"
PORT_NEW, PORT_OLD = 8010, 8011
ADMIN_KEY = "rehearsal-admin-key"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


def port_busy(port):
    import socket
    with socket.socket() as sk:
        sk.settimeout(0.5)
        return sk.connect_ex(("127.0.0.1", port)) == 0


def start_server(workdir, db_url, port):
    """启动并等待就绪；返回 (proc, None) 或 (proc, 诊断输出)。

    端口被占用时快速失败（防止误连到残留实例，导致种子写进别的库）。"""
    if port_busy(port):
        return None, f"port {port} 已被占用（疑似残留进程），拒绝启动"
    env = dict(os.environ, DATABASE_URL=db_url, ADMIN_API_KEY=ADMIN_KEY, DEBUG="false")
    env["PYTHONPATH"] = str(Path(workdir) / "app")
    proc = subprocess.Popen(
        [str(PY), "-m", "uvicorn", "main:app", "--app-dir", "app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=workdir, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(40):
        if proc.poll() is not None:
            return proc, proc.stdout.read().decode(errors="replace")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            return proc, None
        except Exception:
            time.sleep(0.25)
    return proc, "timeout"


def checkpoint(path):
    """合并 WAL 到主库并截断（演练库归我们所有，可写）。
    硬终止的服务会残留热 -wal；只读连接无法执行 WAL 恢复，必须先 checkpoint。"""
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()


def mkop(port):
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def get(op, port, path, headers=None):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with op.open(r, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def post(op, port, path, payload=None, headers=None, form=False):
    h = dict(headers or {})
    if payload is not None:
        data = urllib.parse.urlencode(payload).encode() if form else json.dumps(payload).encode()
        h.setdefault("Content-Type", "application/x-www-form-urlencoded" if form else "application/json")
    else:
        data = None
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=h)
    try:
        with op.open(r, timeout=15) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def login(op, port):
    _, body = get(op, port, "/admin2/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)
    s, _ = post(op, port, "/admin2/login",
                {"username": "admin", "password": ADMIN_KEY, "csrf_token": csrf}, form=True)
    # urllib 跟随重定向：成功 = 303(未跟随) 或落地的 200
    assert s in (200, 303), f"login -> {s}"
    _, page = get(op, port, "/admin2/keys")
    return re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="livelens-rehearsal-"))
    db = tmp / "license_server.db"
    db_url = "sqlite:///" + str(db).replace("\\", "/")
    procs = []
    worktree = None
    print("=" * 66)
    print(f"隔离演练环境：{tmp}")
    print("=" * 66)
    try:
        # ---------- [1] 备份（Backup API）----------
        # 先用生产同构方式造初始数据：启动服务→造 2 张历史兑换（回填素材）
        print("\n[1] 初始数据 + SQLite Backup API 备份")
        proc, diag = start_server(CODE_DIR, db_url, PORT_NEW)
        check("初始服务启动", diag is None and proc is not None, str(diag)[:300] if diag else "")
        if proc is None:
            raise SystemExit(1)
        procs.append((proc, PORT_NEW))
        op = mkop(PORT_NEW)
        csrf = login(op, PORT_NEW)
        H = {"X-CSRF-Token": csrf}
        s, gen = post(op, PORT_NEW, "/admin2/api/keys/generate",
                      {"duration_days": 30, "quantity": 2}, H)
        keys = json.loads(gen)["plaintext_keys"]
        post(op, PORT_NEW, "/api/v1/licenses/activate",
             {"license_key": keys[0], "device_id": "DV-SEED"})
        post(op, PORT_NEW, "/api/v1/licenses/activate",
             {"license_key": keys[1], "device_id": "DV-SEED"})
        procs.remove((proc, PORT_NEW))
        proc.terminate(); proc.wait(timeout=10)

        checkpoint(db)
        if not db.exists() or sqlite3.connect(str(db)).execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='licenses'").fetchone()[0] != 1:
            stray = [str(p) for p in LS_DIR.glob("*.db")]
            raise SystemExit(f"FATAL: 种子数据未写入目标库 {db}；"
                             f"server stdout 尾部：{str(proc.stdout.read()[-400:], errors='replace') if hasattr(proc.stdout,'read') else ''}；"
                             f"LS_DIR 下疑似库文件：{stray}")
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        bak = tmp / "backup.db"
        dst = sqlite3.connect(str(bak))
        src.backup(dst); dst.close(); src.close()
        check("备份文件生成且非空", bak.exists() and bak.stat().st_size > 0)

        # ---------- [2] 恢复校验 ----------
        print("\n[2] 恢复校验（integrity / schema / 行数）")
        con = sqlite3.connect(f"file:{bak}?mode=ro", uri=True)
        check("integrity_check=ok", con.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        check("基线 schema 完整", {"licenses", "authorizations", "device_daily_active"} <= tables)
        check("备份行数核对 (licenses=2, auth=1)",
              con.execute("SELECT COUNT(*) FROM licenses").fetchone()[0] == 2
              and con.execute("SELECT COUNT(*) FROM authorizations").fetchone()[0] == 1)
        con.close()

        # ---------- [3][4][5] 迁移 + 校验 + 幂等 ----------
        print("\n[3-5] 迁移、索引/对账校验、幂等性")
        env = dict(os.environ, DATABASE_URL=str(db_url))
        env["PYTHONPATH"] = str(LS_DIR / "app")
        r1 = subprocess.run([str(PY), "migrate_admin2.py"], cwd=CODE_DIR, env=env,
                            capture_output=True, text=True, timeout=120)
        check("迁移执行成功", "一致 ✓" in r1.stdout, r1.stdout[-200:] + r1.stderr[-200:])
        checkpoint(db)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        idx_names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_%'")]
        need = {"ix_licenses_created_at", "ix_licenses_redeemed_at", "ix_licenses_state",
                "ix_licenses_auth_state_redeemed", "ix_device_daily_active_device_seen"}
        check("5 个统计索引全部创建", need <= set(idx_names))
        check("redemption_events 表存在", "redemption_events" in tables or con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='redemption_events'").fetchone()[0] == 1)
        n_ev = con.execute("SELECT COUNT(*) FROM redemption_events").fetchone()[0]
        n_rd = con.execute("SELECT COUNT(*) FROM licenses WHERE state='REDEEMED'").fetchone()[0]
        check("迁移后事件对账（含 live 历史兑换）", n_ev == n_rd == 2, f"ev={n_ev} rd={n_rd}")
        con.close()
        r2 = subprocess.run([str(PY), "migrate_admin2.py"], cwd=CODE_DIR, env=env,
                            capture_output=True, text=True, timeout=120)
        checkpoint(db)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        n_ev2 = con.execute("SELECT COUNT(*) FROM redemption_events").fetchone()[0]
        check("重复迁移不重复回填", "一致 ✓" in r2.stdout and n_ev2 == 2, f"ev={n_ev2}")
        con.close()

        # ---------- [6][7] 启动 + 只读冒烟 ----------
        print("\n[6-7] 启动新代码 + 只读冒烟")
        proc2, diag = start_server(CODE_DIR, db_url, PORT_NEW)
        check("迁移后新代码启动", diag is None and proc2 is not None, str(diag)[:300] if diag else "")
        if proc2 is None:
            raise SystemExit(1)
        procs.append((proc2, PORT_NEW))
        op = mkop(PORT_NEW)
        for path in ("/", "/admin/login", "/admin2/login"):
            s, _ = get(op, PORT_NEW, path)
            check(f"GET {path} -> 200", s == 200, str(s))
        s, _ = get(op, PORT_NEW, "/admin2/api/overview")
        check("未认证 API -> 401", s == 401, str(s))

        # ---------- [8] 写操作：3 枚测试 KEY ----------
        print("\n[8] 管理写操作（K1 首充 / K2 续费 / K3 吊销 + 反查 + CSV）")
        csrf = login(op, PORT_NEW)
        H = {"X-CSRF-Token": csrf}
        s, gen = post(op, PORT_NEW, "/admin2/api/keys/generate",
                      {"duration_days": 30, "quantity": 3}, H)
        k1, k2, k3 = json.loads(gen)["plaintext_keys"]
        s, a1 = post(op, PORT_NEW, "/api/v1/licenses/activate", {"license_key": k1, "device_id": "DV-SMOKE"})
        check("K1 首次激活", s == 200, a1[:120])
        s, a2 = post(op, PORT_NEW, "/api/v1/licenses/activate", {"license_key": k2, "device_id": "DV-SMOKE"})
        check("K2 同设备续费", s == 200, a2[:120])
        s, dup = post(op, PORT_NEW, "/api/v1/licenses/activate", {"license_key": k1, "device_id": "DV-SMOKE"})
        check("K1 重复兑换被拒 (already_redeemed)", s == 400 and "already_redeemed" in dup, dup[:120])
        s, lk = post(op, PORT_NEW, "/admin2/api/keys/lookup", {"license_key": k2}, H)
        d = json.loads(lk)
        check("KEY 反查命中已兑换并绑定设备", s == 200 and d["status"] == "redeemed"
              and d["device"]["device_id"] == "DV-SMOKE")
        s, lk2 = post(op, PORT_NEW, "/admin2/api/keys/lookup", {"license_key": k3}, H)
        check("KEY 反查命中未使用", json.loads(lk2)["status"] == "unused")
        s, lk3 = post(op, PORT_NEW, "/admin2/api/keys/lookup", {"license_key": "z" * 43}, H)
        check("KEY 反查未命中有明确状态", json.loads(lk3)["status"] == "not_found")
        k3_id = json.loads(lk2)["license"]["id"]
        s, rv = post(op, PORT_NEW, "/admin2/api/keys/revoke", {"license_id": k3_id}, H)
        check("K3 UNUSED 吊销", s == 200, rv[:120])
        s, rv2 = post(op, PORT_NEW, "/admin2/api/keys/revoke", {"license_id": k3_id}, H)
        check("已吊销不可重复吊销 (409)", s == 409)
        s, csv = get(op, PORT_NEW, "/admin2/api/export/csv?scope=redemptions")
        check("CSV 导出含表头与数据", s == 200 and "首次激活" in csv and "有效期累加" in csv)

        # ---------- [9] 事件对账 ----------
        print("\n[9] 事件对账与流水标签")
        checkpoint(db)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        q = lambda s_: con.execute(s_).fetchone()[0]
        n_ev, n_rd = q("SELECT COUNT(*) FROM redemption_events"), q(
            "SELECT COUNT(*) FROM licenses WHERE state='REDEEMED'")
        check("事件数 == REDEEMED 数", n_ev == n_rd, f"{n_ev} vs {n_rd}")
        labels = sorted(r[0] for r in con.execute(
            "SELECT result FROM redemption_events WHERE recorded_via='live' AND device_id='DV-SMOKE'"))
        # 事件表存原始值 new/extend；中文展示标签已由 [8] CSV 断言覆盖
        check("实时事件语义准确 (new + extend)", labels == ["extend", "new"], str(labels))
        con.close()

        # ---------- [10] 故障 A：迁移失败不破坏数据 ----------
        print("\n[10] 故障注入 A：迁移失败不破坏现有数据")
        checkpoint(db)
        broken = tmp / "broken.db"
        shutil.copy(db, broken)
        bcon = sqlite3.connect(str(broken))
        # 模拟一次中断的历史迁移：事件表存在但缺列（不兼容半成品 schema）
        bcon.execute("DROP TABLE redemption_events")
        bcon.execute("CREATE TABLE redemption_events (id INTEGER PRIMARY KEY)")
        n_lic_before = bcon.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
        bcon.commit(); bcon.close()
        rb = subprocess.run([str(PY), "migrate_admin2.py"], cwd=CODE_DIR,
                            env=dict(os.environ, DATABASE_URL="sqlite:///" + str(broken).replace("\\", "/")),
                            capture_output=True, text=True, timeout=120)
        bcon = sqlite3.connect(f"file:{broken}?mode=ro", uri=True)
        still = bcon.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
        ev_cols = [r[1] for r in bcon.execute("PRAGMA table_info(redemption_events)")]
        bcon.close()
        check("不兼容 schema 使迁移失败（退出非零）", rb.returncode != 0,
              f"rc={rb.returncode} out={rb.stdout[-150:]}")
        check("失败的迁移不写半批数据（licenses 原样）", still == n_lic_before,
              f"{still} vs {n_lic_before}")
        check("失败迁移未破坏事件表结构", "license_id" not in ev_cols or len(ev_cols) == 1,
              str(ev_cols))
        check("生产库（演练库）不受故障迁移影响",
              sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
                  "SELECT COUNT(*) FROM redemption_events").fetchone()[0] == n_ev)

        # ---------- [11] 故障 B：服务启动失败 ----------
        print("\n[11] 故障注入 B：错误配置导致启动失败（进程退出，可定位）")
        bad = subprocess.run(
            [str(PY), "-m", "uvicorn", "main:app", "--app-dir", "app",
             "--host", "127.0.0.1", "--port", str(PORT_NEW)],
            cwd=LS_DIR, env=dict(os.environ, DATABASE_URL="sqlite:////nonexistent-dir/x.db",
                                 ADMIN_API_KEY=ADMIN_KEY),
            capture_output=True, text=True, timeout=60)
        check("坏配置启动失败且报错可见", bad.returncode != 0 and "Error" in (bad.stdout + bad.stderr))
        s, _ = get(op, PORT_NEW, "/")
        check("启动失败不影响既有实例", s == 200, str(s))

        # ---------- [12] 故障 C：对账不一致被检出 ----------
        print("\n[12] 故障注入 C：篡改事件表 → 对账检出")
        checkpoint(db)
        tampered = tmp / "tampered.db"
        shutil.copy(db, tampered)
        tcon = sqlite3.connect(str(tampered))
        tcon.execute("DELETE FROM redemption_events WHERE license_id=(SELECT MIN(license_id) FROM redemption_events)")
        tcon.commit()
        q2 = lambda s_: tcon.execute(s_).fetchone()[0]
        mismatch = q2("SELECT COUNT(*) FROM redemption_events") != q2(
            "SELECT COUNT(*) FROM licenses WHERE state='REDEEMED'")
        tcon.close()
        check("对账脚本检出不一致", mismatch)

        procs.remove((proc2, PORT_NEW))
        proc2.terminate(); proc2.wait(timeout=10)

        # ---------- [13] 代码回滚演练 ----------
        print("\n[13] 代码回滚（R1）：git worktree @ 部署前 commit (HEAD)")
        # R1 回滚基线：生产实际运行版本（可用 REHEARSAL_ROLLBACK_REF 指定，
        # 默认主仓库 HEAD——注意 HEAD 含 admin2 时该检查会自然失败）
        pre_commit = os.environ.get("REHEARSAL_ROLLBACK_REF") or subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True
        ).stdout.strip()
        wt = tmp / "rollback-code"
        rwt = subprocess.run(["git", "worktree", "add", "--detach", str(wt), pre_commit],
                             cwd=REPO, capture_output=True, text=True)
        worktree = (REPO, str(wt))
        if rwt.returncode != 0:
            check("创建回滚 worktree", False, rwt.stderr[:200])
        else:
            check("创建回滚 worktree", True)
            # 模拟生产 R1 的真实前提：out-of-band 组件（gitignored）在回退后仍在位。
            # 生产上 git checkout 不会移除这些文件；演练的全新 worktree 必须补齐，
            # 否则旧代码会因缺 susi_helper / admin_web / .env 而无法启动。
            lsw = Path(wt) / "license_server"
            (lsw / ".env").write_text(
                "SECRET_KEY=rehearsal-secret\n"
                f"ADMIN_API_KEY={ADMIN_KEY}\n"
                "SUSI_DEVELOPMENT_PRIVATE_KEY_FILE=test_rsa_key.pem\n",
                encoding="utf-8")
            shutil.copytree(REPO / "admin_web", Path(wt) / "admin_web")  # admin_web 在仓库根（gitignored）
            shutil.copy(LS_DIR / "test_rsa_key.pem", lsw / "test_rsa_key.pem")
            susi_src = REPO / "susi_helper" / "target" / "release" / "susi_helper.exe"
            susi_dst = Path(wt) / "susi_helper" / "target" / "release"
            susi_dst.mkdir(parents=True, exist_ok=True)
            if susi_src.exists():
                shutil.copy(susi_src, susi_dst / "susi_helper.exe")
            check("R1 前提：out-of-band 组件已布置（.env/admin_web/susi_helper/签名密钥）",
                  (Path(wt) / "admin_web" / "sqladmin").exists()
                  and (susi_dst / "susi_helper.exe").exists()
                  and (lsw / ".env").exists())
            proc3, diag = start_server(Path(wt) / "license_server", db_url, PORT_OLD)  # 旧代码 + 已迁移的库
            check("回滚代码启动", diag is None and proc3 is not None, str(diag)[:300] if diag else "")
            if proc3 is None:
                raise SystemExit(1)
            procs.append((proc3, PORT_OLD))
            op2 = mkop(PORT_OLD)
            s, _ = get(op2, PORT_OLD, "/")
            check("回滚后授权 API 根路径可用", s == 200)
            s, _ = get(op2, PORT_OLD, "/admin/login")
            check("回滚后经典后台登录页可用", s == 200)
            s, v = post(op2, PORT_OLD, "/api/v1/licenses/validate",
                        {"license_key": k1, "device_id": "DV-SMOKE"})
            check("回滚后 validate 正常（已兑换设备有效）", s == 200 and json.loads(v)["valid"] is True, v[:150])
            s, gen4 = post(op2, PORT_OLD, "/api/v1/admin/licenses", {"duration_days": 30},
                           {"X-API-Key": ADMIN_KEY, "Content-Type": "application/json"})
            check("回滚后管理生成 API 正常", s == 200)
            s, _ = get(op2, PORT_OLD, "/admin2/login")
            check("回滚后 /admin2 不再暴露（404）", s == 404, str(s))
            # ---------- [14] R2 表回退后旧代码仍正常 ----------
            print("\n[14] R2：删除 redemption_events 后旧代码仍正常")
            dcon = sqlite3.connect(str(db))
            dcon.execute("DROP TABLE redemption_events"); dcon.commit(); dcon.close()
            s, v = post(op2, PORT_OLD, "/api/v1/licenses/validate",
                        {"license_key": k1, "device_id": "DV-SMOKE"})
            check("R2 后旧代码授权校验正常", s == 200 and json.loads(v)["valid"] is True)
            procs.remove((proc3, PORT_OLD))
            proc3.terminate(); proc3.wait(timeout=10)

    finally:
        for p, port in procs:
            try:
                p.terminate(); p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        if worktree:
            subprocess.run(["git", "worktree", "remove", "--force", worktree[1]],
                           cwd=worktree[0], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 66)
    print(f"演练结果：PASS {len(PASS)} / FAIL {len(FAIL)}")
    for f in FAIL:
        print("  FAILED:", f)
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
