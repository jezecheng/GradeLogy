# -*- coding: utf-8 -*-
"""节点 detect：按页识别每道题的作答区域（题号 + 页码 + bbox）。

POC：python -m utilities.detect --pdf uploads/test_stu.pdf [--pages 1] [--teacher 6]
"""
import argparse
import base64
import json
import os
import re
import secrets

from . import llm

DETECT_PROMPT = (
    "你是试卷切题助手。输入是学生手写作答的扫描页（可能多页）。\n"
    "请识别每页中每道题的作答区域，只输出 JSON，不要输出解释或代码块标记：\n"
    '{"questions":[{"no":"3.1","page":1,"bbox":[x1,y1,x2,y2]}]}\n'
    "要求：\n"
    "1. bbox 用 0-1000 归一化坐标（以该页左上角为原点），紧贴该题作答区，禁止跨题合并；\n"
    "2. no 用题面标注的题号，保留原样（如 3.1、例1.1、4(1)、选3）；\n"
    "3. page 必须填页面标签中给出的真实页码，禁止从 1 重新编号；\n"
    "4. 只输出题目位置，不要写答案、不要批改。")


# ---------- 图片工具 ----------
def pdf_to_pages(pdf_path, max_pages=12, prefix="page", subdir="", stem=""):
    """把 PDF 逐页渲染成 JPG，返回相对 uploads 的文件名列表；失败返回 []。

    subdir: 输出子目录（相对 uploads，如 attachments/课程/讲次），需已存在。
    stem:   指定后文件名为 {stem}_p{页码}.jpg（确定性命名，重跑覆盖）；
            否则沿用随机名 {prefix}_{随机}_p{页码}.jpg（POC 用完即删）。
    """
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf_path)
    out = []
    for i in range(min(len(doc), max_pages)):
        img = doc[i].render(scale=2.0).to_pil()
        name = f"{stem}_p{i + 1}.jpg" if stem else f"{prefix}_{secrets.token_hex(5)}_p{i + 1}.jpg"
        rel = os.path.join(subdir, name) if subdir else name
        img.convert("RGB").save(os.path.join(llm.UPLOAD_DIR, rel), quality=90)
        out.append(rel)
    return out


def crop_bbox(file_name, bbox, stem, subdir=""):
    """按 0-1000 归一化 bbox 裁剪图片，返回相对 uploads 的新文件名；失败返回空串。

    file_name 可自带子目录；stem 为确定性文件名（不含扩展名），重跑覆盖旧文件。
    """
    if not bbox or len(bbox) != 4:
        return ""
    src = os.path.join(llm.UPLOAD_DIR, file_name)
    if file_name.rsplit(".", 1)[-1].lower() not in ("png", "jpg", "jpeg", "webp"):
        return ""
    try:
        from PIL import Image
        img = Image.open(src).convert("RGB")
        w, h = img.size
        nums = [float(v) for v in bbox]
        scale = 1000.0 if max(nums) <= 1000 else 1.0
        x1, y1, x2, y2 = (nums[0] / scale * w, nums[1] / scale * h,
                          nums[2] / scale * w, nums[3] / scale * h)
        pad = 8
        box = (max(0, int(min(x1, x2)) - pad), max(0, int(min(y1, y2)) - pad),
               min(w, int(max(x1, x2)) + pad), min(h, int(max(y1, y2)) + pad))
        if box[2] - box[0] < 20 or box[3] - box[1] < 20:
            return ""
        out = f"{stem}.jpg"
        rel = os.path.join(subdir, out) if subdir else out
        img.crop(box).save(os.path.join(llm.UPLOAD_DIR, rel), quality=88)
        return rel
    except Exception:
        return ""


# ---------- 内部辅助（与 app.py 同源逻辑） ----------
def _as_int(value, default=1):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _bbox_area(bbox):
    try:
        nums = [float(v) for v in bbox]
    except (TypeError, ValueError):
        return 0.0
    if len(nums) != 4:
        return 0.0
    return abs((nums[2] - nums[0]) * (nums[3] - nums[1]))


def _natural_key(no):
    key = []
    for part in re.split(r"(\d+)", str(no)):
        if part == "":
            continue
        key.append((0, int(part), "") if part.isdigit() else (1, 0, part.lower()))
    return key


def _fix_page(page, offset, size):
    """模型会把每批页码从 1 重编，这里校正成全局真实页码。"""
    if page <= offset:
        page = offset + page
    if not offset < page <= offset + size:
        page = offset + 1
    return page


def _salvage_questions(text):
    """JSON 被截断时，按括号配对逐个抢救 questions 数组里已完整输出的题目对象。"""
    key = text.find('"questions"')
    if key < 0:
        return []
    arr = text.find("[", key)
    if arr < 0:
        return []
    out, depth, obj_at, in_str, esc = [], 0, -1, False, False
    for i in range(arr + 1, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                obj_at = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and obj_at >= 0:
                    try:
                        obj = json.loads(text[obj_at:i + 1])
                    except json.JSONDecodeError:
                        obj = None
                    if isinstance(obj, dict):
                        out.append(obj)
                    obj_at = -1
        elif ch == "]" and depth == 0:
            break
    return out


# ---------- 节点主函数 ----------
def detect_questions(pages, api_url, api_key, model, extra="", batch=4):
    """按页（批）识别题目位置。

    pages: uploads 目录下的整页图片文件名列表。
    返回 (questions, errors)，questions 为 [{no, page, bbox}, ...]。
    """
    prompt = DETECT_PROMPT + (f"\n补充要求：{extra}" if extra else "")
    detected = []
    errors = []
    for start in range(0, len(pages), batch):
        group = pages[start:start + batch]
        last = start + len(group)
        content = [{"type": "text", "text": (
            prompt +
            f"\n本次为学生作答第 {start + 1} 页至第 {last} 页，共 {len(group)} 页；"
            f"page 字段必须填这些真实页码（{start + 1}-{last}），不要从 1 重新编号。")}]
        for i, pg in enumerate(group):
            content.append({"type": "text", "text": f"学生作答第 {start + i + 1} 页："})
            with open(os.path.join(llm.UPLOAD_DIR, pg), "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        reply, finish, err = llm.chat(api_url, api_key, model, content, max_tokens=3000)
        span = f"第 {start + 1}-{last} 页"
        if err:
            errors.append(f"{span}切题失败：{err}")
            continue
        parsed = llm.extract_json(reply)
        raw = parsed.get("questions", []) if parsed else []
        if not raw and finish == "length":
            raw = _salvage_questions(reply)
        if not raw:
            errors.append(f"{span}：未识别到题目")
            continue
        for q in raw:
            if not isinstance(q, dict):
                continue
            detected.append({
                "no": str(q.get("no", "")).strip(),
                "page": _fix_page(_as_int(q.get("page"), start + 1), start, len(group)),
                "bbox": q.get("bbox"),
            })

    # 同页同题号去重，保留 bbox 面积更大的那条
    merged = {}
    for q in detected:
        key = (q["page"], q["no"])
        old = merged.get(key)
        if old is None or _bbox_area(q.get("bbox")) > _bbox_area(old.get("bbox")):
            merged[key] = q
    return sorted(merged.values(), key=lambda q: (q["page"], _natural_key(q["no"]))), errors


def main():
    ap = argparse.ArgumentParser(description="POC：识别试卷每道题的作答区域")
    ap.add_argument("--pdf", required=True, help="学生作答 PDF（项目根相对路径或绝对路径）")
    ap.add_argument("--pages", type=int, default=12, help="最多渲染页数（默认 12）")
    ap.add_argument("--batch", type=int, default=4, help="每批送模型的页数（默认 4）")
    ap.add_argument("--teacher", type=int, default=None, help="指定 teacher_id 的配置")
    args = ap.parse_args()
    cfg = llm.resolve_cli_config(args)

    pdf_path = args.pdf if os.path.isabs(args.pdf) else os.path.join(llm.BASE_DIR, args.pdf)
    pages = pdf_to_pages(pdf_path, max_pages=args.pages, prefix="poc_detect")
    if not pages:
        raise SystemExit("PDF 渲染失败")
    try:
        questions, errors = detect_questions(pages, cfg["api_url"], cfg["api_key"],
                                             cfg["model"], cfg["extra"], batch=args.batch)
    finally:
        for pg in pages:  # POC 渲染的整页图用完即删，不污染 uploads
            try:
                os.remove(os.path.join(llm.UPLOAD_DIR, pg))
            except OSError:
                pass
    print(json.dumps({"questions": questions, "errors": errors},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
