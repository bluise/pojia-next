# -*- coding: utf-8 -*-
"""重算 SHA256SUMS.txt：沿用旧清单的文件顺序，缺的文件剔除，版本号更新到当前。
（上游 v8.16 已经把这份清单删了 —— 因为他们不再分发源码；本 fork 源码照发，
  所以这份清单必须保留，而且要跟着核心 8.2 一起更新。）
"""
import hashlib
import io
import os
import re

REPO = os.path.dirname(os.path.abspath(__file__))
SUMS = os.path.join(REPO, "SHA256SUMS.txt")

HEADER = """# 破甲一键通 v%s —— SHA256 校验清单
# 校验方法（Windows PowerShell）:
#   Get-FileHash .\\破甲一键通.py -Algorithm SHA256
# 校验方法（Linux/macOS）:
#   sha256sum -c SHA256SUMS.txt
"""


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 16), b""):
            h.update(b)
    return h.hexdigest()


def main():
    # 1) 从旧清单里扒出文件顺序（跳过注释行）
    old = io.open(SUMS, encoding="utf-8").read().split("\n")
    files, missing = [], []
    for ln in old:
        ln = ln.rstrip()
        if not ln or ln.lstrip().startswith("#"):
            continue
        name = ln.split("  ", 1)[1] if "  " in ln else ln.split(None, 1)[1]
        name = name.strip()
        if os.path.exists(os.path.join(REPO, name)):
            files.append(name)
        else:
            missing.append(name)

    # 2) 版本号取 GUI 的显示版本，跟 Release tag 保持一致
    gui = io.open(os.path.join(REPO, "破甲GUI.py"), encoding="utf-8").read()
    m = re.search(r'^DISPLAY_VER\s*=\s*"([^"]+)"', gui, re.M)
    ver = m.group(1) if m else "?"

    # HEADER 自带结尾换行，这里 rstrip 掉再靠 join 补：否则会多出一个空行，
    # sha256sum -c 会多报一行 "improperly formatted"。
    out = [(HEADER % ver).rstrip("\n"), ""]
    for name in files:
        out.append("%s  %s" % (sha256(os.path.join(REPO, name)), name))

    io.open(SUMS, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
    print("[✓] SHA256SUMS.txt 已重算：版本 v%s，%d 个文件" % (ver, len(files)))
    if missing:
        print("    仓库里已不存在，从清单剔除：")
        for n in missing:
            print("      -", n)


if __name__ == "__main__":
    main()
