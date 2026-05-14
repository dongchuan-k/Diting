#!/usr/bin/env python3
"""
谛听（DiTing） — 微信聊天 AI 总结助手 v1.5

用法：
    diting init                          首次初始化配置
    diting doctor                        系统自检
    diting --check                       检查密钥是否可用
    diting list                          列出所有会话
    diting list --groups                 只列群聊
    diting list --contacts               只列联系人
    diting search "关键词"               搜索消息
    diting export -c "会话名"            导出消息
    diting export -c "会话名" --today    导出今天消息
    diting summarize -c "会话名"         总结指定会话
    diting summarize --all --today       一键总结今天所有配置的会话
    diting summarize --all --yesterday   一键总结昨天
    diting --version                      显示版本号
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

VERSION = "1.5.0"

# --- 路径常量（可从 config 中覆盖） ---
WORKDIR = os.path.expanduser("~/.diting")
TOOL_DIR = os.path.join(WORKDIR, "wechat-db-decrypt-macos")
VENV_PYTHON = os.path.join(WORKDIR, "venv", "bin", "python3")
CONFIG_PATH = os.path.join(WORKDIR, "config.yaml")
DECRYPTED_DIR = os.path.join(WORKDIR, "decrypted")
KEYS_FILE = os.path.join(TOOL_DIR, "wechat_keys.json")

# 默认路径（会在 load_config 后被覆盖）
TEMP_DIR = os.path.join(WORKDIR, "tmp")
OUTPUT_DIR = os.path.join(WORKDIR, "outputs")

# 微信数据库根路径
WECHAT_DB_BASE = os.path.expanduser(
    "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
)


# ═══════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════

def eprint(*args, **kwargs):
    """输出到 stderr"""
    print(*args, file=sys.stderr, **kwargs)


def get_python():
    """获取 venv Python 或兜底系统 Python"""
    if os.path.exists(VENV_PYTHON):
        return VENV_PYTHON
    for p in ["/opt/homebrew/bin/python3.12", "/usr/bin/python3"]:
        if os.path.exists(p):
            return p
    return sys.executable


def ensure_dirs():
    """确保必要目录存在，并设置严格权限"""
    for d in [WORKDIR, OUTPUT_DIR, DECRYPTED_DIR, TEMP_DIR]:
        os.makedirs(d, exist_ok=True)
    try:
        for d in [WORKDIR, OUTPUT_DIR, DECRYPTED_DIR, TEMP_DIR]:
            if os.path.isdir(d):
                os.chmod(d, 0o700)
    except Exception:
        pass


def assert_safe_temp_dir(path):
    """
    安全校验临时目录路径，防止误删重要目录。
    TEMP_DIR 必须位于 ~/.diting/ 下，且不能是根目录、HOME 或 WORKDIR 本身。
    """
    path = os.path.abspath(os.path.expanduser(path))
    workdir = os.path.abspath(os.path.expanduser("~/.diting"))

    if not path.startswith(workdir + os.sep):
        raise ValueError(
            f"temp_dir 必须位于 ~/.diting/ 目录下，当前值: {path}"
        )

    forbidden = {
        "/",
        os.path.expanduser("~"),
        workdir,
    }
    if path in forbidden:
        raise ValueError(f"危险的 temp_dir，拒绝删除: {path}")

    return path


# ═══════════════════════════════════════════════════
# P0-1: 微信路径动态扫描（不写死任何 wxid）
# ═══════════════════════════════════════════════════

def scan_wechat_accounts():
    """
    扫描 Mac 微信本地账号目录。
    返回: [(display_name, db_storage_path), ...]
    """
    pattern = os.path.join(WECHAT_DB_BASE, "wxid_*", "db_storage")
    dirs = sorted(glob.glob(pattern))

    if not dirs:
        # 再试一次，可能路径结构不同
        pattern2 = os.path.join(WECHAT_DB_BASE, "*", "db_storage")
        dirs = sorted(glob.glob(pattern2))

    accounts = []
    for d in dirs:
        parent_dir = os.path.basename(os.path.dirname(d))
        display = parent_dir  # wxid_xxx
        # 看看有没有 Message 目录下有数据，确认是真的数据库目录
        msg_dir = os.path.join(d, "Message")
        if os.path.isdir(msg_dir) and os.listdir(msg_dir):
            accounts.append((display, d))

    return accounts


def get_wechat_db_dir(config):
    """
    获取微信数据库目录（动态扫描，不写死）。
    优先级: config > 自动扫描
    """
    # 1. 检查配置中是否指定了固定目录
    wechat_cfg = config.get("wechat", {})
    fixed_dir = wechat_cfg.get("account_dir", "auto")
    if fixed_dir and fixed_dir != "auto":
        expanded = os.path.expanduser(fixed_dir)
        if os.path.isdir(expanded):
            return expanded
        eprint(f"[警告] 配置的 account_dir 不存在: {expanded}，将自动扫描")

    # 2. 自动扫描
    accounts = scan_wechat_accounts()
    if not accounts:
        return None

    if len(accounts) == 1:
        return accounts[0][1]

    # 3. 多个账号
    eprint(f"[错误] 发现 {len(accounts)} 个微信账号，无法自动选择：")
    for i, (name, path) in enumerate(accounts, 1):
        eprint(f"  {i}. {name} ({path})")
    eprint("请在 config.yaml 的 wechat.account_dir 中指定要使用的账号路径")
    eprint("或运行 diting init 进行初始化")
    return None


# ═══════════════════════════════════════════════════
# P0-3: 重构的解密逻辑（不 patch 第三方脚本）
# ═══════════════════════════════════════════════════

def extract_keys(quiet=False):
    """提取微信数据库密钥"""
    if not quiet:
        eprint("🔑 正在提取微信数据库密钥...")

    env = os.environ.copy()
    try:
        lldb_p = subprocess.run(
            ["/opt/homebrew/opt/llvm/bin/lldb", "-P"],
            capture_output=True, text=True, timeout=10
        ).stdout.strip()
        if lldb_p:
            env["PYTHONPATH"] = lldb_p
    except Exception:
        pass

    key_script = os.path.join(TOOL_DIR, "find_key_memscan.py")
    if not os.path.exists(key_script):
        eprint("[错误] 找不到密钥提取脚本，请先运行 install.sh 安装")
        return False

    result = subprocess.run(
        [get_python(), key_script],
        capture_output=True, text=True, cwd=TOOL_DIR, env=env, timeout=60
    )

    success = "keys found" in result.stdout
    if not quiet:
        if success:
            eprint("✅ 密钥提取成功")
        else:
            eprint("[警告] 密钥提取可能不完整，请检查 SIP 状态和微信版本")
    return success


def check_keys_valid():
    """检查当前密钥文件是否可用"""
    return os.path.exists(KEYS_FILE) and os.path.getsize(KEYS_FILE) > 0


def decrypt_db_copy(src_db_dir, quiet=False):
    """
    复制数据库到临时目录，然后直接调用 sqlcipher 参数化解密。
    绝不修改微信原始数据库，不依赖字符串 patch 第三方脚本。

    解密链路：
    1. 复制数据库到 TEMP_DIR
    2. 读取 wechat_keys.json 获取密钥
    3. 直接调用 sqlcipher 对 TEMP_DIR 中的副本解密到 DECRYPTED_DIR
    4. 验证输出是可读的 SQLite 数据库

    返回: (成功标志, 解密后的数据库目录路径)
    """
    if not quiet:
        eprint(f"📁 源数据库目录: {src_db_dir}")

    # 1. 清理旧的临时目录
    if os.path.exists(TEMP_DIR):
        assert_safe_temp_dir(TEMP_DIR)
        shutil.rmtree(TEMP_DIR)

    # 2. 复制数据库目录到临时目录
    if not quiet:
        eprint(f"📋 复制到临时目录: {TEMP_DIR}")
    try:
        shutil.copytree(src_db_dir, TEMP_DIR, symlinks=True)
    except Exception as e:
        eprint(f"[错误] 复制数据库失败: {e}")
        return False, None

    if not quiet:
        eprint("✅ 已复制数据库到临时目录（原始数据库未被修改）")

    # 3. 读取密钥文件
    if not os.path.exists(KEYS_FILE):
        eprint("[错误] 密钥文件不存在，请先运行 install.sh 提取密钥")
        return False, None

    try:
        with open(KEYS_FILE, "r", encoding="utf-8") as f:
            keys_data = json.load(f)
    except Exception as e:
        eprint(f"[错误] 读取密钥文件失败: {e}")
        return False, None

    entries = {k: v for k, v in keys_data.items() if not k.startswith("__")}
    if not entries:
        eprint("[错误] 密钥文件中没有有效的数据库密钥")
        return False, None

    # 4. 查找 sqlcipher
    sqlcipher_bin = None
    brew_path = "/opt/homebrew/opt/sqlcipher/bin/sqlcipher"
    if os.path.isfile(brew_path):
        sqlcipher_bin = brew_path
    else:
        for p in os.environ.get("PATH", "").split(":"):
            candidate = os.path.join(p, "sqlcipher")
            if os.path.isfile(candidate):
                sqlcipher_bin = candidate
                break

    if not sqlcipher_bin:
        eprint("[错误] sqlcipher 未找到，请运行 brew install sqlcipher")
        return False, None

    if not quiet:
        eprint(f"🔓 正在解密数据库副本（{len(entries)} 个库）...")

    # 5. 清理旧的解密输出
    if os.path.exists(DECRYPTED_DIR):
        shutil.rmtree(DECRYPTED_DIR)

    # 6. 逐个解密（参数化调用 sqlcipher，不依赖第三方脚本）
    passed = 0
    failed = 0
    for db_rel_path, key_hex in sorted(entries.items()):
        src = os.path.join(TEMP_DIR, db_rel_path)
        dst = os.path.join(DECRYPTED_DIR, db_rel_path)

        if not os.path.isfile(src):
            continue

        try:
            _decrypt_single_db(sqlcipher_bin, src, dst, key_hex)
            if os.path.isfile(dst) and os.path.getsize(dst) > 0:
                passed += 1
            else:
                failed += 1
        except Exception as e:
            if not quiet:
                eprint(f"  [警告] 解密 {db_rel_path} 失败: {e}")
            failed += 1

    if not quiet:
        eprint(f"  解密结果: {passed} 成功, {failed} 失败")

    # 7. 验证解密结果
    decrypted_dir = DECRYPTED_DIR
    decrypted_dbs = []
    if os.path.isdir(decrypted_dir):
        for root, dirs, files in os.walk(decrypted_dir):
            for f in files:
                if f.endswith(".db"):
                    db_path = os.path.join(root, f)
                    if os.path.getsize(db_path) > 0:
                        decrypted_dbs.append(db_path)

    if passed == 0:
        if not quiet:
            eprint("[错误] 没有数据库解密成功")
        return False, None

    # 验证输出是真实可读的 SQLite 数据库（用 sqlite3，而非 sqlcipher）
    verified_count = 0
    for db_path in decrypted_dbs:
        try:
            r = subprocess.run(
                ["sqlite3", db_path, "SELECT count(*) FROM sqlite_master;"],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0 and r.stdout.strip().isdigit():
                verified_count += 1
        except Exception:
            pass

    if verified_count == 0:
        if not quiet:
            eprint("[错误] 所有输出数据库都无法读取，解密可能失败")
        return False, None

    if not quiet:
        eprint(f"✅ 解密完成（{verified_count}/{len(decrypted_dbs)} 个数据库验证通过）")
        eprint(f"📁 解密后的目录: {decrypted_dir}（原始数据库未被修改）")

    return True, decrypted_dir


def _decrypt_single_db(sqlcipher_bin, src_path, dst_path, key_hex):
    """使用 sqlcipher 解密单个数据库文件"""
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)

    if os.path.exists(dst_path):
        os.remove(dst_path)

    sql_commands = f"""PRAGMA key = "x'{key_hex}'";
PRAGMA cipher_page_size = 4096;
ATTACH DATABASE '{dst_path}' AS plaintext KEY '';
SELECT sqlcipher_export('plaintext');
DETACH DATABASE plaintext;
"""

    result = subprocess.run(
        [sqlcipher_bin, src_path],
        input=sql_commands,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"sqlcipher 返回非零退出码: {result.stderr.strip()[:200]}")

    if not os.path.isfile(dst_path) or os.path.getsize(dst_path) == 0:
        raise RuntimeError("解密输出文件为空")


def refresh_data(config, quiet=False):
    """
    完整刷新流程：提取密钥 + 复制到临时目录 + 解密副本。
    绝不修改原始数据库。
    """
    if not quiet:
        eprint("🔄 正在刷新微信数据...")

    # 1. 提取密钥
    if not extract_keys(quiet=quiet):
        if not check_keys_valid():
            eprint("[错误] 密钥提取失败，请确认:")
            eprint("  1. SIP 已关闭？运行 csrutil status")
            eprint("  2. 微信版本是 4.x？")
            eprint("  3. 微信已登录？")
            eprint("  4. 是否只有一个微信进程？")
            return False

    # 2. 找到微信数据库目录
    db_dir = get_wechat_db_dir(config)
    if not db_dir:
        eprint("[错误] 找不到微信数据库目录。")
        eprint("请确认:")
        eprint("  1. 微信 Mac 版已安装并登录")
        eprint("  2. 微信已产生聊天记录")
        eprint("  3. 运行 diting doctor 检查环境")
        return False

    if not quiet:
        eprint(f"📁 微信账号: {os.path.basename(os.path.dirname(db_dir))}")

    # 3. 先清理旧的解密输出，避免误读上一次残留数据
    if os.path.isdir(DECRYPTED_DIR):
        shutil.rmtree(DECRYPTED_DIR)

    # 记录本次运行时间，用于验证输出文件
    run_start = time.time()

    # 4. 复制到临时目录再解密（decrypt_db_copy 内部已完成所有步骤）
    success, decrypted_path = decrypt_db_copy(db_dir, quiet=quiet)
    if success:
        # 验证解密输出文件是本次运行生成的
        valid_files = []
        if os.path.isdir(decrypted_path):
            for root, dirs, files in os.walk(decrypted_path):
                for f in files:
                    fpath = os.path.join(root, f)
                    if f.endswith(".db") and os.path.getmtime(fpath) >= run_start - 2:
                        valid_files.append(fpath)
        if not valid_files:
            if not quiet:
                eprint("[错误] 解密输出文件没有更新，可能是旧快照")
            return False
    return success


def get_decrypted_dir():
    """返回解密后的数据库目录"""
    return DECRYPTED_DIR


def clean_temp(force=False):
    """清理临时目录"""
    if os.path.exists(TEMP_DIR):
        if force or input("是否清理临时目录？(y/N): ").lower() == "y":
            assert_safe_temp_dir(TEMP_DIR)
            shutil.rmtree(TEMP_DIR)
            eprint("✅ 临时目录已清理")


# ═══════════════════════════════════════════════════
# P0-6: 改进的消息导出（不粗暴过滤中文）
# ═══════════════════════════════════════════════════

def run_export(args, decrypted_dir=None):
    """
    调用底层 export_messages.py 导出消息。
    decrypted_dir 明确指定解密后的数据库目录，不依赖第三方工具默认路径。
    """
    python = get_python()
    script = os.path.join(TOOL_DIR, "export_messages.py")
    if not os.path.exists(script):
        eprint("[错误] 找不到 export_messages.py，请先运行 install.sh 安装")
        return "", "script not found", -1

    # 明确传递解密目录给 export_messages.py
    effective_dir = decrypted_dir or get_decrypted_dir()
    final_args = ["-d", effective_dir] + list(args)

    result = subprocess.run(
        [python, script] + final_args,
        capture_output=True, text=True, cwd=TOOL_DIR
    )
    return result.stdout, result.stderr, result.returncode


def parse_message_line(line, config):
    """
    解析一行导出消息，返回结构化字典或 None（跳过无意义行）。
    保留所有可能的信息，不做中文过滤。
    """
    line = line.strip()
    if not line:
        return None

    # 系统元信息行跳过
    if line.startswith("====") or line.startswith("---"):
        return None
    if line.startswith("导出时间") or line.startswith("消息数量"):
        return None

    # 检测消息类型
    msg_type = "text"
    content = line

    # 配置过滤
    config_filter = config.get("filters", {})

    if "[图片]" in line or "[image]" in line:
        msg_type = "image"
        content = "[图片消息]"
    elif "[语音]" in line or "[audio]" in line:
        msg_type = "audio"
        content = "[语音消息]"
    elif "[视频]" in line or "[video]" in line:
        msg_type = "video"
        content = "[视频消息]"
    elif "[文件]" in line or "[file]" in line:
        msg_type = "file"
        content = re.sub(r'\[文件\].*?(\S+)', r'[文件消息] \1', line)
    elif "[链接]" in line or "[link]" in line:
        msg_type = "link"
        content = "[链接消息]"
    elif "[表情]" in line or "[sticker]" in line:
        msg_type = "sticker"
        if config_filter.get("skip_sticker", False):
            return None
        content = "[表情]"
    elif "type:" in line:
        if config_filter.get("skip_system_message", True):
            return None  # 系统消息跳过

    # 提取时间戳和发送者（如果格式匹配 [时间] 发送者: 内容）
    timestamp = ""
    sender = ""
    time_match = re.match(r'\[(\d{2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', line)
    if time_match:
        timestamp = time_match.group(1)
        rest = line[time_match.end():].strip()
        sender_match = re.match(r'^(.+?)[：:]', rest)
        if sender_match:
            sender = sender_match.group(1)
            content = rest[sender_match.end():].strip()

    # 消息长度限制（在解析完发送者/内容之后）
    max_len = config_filter.get("max_message_length", 0)
    if max_len > 0 and len(content) > max_len:
        content = content[:max_len] + "…（截断）"

    return {
        "timestamp": timestamp,
        "sender": sender,
        "type": msg_type,
        "content": content,
        "raw": line
    }


def export_chat(chat_name, limit=200, date_from=None, date_to=None, config=None):
    """
    导出指定会话的消息，返回结构化消息列表。
    按日期范围查询时用较大 limit 避免漏消息。

    ⚠️ 已知限制：当前日期筛选为先拉取大量消息再本地过滤，
    超活跃群可能因消息超过拉取上限而遗漏。
    """
    if config is None:
        config = {}

    # 按日期范围查询时，用大 limit 确保不遗漏
    effective_limit = 5000 if (date_from or date_to) else limit

    # 构建导出参数
    args = ["-c", chat_name, "-n", str(effective_limit)]

    stdout, stderr, rc = run_export(args)
    if rc != 0:
        eprint(f"[错误] 导出 '{chat_name}' 失败: {stderr[:200]}")
        return []

    # 解析消息行
    messages = []
    for line in stdout.split("\n"):
        parsed = parse_message_line(line, config)
        if parsed is None:
            continue

        # 日期范围过滤
        if date_from or date_to:
            # 只过滤有时间戳的行
            if parsed["timestamp"]:
                ts = parsed["timestamp"]
                try:
                    # 处理两种时间格式
                    if len(ts) > 10:
                        msg_date = ts[:10]
                    else:
                        msg_date = datetime.now().strftime("%Y-%m-%d")
                    if date_from and msg_date < date_from:
                        continue
                    if date_to and msg_date > date_to:
                        continue
                except:
                    pass

        messages.append(parsed)

        # 非日期范围查询时受 limit 限制
        if not (date_from or date_to) and len(messages) >= limit:
            break

    return messages


# ═══════════════════════════════════════════════════
# 配置管理
# ═══════════════════════════════════════════════════

DEFAULT_CONFIG = {
    "wechat": {
        "account_dir": "auto",
        "temp_dir": "~/.diting/tmp",
        "output_dir": "~/.diting/outputs"
    },
    "summary": {
        "message_limit": 200,
        "language": "zh-CN",
        "output_format": "markdown"
    },
    "sessions": {
        "groups": [],
        "contacts": []
    },
    "ai": {
        "enabled": False,
        "provider": "deepseek",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "temperature": 0.3,
        "api_key_env": "DEEPSEEK_API_KEY"
    },
    "privacy": {
        "redact_before_ai": True,
        "redact_phone": True,
        "redact_email": True,
        "redact_id_card": True,
        "redact_bank_card": True
    },
    "filters": {
        "skip_system_message": True,
        "skip_sticker": False,
        "max_message_length": 0
    },
    "schedule": {
        "enabled": False,
        "time": "09:00"
    }
}


def load_config():
    """加载配置，未配置的字段用默认值，并更新全局路径"""
    global TEMP_DIR, OUTPUT_DIR

    if not os.path.exists(CONFIG_PATH):
        return dict(DEFAULT_CONFIG)

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f) or {}
    except Exception as e:
        eprint(f"[警告] 配置文件读取失败: {e}，使用默认配置")
        return dict(DEFAULT_CONFIG)

    # 深度合并
    merged = dict(DEFAULT_CONFIG)
    _deep_merge(merged, user_config)

    # 从配置中读取路径并更新全局变量
    wechat_cfg = merged.get("wechat", {})
    if wechat_cfg.get("temp_dir"):
        TEMP_DIR = os.path.expanduser(wechat_cfg["temp_dir"])
    if wechat_cfg.get("output_dir"):
        OUTPUT_DIR = os.path.expanduser(wechat_cfg["output_dir"])

    # 确保目录存在
    ensure_dirs()

    return merged


def _deep_merge(base, override):
    """递归合并字典"""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def get_message_limit(config, args, session_name=None):
    """
    P0-5: 消息条数配置优先级
    1. 命令行 -n 参数
    2. 会话级配置（每个群/联系人可单独设置）
    3. 全局 summary.message_limit
    4. 默认值 200
    """
    # 命令行优先
    if args.number is not None:
        return args.number

    # 会话级配置
    if session_name:
        for g in config.get("sessions", {}).get("groups", []):
            if g.get("name") == session_name and g.get("message_limit"):
                return g["message_limit"]
        for c in config.get("sessions", {}).get("contacts", []):
            if c.get("name") == session_name and c.get("message_limit"):
                return c["message_limit"]

    # 全局配置
    return config.get("summary", {}).get("message_limit", 200)


# ═══════════════════════════════════════════════════
# 隐私脱敏
# ═══════════════════════════════════════════════════

def redact_text(text, config):
    """对文本进行脱敏处理"""
    privacy = config.get("privacy", {})
    if not privacy.get("redact_before_ai"):
        return text

    if privacy.get("redact_phone"):
        text = re.sub(r'1[3-9]\d{9}', '[手机号]', text)
    if privacy.get("redact_email"):
        text = re.sub(r'[\w.-]+@[\w.-]+\.\w+', '[邮箱]', text)
    if privacy.get("redact_id_card"):
        text = re.sub(r'\d{17}[\dXx]', '[身份证号]', text)
    if privacy.get("redact_bank_card"):
        text = re.sub(r'\d{16,19}', '[银行卡号]', text)

    return text


def redact_messages(messages, config):
    """对消息列表进行脱敏"""
    for msg in messages:
        msg["content"] = redact_text(msg["content"], config)
        msg["raw"] = redact_text(msg["raw"], config)
    return messages


# ═══════════════════════════════════════════════════
# AI 总结（分块 + Markdown 输出）
# ═══════════════════════════════════════════════════

def get_ai_api_key(config):
    """获取 AI API Key：优先环境变量"""
    ai_cfg = config.get("ai", {})
    if not ai_cfg.get("enabled"):
        return None, None, None

    env_var = ai_cfg.get("api_key_env", "DEEPSEEK_API_KEY")
    api_key = os.environ.get(env_var) or ai_cfg.get("api_key")
    if not api_key:
        return None, None, None

    provider = ai_cfg.get("provider", "deepseek")
    model = ai_cfg.get("model", "deepseek-chat")
    base_url = ai_cfg.get("base_url", "https://api.deepseek.com")

    return api_key, provider, model, base_url


def call_ai_api(prompt_text, config):
    """调用 AI API"""
    result = get_ai_api_key(config)
    if not result or result[0] is None:
        return None

    api_key, provider, model, base_url = result
    import requests

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt_text}],
        "temperature": config.get("ai", {}).get("temperature", 0.3),
        "max_tokens": 4000,
    }

    try:
        if provider == "openai":
            url = base_url or "https://api.openai.com/v1/chat/completions"
        else:
            url = f"{base_url}/chat/completions"

        resp = requests.post(url, headers=headers, json=payload, timeout=180)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        else:
            eprint(f"[警告] API 请求失败: {resp.status_code}")
            return None
    except Exception as e:
        eprint(f"[警告] API 调用异常: {e}")
        return None


def chunk_messages(messages, max_chars=3000):
    """将消息分块，每块不超过 max_chars 字符。支持 dict 和 str 列表。"""
    chunks = []
    current = []
    current_len = 0

    for msg in messages:
        if isinstance(msg, dict):
            line = _format_message_line(msg)
        else:
            line = str(msg)
        line_len = len(line)

        if current_len + line_len > max_chars and current:
            chunks.append(current)
            current = []
            current_len = 0

        current.append(line)
        current_len += line_len

    if current:
        chunks.append(current)

    return chunks


def _format_message_line(msg):
    """格式化一条消息为可读文本"""
    ts = msg.get("timestamp", "")
    sender = msg.get("sender", "")
    content = msg.get("content", "")

    if sender:
        return f"[{ts}] {sender}: {content}"
    elif content:
        return content
    return msg.get("raw", "")


def summarize_group_messages(messages, chat_name, config, date_label=""):
    """
    总结群聊消息，输出结构化 Markdown。
    支持分块处理长消息。
    """
    ai_result = get_ai_api_key(config)
    use_ai = ai_result[0] is not None

    fmt_messages = [_format_message_line(m) for m in messages]

    if not use_ai:
        return _format_export_markdown(chat_name, messages, fmt_messages, date_label)

    # 分块处理
    chunks = chunk_messages(fmt_messages)
    chunk_summaries = []

    for i, chunk_lines in enumerate(chunks):
        chunk_text = "\n".join(chunk_lines)
        prompt = f"""你是一个群聊信息流分析助手。分析以下群聊消息，生成结构化总结。

群聊名称：{chat_name}
时间范围：{date_label or "最近"}
消息数量：{len(chunk_lines)}

请按以下格式输出（Markdown）：

## 核心主题
- 列出1-3个主要讨论话题

## 关键决策
- 每项决策：内容、相关人员

## 待办事项
| 事项 | 负责人 | 状态 |
|---|---|---|

## 商机 / 机会
- 机会描述和线索

## 风险 / 异常
- 风险描述

## 关键人物观点
- 发言人: 主要观点

## 值得回看的消息
- [时间] 发送人: 原文摘要

没有相关内容就不要硬编，写"未发现明确内容"。

消息内容：
{chunk_text}"""

        summary = call_ai_api(prompt, config)
        if summary:
            chunk_summaries.append(summary)

    # 如果有多个分块，合并
    if len(chunk_summaries) > 1:
        combined = "\n\n---\n\n".join(chunk_summaries)
        merge_prompt = f"""以下是同一群聊多个时间段的消息摘要，请合并为一份完整的结构化报告，去除重复内容：

{combined}

请按之前的格式输出完整报告。"""
        final = call_ai_api(merge_prompt, config)
        if final:
            return final
        return combined
    elif chunk_summaries:
        return chunk_summaries[0]

    return _format_export_markdown(chat_name, messages, fmt_messages, date_label)


def summarize_private_messages(messages, contact_name, config, date_label=""):
    """
    总结私聊消息（不同于群聊模板，更关注关系、承诺、需求）。
    """
    ai_result = get_ai_api_key(config)
    use_ai = ai_result[0] is not None

    fmt_messages = [_format_message_line(m) for m in messages]

    if not use_ai:
        return _format_export_markdown(contact_name, messages, fmt_messages, date_label, is_group=False)

    chunks = chunk_messages(fmt_messages)
    chunk_summaries = []

    for i, chunk_lines in enumerate(chunks):
        chunk_text = "\n".join(chunk_lines)
        prompt = f"""你是一个私聊分析助手。分析以下与 {contact_name} 的聊天记录，生成结构化总结。

时间范围：{date_label or "最近"}
消息数量：{len(chunk_lines)}

请按以下格式输出（Markdown）：

## 沟通主题
- 主要讨论了什么

## 承诺事项
| 事项 | 承诺人 | 时间 |
|---|---|---|

## 客户需求 / 关注点
- 具体需求或关注点

## 下一步行动
- 需要做什么、谁做

## 关键引用
- [时间] 发送人: 值得注意的原文

没有相关内容不要硬编。

消息内容：
{chunk_text}"""

        summary = call_ai_api(prompt, config)
        if summary:
            chunk_summaries.append(summary)

    if len(chunk_summaries) > 1:
        return "\n\n---\n\n".join(chunk_summaries)
    elif chunk_summaries:
        return chunk_summaries[0]

    return _format_export_markdown(contact_name, messages, fmt_messages, date_label, is_group=False)


def _format_export_markdown(name, raw_messages, fmt_messages, date_label="", is_group=True):
    """格式化纯导出（无 AI 时）为 Markdown"""
    lines = [
        f"# {'群聊' if is_group else '私聊'}消息导出：{name}",
        f"",
        f"- 会话：{name}",
        f"- 时间范围：{date_label or '最近'}",
        f"- 消息数量：{len(raw_messages)}",
        f"- 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"",
        f"---",
        f"",
    ]
    for msg in raw_messages:
        lines.append(_format_message_line(msg))

    return "\n".join(lines)


# ═══════════════════════════════════════════════════
# P1-8: diting doctor 自检
# ═══════════════════════════════════════════════════

def run_doctor(config):
    """系统自检"""
    print("=" * 50)
    print("  谛听（DiTing）系统自检")
    print("=" * 50)
    print()

    checks = []

    # 1. Python 版本
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 8)
    checks.append(("Python 版本", py_ver, "通过" if py_ok else "建议升级到 Python 3.8+"))

    # 2. macOS
    checks.append(("操作系统", f"macOS ({os.uname().machine})",
                   "通过" if os.uname().machine == "arm64" else "仅支持 Apple Silicon"))

    # 3. WorkDir
    workdir_ok = os.path.isdir(WORKDIR)
    checks.append(("工作目录", WORKDIR, "通过" if workdir_ok else "未创建，请先运行 install.sh"))

    # 4. 虚拟环境（检查多个可能的位置）
    venv_candidates = [
        os.path.join(WORKDIR, "venv", "bin", "python3"),
        os.path.join(TOOL_DIR, "venv", "bin", "python3"),
    ]
    venv_found = None
    for vp in venv_candidates:
        if os.path.exists(vp):
            venv_found = vp
            break
    venv_ok = venv_found is not None
    venv_path = venv_found or str(venv_candidates[0])
    checks.append(("虚拟环境", str(venv_path), "通过" if venv_ok else "未创建，请先运行 install.sh"))

    # 5. Python 依赖（用 venv Python 检查）
    deps_ok = True
    deps_check_py = venv_found or VENV_PYTHON or get_python()
    try:
        r = subprocess.run(
            [deps_check_py, "-c", "import yaml; import requests; print('ok')"],
            capture_output=True, text=True, timeout=5
        )
        deps_ok = "ok" in r.stdout
    except:
        deps_ok = False
    checks.append(("Python 依赖 (yaml, requests)",
                   "已安装" if deps_ok else "未安装",
                   "通过" if deps_ok else "请运行 install.sh 或 pip install -r requirements.txt"))

    # 6. 解密工具
    tool_ok = os.path.isdir(TOOL_DIR) and os.path.exists(os.path.join(TOOL_DIR, "decrypt_db.py"))
    checks.append(("解密工具", str(TOOL_DIR), "通过" if tool_ok else "未安装，请先运行 install.sh"))

    # 7. sqlcipher
    sqlcipher_ok = False
    try:
        r = subprocess.run(["which", "sqlcipher"], capture_output=True, text=True)
        sqlcipher_ok = r.returncode == 0
    except:
        pass
    checks.append(("sqlcipher", "已安装" if sqlcipher_ok else "未安装",
                   "通过" if sqlcipher_ok else "请运行 brew install sqlcipher"))

    # 8. 微信路径
    accounts = scan_wechat_accounts()
    wechat_ok = len(accounts) > 0
    if wechat_ok:
        wechat_info = f"发现 {len(accounts)} 个账号"
    else:
        wechat_info = "未找到微信数据库目录"
    checks.append(("微信账号", wechat_info, "通过" if wechat_ok else "请确认微信已安装并登录"))

    # 9. 密钥
    key_ok = check_keys_valid()
    checks.append(("数据库密钥", "可用" if key_ok else "未提取或已失效",
                   "通过" if key_ok else "请关闭 SIP 后重新运行 install.sh 提取密钥"))

    # 10. AI API
    ai_result = get_ai_api_key(config)
    ai_enabled = config.get("ai", {}).get("enabled", False)
    if ai_enabled:
        ai_msg = f"已配置 ({config['ai']['provider']})" if ai_result[0] else "API Key 未设置"
        ai_status = "通过" if ai_result[0] else "建议设置 DEEPSEEK_API_KEY 环境变量"
    else:
        ai_msg = "未启用（仅导出模式）"
        ai_status = "通过（按需启用）"
    checks.append(("AI API", ai_msg, ai_status))

    # 输出结果
    all_pass = True
    for name, value, status in checks:
        icon = "✅" if "通过" in status or "仅导出" in status else "❌"
        if "通过" not in status:
            all_pass = False
        print(f"  {icon} {name}")
        print(f"    状态: {value}")
        if "通过" not in status and "建议" not in status:
            print(f"    建议: {status}")
        print()

    print("=" * 50)
    if all_pass:
        print("  ✅ 所有检查通过，谛听可以正常使用")
    else:
        print("  ⚠️ 部分检查未通过，请参考上面的建议修复")
    print("=" * 50)

    return all_pass


# ═══════════════════════════════════════════════════
# 密钥刷新
# ═══════════════════════════════════════════════════

def cmd_keys_refresh():
    """删除旧密钥文件，重新提取密钥"""
    print("=" * 50)
    print("  谛听（DiTing）密钥刷新")
    print("=" * 50)
    print()

    # 1. 删除旧密钥
    if os.path.exists(KEYS_FILE):
        print(f"🗑️  删除旧密钥文件: {KEYS_FILE}")
        os.remove(KEYS_FILE)
    else:
        print("ℹ️  旧密钥文件不存在，跳过删除")

    # 2. 重新提取
    print()
    print("🔑 开始重新提取密钥...")
    success = extract_keys()

    # 3. 设置权限
    if os.path.exists(KEYS_FILE):
        os.chmod(KEYS_FILE, 0o600)
        print(f"🔒 密钥文件权限已设置为 600")

    print()
    if success:
        print("✅ 密钥刷新成功！")
    else:
        print("❌ 密钥刷新失败，请检查:")
        print("  1. SIP 是否已关闭？运行 csrutil status")
        print("  2. 微信是否已登录？")
        print("  3. 是否只有一个微信进程？")

    print("=" * 50)
    return success


# ═══════════════════════════════════════════════════
# P1-7: diting init 初始化
# ═══════════════════════════════════════════════════

def run_init():
    """首次初始化流程"""
    print("=" * 50)
    print("  谛听（DiTing）初始化向导")
    print("=" * 50)
    print()

    ensure_dirs()

    # 1. 检查微信并选择账号
    accounts = scan_wechat_accounts()
    selected_account = None
    if accounts:
        print(f"✅ 发现 {len(accounts)} 个微信账号")
        if len(accounts) == 1:
            selected_account = accounts[0][1]
            print(f"   自动选择: {accounts[0][0]}")
        else:
            print()
            for i, (name, path) in enumerate(accounts, 1):
                print(f"  {i}. {name}")
            print()
            try:
                choice = input("请选择账号编号 (1-{}): ".format(len(accounts)))
                idx = int(choice.strip()) - 1
                if 0 <= idx < len(accounts):
                    selected_account = accounts[idx][1]
                    print(f"  已选择: {accounts[idx][0]}")
                else:
                    print("  无效选择，跳过")
            except (ValueError, EOFError):
                print("  跳过账号选择")
    else:
        print("⚠️ 未找到微信账号，请确认微信 Mac 版已安装并登录")
        print("   可稍后运行 diting doctor 重新检查")

    # 2. 检查安装
    tool_ok = os.path.isdir(TOOL_DIR) and os.path.exists(os.path.join(TOOL_DIR, "decrypt_db.py"))
    if tool_ok:
        print("✅ 解密工具已安装")
    else:
        print("⚠️ 解密工具未安装，请先运行 bash install.sh")

    # 3. 检查 config 并生成/更新
    if os.path.exists(CONFIG_PATH) and selected_account:
        # 已有 config，检查 account_dir 是否还是 auto
        cfg = load_config()
        current_dir = cfg.get("wechat", {}).get("account_dir", "auto")
        if current_dir == "auto":
            cfg["wechat"]["account_dir"] = selected_account
            try:
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
                os.chmod(CONFIG_PATH, 0o600)
                print(f"✅ 已更新账号目录到配置文件")
            except Exception as e:
                print(f"⚠️ 写入配置失败: {e}")
        else:
            print(f"✅ 配置文件已有账号目录: {current_dir}")
            yn = input("是否覆盖？(y/N): ").lower()
            if yn == "y":
                cfg["wechat"]["account_dir"] = selected_account
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
                print("✅ 已更新")
    elif not os.path.exists(CONFIG_PATH):
        print("📝 正在生成默认配置文件...")
        _write_default_config(selected_account)
        print(f"✅ 配置文件已创建: {CONFIG_PATH}")
        if not selected_account:
            print("   请编辑配置文件，填入要监控的群聊和联系人")
    else:
        print(f"✅ 配置文件已存在: {CONFIG_PATH}")
        print("   如需修改，请直接编辑")

    # 4. 引导 AI API
    print()
    print("💡 AI API 配置（可选）")
    print("   如果要用 AI 自动总结，请设置环境变量：")
    print("   export DEEPSEEK_API_KEY=sk-xxxxxxxxxxxx")
    print("   或者编辑 config.yaml 配置 ai.api_key")
    print()
    print("   不配置也可以使用导出模式（diting export）")

    print()
    print("=" * 50)
    print("  ✅ 初始化完成！")
    print()
    print("  下一步：")
    print("  1. 编辑 ~/.diting/config.yaml 填入要监控的群聊/联系人")
    print("  2. 运行 diting doctor 检查环境")
    print("  3. 运行 diting summarize --all --today 试试效果")
    print("=" * 50)


def _write_default_config(account_dir=None):
    """写入默认配置文件，可选指定账号目录"""
    config = dict(DEFAULT_CONFIG)
    if account_dir:
        config["wechat"]["account_dir"] = account_dir
    config["sessions"]["groups"] = [
        {"name": "项目对接群", "enabled": True},
        {"name": "部门群", "enabled": True},
    ]
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
        os.chmod(CONFIG_PATH, 0o600)  # 仅当前用户可读写
    except Exception as e:
        eprint(f"[错误] 配置文件写入失败: {e}")


# ═══════════════════════════════════════════════════
# CLI 命令入口
# ═══════════════════════════════════════════════════

def cmd_list(config, args):
    """列出会话，返回 True 表示成功"""
    list_groups = args.list_groups
    list_contacts = args.list_contacts

    # 先解密获取最新数据
    stdout, stderr, rc = run_export([])
    if rc != 0 and not stdout:
        eprint("[错误] 获取会话列表失败，请确认微信已登录且已安装解密工具")
        return False

    lines = stdout.split("\n")
    groups = []
    contacts = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 尝试判断是群聊还是联系人（导出工具本身的格式）
        if "群" in line or line.startswith("群"):
            groups.append(line)
        else:
            contacts.append(line)

    if list_groups or (not list_groups and not list_contacts):
        print(f"\n📢 群聊 ({len(groups)}):")
        for g in groups:
            print(f"  {g}")

    if list_contacts or (not list_groups and not list_contacts):
        print(f"\n👤 联系人 ({len(contacts)}):")
        for c in contacts:
            print(f"  {c}")
    print()

    return True


def cmd_export(config, args):
    """导出消息，返回 True 表示成功"""
    chat_name = args.chat
    limit = get_message_limit(config, args)
    date_from, date_to = _parse_date_args(args)

    print(f"📥 正在导出 '{chat_name}' 的消息...")

    # 自动刷新数据
    if not refresh_data(config):
        return False

    messages = export_chat(chat_name, limit=limit, date_from=date_from, date_to=date_to, config=config)
    if not messages:
        print("❌ 未获取到消息。请检查会话名称是否正确。")
        return False

    print(f"✅ 共 {len(messages)} 条消息")

    # 脱敏
    if config.get("privacy", {}).get("redact_before_ai"):
        messages = redact_messages(messages, config)

    # 生成 Markdown
    date_label = _date_label(args)
    output = _format_export_markdown(chat_name, messages,
                                     [_format_message_line(m) for m in messages],
                                     date_label)

    # 输出
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(output)
        os.chmod(args.out, 0o600)
        print(f"💾 已保存到: {args.out}")
    else:
        print(output)
    return True


def cmd_summarize(config, args):
    """总结消息，返回 True 表示所有会话成功"""
    ok_count = 0
    fail_count = 0
    limit = get_message_limit(config, args)
    date_from, date_to = _parse_date_args(args)
    date_label = _date_label(args)
    quiet = getattr(args, "quiet", False)
    out_dir = getattr(args, "output_dir", None) or OUTPUT_DIR

    def p(*a, **kw):
        if not quiet:
            print(*a, **kw)
    def ep(*a, **kw):
        if not quiet:
            eprint(*a, **kw)

    # 自动刷新数据
    if not refresh_data(config, quiet=quiet):
        return

    targets = []
    if args.all:
        for g in config.get("sessions", {}).get("groups", []):
            if g.get("enabled", True):
                targets.append(("group", g["name"]))
        for c in config.get("sessions", {}).get("contacts", []):
            if c.get("enabled", True):
                targets.append(("contact", c["name"]))
        if not targets:
            p("[提示] config.yaml 中没有配置任何会话（群聊或联系人）。")
            p("请先编辑配置文件，或使用 diting export -c 指定会话。")
            return False
    elif args.chat:
        session_type = _detect_session_type(config, args.chat, args.type)
        targets.append((session_type, args.chat))
    else:
        p("[错误] 请指定 -c 会话名 或 --all")
        return False

    for stype, sname in targets:
        label = "群聊" if stype == "group" else "联系人"
        session_limit = get_message_limit(config, args, session_name=sname)
        p(f"\n📊 [{label}] {sname} ({session_limit}条, {date_label})")

        messages = export_chat(sname, limit=session_limit,
                               date_from=date_from, date_to=date_to, config=config)
        if not messages:
            p("  ❌ 无消息或导出失败")
            fail_count += 1
            continue

        p(f"  📝 共 {len(messages)} 条消息")

        if config.get("privacy", {}).get("redact_before_ai"):
            ep("  🔒 正在脱敏...")
            messages = redact_messages(messages, config)

        ai_enabled = config.get("ai", {}).get("enabled", False)
        if ai_enabled:
            ep("  🤖 正在 AI 总结...")
            if stype == "group":
                result = summarize_group_messages(messages, sname, config, date_label)
            else:
                result = summarize_private_messages(messages, sname, config, date_label)
        else:
            fmt = [_format_message_line(m) for m in messages]
            result = _format_export_markdown(sname, messages, fmt, date_label)
            ep("  💡 提示: 未启用 AI API，仅显示原始消息")
            ep("     设置 DEEPSEEK_API_KEY 环境变量可启用 AI 总结")

        # 输出到指定目录
        os.makedirs(out_dir, exist_ok=True)
        output_file = os.path.join(out_dir, f"{sname}-{date_label or 'latest'}.md")
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(result)
        os.chmod(output_file, 0o600)
        p(f"  💾 已保存: {output_file}")
        ok_count += 1

        if not quiet:
            print()
            print(result)
            print()

    return ok_count > 0


def cmd_search(config, args):
    """搜索消息，返回 True 表示成功"""
    keyword = getattr(args, "keyword", None) or getattr(args, "search", None)
    if not keyword:
        eprint("[错误] 请指定搜索关键词")
        return False

    if not refresh_data(config):
        return False

    stdout, stderr, rc = run_export(["-s", keyword])
    if rc != 0:
        eprint(f"[错误] 搜索失败: {stderr[:200]}")
        return False

    lines = [l.strip() for l in stdout.split("\n") if l.strip()]
    print(f"\n🔍 搜索 '{keyword}' 结果 ({len(lines)} 条):\n")
    for line in lines[:50]:  # 最多显示 50 条
        print(f"  {line}")
    if len(lines) > 50:
        print(f"  ... 以及 {len(lines) - 50} 条更多结果")
    return True


def _detect_session_type(config, name, type_hint=None):
    """通过配置和提示判断会话类型"""
    if type_hint:
        return type_hint
    # 在配置中查找
    for g in config.get("sessions", {}).get("groups", []):
        if g.get("name") == name:
            return "group"
    for c in config.get("sessions", {}).get("contacts", []):
        if c.get("name") == name:
            return "contact"
    return "group"


def _parse_date_args(args):
    """解析日期范围参数"""
    if args.today:
        today = datetime.now().strftime("%Y-%m-%d")
        return today, today
    if args.yesterday:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        return yesterday, yesterday
    if args.date_from or args.date_to:
        return args.date_from, args.date_to
    return None, None


def _date_label(args):
    """生成日期标签"""
    if args.today:
        return datetime.now().strftime("%Y-%m-%d")
    if args.yesterday:
        return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    if args.date_from and args.date_to:
        if args.date_from == args.date_to:
            return args.date_from
        return f"{args.date_from}~{args.date_to}"
    return ""


# ═══════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════

def main():
    ensure_dirs()

    parser = argparse.ArgumentParser(
        description=f"谛听（DiTing）— 微信聊天 AI 总结助手 v{VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            示例：
              diting init                    首次初始化
              diting doctor                  系统自检
              diting list                    列出会话
              diting export -c "项目群" --today   导出今日消息
              diting summarize -c "项目群" --today  总结今日群聊
              diting summarize --all --today       总结所有配置的会话
              diting search "报价"            搜索消息
        """)
    )

    # -- 通用参数 --
    parser.add_argument("--verbose", action="store_true", help="显示详细错误信息")
    parser.add_argument("--version", action="store_true", help="显示版本号")
    parser.add_argument("--init", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--check", action="store_true", help=argparse.SUPPRESS)

    # -- 子命令 --
    subparsers = parser.add_subparsers(dest="command", title="命令")

    # init
    p_init = subparsers.add_parser("init", help="首次初始化配置")

    # doctor
    p_doctor = subparsers.add_parser("doctor", help="系统自检")

    # keys
    p_keys = subparsers.add_parser("keys", help="密钥管理")
    p_keys_sub = p_keys.add_subparsers(dest="keys_command", title="密钥子命令")
    p_keys_refresh = p_keys_sub.add_parser("refresh", help="重新提取密钥")

    # list
    p_list = subparsers.add_parser("list", help="列出会话")
    p_list.add_argument("--groups", dest="list_groups", action="store_true", help="只列群聊")
    p_list.add_argument("--contacts", dest="list_contacts", action="store_true", help="只列联系人")

    # export
    p_export = subparsers.add_parser("export", help="导出消息")
    p_export.add_argument("-c", "--chat", required=True, help="会话名称")
    p_export.add_argument("-n", "--number", type=int, default=None, help="消息条数")
    p_export.add_argument("--out", type=str, help="输出文件路径")
    p_export.add_argument("--today", action="store_true", help="今天")
    p_export.add_argument("--yesterday", action="store_true", help="昨天")
    p_export.add_argument("--from", dest="date_from", type=str, help="起始日期 YYYY-MM-DD")
    p_export.add_argument("--to", dest="date_to", type=str, help="结束日期 YYYY-MM-DD")

    # summarize
    p_sum = subparsers.add_parser("summarize", help="AI 总结消息")
    p_sum.add_argument("-c", "--chat", type=str, default=None, help="会话名称")
    p_sum.add_argument("--all", action="store_true", help="总结所有配置的会话")
    p_sum.add_argument("-n", "--number", type=int, default=None, help="消息条数")
    p_sum.add_argument("--today", action="store_true", help="今天")
    p_sum.add_argument("--yesterday", action="store_true", help="昨天")
    p_sum.add_argument("--from", dest="date_from", type=str, help="起始日期 YYYY-MM-DD")
    p_sum.add_argument("--to", dest="date_to", type=str, help="结束日期 YYYY-MM-DD")
    p_sum.add_argument("--type", choices=["group", "contact"], default=None,
                       help="会话类型: group=群聊, contact=联系人")
    p_sum.add_argument("--output-dir", type=str, default=None, help="输出目录（默认 ~/.diting/outputs）")
    p_sum.add_argument("--quiet", action="store_true", help="静默模式，仅打印关键信息")

    # search
    p_search = subparsers.add_parser("search", help="搜索消息")
    p_search.add_argument("keyword", type=str, help="搜索关键词")

    # 兼容旧命令（带短横线的）
    parser.add_argument("--list", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-c", "--chat", type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument("-n", "--number", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("-s", "--search", type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--all", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--summary", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--out", type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--today", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--yesterday", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args()

    # --version
    if args.version:
        print(f"谛听（DiTing） v{VERSION}")
        return

    # 加载配置
    config = load_config()

    # 处理旧命令兼容
    ok = True
    if args.command is None:
        if args.init:
            run_init()
        elif args.check:
            ok = run_doctor(config)
        elif args.list:
            if not refresh_data(config):
                ok = False
            else:
                ok = cmd_list(config, args)
        elif args.search:
            ok = cmd_search(config, args)
        elif args.all or (args.chat and args.summary):
            summary_args = argparse.Namespace(
                chat=args.chat, all=args.all, number=args.number,
                today=False, yesterday=False, date_from=None, date_to=None, type=None
            )
            ok = cmd_summarize(config, summary_args)
        elif args.chat:
            export_args = argparse.Namespace(
                chat=args.chat, number=args.number, out=args.out,
                today=args.today, yesterday=args.yesterday,
                date_from=None, date_to=None, list_groups=False,
                list_contacts=False, search=None, all=False,
                summary=args.summary, type=None
            )
            if args.summary:
                ok = cmd_summarize(config, export_args)
            else:
                ok = cmd_export(config, export_args)
        else:
            parser.print_help()
        sys.exit(0 if ok else 1)

    # 新版子命令
    ok = True
    if args.command == "init":
        run_init()
    elif args.command == "doctor":
        ok = run_doctor(config)
    elif args.command == "keys":
        if getattr(args, "keys_command", None) == "refresh":
            ok = cmd_keys_refresh()
        else:
            print("用法: diting keys refresh")
            ok = False
    elif args.command == "list":
        if not refresh_data(config):
            ok = False
        else:
            ok = cmd_list(config, args)
    elif args.command == "export":
        ok = cmd_export(config, args)
    elif args.command == "summarize":
        ok = cmd_summarize(config, args)
    elif args.command == "search":
        ok = cmd_search(config, args)
    else:
        parser.print_help()

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        eprint("\n[提示] 用户中断")
        sys.exit(1)
    except Exception as e:
        if "--verbose" in sys.argv:
            import traceback
            traceback.print_exc()
        else:
            eprint(f"[错误] {e}")
            eprint("使用 --verbose 查看详细错误信息")
        sys.exit(1)
