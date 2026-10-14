#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 破甲一键通 · 图形界面（v8.0 起）
================================================================================

 双击 一键破甲.bat 默认进这里。本机网页 UI：Python 内置 http.server 起一个
 只监听 127.0.0.1 的本地服务，浏览器打开即用 —— 零第三方依赖，不联网。

   · 数据来自核心脚本（破甲一键通.py）的只读 check()，动作复用它的
     run_action()，补丁 / 备份 / 还原逻辑与命令行完全同一条路径；
   · 核心输出通过挂钩 say() 收进环形缓冲，前端轮询增量渲染；
   · 命令行全套开关不受影响：python 破甲一键通.py --status ... 照旧。

 本文件不属于封条保护范围 —— 随便改，改坏了删掉重下就行；封条只在核心脚本上。
================================================================================
"""

import os
import re
import sys
import json
import glob
import time
import queue
import shutil
import threading
import traceback
import webbrowser
import subprocess
import importlib.util
import concurrent.futures

try:                                    # v8.1：原生窗口（WebView2）。缺它就退回浏览器模式
    import webview
except Exception:
    webview = None
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- pythonw 下 stdout/stderr 是 None，先兜底，后面任何 print 都不会炸 ----------
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _fatal_box(msg):
    """无控制台 exe 里启动失败时的最后手段：弹个 Windows 消息框。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "破甲一键通 GUI", 0x10)
    except Exception:
        pass


if getattr(sys, "frozen", False):        # PyInstaller 打包后：以 exe 所在目录为根
    HERE = os.path.dirname(os.path.abspath(sys.executable))
else:
    HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 定位并加载核心
def _find_core():
    """先找 exe/脚本旁边的核心（文件名含「一键通」的 .py）；
    单文件 exe 模式下旁边没有，就用 PyInstaller 打包进 exe 的那份。"""
    for p in sorted(glob.glob(os.path.join(HERE, "*.py"))):
        if os.path.basename(p) == os.path.basename(__file__):
            continue
        if "一键通" in os.path.basename(p):
            return p
    base = getattr(sys, "_MEIPASS", "")       # PyInstaller onefile 的自解压目录
    if base:
        for p in sorted(glob.glob(os.path.join(base, "*.py"))):
            if "一键通" in os.path.basename(p):
                return p
    return ""


CORE_PATH = _find_core()
# v8.8：核心脚本已随 exe 一起打包（见 .spec 的 datas），启动时直接本地加载，
# 不再向官方服务器握手、不再要求输入 QQ 群号。CLOUD_MODE 恒为 False，仅为兼容
# 旧接口保留（旧前端读到 mode:"local" 会直接进主界面）。
CLOUD_MODE = False
if not CORE_PATH:
    _fatal_box("找不到核心脚本（文件名含「一键通」的 .py）。\n\n"
               "请重新下载完整版，确认 exe 与核心文件没有被分离。")
    raise SystemExit(1)

# v8.5：核心改为后台线程加载 —— 窗口先弹出来，封印自检 / 类初始化在后台跑完
# 再放行 API。启动体感快一截。核心没就绪时，所有接口返回「加载中」占位。
DISPLAY_VER = "8.16"             # GUI 显示版本（与上游保持一致；核心 VERSION 以封印文件为准，现为 8.2）
core = None
PERSONA_FILE = ""
VERSION = DISPLAY_VER
_CORE_READY = threading.Event()
_CORE_T0 = time.time()


def _core_module_from(c, src=None, path=None):
    """构建核心模块对象。path=本地文件 或 src=云端源码 bytes（不落盘）。"""
    if src is not None:
        spec = importlib.util.spec_from_loader("pojia_core", loader=None)
        m = importlib.util.module_from_spec(spec)
        m.__file__ = "<pojia-cloud>"
        m.__dict__["__CLOUD_SRC__"] = src.decode("utf-8", "replace")
        sys.modules["pojia_core"] = m
        exec(compile(src, "<pojia-cloud>", "exec"), m.__dict__)
        return m
    spec = importlib.util.spec_from_file_location("pojia_core", path)
    m = importlib.util.module_from_spec(spec)
    sys.modules["pojia_core"] = m
    spec.loader.exec_module(m)       # 导入期即完成封条自检（被二改的核心会在这里拒启）
    return m


def _load_core():
    """后台加载核心：读随包核心文件 + 封条自检 + 接线放行。"""
    global core, PERSONA_FILE
    try:
        c = _core_module_from(None, path=CORE_PATH)
    except SystemExit as e:
        _push_log("[!] 核心封印校验未通过，退出。")
        _fatal_box("检测到核心文件损坏 / 被二改，已拒绝启动（退出码 3）。\n\n"
                   "请从官方渠道重新下载完整文件。\n\n（%s）" % e)
        os._exit(3)
    except BaseException as e:
        _push_log("[!] 核心加载失败：%s" % e)
        _fatal_box("核心脚本加载失败：\n%s" % e)
        os._exit(1)
    _core_wire(c, os.path.basename(CORE_PATH))


# v8.3：WebView2 用固定缓存目录。pywebview 私有模式给的是
# tempfile.TemporaryDirectory().name —— 那个临时目录对象一被 GC 就被删掉，
# WebView2 渲染进程启动时目录已消失，偶发崩溃（黑窗 + CrashSender 报错弹窗）。
# 固定到 LOCALAPPDATA 下，跨次启动复用，稳定且不产生临时目录。
_UI_CACHE = os.path.join(os.environ.get("LOCALAPPDATA") or HERE,
                         "破甲一键通", "ui-cache")
try:
    os.makedirs(_UI_CACHE, exist_ok=True)
except Exception:
    _UI_CACHE = None


def _core_wire(c, label):
    """核心就位后的统一接线：可写目录重定向 + hooks + 放行 API。"""
    global core, PERSONA_FILE
    # ---- 单文件 exe / 云端模式：核心载体随进程消失，人格 / 日志 / 状态 /
    # 备份这些可写文件必须挪去持久目录，否则每次退出全丢。
    _from_mine = (os.path.dirname(os.path.abspath(CORE_PATH)) ==
                  getattr(sys, "_MEIPASS", "")) if CORE_PATH else False
    if _from_mine or (not CORE_PATH):
        _WORK = os.path.join(os.environ.get("LOCALAPPDATA") or HERE, "破甲一键通")
        try:
            os.makedirs(os.path.join(_WORK, "状态"), exist_ok=True)
            os.makedirs(os.path.join(_WORK, "历史备份"), exist_ok=True)
        except Exception:
            _WORK = HERE
        c.HERE = _WORK
        c.STATE_DIR = os.path.join(_WORK, "状态")
        c.HIST_DIR = os.path.join(_WORK, "历史备份")
        c.LOG_PATH = os.path.join(_WORK, "破甲日志.txt")
        c.PERSONA_FILE = os.path.join(_WORK, "persona.md")
        c.LEGACY_PROMPT = os.path.join(_WORK, "my-prompt.txt")
        c.MANUAL_PATH_FILE = os.path.join(c.STATE_DIR, "手动指定目录.json")
    core = c
    PERSONA_FILE = c.PERSONA_FILE
    core.say = _hook_say               # 核心内部所有 say(...) 调用从这里改道
    # ---- 安装定位去重：detect_ok() 和 check() 各调一遍同一批"找安装目录"
    # 解析器（DSH 的 find_bases 实测 7 秒，双跑 14 秒）。套快照级缓存。
    core.DshTarget.find_bases = _cached_resolver(
        core.DshTarget.find_bases, "find_bases")
    core.WorkBuddyTarget.resolve_install_dir = _cached_resolver(
        core.WorkBuddyTarget.resolve_install_dir, "resolve_install_dir")
    core.WorkBuddyTarget.resolve_data_dir = _cached_resolver(
        core.WorkBuddyTarget.resolve_data_dir, "resolve_data_dir")
    core.ZCodeTarget.resolve_home = _cached_resolver(
        core.ZCodeTarget.resolve_home, "resolve_home")
    core._MarkBlockTarget.resolve_home = _cached_resolver(
        core._MarkBlockTarget.resolve_home, "resolve_home")
    global _orig_find_zcode_cjs
    _orig_find_zcode_cjs = core.find_zcode_cjs
    core.find_zcode_cjs = _find_zcode_cjs_cached
    # ---- v8.0 核心已知 bug 兜底：wb/zcode 的 apply 在收尾调
    # passport_new(..., persona_hash=want)，但核心的函数定义没这个参数
    # → 补丁全部写完后在护照这一步 TypeError（本次有 1 项没做成）。
    # 包一层把 persona_hash 收进护照，"宽松态人格过期"自愈判据才有数据可比。
    _orig_passport_new = core.passport_new

    def _passport_new_compat(key, root, cfg_path, instr_path, mode,
                             agents_path="", created_by_other=None, **kw):
        pp = _orig_passport_new(key, root, cfg_path, instr_path, mode,
                                agents_path, created_by_other)
        ph = (kw or {}).get("persona_hash") or ""
        if ph and isinstance(pp, dict):
            pp["persona_hash"] = ph
        return pp
    core.passport_new = _passport_new_compat
    _CORE_READY.set()
    _push_log("破甲一键通 GUI v%s —— 核心已加载：%s（%.2fs）"
              % (VERSION, label, time.time() - _CORE_T0))


# v8.5：窗口/任务栏图标（exe 内嵌 icon.ico）
_ICON_PATH = os.path.join(getattr(sys, "_MEIPASS", "") or HERE, "icon.ico")


def _apply_window_icon(hwnd):
    """给原生窗口 + 任务栏 + 窗口类都挂上自定义图标。失败静默（不影响功能）。"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hicon = user32.LoadImageW(None, _ICON_PATH, 1,          # IMAGE_ICON
                                  0, 0, 0x10 | 0x40)             # LR_DEFAULTSIZE|LR_LOADFROMFILE
        if not hicon:
            return
        user32.SendMessageW(hwnd, 0x80, 0, hicon)                # WM_SETICON ICON_SMALL
        user32.SendMessageW(hwnd, 0x80, 1, hicon)                # WM_SETICON ICON_BIG
        try:                                                    # 任务栏用类图标
            user32.SetClassLongPtrW(hwnd, -14, hicon)            # GCLP_HICON
            user32.SetClassLongPtrW(hwnd, -34, hicon)            # GCLP_HICONSM
        except Exception:
            pass
    except Exception:
        pass

# ---------------------------------------------------------------- 日志环形缓冲
_LOG_LOCK = threading.Lock()
_LOG = []                # [(seq, line)]
_LOG_SEQ = 0
_LOG_MAX = 2000


def _push_log(line):
    global _LOG_SEQ
    with _LOG_LOCK:
        _LOG_SEQ += 1
        _LOG.append((_LOG_SEQ, line))
        if len(_LOG) > _LOG_MAX:
            del _LOG[:len(_LOG) - _LOG_MAX]


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _hook_say(msg="", color=""):
    """替换核心的 say()：所有控制台输出改道进环形缓冲（同时尽量打到 stdout）。"""
    try:
        line = _ANSI.sub("", str(msg)).rstrip("\n")
        for ln in line.split("\n"):
            _push_log(ln)
        sys.stdout.write(str(msg) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


# ---------------------------------------------------------------- 安装定位去重
# 核心里 detect_ok() 和 check() 会各自调一遍同一批"找安装目录"解析器
# （DSH 的 find_bases 实测 7 秒，双跑就是 14 秒）。这些都是纯读函数，
# 给它们套一层快照级缓存：同一轮快照内只跑一遍，每轮快照开始时清空。
_RES_CACHE = {}
_RES_LOCK = threading.Lock()


def _cached_resolver(orig, name):
    """(self, explicit) 型解析器 → 带缓存的版本。"""
    def wrapper(self, explicit=""):
        ck = (self.key, name, explicit)
        with _RES_LOCK:
            if ck in _RES_CACHE:
                return _RES_CACHE[ck]
        val = orig(self, explicit)
        with _RES_LOCK:
            _RES_CACHE[ck] = val
        return val
    wrapper.__name__ = name
    wrapper.__doc__ = orig.__doc__
    return wrapper


_orig_find_zcode_cjs = None


def _find_zcode_cjs_cached(explicit=""):
    ck = ("zcode", "find_zcode_cjs", explicit)
    with _RES_LOCK:
        if ck in _RES_CACHE:
            return _RES_CACHE[ck]
    val = _orig_find_zcode_cjs(explicit)
    with _RES_LOCK:
        _RES_CACHE[ck] = val
    return val

# ---------------------------------------------------------------- 动作执行器
_BUSY = threading.Lock()          # 同一时刻只允许一个改盘动作
_STATE = {
    "busy": False,
    "busy_label": "",
    "last_action": "",
    "last_done_at": "",
}


class _GuiArgs:
    """给核心用的 args：全部走默认探测，非交互，绝不杀进程。"""
    quiet = True
    yes = True
    dry_run = False
    force = False
    diagnose = False
    persona = ""
    kill_dsh = False
    restart_wb = False
    full = False
    ask_mode = False
    web_filter = False
    zpatch = False
    keep_cache = False


def _mk_args(force=False):
    a = _GuiArgs()
    a.force = bool(force)
    return a


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw) or []
    except Exception as e:
        return [("fail", "检查出错：%s" % e, "")]


def _run_action_async(mode, targets, force):
    """后台线程跑核心 run_action；完成后由前端下次轮询看到新状态。"""
    def work():
        _STATE["busy"] = True
        _STATE["busy_label"] = {"apply": "正在破甲", "revert": "正在还原",
                                "dry-run": "正在演练"}.get(mode, mode)
        try:
            m_run = core.run_action
            args = _mk_args(force)
            if mode == "dry-run":
                args.dry_run = True
            m_run(args, mode, list(targets))
            _push_log("—— %s 完成（退出错误计数 %d）——" % (_STATE["busy_label"], core.ERRORS))
        except Exception as e:
            _push_log("[!] 动作失败：%s" % e)
            _push_log(traceback.format_exc().rstrip())
        finally:
            _STATE["busy"] = False
            _STATE["busy_label"] = ""
            _STATE["last_action"] = "%s %s" % (mode, ",".join(targets))
            _STATE["last_done_at"] = time.strftime("%H:%M:%S")
            _SNAP["want_refresh"] = True      # 动作一结束就重扫一遍
    threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------- 状态快照（后台缓存）
# 全量 check() 实测要几十秒，直接放进 /api/state 会把页面卡死。
# 改成：后台线程维护快照缓存，/api/state 只读缓存+拼接实时字段（毫秒级返回）。
_SNAP = {
    "data": None,           # 最近一次完整快照
    "built_at": 0.0,
    "building": False,
    "want_refresh": True,
}
_SNAP_IDLE_TTL = 60.0       # 空闲多久自动重扫（秒）—— 别太勤，空闲时没必要一直扫盘
_SNAP_WORKERS = 6           # 并行检查线程数


def _target_snapshot(key):
    """单个目标的只读快照：给前端卡片用。"""
    t = core.TARGETS[key]()
    snap = {
        "key": key,
        "label": core.TARGET_LABEL.get(key, key),
        "detected": True,
        "status": "unknown",       # ok | warn | fail | off
        "status_text": "",
        "rows": [],
        "hint": "",
    }
    args = _mk_args()
    try:
        snap["detected"] = bool(t.detect_ok(args))
    except Exception:
        snap["detected"] = True
    rows = _safe(t.check, args)
    out_rows = []
    for st, note, detail in rows:
        out_rows.append({"st": st, "note": str(note), "detail": str(detail or "")})
    snap["rows"] = out_rows
    hard = [r for r in out_rows if r["st"] in ("warn", "fail")]
    good = [r for r in out_rows if r["st"] in ("ok", "own")]
    if not snap["detected"]:
        snap["status"] = "off"
        snap["status_text"] = "未安装"
        name_url = core.INSTALL_HINT.get(key, ("", ""))
        snap["hint"] = name_url[0]
    elif any(r["st"] == "fail" for r in out_rows):
        snap["status"] = "fail"
        snap["status_text"] = "有错误"
    elif hard:
        snap["status"] = "warn"
        snap["status_text"] = "需处理 %d 项" % len(hard)
    elif good:
        snap["status"] = "ok"
        snap["status_text"] = "已生效"
    else:
        snap["status"] = "warn"
        snap["status_text"] = "待确认"
    return snap


def _build_snapshot():
    """并行检查所有目标，拼出完整快照。"""
    with _RES_LOCK:
        _RES_CACHE.clear()      # 每轮快照重新定位一次安装
    keys = list(core.DEFAULT_TARGETS)
    got = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=_SNAP_WORKERS) as ex:
        futs = {ex.submit(_target_snapshot, k): k for k in keys}
        for f in concurrent.futures.as_completed(futs):
            k = futs[f]
            try:
                got[k] = f.result()
            except Exception as e:
                got[k] = {"key": k, "label": core.TARGET_LABEL.get(k, k),
                          "detected": False, "status": "fail", "status_text": "快照失败",
                          "rows": [{"st": "fail", "note": str(e), "detail": ""}], "hint": ""}
    targets = [got[k] for k in keys]
    detected = [t for t in targets if t["detected"]]
    good = [t for t in detected if t["status"] == "ok"]
    warn = sum(len([r for r in t["rows"] if r["st"] in ("warn", "fail")]) for t in detected)
    return {
        "version": VERSION,
        "targets": targets,
        "stats": {
            "total": len(targets),
            "detected": len(detected),
            "ok": len(good),
            "pending": warn,
        },
        "log_path": getattr(core, "LOG_PATH", ""),
        "passphrase": str(core.PASSPHRASE),
        "signal_reply": str(core.SIGNAL_REPLY),
    }


def _snapshot_worker():
    """后台线程：启动即建、动作结束即建、空闲超时重建；忙碌期间不碰盘。"""
    _CORE_READY.wait()                 # v8.5：核心没加载完不扫盘
    while True:
        stale = (time.time() - _SNAP["built_at"]) > _SNAP_IDLE_TTL
        want = _SNAP["want_refresh"] or _SNAP["data"] is None or (stale and not _STATE["busy"])
        if want:
            if _STATE["busy"]:
                time.sleep(1.0)          # 动作跑着的时候等它结束，扫到一半没意义
                continue
            _SNAP["building"] = True
            t0 = time.time()
            try:
                _SNAP["data"] = _build_snapshot()
                _SNAP["built_at"] = time.time()
                _SNAP["want_refresh"] = False
                _push_log("（状态快照 %.1fs 生成完毕）" % (time.time() - t0))
            except Exception:
                _push_log("[!] 状态快照失败：%s" % traceback.format_exc().rstrip())
            finally:
                _SNAP["building"] = False
        time.sleep(2.0)


def state_view(fresh=False):
    """给 /api/state 的即时视图：缓存数据 + 实时 busy 字段。"""
    if fresh:
        _SNAP["want_refresh"] = True
        deadline = time.time() + 60.0
        while time.time() < deadline:
            if (not _SNAP["building"] and _SNAP["data"] is not None
                    and not _SNAP["want_refresh"]):
                break
            time.sleep(0.25)
    if _SNAP["data"]:
        st = dict(_SNAP["data"])
    else:
        st = {
            "version": VERSION,
            "targets": [],
            "stats": {"total": len(core.DEFAULT_TARGETS) if core else 0,
                      "detected": 0, "ok": 0, "pending": 0},
            "log_path": getattr(core, "LOG_PATH", "") if core else "",
            "passphrase": str(core.PASSPHRASE) if core else "",
            "signal_reply": str(core.SIGNAL_REPLY) if core else "",
            "loading": not _CORE_READY.is_set(),
        }
    st.update({
        "busy": _STATE["busy"],
        "busy_label": _STATE["busy_label"],
        "last_action": _STATE["last_action"],
        "last_done_at": _STATE["last_done_at"],
        "building": _SNAP["building"],
        "snap_age": round(time.time() - _SNAP["built_at"], 1) if _SNAP["built_at"] else None,
    })
    return st


def _read_persona():
    try:
        with open(PERSONA_FILE, "rb") as fh:
            raw = fh.read()
        return raw.decode("utf-8")
    except Exception:
        try:
            return core.DEFAULT_PERSONA
        except Exception:
            return ""


def _write_persona(text):
    tmp = PERSONA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, PERSONA_FILE)


# ---------------------------------------------------------------- 环境一键补全
# 常用开发环境检测 + 缺失项下载安装（v8.4）。检测只读不改盘；安装走后台线程，
# 全部进度打进日志环形缓冲（前端「日志」页实时可见）。
_DL_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or HERE, "破甲一键通", "下载")
_ENV_LOCK = threading.Lock()
_ENV = {"items": [], "scanning": False, "busy": False,
        "busy_label": "", "busy_key": ""}
_NO_WIN = 0x08000000             # CREATE_NO_WINDOW：noconsole 下跑子进程不闪黑框
_OK_CODES = (0, 3010)            # 3010 = 成功但需要重启


def _env_run(cmd, timeout=8):
    """跑一条命令，拿 (returncode, 合并输出)。"""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=_NO_WIN)
        out = ((r.stdout or b"") + b"\n" + (r.stderr or b"")).decode("utf-8", "ignore")
        return r.returncode, out
    except Exception:
        return -1, ""


def _env_which(name):
    try:
        return shutil.which(name) or ""
    except Exception:
        return ""


def _env_reg(root, sub, value=None):
    """读注册表（出错给 None）。value=None 读默认值。"""
    import winreg
    try:
        with winreg.OpenKey(root, sub) as k:
            if value is None:
                return winreg.QueryValue(k, None)
            return winreg.QueryValueEx(k, value)[0]
    except Exception:
        return None


def _env_reg_keys(root, sub):
    import winreg
    try:
        with winreg.OpenKey(root, sub) as k:
            out, i = [], 0
            while True:
                try:
                    out.append(winreg.EnumKey(k, i))
                    i += 1
                except OSError:
                    return out
    except Exception:
        return []


# ---- 各环境检测：返回 {key,name,note,status,version,detail[,home]} ----

def _mk_env(key, name, note, status, version="", detail="", **extra):
    d = {"key": key, "name": name, "note": note, "status": status,
         "version": str(version or ""), "detail": str(detail or "")}
    d.update(extra)
    return d


def _detect_python():
    import winreg
    exe, via = _env_which("python") or _env_which("python3"), "PATH"
    if not exe:
        p = _env_which("py")
        if p:
            exe, via = p, "py 启动器"
    if not exe:
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for sub in (r"Software\Python\PythonCore",
                        r"SOFTWARE\WOW6432Node\Python\PythonCore"):
                for ver in _env_reg_keys(root, sub):
                    ip = _env_reg(root, "%s\\%s\\InstallPath" % (sub, ver))
                    if ip:
                        cand = os.path.join(str(ip), "python.exe")
                        if os.path.isfile(cand):
                            exe, via = cand, "注册表定位"
                            break
                if exe:
                    break
            if exe:
                break
    if not exe:
        for pat in (os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                 r"Programs\Python\Python3*\python.exe"),
                    r"C:\Python3*\python.exe"):
            g = sorted(glob.glob(pat))
            if g:
                exe, via = g[-1], "已装但未进 PATH"
                break
    if exe:
        rc, out = _env_run([exe, "--version"])
        m = re.search(r"Python\s+([\w.]+)", out)
        return _mk_env("python", "Python 3", "脚本 / 自动化 / pip 生态", "ok",
                       m.group(1) if m else "", "%s（%s）" % (exe, via))
    return _mk_env("python", "Python 3", "脚本 / 自动化 / pip 生态", "miss")


def _detect_java():
    env_jh = os.environ.get("JAVA_HOME", "")
    cands = []
    if env_jh and os.path.isfile(os.path.join(env_jh, "bin", "java.exe")):
        cands.append(os.path.join(env_jh, "bin", "java.exe"))
    w = _env_which("java")
    if w:
        cands.append(w)
    for pat in (r"C:\Program Files\Java\*\bin\java.exe",
                r"C:\Program Files\Eclipse Adoptium\*\bin\java.exe",
                r"C:\Program Files\Microsoft\jdk-*\bin\java.exe",
                r"C:\Program Files\Zulu\*\bin\java.exe",
                r"C:\Program Files\Amazon Corretto\*\bin\java.exe",
                r"C:\Program Files\BellSoft\*\bin\java.exe",
                r"C:\Program Files (x86)\Java\*\bin\java.exe"):
        cands += glob.glob(pat)
    seen = set()
    for c in cands:
        nc = os.path.normcase(c)
        if nc in seen:
            continue
        seen.add(nc)
        if not os.path.isfile(c):
            continue
        rc, out = _env_run([c, "-version"])
        m = re.search(r'"([^"]+)"', out)
        home = os.path.dirname(os.path.dirname(c))
        if env_jh and os.path.normcase(os.path.normpath(env_jh)) \
                == os.path.normcase(os.path.normpath(home)):
            return _mk_env("java", "Java JDK", "apktool / JetBrains / 服务端", "ok",
                           m.group(1) if m else "", c, home=home)
        return _mk_env("java", "Java JDK", "apktool / JetBrains / 服务端", "warn",
                       m.group(1) if m else "",
                       "%s（JAVA_HOME 未指向它）" % c, home=home)
    return _mk_env("java", "Java JDK", "apktool / JetBrains / 服务端", "miss")


def _detect_node():
    exe = _env_which("node")
    in_path = bool(exe)
    if not exe:
        for pat in (r"C:\Program Files\nodejs\node.exe",
                    os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                 r"Programs\nodejs\node.exe")):
            if os.path.isfile(pat):
                exe = pat
                break
    if exe:
        rc, out = _env_run([exe, "--version"])
        m = re.search(r"v([\w.\-]+)", out)
        return _mk_env("node", "Node.js", "npm / 前端 / 各类 CLI 工具",
                       "ok" if in_path else "warn",
                       m.group(1) if m else "",
                       exe if in_path else "%s（不在 PATH）" % exe)
    return _mk_env("node", "Node.js", "npm / 前端 / 各类 CLI 工具", "miss")


def _detect_git():
    exe = _env_which("git")
    in_path = bool(exe)
    if not exe:
        for pat in (r"C:\Program Files\Git\cmd\git.exe",
                    os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                 r"Programs\Git\cmd\git.exe")):
            if os.path.isfile(pat):
                exe = pat
                break
    if exe:
        rc, out = _env_run([exe, "--version"])
        m = re.search(r"([\d.]+)", out)
        return _mk_env("git", "Git", "版本管理 / 代码获取",
                       "ok" if in_path else "warn",
                       m.group(1) if m else "",
                       exe if in_path else "%s（不在 PATH）" % exe)
    return _mk_env("git", "Git", "版本管理 / 代码获取", "miss")


def _detect_webview2():
    import winreg
    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for root, sub in ((winreg.HKEY_LOCAL_MACHINE,
                       r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\\" + guid),
                      (winreg.HKEY_LOCAL_MACHINE,
                       r"SOFTWARE\Microsoft\EdgeUpdate\Clients\\" + guid),
                      (winreg.HKEY_CURRENT_USER,
                       r"Software\Microsoft\EdgeUpdate\Clients\\" + guid)):
        pv = _env_reg(root, sub, "pv")
        if pv and str(pv) not in ("", "0.0.0.0"):
            return _mk_env("webview2", "WebView2 运行时",
                           "本窗口内核 / 很多桌面应用依赖", "ok", pv, "系统运行时")
    return _mk_env("webview2", "WebView2 运行时",
                   "本窗口内核 / 很多桌面应用依赖", "miss")


# ---- v8.6 显示修复：黑窗免疫 ------------------------------------------------
# 客户端黑窗/空窗的根因：客户机（尤其精简版系统）缺 WebView2 运行时。
# pywebview 6.x 在 runtime 缺失时 WinForms 窗口照常创建、WebView2 控件初始化
# 失败但【不抛异常】——现有"起不来退浏览器"兜底永远不触发，用户看到的就是
# 黑窗或只剩静态骨架的残废页。修复 = 启动前主动检测，缺失就弹窗自动装，
# 装不上强制走浏览器模式，彻底不让黑窗出现。

def _webview2_state():
    """轻量版 WebView2 检测：返回 (installed, version)。启动链用。"""
    import winreg
    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for root, sub in ((winreg.HKEY_LOCAL_MACHINE,
                       r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\\" + guid),
                      (winreg.HKEY_LOCAL_MACHINE,
                       r"SOFTWARE\Microsoft\EdgeUpdate\Clients\\" + guid),
                      (winreg.HKEY_CURRENT_USER,
                       r"Software\Microsoft\EdgeUpdate\Clients\\" + guid)):
        pv = _env_reg(root, sub, "pv")
        if pv and str(pv) not in ("", "0.0.0.0"):
            return True, str(pv)
    return False, ""


def _ensure_webview2_or_browser(url):
    """原生窗口启动前的守门员。返回 "webview"（可以建原生窗口）或
    "browser"（改走浏览器模式）。缺运行时时弹窗二选一：自动装 / 用浏览器。"""
    import ctypes
    ok, pv = _webview2_state()
    if ok:
        return "webview"
    _push_log("[!] 未检测到 WebView2 运行时（原生窗口会黑屏），弹窗引导。")
    r = ctypes.windll.user32.MessageBoxW(
        None,
        "你的电脑缺少界面组件（WebView2 运行时），\n"
        "直接打开会显示黑屏或空白。\n\n"
        "【是】自动下载安装（微软官方组件，约 1~2 分钟，需联网）\n"
        "【否】改用浏览器打开界面（功能完全一样）",
        "破甲一键通 · 首次运行准备", 0x34)          # MB_ICONWARNING|YESNO|MB_SETFOREGROUND
    if r != 6:                                       # 没选"是" → 浏览器模式
        return "browser"
    dest = os.path.join(os.environ.get("TEMP") or HERE,
                        "MicrosoftEdgeWebview2Setup.exe")
    got = _env_download("https://go.microsoft.com/fwlink/p/?LinkId=2124703",
                        dest, "WebView2 运行时")
    if not got:
        ctypes.windll.user32.MessageBoxW(
            None,
            "WebView2 运行时下载失败（可能没联网）。\n\n点击确定改用浏览器打开界面。",
            "破甲一键通", 0x30)                      # MB_ICONEXCLAMATION
        return "browser"
    okr, msg = _run_elevated(got, "/silent /install", timeout_s=300)
    if not okr:
        _push_log("[!] WebView2 安装未完成：%s" % msg)
    # 安装器静默模式可能已返回但注册表落库稍有延迟 → 轮询最多 120 秒
    deadline = time.time() + 120
    while time.time() < deadline:
        ok, pv2 = _webview2_state()
        if ok:
            _push_log("[环境] ✓ WebView2 运行时 %s 就绪" % pv2)
            return "webview"
        time.sleep(2)
    ctypes.windll.user32.MessageBoxW(
        None,
        "WebView2 运行时安装未完成（%s）。\n\n点击确定改用浏览器打开界面；\n"
        "装好后重新双击破甲即可用原生窗口。" % (msg or "超时"),
        "破甲一键通", 0x30)
    return "browser"


def _detect_vcrun():
    import winreg
    sub = r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub) as k:
            installed = winreg.QueryValueEx(k, "Installed")[0]
            major = winreg.QueryValueEx(k, "Major")[0]
            minor = winreg.QueryValueEx(k, "Minor")[0]
            bld = winreg.QueryValueEx(k, "Bld")[0]
        if installed:
            return _mk_env("vcrun", "VC++ 运行库 x64",
                           "大量软件 / 游戏的底层依赖", "ok",
                           "%d.%d.%d" % (major, minor, bld),
                           "2015-2022 Redistributable")
    except Exception:
        pass
    return _mk_env("vcrun", "VC++ 运行库 x64",
                   "大量软件 / 游戏的底层依赖", "miss")


def _detect_adb():
    exe = _env_which("adb")
    in_path = bool(exe)
    found = exe
    if not found:
        la = os.environ.get("LOCALAPPDATA", "")
        for p in (os.path.join(la, "Android", "platform-tools", "adb.exe"),
                  os.path.join(la, "Android", "Sdk", "platform-tools", "adb.exe")):
            if os.path.isfile(p):
                found = p
                break
    if found:
        rc, out = _env_run([found, "--version"])
        m = re.search(r"Version\s+([\w.\-]+)", out)
        return _mk_env("adb", "ADB", "安卓调试 / root / fastboot",
                       "ok" if in_path else "warn",
                       m.group(1) if m else "",
                       found if in_path else "%s（不在 PATH）" % found)
    return _mk_env("adb", "ADB", "安卓调试 / root / fastboot", "miss")


_ENV_DETECTORS = [_detect_python, _detect_java, _detect_node, _detect_git,
                  _detect_webview2, _detect_vcrun, _detect_adb]


def _env_detect_all():
    _refresh_process_env()
    items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(f) for f in _ENV_DETECTORS]
        for f in futs:
            try:
                items.append(f.result())
            except Exception as e:
                _push_log("[环境] 某项检测出错：%s" % e)
    return items


# ---- 用户环境变量 / PATH 修复 ----

def _broadcast_env():
    """告诉全系统「环境变量变了」，新开的终端才能拿到。"""
    import ctypes
    import ctypes.wintypes as wt
    try:
        u32 = ctypes.windll.user32
        u32.SendMessageTimeoutW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM,
                                            wt.LPCWSTR, wt.UINT, wt.UINT,
                                            ctypes.POINTER(ctypes.c_size_t)]
        res = ctypes.c_size_t()
        u32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 3000,
                                ctypes.byref(res))
    except Exception:
        pass


def _env_set_user(name, value):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, str(value))
        _broadcast_env()
        return True
    except Exception as e:
        _push_log("[环境] 写用户变量 %s 失败：%s" % (name, e))
        return False


def _user_path_add(d):
    """把目录追加进用户 PATH（已在则不动）。返回是否真的加了。"""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            cur, typ = winreg.QueryValueEx(k, "Path")
        cur, typ = str(cur), int(typ)
    except FileNotFoundError:
        cur, typ = "", winreg.REG_EXPAND_SZ
    except Exception as e:
        _push_log("[环境] 读用户 PATH 失败：%s" % e)
        return False
    nc = os.path.normcase(os.path.normpath(d))
    for ent in cur.split(";"):
        ent = ent.strip()
        if ent and os.path.normcase(os.path.normpath(os.path.expandvars(ent))) == nc:
            return False
    newv = (cur.rstrip(";") + ";" + d) if cur.strip() else d
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "Path", 0, typ, newv)
        _broadcast_env()
        return True
    except Exception as e:
        _push_log("[环境] 写用户 PATH 失败：%s" % e)
        return False


def _refresh_process_env():
    """把注册表里的 PATH / JAVA_HOME 刷进本进程，否则 which() 看不到新装的。"""
    import winreg
    try:
        machine = _env_reg(winreg.HKEY_LOCAL_MACHINE,
                           r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
                           "Path") or ""
        user = _env_reg(winreg.HKEY_CURRENT_USER, "Environment", "Path") or ""
        parts = []
        for ent in str(machine).split(";") + str(user).split(";"):
            ent = ent.strip()
            if ent:
                parts.append(os.path.expandvars(ent))
        if parts:
            os.environ["Path"] = ";".join(parts)
        jh = (_env_reg(winreg.HKEY_CURRENT_USER, "Environment", "JAVA_HOME")
              or _env_reg(winreg.HKEY_LOCAL_MACHINE,
                          r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
                          "JAVA_HOME"))
        if jh:
            os.environ["JAVA_HOME"] = str(jh)
    except Exception:
        pass


# ---- 下载 / 安装 ----

def _fmt_sz(n):
    return "%.1fMB" % (n / 1048576.0) if n >= 1048576 else "%dKB" % max(n // 1024, 1)


def _env_download(url, dest, label):
    import urllib.request
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    _push_log("[环境] %s 下载：%s" % (label, url))
    t_last = 0.0
    try:
        with urllib.request.urlopen(url, timeout=30) as r, open(dest + ".part", "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                now = time.time()
                if now - t_last >= 3.0:
                    t_last = now
                    if total:
                        _push_log("[环境] %s 下载中 %d%%（%s / %s）" %
                                  (label, got * 100 // total, _fmt_sz(got), _fmt_sz(total)))
                    else:
                        _push_log("[环境] %s 下载中 %s" % (label, _fmt_sz(got)))
        os.replace(dest + ".part", dest)
        _push_log("[环境] ✓ %s 下载完成（%s）" %
                  (label, _fmt_sz(os.path.getsize(dest))))
        return dest
    except Exception as e:
        _push_log("[环境] %s 下载失败：%s" % (label, e))
        try:
            os.remove(dest + ".part")
        except OSError:
            pass
        return ""


def _run_elevated(path, params="", timeout_s=1800):
    """UAC 提权跑安装程序，等到退出。返回 (ok, msg)。"""
    import ctypes
    import ctypes.wintypes as wt

    class _SEEI(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("fMask", wt.ULONG), ("hwnd", wt.HWND),
                    ("lpVerb", ctypes.c_wchar_p), ("lpFile", ctypes.c_wchar_p),
                    ("lpParameters", ctypes.c_wchar_p),
                    ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int),
                    ("hInstApp", wt.HINSTANCE), ("lpIDList", wt.LPVOID),
                    ("lpClass", ctypes.c_wchar_p), ("hkeyClass", wt.HKEY),
                    ("dwHotKey", wt.DWORD), ("hIcon", wt.HANDLE),
                    ("hProcess", wt.HANDLE)]

    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SEEI)]
        shell32.ShellExecuteExW.restype = ctypes.c_bool
        sei = _SEEI()
        sei.cbSize = ctypes.sizeof(_SEEI)
        sei.fMask = 0x40                      # SEE_MASK_NOCLOSEPROCESS
        sei.lpVerb = "runas"
        sei.lpFile = os.path.abspath(path)
        sei.lpParameters = params
        sei.nShow = 1
        if not shell32.ShellExecuteExW(ctypes.byref(sei)):
            err = ctypes.get_last_error()
            if err == 1223:
                return False, "取消了管理员授权"
            return False, "启动失败（错误码 %d）" % err
        k32 = ctypes.windll.kernel32
        rc = k32.WaitForSingleObject(sei.hProcess, timeout_s * 1000)
        if rc == 258:
            return False, "安装超时（%d 分钟）" % (timeout_s // 60)
        code = wt.DWORD()
        k32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
        k32.CloseHandle(sei.hProcess)
        if code.value in _OK_CODES:
            return True, "退出码 %d%s" % (code.value,
                                          "（重启后完全生效）" if code.value == 3010 else "")
        return False, "退出码 %d" % code.value
    except Exception as e:
        return False, str(e)


def _run_plain(path, args, timeout_s=1800):
    """普通权限跑安装程序，等到退出。"""
    try:
        r = subprocess.run([os.path.abspath(path)] + list(args), timeout=timeout_s,
                           creationflags=_NO_WIN)
    except subprocess.TimeoutExpired:
        return False, "安装超时"
    except Exception as e:
        return False, str(e)
    if r.returncode in _OK_CODES:
        return True, "退出码 %d" % r.returncode
    return False, "退出码 %d" % r.returncode


def _fix_python(dest):
    return _run_plain(dest, ["/quiet", "InstallAllUsers=0", "PrependPath=1",
                             "Include_launcher=1"])


def _fix_java(dest):
    return _run_elevated(dest,
                         "ADDLOCAL=FeatureMain,FeatureEnvironment,"
                         "FeatureJarFileRunWith,FeatureJavaHome /qn /norestart")


def _fix_node(dest):
    return _run_elevated(dest, "/qn /norestart")


def _fix_git(dest):
    return _run_elevated(dest, "/VERYSILENT /NORESTART /SUPPRESSMSGBOXES /NOCANCEL")


def _fix_webview2(dest):
    return _run_elevated(dest, "/silent /install")


def _fix_vcrun(dest):
    return _run_elevated(dest, "/install /quiet /norestart")


def _fix_adb(dest):
    import zipfile
    la = os.environ.get("LOCALAPPDATA", "")
    base = os.path.join(la, "Android") if la else os.path.join(HERE, "Android")
    tgt = os.path.join(base, "platform-tools")
    _push_log("[环境] 解压 platform-tools → %s" % tgt)
    try:
        with zipfile.ZipFile(dest) as z:
            z.extractall(base)
    except Exception as e:
        return False, "解压失败：%s" % e
    if not os.path.isfile(os.path.join(tgt, "adb.exe")):
        return False, "解压后找不到 adb.exe"
    if _user_path_add(tgt):
        _push_log("[环境] ✓ 已把 %s 加入用户 PATH（新开终端生效）" % tgt)
    return True, ""


_ENV_INSTALL = {
    "python": {
        "name": "Python 3.12", "file": "python-3.12.10-amd64.exe",
        "minsize": 25_000_000, "fix": _fix_python,
        "urls": ["https://mirrors.huaweicloud.com/python/3.12.10/python-3.12.10-amd64.exe",
                 "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"]},
    "java": {
        "name": "Temurin JDK 21",
        "file": "OpenJDK21U-jdk_x64_windows_hotspot_21.0.5_11.msi",
        "minsize": 90_000_000, "fix": _fix_java,
        "urls": ["https://mirrors.tuna.tsinghua.edu.cn/Adoptium/21/jdk/x64/windows/"
                 "OpenJDK21U-jdk_x64_windows_hotspot_21.0.5_11.msi",
                 "https://api.adoptium.net/v3/binary/latest/21/ga/windows/x64/jdk/"
                 "hotspot/normal/eclipse"]},
    "node": {
        "name": "Node.js 22 LTS", "file": "node-v22.14.0-x64.msi",
        "minsize": 28_000_000, "fix": _fix_node,
        "urls": ["https://mirrors.huaweicloud.com/nodejs/v22.14.0/node-v22.14.0-x64.msi",
                 "https://nodejs.org/dist/v22.14.0/node-v22.14.0-x64.msi"]},
    "git": {
        "name": "Git 2.47", "file": "Git-2.47.1-64-bit.exe",
        "minsize": 55_000_000, "fix": _fix_git,
        "urls": ["https://mirrors.huaweicloud.com/git-for-windows/v2.47.1.windows.1/"
                 "Git-2.47.1-64-bit.exe",
                 "https://github.com/git-for-windows/git/releases/download/"
                 "v2.47.1.windows.1/Git-2.47.1-64-bit.exe"]},
    "webview2": {
        "name": "WebView2 运行时", "file": "MicrosoftEdgeWebview2Setup.exe",
        "minsize": 1_500_000, "fix": _fix_webview2,
        "urls": ["https://go.microsoft.com/fwlink/p/?LinkId=2124703"]},
    "vcrun": {
        "name": "VC++ 运行库", "file": "vc_redist.x64.exe",
        "minsize": 14_000_000, "fix": _fix_vcrun,
        "urls": ["https://aka.ms/vs/17/release/vc_redist.x64.exe"]},
    "adb": {
        "name": "ADB platform-tools", "file": "platform-tools-latest-windows.zip",
        "minsize": 4_000_000, "fix": _fix_adb,
        "urls": ["https://dl.google.com/android/repository/"
                 "platform-tools-latest-windows.zip"]},
}

_ENV_ST_TXT = {"ok": "已就绪", "warn": "需修复", "miss": "未安装"}


def _env_fix_one(key):
    spec = _ENV_INSTALL.get(key)
    if not spec:
        return
    name = spec["name"]
    with _ENV_LOCK:
        cur = {i["key"]: i for i in _ENV["items"]}.get(key, {})
    st = cur.get("status", "miss")
    _push_log("[环境] —— %s（当前：%s）——" % (name, _ENV_ST_TXT.get(st, st)))
    if st == "ok":
        _push_log("[环境] %s 已就绪，跳过" % name)
        return
    # warn：多数是 PATH / JAVA_HOME 没配，先试只修配置，不动安装包
    if st == "warn":
        if key == "java" and cur.get("home"):
            if _env_set_user("JAVA_HOME", cur["home"]):
                _push_log("[环境] ✓ JAVA_HOME 已指向 %s（新开终端生效）" % cur["home"])
            return
        d = ""
        if key == "node":
            d = r"C:\Program Files\nodejs"
        elif key == "git":
            d = r"C:\Program Files\Git\cmd"
        elif key == "adb" and cur.get("detail"):
            d = os.path.dirname(cur["detail"].split("（")[0])
        if d and os.path.isdir(d):
            if _user_path_add(d):
                _push_log("[环境] ✓ 已把 %s 加入用户 PATH（新开终端生效）" % d)
            else:
                _push_log("[环境] %s 的 PATH 原本就有，跳过" % name)
            return
        _push_log("[环境] %s 配置修复未奏效，按未安装处理" % name)
    # miss：下载 + 静默安装
    dest = os.path.join(_DL_DIR, spec["file"])
    if os.path.isfile(dest) and os.path.getsize(dest) >= spec["minsize"]:
        _push_log("[环境] %s 安装包已在本地，复用（%s）" % (name, _fmt_sz(os.path.getsize(dest))))
    else:
        got = ""
        for url in spec["urls"]:
            got = _env_download(url, dest, name)
            if got and os.path.getsize(got) >= spec["minsize"]:
                break
            if got:
                try:
                    os.remove(got)          # 尺寸不对多半是错误页，删掉换源
                except OSError:
                    pass
                got = ""
        if not got:
            _push_log("[环境] ✗ %s 所有下载源都失败。手动安装：%s"
                      % (name, spec["urls"][-1]))
            return
    _push_log("[环境] 开始安装 %s（可能弹管理员授权框）…" % name)
    ok, msg = spec["fix"](dest)
    if ok:
        _push_log("[环境] ✓ %s 安装成功%s" % (name, "（%s）" % msg if msg else ""))
    else:
        _push_log("[环境] ✗ %s 安装失败：%s" % (name, msg))


def _env_scan_async():
    if _ENV["scanning"]:
        return

    def work():
        _ENV["scanning"] = True
        try:
            t0 = time.time()
            items = _env_detect_all()
            with _ENV_LOCK:
                _ENV["items"] = items
            ok_n = sum(1 for i in items if i["status"] == "ok")
            _push_log("[环境] 检测完成：%d/%d 就绪（%.1fs）"
                      % (ok_n, len(items), time.time() - t0))
        finally:
            _ENV["scanning"] = False
    threading.Thread(target=work, daemon=True, name="env-scan").start()


def _env_install_async(keys):
    def work():
        _ENV["busy"] = True
        _ENV["busy_label"] = "补全环境中"
        _push_log("[环境] ===== 开始环境补全：%s =====" % "、".join(
            _ENV_INSTALL.get(k, {}).get("name", k) for k in keys))
        try:
            for k in keys:
                with _ENV_LOCK:
                    _ENV["busy_key"] = k
                try:
                    _env_fix_one(k)
                except Exception as e:
                    _push_log("[环境] ✗ %s 处理异常：%s"
                              % (_ENV_INSTALL.get(k, {}).get("name", k), e))
                finally:
                    with _ENV_LOCK:
                        _ENV["busy_key"] = ""
                _refresh_process_env()
        finally:
            _ENV["busy"] = False
            _ENV["busy_label"] = ""
            _push_log("[环境] ===== 环境补全流程结束，重新检测 =====")
            _env_scan_async()
    threading.Thread(target=work, daemon=True, name="env-install").start()


# ---------------------------------------------------------------- 技能库
# 精选推荐（写死的运营清单）+ 本机已装扫描（~/.workbuddy/skills + 插件缓存）。
# 这些技能装在 AI 客户端里，对话中点名即可触发；「未装」的按技能名去市场搜。
_SKILL_RECOMMEND = [
    ("find-skills", "技能发现",
     "搜技能市场，说需求直接给可装的技能并安装",
     "找个能做 XX 的技能"),
    ("android-apk-unlock", "搞机逆向",
     "APK 会员/授权/内购绕过：反编译、定位校验点、打补丁、重签",
     "破解这个 APK，去掉会员验证"),
    ("app-deeplink-capture", "搞机逆向",
     "无 root 抓 App 跳转的深链，判断能否脱离本机转发给别人",
     "抓这个 App 的跳转链接"),
    ("web-clone-to-apk", "搞机逆向",
     "把网站完整镜像并打包成可离线运行的 APK",
     "把这个网站做成 APP"),
    ("dsh-preset-bundle-inject", "搞机逆向",
     "给 DSH 注入自定义 preset：换人格、调工具集、修启动报错",
     "给 DSH 加个模式"),
    ("agent-browser", "效率工具",
     "浏览器自动化：开页面、点击、填表、截图、提取内容",
     "打开这个网页帮我截图"),
    ("motrix-download-manager", "效率工具",
     "管 Motrix/aria2 下载：加任务、进度、暂停续传、限速",
     "下载这个磁力"),
    ("open-kimi-ppt", "内容创作",
     "生成完整 PPT 项目并导出 pptx",
     "做个 PPT"),
    ("ranking-video-pipeline", "内容创作",
     "HTML 逐帧渲染加 ffmpeg 合成，产出中文配音的榜单视频",
     "以 XX 排名做个视频"),
    ("retro-90s-portal-html", "内容创作",
     "生成 90 年代风格的单文件 HTML 网页",
     "做成老网站的样子"),
    ("wechat-chat-style-distill", "内容创作",
     "微信聊天导出文件蒸馏成风格人格卡和训练数据集",
     "学一下这个人的聊天方式"),
    ("py-self-seal", "脚本保护",
     "给 Python 脚本加防二改封条，删掉保护块就拒跑",
     "给这个脚本加防改保护"),
    ("windows-mcp-setup", "系统控制",
     "AI 直接操作 Windows 桌面：鼠标键盘、截屏、注册表",
     "帮我点桌面上的回收站"),
    ("windows-display-flicker", "系统诊断",
     "屏幕闪烁/黑屏/花屏分层排查，定位 HDR、虚拟驱动、线材",
     "屏幕在闪"),
    ("mijia-3mini-display", "硬件改造",
     "米家温湿度计 3 mini 刷 pvvx 固件，改成显示电脑温度",
     "温湿度计显示电脑温度"),
    ("lan-media-server", "网络服务",
     "本机视频开 HTTP 给局域网在线播放，支持拖进度",
     "让手机能看电脑里的电影"),
    ("marzban-vps-panel", "网络服务",
     "VPS 部署 Marzban 面板，可从 x-ui/3x-ui 迁移且节点零改动",
     "装个 marzban"),
    ("github-release-archive", "仓库维护",
     "删 GitHub 旧 Release 前先完整归档到本地再删线上",
     "清理旧版本只留最新"),
    ("local-news-cms", "网络服务",
     "纯 Python 新闻站，定时 RSS 采集自动更新",
     "做个自动更新的资讯站"),
    ("life-decision-guide", "生活决策",
     "按成本、收益量级、证据等级回答人生决策问题",
     "这个决定值不值"),
]

_SKILL_CACHE = {"at": 0.0, "data": None}


def _scan_installed_skills():
    """扫本机 AI 客户端已装技能：用户级 skills 目录 + 插件市场缓存里的 SKILL.md。

    返回 [{"name": 目录名, "desc": frontmatter description}]，按名排序，60s 缓存。
    """
    now = time.time()
    if _SKILL_CACHE["data"] is not None and now - _SKILL_CACHE["at"] < 60:
        return _SKILL_CACHE["data"]
    home = os.path.expanduser("~")
    roots = [os.path.join(home, ".workbuddy", "skills"),
             os.path.join(home, ".workbuddy", "plugins", "cache")]
    out, seen = [], set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for base, _dirs, files in os.walk(root):
            if "SKILL.md" not in files:
                continue
            desc = ""
            fm_name = ""
            try:
                with open(os.path.join(base, "SKILL.md"), "r",
                          encoding="utf-8", errors="ignore") as fh:
                    head = fh.read(4096)
                m = re.search(r"(?m)^description:\s*(.+)$", head)
                if m:
                    desc = m.group(1).strip().strip("\"'")
                if len(desc) > 110:
                    desc = desc[:107] + "…"
                m2 = re.search(r"(?m)^name:\s*(.+)$", head)
                if m2:
                    fm_name = m2.group(1).strip().strip("\"'")
            except Exception:
                pass
            # 名字优先用 frontmatter 的 name:（插件包版本目录 0.1.x 直下的 SKILL.md
            # 目录名是版本号，不是技能名）；目录名兜底。
            name = fm_name or os.path.basename(base)
            if name in seen:
                continue
            seen.add(name)
            out.append({"name": name, "desc": desc or "（无描述）"})
    out.sort(key=lambda x: x["name"].lower())
    _SKILL_CACHE["at"] = now
    _SKILL_CACHE["data"] = out
    return out


_MKT_CACHE = {"at": 0.0, "data": None}


def _mkt_root():
    return os.path.join(os.path.expanduser("~"), ".workbuddy",
                        "plugins", "marketplaces")


def _read_skill_md_head(base):
    """读一个技能目录 SKILL.md 头部的 name/description，返回 (name, desc)。"""
    try:
        with open(os.path.join(base, "SKILL.md"), "r",
                  encoding="utf-8", errors="ignore") as fh:
            head = fh.read(4096)
    except Exception:
        return "", ""
    desc = ""
    m = re.search(r"(?m)^description:\s*(.+)$", head)
    if m:
        desc = m.group(1).strip().strip("\"'")
    if len(desc) > 110:
        desc = desc[:107] + "…"
    name = ""
    m2 = re.search(r"(?m)^name:\s*(.+)$", head)
    if m2:
        name = m2.group(1).strip().strip("\"'")
    return name, desc


def _scan_marketplace_skills(installed_names):
    """扫本地市场目录里还没装的技能（市场 zip 由客户端自动在线更新）。

    返回 [{"name","desc","src"}]，src = 相对市场根的路径（安装时校验用）。
    """
    now = time.time()
    if _MKT_CACHE["data"] is not None and now - _MKT_CACHE["at"] < 60:
        raw = _MKT_CACHE["data"]
    else:
        root = _mkt_root()
        out, seen = [], set()
        if os.path.isdir(root):
            for mk in sorted(os.listdir(root)):
                mp = os.path.join(root, mk, ".codebuddy-plugin",
                                  "marketplace.json")
                if not os.path.isfile(mp):
                    continue
                try:
                    with open(mp, "r", encoding="utf-8",
                              errors="ignore") as fh:
                        manifest = json.load(fh)
                except Exception:
                    continue
                for plug in manifest.get("plugins", []):
                    for sp in plug.get("skills", []):
                        sd = os.path.normpath(os.path.join(root, mk, sp))
                        if (not os.path.isdir(sd)
                                or not os.path.isfile(os.path.join(sd, "SKILL.md"))
                                or sd.lower() in seen):
                            continue
                        seen.add(sd.lower())
                        fm_name, desc = _read_skill_md_head(sd)
                        out.append({"name": fm_name or os.path.basename(sd),
                                    "desc": desc or "（无描述）",
                                    "src": os.path.relpath(sd, root)})
        out.sort(key=lambda x: x["name"].lower())
        _MKT_CACHE["at"] = now
        _MKT_CACHE["data"] = out
        raw = out
    inst = set(installed_names)
    return [s for s in raw if s["name"] not in inst]


def _install_marketplace_skill(src):
    """把市场里的一个技能目录复制到用户级 skills 目录。

    返回 (ok, name_or_err)。路径做了根内校验，防目录穿越。
    """
    root = os.path.realpath(_mkt_root())
    src_abs = os.path.realpath(os.path.join(root, src))
    if (not src_abs.startswith(root + os.sep)
            or not os.path.isfile(os.path.join(src_abs, "SKILL.md"))):
        return False, "不是市场里的有效技能"
    name, _d = _read_skill_md_head(src_abs)
    if not name:
        name = os.path.basename(src_abs)
    name = re.sub(r'[<>:"/\\|?*\s]+', "-", name).strip("-") or "skill"
    dest = os.path.join(os.path.expanduser("~"), ".workbuddy", "skills", name)
    if os.path.exists(dest):
        return False, "「%s」已安装" % name
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copytree(src_abs, dest)
    except Exception as e:
        return False, "复制失败：%s" % e
    _SKILL_CACHE["at"] = 0.0          # 立刻让两个缓存失效
    _MKT_CACHE["at"] = 0.0
    return True, name


# ---------------------------------------------------------------- 前端页面
#  设计口径：深色工作台。参考图（某客户端更新器）的版式 —— 顶部页签、
#  版本行、三张统计卡、状态行列表、底部提示词编辑区。不抄内容，只借骨架。

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>破甲一键通</title>
<style>
:root{
  /* ---- 色板（OKLCH，感知均匀；中性色带极轻微 teal 偏移）---- */
  /* 表面：深色用「更亮 = 更高层」，不用阴影堆叠 */
  --void:  oklch(13.5% 0.006 200);      /* 最底（日志页） */
  --bg:    oklch(16%   0.008 200);      /* 应用背景 */
  --panel: oklch(19.5% 0.009 200);      /* 面板 */
  --card:  oklch(22.5% 0.010 200);      /* 卡片 */
  --card2: oklch(26%   0.011 200);      /* 卡片高亮/按钮底 */
  --raise: oklch(30%   0.012 200);      /* 抬升态 */

  --line:  oklch(28.5% 0.010 200);      /* 分割线 */
  --line2: oklch(35%   0.013 200);      /* 分割线（强） */

  --tx:    oklch(93%   0.006 200);      /* 主文本 */
  --tx2:   oklch(71%   0.012 200);      /* 次文本 */
  --tx3:   oklch(53%   0.010 200);      /* 弱文本 / 占位 */

  /* 主强调 = teal（冷冽专业，呼应「破甲」）；仅在 10% 面积出现 */
  --ac:    oklch(72%   0.115 192);
  --ac-dim:oklch(56%   0.085 192);
  --ac-bg: oklch(72%   0.115 192 / .12);

  --ok:    oklch(74%   0.135 152);
  --warn:  oklch(78%   0.125 78);
  --bad:   oklch(66%   0.165 25);
  --off:   oklch(50%   0.010 200);

  /* 字体：系统栈（离线硬要求）+ 等宽做数据 */
  --sans:"Segoe UI Variable Text","Segoe UI","Microsoft YaHei UI","PingFang SC",system-ui,sans-serif;
  --display:"Segoe UI Variable Display","Segoe UI","Microsoft YaHei UI",system-ui,sans-serif;
  --mono:ui-monospace,"Cascadia Mono","JetBrains Mono",Consolas,"Courier New",monospace;

  /* 动效：指数缓动，禁回弹 */
  --e-out-quart:cubic-bezier(.25,1,.5,1);
  --e-out-expo:cubic-bezier(.16,1,.3,1);
  --e-in-out:cubic-bezier(.65,0,.35,1);

  --r-sm:8px; --r-md:11px; --r-lg:14px; --r-xl:20px;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:var(--bg);color:var(--tx);
  font:14px/1.68 var(--sans);          /* 深色背景行高加裕量 */
  font-kerning:normal;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
/* 环境光：极淡的两团teal辉光，给纯深色底一点纵深（不刺眼） */
body::before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;
  background:
    radial-gradient(60vw 42vh at 14% -6%, oklch(72% .115 192 / .055), transparent 68%),
    radial-gradient(52vw 40vh at 96% 104%, oklch(72% .115 192 / .040), transparent 70%)}
.app{position:relative;z-index:1;max-width:1120px;margin:0 auto;padding:0 22px 118px}
body[data-p="log"] .app{padding-bottom:34px}

/* 等宽数字对齐 —— 所有数据用 */
.mono,.tkey,.ever,.edesc,.logbox,.skname,.upd-meta{font-family:var(--mono)}
.mono,[data-num]{font-variant-numeric:tabular-nums}

/* ---- 顶栏 ---- */
.top{display:flex;align-items:center;gap:16px;padding:16px 2px 0;
  border-bottom:1px solid var(--line);position:relative}
.brand{display:flex;align-items:center;gap:10px;font:600 15px/1 var(--display);
  letter-spacing:.2px;padding-bottom:13px}
.brand .dot{width:9px;height:9px;border-radius:50%;background:var(--ok);
  box-shadow:0 0 0 3px oklch(74% .135 152 / .16);flex:none}
.brand .dot.busy{background:var(--warn);box-shadow:0 0 0 3px oklch(78% .125 78 / .16);
  animation:pulse 1.1s var(--e-in-out) infinite alternate}
@keyframes pulse{to{opacity:.38}}
.brand .mk{width:22px;height:22px;flex:none;display:grid;place-items:center;
  border-radius:7px;background:var(--ac-bg);border:1px solid oklch(72% .115 192 / .34);
  color:var(--ac);font:700 12px/1 var(--mono)}

/* 玻璃药丸导航 —— 保留液态玻璃，但收窄、去浮夸 */
.tabs{position:relative;display:flex;padding:4px;margin-left:8px;border-radius:16px;
  background:linear-gradient(160deg,oklch(38% .012 200 / .34),oklch(24% .010 200 / .30));
  backdrop-filter:blur(18px) saturate(150%);-webkit-backdrop-filter:blur(18px) saturate(150%);
  border:1px solid oklch(60% .015 200 / .14);
  box-shadow:inset 0 1px 0 oklch(96% .01 200 / .10)}
#topPill{position:absolute;top:4px;left:0;height:calc(100% - 8px);width:0;border-radius:12px;
  background:linear-gradient(170deg,oklch(52% .020 200 / .85),oklch(38% .014 200 / .85));
  box-shadow:inset 0 1px 0 oklch(96% .01 200 / .22);
  opacity:0;pointer-events:none;
  transition:transform .34s var(--e-out-quart),width .34s var(--e-out-quart),opacity .2s}
.tab{position:relative;z-index:2;padding:6px 17px 5px;cursor:pointer;color:var(--tx2);
  user-select:none;font-size:13px;border-radius:12px;
  transition:color .2s var(--e-out-quart)}
.tab:hover{color:var(--tx)}
.tab.on{color:oklch(97% .006 200)}

.ver{margin-left:auto;font-size:12px;color:var(--tx3);padding-bottom:13px;
  display:flex;gap:4px;align-items:center}
.ver b{color:var(--tx2);font-weight:600}
.pill{font-size:11px;padding:2px 9px;border-radius:99px;border:1px solid var(--line2);
  color:var(--tx2);font-variant-numeric:tabular-nums;white-space:nowrap}
.pill.cur{border-color:oklch(72% .115 192 / .45);color:var(--ac);
  background:var(--ac-bg)}
a.gh{color:var(--tx3);text-decoration:none;font-size:12.5px;padding:5px 8px;
  border-radius:7px;transition:color .18s,background .18s;white-space:nowrap}
a.gh:hover{color:var(--tx);background:oklch(60% .015 200 / .12)}

.page{display:none;padding-top:20px}
.page.on{display:block;animation:pageIn .34s var(--e-out-quart)}
@keyframes pageIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}

/* ---- 状态条（页首信息行）---- */
.vrow{display:flex;align-items:center;gap:11px;background:var(--panel);
  border:1px solid var(--line);border-left:2px solid var(--ac-dim);
  border-radius:var(--r-md);padding:11px 15px;margin-bottom:16px}
.vrow .tag{font-size:12px;color:var(--tx2);font-weight:600}
.vrow .cur{font-size:12px;color:var(--ok);font-variant-numeric:tabular-nums}
.vrow .path{margin-left:auto;font-size:11.5px;color:var(--tx3);font-family:var(--mono);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:48%}

/* ---- 统计卡：三栏信息密度 ---- */
.cards{display:grid;grid-template-columns:1fr 1.35fr 1fr;gap:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r-md);
  padding:15px 17px;min-height:82px;position:relative;overflow:hidden;
  transition:transform .22s var(--e-out-quart),border-color .22s}
.card::after{content:"";position:absolute;left:0;top:0;bottom:0;width:2px;
  background:var(--ac-dim);opacity:.5}
.card:hover{transform:translateY(-2px);border-color:var(--line2)}
.card:active{transform:translateY(-1px) scale(.995)}
@keyframes tick{0%{transform:scale(1.10);color:var(--ac)}100%{transform:none}}
.card .big{font:700 26px/1.15 var(--display);letter-spacing:-.3px;
  font-variant-numeric:tabular-nums}
.card .big.tick{animation:tick .42s var(--e-out-quart)}
.card .big small{font:400 14px/1 var(--sans);color:var(--tx3)}
.card .sub{font-size:11.5px;color:var(--tx3);margin-top:5px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{font-size:11px;color:var(--tx2);border:1px solid var(--line2);
  border-radius:99px;padding:2px 10px;white-space:nowrap}
.chip.ok{color:var(--ok);border-color:oklch(74% .135 152 / .42);background:oklch(74% .135 152 / .07)}
.chip.warn{color:var(--warn);border-color:oklch(78% .125 78 / .42);background:oklch(78% .125 78 / .07)}
.chip.off{color:var(--tx3)}

/* ---- 按钮 ---- */
button{position:relative;overflow:hidden;background:var(--card2);color:var(--tx);
  border:1px solid var(--line2);border-radius:var(--r-sm);padding:8px 17px;font-size:13px;
  cursor:pointer;font-family:inherit;
  transition:transform .16s var(--e-out-quart),background .14s,border-color .14s}
button:hover:not(:disabled){background:var(--raise);border-color:oklch(42% .015 200)}
button:active:not(:disabled){transform:scale(.955)}
button:focus-visible{outline:2px solid var(--ac);outline-offset:2px}
button:disabled{opacity:.42;cursor:not-allowed}
button.mini{padding:4px 11px;font-size:12px;border-radius:7px}
.spacer{flex:1;min-width:8px}
.ripple{position:absolute;border-radius:50%;pointer-events:none;
  background:radial-gradient(circle,oklch(96% .01 200 / .34),transparent 62%);
  animation:rip .48s var(--e-out-quart) forwards}
@keyframes rip{from{opacity:.85;transform:scale(.1)}to{opacity:0;transform:scale(1)}}

/* ---- 底部操作坞（液态玻璃）---- */
.dock{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:60;
  display:flex;align-items:center;gap:9px;padding:9px 12px;
  max-width:min(96vw,920px);border-radius:var(--r-xl);
  background:linear-gradient(160deg,oklch(40% .013 200 / .48),oklch(25% .010 200 / .42));
  backdrop-filter:blur(24px) saturate(165%);-webkit-backdrop-filter:blur(24px) saturate(165%);
  border:1px solid oklch(64% .015 200 / .16);
  box-shadow:inset 0 1px 0 oklch(96% .01 200 / .16),0 12px 34px oklch(8% .01 200 / .55);
  transition:transform .36s var(--e-out-quart),opacity .28s}
body[data-p="log"] .dock{transform:translateX(-50%) translateY(130px);opacity:0;pointer-events:none}
.dsep{width:1px;height:22px;background:oklch(64% .015 200 / .18);flex:none}
.dock .ck{color:var(--tx2);flex:none}
.dock .snapinfo{color:var(--tx3);opacity:.95;max-width:180px;overflow:hidden;
  text-overflow:ellipsis;font-variant-numeric:tabular-nums}
.dbtn{position:relative;overflow:hidden;flex:none;
  background:oklch(66% .014 200 / .10);color:var(--tx);border:1px solid oklch(66% .014 200 / .18);
  border-radius:15px;padding:9px 18px;font-size:13px;cursor:pointer;font-family:inherit;
  transition:transform .18s var(--e-out-quart),background .15s,border-color .15s}
.dbtn:hover:not(:disabled){background:oklch(70% .014 200 / .18)}
.dbtn:active:not(:disabled){transform:scale(.94)}
.dbtn:focus-visible{outline:2px solid var(--ac);outline-offset:2px}
.dbtn:disabled{opacity:.4;cursor:not-allowed}
.dbtn.primary{background:linear-gradient(160deg,oklch(76% .115 192),oklch(63% .105 192));
  color:oklch(18% .02 200);font-weight:650;border-color:oklch(80% .10 192 / .5);
  box-shadow:inset 0 1px 0 oklch(96% .02 192 / .4),0 3px 15px oklch(72% .115 192 / .28)}
.dbtn.primary:hover:not(:disabled){background:linear-gradient(160deg,oklch(80% .12 192),oklch(67% .11 192))}
.dbtn.danger{color:oklch(76% .14 25);background:oklch(66% .165 25 / .14);
  border-color:oklch(66% .165 25 / .34)}
.dbtn.danger:hover:not(:disabled){background:oklch(66% .165 25 / .24)}
.snapinfo{font-size:11px;color:var(--tx3);white-space:nowrap;opacity:.9}
label.ck{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--tx2);
  cursor:pointer;user-select:none}
.note{font-size:12px;color:var(--tx3);line-height:1.6}

/* ---- 目标列表（信息密度优先）---- */
.tlist{border:1px solid var(--line);border-radius:var(--r-md);overflow:hidden;
  background:var(--panel)}
.trow{border-bottom:1px solid var(--line)}
.trow:last-child{border-bottom:none}
.thead{display:flex;align-items:center;gap:12px;padding:13px 16px;cursor:pointer;
  user-select:none;transition:background .15s}
.thead:hover{background:oklch(64% .014 200 / .05)}
.thead:active{background:oklch(64% .014 200 / .09)}
.thead .st{width:8px;height:8px;border-radius:50%;flex:none}
.st.ok{background:var(--ok);box-shadow:0 0 0 3px oklch(74% .135 152 / .14)}
.st.warn{background:var(--warn)}
.st.fail{background:var(--bad)}
.st.off{background:var(--off)}
.tname{font-weight:600;font-size:13.5px;width:158px;flex:none}
.tkey{font-size:11px;color:var(--tx3);width:86px;flex:none}
.tstat{font-size:12px;color:var(--tx2)}
.tstat.warn{color:var(--warn)}.tstat.ok{color:var(--ok)}
.tstat.fail{color:var(--bad)}.tstat.off{color:var(--tx3)}
.tsel{margin-left:auto;display:flex;align-items:center;gap:11px}
input[type=checkbox]{accent-color:var(--ac);width:15px;height:15px;cursor:pointer;
  transition:transform .16s var(--e-out-quart)}
input[type=checkbox]:active{transform:scale(1.24)}
.arrow{color:var(--tx3);font-size:11px;
  transition:transform .3s var(--e-out-quart)}
.trow.open .arrow{transform:rotate(90deg)}
.tbody{display:none;padding:3px 16px 15px 38px}
.trow.open .tbody{display:block}
.trow.open .tbody>.drow{animation:rowIn .26s var(--e-out-quart) backwards}
@keyframes rowIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
.drow{display:flex;gap:11px;font-size:12.5px;padding:3px 0;align-items:baseline}
.drow .m{flex:none;font-family:var(--mono);font-size:11px;width:40px}
.m.ok,.m.own{color:var(--ok)}.m.warn{color:var(--warn)}
.m.fail{color:var(--bad)}.m.info{color:var(--tx3)}
.drow .t{color:var(--tx2);word-break:break-all}
.drow .d{color:var(--tx3);font-size:11.5px;word-break:break-all}
.thead input[type=checkbox]{flex:none}

/* ---- 提示词 ---- */
.pwrap{margin-top:20px}
.phead{display:flex;align-items:center;margin-bottom:9px;gap:9px}
.phead .t{font-size:13.5px;font-weight:600}
.phead .s{font-size:12px;color:var(--tx3)}
textarea#persona{width:100%;min-height:158px;background:var(--void);
  color:var(--tx);border:1px solid var(--line);border-radius:var(--r-md);padding:13px 15px;
  font:12.5px/1.72 var(--mono);resize:vertical;outline:none;
  transition:border-color .18s}
textarea#persona:focus{border-color:var(--ac-dim)}
.saved{color:var(--ok);font-size:12px;opacity:0;transition:opacity .3s}
.saved.show{opacity:1}

/* ---- 环境页 ---- */
.elist{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.ecard{background:var(--card);border:1px solid var(--line);border-radius:var(--r-md);
  padding:14px 16px;display:flex;gap:13px;align-items:flex-start;
  transition:transform .22s var(--e-out-quart),border-color .22s}
.ecard:hover{transform:translateY(-2px);border-color:var(--line2)}
.ecard:active{transform:translateY(-1px) scale(.995)}
.ecard.miss{border-color:oklch(66% .165 25 / .36)}
.ecard.warn{border-color:oklch(78% .125 78 / .36)}
.einfo{flex:1;min-width:0}
.ename{font-weight:600;font-size:13.5px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.ever{font-size:11px;color:var(--tx3);font-weight:400}
.edesc{font-size:11.5px;color:var(--tx3);margin-top:4px;word-break:break-all;
  max-height:34px;overflow:hidden}
.ebadge{flex:none;font-size:11px;border-radius:99px;padding:3px 11px;border:1px solid;
  white-space:nowrap;font-weight:600}
.ebadge.ok{color:var(--ok);border-color:oklch(74% .135 152 / .4);background:oklch(74% .135 152 / .08)}
.ebadge.miss{color:var(--bad);border-color:oklch(66% .165 25 / .4);background:oklch(66% .165 25 / .08)}
.ebadge.warn{color:var(--warn);border-color:oklch(78% .125 78 / .4);background:oklch(78% .125 78 / .08)}
.ebadge.busy{color:var(--ac);border-color:oklch(72% .115 192 / .4);background:var(--ac-bg);
  animation:pulse 1.1s var(--e-in-out) infinite alternate}
.ecard input[type=checkbox]{margin-top:3px}

/* 玻璃坞按页切换 */
body[data-p="env"] .dock .main-only{display:none}
body:not([data-p="env"]) .dock .env-only{display:none}
body[data-p="env"] .dock .spacer{display:none}

/* ---- Skill 页 ---- */
body[data-p="skills"] .app{padding-bottom:34px}
.skbar{display:flex;gap:10px;align-items:center;margin:0 0 4px}
.sksearch{flex:1;max-width:380px;background:var(--void);color:var(--tx);
  border:1px solid var(--line);border-radius:var(--r-md);padding:8px 13px;
  font:12.5px/1.4 var(--sans);outline:none;transition:border-color .18s}
.sksearch:focus{border-color:var(--ac-dim)}
.sksearch::placeholder{color:var(--tx3)}
.sksec{display:flex;align-items:baseline;gap:9px;margin:24px 0 11px;
  padding-bottom:8px;border-bottom:1px solid var(--line)}
.sksec .t{font-size:13px;font-weight:600;color:var(--tx)}
.sksec .n{font-size:11px;color:var(--tx3);font-family:var(--mono)}
.sksec .rule{flex:1}
.skgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.skgrid.inst{grid-template-columns:1fr 1fr 1fr}
@media(max-width:900px){.skgrid{grid-template-columns:1fr}
  .skgrid.inst{grid-template-columns:1fr 1fr}}
.skcard{background:var(--card);border:1px solid var(--line);border-radius:var(--r-md);
  padding:13px 15px;display:flex;gap:12px;align-items:flex-start;
  transition:transform .22s var(--e-out-quart),border-color .22s}
.skcard:hover{transform:translateY(-2px);border-color:var(--line2)}
.skcard:active{transform:translateY(-1px) scale(.995)}
.skcard .ebadge{align-self:center;margin-top:2px}
.skava{flex:none;width:34px;height:34px;border-radius:9px;display:flex;
  align-items:center;justify-content:center;font:700 15px var(--mono)}
.skinfo{flex:1;min-width:0}
.skname{font-weight:600;font-size:13px;display:flex;gap:7px;align-items:center;
  flex-wrap:wrap;word-break:break-all}
.skcat{font-size:10.5px;border-radius:99px;padding:1px 9px;font-weight:400;flex:none;
  font-family:var(--sans)}
.skuse{font-size:12px;color:var(--tx2);margin-top:5px;line-height:1.6}
.skhow{font-size:11px;color:var(--tx3);margin-top:4px;line-height:1.55}
.skempty{padding:28px 0;color:var(--tx3);font-size:12.5px;text-align:center;display:none}
.skinst{flex:none;align-self:center;font-size:11px;border-radius:8px;padding:5px 13px;
  cursor:pointer;border:1px solid oklch(74% .135 152 / .4);
  background:oklch(74% .135 152 / .10);color:var(--ok);font-family:inherit;
  transition:transform .16s,background .18s,opacity .18s}
.skinst:hover{background:oklch(74% .135 152 / .18)}
.skinst:active{transform:scale(.94)}
.skinst:disabled{opacity:.55;cursor:default}
.skinst.done{border-color:var(--line2);background:oklch(64% .014 200 / .05);
  color:var(--tx3);cursor:default}
.ebadge.no{color:var(--tx3);border-color:var(--line2);background:oklch(64% .014 200 / .05)}
body[data-p="skills"] .dock .main-only{display:none}
body[data-p="skills"] .dock{transform:translateX(-50%) translateY(130px);opacity:0;pointer-events:none}

/* ---- 日志页 ---- */
.logbar{display:flex;gap:9px;align-items:center;margin-bottom:11px}
.logbox{background:var(--void);border:1px solid var(--line);border-radius:var(--r-md);
  padding:13px 15px;height:calc(100vh - 222px);overflow-y:auto;
  font:12px/1.78 var(--mono);white-space:pre-wrap;word-break:break-all}
body[data-p="log"] .logbox{height:calc(100vh - 162px)}
::-webkit-scrollbar{width:11px;height:11px}
::-webkit-scrollbar-thumb{background:oklch(34% .012 200);border-radius:7px;
  border:3px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:oklch(42% .014 200);background-clip:padding-box}
::-webkit-scrollbar-track,::-webkit-scrollbar-corner{background:transparent}
.logbox .ln{color:oklch(76% .008 200)}
.logbox .ln.err{color:var(--bad)}
.passcard{background:var(--card);border:1px solid var(--line);border-radius:var(--r-md);
  padding:13px 17px;margin-top:13px;font-size:12.5px;color:var(--tx2)}
.passcard code{font-family:var(--mono);color:var(--warn);
  background:oklch(78% .125 78 / .09);padding:2px 8px;border-radius:6px}
.toast{position:fixed;left:50%;bottom:100px;transform:translateX(-50%) translateY(16px);
  background:oklch(30% .014 200 / .96);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);
  border:1px solid var(--line2);color:var(--tx);
  padding:10px 19px;border-radius:10px;font-size:13px;opacity:0;pointer-events:none;
  transition:opacity .22s var(--e-out-quart),transform .22s var(--e-out-quart);
  box-shadow:0 10px 34px oklch(8% .01 200 / .6);z-index:99}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

/* 无障碍：尊重系统「减少动态效果」 */
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms!important;
    animation-iteration-count:1!important;transition-duration:.01ms!important}
}
</style>
</head>
<body data-p="main">
<div class="app">

  <div class="top">
    <div class="brand"><span class="mk">破</span>破甲一键通<span class="dot" id="dot" style="margin-left:2px"></span></div>
    <div class="tabs">
      <div id="topPill"></div>
      <div class="tab on" data-p="main">破甲</div>
      <div class="tab" data-p="env">环境</div>
      <div class="tab" data-p="skills">Skill</div>
      <div class="tab" data-p="log">日志</div>
    </div>
    <div class="ver"><span class="pill cur" id="verpill">v__VER__ · 当前</span>
      <a class="gh" href="https://github.com/z91772524-ai/pojia-next" target="_blank">GitHub</a></div>
  </div>

  <!-- ========== 目标页 ========== -->
  <div class="page on" id="p-main">
    <div class="vrow">
      <span class="tag">核心</span><span class="pill cur">已就绪</span>
      <span class="cur" id="coreinfo">v__VER__ · 本地核心</span>
      <span class="path" id="corepath"></span>
    </div>

    <div class="cards">
      <div class="card"><div class="big" id="c1">- <small>/ -</small></div>
        <div class="sub">已破甲 / 已检测到的客户端</div></div>
      <div class="card"><div class="chips" id="c2"><span class="chip off">加载中…</span></div>
        <div class="sub">客户端探测结果</div></div>
      <div class="card"><div class="big" id="c3">-</div>
        <div class="sub">待处理项（过期 / 未破甲 / 错误）</div></div>
    </div>

    <div class="note" id="hintbar" style="margin:0 0 12px">
      破甲 = 对勾选目标执行破甲 · 演练 = 只看会改什么、不写盘 · 还原 = 回到官方原版
    </div>

    <div class="tlist" id="tlist"></div>

    <div class="pwrap">
      <div class="phead">
        <span class="t">提示词</span>
        <span class="s">所有目标共用这一份人格（persona.md）</span>
        <span class="spacer"></span>
        <span class="saved" id="psaved">已保存</span>
        <button class="mini" id="b-psave">保存</button>
        <button class="mini" id="b-preset">恢复默认</button>
      </div>
      <textarea id="persona" spellcheck="false"></textarea>
    </div>
  </div>

  <!-- ========== 环境页 ========== -->
  <div class="page" id="p-env">
    <div class="vrow">
      <span class="tag">一键补全</span>
      <span class="cur" id="envstat">检测中…</span>
      <span class="path" id="envnote">常用开发环境：缺什么补什么</span>
    </div>

    <div class="note" style="margin:0 0 12px">
      勾选要补全的环境（缺的默认勾上），点底部「补全环境」。
      下载优先走国内镜像；需要管理员权限的项会弹系统授权框（UAC），点「是」继续；
      安装进度实时输出在「日志」页，装完自动重新检测。
    </div>

    <div class="elist" id="elist"></div>
  </div>

  <!-- ========== Skill 页 ========== -->
  <div class="page" id="p-skills">
    <div class="vrow">
      <span class="tag">Skill</span>
      <span class="cur" id="skstat">加载中…</span>
      <span class="path" id="sknote">市场数据由客户端自动同步 · 安装到用户级 skills 目录</span>
    </div>

    <div class="skbar">
      <input class="sksearch" id="sksearch" placeholder="按名字、描述、分类过滤">
    </div>

    <div class="sksec">
      <span class="t">精选</span><span class="n" id="skrecn"></span>
    </div>
    <div class="skgrid" id="skrec"></div>
    <div class="skempty" id="skrecnone">没有匹配的精选</div>

    <div class="sksec">
      <span class="t">市场 · 可装</span><span class="n" id="skmktn"></span>
    </div>
    <div class="skgrid inst" id="skmkt"></div>
    <div class="skempty" id="skmktnone">没有可装的（市场技能都已安装，或客户端还没同步市场数据）</div>

    <div class="sksec">
      <span class="t">已装</span><span class="n" id="skalln"></span>
    </div>
    <div class="skgrid inst" id="skall"></div>
    <div class="skempty" id="skallnone">没有匹配的技能</div>
  </div>

  <!-- ========== 日志页 ========== -->
  <div class="page" id="p-log">
    <div class="logbar">
      <span class="note" id="logstat"></span>
      <span class="spacer"></span>
      <label class="ck"><input type="checkbox" id="autoscroll" checked>自动滚动</label>
      <button class="mini" id="b-logclear">清空显示</button>
    </div>
    <div class="logbox" id="logbox"></div>
    <div class="passcard">自证口令：在对应客户端的新会话里单独发
      <code id="passtext">…</code>，它应只回复 <code id="replytext">…</code></div>
  </div>
</div>

<!-- 底部悬浮液态玻璃操作坞 -->
<div class="dock" id="dock">
  <label class="ck main-only"><input type="checkbox" id="selall">全选已检测</label>
  <span class="dsep main-only"></span>
  <span class="snapinfo main-only" id="snapinfo"></span>
  <span class="spacer"></span>
  <button class="dbtn primary main-only" id="b-apply" title="对勾选目标执行破甲（写盘）">破甲</button>
  <button class="dbtn main-only" id="b-dry" title="演练：只看会改什么，不写盘">演练</button>
  <button class="dbtn danger main-only" id="b-revert" title="还原 = 勾选目标回到官方原版">还原</button>
  <button class="dbtn main-only" id="b-refresh" title="强制重扫磁盘状态">刷新</button>
  <button class="dbtn env-only" id="b-envrescan" title="重新检测所有环境">重新检测</button>
  <button class="dbtn primary env-only" id="b-envfix" title="下载并安装勾选的缺失环境">补全环境</button>
</div>
<div class="toast" id="toast"></div>


<script>
"use strict";
const $=s=>document.querySelector(s), $$=s=>document.querySelectorAll(s);
let STATE=null, LOGNEXT=0, LOGTIMER=null, SEL=new Set(), OPEN=new Set(), _lastSig="";

function toast(t){const e=$("#toast");e.textContent=t;e.classList.add("show");
  clearTimeout(e._t);e._t=setTimeout(()=>e.classList.remove("show"),2200);}

/* ---- 页签（玻璃药丸滑动）---- */
function movePill(el){const p=document.getElementById("topPill");
  if(!p||!el||!el.offsetWidth)return;
  p.style.width=el.offsetWidth+"px";
  p.style.transform="translateX("+el.offsetLeft+"px)";p.style.opacity="1";}
addEventListener("resize",()=>movePill(document.querySelector(".tab.on")));
$$(".tab").forEach(t=>t.onclick=()=>{
  $$(".tab").forEach(x=>x.classList.toggle("on",x===t));
  $$(".page").forEach(p=>p.classList.toggle("on",p.id==="p-"+t.dataset.p));
  document.body.dataset.p=t.dataset.p;
  movePill(t);
  if(t.dataset.p==="log") logTimerStart(); else logTimerStop();
  if(t.dataset.p==="env") loadEnv();
  if(t.dataset.p==="skills") loadSkills();
});

/* ---- 全局点击涟漪（每个按钮按下都有一圈光晕）---- */
document.addEventListener("pointerdown",e=>{
  const b=e.target.closest("button");if(!b||b.disabled)return;
  const rect=b.getBoundingClientRect(),d=Math.max(rect.width,rect.height)*1.5;
  const r=document.createElement("span");r.className="ripple";
  r.style.cssText="width:"+d+"px;height:"+d+"px;left:"+(e.clientX-rect.left-d/2)
    +"px;top:"+(e.clientY-rect.top-d/2)+"px";
  b.appendChild(r);setTimeout(()=>r.remove(),520);
});

/* ---- 状态加载 ---- */
async function loadState(fresh){
  try{
    const r=await fetch("/api/state"+(fresh?"?fresh=1":""));STATE=await r.json();
    render();renderLogMeta();
  }catch(e){ $("#c1").innerHTML="离线"; }
}
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}

function render(){
  if(!STATE)return;
  $("#dot").className="dot"+(STATE.busy?" busy":"");
  $("#corepath").textContent=STATE.core_file||"";
  const ci=$("#coreinfo");
  if(ci)ci.textContent="v__VER__ · "+(STATE.core_file&&STATE.core_file.indexOf("云")>=0?"云端下发":"本地核心");  const st=STATE.stats;
  const sig=st.ok+"/"+st.detected+"/"+st.pending;
  if(sig!==_lastSig){_lastSig=sig;
    for(const id of["c1","c3"]){const el=$("#"+id);
      el.classList.remove("tick");void el.offsetWidth;el.classList.add("tick");}}
  $("#c1").innerHTML=st.detected?(STATE.building&&!st.total?"检测中…":`${st.ok} <small>/ ${st.detected}</small>`):"检测中…";
  $("#c3").textContent=st.pending;
  const age=STATE.snap_age!=null?`快照 ${STATE.snap_age}s 前${STATE.building?" · 重扫中…":""}`:(STATE.building?"首次检测中…":"");
  $("#snapinfo").textContent=age;
  const chips=STATE.targets.map(t=>{
    const cls=t.status==="ok"?"ok":(t.status==="off"?"off":(t.status==="fail"?"warn":"warn"));
    return `<span class="chip ${cls}" title="${esc(t.status_text)}">${esc(t.label)}</span>`;
  });
  $("#c2").innerHTML=chips.join("")||'<span class="chip off">检测中…</span>';

  /* 目标列表 */
  const list=$("#tlist");list.innerHTML="";
  STATE.targets.forEach(t=>{
    const row=document.createElement("div");row.className="trow"+(OPEN.has(t.key)?" open":"");
    const canSel=t.detected;
    const checked=SEL.has(t.key)?"checked":"";
    row.innerHTML=`
      <div class="thead">
        <span class="st ${t.status}"></span>
        <span class="tname">${esc(t.label)}</span>
        <span class="tkey">${esc(t.key)}</span>
        <span class="tstat ${t.status}">${esc(t.status_text)}</span>
        <span class="tsel">
          ${canSel?`<input type="checkbox" data-k="${t.key}" ${checked}>`:""}
          <span class="arrow">▶</span>
        </span>
      </div>
      <div class="tbody">${t.rows.map(r=>
        `<div class="drow"><span class="m ${r.st}">[${esc(r.st)}]</span>
         <span class="t">${esc(r.note)}</span>
         ${r.detail?`<span class="d">${esc(r.detail)}</span>`:""}</div>`).join("")||
         '<div class="drow"><span class="m info">[--]</span><span class="t">无详情</span></div>'}
      </div>`;
    if(canSel) row.querySelector("input").onclick=ev=>{
      ev.stopPropagation();
      if(ev.target.checked)SEL.add(t.key);else SEL.delete(t.key);
      syncSelAll();
    };
    row.querySelector(".thead").onclick=()=>{
      row.classList.toggle("open");
      row.classList.contains("open")?OPEN.add(t.key):OPEN.delete(t.key);
    };
    list.appendChild(row);
  });
  syncSelAll();

  /* 按钮可用性 */
  const busy=STATE.busy;
  ["b-apply","b-dry","b-revert","b-refresh"].forEach(id=>$("#"+id).disabled=busy);
  $("#b-apply").textContent=busy?(STATE.busy_label||"处理中…"):"破甲";
  $("#passtext").textContent=STATE.passphrase;
  $("#replytext").textContent=STATE.signal_reply;
}

function syncSelAll(){
  const det=[...$$(".trow input[data-k]")];
  $("#selall").checked=det.length>0&&det.every(i=>i.checked);
  $("#selall").indeterminate=!$("#selall").checked&&det.some(i=>i.checked);
}
$("#selall").onchange=e=>{
  const det=[...$$(".trow input[data-k]")];
  det.forEach(i=>{i.checked=e.target.checked;
    e.target.checked?SEL.add(i.dataset.k):SEL.delete(i.dataset.k);});
};

/* ---- 动作 ---- */
async function act(action,confirmMsg){
  const targets=[...$$(".trow input[data-k]:checked")].map(i=>i.dataset.k);
  if(!targets.length){toast("先勾选至少一个目标");return;}
  if(confirmMsg&&!confirm(confirmMsg))return;
  try{
    const r=await fetch("/api/action",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({action,targets})});
    const j=await r.json();
    if(!j.ok){toast(j.err||"动作被拒绝");return;}
    toast({apply:"开始破甲",revert:"开始还原","dry-run":"开始演练（不写盘）",
           status:"开始检测"}[action]||action);
    setTimeout(loadState,600);
  }catch(e){toast("请求失败："+e);}
}
$("#b-apply").onclick=()=>act("apply",null);
$("#b-dry").onclick=()=>act("dry-run",null);
$("#b-revert").onclick=()=>act("revert","还原会把勾选目标变回官方原版，确认？");
$("#b-refresh").onclick=async()=>{toast("正在重扫…");await loadState(true);toast("已刷新");};

/* ---- 环境页 ---- */
let ENV=null, ESEL=null;
async function loadEnv(){
  try{
    const r=await fetch("/api/env");ENV=await r.json();renderEnv();
  }catch(e){}
}
function renderEnv(){
  if(!ENV)return;
  if(ESEL===null&&ENV.items&&ENV.items.length){
    ESEL=new Set(ENV.items.filter(i=>i.status!=="ok").map(i=>i.key));
  }
  const items=ENV.items||[];
  const okn=items.filter(i=>i.status==="ok").length;
  $("#envstat").textContent=ENV.scanning?"检测中…":
    (items.length?("就绪 "+okn+" / "+items.length):"等待检测…");
  const box=$("#elist");
  box.innerHTML=items.map(i=>{
    const badge=(ENV.busy&&ENV.busy_key===i.key)?"busy":i.status;
    const btxt={ok:"已就绪",miss:"未安装",warn:"需修复",busy:"处理中…"}[badge]||"检测中";
    const bcls=["ok","miss","warn","busy"].includes(badge)?badge:"warn";
    const cls=i.status==="ok"?"":i.status;
    const ver=i.version?`<span class="ever">${esc(i.version)}</span>`:"";
    const chk=(ESEL&&ESEL.has(i.key))?"checked":"";
    return `<div class="ecard ${cls}">
      <input type="checkbox" class="eck" data-k="${i.key}" ${chk}>
      <div class="einfo">
        <div class="ename">${esc(i.name)} ${ver}</div>
        <div class="edesc" title="${esc(i.detail||i.note||"")}">${esc(i.detail||i.note||"")}</div>
      </div>
      <span class="ebadge ${bcls}">${btxt}</span>
    </div>`;
  }).join("")||'<div class="note">检测中…</div>';
  $$(".eck").forEach(chk=>chk.onchange=ev=>{
    if(!ESEL)ESEL=new Set();
    ev.target.checked?ESEL.add(chk.dataset.k):ESEL.delete(chk.dataset.k);
  });
  $("#b-envfix").disabled=!!ENV.busy;
  $("#b-envrescan").disabled=!!ENV.busy||!!ENV.scanning;
  $("#b-envfix").textContent=ENV.busy?(ENV.busy_label||"补全中…"):"补全环境";
}
$("#b-envrescan").onclick=async()=>{
  try{
    await fetch("/api/env/scan",{method:"POST"});
    toast("重新检测中…");setTimeout(loadEnv,700);
  }catch(e){toast("请求失败："+e);}
};
$("#b-envfix").onclick=async()=>{
  const keys=ESEL?[...ESEL]:[];
  if(!keys.length){toast("先勾选要补全的环境");return;}
  const need=keys.filter(k=>{
    const it=(ENV&&ENV.items||[]).find(x=>x.key===k);
    return it&&it.status!=="ok";
  });
  if(!need.length){toast("勾选的环境都已就绪");return;}
  if(!confirm("开始补全选中的缺失环境？\n"+
      "需要管理员权限的项目会弹系统授权框（UAC），一路点「是」即可。"))return;
  try{
    const r=await fetch("/api/env/install",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({keys:need})});
    const j=await r.json();
    if(!j.ok){toast(j.err||"被拒绝");return;}
    toast("开始补全 —— 进度见「日志」页");
    setTimeout(loadEnv,600);
  }catch(e){toast("请求失败："+e);}
};

/* ---- Skill ---- */
let SKILLS=null;
const SKCAT_COLOR={"搞机逆向":"#c08cff","效率工具":"#4ecfb4","内容创作":"#e0a85c",
  "脚本保护":"#e07a72","系统控制":"#56bede","系统诊断":"#d4c85a","硬件改造":"#63c586",
  "网络服务":"#7fa8f0","仓库维护":"#9aa0a8","生活决策":"#e58cb8","技能发现":"#4ec9c9"};
function skCol(c){return SKCAT_COLOR[c]||"#4ec9c9";}
const SKAVA_PALETTE=["#c08cff","#4ecfb4","#e0a85c","#e07a72","#56bede",
  "#d4c85a","#63c586","#7fa8f0","#e58cb8","#4ec9c9"];
function skAvaColor(c,n){
  if(c&&SKCAT_COLOR[c])return SKCAT_COLOR[c];
  let h=0;for(let i=0;i<n.length;i++)h=(h*31+n.charCodeAt(i))>>>0;
  return SKAVA_PALETTE[h%SKAVA_PALETTE.length];}
function skChip(c){const col=skCol(c);
  return `<span class="skcat" style="color:${col};border:1px solid ${col}55;background:${col}14">${esc(c)}</span>`;}
function skAva(c,n){const col=skAvaColor(c,n);
  return `<div class="skava" style="color:${col};background:${col}14;border:1px solid ${col}33">${esc(n.charAt(0).toUpperCase())}</div>`;}
function skHit(s,q){q=q.trim().toLowerCase();if(!q)return true;
  return (s.name+" "+(s.desc||"")+" "+(s.use||"")+" "+(s.how||"")+" "+(s.cat||"")).toLowerCase().includes(q);}
async function loadSkills(){
  try{
    const r=await fetch("/api/skills");SKILLS=await r.json();renderSkills();
  }catch(e){ $("#skstat").textContent="加载失败"; }
}
function renderSkills(){
  if(!SKILLS)return;
  const q=$("#sksearch").value||"";
  const rec=SKILLS.recommended.filter(s=>skHit(s,q));
  const ins=SKILLS.installed.filter(s=>skHit(s,q));
  const mkt=(SKILLS.market||[]).filter(s=>skHit(s,q));
  $("#skrec").innerHTML=rec.map(s=>`
    <div class="skcard">
      ${skAva(s.cat,s.name)}
      <div class="skinfo">
        <div class="skname">${esc(s.name)} ${skChip(s.cat)}</div>
        <div class="skuse">${esc(s.use)}</div>
        <div class="skhow">例：${esc(s.how)}</div>
      </div>
      ${s.installed?'<span class="ebadge ok">已装</span>'
                  :'<span class="ebadge no">未装</span>'}
    </div>`).join("");
  $("#skmkt").innerHTML=mkt.map(s=>`
    <div class="skcard">
      ${skAva("",s.name)}
      <div class="skinfo">
        <div class="skname">${esc(s.name)}</div>
        <div class="skuse">${esc(s.desc)}</div>
      </div>
      <button class="skinst" data-src="${esc(s.src)}">安装</button>
    </div>`).join("");
  $("#skall").innerHTML=ins.map(s=>`
    <div class="skcard">
      ${skAva("",s.name)}
      <div class="skinfo">
        <div class="skname">${esc(s.name)}</div>
        <div class="skuse">${esc(s.desc)}</div>
      </div>
    </div>`).join("");
  $("#skrecn").textContent=rec.length+"/"+SKILLS.recommended.length;
  $("#skmktn").textContent=mkt.length+"/"+(SKILLS.market||[]).length;
  $("#skalln").textContent=ins.length+"/"+SKILLS.installed.length;
  $("#skrecnone").style.display=rec.length?"none":"block";
  $("#skmktnone").style.display=mkt.length?"none":"block";
  $("#skallnone").style.display=ins.length?"none":"block";
  const nInst=SKILLS.installed.length,nRec=SKILLS.recommended.length;
  const nYes=SKILLS.recommended.filter(s=>s.installed).length;
  const nMkt=(SKILLS.market||[]).length;
  $("#skstat").textContent=`本机 ${nInst} · 市场 ${nMkt} · 精选 ${nRec}（在机 ${nYes}）`;
}
$("#sksearch").addEventListener("input",()=>renderSkills());
$("#skmkt").addEventListener("click",async e=>{
  const b=e.target.closest(".skinst");if(!b||b.disabled)return;
  const src=b.dataset.src;
  b.disabled=true;b.textContent="安装中…";
  try{
    const r=await fetch("/api/skill/install",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({src})});
    const j=await r.json();
    if(j.ok){b.textContent="已装";b.classList.add("done");toast("已装 "+j.name);
      loadSkills();}
    else{b.disabled=false;b.textContent="安装";toast(j.err||"安装失败");}
  }catch(err){b.disabled=false;b.textContent="安装";toast("请求失败："+err);}
});

/* ---- 提示词 ---- */
async function loadPersona(){
  const r=await fetch("/api/persona");const j=await r.json();
  $("#persona").value=j.text;
}
$("#b-psave").onclick=async()=>{
  await fetch("/api/persona",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({text:$("#persona").value})});
  const s=$("#psaved");s.classList.add("show");setTimeout(()=>s.classList.remove("show"),1500);
  toast("persona.md 已保存 —— 对目标重新「破甲」后生效");
};
$("#b-preset").onclick=async()=>{
  if(!confirm("恢复成内置默认人格？（会覆盖当前编辑框内容）"))return;
  const r=await fetch("/api/persona/reset",{method:"POST"});
  const j=await r.json();$("#persona").value=j.text;toast("已恢复默认");
};

/* ---- 日志 ---- */
function renderLogMeta(){
  $("#logstat").textContent=STATE&&STATE.log_path?("落盘日志："+STATE.log_path):"";
}
async function pullLog(){
  try{
    const r=await fetch("/api/log?after="+LOGNEXT);const j=await r.json();
    if(!j.lines.length)return;
    const box=$("#logbox");
    const atBottom=box.scrollHeight-box.scrollTop-box.clientHeight<40;
    j.lines.forEach(([seq,ln])=>{
      const d=document.createElement("div");d.className="ln"+(/错误|失败|失败：|！！/.test(ln)?" err":"");
      d.textContent=ln;box.appendChild(d);
    });
    LOGNEXT=j.next;
    while(box.childNodes.length>3000)box.removeChild(box.firstChild);
    if(atBottom||$("#autoscroll").checked)box.scrollTop=box.scrollHeight;
  }catch(e){}
}
function logTimerStart(){pullLog();LOGTIMER=setInterval(pullLog,1200);}
function logTimerStop(){if(LOGTIMER){clearInterval(LOGTIMER);LOGTIMER=null;}}
$("#b-logclear").onclick=async()=>{
  await fetch("/api/log/clear",{method:"POST"});
  $("#logbox").innerHTML="";LOGNEXT=0;
};

/* ---- 启动 ---- */
function appBoot(){
  if(window.__pojiaBooted)return;window.__pojiaBooted=true;
  loadState();loadPersona();pullLog();
  setTimeout(()=>movePill(document.querySelector(".tab.on")),60);
  setInterval(async()=>{   // 闲时 8s 刷一次目标页
    if($$(".tab")[0].classList.contains("on"))await loadState();
  },8000);
  setInterval(async()=>{   // 忙碌 / 后台重扫期间 1.2s 加速轮询
    if(STATE&&(STATE.busy||STATE.building))await loadState();
  },1200);
  setInterval(async()=>{   // 环境页在场时 5s 刷一次
    if(document.body.dataset.p==="env")await loadEnv();
  },5000);
  setInterval(async()=>{   // 环境补全进行中 1.2s 加速
    if(ENV&&ENV.busy)await loadEnv();
  },1200);
}

/* ---- 启动（v8.8：核心本地随包加载，直接进入主界面，无群号验证） ---- */
appBoot();
</script>
</body>
</html>
""".replace("__VER__", VERSION)


# ---------------------------------------------------------------- HTTP 服务
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):        # 静默访问日志
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _page(self):
        body = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._page()
        if self.path.startswith("/api/state"):
            fresh = ("fresh=1" in self.path)
            st = state_view(fresh=fresh)
            st["core_file"] = os.path.basename(CORE_PATH)
            return self._json(st)
        if self.path.startswith("/api/log"):
            m = re.search(r"after=(\d+)", self.path)
            after = int(m.group(1)) if m else 0
            with _LOG_LOCK:
                lines = [(s, ln) for s, ln in _LOG if s > after]
                nxt = _LOG_SEQ
            return self._json({"lines": lines, "next": nxt})
        if self.path == "/api/persona":
            return self._json({"text": _read_persona(), "path": PERSONA_FILE})
        if self.path == "/api/persona/default":
            try:
                return self._json({"text": core.DEFAULT_PERSONA})
            except Exception:
                return self._json({"text": ""})
        if self.path == "/api/env":
            if not _ENV["items"] and not _ENV["scanning"]:
                _env_scan_async()          # 首次访问才检测，省资源
            with _ENV_LOCK:
                return self._json({
                    "items": _ENV["items"],
                    "scanning": _ENV["scanning"],
                    "busy": _ENV["busy"],
                    "busy_label": _ENV["busy_label"],
                    "busy_key": _ENV["busy_key"],
                })
        if self.path == "/api/skills":
            installed = _scan_installed_skills()
            inst_names = {s["name"] for s in installed}
            rec = [{"name": n, "cat": c, "use": u, "how": h, "installed": n in inst_names}
                   for (n, c, u, h) in _SKILL_RECOMMEND]
            market = _scan_marketplace_skills(inst_names)
            return self._json({"recommended": rec, "installed": installed,
                               "market": market})
        return self._json({"err": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            req = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return self._json({"ok": False, "err": "请求体不是合法 JSON"}, 400)

        if self.path == "/api/skill/install":
            src = str(req.get("src") or "")
            if not src:
                return self._json({"ok": False, "err": "缺少 src"}, 400)
            ok, info = _install_marketplace_skill(src)
            if not ok:
                return self._json({"ok": False, "err": info}, 400)
            _push_log("[Skill] 安装成功：%s -> ~/.workbuddy/skills/" % info)
            return self._json({"ok": True, "name": info})

        if self.path == "/api/action":
            if not _CORE_READY.is_set():
                return self._json({"ok": False, "err": "核心还在加载，稍等两秒再试"}, 503)
            if _STATE["busy"]:
                return self._json({"ok": False, "err": "有动作正在进行，等它跑完"})
            act = req.get("action")
            targets = req.get("targets") or []
            targets = [t for t in targets if t in core.TARGETS]
            if not targets:
                return self._json({"ok": False, "err": "没有有效的目标"})
            mode = {"apply": "apply", "revert": "revert",
                    "dry-run": "dry-run", "force": "apply"}.get(act)
            if not mode:
                return self._json({"ok": False, "err": "未知动作:%s" % act})
            force = act == "force"
            _run_action_async(mode, targets, force)
            return self._json({"ok": True, "mode": mode, "targets": targets})

        if self.path == "/api/persona":
            if not _CORE_READY.is_set():
                return self._json({"ok": False, "err": "核心还在加载，稍等再保存"}, 503)
            text = str(req.get("text") or "")
            if not text.strip():
                return self._json({"ok": False, "err": "人格不能是空的"}, 400)
            try:
                _write_persona(text)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "err": str(e)}, 500)

        if self.path == "/api/persona/reset":
            if not _CORE_READY.is_set():
                return self._json({"ok": False, "err": "核心还在加载，稍等再试"}, 503)
            try:
                _write_persona(core.DEFAULT_PERSONA)
                return self._json({"ok": True, "text": core.DEFAULT_PERSONA})
            except Exception as e:
                return self._json({"ok": False, "err": str(e)}, 500)

        if self.path == "/api/env/scan":
            _env_scan_async()
            return self._json({"ok": True})

        if self.path == "/api/env/install":
            if _ENV["busy"]:
                return self._json({"ok": False, "err": "环境补全正在跑，等它结束"}, 409)
            keys = [k for k in (req.get("keys") or []) if k in _ENV_INSTALL]
            if not keys:
                return self._json({"ok": False, "err": "没有可处理的环境"})
            _env_install_async(keys)
            return self._json({"ok": True, "keys": keys})

        if self.path == "/api/log/clear":
            with _LOG_LOCK:
                _LOG.clear()
            return self._json({"ok": True})

        return self._json({"err": "not found"}, 404)


def main():
    import socket as _sock

    # v8.5：核心放到后台线程加载（封条自检 + 类初始化约 1-2s），窗口/网页先起，
    # 核心就绪前 API 返回「加载中」，就绪瞬间前端轮询自然拿到真数据。
    threading.Thread(target=_load_core, daemon=True, name="core-loader").start()

    def _port_busy(p):
        """有人真在听这个口吗？（SO_REUSEADDR 下 bind 成功不代表端口空闲）"""
        s = _sock.socket()
        s.settimeout(0.3)
        try:
            s.connect(("127.0.0.1", p))
            return True
        except Exception:
            return False
        finally:
            s.close()

    port = 8317
    srv = None
    for p in range(8317, 8337):
        if _port_busy(p):
            continue
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        sys.stderr.write("界面服务端口都被占用，起不来服务。\n")
        _fatal_box("界面服务被其它程序占用，界面起不来。\n\n"
                   "关掉其它正在运行的破甲 GUI 后再试。")
        raise SystemExit(1)
    url = "http://127.0.0.1:%d/" % port
    threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()
    threading.Thread(target=_snapshot_worker, daemon=True,
                     name="snapshot").start()
    # v8.6：正式 exe 不露本地端口——调试跑（非 frozen）才打印 UI 地址
    if not getattr(sys, "frozen", False):
        _push_log("UI 地址：%s" % url)

    # ---- 首选原生窗口（WebView2）；--browser 强制浏览器；--no-browser 无头（自动化用）
    headless = "--no-browser" in sys.argv
    force_browser = "--browser" in sys.argv
    _use_webview = (webview is not None and not force_browser and not headless)
    if _use_webview:
        # v8.6 显示修复：先守门。客户机缺 WebView2 运行时时，pywebview 的
        # WinForms 窗口照开但控件初始化失败且不抛异常 → 黑窗。必须在
        # create_window 之前检测/补装，补不上就走浏览器模式。
        if _ensure_webview2_or_browser(url) == "browser":
            try:
                webbrowser.open(url)
            except Exception:
                pass
            _push_log("已切换浏览器模式：%s" % url)
            _use_webview = False
    if _use_webview:
        # v8.6 显示修复②：远程桌面 / --nogpu 时关掉 WebView2 硬件加速
        # —— RDP 会话里 GPU 加速黑窗是常见病，自动规避。
        try:
            import ctypes as _ct
            if _ct.windll.user32.GetSystemMetrics(0x1000) or \
                    "--nogpu" in sys.argv:          # SM_REMOTESESSION
                gpu_arg = _ct.create_unicode_buffer(512)
                _k32 = _ct.windll.kernel32
                _n = _k32.GetEnvironmentVariableW(
                    "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", gpu_arg, 512)
                _cur = gpu_arg.value if _n else ""
                if "--disable-gpu" not in _cur:
                    _k32.SetEnvironmentVariableW(
                        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
                        (_cur + " --disable-gpu").strip())
                    _push_log("[*] 远程会话/--nogpu：已关闭 WebView2 硬件加速")
        except Exception:
            pass
        win = None
        try:
            try:                    # QQ/TG 链接丢给系统默认浏览器打开
                webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
            except Exception:
                pass

            class _JsApi:            # JS 侧可靠开外链：Python 出手，不受 WebView2
                def open_link(self, url):   # 弹窗拦截 / 协议白名单影响
                    try:
                        webbrowser.open(str(url))
                    except Exception:
                        pass
                    return True

            win = webview.create_window(
                "破甲一键通 v%s" % VERSION, url,
                width=1120, height=800, min_size=(960, 640),
                background_color="#141518", js_api=_JsApi())

            def _arm_reap(delay):
                """关闭已确认：无论 .NET 拆卸卡没卡，delay 秒后硬收尾。"""
                def _reap():
                    time.sleep(delay)
                    os._exit(0)
                threading.Thread(target=_reap, daemon=True).start()

            # ---- 原生兜底（v8.3）：上面那些都是 Python 线程，GIL 被 .NET 拆卸
            # 循环占死时一个都跑不动。这条走 OS 线程池定时器，回调指针直接
            # 指向 TerminateProcess、参数传当前进程伪句柄(-1) —— 定时器到期时
            # 在纯原生层把进程带走，完全不经 Python，GIL 卡死也能触发。
            _NATIVE_TIMERS = []

            def _arm_native_reap(delay_ms):
                try:
                    import ctypes
                    k32 = ctypes.windll.kernel32
                    cb_t = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_bool)
                    term = cb_t(ctypes.cast(k32.TerminateProcess,
                                            ctypes.c_void_p).value)
                    h = ctypes.c_void_p()
                    ok = k32.CreateTimerQueueTimer(
                        ctypes.byref(h), None, term,
                        ctypes.c_void_p(-1),          # 参数=当前进程伪句柄
                        ctypes.c_ulong(delay_ms),     # DueTime（毫秒）
                        0, 0)                         # 一次性
                    if ok:
                        _NATIVE_TIMERS.append((h, term))   # 防 GC 回收句柄
                except Exception:
                    pass                                     # 兜底失败也不影响主流程

            def _on_closing():
                if not _STATE["busy"]:
                    _arm_reap(2.5)
                    _arm_native_reap(3500)
                    return True
                import ctypes
                r = ctypes.windll.user32.MessageBoxW(
                    None,
                    "有动作正在进行，确定要退出吗？\n"
                    "（中途退出可能留下半破甲状态，重新破甲一次即可修复）",
                    "破甲一键通", 0x24)          # MB_ICONWARNING | YESNO
                if r == 6:                      # IDYES：用户确认退，武装硬退
                    _arm_reap(2.5)
                    _arm_native_reap(3500)
                    return True
                return False

            def _on_closed():
                _arm_reap(1.5)
                _arm_native_reap(2000)

            win.events.closing += _on_closing
            win.events.closed += _on_closed

            # 窗口消失看门狗：不依赖 pywebview 事件（拆卸偶尔卡死事件不触发）。
            # 窗口出现后一旦消失超过 2 秒进程还活着 → 直接收尾。
            def _watchdog(wtitle):
                import ctypes
                user32 = ctypes.windll.user32
                t0 = time.time()
                appeared = False
                while time.time() - t0 < 30.0:      # 最多等 30s 等窗口出现
                    h = user32.FindWindowW(None, wtitle)
                    if h:
                        appeared = True
                        _apply_window_icon(h)       # v8.5：窗口一出现就换图标
                        break
                    time.sleep(0.5)
                if not appeared:
                    return                          # 窗口没起来，交给异常兜底
                gone = 0.0
                while True:
                    time.sleep(0.5)
                    if user32.FindWindowW(None, wtitle):
                        gone = 0.0
                    else:
                        gone += 0.5
                        if gone >= 2.0:
                            _arm_native_reap(1500)   # 原生兜底再压一道
                            os._exit(0)
            threading.Thread(target=_watchdog,
                             args=("破甲一键通 v%s" % VERSION,),
                             daemon=True).start()

            if _UI_CACHE:
                webview.start(private_mode=False, storage_path=_UI_CACHE,
                              gui="edgechromium")   # v8.6：强制，缺失就抛→浏览器兜底
            else:
                webview.start(gui="edgechromium")  # 阻塞到窗口关闭；同上强制
            _push_log("窗口已关闭，退出。")
            try:
                srv.shutdown()
            except Exception:
                pass
            os._exit(0)                        # .NET 线程不肯 join，直接收尾
            return 0
        except Exception as e:
            _push_log("[!] 原生窗口起不来（%s），退回浏览器模式。" % e)
            try:
                webbrowser.open(url)
            except Exception:
                pass
    elif "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    if not getattr(sys, "frozen", False):       # v8.6：exe 不打本地地址
        sys.stdout.write("破甲一键通 GUI v%s  ->  %s  （Ctrl+C 退出）\n" % (VERSION, url))
        sys.stdout.flush()
    try:
        while True:
            time.sleep(3600)                   # 浏览器模式：主线程挂起等服务
    except KeyboardInterrupt:
        pass
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
