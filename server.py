#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
错题本 · 本地服务端
================================================================
只用 Python 标准库，不需要安装任何第三方包。
按方案文档实现：SQLite 本地存储（数据不出家门）+ 多模态大模型一步完成
「识别 + 讲解 + 打标签」，识别模块独立封装，后续可无痛替换成 PaddleOCR。

启动：双击「启动.bat」，或在本目录执行  python server.py
"""

import base64
import hashlib
import io
import json
import math
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import sys
import threading
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse

# ----------------------------------------------------------------------------
# 路径与常量
# ----------------------------------------------------------------------------
# 冻结（PyInstaller exe）与非冻结（python server.py）两栖：
#   · 数据（data/、config.json）永远跟着 exe/脚本所在目录走 —— 「拷文件夹=换机」不变；
#   · 只读资源（web/、segno/）打包后住在 _internal（sys._MEIPASS）里，从那儿读。
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    ASSET_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
    if ASSET_DIR not in sys.path:
        sys.path.insert(0, ASSET_DIR)      # 让 --add-data 进包的 segno（namespace 包）能被 import
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    ASSET_DIR = BASE_DIR
WEB_DIR = os.path.join(ASSET_DIR, "web")
DATA_DIR = os.path.join(BASE_DIR, "data")
IMAGE_DIR = os.path.join(DATA_DIR, "images")
AUDIO_DIR = os.path.join(DATA_DIR, "audio")   # 听力音频存放目录
DB_PATH = os.path.join(DATA_DIR, "app.db")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DRAFT_PATH = os.path.join(DATA_DIR, "draft.json")   # 未点「存入错题本」的识别结果（草稿）
DRAFT_IMG_PREFIX = "draft-"                         # 草稿原图文件名前缀（仍放在 data/images/ 下）
# 本实例「数据目录」的指纹：只暴露一段哈希，用来让自检/测试脚本确认
# 「我连上的这个服务，用的确实是我这个目录的数据」。
# 起因：selftest.py 早期把地址写死 127.0.0.1:8765，一旦本机另有实例在跑，
# 自测数据就会写进**别人正在用的库**，事后很难察觉。
DIR_ID = hashlib.md5(DATA_DIR.encode("utf-8")).hexdigest()[:12]
PORT = int(os.environ.get("CTB_PORT", "8765"))
VERSION = "1.18.0"
MAX_BATCH_ITEMS = 40          # 一图多题：单次最多保存多少道题
MAX_AUDIO_MB = 25             # 单个听力音频上限（MB），超过直接拒绝，避免把请求撑爆
DRAFT_MAX_ITEMS = 40          # 草稿最多存多少道题（与 MAX_BATCH_ITEMS 对齐）
DRAFT_MAX_BYTES = 12 * 1024 * 1024   # 草稿 JSON 上限（防误传超大 payload 把磁盘写满）
DRAFT_TEXT_CAP = 8000         # 草稿里每个文本字段的截断长度

# --------------------------------------------------------------------------
# 间隔复习（艾宾浩斯遗忘曲线，简化版）
# --------------------------------------------------------------------------
# 掌握度 → 下次复习间隔（天）。做对一次掌握度 +1，间隔就往后拉一档；
# 做错归零，立刻回到「今天就要再练」。这是最经典的一条曲线：
#   1 天 → 2 天 → 4 天 → 7 天 → 15 天
# 已掌握（5）默认不进复习池，但仍保留 15 天的抽查间隔，
# 万一哪天要「抽已掌握的题复查」可以直接用。
SPACED_INTERVALS = {0: 0, 1: 1, 2: 2, 3: 4, 4: 7, 5: 15}

# 间隔的对外解释，前端直接拿去展示，不用两边各写一份
SPACED_LABELS = {
    0: "今天再来一遍",
    1: "明天复习",
    2: "2 天后复习",
    3: "4 天后复习",
    4: "7 天后复习",
    5: "15 天后抽查（已掌握）",
}

TS_FMT = "%Y-%m-%d %H:%M:%S"

# 上面那套算法的 SQL 等价写法：把间隔表翻译成 CASE，基准时间取「上次复习」否则「收录时间」。
# 有了它才能在数据库里直接做「只取今天到期的」筛选和「到期优先」排序，
# 不用把所有行拉到 Python 里再算。注意 SQLite 的 datetime('now') 是 UTC，
# 而 now_str() 存的是本地时间，所以比较时必须带 'localtime'。
SQL_DUE_AT = (
    "datetime(COALESCE(NULLIF(m.last_reviewed_at,''), m.created_at), '+' || ("
    "CASE CAST(COALESCE(m.mastery,0) AS INTEGER) "
    "WHEN 0 THEN 0 WHEN 1 THEN 1 WHEN 2 THEN 2 "
    "WHEN 3 THEN 4 WHEN 4 THEN 7 ELSE 15 END"
    ") || ' days')"
)
SQL_NOW_LOCAL = "datetime('now','localtime')"

DEFAULT_CONFIG = {
    "provider": "siliconflow",
    "base_url": "https://api.siliconflow.cn/v1",
    "model": "Qwen/Qwen3-VL-235B-A22B-Instruct",
    "api_key": "",
    "proxy": "",
    "temperature": 0.2,
    "max_tokens": 2000,
    "timeout": 180,
    "budget": 0,   # 用户在设置页调的 Token 预算下限，0=自动
    # --- 听力音频转写（ASR）---
    # DeepSeek 不支持音频输入，所以听力音频先用这套「语音转文字」接口转成文本，
    # 再把文本连同题目照片一起发给检查模型。硅基流动的 SenseVoiceSmall 是免费的，
    # 且接口是 OpenAI 兼容的 multipart，纯标准库就能调，不引入任何依赖。
    "asr_base_url": "https://api.siliconflow.cn/v1",
    "asr_model": "FunAudioLLM/SenseVoiceSmall",
    "asr_api_key": "",     # 留空则回退用 api_key（同一家的 Key 可以直接共用）
}

SEED_SUBJECTS = [("语文", 1), ("数学", 2), ("英语", 3), ("科学", 4)]

_db_lock = threading.Lock()


# ----------------------------------------------------------------------------
# 配置读写
# ----------------------------------------------------------------------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f) or {})
        except Exception as exc:
            print("[warn] config.json 读取失败：%s" % exc)
    # 环境变量兜底
    if os.environ.get("CTB_API_KEY"):
        cfg["api_key"] = os.environ["CTB_API_KEY"]
    return cfg


def save_config(patch):
    cfg = load_config()
    for key, value in (patch or {}).items():
        if key in DEFAULT_CONFIG:
            cfg[key] = value
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def public_config(cfg=None):
    cfg = load_config() if cfg is None else cfg
    key = (cfg.get("api_key") or "").strip()
    masked = ""
    if key:
        masked = key[:6] + "*" * 6 + key[-4:] if len(key) > 14 else "*" * len(key)
    # ASR（语音转文字）用的是另一套地址与 Key，单独对外暴露状态
    asr_key = (cfg.get("asr_api_key") or "").strip()
    asr_base = (cfg.get("asr_base_url") or "").rstrip("/")
    # 没单独填 ASR Key 时，只有「主接口和转写接口同一家」才允许共用主 Key
    asr_shared = bool(not asr_key and key and _same_host(cfg.get("base_url") or "", asr_base))
    asr_effective = asr_key or (key if asr_shared else "")
    asr_masked = ""
    if asr_key:
        asr_masked = asr_key[:6] + "*" * 6 + asr_key[-4:] if len(asr_key) > 14 else "*" * len(asr_key)
    return {
        "provider": cfg.get("provider", ""),
        "base_url": cfg.get("base_url", ""),
        "model": cfg.get("model", ""),
        "proxy": cfg.get("proxy", ""),
        "temperature": cfg.get("temperature", 0.2),
        "max_tokens": cfg.get("max_tokens", 2000),
        "budget": cfg.get("budget", 0),
        "api_key_masked": masked,
        "api_key_set": bool(key),
        "asr_base_url": cfg.get("asr_base_url", ""),
        "asr_model": cfg.get("asr_model", ""),
        "asr_api_key_masked": asr_masked,
        "asr_api_key_set": bool(asr_key),
        # 是否正与主 Key 共用（前端据此提示"复用上面那个主 Key"；不同厂商时为 False，
        # 此时即使主 Key 有值也不能用，必须单独填）
        "asr_shared": asr_shared,
        # 能不能真的转写：只要「回退后的 Key」非空就算可用（前端据此决定是否放行听力检查）
        "asr_ready": bool(asr_effective),
    }


# ----------------------------------------------------------------------------
# 数据库
# ----------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS subjects (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL UNIQUE,
  sort_order  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mistakes (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_id        INTEGER REFERENCES subjects(id),
  chapter           TEXT DEFAULT '',
  knowledge_point   TEXT DEFAULT '',
  error_reason      TEXT DEFAULT '',
  reason_tags       TEXT DEFAULT '',
  question_image    TEXT DEFAULT '',
  question_text     TEXT DEFAULT '',
  clean_text        TEXT DEFAULT '',
  clean_at          TEXT DEFAULT '',
  student_answer    TEXT DEFAULT '',
  correct_answer    TEXT DEFAULT '',
  explanation       TEXT DEFAULT '',
  similar_question  TEXT DEFAULT '',
  check_result      TEXT DEFAULT '',
  is_correct        TEXT DEFAULT 'unknown',
  mastery           INTEGER DEFAULT 0,
  source_type       TEXT DEFAULT '作业',
  audio_url         TEXT DEFAULT '',
  listening_text    TEXT DEFAULT '',
  review_count      INTEGER DEFAULT 0,
  last_reviewed_at  TEXT DEFAULT '',
  created_at        TEXT,
  updated_at        TEXT,
  deleted           INTEGER DEFAULT 0,
  mark_delete       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS review_records (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  mistake_id   INTEGER NOT NULL REFERENCES mistakes(id),
  reviewed_at  TEXT,
  is_correct   INTEGER DEFAULT 0,
  note         TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_mistakes_subject ON mistakes(subject_id);
CREATE INDEX IF NOT EXISTS idx_mistakes_mastery ON mistakes(mastery);
CREATE INDEX IF NOT EXISTS idx_review_mistake ON review_records(mistake_id);
"""


def _schema_columns(schema):
    """从 SCHEMA 里解析出「表名 -> [(列名, 类型+默认值)]」，供自动补列用。"""
    tables = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\n\);", schema, re.S):
        table, cols = m.group(1), []
        for raw in m.group(2).split("\n"):
            line = raw.strip().rstrip(",")
            if not line:
                continue
            head = line.split()[0].lower()
            if head in ("primary", "foreign", "unique", "check", "constraint"):
                continue
            mm = re.match(r"^(\w+)\s+(\w+)(.*)$", line, re.S)
            if not mm:
                continue
            name, dtype, rest = mm.group(1), mm.group(2), mm.group(3)
            dm = re.search(r"DEFAULT\s+(.+?)(?=\s+(?:NOT|UNIQUE|REFERENCES|CHECK)\b|$)",
                           rest, re.I)
            default = dm.group(1).strip() if dm else ""
            # ALTER TABLE ADD COLUMN 不允许 NOT NULL 无默认值，这里统一只带类型 + 默认值
            tables.setdefault(table, []).append((name, (dtype + (" DEFAULT " + default if default else "")).strip()))
    return tables


SCHEMA_COLUMNS = _schema_columns(SCHEMA)


def migrate_db(conn):
    """旧数据库自动升级：SCHEMA 里新增的列，就地补上（**已有数据和数据行全部保留**）。

    没有这段，把新版程序复制到「已有旧 app.db」的电脑上时，旧库缺少新列会直接报
    "no such column" 打不开错题数据；有了它，拷贝程序过去后首次启动即自动对齐。
    """
    upgraded = []
    for table, cols in SCHEMA_COLUMNS.items():
        try:
            have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        except sqlite3.Error:
            continue
        if not have:                     # 表还没建出来（正常不会发生）
            continue
        for name, ddl in cols:
            if name in have:
                continue
            try:
                conn.execute('ALTER TABLE "%s" ADD COLUMN "%s" %s' % (table, name, ddl))
                upgraded.append("%s.%s" % (table, name))
            except sqlite3.Error as exc:
                print("[warn] 数据库升级失败（%s.%s）：%s" % (table, name, exc))
    if upgraded:
        print("[ok] 数据库已升级，新增字段：%s" % "、".join(upgraded))
    return upgraded


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    os.makedirs(AUDIO_DIR, exist_ok=True)      # 听力音频目录（可为空）
    with connect() as conn:
        conn.executescript(SCHEMA)
        migrate_db(conn)               # 旧库补列：保证新版程序能打开老数据
        for name, order in SEED_SUBJECTS:
            conn.execute(
                "INSERT OR IGNORE INTO subjects(name, sort_order) VALUES(?,?)", (name, order)
            )
    print("[ok] 数据库就绪：%s" % DB_PATH)
    if not os.path.exists(CONFIG_PATH):
        save_config({})
        print("[ok] 已生成配置文件：%s" % CONFIG_PATH)


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def subject_id_of(conn, name):
    name = (name or "").strip() or "未分类"
    row = conn.execute("SELECT id FROM subjects WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    order = conn.execute("SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM subjects").fetchone()["n"]
    cur = conn.execute("INSERT INTO subjects(name, sort_order) VALUES(?,?)", (name, order))
    return cur.lastrowid


def kp_to_list(text):
    if not text:
        return []
    return [p.strip() for p in re.split(r"[,，、;；/|]+", text) if p.strip()]


def parse_ts(text):
    """把库里存的时间字符串解析成 datetime；解析不了就返回 None。"""
    if not text:
        return None
    text = str(text).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19] if len(text) >= 19 else text, fmt)
        except ValueError:
            continue
    return None


def compute_schedule(mastery, last_reviewed_at, created_at, review_count=0, now=None):
    """
    按艾宾浩斯间隔算出这道题「下次该什么时候复习」。

    规则：
      · 间隔只由掌握度决定：0→0天、1→1天、2→2天、3→4天、4→7天、5→15天
      · 基准时间 = 上次复习时间；从没复习过就用收录时间
        （所以新错题掌握度 0、间隔 0 天 → 当天立刻进入复习池）
      · 做错过 → 掌握度归 0、间隔 0 天，当天重新到期
    返回 dict，直接挂到错题对象上给前端用。
    """
    now = now or datetime.now()
    mastery = max(0, min(5, int(mastery or 0)))
    days = SPACED_INTERVALS.get(mastery, 0)

    base = parse_ts(last_reviewed_at) if review_count else None
    anchor = base or parse_ts(created_at) or now
    due = anchor + timedelta(days=days)

    delta = (due - now).total_seconds() / 86400.0
    # 天数取整要当心浮点边界：刚建出来的题 delta 是 -1e-9 这种极小负数，
    # 直接 floor 会算成「过期 1 天」。未来向上取整（还有 0.5 天 → 说明天到期），
    # 过去向下取整（过期 2.5 天 → 算 2 天）。eps 用来吸掉浮点误差。
    eps = 1e-9
    if delta <= eps:
        due_in_days = -int(math.floor(-delta + eps))
    else:
        due_in_days = int(math.ceil(delta - eps))
    return {
        "next_due_at": due.strftime(TS_FMT),
        "due_in_days": due_in_days,
        "is_due": delta <= 0,
        "interval_days": days,
        "interval_label": SPACED_LABELS.get(mastery, ""),
        "overdue_days": max(0, -due_in_days),
    }


def row_to_mistake(row):
    d = dict(row)
    d["knowledge_points"] = kp_to_list(d.get("knowledge_point"))
    # 标签同时给「串」和「数组」两种形态：前端渲染用数组最省事，
    # 而排错时直接看库里的串更直观。两边都从同一个字典出，不会不一致。
    d["reason_tags"] = norm_tags(d.get("reason_tags"))
    d["tags"] = split_tags(d["reason_tags"])
    d["subject"] = d.get("subject") or "未分类"
    d["mastery"] = int(d.get("mastery") or 0)
    d["review_count"] = int(d.get("review_count") or 0)
    d["deleted"] = int(d.get("deleted") or 0)
    d.update(compute_schedule(
        d["mastery"], d.get("last_reviewed_at"), d.get("created_at"),
        d["review_count"],
    ))
    return d


# ----------------------------------------------------------------------------
# 错因标签（reason_tags）
# ----------------------------------------------------------------------------
# 一道题可以同时贴好几个标签（复选）：例如「审题不清 + 计算粗心」。
# 存储上就是「，」连起来的一串，读的时候统一过 split_tags()/norm_tags()。
#
# 为什么不单建一张标签表：标签是随用随改的短词，不值得联表；
# 串在 mistakes 一行里，导出 / 导入 / 草稿暂存这些环节都是零成本，
# 数据依旧只有一个 app.db（这是「直接复制整个文件夹就能搬走」的前提）。
TAG_SEP = re.compile(r"[,，、;；/|\n\r]+")
TAG_MAX_N = 12        # 一道题最多贴几个标签（再多就不是标签、是小作文了）
TAG_MAX_LEN = 16      # 单个标签最长几个字


def split_tags(value):
    """标签串 / 数组 → 去空后的标签列表（保序、去重、限量）。"""
    if isinstance(value, (list, tuple)):
        parts = [str(x) for x in value]
    else:
        parts = TAG_SEP.split(str(value or ""))
    out = []
    for p in parts:
        t = p.strip().lstrip("#").strip()
        if not t or t in ("无", "无标签", "没有", "none", "None"):
            continue
        if len(t) > TAG_MAX_LEN:
            t = t[:TAG_MAX_LEN]
        if t not in out:
            out.append(t)
    return out[:TAG_MAX_N]


def norm_tags(value):
    """存库前的规范化：数组/各种分隔符 → 统一用「，」连的一串。
    旧标签（2026-10-12 精简前 88 个清单里的词）在这里自动归入新的 20 个标签 ——
    库里的历史数据读出来再存就变成新口径，不用迁移数据。"""
    return "，".join(remap_tags(split_tags(value)))


# 2026-10-12 标签精简：旧标签 → 新标签（与前端 web/index.html 的 TAG_ALIAS 同口径）。
# 不在表里的旧标签保留原样（自定义标签不受影响）。
TAG_REMAP = {
    # → 概念不清
    "概念模糊": "概念不清", "概念不清楚": "概念不清", "概念混淆": "概念不清",
    "定义混淆": "概念不清", "公式记错": "概念不清", "公式用错": "概念不清",
    "定理记错": "概念不清", "单位换算错": "概念不清", "单位错": "概念不清",
    "数量关系搞反": "概念不清", "公式不会用": "概念不清",
    # → 方法不会
    "思路不对": "方法不会", "不会举一反三": "方法不会", "综合题不会拆解": "方法不会",
    "不会画图分析": "方法不会", "不会列式": "方法不会", "找不到等量关系": "方法不会",
    "分类讨论漏情况": "方法不会", "推理不严谨": "方法不会",
    # → 知识点遗忘
    "单词记不牢": "知识点遗忘", "固定搭配不熟": "知识点遗忘", "课文背诵不熟": "知识点遗忘",
    "成语词语用错": "知识点遗忘", "词形变化错": "知识点遗忘",
    # → 审题不清
    "没读懂题意": "审题不清", "看错题目要求": "审题不清", "漏看条件": "审题不清",
    "忽略隐含条件": "审题不清", "多问只答一问": "审题不清", "答非所问": "审题不清",
    "看错符号": "审题不清",
    # → 计算粗心
    "口算失误": "计算粗心", "进位退位错": "计算粗心", "小数点点错": "计算粗心",
    "约分通分错": "计算粗心", "运算顺序错": "计算粗心", "解方程出错": "计算粗心",
    "竖式对位错": "计算粗心", "没验算": "计算粗心", "草稿太乱": "计算粗心",
    # → 马虎粗心 / 看错数字 / 状态不好
    "漏题": "马虎粗心", "漏做小题": "马虎粗心", "抄错数": "马虎粗心", "抄错": "马虎粗心",
    "答案写错位置": "马虎粗心", "誊写错误": "马虎粗心",
    "看错数": "看错数字",
    "边做边玩分心": "状态不好", "紧张看错": "状态不好",
    # → 忘写单位 / 答得不完整 / 表达书写不规范
    "单位用错": "忘写单位", "没写单位": "忘写单位", "忘写答语": "答得不完整",
    "表达不准确": "表达书写不规范", "表达不清": "表达书写不规范", "表述不准确": "表达书写不规范",
    "用词不当": "表达书写不规范", "语句不通顺": "表达书写不规范",
    "表达不完整": "答得不完整", "只写答案没过程": "答得不完整",
    "步骤不规范": "表达书写不规范", "步骤不全": "表达书写不规范", "格式不规范": "表达书写不规范",
    "字迹潦草": "表达书写不规范", "书写潦草": "表达书写不规范", "涂改不清": "表达书写不规范",
    "标点使用错": "表达书写不规范", "标点错误": "表达书写不规范", "病句判断错": "表达书写不规范",
    "关联词用错": "表达书写不规范", "内容不具体": "表达书写不规范",
    "作文跑题": "表达书写不规范", "作文偏题": "表达书写不规范", "作文结构乱": "表达书写不规范",
    # → 学科专项
    "单词拼写错": "单词拼写", "拼写错误": "单词拼写", "错别字": "单词拼写", "生字词写错": "单词拼写",
    "单词不会": "单词拼写",
    "语法错误": "语法时态", "语法记混": "语法时态", "时态错误": "语法时态", "时态用错": "语法时态",
    "单复数用错": "语法时态", "冠词介词用错": "语法时态", "句型转换错": "语法时态",
    "代词指代错": "语法时态",
    "听力不清": "听力没听清",
    "阅读偏差": "阅读理解偏差", "完形语境没联系": "阅读理解偏差", "语篇逻辑不清晰": "阅读理解偏差",
    "阅读概括不准": "阅读理解偏差", "抓不住中心": "阅读理解偏差",
    "不会赏析句子": "阅读理解偏差", "修辞判断错": "阅读理解偏差",
    "古诗文默写错": "古诗文默写",
    # → 状态其他
    "题目有争议": "题目超纲", "超出学习范围": "题目超纲",
}

# 旧标签 → 主错因（TAG_TO_REASON 的旧口径）；归一化前的旧标签也照样能推出主错因
TAG_TO_REASON_LEGACY = {
    "定义混淆": "概念不清", "公式记错": "概念不清", "公式用错": "概念不清",
    "定理记错": "概念不清", "单位换算错": "概念不清", "数量关系搞反": "概念不清",
    "口算失误": "计算粗心", "进位退位错": "计算粗心", "小数点点错": "计算粗心",
    "约分通分错": "计算粗心", "运算顺序错": "计算粗心", "解方程出错": "计算粗心",
    "竖式对位错": "计算粗心", "没验算": "计算粗心", "草稿太乱": "计算粗心", "马虎粗心": "计算粗心",
    "没读懂题意": "审题失误", "看错题目要求": "审题失误", "漏看条件": "审题失误",
    "看错数字": "审题失误", "抄错数": "审题失误", "看错符号": "审题失误",
    "忽略隐含条件": "审题失误", "多问只答一问": "审题失误", "答非所问": "审题失误",
    "单词记不牢": "知识遗忘", "课文背诵不熟": "知识遗忘", "古诗文默写错": "知识遗忘",
    "生字词写错": "知识遗忘", "固定搭配不熟": "知识遗忘",
    "思路不对": "方法不会", "不会举一反三": "方法不会", "综合题不会拆解": "方法不会",
    "不会画图分析": "方法不会", "不会列式": "方法不会", "找不到等量关系": "方法不会",
    "分类讨论漏情况": "方法不会", "推理不严谨": "方法不会",
}


def remap_tags(tags):
    """旧标签归入新清单：能映射的换成新名，映射不了的保留（自定义标签不受影响）。"""
    out = []
    for t in tags:
        t2 = TAG_REMAP.get(t, t)
        if t2 not in out:
            out.append(t2)
    return out


# 标签 → 老的「主错因」（error_reason，六选一）。
# 为什么还留着它：列表分组、统计、打印都还在用这一档粗分类，
# 让标签自动推出主错因，新老两套口径就不会各说各话。
# 一个标签都没命中时主错因留空，绝不瞎猜。
TAG_TO_REASON = {}
for _rs, _ts in (
    ("概念不清", ("概念不清", "完全没学过")),
    ("计算粗心", ("计算粗心", "马虎粗心", "看错数字", "没检查")),
    ("审题失误", ("审题不清", "忘写单位")),
    ("知识遗忘", ("知识点遗忘", "古诗文默写", "单词拼写")),
    ("方法不会", ("方法不会",)),
):
    TAG_TO_REASON.update({_t: _rs for _t in _ts})
del _rs, _ts


def reason_from_tags(value):
    """从标签里挑一个「主错因」（按标签顺序取第一个能认出的）；认不出返回空串。
    先按新口径认，认不出再按旧口径认一遍 —— 老数据没归一化前也能推出主错因。"""
    for t in split_tags(value):
        if t in TAG_TO_REASON:
            return TAG_TO_REASON[t]
    for t in split_tags(value):
        if t in TAG_TO_REASON_LEGACY:
            return TAG_TO_REASON_LEGACY[t]
    return ""


def _tag_filter(params):
    """按错因标签筛选的 SQL 片段。多个标签之间是「命中任意一个即可」。

    用 instr 前后补「，」做整词匹配 —— 直接 LIKE '%概念不清%' 会让
    「概念不清」误命中「概念不清楚」这类更长的标签，补上边界就不会。
    """
    tags = split_tags(params.get("tags") or "")
    if not tags:
        return "", []
    where = " OR ".join(["instr('，'||m.reason_tags||'，','，'||?||'，')>0"] * len(tags))
    return "(" + where + ")", list(tags)


# 「练过几次」的预设档位 → (下限, 上限)。上限 None = 不封顶。
# 代号跟前端 RC_PRESETS 一一对应，两边口径必须一致。
RC_PRESETS = {
    "0": (0, 0),      # 没练过
    "1": (1, 1),      # 练过 1 次
    "2": (2, 2),      # 练过 2 次
    "3+": (3, None),  # 练过 3 次及以上
}


def _review_filter(params):
    """按「复习次数」筛题的 SQL 片段（复习次数 = review_count = 练过几次）。

    两种传法：rc 走预设档位（0 / 1 / 2 / 3+），rc_min / rc_max 走精确区间。
    ★ 两个都传时以精确区间为准 —— 用户在区间框里填了数，说明他就是要精确筛。
    认不出的值一律忽略（宁可不过滤，也不要筛出错误结果）。
    """
    lo, hi = None, None
    preset = (params.get("rc") or "").strip()
    if preset in RC_PRESETS:
        lo, hi = RC_PRESETS[preset]

    # 先只解析区间，再决定用不用档位：★ 只要区间里有一个数是有效的，
    # 就整个丢掉档位 —— 否则 rc=0（0~0）配 rc_min=3 会算成「0~3 次」，
    # 而用户的意思明明是「3 次以上」。
    span = {}
    for key, is_lo in (("rc_min", True), ("rc_max", False)):
        raw = str(params.get(key) or "").strip()
        if not raw:
            continue
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        if n < 0:
            continue
        span["lo" if is_lo else "hi"] = n
    if span:
        lo = span.get("lo")
        hi = span.get("hi")

    if lo is None and hi is None:
        return "", []
    # 上下限填反了就直接换过来，比给用户一个空结果友好
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    if lo is not None and hi is not None and lo == hi:
        return "m.review_count = ?", [lo]
    parts, args = [], []
    if lo is not None:
        parts.append("m.review_count >= ?")
        args.append(lo)
    if hi is not None:
        parts.append("m.review_count <= ?")
        args.append(hi)
    return "(" + " AND ".join(parts) + ")", args


MISTAKE_FIELDS = [
    "chapter", "knowledge_point", "error_reason", "reason_tags", "question_image", "question_text",
    "clean_text", "clean_at",
    "student_answer", "correct_answer", "explanation", "similar_question",
    "check_result", "is_correct", "mastery", "source_type",
    "audio_url", "listening_text", "mark_delete",
]


# ----------------------------------------------------------------------------
# 业务操作
# ----------------------------------------------------------------------------
def save_data_url(data_url, prefix=""):
    """把 data:image/jpeg;base64,... 存成本地文件，返回相对路径。

    prefix 用来给文件名加个前缀（草稿图用 draft-），方便启动概况里区分计数。
    """
    if not data_url or not data_url.startswith("data:"):
        return data_url or ""
    m = re.match(r"data:(image/[a-zA-Z0-9.+-]+);base64,(.+)", data_url, re.S)
    if not m:
        return ""
    mime, payload = m.group(1), m.group(2)
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
           "image/gif": ".gif", "image/bmp": ".bmp", "image/heic": ".heic"}.get(mime, ".jpg")
    name = "%s%s%s" % (prefix, datetime.now().strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
    filename = name + ext
    os.makedirs(IMAGE_DIR, exist_ok=True)
    with open(os.path.join(IMAGE_DIR, filename), "wb") as f:
        f.write(base64.b64decode(payload))
    return "images/" + filename


def save_audio_data_url(data_url):
    """把 data:audio/xxx;base64,... 存到 data/audio/，返回相对路径（audio/xxx.mp3）。"""
    if not data_url or not data_url.startswith("data:"):
        return data_url or ""
    m = re.match(r"data:(audio/[a-zA-Z0-9.+-]+);base64,(.+)", data_url, re.S)
    if not m:
        return ""
    mime, payload = m.group(1), m.group(2)
    ext = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav",
           "audio/x-wav": ".wav", "audio/webm": ".webm", "audio/ogg": ".ogg",
           "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac"}.get(mime, ".mp3")
    name = "%s%s" % (datetime.now().strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
    filename = name + ext
    os.makedirs(AUDIO_DIR, exist_ok=True)
    with open(os.path.join(AUDIO_DIR, filename), "wb") as f:
        f.write(base64.b64decode(payload))
    return "audio/" + filename


def save_images_field(value, prefix=""):
    """question_image 可能是单张，也可能是多张（用 | 连起来，跨页/整卷题）。

    以前多页会先在前端拼成一张长图再存 —— 长图在手机上要放到能看清字的时候
    横向早就出屏了。现在各页分别落盘、用 | 连起来，复习时一张一张翻。
    传进来的既可能是 data URL（新拍/新传），也可能是已落盘的 images/xxx.jpg
    （编辑时没换图），后者原样保留。
    """
    parts = [p.strip() for p in str(value or "").split("|") if p.strip()]
    if not parts:
        return ""
    return "|".join(save_data_url(p, prefix) if p.startswith("data:") else p
                    for p in parts)


def _same_host(url_a, url_b):
    """两个接口地址是不是同一台主机（用于判断能不能共用一个 Key）。"""
    def host(u):
        try:
            return (urlparse(u).hostname or "").lower()
        except Exception:
            return ""
    a, b = host(url_a), host(url_b)
    return bool(a) and a == b


def _data_url_to_bytes(data_url):
    """解析任意 data URL → (bytes, mime, 建议扩展名)；失败抛 ValueError。"""
    if not data_url or not data_url.startswith("data:"):
        raise ValueError("音频数据格式不对")
    m = re.match(r"data:([a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+);base64,(.+)", data_url, re.S)
    if not m:
        raise ValueError("音频数据不是 base64 data URL")
    mime, payload = m.group(1), m.group(2)
    try:
        raw = base64.b64decode(payload)
    except Exception:
        raise ValueError("音频 base64 解码失败")
    ext = {"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
           "audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a",
           "audio/x-m4a": "m4a", "audio/aac": "aac"}.get(mime, "mp3")
    return raw, mime, ext


def asr_transcribe(payload):
    """把一段听力音频转成文字。

    为什么需要它：检查用的模型（DeepSeek）**不认识音频**——input_audio 直接 422、
    file 块只收图片。所以走「先转文字、再把文字当题目上下文发给检查模型」这条路。

    接口是 OpenAI 兼容的 POST /audio/transcriptions（multipart/form-data），
    纯标准库手工拼 multipart，不依赖 requests。
    """
    cfg = load_config()
    data_url = (payload or {}).get("audio") or ""
    raw, mime, ext = _data_url_to_bytes(data_url)
    if len(raw) > MAX_AUDIO_MB * 1024 * 1024:
        raise RuntimeError("音频超过 %d MB，请换短一点的录音" % MAX_AUDIO_MB)

    base = (cfg.get("asr_base_url") or "").rstrip("/")
    model = cfg.get("asr_model") or "FunAudioLLM/SenseVoiceSmall"
    # Key 的选择：优先用 ASR 专用 Key；没填则**只在同一厂商时**回退主 Key。
    # 为什么加"同厂商"这个条件：主 Key 常是别家（如 DeepSeek）的，直接拿去调硅基流动
    # 只会换回一个 401，错误信息还容易让人以为是 Key 填错了，不如提前说清楚。
    main_key = (cfg.get("api_key") or "").strip()
    main_base = (cfg.get("base_url") or "").rstrip("/")
    key = (cfg.get("asr_api_key") or "").strip()
    if not key and main_key and _same_host(main_base, base):
        key = main_key
    if not key:
        if main_key and not _same_host(main_base, base):
            raise RuntimeError(
                "语音转文字还没有配 Key。当前的模型接口是 %s，而转写接口是 %s，不是同一家，"
                "需要在「设置」页的「转写专用 Key」里单独填一个对应的 Key（硅基流动的免费语音模型即可）。"
                % (main_base or "（未填）", base or "（未填）"))
        raise RuntimeError("还没有配语音转文字用的 API Key，请到「设置」页填写后再上传听力音频")
    if not base.startswith("http"):
        raise RuntimeError("语音转文字的接口地址不正确：%s" % base)

    boundary = "----ctb" + uuid.uuid4().hex
    parts = []
    for k, v in (("model", model),):
        parts.append(("--" + boundary + "\r\n").encode("utf-8"))
        parts.append(('Content-Disposition: form-data; name="%s"\r\n\r\n' % k).encode("utf-8"))
        parts.append((v + "\r\n").encode("utf-8"))
    parts.append(("--" + boundary + "\r\n").encode("utf-8"))
    parts.append(('Content-Disposition: form-data; name="file"; filename="listening.%s"\r\n' % ext).encode("utf-8"))
    parts.append(("Content-Type: %s\r\n\r\n" % mime).encode("utf-8"))
    parts.append(raw + b"\r\n")
    parts.append(("--" + boundary + "--\r\n").encode("utf-8"))
    body = b"".join(parts)

    req = urllib.request.Request(base + "/audio/transcriptions", data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    req.add_header("Authorization", "Bearer " + key)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(_proxy_map(cfg)))
    try:
        with opener.open(req, timeout=int(cfg.get("timeout", 180))) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:400]
        hint = ""
        if exc.code in (401, 403):
            hint = "（Key 无效或没有语音模型权限）"
        elif exc.code == 404:
            hint = "（接口地址或模型名不对，模型名示例：FunAudioLLM/SenseVoiceSmall）"
        raise RuntimeError("语音转文字失败 HTTP %s%s：%s" % (exc.code, hint, detail))
    except URLError as exc:
        raise RuntimeError("连不上语音转文字接口：%s" % exc.reason)
    except socket.timeout:
        raise RuntimeError("语音转文字超时，音频太长或网速慢，可在「设置」页把超时调大")

    text = (out.get("text") or "").strip()
    if not text:
        raise RuntimeError("语音转文字没返回内容：%s" % json.dumps(out, ensure_ascii=False)[:300])
    print("[asr] model=%s bytes=%d → %d 字" % (model, len(raw), len(text)))
    return text



def _mistake_cols(conn, payload, image, audio=None):
    """把前端 payload 规整成一行 mistakes 的列值。

    audio：音频的落盘结果（相对路径）。整卷模式下多道题共用同一份音频，
    由调用方只存一次再传进来；这里不再自己存，避免 N 道题写 N 份音频文件。
    """
    ts = now_str()
    tags = norm_tags(payload.get("reason_tags"))
    # 干净原题：只留印刷体题干（已抹掉学生手写与答案），专供复习时直接做。
    # 提取时间由服务端自己盖，前端只管传文本，免得两边时间口径打架。
    clean = str(payload.get("clean_text") or "")
    return {
        "subject_id": subject_id_of(conn, payload.get("subject")),
        "chapter": payload.get("chapter") or "",
        "knowledge_point": payload.get("knowledge_point") or "",
        # 主错因优先用传进来的；没传就从标签里推一个，保证两套口径一致
        "error_reason": payload.get("error_reason") or reason_from_tags(tags),
        "reason_tags": tags,
        "question_image": image,
        "question_text": payload.get("question_text") or "",
        "clean_text": clean,
        "clean_at": ts if clean.strip() else "",
        "student_answer": payload.get("student_answer") or "",
        "correct_answer": payload.get("correct_answer") or "",
        "explanation": payload.get("explanation") or "",
        "similar_question": payload.get("similar_question") or "",
        "check_result": payload.get("check_result") or "",
        "is_correct": payload.get("is_correct") or "unknown",
        "mastery": int(payload.get("mastery") or 0),
        "source_type": payload.get("source_type") or "作业",
        "audio_url": audio or "",
        "listening_text": payload.get("listening_text") or "",
        "created_at": ts,
        "updated_at": ts,
        "deleted": 0,
    }


def _insert_mistake(conn, payload, image, audio=None):
    cols = _mistake_cols(conn, payload, image, audio)
    keys = ",".join(cols.keys())
    marks = ",".join("?" for _ in cols)
    cur = conn.execute("INSERT INTO mistakes(%s) VALUES(%s)" % (keys, marks), list(cols.values()))
    return cur.lastrowid


def _resolve_audio(path):
    """音频路径归一化：data URL → 落盘；普通路径原样返回。"""
    path = path or ""
    if path.startswith("data:"):
        return save_audio_data_url(path)
    return path


def create_mistake(payload):
    with _db_lock, connect() as conn:
        image = save_images_field(payload.get("question_image") or "")
        audio = _resolve_audio(payload.get("audio_url") or payload.get("audio") or "")
        mid = _insert_mistake(conn, payload, image, audio)
    return get_mistake(mid)


def create_mistakes_batch(payload):
    """一图多题：一次写入多道题。

    整页原图只有一份 —— shared_image 先落盘一次，所有题共用同一个路径，
    避免 N 道题往 data/images 里塞 N 份一模一样的照片。
    整个过程在同一个事务里，要么全成功要么全回滚。
    听力音频同理：整卷共用一份，只在外面存一次。
    """
    items = payload.get("items") or []
    if not isinstance(items, list):
        raise ValueError("items 必须是数组")
    items = [it for it in items if isinstance(it, dict)][:MAX_BATCH_ITEMS]
    if not items:
        raise ValueError("没有要保存的题目")

    shared = save_images_field(payload.get("shared_image") or "")
    # 听力音频：整卷共用一份，只落盘一次
    shared_audio = _resolve_audio(payload.get("audio_url") or payload.get("audio") or "")

    created = []
    with _db_lock, connect() as conn:
        for it in items:
            own = it.get("question_image") or ""
            image = save_images_field(own) if own else shared
            audio = _resolve_audio(it.get("audio_url") or "") or shared_audio
            created.append(_insert_mistake(conn, it, image, audio))
    return [get_mistake(mid) for mid in created]


# ----------------------------------------------------------------------------
# 未保存的识别结果（草稿）
# 场景：AI 识别完、还没点「存入错题本」时，误触返回 / 切页签 / 刷新，白拍一场。
# 做法：结果 + 原图先暂存。原图直接落在 data/images/（文件名带 draft- 前缀），
#      点「存入错题本」时这些图**原样转正**（不重复落盘）；只有「放弃草稿」
#      才删除草稿图，且删之前必须先确认没被任何错题引用。
# ----------------------------------------------------------------------------
DRAFT_ITEM_TEXT_FIELDS = ("question_text", "clean_text", "student_answer", "correct_answer",
                          "explanation", "similar_question", "check_result", "is_correct",
                          "error_reason", "reason_tags")


def _clean_draft_item(x):
    """草稿条目只留必要字段，并逐个限长（防止把超长文本塞进 draft.json）。"""
    out = {}
    for k in DRAFT_ITEM_TEXT_FIELDS:
        out[k] = str(x.get(k) or "")[:DRAFT_TEXT_CAP]
    kps = x.get("knowledge_points")
    out["knowledge_points"] = [str(t)[:60] for t in kps][:12] if isinstance(kps, list) else []
    try:
        out["index"] = int(x.get("index") or 0)      # 题号（卡片左上角）
    except Exception:
        out["index"] = 0
    out["save"] = bool(x.get("save"))
    out["open"] = bool(x.get("open"))
    return out


def _draft_image_refs(draft):
    """草稿引用的所有 images/... 路径。"""
    return [p for p in (draft.get("photos") or [])
            if isinstance(p, str) and p.startswith("images/")]


def _refs_used_by_mistakes(refs):
    """这批图里，哪些已经被错题引用了（放弃草稿时绝不能删）。

    注意 question_image 可能是多张（| 分隔），所以不能只做等值比较 ——
    否则多页题里的每一页都会被判成「没人用」，一放弃草稿就被删掉。
    这里把字段和待查路径都补上 | 边界，用 instr 做包含判断（避开通配符问题）。
    """
    used = set()
    if not refs:
        return used
    with connect() as conn:
        for r in refs:
            row = conn.execute(
                "SELECT 1 FROM mistakes WHERE question_image=?"
                " OR instr('|'||question_image||'|','|'||?||'|')>0 LIMIT 1",
                (r, r)).fetchone()
            if row:
                used.add(r)
    return used


def _drop_draft_images(refs):
    """删除草稿图。只删 draft- 前缀的、且没被错题引用的 —— 两道闸都不放松。"""
    refs = [r for r in refs if os.path.basename(r).startswith(DRAFT_IMG_PREFIX)]
    used = _refs_used_by_mistakes(refs)
    n = 0
    for r in refs:
        if r in used:
            continue
        try:
            os.remove(os.path.join(DATA_DIR, r.replace("/", os.sep)))
            n += 1
        except Exception:
            pass
    return n


def load_draft_raw():
    try:
        with open(DRAFT_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def load_draft():
    """读草稿。顺带标出哪些原图丢了（丢图不影响题目文字，仍可保存/重拍）。"""
    d = load_draft_raw()
    if not d:
        return None
    miss = []
    for p in _draft_image_refs(d):
        if not os.path.isfile(os.path.join(DATA_DIR, p.replace("/", os.sep))):
            miss.append(p)
    d["missing_photos"] = miss
    d["photos_ok"] = [p for p in (d.get("photos") or []) if p not in miss]
    return d


def save_draft(payload):
    """保存/覆盖草稿（同一时刻只留最新一份）。data URL 落盘后记路径，路径原样沿用。"""
    if not isinstance(payload, dict):
        raise ValueError("草稿格式不对")
    old = load_draft_raw() or {}

    photos = []
    for p in (payload.get("photos") or [])[:MAX_BATCH_ITEMS]:
        if not isinstance(p, str) or not p:
            continue
        if p.startswith("data:"):
            p = save_data_url(p, DRAFT_IMG_PREFIX)     # 首次暂存才落盘，之后沿用同一张
        if p:
            photos.append(p)

    audio = payload.get("audio") or ""
    if isinstance(audio, str) and audio.startswith("data:"):
        audio = save_audio_data_url(audio)

    items = [_clean_draft_item(x) for x in (payload.get("items") or [])
             if isinstance(x, dict)][:DRAFT_MAX_ITEMS]
    if not items:
        raise ValueError("草稿里没有题目，不用暂存")

    draft = {
        "created_at": old.get("created_at") or now_str(),
        "updated_at": now_str(),
        "subject": str(payload.get("subject") or "未分类")[:40],
        "chapter": str(payload.get("chapter") or "")[:80],
        "source_type": str(payload.get("source_type") or "作业")[:20],
        "extra": str(payload.get("extra") or "")[:500],
        "listeningText": str(payload.get("listeningText") or "")[:20000],
        "asrNote": str(payload.get("asrNote") or "")[:200],
        "declared": max(0, int(payload.get("declared") or 0)),
        "missing": max(0, int(payload.get("missing") or 0)),
        "recovered": bool(payload.get("recovered")),
        "photos": photos,
        "audio": audio or "",
        "items": items,
    }
    raw = json.dumps(draft, ensure_ascii=False)
    if len(raw.encode("utf-8")) > DRAFT_MAX_BYTES:
        raise ValueError("这次识别的内容太大，暂存不下；先存入错题本再继续")

    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DRAFT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(raw)
    os.replace(tmp, DRAFT_PATH)                 # 原子替换：中途断电也不会留下半截草稿

    # 旧草稿里已经不再引用的草稿图清掉（例如用户重拍换了照片）
    stale = [r for r in _draft_image_refs(old) if r not in photos]
    _drop_draft_images(stale)
    return load_draft()


def _pending_draft_images(draft):
    """草稿带来、但还没被任何错题引用的原图（启动概况里单独提示，对账时不会以为错题变多了）。"""
    if not draft:
        return []
    refs = [r for r in _draft_image_refs(draft) if os.path.basename(r).startswith(DRAFT_IMG_PREFIX)]
    used = _refs_used_by_mistakes(refs)
    return [r for r in refs if r not in used]


def clear_draft(delete_images=False):
    """清除草稿。delete_images=True（用户点「放弃」）时连没被错题引用的草稿图一起删。

    注意：点「存入错题本」成功后调用的是 clear_draft(False) —— 那些图已经
    被错题引用了，必须留着（它们就是错题原图）。
    """
    old = load_draft_raw() or {}
    try:
        os.remove(DRAFT_PATH)
    except Exception:
        pass
    removed = _drop_draft_images(_draft_image_refs(old)) if delete_images else 0
    return removed


def get_mistake(mid):
    with connect() as conn:
        row = conn.execute(
            "SELECT m.*, s.name AS subject FROM mistakes m "
            "LEFT JOIN subjects s ON s.id=m.subject_id WHERE m.id=?", (mid,)
        ).fetchone()
        if not row:
            return None
        item = row_to_mistake(row)
        item["reviews"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM review_records WHERE mistake_id=? ORDER BY id DESC", (mid,)
            ).fetchall()
        ]
    return item


def list_mistakes(params):
    where = ["m.deleted=0"]
    args = []
    subject = (params.get("subject") or "").strip()
    if subject and subject != "全部":
        where.append("s.name=?")
        args.append(subject)
    keyword = (params.get("q") or "").strip()
    if keyword:
        # clean_text（干净原题）也纳入搜索：列表卡片上展示的就是它，
        # 它搜不到会让人以为「明明有这道题却搜不出来」。student_answer（孩子写的答案）同理。
        where.append("(m.question_text LIKE ? OR m.clean_text LIKE ? OR m.knowledge_point LIKE ? "
                     "OR m.chapter LIKE ? OR m.explanation LIKE ? OR m.error_reason LIKE ? "
                     "OR m.reason_tags LIKE ? OR m.student_answer LIKE ?)")
        args += ["%%%s%%" % keyword] * 8
    reason = (params.get("error_reason") or "").strip()
    if reason and reason != "全部":
        where.append("m.error_reason=?")
        args.append(reason)
    tag_sql, tag_args = _tag_filter(params)
    if tag_sql:
        where.append(tag_sql)
        args += tag_args
    rc_sql, rc_args = _review_filter(params)
    if rc_sql:
        where.append(rc_sql)
        args += rc_args
    if params.get("low") in ("1", "true"):
        where.append("m.mastery < 3")
    if params.get("unmastered") in ("1", "true"):
        where.append("m.mastery < 5")
    if params.get("weak") in ("1", "true"):
        where.append("m.mastery < 5 AND m.review_count = 0")
    if params.get("due") in ("1", "true"):
        # 只保留「到点了」的题：按遗忘曲线算出的下次复习时间已经 <= 现在
        where.append(SQL_DUE_AT + " <= " + SQL_NOW_LOCAL)
    sql = ("SELECT m.*, s.name AS subject FROM mistakes m "
           "LEFT JOIN subjects s ON s.id=m.subject_id WHERE " + " AND ".join(where))
    order = (params.get("sort") or "new").strip()
    sql += {
        "new": " ORDER BY m.id DESC",
        "old": " ORDER BY m.id ASC",
        "mastery": " ORDER BY m.mastery ASC, m.id DESC",
        "review": " ORDER BY m.review_count ASC, m.id DESC",
        # 到期优先：过期越久越靠前（负数最小 → 最该练的排最上面），未到期的按到期时间顺延
        "due": " ORDER BY " + SQL_DUE_AT + " ASC",
    }.get(order, " ORDER BY m.id DESC")
    limit = params.get("limit")
    if limit:
        sql += " LIMIT %d" % max(1, min(500, int(limit)))
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [row_to_mistake(r) for r in rows]


def update_mistake(mid, patch):
    # 编辑时新传的图片/音频是 data URL，先落盘成相对路径再入库。
    # 图片这条以前漏了：编辑时补拍一页会把整串 base64 当路径写进库。
    # 注意必须用「整串里有没有 data:」来判断，不能只看开头 ——
    # 多页题编辑后是「已落盘的路径|新拍的 data URL」这种混合串。
    qi = str(patch.get("question_image") or "")
    if "data:" in qi:
        patch = dict(patch)
        patch["question_image"] = save_images_field(qi)
    if str(patch.get("audio_url") or "").startswith("data:"):
        patch = dict(patch)
        patch["audio_url"] = save_audio_data_url(patch["audio_url"])
    # 只改标签、没一起提交主错因时，跟着标签把主错因重算一遍（两边不能打架）
    if "reason_tags" in patch and not patch.get("error_reason"):
        patch = dict(patch)
        patch["error_reason"] = reason_from_tags(patch["reason_tags"])
    # 干净原题改了（或清空）就自动盖/清提取时间，前端只管传文本
    if "clean_text" in patch and "clean_at" not in patch:
        patch = dict(patch)
        patch["clean_at"] = now_str() if str(patch.get("clean_text") or "").strip() else ""
    fields, values = [], []
    for key in MISTAKE_FIELDS:
        if key in patch and patch[key] is not None:
            fields.append("%s=?" % key)
            if key == "mastery":
                values.append(int(patch[key]))
            elif key == "reason_tags":
                values.append(norm_tags(patch[key]))
            else:
                values.append(patch[key])
    if "subject" in patch and patch["subject"]:
        with connect() as conn:
            sid = subject_id_of(conn, patch["subject"])
        fields.append("subject_id=?")
        values.append(sid)
    if not fields:
        return get_mistake(mid)
    fields.append("updated_at=?")
    values.append(now_str())
    values.append(mid)
    with _db_lock, connect() as conn:
        conn.execute("UPDATE mistakes SET %s WHERE id=?" % ",".join(fields), values)
    return get_mistake(mid)


def delete_mistake(mid):
    with _db_lock, connect() as conn:
        conn.execute("UPDATE mistakes SET deleted=1, updated_at=? WHERE id=?", (now_str(), mid))
    return True


def add_review(mid, is_correct, note=""):
    """复习一次：写 review_records 并回写 mastery。做对 +1（上限 5），做错归 0。"""
    with _db_lock, connect() as conn:
        row = conn.execute(
            "SELECT mastery, created_at, review_count FROM mistakes WHERE id=?", (mid,)
        ).fetchone()
        if not row:
            raise ValueError("错题不存在")
        current = int(row["mastery"] or 0)
        new_mastery = min(5, current + 1) if is_correct else 0
        ts = now_str()
        cur = conn.execute(
            "INSERT INTO review_records(mistake_id, reviewed_at, is_correct, note) VALUES(?,?,?,?)",
            (mid, ts, 1 if is_correct else 0, note or ""),
        )
        conn.execute(
            "UPDATE mistakes SET mastery=?, review_count=review_count+1, last_reviewed_at=?, "
            "updated_at=? WHERE id=?",
            (new_mastery, ts, ts, mid),
        )
        rec = dict(conn.execute("SELECT * FROM review_records WHERE id=?", (cur.lastrowid,)).fetchone())
    # 用「刚写完」的状态算下一次到期：前端做完一题就能立刻告诉家长下次啥时候练
    schedule = compute_schedule(new_mastery, ts, row["created_at"], int(row["review_count"] or 0) + 1)
    return {
        "record": rec,
        "mastery": new_mastery,
        "mastery_before": current,
        "correct": bool(is_correct),
        "next_due_at": schedule["next_due_at"],
        "due_in_days": schedule["due_in_days"],
        "interval_days": schedule["interval_days"],
        "interval_label": schedule["interval_label"],
    }


def stats():
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) n FROM mistakes WHERE deleted=0").fetchone()["n"]
        mastered = conn.execute(
            "SELECT COUNT(*) n FROM mistakes WHERE deleted=0 AND mastery>=5").fetchone()["n"]
        fresh = conn.execute(
            "SELECT COUNT(*) n FROM mistakes WHERE deleted=0 AND review_count=0").fetchone()["n"]
        reviews = conn.execute("SELECT COUNT(*) n FROM review_records").fetchone()["n"]
        by_subject = [dict(r) for r in conn.execute(
            "SELECT s.name AS subject, COUNT(m.id) AS n, "
            "COALESCE(SUM(CASE WHEN m.mastery>=5 THEN 1 ELSE 0 END),0) AS mastered "
            "FROM subjects s LEFT JOIN mistakes m ON m.subject_id=s.id AND m.deleted=0 "
            "GROUP BY s.id ORDER BY s.sort_order, s.id").fetchall()]
        by_reason = [dict(r) for r in conn.execute(
            "SELECT error_reason AS reason, COUNT(*) AS n FROM mistakes "
            "WHERE deleted=0 AND error_reason<>'' GROUP BY error_reason ORDER BY n DESC").fetchall()]
        # 错因标签是复选的，一条题会落进好几个标签，所以这里得逐行拆开数
        tag_n = {}
        for r in conn.execute("SELECT reason_tags FROM mistakes "
                              "WHERE deleted=0 AND reason_tags<>''").fetchall():
            for t in split_tags(r["reason_tags"]):
                tag_n[t] = tag_n.get(t, 0) + 1
        by_tag = [{"tag": k, "n": v}
                  for k, v in sorted(tag_n.items(), key=lambda kv: (-kv[1], kv[0]))[:30]]
        by_kp = [dict(r) for r in conn.execute(
            "SELECT knowledge_point AS kp, COUNT(*) AS n FROM mistakes "
            "WHERE deleted=0 AND knowledge_point<>'' GROUP BY knowledge_point "
            "ORDER BY n DESC LIMIT 20").fetchall()]
        # 注意用 'localtime'：created_at 存的是本地时间，而 SQLite 的 date('now') 是 UTC，
        # 不加这个修饰符在 UTC+8 会平白多算/少算 8 小时
        last7 = conn.execute(
            "SELECT COUNT(*) n FROM mistakes WHERE deleted=0 "
            "AND created_at >= datetime('now','localtime','-7 day')"
        ).fetchone()["n"]

        # ---- 间隔复习视角 ----
        # 今天该练的：还没掌握（<5）且按遗忘曲线已经到期
        due_count = conn.execute(
            "SELECT COUNT(*) n FROM mistakes m WHERE m.deleted=0 AND m.mastery < 5 "
            "AND " + SQL_DUE_AT + " <= " + SQL_NOW_LOCAL
        ).fetchone()["n"]
        # 今天没有到期的题时，告诉家长下一批什么时候来
        next_row = conn.execute(
            "SELECT MIN(" + SQL_DUE_AT + ") AS t FROM mistakes m "
            "WHERE m.deleted=0 AND m.mastery < 5 AND " + SQL_DUE_AT + " > " + SQL_NOW_LOCAL
        ).fetchone()
        by_mastery = {int(r["mastery"] or 0): r["n"] for r in conn.execute(
            "SELECT mastery, COUNT(*) AS n FROM mistakes WHERE deleted=0 "
            "GROUP BY mastery ORDER BY mastery").fetchall()}

        # ---- 练过几次的分布（复习页那几个档位的题量就按这个显示）----
        # 4 次以上全并进 "3" 这一档，跟筛选档位「3 次以上」对齐
        by_review_count = {"0": 0, "1": 0, "2": 0, "3": 0}
        for r in conn.execute(
                "SELECT review_count AS c, COUNT(*) AS n FROM mistakes "
                "WHERE deleted=0 GROUP BY review_count").fetchall():
            k = int(r["c"] or 0)
            by_review_count["3" if k >= 3 else str(k)] += int(r["n"] or 0)

        # ---- 提醒口吻用：当前最久的一道题过期几天 ----
        # 过期 1 天和过期 10 天，提示语该不一样，所以单独算一个最大值出来
        row = conn.execute(
            "SELECT MAX(CAST(julianday(" + SQL_NOW_LOCAL + ") - julianday(" + SQL_DUE_AT + ") AS INTEGER)) AS d "
            "FROM mistakes m WHERE m.deleted=0 AND m.mastery < 5 AND " + SQL_DUE_AT + " <= " + SQL_NOW_LOCAL
        ).fetchone()
        overdue_max = int(row["d"] or 0) if row else 0

        # ---- 本周小结（近 7 天，全部按本地时间口径）----
        wk = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(is_correct),0) c FROM review_records "
            "WHERE reviewed_at >= datetime('now','localtime','-7 day')").fetchone()
        wk_reviewed, wk_correct = int(wk["n"] or 0), int(wk["c"] or 0)
        wk_new = conn.execute(
            "SELECT COUNT(*) n FROM mistakes WHERE deleted=0 "
            "AND created_at >= datetime('now','localtime','-7 day')").fetchone()["n"]
        wk_days = conn.execute(
            "SELECT COUNT(DISTINCT date(reviewed_at)) n FROM review_records "
            "WHERE reviewed_at >= datetime('now','localtime','-7 day')").fetchone()["n"]

        # ---- 首页看板用 ----
        # 连续复习天数：从今天往前数每天都练过就连着；今天还没练不打断，从昨天起算
        day_set = {r["d"] for r in conn.execute(
            "SELECT DISTINCT date(reviewed_at) d FROM review_records").fetchall()}
        streak_days = 0
        _d = datetime.now().date()
        if _d.isoformat() not in day_set:
            _d = _d - timedelta(days=1)
        while _d.isoformat() in day_set:
            streak_days += 1
            _d = _d - timedelta(days=1)

        # 优先补练：过期最久的前 3 题（首页「先去见见它」列表）
        top_due = []
        for r in conn.execute(
                "SELECT m.id, s.name AS subject, "
                "CAST(julianday(" + SQL_NOW_LOCAL + ") - julianday(" + SQL_DUE_AT + ") AS INTEGER) AS overdue, "
                "COALESCE(NULLIF(m.clean_text,''), m.question_text, '') AS q "
                "FROM mistakes m LEFT JOIN subjects s ON s.id=m.subject_id "
                "WHERE m.deleted=0 AND m.mastery<5 AND " + SQL_DUE_AT + " <= " + SQL_NOW_LOCAL + " "
                "ORDER BY " + SQL_DUE_AT + " ASC LIMIT 3").fetchall():
            pv = " ".join((r["q"] or "").split())
            top_due.append({"id": r["id"], "subject": r["subject"] or "未分类",
                            "overdue": max(0, int(r["overdue"] or 0)),
                            "preview": (pv[:38] + "…") if len(pv) > 40 else pv})

        # 到期日分布（今天起，未掌握）：复习日历的格子数字
        due_dates = {}
        for r in conn.execute(
                "SELECT date(" + SQL_DUE_AT + ") d, COUNT(*) n FROM mistakes m "
                "WHERE m.deleted=0 AND m.mastery<5 "
                "AND " + SQL_DUE_AT + " >= date(" + SQL_NOW_LOCAL + ") GROUP BY d").fetchall():
            due_dates[r["d"]] = int(r["n"] or 0)
        # 过期的题都滚进「今天」这一格（和复习页口径一致：到期即算今天该练）
        due_dates[datetime.now().strftime("%Y-%m-%d")] = due_count


        # ---- 最该补的知识点：还没掌握的题里，出现最多、平均掌握度最低的 ----
        weak_map = {}
        for r in conn.execute(
                "SELECT knowledge_point AS kp, mastery FROM mistakes "
                "WHERE deleted=0 AND mastery < 5 AND knowledge_point <> ''"):
            for k in kp_to_list(r["kp"]):
                d = weak_map.setdefault(k, [0, 0])
                d[0] += 1
                d[1] += int(r["mastery"] or 0)
        weak_kp = sorted(
            [{"kp": k, "n": v[0], "avg_mastery": round(v[1] / float(v[0]), 1)}
             for k, v in weak_map.items()],
            key=lambda x: (-x["n"], x["avg_mastery"]),
        )[:5]

    return {
        "total": total, "mastered": mastered, "fresh": fresh, "review_count": reviews,
        "by_subject": by_subject, "by_reason": by_reason, "by_tag": by_tag,
        "by_kp": by_kp, "last7": last7,
        "due_count": due_count,
        "next_due_at": next_row["t"] if next_row else None,
        "by_mastery": by_mastery,
        "by_review_count": by_review_count,
        "overdue_max": overdue_max,
        "streak_days": streak_days,
        "top_due": top_due,
        "due_dates": due_dates,
        "weak_kp": weak_kp,
        "week": {
            "reviewed": wk_reviewed,
            "correct": wk_correct,
            "accuracy": round(wk_correct * 100.0 / wk_reviewed) if wk_reviewed else 0,
            "new": wk_new,
            "active_days": wk_days,
        },
        "intervals": SPACED_INTERVALS,
        "interval_labels": SPACED_LABELS,
    }


def export_all():
    with connect() as conn:
        mistakes = [row_to_mistake(r) for r in conn.execute(
            "SELECT m.*, s.name AS subject FROM mistakes m "
            "LEFT JOIN subjects s ON s.id=m.subject_id WHERE m.deleted=0 ORDER BY m.id").fetchall()]
        reviews = [dict(r) for r in conn.execute("SELECT * FROM review_records ORDER BY id").fetchall()]
        subjects = [dict(r) for r in conn.execute("SELECT * FROM subjects ORDER BY sort_order").fetchall()]
    return {"version": VERSION, "exported_at": now_str(),
            "subjects": subjects, "mistakes": mistakes, "review_records": reviews}


def import_all(data):
    mistakes = (data or {}).get("mistakes") or []
    reviews = (data or {}).get("review_records") or []
    count = 0
    for m in mistakes:
        payload = dict(m)
        payload.pop("id", None)
        payload["subject"] = m.get("subject") or "未分类"
        payload["knowledge_point"] = m.get("knowledge_point") or ",".join(m.get("knowledge_points") or [])
        # 老备份文件里没有 reason_tags，就按主错因给一个等效标签，别让标签白白空着
        if not payload.get("reason_tags") and payload.get("error_reason"):
            payload["reason_tags"] = payload["error_reason"]
        item = create_mistake(payload)
        count += 1
        for r in [x for x in reviews if x.get("mistake_id") == m.get("id")]:
            with connect() as conn:
                conn.execute(
                    "INSERT INTO review_records(mistake_id, reviewed_at, is_correct, note) VALUES(?,?,?,?)",
                    (item["id"], r.get("reviewed_at") or now_str(), int(r.get("is_correct") or 0),
                     r.get("note") or ""),
                )
    return count


# ----------------------------------------------------------------------------
# 全量导出 / 导入（zip 整包：错题 + 复习记录 + 题目图片 + 听力音频）
# ----------------------------------------------------------------------------
EXPORT_MAGIC = "cuotiben-export"   # 导出包自校验标记：data.json 里必须带它


def _backup_data_dir(tag):
    """导入类危险操作前，把整个 data/ 原样拷到 BASE_DIR/backup_<tag>_<时间戳>/，
    出问题靠它兜底找回（与孤儿图清理、旧 exe 归档同一个套路）。"""
    dest = os.path.join(BASE_DIR, "backup_%s_%s" % (tag, datetime.now().strftime("%Y%m%d-%H%M%S")))
    shutil.copytree(DATA_DIR, dest, dirs_exist_ok=True)
    return os.path.basename(dest)


def export_zip():
    """打包导出：data.json（全量行，含已删除的行，还原时才不丢历史）+
    images/、audio/ 里实际存在的文件。返回 zip 字节串。"""
    with connect() as conn:
        data = {
            "format": EXPORT_MAGIC, "export_version": 1,
            "exported_at": now_str(), "server_version": VERSION,
            "subjects": [dict(r) for r in conn.execute(
                "SELECT * FROM subjects ORDER BY sort_order, id").fetchall()],
            "mistakes": [dict(r) for r in conn.execute(
                "SELECT * FROM mistakes ORDER BY id").fetchall()],
            "review_records": [dict(r) for r in conn.execute(
                "SELECT * FROM review_records ORDER BY id").fetchall()],
        }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("data.json", json.dumps(data, ensure_ascii=False))
        for sub, d in (("images", IMAGE_DIR), ("audio", AUDIO_DIR)):
            if os.path.isdir(d):
                for fn in sorted(os.listdir(d)):
                    p = os.path.join(d, fn)
                    if os.path.isfile(p):
                        z.write(p, "%s/%s" % (sub, fn))
    return buf.getvalue()


def _extract_zip_files(zf, overwrite):
    """把包里的 images/、audio/ 释放到数据目录。合并模式只补缺（本地已有的文件不动），
    还原模式整目录覆盖。只认一层深度的 images/xxx、audio/xxx，防路径穿越。"""
    n_img = n_aud = 0
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if name.endswith("/"):
            continue
        sub, _, fn = name.partition("/")
        if sub not in ("images", "audio") or not fn or "/" in fn or ".." in fn:
            continue
        dest = os.path.join(IMAGE_DIR if sub == "images" else AUDIO_DIR, os.path.basename(fn))
        if not overwrite and os.path.exists(dest):
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(zf.read(info))
        if sub == "images":
            n_img += 1
        else:
            n_aud += 1
    return n_img, n_aud


def import_zip(raw, mode="merge"):
    """导入导出包。merge：两台电脑都录过题时按 id 对齐、「谁改得新听谁的」逐题并
    （updated_at 新者胜；复习记录按 (mistake_id, reviewed_at) 去重后并入）；
    replace：清空三张表后整库还原（导出包里的 id 原样保留）。
    两种模式动手前都先把当前 data/ 自动备份一份。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        data = json.loads(zf.read("data.json").decode("utf-8"))
    except Exception as exc:   # noqa: BLE001
        raise ValueError("不是有效的错题本导出包（.zip）：%s" % exc)
    if not isinstance(data, dict) or data.get("format") != EXPORT_MAGIC:
        raise ValueError("导出包缺 %s 标记。老版 JSON 备份请走「导入备份」的原方式" % EXPORT_MAGIC)

    exp_subs = data.get("subjects") or []
    exp_mists = data.get("mistakes") or []
    exp_revs = data.get("review_records") or []
    sub_name = {s.get("id"): (s.get("name") or "未分类") for s in exp_subs}
    backup = _backup_data_dir("导入前")

    if mode == "replace":
        with connect() as conn:
            conn.execute("DELETE FROM review_records")
            conn.execute("DELETE FROM mistakes")
            conn.execute("DELETE FROM subjects")
            for s in exp_subs:
                conn.execute("INSERT INTO subjects(id, name, sort_order) VALUES(?,?,?)",
                             (s.get("id"), s.get("name") or "未分类", s.get("sort_order") or 0))
            sub_ids = {s.get("id") for s in exp_subs}
            mcols = [c[1] for c in conn.execute("PRAGMA table_info(mistakes)").fetchall()]
            for m in exp_mists:
                row = dict(m)
                if row.get("subject_id") not in sub_ids:
                    row["subject_id"] = None      # 包里缺学科的孤儿行：学科留空，别让外键炸整库
                conn.execute(
                    "INSERT INTO mistakes(%s) VALUES(%s)" % (",".join(mcols), ",".join("?" * len(mcols))),
                    [row.get(c) for c in mcols])
            rcols = [c[1] for c in conn.execute("PRAGMA table_info(review_records)").fetchall()]
            mist_ids = {m.get("id") for m in exp_mists}
            for r in exp_revs:
                row = dict(r)
                if row.get("mistake_id") not in mist_ids:
                    continue                      # 没有对应错题的复习记录是脏数据，跳过
                conn.execute(
                    "INSERT INTO review_records(%s) VALUES(%s)" % (",".join(rcols), ",".join("?" * len(rcols))),
                    [row.get(c) for c in rcols])
        n_img, n_aud = _extract_zip_files(zf, overwrite=True)
        return {"mode": "replace", "backup": backup, "subjects": len(exp_subs),
                "mistakes": len(exp_mists), "reviews": len(exp_revs),
                "images": n_img, "audio": n_aud}

    # ---- 合并模式 ----
    with connect() as conn:
        loc_subs = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM subjects").fetchall()}
        sub_added = 0
        for s in exp_subs:                        # 学科先落位：后面错题的 subject_id 全靠它映射
            nm = s.get("name") or "未分类"
            if nm not in loc_subs:
                conn.execute("INSERT INTO subjects(name, sort_order) VALUES(?,?)",
                             (nm, s.get("sort_order") or 0))
                loc_subs[nm] = conn.execute("SELECT id FROM subjects WHERE name=?", (nm,)).fetchone()["id"]
                sub_added += 1
        loc_m = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM mistakes").fetchall()}
        mcols = [c[1] for c in conn.execute("PRAGMA table_info(mistakes)").fetchall()]
        m_add = m_upd = m_keep = 0
        for m in exp_mists:
            mid = m.get("id")
            if mid is None:
                continue
            row = dict(m)
            nm = sub_name.get(row.get("subject_id"), "") or "未分类"
            row["subject_id"] = loc_subs.get(nm)
            if mid in loc_m:
                if (row.get("updated_at") or "") >= (loc_m[mid].get("updated_at") or ""):
                    sets = ",".join("%s=?" % c for c in mcols if c != "id")
                    conn.execute("UPDATE mistakes SET %s WHERE id=?" % sets,
                                 [row.get(c) for c in mcols if c != "id"] + [mid])
                    m_upd += 1
                else:
                    m_keep += 1
            else:
                conn.execute("INSERT INTO mistakes(%s) VALUES(%s)" % (
                    ",".join(mcols), ",".join("?" * len(mcols))), [row.get(c) for c in mcols])
                m_add += 1
        valid_mids = set(loc_m) | {m.get("id") for m in exp_mists if m.get("id") is not None}
        have = set((r["mistake_id"], r["reviewed_at"] or "") for r in conn.execute(
            "SELECT mistake_id, reviewed_at FROM review_records").fetchall())
        r_add = 0
        for r in exp_revs:
            key = (r.get("mistake_id"), r.get("reviewed_at") or "")
            if key[0] not in valid_mids or key in have:
                continue                          # 指向不存在错题的脏记录 / 两边都有的复习记录
            conn.execute(
                "INSERT INTO review_records(mistake_id, reviewed_at, is_correct, note) VALUES(?,?,?,?)",
                (r.get("mistake_id"), r.get("reviewed_at") or now_str(),
                 int(r.get("is_correct") or 0), r.get("note") or ""))
            have.add(key)
            r_add += 1
    n_img, n_aud = _extract_zip_files(zf, overwrite=False)
    return {"mode": "merge", "backup": backup, "subjects_added": sub_added,
            "mistakes_added": m_add, "mistakes_updated": m_upd, "mistakes_kept": m_keep,
            "reviews_added": r_add, "images_added": n_img, "audio_added": n_aud}


# ----------------------------------------------------------------------------
# 大模型调用（识别 + 讲解一步到位；封装成独立接口，便于后续换成 PaddleOCR）
# ----------------------------------------------------------------------------
def _proxy_map(cfg):
    proxy = (cfg.get("proxy") or "").strip()
    if not proxy:
        return {}
    return {"http": proxy, "https": proxy}


def _http_json(url, body=None, headers=None, method="POST", cfg=None, timeout=None):
    cfg = cfg or load_config()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(_proxy_map(cfg)))
    with opener.open(req, timeout=timeout or int(cfg.get("timeout", 180))) as resp:
        return json.loads(resp.read().decode("utf-8"))


# 推理模型（deepseek-flash 这类）的「思考过程」也算进 max_tokens：
# 一次发 6 页整卷时实测思考会烧掉 2 万多个 token，把预算吃光后正文一个字都出不来，
# 用户看到的就是「分析不了」。所以调模型这一层要能自己兜底，不能指望用户去设置页调参数。
LLM_FALLBACK_MAX_TOKENS = 32000   # 兜底时把预算抬到这里（实测 6 页整卷 25855 够用）
# 记下哪些接口不认 reasoning_effort，之后不再白试（进程内缓存即可）
_EFFORT_UNSUPPORTED = set()


def _looks_like_unknown_param(detail):
    """服务商报「不认识这个参数」时的各种可能说法（OpenAI 兼容实现五花八门）。"""
    d = (detail or "").lower()
    return ("reasoning_effort" in d or "unrecognized" in d or "unknown" in d
            or "unexpected" in d or "extra_forbidden" in d or "not permitted" in d)


def _llm_post(cfg, key, base, body, effort):
    """发一次 chat/completions。
    附带 reasoning_effort 时若服务商不认这个参数（换个模型/换个平台就可能不认），
    自动去掉它重发一次，而不是把整个识别搞失败。"""
    def _send(b):
        return _http_json(base + "/chat/completions", b,
                          {"Authorization": "Bearer " + key}, cfg=cfg)

    use_effort = effort and base not in _EFFORT_UNSUPPORTED
    b = dict(body)
    if use_effort:
        b["reasoning_effort"] = effort
    try:
        return _send(b)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:600]
        if use_effort and _looks_like_unknown_param(detail):
            _EFFORT_UNSUPPORTED.add(base)
            print("[llm] %s 不支持 reasoning_effort，已去掉该参数重试" % base)
            try:
                return _send(body)
            except HTTPError as exc2:
                detail = exc2.read().decode("utf-8", "ignore")[:600]
                exc = exc2
        hint = ""
        if exc.code == 404 or "model" in detail.lower():
            hint = "（模型名可能已下线，请在「设置」页点『拉取可用模型』重新选择）"
        raise RuntimeError("模型接口返回 HTTP %s%s：%s" % (exc.code, hint, detail))
    except URLError as exc:
        raise RuntimeError("连不上模型接口：%s（如在国内直连 OpenAI 需在设置里填代理）" % exc.reason)
    except socket.timeout:
        raise RuntimeError("模型接口超时，可在「设置」页把超时调大或换更快的模型")


def _extract_content(data):
    """取出正文、finish_reason、思考 token 数。"""
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("模型未返回结果：%s" % json.dumps(data, ensure_ascii=False)[:400])
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # 部分模型返回分段内容
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    usage = data.get("usage") or {}
    return ((content or "").strip(),
            choices[0].get("finish_reason") or "",
            usage,
            (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))


def call_llm(payload):
    cfg = load_config()
    key = (cfg.get("api_key") or "").strip()
    if not key:
        raise RuntimeError(
            "还没有配置大模型 API Key。请在「设置」页填入硅基流动 / 火山方舟 / OpenAI 的 Key 后再试。"
        )
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base.startswith("http"):
        raise RuntimeError("接口地址不正确：%s" % base)

    mt = int(payload.get("max_tokens") or cfg.get("max_tokens") or 2000)
    effort = (payload.get("reasoning_effort") or "").strip()
    messages = payload.get("messages") or []

    def body_of(_mt, _eff):
        b = {
            "model": cfg.get("model"),
            "messages": messages,
            "temperature": float(cfg.get("temperature", 0.2)),
            "max_tokens": _mt,
            "stream": False,
        }
        return b, _eff

    # 兜底阶梯：① 按请求原样 → ② 关掉思考（预算全留给正文）→ ③ 关思考 + 抬预算
    # 越往后越「省思考、保输出」：宁可回答得朴素一点，也不能一个字都不给。
    steps = [body_of(mt, effort)]
    if effort != "none":
        steps.append(body_of(mt, "none"))
        if mt < LLM_FALLBACK_MAX_TOKENS:
            steps.append(body_of(LLM_FALLBACK_MAX_TOKENS, "none"))

    last = None
    for idx, (body, eff) in enumerate(steps):
        data = _llm_post(cfg, key, base, body, eff)
        content, finish, usage, rtok = _extract_content(data)
        print("[llm] model=%s tokens: prompt=%s reasoning=%s completion=%s "
              "finish=%s max_tokens=%s effort=%s%s" % (
                  cfg.get("model"), usage.get("prompt_tokens"), rtok,
                  usage.get("completion_tokens"), finish, body["max_tokens"],
                  eff or "默认", "  <== 兜底第%d档" % idx if idx else ""))
        if content:
            return content
        last = (finish, body["max_tokens"], rtok)
        # 正文为空：多半是思考把预算吃光了 —— 换下一档（关思考）再试，不打扰用户
        if idx + 1 < len(steps):
            print("[llm] 第%d档正文为空（finish=%s），自动降到第%d档重试" % (idx, finish, idx + 1))

    finish, used_mt, rtok = last or ("", mt, None)
    if finish == "length":
        raise RuntimeError(
            "模型把 %s 个 token 全用在思考上、没来得及写答案（已自动关掉思考重试过仍不够）。"
            "可以：① 少传几页（比如一次 3～4 页）分两次识别；② 换个更快的/非推理模型；"
            "③ 到「设置」页把「Token 预算」再调大。" % (rtok or used_mt))
    raise RuntimeError("模型返回了空内容（已自动重试过）。请稍后再试一次；若反复出现，请到「设置」页换一个模型。")


def list_models():
    cfg = load_config()
    key = (cfg.get("api_key") or "").strip()
    if not key:
        raise RuntimeError("请先填写 API Key")
    base = (cfg.get("base_url") or "").rstrip("/")
    data = _http_json(base + "/models", None, {"Authorization": "Bearer " + key},
                      method="GET", cfg=cfg, timeout=40)
    ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
    return sorted(ids)


# ----------------------------------------------------------------------------
# HTTP 服务
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "CuotiBen/" + VERSION

    def log_message(self, fmt, *args):
        if os.environ.get("CTB_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---------- 基础工具 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        extra = dict(extra or {})
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # extra 里带了自己的 Cache-Control（图片/音频的可缓存头）就别再发默认 no-store，
        # 否则同一响应出现两个 Cache-Control，浏览器按最严格口径执行，可缓存头等于白发
        self.send_header("Cache-Control", extra.pop("Cache-Control", "no-store"))
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _err(self, message, code=400):
        self._json({"ok": False, "error": str(message)}, code)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _raw_body(self):
        """原始请求体字节（zip 上传等非 JSON 场景；_body 是 JSON 解析版）"""
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if path == "/api/health":
                cfg = load_config()
                return self._json({
                    "ok": True, "storage": "sqlite", "version": VERSION,
                    "ai_configured": bool((cfg.get("api_key") or "").strip()),
                    "model": cfg.get("model"), "db": os.path.basename(DB_PATH),
                    "dir_id": DIR_ID,     # 数据目录指纹（自检脚本用它确认「连的是不是自己这个实例」）
                })
            if path == "/api/subjects":
                with connect() as conn:
                    rows = conn.execute("SELECT * FROM subjects ORDER BY sort_order, id").fetchall()
                return self._json({"ok": True, "items": [dict(r) for r in rows]})
            if path == "/api/mistakes":
                return self._json({"ok": True, "items": list_mistakes(params)})
            if path.startswith("/api/mistakes/"):
                mid = int(path.rsplit("/", 1)[-1])
                item = get_mistake(mid)
                if not item:
                    return self._err("错题不存在", 404)
                return self._json({"ok": True, "item": item})
            if path == "/api/stats":
                return self._json({"ok": True, "stats": stats()})
            if path == "/api/orphan_images":
                # 集中删除标删题之后的孤儿原图盘点（删题不删图策略的配套）
                return self._json({"ok": True, "items": orphan_images()})
            if path == "/api/qrcode":
                # 看板上的「手机扫码」图：默认编码局域网地址；?url= 可指定（面板复用）。
                # 二维码内容不变，允许浏览器缓存，省得每次进看板都重画。
                url = (params.get("url") or "").strip() or lan_base_url()
                if not url.startswith("http://") and not url.startswith("https://"):
                    return self._err("非法 URL", 400)
                png = qr_png_bytes(url)
                return self._send(200, png, "image/png", {"Cache-Control": "public, max-age=300"})
            if path == "/api/config":
                return self._json({"ok": True, "config": public_config()})
            if path == "/api/models":
                try:
                    return self._json({"ok": True, "items": list_models()})
                except Exception as exc:
                    return self._err(exc, 502)
            if path == "/api/export":
                if params.get("format") == "zip":
                    # 整包导出（含图片/音频）；文件名自动带导出日期，浏览器直接落盘
                    stamp = datetime.now().strftime("%Y%m%d")
                    disp = 'attachment; filename="cuotiben-export-%s.zip"; filename*=UTF-8\'\'%s' % (
                        stamp, quote("错题数据导出-%s.zip" % stamp))
                    return self._send(200, export_zip(), "application/zip",
                                      {"Content-Disposition": disp})
                return self._json({"ok": True, "data": export_all()})
            if path == "/api/draft":
                # 未保存的识别结果（误触离开后从这里找回）
                return self._json({"ok": True, "draft": load_draft()})
            if path.startswith("/images/"):
                return self._serve_file(IMAGE_DIR, path[len("/images/"):])
            if path.startswith("/audio/"):
                return self._serve_file(AUDIO_DIR, path[len("/audio/"):])
            return self._serve_static(path)
        except Exception as exc:  # noqa: BLE001
            return self._err("服务端错误：%s" % exc, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/orphan_images":
                # 执行孤儿图清理：移到 backup_孤儿图_<时间>/，不直接删
                return self._json({"ok": True, **orphan_cleanup()})
            if path == "/api/mistakes":
                return self._json({"ok": True, "item": create_mistake(self._body())})
            if path == "/api/mistakes/batch":
                try:
                    items = create_mistakes_batch(self._body())
                except ValueError as exc:
                    return self._err(exc, 400)
                return self._json({"ok": True, "items": items, "created": len(items)})
            if path.endswith("/review") and path.startswith("/api/mistakes/"):
                mid = int(path.split("/")[3])
                body = self._body()
                result = add_review(mid, bool(body.get("is_correct")), body.get("note") or "")
                return self._json({"ok": True, **result})
            if path == "/api/llm":
                body = self._body()
                try:
                    content = call_llm(body)
                except Exception as exc:  # noqa: BLE001
                    return self._err(exc, 502)
                return self._json({"ok": True, "content": content})
            if path == "/api/asr":
                # 听力音频先转文字（模型不认识音频，只能走这条路）
                body = self._body()
                try:
                    text = asr_transcribe(body)
                except Exception as exc:  # noqa: BLE001
                    return self._err(exc, 502)
                return self._json({"ok": True, "text": text})
            if path == "/api/config":
                body = self._body() or {}
                cfg = load_config()
                patch = {k: v for k, v in body.items() if k in DEFAULT_CONFIG}
                # 两个 Key 字段遵循同一套约定：null = 清空；空串 = 不修改（避免前端留空就把已存的 Key 抹掉）
                for kf in ("api_key", "asr_api_key"):
                    if kf in patch and patch[kf] is None:
                        patch[kf] = ""
                    elif patch.get(kf) == "" and cfg.get(kf):
                        patch.pop(kf, None)
                save_config(patch)
                return self._json({"ok": True, "config": public_config()})
            if path == "/api/import":
                raw = self._raw_body()
                if raw[:2] == b"PK":          # 新版 zip 整包（含图片/音频）；?mode=merge|replace
                    qmode = parse_qs(parsed.query).get("mode", ["merge"])[0]
                    try:
                        summary = import_zip(raw, qmode)
                    except ValueError as exc:
                        return self._err(str(exc), 400)
                    return self._json({"ok": True, "summary": summary})
                # 老版 JSON 备份（只有题目行，没有图片）保持原路径不变。
                # 注意：流只能读一次，JSON 得从同一份 raw 里解，不能回头再调 _body()
                try:
                    legacy = json.loads(raw.decode("utf-8"))
                except Exception:
                    legacy = {}
                return self._json({"ok": True, "imported": import_all(legacy)})
            if path == "/api/draft":
                # 暂存未保存的识别结果：图片落盘、题目文字进 draft.json
                try:
                    draft = save_draft(self._body())
                except ValueError as exc:
                    return self._err(exc, 400)
                return self._json({"ok": True, "draft": draft})
            return self._err("未知接口：%s" % path, 404)
        except Exception as exc:  # noqa: BLE001
            return self._err("服务端错误：%s" % exc, 500)

    def do_PUT(self):
        path = unquote(urlparse(self.path).path)
        if path.startswith("/api/mistakes/"):
            mid = int(path.rsplit("/", 1)[-1])
            item = update_mistake(mid, self._body())
            if not item:
                return self._err("错题不存在", 404)
            return self._json({"ok": True, "item": item})
        return self._err("未知接口：%s" % path, 404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if path == "/api/draft":
            # images=1 表示用户主动「放弃」，连没被错题引用的草稿图一起删；
            # 保存错题后调用的是不带参数的版本（图已转正，必须留）
            removed = clear_draft(delete_images=(params.get("images") == "1"))
            return self._json({"ok": True, "removed_images": removed})
        if path.startswith("/api/mistakes/"):
            mid = int(path.rsplit("/", 1)[-1])
            delete_mistake(mid)
            return self._json({"ok": True})
        return self._err("未知接口：%s" % path, 404)

    # ---------- 静态文件 ----------
    def _serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        if ".." in rel:
            return self._err("非法路径", 403)
        full = os.path.join(WEB_DIR, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            return self._err("找不到文件：%s" % path, 404)
        return self._serve_file(WEB_DIR, rel)

    def _serve_file(self, root, rel):
        if ".." in rel:
            return self._err("非法路径", 403)
        full = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            return self._err("找不到文件", 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype == "application/octet-stream" and full.endswith(".webmanifest"):
            ctype = "application/manifest+json"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json",
                                                  "application/manifest+json"):
            ctype += "; charset=utf-8"
        # 图片与音频允许浏览器长期缓存：文件名带时间戳（20260926-xxx.jpg），内容一旦落盘不可变。
        # 以前全局 no-store，列表页每次进入都把同一批原图（十几 MB）重新下载一遍，
        # 手机上既慢又费流量；HTML/JSON 保持 no-store，保证升级后刷新即生效。
        cache = ("public, max-age=31536000, immutable"
                 if ctype.startswith(("image/", "audio/")) else "no-store")
        with open(full, "rb") as f:
            data = f.read()
        return self._send(200, data, ctype, {"Cache-Control": cache})


# ----------------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------------
def local_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return ips or ["127.0.0.1"]


def lan_base_url():
    """手机访问用的基地址（挑第一个非回环 IP；Tailscale 的 100.x 排后面也不影响第一个）"""
    for ip in local_ips():
        if not ip.startswith("127.") and not ip.startswith("100."):
            return "http://%s:%d" % (ip, PORT)
    return "http://%s:%d" % (local_ips()[0], PORT)


def qr_png_bytes(url, scale=6):
    """URL → PNG 字节（segno 随包自带，纯 Python，零依赖铁律不破）"""
    import io as _io
    import segno  # noqa
    buf = _io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=scale, border=2, dark="#1b2430", light="#ffffff")
    return buf.getvalue()


def print_qr(url):
    """可选：装了 segno / qrcode 就在窗口里画一个二维码，手机直接扫。"""
    try:
        import segno  # noqa
        segno.make(url, error="m").terminal(compact=True)
        return True
    except Exception:
        pass
    try:
        import qrcode  # noqa
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make()
        qr.print_ascii(invert=True)
        return True
    except Exception:
        return False


def orphan_images():
    """images/ 里已经没有任何**未删除**错题引用的图片（集中删除题之后留下来 =
    「删题不删图」策略的产物）。返回相对路径列表，前端拿去展示、由用户确认后清理。"""
    refs = set()
    try:
        with connect() as conn:
            for (imgs,) in conn.execute(
                    "SELECT question_image FROM mistakes WHERE deleted=0 AND question_image<>''"):
                for p in str(imgs).split("|"):
                    p = p.strip()
                    if p:
                        refs.add(p)
    except Exception:
        return []
    orphans = []
    if os.path.isdir(IMAGE_DIR):
        for f in sorted(os.listdir(IMAGE_DIR)):
            if f.startswith(".") or f.startswith(DRAFT_IMG_PREFIX):
                continue                       # 草稿图归草稿管，不在这里算
            rel = "images/" + f
            if rel not in refs:
                orphans.append(rel)
    return orphans


def orphan_cleanup():
    """把孤儿图移到 backup_孤儿图_<日期>/（不直接删 —— 万一盘点口径有偏差还能找回来）。
    只动 images/ 下真实存在、且没有任何未删除错题引用的文件。"""
    orphans = orphan_images()
    moved, missing = [], 0
    if not orphans:
        return {"moved": [], "missing": 0}
    dest = os.path.join(BASE_DIR, "backup_孤儿图_" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(dest, exist_ok=True)
    for rel in orphans:
        # rel 形如 "images/xxx.jpg"，文件实际在 IMAGE_DIR（data/images/）下 ——
        # 不能拿 BASE_DIR 直接拼，否则差一层 data/、一张都动不了
        full = os.path.join(IMAGE_DIR, os.path.basename(rel))
        if not os.path.isfile(full):
            missing += 1
            continue
        shutil.move(full, os.path.join(dest, os.path.basename(full)))
        moved.append(rel)
    return {"moved": moved, "missing": missing, "dest": dest}


def data_summary():
    """启动时亮一下数据概况 —— 复制/升级完文件夹，一眼就能确认错题还在不在。"""
    try:
        if not os.path.exists(DB_PATH):
            return "空（还没录错题）"
        conn = connect()
        try:
            live = conn.execute("SELECT COUNT(*) FROM mistakes WHERE deleted=0").fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0]
            rev = conn.execute("SELECT COUNT(*) FROM review_records").fetchone()[0]
        finally:
            conn.close()
        imgs = 0
        if os.path.isdir(IMAGE_DIR):
            imgs = len([f for f in os.listdir(IMAGE_DIR) if not f.startswith(".")])
        audios = 0
        if os.path.isdir(AUDIO_DIR):
            audios = len([f for f in os.listdir(AUDIO_DIR) if not f.startswith(".")])
        s = "错题 %d 条（另有已删 %d 条）· 复习记录 %d 条 · 原图 %d 张" % (
            live, total - live, rev, imgs)
        if audios:
            s += " · 听力音频 %d 个" % audios
        if os.path.exists(DRAFT_PATH):
            d = load_draft() or {}
            pend = len(_pending_draft_images(d))
            s += "\n         有 1 份没保存完的识别结果（%d 题%s）—— 首页点「继续整理」可找回" % (
                len(d.get("items") or []),
                "，含 %d 张待确认原图" % pend if pend else "")
        return s
    except Exception as exc:
        return "读取失败：%s" % exc


def print_banner():
    cfg = load_config()
    line = "=" * 60
    print(line)
    print("  错题本 · 本地服务已启动  (Ctrl+C 停止)")
    print(line)
    print("  电脑访问：http://127.0.0.1:%d" % PORT)
    ips = local_ips()
    for ip in ips:
        print("  手机访问：http://%s:%d" % (ip, PORT))
    print(line)
    if ips and ips[0] != "127.0.0.1":
        if print_qr("http://%s:%d" % (ips[0], PORT)):
            print("  ↑ 用手机相机/微信扫上面这个二维码，直接打开错题本")
        else:
            print("  （此窗口画不出二维码时，双击「生成手机二维码.bat」另存一张 PNG 再扫，")
            print("    或直接在手机浏览器里输入上面的「手机访问」网址）")
    print("  手机上：浏览器打开「手机访问」网址 → 加到主屏幕，用起来跟 App 一样")
    print("  模型：%s | %s" % (cfg.get("model"), "已配置 Key"
                              if (cfg.get("api_key") or "").strip() else "未配置 Key（去「设置」页填）"))
    # 听力转写单独亮一行：它是另一家的接口、另一个 Key，配没配要能一眼看到
    try:
        pc = public_config(cfg)
        if pc.get("asr_ready"):
            print("  听力：已就绪 🎧（%s，英语听力题会结合录音检查）" % pc.get("asr_model"))
        elif (cfg.get("asr_api_key") or "").strip():
            print("  听力：Key 已填但接口地址/模型名不完整，去「设置」页核对")
        else:
            print("  听力：未配转写 Key（英语卷的听力题会被跳过；去「设置」页填硅基流动的 Key）")
    except Exception:
        pass
    print("  数据：%s" % DB_PATH)
    print("        %s" % data_summary())
    print(line)


def main():
    if sys.version_info < (3, 7):
        print("需要 Python 3.7 及以上版本")
        return 1
    init_db()
    output = None
    try:
        output = open(os.path.join(DATA_DIR, "server-out.log"), "a", encoding="utf-8")
    except Exception:
        pass
    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as exc:
        print("[错误] 端口 %d 被占用或无权限：%s" % (PORT, exc))
        print("       可换个端口：set CTB_PORT=8790 && python server.py")
        return 1
    print_banner()
    if output:
        print_banner_to(output)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
        if output:
            output.close()
    return 0


def print_banner_to(stream):
    for ip in local_ips():
        stream.write("%s http://%s:%d\n" % (now_str(), ip, PORT))
    stream.flush()


if __name__ == "__main__":
    sys.exit(main())
