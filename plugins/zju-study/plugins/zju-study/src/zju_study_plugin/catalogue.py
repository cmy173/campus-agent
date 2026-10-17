"""Named tool allowlist; request bodies are validated by the versioned backend API."""

def field(kind='string', where='query', required=False, description='', default=None, enum=None):
    return dict(type=kind, where=where, required=required, description=description, default=default, enum=enum)


def ident(name):
    return {name: field(where='path', required=True, description='Opaque ID returned by a previous tool')}


def payload():
    return {'request': field('object', 'body', True, 'JSON request matching the corresponding REST endpoint; see bundled OpenAPI')}


def paged():
    return {'cursor': field(description='Opaque next_cursor from previous page'), 'limit': field('integer', default=50, description='Page size, 1 to 200')}


def spec(name, description, method, path, fields=None, ai=False):
    return dict(name=name, description=description, method=method, path=path, fields=fields or {}, ai=ai)


TOOLS = [
 spec('study_import_file', 'Import one explicitly selected local PDF/PPT/PPTX/DOCX/ZIP into a course. Local transfer only; does not send it to AI. purpose=reference marks past-paper reference.', 'POST', '/imports', {
     'course_id':field(where='upload',required=True), 'file_path':field(where='upload',required=True,description='Absolute path of the file explicitly selected by the user'),
     'title':field(where='upload'), 'purpose':field(where='upload',default='material',enum=['material','reference']), 'textbook_id':field(where='upload'), 'idempotency_key':field(where='header')}),
 spec('study_status', 'Check whether the local study backend is ready.', 'GET', '/health'),
 spec('study_connections', 'List platform connection and login status; no credentials returned.', 'GET', '/connections'),
 spec('study_check_connection', 'Recheck an existing school login. Human authentication may be required.', 'POST', '/connections/{connection_id}/check', ident('connection_id')),
 spec('study_courses', 'List local courses; follow next_cursor until null.', 'GET', '/courses', paged()),
 spec('study_discover_courses', 'Read courses from the connected school platform.', 'GET', '/connections/{connection_id}/courses', {**ident('connection_id'), 'cursor':field(), 'term':field()}),
 spec('study_import_courses', 'Attach selected discovered courses to the local library. Does not enroll or modify school courses.', 'POST', '/connections/{connection_id}/courses/import', {**ident('connection_id'), **payload()}),
 spec('study_sync', 'Start asynchronous sync of bound courses; return a job immediately. School data is read only.', 'POST', '/connections/{connection_id}/sync', {**ident('connection_id'), 'request':field('object','body',False,'Optional {full:false}',default={}), 'idempotency_key':field(where='header',description='Reuse the same 8-128 character key when retrying a request')}),
 spec('study_resources', 'List course documents and parsing state.', 'GET', '/courses/{course_id}/resources', {**ident('course_id'), **paged()}),
 spec('study_chunks', 'Read parsed document text with provenance and page locators.', 'GET', '/resources/{resource_id}/chunks', {**ident('resource_id'), **paged()}),
 spec('study_assignments', 'Read course homework and due dates.', 'GET', '/courses/{course_id}/assignments', {**ident('course_id'), **paged()}),
 spec('study_recordings', 'List synchronized classroom sessions for a course.', 'GET', '/courses/{course_id}/recordings', {**ident('course_id'), **paged()}),
 spec('study_transcript', 'Read existing platform speech-recognition text for one synchronized session; this does not re-transcribe audio.', 'GET', '/recordings/{recording_id}/transcript', ident('recording_id')),
 spec('study_classroom_sessions', 'List own synchronized past classroom sessions, including course and teacher.', 'GET', '/classroom/recordings'),
 spec('study_summarize_class', 'Send user-reviewed transcript to the selected configured AI API. Costs API usage. request={provider_id, detail:brief|detailed, transcript}; returns job, not final summary.', 'POST', '/recordings/{recording_id}/summary', {**ident('recording_id'), **payload()}, ai=True),
 spec('study_class_summaries', 'Read saved classroom summaries and jobs, newest first.', 'GET', '/classroom/summaries'),
 spec('study_providers', 'List configured AI service IDs and models; no API keys are returned.', 'GET', '/ai/providers', paged()),
 spec('study_job', 'Poll asynchronous task status. Only succeeded means complete; inspect errors and warnings.', 'GET', '/jobs/{job_id}', ident('job_id')),
 spec('study_cancel_job', 'Request cancellation of a local background job.', 'POST', '/jobs/{job_id}/cancel', ident('job_id')),
 spec('study_retry_job', 'Retry a failed local job. AI generation jobs may incur API usage again.', 'POST', '/jobs/{job_id}/retry', {**ident('job_id'), 'idempotency_key':field(where='header')}, ai=True),
 spec('study_tasks', 'List learning tasks, optionally scoped to a course.', 'GET', '/tasks', {**paged(), 'course_id':field()}),
 spec('study_build_tasks', 'Build local learning tasks from parsed materials. Optional request={course_id}.', 'POST', '/tasks/build', {'request':field('object','body',default={})}),
 spec('study_task_feedback', 'Record local learning progress using FeedbackRequest; does not submit homework.', 'POST', '/tasks/{task_id}/feedback', {**ident('task_id'), **payload()}),
 spec('study_availability', 'Read study availability and timezone.', 'GET', '/preferences/availability'),
 spec('study_set_availability', 'Save local study availability using the REST availability schema.', 'PUT', '/preferences/availability', payload()),
 spec('study_preview_plan', 'Create a candidate study schedule; does not activate it. request may contain horizon_start, horizon_end, respect_locked.', 'POST', '/plans/preview', {'request':field('object','body',default={})}),
 spec('study_current_plan', 'Read the active local learning schedule.', 'GET', '/plans/current'),
 spec('study_apply_plan', 'Activate a previously previewed local plan after the user selects it.', 'POST', '/plans/{plan_id}/apply', ident('plan_id')),
 spec('study_review_materials', 'List available PPT, assignments and review-paper reference materials.', 'GET', '/courses/{course_id}/review-materials', {**ident('course_id'), **paged()}),
 spec('study_review_preflight', 'Validate a review-paper request without calling an AI model.', 'POST', '/review-papers/preflight', payload()),
 spec('study_generate_review', 'Generate a review paper using the selected AI API. Costs API usage; request follows ReviewRequest in OpenAPI. Returns asynchronous job; never claim completed before job succeeds.', 'POST', '/review-papers/generate', {**payload(), 'idempotency_key':field(where='header')}, ai=True),
 spec('study_revise_review', 'Revise a generated paper using the REST ReviewRevisionRequest schema; costs AI API usage.', 'POST', '/review-papers/{paper_id}/revisions', {**ident('paper_id'), **payload(), 'idempotency_key':field(where='header')}, ai=True),
 spec('study_review_papers', 'List saved review papers.', 'GET', '/review-papers', {**paged(), 'course_id':field()}),
 spec('study_review_paper', 'Read one generated review paper.', 'GET', '/review-papers/{paper_id}', ident('paper_id')),
 spec('study_export_review', 'Read a generated paper as Markdown or student/answer text.', 'GET', '/review-papers/{paper_id}/export', {**ident('paper_id'), 'format':field(default='markdown',enum=['markdown','student','answers'])}),
 spec('study_textbooks', 'List confirmed and candidate textbooks for one course.', 'GET', '/courses/{course_id}/textbooks', ident('course_id')),
 spec('study_save_textbook', 'Save textbook identity and file association; confirm exact edition before exercise matching. Uses BookInput schema.', 'POST', '/courses/{course_id}/textbooks', {**ident('course_id'), **payload()}),
 spec('study_index_textbook', 'Find exercise candidates in an already parsed local book. Candidates require checking.', 'POST', '/textbooks/{textbook_id}/auto-index', ident('textbook_id')),
 spec('study_exercises', 'Read numbered textbook exercise candidates and confirmation state.', 'GET', '/textbooks/{textbook_id}/exercises', ident('textbook_id')),
 spec('study_confirm_exercise', 'Save a user-checked exercise statement and edition association using ExerciseInput.', 'PUT', '/textbooks/{textbook_id}/exercises', {**ident('textbook_id'), **payload()}),
]
