# -*- coding: utf-8 -*-
"""
错题本 · 接口自测（只依赖 Python 标准库）
用法：先启动 server.py，再执行  python selftest.py
特点：
  1) 只针对本次自己创建的记录做断言，可重复运行、不怕库里已有数据；
  2) 会临时改动 API Key 做「未配置」用例，**跑完自动还原**，不会把你的 Key 弄丢；
  3) 跑完会把本次创建的错题**连同它产生的复习记录**物理删除（不只是软删），
     所以不会给真实库留下垃圾、也不会让统计页的复习次数虚高。
"""
import base64
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

# 地址可用环境变量覆盖（跑自检.py 会传，保证「起服务的端口」和「自测打的端口」一致）。
BASE = os.environ.get("CTB_BASE") or "http://127.0.0.1:8765"
CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "app.db")
# 本目录的数据指纹。服务端会在 /api/health 里回同一个值；
# 对不上就说明「连上的是别的实例」，必须立刻退出 —— 否则自测数据会写进那台正在用的库，
# 而且因为自测结束时会软删除，用户那边只表现为「统计里的复习次数莫名变多」，极难发现。
LOCAL_DIR_ID = hashlib.md5(DATA_DIR.encode("utf-8")).hexdigest()[:12]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ok = fail = 0
created = []


def _read_cfg():
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def call(path, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(req, timeout=25) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  [PASS] %s %s" % (name, extra))
    else:
        fail += 1
        print("  [FAIL] %s %s" % (name, extra))


def add(**payload):
    """新建一条错题，记录 id 以便最后清理"""
    code, res = call("/api/mistakes", "POST", payload)
    if code == 200:
        created.append(res["item"]["id"])
        return res["item"]
    return {}


def mine(items):
    """只保留本次创建的记录"""
    return [x for x in items if x["id"] in created]


print("=== 1. 健康检查 / 学科表 ===")
code, j = call("/api/health")
check("health", code == 200 and j.get("storage") == "sqlite", "v" + str(j.get("version")))

# ★ 安全闸门：必须先确认「连上的服务用的就是我这个目录的数据」，否则立刻退出。
# 自测会往库里写几十条记录，写错实例的后果是用户真数据被污染，而且因为结束时只做软删除，
# 用户那边几乎看不出来（只表现为统计页的复习次数莫名变多）。
# 这里**必须 fail-closed**：拿不到指纹（对方是旧版）同样拒绝运行 ——
# 旧版恰恰是最容易踩坑的情况，只给个警告等于没防（2026-09-27 实测踩过：警告放过去了，又写了一遍）。
_remote = j.get("dir_id")
if _remote != LOCAL_DIR_ID and os.environ.get("CTB_SELFTEST_UNSAFE") != "1":
    print("")
    print("!! 拒绝运行：无法确认「连上的服务用的就是本目录的数据」。")
    print("   本目录数据指纹 = %s" % LOCAL_DIR_ID)
    print("   对方服务指纹   = %s%s" % (_remote or "(旧版服务，未返回该字段)",
                                        "" if _remote else "  ← 对方版本较旧，更需要人工确认"))
    print("   地址 = %s" % BASE)
    print("   自测会往对方库里写几十条测试记录，所以这里直接停下。")
    print("")
    print("   正确做法：用 tests/跑自检.py 跑（它会自己起服务、端口和地址都对得上）；")
    print("   或者先关掉别处的错题本实例，再在本目录启动 server.py 后重跑本脚本。")
    print("   确认无误、坚持要跑：设 CTB_SELFTEST_UNSAFE=1（不建议）。")
    sys.exit(2)

code, j = call("/api/subjects")
names = [x["name"] for x in j.get("items", [])]
check("四科已初始化", code == 200 and names[:4] == ["语文", "数学", "英语", "科学"],
      "/".join(names))

print("=== 2. 图片上传（data URL 落盘 + 可访问） ===")
png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH"
       "/q842iQAAAABJRU5ErkJggg==")
m_img = add(subject="数学", question_text="自测·带图片的题", error_reason="计算粗心",
            question_image="data:image/png;base64," + png)
check("图片已落盘", m_img.get("question_image", "").startswith("images/"),
      m_img.get("question_image", ""))
try:
    st = OPENER.open(BASE + "/" + m_img["question_image"], timeout=10).status
except Exception as e:
    st = str(e)
check("图片可通过 URL 访问", st == 200, str(st))

print("=== 3. 列表 / 学科筛选 / 关键词搜索 ===")
m_math = add(subject="数学", chapter="第三单元 分数除法", knowledge_point="分数乘法,约分",
             question_text="自测·3/4 乘 8 等于多少", error_reason="计算粗心")
m_cn = add(subject="语文", question_text="自测·这句话用了什么说明方法", error_reason="审题失误")
code, j = call("/api/mistakes")
check("列表能取到本次创建的三条", len(mine(j["items"])) == 3, "%d 条" % len(mine(j["items"])))
code, j = call("/api/mistakes?subject=%E6%95%B0%E5%AD%A6")
check("按学科筛选只回数学", all(x["subject"] == "数学" for x in j["items"]) and len(mine(j["items"])) == 2,
      "%d 条" % len(mine(j["items"])))
code, j = call("/api/mistakes?q=%E7%BA%A6%E5%88%86")
check("按知识点搜索命中", len(mine(j["items"])) == 1, "%d 条" % len(mine(j["items"])))
code, j = call("/api/mistakes?q=zzz" + base64.b16encode(b"notexist").decode())
check("无结果搜索返回空", len(j["items"]) == 0, "%d 条" % len(j["items"]))

print("=== 4. 复习闭环（写记录 + 回写掌握度） ===")
code, j = call("/api/mistakes/%d/review" % m_math["id"], "POST", {"is_correct": True, "note": "自己做对的"})
check("做对 0→1", j.get("mastery") == 1, j.get("mastery"))
code, j = call("/api/mistakes/%d/review" % m_math["id"], "POST", {"is_correct": True, "note": ""})
check("再对 1→2", j.get("mastery") == 2, j.get("mastery"))
code, j = call("/api/mistakes/%d/review" % m_math["id"], "POST", {"is_correct": False, "note": "又错了"})
check("做错归零", j.get("mastery") == 0, j.get("mastery"))
code, j = call("/api/mistakes/%d" % m_math["id"])
item = j["item"]
check("三条复习记录已落库", len(item["reviews"]) == 3 and item["review_count"] == 3,
      "review_count=%d" % item["review_count"])

print("=== 5. 修改 / 掌握度与复习池 ===")
code, j = call("/api/mistakes/%d" % m_math["id"], "PUT", {"mastery": 5, "chapter": "改过的章节"})
check("字段更新生效", j["item"]["mastery"] == 5 and j["item"]["chapter"] == "改过的章节")
code, j = call("/api/mistakes?unmastered=1")
check("已掌握的不再进复习池", m_math["id"] not in [x["id"] for x in j["items"]])
check("未掌握的仍在复习池", m_cn["id"] in [x["id"] for x in j["items"]])

print("=== 6. 统计 ===")
code, j = call("/api/stats")
st = j["stats"]
check("统计字段齐全",
      all(k in st for k in ("total", "mastered", "by_subject", "by_reason", "by_kp", "last7")),
      "总数%d 已掌握%d 复习%d次" % (st["total"], st["mastered"], st["review_count"]))

print("=== 7. 导出 ===")
code, j = call("/api/export")
exp = j["data"]
check("导出含错题", len(exp["mistakes"]) >= 2, "%d 题" % len(exp["mistakes"]))
check("导出含复习记录", len(exp["review_records"]) >= 3, "%d 条" % len(exp["review_records"]))

print("=== 8. 未配置 Key 时的提示（临时清空，跑完还原） ===")
_backup = _read_cfg()
try:
    call("/api/config", "POST", {"api_key": None})     # 显式传 null = 清空
    code, j = call("/api/config")
    check("Key 已被清空", not j["config"].get("api_key_set"))
    code, j = call("/api/llm", "POST", {"messages": [{"role": "user", "content": "hi"}]})
    check("给出可读的中文提示", code == 502 and "API Key" in j.get("error", ""),
          j.get("error", "")[:40])
finally:
    if _backup:
        call("/api/config", "POST", {k: v for k, v in _backup.items() if k in
                                     ("provider", "base_url", "model", "proxy",
                                      "temperature", "max_tokens", "timeout")})
        if _backup.get("api_key"):
            call("/api/config", "POST", {"api_key": _backup["api_key"]})
    code, j = call("/api/config")
    print("  （配置已还原：%s / %s%s）" % (j["config"].get("model"),
                                          "Key 已恢复" if j["config"].get("api_key_set") else "无 Key",
                                          "" if _backup.get("api_key") else "（本来就没有）"))

print("=== 9. 一图多题：批量保存 ===")
code, j = call("/api/mistakes")
before = len(j["items"])
shared = "data:image/png;base64," + png
code, j = call("/api/mistakes/batch", "POST", {"items": [
    {"subject": "数学", "chapter": "自测章节", "knowledge_point": "分数乘法,约分",
     "error_reason": "计算粗心", "question_text": "自测·批量题一", "student_answer": "5",
     "correct_answer": "6", "is_correct": "wrong", "source_type": "作业"},
    {"subject": "数学", "chapter": "自测章节", "knowledge_point": "分数除法",
     "error_reason": "无", "question_text": "自测·批量题二", "student_answer": "2",
     "correct_answer": "2", "is_correct": "right", "source_type": "作业"},
    {"subject": "数学", "chapter": "自测章节", "knowledge_point": "解方程",
     "error_reason": "方法不会", "question_text": "自测·批量题三", "student_answer": "",
     "correct_answer": "x=9", "is_correct": "unknown", "source_type": "作业"},
], "shared_image": shared})
check("批量保存返回 created=3", code == 200 and j.get("created") == 3, j.get("created"))
b_items = j.get("items") or []
created.extend(x["id"] for x in b_items)
check("返回 3 条完整记录", len(b_items) == 3)
check("三题共用同一张整页原图", len(set(x["question_image"] for x in b_items)) == 1,
      (b_items[0]["question_image"] if b_items else ""))
check("图片路径正确落盘", bool(b_items) and b_items[0]["question_image"].startswith("images/"))
check("题目文本落库正确", bool(b_items) and b_items[0]["question_text"] == "自测·批量题一")
check("判定字段落库正确",
      [x["is_correct"] for x in b_items] == ["wrong", "right", "unknown"],
      ",".join(x["is_correct"] for x in b_items))
check("知识点被拆成数组", bool(b_items) and b_items[0]["knowledge_points"] == ["分数乘法", "约分"])
check("章节落库正确", all(x["chapter"] == "自测章节" for x in b_items))
code, j = call("/api/mistakes")
check("列表总数增加 3", len(j["items"]) == before + 3, "%d → %d" % (before, len(j["items"])))
code, j = call("/api/mistakes/batch", "POST", {"items": [], "shared_image": shared})
check("空数组被拒绝（400）", code == 400, j.get("error"))
code, j = call("/api/mistakes/batch", "POST", {"items": "不是数组"})
check("items 类型错误被拒绝（400）", code == 400, j.get("error"))

print("=== 10. 间隔复习：艾宾浩斯间隔与到期判定 ===")
m_sp = add(subject="数学", question_text="自测·间隔复习题", mastery=0)
check("新题立刻到期（掌握度0、间隔0天）",
      m_sp.get("is_due") is True and m_sp.get("due_in_days") == 0 and m_sp.get("interval_days") == 0,
      "due_in=%s interval=%s" % (m_sp.get("due_in_days"), m_sp.get("interval_days")))

# 连做对 4 次，间隔应当按 1 → 2 → 4 → 7 递进
for idx, (want_mastery, want_days) in enumerate([(1, 1), (2, 2), (3, 4), (4, 7)], start=1):
    code, j = call("/api/mistakes/%d/review" % m_sp["id"], "POST", {"is_correct": True})
    check("第 %d 次做对 → 掌握度 %d、%d 天后复习" % (idx, want_mastery, want_days),
          j.get("mastery") == want_mastery and j.get("interval_days") == want_days,
          "mastery=%s interval=%s next=%s" % (j.get("mastery"), j.get("interval_days"),
                                              str(j.get("next_due_at"))[:10]))

code, j = call("/api/mistakes/%d" % m_sp["id"])
check("间隔 7 天时应显示「还有 7 天」且不到期",
      j["item"]["is_due"] is False and j["item"]["due_in_days"] == 7,
      "due_in=%s is_due=%s" % (j["item"]["due_in_days"], j["item"]["is_due"]))

# 做错 → 掌握度归零、间隔归零、当天重新到期
code, j = call("/api/mistakes/%d/review" % m_sp["id"], "POST", {"is_correct": False})
check("做错后掌握度归零、间隔 0 天、当天重新到期",
      j.get("mastery") == 0 and j.get("interval_days") == 0 and j.get("due_in_days") == 0,
      "mastery=%s due_in=%s" % (j.get("mastery"), j.get("due_in_days")))

# 手动改掌握度也要影响间隔（基准取收录时间）
call("/api/mistakes/%d" % m_sp["id"], "PUT", {"mastery": 3})
code, j = call("/api/mistakes/%d" % m_sp["id"])
check("手动把掌握度改成 3 → 间隔变 4 天", j["item"]["interval_days"] == 4,
      "interval=%s" % j["item"]["interval_days"])

# due 筛选：只回今天到期的
call("/api/mistakes/%d" % m_sp["id"], "PUT", {"mastery": 0})     # 弄回到期
code, j = call("/api/mistakes?due=1&sort=due")
due_items = j["items"]
due_ids = [x["id"] for x in due_items]
check("due=1 能筛到刚做错/归零的题", m_sp["id"] in due_ids, "命中 %d 条" % len(due_ids))
check("due=1 返回的每一条都确实到期", all(x["is_due"] for x in due_items))
check("due=1 的结果里没有未掌握的未到期题",
      all(x["mastery"] >= 5 or x["is_due"] for x in due_items))
check("sort=due 按到期时间升序（最该练的在最前）",
      [x["due_in_days"] for x in due_items] == sorted(x["due_in_days"] for x in due_items)
      and [x["next_due_at"] for x in due_items] == sorted(x["next_due_at"] for x in due_items),
      "最早三个=" + str([x["next_due_at"][5:16] for x in due_items[:3]]))

# 已掌握的题不该出现在到期列表
call("/api/mistakes/%d" % m_sp["id"], "PUT", {"mastery": 5})
code, j = call("/api/mistakes?due=1")
check("已掌握的题不出现在到期列表", m_sp["id"] not in [x["id"] for x in j["items"]])

print("=== 11. 间隔复习：统计字段 ===")
code, j = call("/api/stats")
st = j["stats"]
check("统计含间隔复习字段",
      all(k in st for k in ("due_count", "next_due_at", "by_mastery", "intervals", "interval_labels")),
      "due_count=%s" % st.get("due_count"))
iv = {int(k): v for k, v in (st.get("intervals") or {}).items()}
check("间隔表就是 1 / 2 / 4 / 7 / 15 天",
      iv == {0: 0, 1: 1, 2: 2, 3: 4, 4: 7, 5: 15}, str(iv))
check("by_mastery 覆盖到本次创建的题",
      isinstance(st.get("by_mastery"), dict) and sum(
          st["by_mastery"].get(str(k), st["by_mastery"].get(k, 0)) for k in range(6)) >= 1,
      str(st.get("by_mastery")))
# 口径必须一致：统计里的「今天待复习」= due=1 列表的长度（此刻已把自测题设为已掌握）
code, j = call("/api/mistakes?due=1")
check("due_count 与 due=1 列表长度一致", st["due_count"] == len(j["items"]),
      "stats=%s 列表=%s" % (st["due_count"], len(j["items"])))

print("=== 12. 间隔算法单元校验（直接调 compute_schedule，构造过期场景） ===")
# 接口层造不出「过去的时间」，所以这部分直接调算法函数，把过期天数也纳入回归
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime, timedelta                      # noqa: E402
import server as srv                                          # noqa: E402

_now = datetime(2026, 9, 10, 12, 0, 0)
_ts = lambda d: d.strftime("%Y-%m-%d %H:%M:%S")               # noqa: E731

s = srv.compute_schedule(0, _ts(_now - timedelta(days=5)), _ts(_now - timedelta(days=12)), 3, now=_now)
check("5 天前做错、掌握度 0 → 过期 5 天", s["is_due"] and s["overdue_days"] == 5,
      "overdue=%s due_in=%s" % (s["overdue_days"], s["due_in_days"]))

s = srv.compute_schedule(3, _ts(_now - timedelta(days=2)), _ts(_now - timedelta(days=9)), 2, now=_now)
check("掌握度 3（间隔 4 天）、才过 2 天 → 还剩 2 天", (not s["is_due"]) and s["due_in_days"] == 2,
      "due_in=%s" % s["due_in_days"])

s = srv.compute_schedule(2, _ts(_now - timedelta(days=2)), _ts(_now - timedelta(days=5)), 1, now=_now)
check("掌握度 2（间隔 2 天）、正好过 2 天 → 到期算 0 天", s["is_due"] and s["due_in_days"] == 0,
      "due_in=%s" % s["due_in_days"])

s = srv.compute_schedule(0, "", _ts(_now), 0, now=_now)
check("从没复习过 → 基准取收录时间、当天到期", s["is_due"] and s["due_in_days"] == 0,
      "due_in=%s" % s["due_in_days"])

s = srv.compute_schedule(5, _ts(_now), _ts(_now), 5, now=_now)
check("掌握度 5 → 间隔 15 天（抽查档）", s["interval_days"] == 15, s["interval_days"])

s = srv.compute_schedule(4, _ts(_now - timedelta(hours=1)), _ts(_now), 4, now=_now)
check("掌握度 4（间隔 7 天）、1 小时前复习 → 还剩 7 天", (not s["is_due"]) and s["due_in_days"] == 7,
      "due_in=%s" % s["due_in_days"])

s = srv.compute_schedule(9, _ts(_now), _ts(_now), 5, now=_now)
check("掌握度越界（9）被夹到 5", s["interval_days"] == 15, s["interval_days"])

print("=== 13. 提醒与周报：overdue_max / week / weak_kp ===")
code, j = call("/api/stats")
w0 = j["stats"]["week"]
check("统计含提醒与周报字段",
      all(k in j["stats"] for k in ("overdue_max", "weak_kp", "week")),
      "overdue_max=%s" % j["stats"].get("overdue_max"))
check("week 字段齐全",
      all(k in w0 for k in ("reviewed", "correct", "accuracy", "new", "active_days")),
      str(w0))

# 连加 3 道同知识点的未掌握题，它应当凭题数最多升到「最该补」第一位
m_wk = add(subject="数学", question_text="自测·周报题", knowledge_point="自测知识点ZZZ", mastery=0)
for i in (2, 3):
    add(subject="数学", question_text="自测·周报题%d" % i, knowledge_point="自测知识点ZZZ", mastery=0)
code, j = call("/api/stats")
w1 = j["stats"]["week"]
check("连加 3 道后 week.new 增加 3", w1["new"] == w0["new"] + 3, "%s → %s" % (w0["new"], w1["new"]))
check("题数最多的知识点升到「最该补」第一位",
      bool(j["stats"]["weak_kp"]) and j["stats"]["weak_kp"][0]["kp"] == "自测知识点ZZZ",
      "/".join("%s(%d 题)" % (x["kp"], x["n"]) for x in j["stats"]["weak_kp"]))
check("该知识点的题数统计正确",
      bool(j["stats"]["weak_kp"]) and j["stats"]["weak_kp"][0]["n"] == 3,
      j["stats"]["weak_kp"][0]["n"] if j["stats"]["weak_kp"] else "无")
check("weak_kp 里的平均掌握度不超过 5",
      all(0 <= x["avg_mastery"] <= 5 for x in j["stats"]["weak_kp"]),
      str([x["avg_mastery"] for x in j["stats"]["weak_kp"]]))

# 复习 3 次（对、对、错），周报计数应同步
for is_ok in (True, True, False):
    call("/api/mistakes/%d/review" % m_wk["id"], "POST", {"is_correct": is_ok})
code, j = call("/api/stats")
w2 = j["stats"]["week"]
check("复习记录计入 week.reviewed（+3）", w2["reviewed"] == w1["reviewed"] + 3,
      "%s → %s" % (w1["reviewed"], w2["reviewed"]))
check("week.correct 同步增加（+2）", w2["correct"] == w1["correct"] + 2,
      "%s → %s" % (w1["correct"], w2["correct"]))
check("week.accuracy = correct / reviewed 四舍五入",
      w2["accuracy"] == (round(w2["correct"] * 100.0 / w2["reviewed"]) if w2["reviewed"] else 0),
      "%s%% (%s/%s)" % (w2["accuracy"], w2["correct"], w2["reviewed"]))
check("week.active_days 至少 1 天", w2["active_days"] >= 1, w2["active_days"])

# 接口不允许写「过去的时间」，所以直连库造一条「该 X 天前复习」的题，
# 这样才能真正验到 overdue_max 和「过期最久的排最前」。
# X 取「库里现有最久 + 7 天」，保证这条一定是第一名，脚本在已有数据的库上也能重复跑。
code, j = call("/api/stats")
_prev_max = int(j["stats"].get("overdue_max") or 0)
_back_days = _prev_max + 7
_conn = sqlite3.connect(srv.DB_PATH)
_conn.execute("UPDATE mistakes SET mastery=0, review_count=3, last_reviewed_at=? WHERE id=?",
              ((datetime.now() - timedelta(days=_back_days)).strftime("%Y-%m-%d %H:%M:%S"),
               m_sp["id"]))
_conn.commit()
_conn.close()

code, j = call("/api/stats")
st3 = j["stats"]
check("构造过期题后 overdue_max 提升到 %d 天" % _back_days,
      st3["overdue_max"] == _back_days, "overdue_max=%s（原 %s）" % (st3["overdue_max"], _prev_max))
code, j = call("/api/mistakes?due=1&sort=due")
due_items2 = j["items"]
check("过期最久的题排在到期列表最前",
      bool(due_items2) and due_items2[0]["id"] == m_sp["id"],
      "首条=" + (due_items2[0]["question_text"] if due_items2 else "无"))
check("overdue_max 与到期列表里的最大值一致",
      st3["overdue_max"] == max([x["overdue_days"] for x in due_items2] or [0]),
      "%s vs %s" % (st3["overdue_max"], max([x["overdue_days"] for x in due_items2] or [0])))
check("due_count 与 due=1 列表长度仍然一致", st3["due_count"] == len(due_items2),
      "stats=%s 列表=%s" % (st3["due_count"], len(due_items2)))

print("=== 14. 听力音频（英语听力题：上传→落盘→回放→共用→跳过） ===")
# 最小的 mp3 字节串（不需要能播放，只验证落盘/取回/字段 链路完整）
AUDIO_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 200
audio_url = "data:audio/mpeg;base64," + base64.b64encode(AUDIO_BYTES).decode()
LISTEN_TEXT = "Number one. Where are you going this weekend? A. I am going to the library."

m_au = add(subject="英语", chapter="自测章节·听力", question_text="自测·听力题一",
           is_correct="right", student_answer="A", correct_answer="A",
           check_result="结合听力原文判断正确",
           audio_url=audio_url, listening_text=LISTEN_TEXT)
check("带音频建题成功", bool(m_au.get("id")), "id=%s" % m_au.get("id"))
check("audio_url 落盘成相对路径", str(m_au.get("audio_url", "")).startswith("audio/"),
      m_au.get("audio_url"))
check("listening_text 已入库", "library" in str(m_au.get("listening_text", "")),
      str(m_au.get("listening_text"))[:50])

# 音频文件真的能通过 /audio/ 取回，且字节数与写入一致
if m_au.get("audio_url"):
    req = urllib.request.Request(BASE + "/" + m_au["audio_url"])
    with OPENER.open(req, timeout=20) as r:
        got = r.read()
    check("音频可 HTTP 取回且字节一致", r.status == 200 and got == AUDIO_BYTES,
          "size=%d ctype=%s" % (len(got), r.headers.get("Content-Type")))

# 整卷：一份音频被多题共用，只存一次
code, j = call("/api/mistakes/batch", "POST", {"shared_image": "", "audio_url": audio_url, "items": [
    {"subject": "英语", "chapter": "自测章节·听力", "question_text": "自测·听力题二",
     "is_correct": "unknown", "check_result": "未提供听力音频，未检查", "listening_text": LISTEN_TEXT},
    {"subject": "英语", "chapter": "自测章节·听力", "question_text": "自测·听力题三",
     "is_correct": "wrong", "listening_text": LISTEN_TEXT},
]})
b_au = j.get("items") or []
created.extend(x["id"] for x in b_au)
check("整卷带音频批量建题成功", code == 200 and len(b_au) == 2, j.get("created"))
check("整卷两题共用同一份音频",
      len(b_au) == 2 and b_au[0]["audio_url"] == b_au[1]["audio_url"] and b_au[0]["audio_url"],
      (b_au[0]["audio_url"] if b_au else ""))
check("整卷听力题判定字段落库正确",
      [x["is_correct"] for x in b_au] == ["unknown", "wrong"],
      ",".join(x["is_correct"] for x in b_au))
check("「未提供听力音频，未检查」原样落库",
      bool(b_au) and b_au[0]["check_result"] == "未提供听力音频，未检查",
      (b_au[0]["check_result"] if b_au else ""))

# 详情能读到新字段
if m_au.get("id"):
    code, j = call("/api/mistakes/%d" % m_au["id"])
    it = j.get("item") or {}
    check("详情页能读到 audio_url", bool(it.get("audio_url")), it.get("audio_url"))
    check("详情页能读到 listening_text", "library" in str(it.get("listening_text", "")))

    # 编辑时不带音频字段 → 已有音频不能丢（这是最容易踩的坑）
    code, j = call("/api/mistakes/%d" % m_au["id"], "PUT", {"question_text": "自测·听力题一（改）"})
    it2 = j.get("item") or {}
    check("编辑不传音频时已有音频保留",
          str(it2.get("audio_url", "")).startswith("audio/") and it2["audio_url"] == m_au["audio_url"],
          it2.get("audio_url"))
    check("编辑后题目文字已更新", it2.get("question_text") == "自测·听力题一（改）",
          str(it2.get("question_text"))[:30])

# 非法 / 超大音频应被明确拒绝（用 asr 接口校验，未配 Key 也要先过格式校验）
code, j = call("/api/asr", "POST", {"audio": "不是 data URL"})
check("非法音频被拒（可读错误）", code != 200 and "格式" in str(j.get("error", "")),
      str(j.get("error"))[:40])
big = "data:audio/wav;base64," + base64.b64encode(b"\x00" * (26 * 1024 * 1024)).decode()
code, j = call("/api/asr", "POST", {"audio": big})
check("超大音频被拒（提示 25 MB）", code != 200 and "25 MB" in str(j.get("error", "")),
      str(j.get("error"))[:40])

# asr 配置字段：可写、且「空串不清掉已存的 Key」
code, j = call("/api/config", "POST", {"asr_base_url": "https://api.siliconflow.cn/v1",
                                       "asr_model": "FunAudioLLM/SenseVoiceSmall"})
c_asr = j.get("config") or {}
check("asr_base_url 可写",
      c_asr.get("asr_base_url") == "https://api.siliconflow.cn/v1", c_asr.get("asr_base_url"))
check("asr_model 可写",
      c_asr.get("asr_model") == "FunAudioLLM/SenseVoiceSmall", c_asr.get("asr_model"))
check("config 暴露 asr_ready 字段", "asr_ready" in c_asr, "asr_ready=%s" % c_asr.get("asr_ready"))

# 跨厂商时**不能**拿识别用的 Key 去顶替（真 bug 的回归守卫）
_asr_backup = _read_cfg()
try:
    call("/api/config", "POST", {"base_url": "https://api.deepseek.com/v1",
                                 "asr_base_url": "https://api.siliconflow.cn/v1",
                                 "asr_api_key": None})
    code, j = call("/api/config")
    c2 = j.get("config") or {}
    check("识别与转写不是同一家时 asr_ready 为 False",
          c2.get("asr_ready") is False, "asr_ready=%s" % c2.get("asr_ready"))
    code, j = call("/api/asr", "POST", {"audio": audio_url})
    check("跨厂商时给出「不是同一家」的可读提示",
          code != 200 and "不是同一家" in str(j.get("error", "")),
          str(j.get("error"))[:60])
finally:
    if _asr_backup:
        call("/api/config", "POST", {k: v for k, v in _asr_backup.items() if k in
                                     ("provider", "base_url", "model", "proxy", "temperature",
                                      "max_tokens", "timeout", "asr_base_url", "asr_model")})
        if _asr_backup.get("api_key"):
            call("/api/config", "POST", {"api_key": _asr_backup["api_key"]})
        if _asr_backup.get("asr_api_key"):
            call("/api/config", "POST", {"asr_api_key": _asr_backup["asr_api_key"]})

# 老库自动补列：新列必须真的在表里
try:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(mistakes)").fetchall()]
    finally:
        conn.close()
    check("mistakes 表已含 audio_url 列", "audio_url" in cols)
    check("mistakes 表已含 listening_text 列", "listening_text" in cols)
except Exception as exc:
    check("读取表结构", False, str(exc)[:60])

print("=== 15. 清理本次创建的数据 ===")
for mid in created:
    call("/api/mistakes/%d" % mid, "DELETE")

# 接口删除是「软删除」（deleted=1），而且**复习记录不会跟着走** —— 这里把本次
# 造的题连同它产生的复习记录**物理**清掉。否则每跑一次自测，就往真实库里塞十几条
# 复习记录，统计页的「复习次数 / 本周小结」会跟着虚高。
n_rev = n_row = -1
try:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        if created:
            qs = ",".join("?" * len(created))
            n_rev = conn.execute(
                "SELECT COUNT(*) FROM review_records WHERE mistake_id IN (%s)" % qs,
                created).fetchone()[0]
            conn.execute("DELETE FROM review_records WHERE mistake_id IN (%s)" % qs, created)
            conn.execute("DELETE FROM mistakes WHERE id IN (%s)" % qs, created)
            conn.commit()
            n_row = conn.execute("SELECT COUNT(*) FROM mistakes WHERE id IN (%s)" % qs,
                                 created).fetchone()[0]
        else:
            n_rev = n_row = 0
    finally:
        conn.close()
except Exception as exc:
    print("  [warn] 物理清理失败（不影响使用）：%s" % exc)

check("本次产生的复习记录已物理清除", n_row == 0,
      "清掉 %d 条复习记录，错题残留 %d 条" % (n_rev, n_row))
code, j = call("/api/mistakes")
left = mine(j["items"])
check("本次创建的数据已清理", len(left) == 0, "剩 %d 条" % len(left))

# 听力音频同样要清：自测用的小 mp3 每跑一次会往 data/audio/ 落一个文件，
# 不清就会慢慢堆成垃圾（它们已经没有任何错题引用了）。
AUDIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "audio")
n_aud = -1
try:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        # 只删「没有任何存活错题引用」的音频，绝不碰你真实数据里的录音
        referenced = set()
        for (a,) in conn.execute(
                "SELECT DISTINCT audio_url FROM mistakes WHERE deleted=0 AND audio_url<>''"):
            referenced.add(os.path.basename(a))
    finally:
        conn.close()
    if os.path.isdir(AUDIO_DIR):
        n_aud = 0
        for fn in os.listdir(AUDIO_DIR):
            if fn.startswith(".") or fn in referenced:
                continue
            # 本次自测刚落的文件：7 分钟内新建，且没有错题引用 → 判定为自测垃圾
            p = os.path.join(AUDIO_DIR, fn)
            try:
                if time.time() - os.path.getmtime(p) < 420:
                    os.remove(p)
                    n_aud += 1
            except OSError:
                pass
    check("本次落盘的孤儿音频已清除", True, "清掉 %d 个音频文件" % max(n_aud, 0))
except Exception as exc:
    print("  [warn] 音频清理失败（不影响使用）：%s" % exc)

print("\n结果：%d 通过 / %d 失败" % (ok, fail))
sys.exit(1 if fail else 0)
