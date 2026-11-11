# -*- coding: utf-8 -*-
"""学生名单 CSV 解析：只做「文件 → 结构化行 + 错误列表」，不碰数据库，便于独立 POC。

支持：
- 编码：UTF-8（含 Excel 导出的 BOM）/ GB18030（中文 Excel 默认）
- 表头：用户名/账号/学号/登录名、姓名/名字、密码/初始密码；无表头时按「用户名,姓名[,密码]」
- 校验：用户名 2-32 位字母数字及 _ . -；密码至少 6 位；文件内重复行报错

CLI：
    python -m utilities.roster --file students.csv
"""
import argparse
import csv
import io
import re

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,31}$")
DEFAULT_PASSWORD = "123456"
MAX_ROWS = 1000

USERNAME_HEADERS = {"用户名", "账号", "学号", "登录名", "username", "user", "account"}
NAME_HEADERS = {"姓名", "名字", "学生姓名", "name", "display_name"}
PASSWORD_HEADERS = {"密码", "初始密码", "password", "passwd"}


def _decode(raw):
    """按 UTF-8(BOM) → GB18030 依次尝试，都失败则报错。"""
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别文件编码，请另存为 UTF-8 或 GBK 的 CSV")


def _pick(header, names):
    """返回表头中第一个命中的列下标，未命中返回 None。"""
    for i, cell in enumerate(header):
        if (cell or "").strip().lower() in names:
            return i
    return None


def parse_csv(raw):
    """解析 CSV 字节流，返回 (rows, errors)。

    rows: [{"username": str, "name": str, "password": str, "line": int}]
    errors: ["第 3 行：用户名不合法", ...]
    整行为空自动跳过；超过 MAX_ROWS 直接报错。
    """
    text = _decode(raw)
    reader = csv.reader(io.StringIO(text))
    rows, errors = [], []
    seen = set()
    cols = None  # (username_idx, name_idx, password_idx)
    for line, cells in enumerate(reader, start=1):
        cells = [(c or "").strip() for c in cells]
        if not any(cells):
            continue
        if cols is None:
            u = _pick(cells, USERNAME_HEADERS)
            if u is not None:
                cols = (u, _pick(cells, NAME_HEADERS), _pick(cells, PASSWORD_HEADERS))
                continue
            cols = (0, 1, 2)
        if len(rows) >= MAX_ROWS:
            errors.append(f"超过单次导入上限 {MAX_ROWS} 行，请拆分文件")
            break

        def cell(idx):
            return cells[idx] if idx is not None and idx < len(cells) else ""

        username = cell(cols[0])
        name = cell(cols[1]) or username
        password = cell(cols[2]) or DEFAULT_PASSWORD
        if not username:
            errors.append(f"第 {line} 行：用户名为空")
            continue
        if not USERNAME_RE.match(username):
            errors.append(f"第 {line} 行：用户名 {username} 不合法（2-32 位字母、数字或 _ . -）")
            continue
        if password and len(password) < 6:
            errors.append(f"第 {line} 行：密码至少 6 位")
            continue
        key = username.lower()
        if key in seen:
            errors.append(f"第 {line} 行：用户名 {username} 在文件中重复")
            continue
        seen.add(key)
        rows.append({"username": username, "name": name,
                     "password": password, "line": line})
    if cols is None:
        errors.append("文件为空")
    return rows, errors


def main():
    ap = argparse.ArgumentParser(description="学生名单 CSV 解析预览（不写数据库）")
    ap.add_argument("--file", required=True, help="CSV 文件路径")
    args = ap.parse_args()
    with open(args.file, "rb") as f:
        rows, errors = parse_csv(f.read())
    print(f"解析出 {len(rows)} 行，{len(errors)} 条错误")
    for r in rows:
        print(f"  第 {r['line']} 行 | {r['username']} | {r['name']} | "
              f"{'默认密码' if r['password'] == DEFAULT_PASSWORD else '自定义密码'}")
    for e in errors:
        print("  !", e)
    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
