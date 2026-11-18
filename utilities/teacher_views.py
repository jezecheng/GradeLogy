"""教师页面的只读待办汇总；调用方先限定可访问的课程。"""


def workload(db, course_ids, by_lecture=False):
    ids = list(course_ids)
    if not ids:
        return {}
    marks = ','.join('?' for _ in ids)
    rows = db.execute(f"""
        SELECT l.course_id,l.id AS lecture_id,s.status,
          COALESCE(q.total,0) AS total,COALESCE(q.reviewed,0) AS reviewed,
          EXISTS(SELECT 1 FROM ai_operations op WHERE op.submission_id=s.id
            AND op.operation_type='grade' AND op.status IN ('queued','running')) AS active
        FROM submissions s JOIN lectures l ON l.id=s.lecture_id
        JOIN users u ON u.id=s.student_id AND u.active=1
        JOIN enrollments e ON e.course_id=l.course_id AND e.student_id=s.student_id
        LEFT JOIN (SELECT submission_id,COUNT(*) total,SUM(reviewed<>0) reviewed
                   FROM questions GROUP BY submission_id) q ON q.submission_id=s.id
        WHERE l.course_id IN ({marks})""", ids).fetchall()
    result = {}
    for row in rows:
        key = row['lecture_id'] if by_lecture else row['course_id']
        item = result.setdefault(key, {'pending': 0, 'review': 0, 'running': 0})
        if row['active']:
            item['running'] += 1
        elif not row['total']:
            item['pending'] += 1
        elif row['reviewed'] < row['total'] or row['status'] != '已批改':
            item['review'] += 1
    return result
