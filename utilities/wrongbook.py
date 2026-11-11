# -*- coding: utf-8 -*-
"""错题本查询（纯函数 + CLI，不依赖 Flask）。

约定：
- 错题 = 提交状态为「已批改」且判定为「错」「半对」的题目；
- 课程汇总按角色区分：teacher 看自己创建的课程，student 看自己选课的课程；
- 状态：'' 视为「待复习」，也可显式存「已掌握」。

说明：本模块是查询工具，不是批改流水线节点，因此不登记到 manifest.json。

CLI 预览：
    python -m utilities.wrongbook --teacher 1
    python -m utilities.wrongbook --course 1 --status pending
    python -m utilities.wrongbook --course 1 --student 3
"""
import argparse
import sqlite3

import config

WRONG_VERDICTS = ("错", "半对")
PENDING = "待复习"
MASTERED = "已掌握"
_STATUS_FILTER = {"pending": PENDING, "mastered": MASTERED}

_BASE_FROM = """
    FROM questions q
    JOIN submissions s ON s.id=q.submission_id
    JOIN homeworks h ON h.id=s.homework_id
    JOIN courses c ON c.id=h.course_id
    JOIN users u ON u.id=s.student_id
"""


def _connect(db_path=None):
    con = sqlite3.connect(db_path or config.DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def course_summary(role, user_id, db_path=None):
    """课程级汇总。返回 [{id, name, n_wrong, n_pending, n_students|n_homeworks}]。"""
    if role == "teacher":
        sql = """
            SELECT c.id, c.name,
                   COUNT(q.id) AS n_wrong,
                   COUNT(DISTINCT CASE WHEN q.id IS NOT NULL
                                  AND COALESCE(NULLIF(q.wrong_status,''),'待复习')='待复习'
                                  THEN q.id END) AS n_pending,
                   COUNT(DISTINCT CASE WHEN q.id IS NOT NULL THEN s.student_id END) AS n_students
            FROM courses c
            LEFT JOIN homeworks h ON h.course_id=c.id
            LEFT JOIN submissions s ON s.homework_id=h.id AND s.status='已批改'
            LEFT JOIN questions q ON q.submission_id=s.id AND q.verdict IN ('错','半对')
            WHERE c.teacher_id=?
            GROUP BY c.id ORDER BY c.id DESC"""
        args = (user_id,)
    else:
        sql = """
            SELECT c.id, c.name,
                   COUNT(q.id) AS n_wrong,
                   COUNT(DISTINCT CASE WHEN q.id IS NOT NULL
                                  AND COALESCE(NULLIF(q.wrong_status,''),'待复习')='待复习'
                                  THEN q.id END) AS n_pending,
                   COUNT(DISTINCT CASE WHEN q.id IS NOT NULL THEN h.id END) AS n_homeworks
            FROM courses c
            JOIN enrollments e ON e.course_id=c.id AND e.student_id=?
            LEFT JOIN homeworks h ON h.course_id=c.id
            LEFT JOIN submissions s ON s.homework_id=h.id AND s.student_id=? AND s.status='已批改'
            LEFT JOIN questions q ON q.submission_id=s.id AND q.verdict IN ('错','半对')
            GROUP BY c.id ORDER BY c.id DESC"""
        args = (user_id, user_id)
    con = _connect(db_path)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def list_students(course_id, db_path=None):
    """课程内每个学生的错题数（老师视图的筛选条）。"""
    con = _connect(db_path)
    try:
        return con.execute("""
            SELECT u.id, u.display_name, COUNT(*) AS n
            FROM questions q
            JOIN submissions s ON s.id=q.submission_id
            JOIN homeworks h ON h.id=s.homework_id
            JOIN users u ON u.id=s.student_id
            WHERE h.course_id=? AND s.status='已批改' AND q.verdict IN ('错','半对')
            GROUP BY u.id ORDER BY u.display_name""", (course_id,)).fetchall()
    finally:
        con.close()


def collect(course_id, status="", student_id=None, db_path=None):
    """课程内错题明细；student_id 为空表示不过滤学生（仅供老师端使用，调用方负责鉴权）。"""
    where = ["c.id=?", "s.status='已批改'", "q.verdict IN ('错','半对')"]
    args = [course_id]
    if status in _STATUS_FILTER:
        where.append("COALESCE(NULLIF(q.wrong_status,''),'待复习')=?")
        args.append(_STATUS_FILTER[status])
    if student_id:
        where.append("s.student_id=?")
        args.append(student_id)
    sql = f"""
        SELECT q.id, q.no, q.verdict, q.score, q.max_score, q.note, q.ai_comment,
               q.ocr_text, q.standard_answer, q.image_path, q.page_no,
               q.wrong_status, q.review_count, q.reviewed_at,
               s.id AS sid, h.id AS hid, h.title AS hw_title,
               c.name AS course_name, u.display_name AS student_name
        {_BASE_FROM}
        WHERE {' AND '.join(where)}
        ORDER BY u.display_name, h.id DESC, q.page_no, q.id"""
    con = _connect(db_path)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _print_overview(role, user_id):
    rows = course_summary(role, user_id)
    if not rows:
        print("没有课程。")
        return
    for r in rows:
        extra = f"{r['n_students']} 人" if role == "teacher" else f"{r['n_homeworks']} 份作业"
        print(f"[{r['id']}] {r['name']}  待复习 {r['n_pending']} / 全部 {r['n_wrong']}  ({extra})")


def main():
    ap = argparse.ArgumentParser(description="错题本查询（只读）")
    ap.add_argument("--teacher", type=int, help="老师视角：列出其课程的错题汇总")
    ap.add_argument("--student-user", type=int, help="学生视角：列出其课程的错题汇总")
    ap.add_argument("--course", type=int, help="课程 ID，输出该课程错题明细")
    ap.add_argument("--student", type=int, help="只看某个学生（老师视角）")
    ap.add_argument("--status", choices=["pending", "mastered"], help="按状态过滤")
    args = ap.parse_args()

    if args.teacher:
        _print_overview("teacher", args.teacher)
    elif args.student_user:
        _print_overview("student", args.student_user)
    elif args.course:
        rows = collect(args.course, status=args.status or "", student_id=args.student)
        if not rows:
            print("没有符合条件的错题。")
            return
        for r in rows:
            state = r["wrong_status"] or PENDING
            print(f"{r['student_name']} | {r['hw_title']} | 第 {r['no']} 题 | {r['verdict']} "
                  f"| {r['score']}/{r['max_score']:g} | {state} | 复习 {r['review_count']} 次")
            if r["note"] or r["ai_comment"]:
                print(f"    评语：{r['note'] or r['ai_comment']}")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
