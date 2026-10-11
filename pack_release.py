# -*- coding: utf-8 -*-
"""把 dist/ 里打好的 exe 整理成 Release 附件（ASCII 名 + 一个保留中文名的 zip）。

为什么不能直接传中文名的 exe —— GitHub Release 附件会把文件名里的非 ASCII
字符剥掉：`破甲一键通GUI.exe` 传上去会变成 `GUI.exe`，用户下载下来完全看不出
是什么。所以这里统一复制出 ASCII 名的副本，另外打一个 zip，包内保留原来的
中文名，想要中文名的下 zip 即可。

用**复制**而不是改名：打包产物保留原中文名，脚本可以反复跑（幂等）。

产出（都落在 dist/）：
    pojia-gui-<ver>-win-x64.exe   图形界面版
    pojia-cli-<ver>-win-x64.exe   命令行版
    pojia-<ver>-win-x64.zip       上面两个的打包（包内为中文名）
    SHA256SUMS-exe.txt            上面三个的校验清单
    release-notes.md              给 gh release create --notes-file 用的说明

用法：
    python pack_release.py              # 整理 + 生成附件与说明
    python pack_release.py --list       # 只打印，不改文件
"""
import hashlib
import os
import shutil
import sys
import zipfile

REPO = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(REPO, "dist")

# (中文源名, ASCII 目标名模板, 说明)
TARGETS = [
    ("破甲一键通GUI.exe", "pojia-gui-%s-win-x64.exe", "图形界面版（推荐）"),
    ("破甲一键通.exe", "pojia-cli-%s-win-x64.exe", "命令行版"),
]
ZIP_TPL = "pojia-%s-win-x64.zip"
SUMS = "SHA256SUMS-exe.txt"
NOTES = "release-notes.md"


def version():
    """对外版本号 = GUI 的 DISPLAY_VER（与 release.py 保持同一口径）。"""
    gui = os.path.join(REPO, "破甲GUI.py")
    if os.path.isfile(gui):
        with open(gui, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("DISPLAY_VER"):
                    return line.split("=", 1)[1].split("#")[0].strip().strip('"')
    with open(os.path.join(REPO, "破甲一键通.py"), encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("VERSION = "):
                return line.split("=", 1)[1].strip().strip('"')
    return "?"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 16), b""):
            h.update(b)
    return h.hexdigest()


def main():
    ver = version()
    write = "--list" not in sys.argv
    print("版本:", ver)

    # 1) 先打 zip（包内保留中文名），再复制 ASCII 名副本 —— 顺序反了会找不到源文件
    pairs = []
    for src, tpl, desc in TARGETS:
        p = os.path.join(DIST, src)
        if not os.path.isfile(p):
            print("[!] 缺少 %s —— 先跑 build_exe.py / PyInstaller 打包" % src)
            return 1
        pairs.append((p, os.path.join(DIST, tpl % ver), desc))

    zp = os.path.join(DIST, ZIP_TPL % ver)
    if write:
        # "w" 模式会截断，不必先删 —— 沙箱里 os.remove 可能被安全策略拦掉
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for p, _d, _x in pairs:
                z.write(p, os.path.basename(p))

    # 2) ASCII 名副本
    assets = []
    for p, dst, desc in pairs:
        if write and os.path.abspath(p) != os.path.abspath(dst):
            shutil.copy2(p, dst)
        assets.append((dst, desc))
    assets.append((zp, "上面两个的打包（解压后是中文文件名）"))

    # 3) 校验清单 + Release 说明
    sump = os.path.join(DIST, SUMS)
    notep = os.path.join(DIST, NOTES)
    if write:
        rows = [(os.path.basename(a), sha256(a), os.path.getsize(a))
                for a, _d in assets if os.path.isfile(a)]
        with open(sump, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# 破甲一键通 v%s —— Release 附件 SHA256 校验清单\n\n" % ver)
            for name, h, sz in rows:
                fh.write("%s  %s   (%d 字节)\n" % (h, name, sz))
        desc_of = dict((os.path.basename(a), d) for a, d in assets)
        with open(notep, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("## 破甲一键通 v%s\n\n" % ver)
            fh.write("打开就用，不再弹任何东西。\n\n")
            fh.write("### 下载\n\n| 文件 | 说明 | 大小 |\n| --- | --- | --- |\n")
            for name, h, sz in rows:
                fh.write("| `%s` | %s | %.1f MB |\n" % (name, desc_of[name], sz / 1048576.0))
            fh.write("\n> GitHub 的 Release 附件会把文件名里的中文剥掉，"
                     "所以 exe 用了 ASCII 名；想要中文名的下 zip。\n\n")
            fh.write("### SHA256\n\n```\n")
            for name, h, _sz in rows:
                fh.write("%s  %s\n" % (h, name))
            fh.write("```\n")

    print("\n--- Release 附件 ---")
    for a, desc in assets:
        if os.path.isfile(a):
            print("  %-30s %10d 字节  %s" % (os.path.basename(a), os.path.getsize(a), desc))
        else:
            print("  %-30s 缺失！" % os.path.basename(a))
    return 0


if __name__ == "__main__":
    sys.exit(main())
