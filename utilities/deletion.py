"""教学记录删除规则；调用方拥有事务，原始附件和用量审计保留。"""


def delete_submissions(db, where, params):
    """where 仅来自内部固定 SQL，不接收用户 SQL。"""
    selection = f"SELECT id FROM submissions WHERE {where}"
    questions = f"SELECT id FROM questions WHERE submission_id IN ({selection})"
    db.execute(f"DELETE FROM student_question_reviews WHERE question_id IN ({questions})", params)
    db.execute(f"DELETE FROM student_weak_points WHERE source_submission_id IN ({selection})", params)
    nq = db.execute(f"DELETE FROM questions WHERE submission_id IN ({selection})", params).rowcount
    ns = db.execute(f"DELETE FROM submissions WHERE {where}", params).rowcount
    return ns, nq


def delete_lecture(db, lecture_id):
    delete_submissions(db, "lecture_id=?", (lecture_id,))
    db.execute("DELETE FROM question_tags WHERE lecture_id=?", (lecture_id,))
    db.execute("DELETE FROM question_assessments WHERE lecture_id=?", (lecture_id,))
    db.execute("DELETE FROM tag_analyses WHERE lecture_id=?", (lecture_id,))
    db.execute("DELETE FROM lectures WHERE id=?", (lecture_id,))


def delete_course(db, course_id):
    for row in db.execute("SELECT id FROM lectures WHERE course_id=?", (course_id,)).fetchall():
        delete_lecture(db, row[0])
    db.execute("DELETE FROM exam_scores WHERE exam_id IN (SELECT id FROM exams WHERE course_id=?)", (course_id,))
    for table in ("exams", "enrollments", "lesson_plans", "course_teachers",
                  "course_knowledge_graphs", "student_weak_points", "tag_analyses"):
        db.execute(f"DELETE FROM {table} WHERE course_id=?", (course_id,))
    db.execute("DELETE FROM courses WHERE id=?", (course_id,))


def unenroll(db, course_id, student_id):
    ns, nq = delete_submissions(db,
        "student_id=? AND lecture_id IN (SELECT id FROM lectures WHERE course_id=?)",
        (student_id, course_id))
    ne = db.execute("DELETE FROM exam_scores WHERE student_id=? AND exam_id IN "
                    "(SELECT id FROM exams WHERE course_id=?)", (student_id, course_id)).rowcount
    for table in ("student_weak_points", "tag_analyses", "enrollments"):
        db.execute(f"DELETE FROM {table} WHERE course_id=? AND student_id=?", (course_id, student_id))
    return ns, nq, ne


def delete_user(db, user_id, administrator_id):
    delete_submissions(db, "student_id=?", (user_id,))
    db.execute("DELETE FROM course_teachers WHERE teacher_id=?", (user_id,))
    for course in db.execute("SELECT id FROM courses WHERE teacher_id=?", (user_id,)).fetchall():
        heir = db.execute("SELECT teacher_id FROM course_teachers WHERE course_id=? ORDER BY teacher_id LIMIT 1",
                          (course[0],)).fetchone()
        if heir:
            db.execute("UPDATE courses SET teacher_id=? WHERE id=?", (heir[0], course[0]))
        else:
            delete_course(db, course[0])
    for table in ("enrollments", "exam_scores", "student_question_reviews", "student_weak_points"):
        db.execute(f"DELETE FROM {table} WHERE student_id=?", (user_id,))
    db.execute("DELETE FROM tag_analyses WHERE student_id=?", (user_id,))
    for table in ("settings", "ai_api_keys", "model_sync", "model_sync_state"):
        db.execute(f"DELETE FROM {table} WHERE teacher_id=?", (user_id,))
    for column in ("grade_prompt_id", "report_prompt_id", "tag_horizontal_prompt_id", "tag_vertical_prompt_id"):
        db.execute(f"UPDATE courses SET {column}=0 WHERE {column} IN "
                   "(SELECT id FROM prompts WHERE teacher_id=? AND is_default=0)", (user_id,))
    db.execute("DELETE FROM prompts WHERE teacher_id=? AND is_default=0", (user_id,))
    db.execute("DELETE FROM kg_members WHERE user_id=?", (user_id,))
    db.execute("UPDATE knowledge_graphs SET created_by=? WHERE created_by=?", (administrator_id, user_id))
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
