"""Trusted native readers for immutable, published personal learning snapshots.

No model, writer, arbitrary executable or arbitrary source path is exposed.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any
from study_paths import subject_root

ROOTS = {s: subject_root(s)
         for s in ('math', 'english', '408')}
MODULES = {'math': ('错题知识网络/scripts', 'study_personalization_math'),
           '408': ('scripts', 'study_personalization_408')}
PYTHON = sys.executable
RUNNER = '''
import importlib,json,sys
from pathlib import Path
request=json.load(sys.stdin)
repo=Path(sys.argv[1])
if request['subject']=='english':
    sys.path.insert(0,str(repo))
    from english_pipeline.learning_state import formal_versions
    from english_pipeline.util import object_sha256
    hashes=formal_versions(repo)
    result={'status':'ready','formal_version':object_sha256(hashes),'formal_source_hashes':hashes}
    try:
        from english_pipeline.writing_publication import summary_identity
        result['personal_summary']=summary_identity(repo/'intake')
    except (OSError, ImportError, ValueError):
        result['personal_summary']={'status':'unknown'}
else:
    sys.path.insert(0,str(repo/sys.argv[2]))
    native=importlib.import_module(sys.argv[3])
    if request['operation']=='identity':
        result=native.current_identity(repo)
    elif request['operation']=='plan':
        planner=getattr(native,'plan_snapshot',None)
        options=({'view':request.get('view','protected'),
                  **{k:request[k] for k in ('exam_question_id','current_question_id','current_question_part','formal_id','source_id','personal_scope','materials','known_version') if k in request}}
                 if request['subject']=='408' else {})
        result=(planner(Path(request['manifest_path']),request['knowledge_points'],request['weak_knowledge_points'],**options)
                if planner else {'layout':'legacy','files':[]})
    else:
        result=native.query_snapshot(Path(request['manifest_path']),
            request['knowledge_points'],request['weak_knowledge_points'],
            request['view'],request['max_bytes'],request['cursor'],
            **({k:request[k] for k in ('exam_question_id','current_question_id','current_question_part','formal_id','source_id','personal_scope','materials','known_version') if k in request}
               if request['subject']=='408' else {}))
json.dump(result,sys.stdout,ensure_ascii=False)
'''


def native_call(subject: str, operation: str, **kwargs: Any) -> dict[str, Any]:
    if subject not in ROOTS or operation not in {'identity', 'query', 'plan'}:
        raise ValueError('unsupported personalization operation')
    if subject == 'english' and operation != 'identity':
        raise ValueError('English has no new knowledge-point personalization adapter')
    directory, name = MODULES.get(subject, ('', ''))
    completed = subprocess.run([PYTHON, '-c', RUNNER, str(ROOTS[subject]), directory, name],
        input=json.dumps({'subject': subject, 'operation': operation, **kwargs}, ensure_ascii=False),
        text=True, capture_output=True, timeout=15)
    if completed.returncode != 0:
        raise ValueError('native personal reader unavailable: ' + completed.stderr[-1200:])
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise ValueError('native personal reader did not return an object')
    return value


def compare_identity(published: dict, current: dict, *, personalized: bool) -> dict:
    """Unknown is never silently promoted to current."""
    pv, cv = published.get('formal_version'), current.get('formal_version')
    personal_old, personal_new = published.get('version'), current.get('version')
    status = 'unknown'
    reason = 'published_identity_unavailable'
    if current.get('status') not in {'ready', 'noop', 'available', 'current'}:
        status, reason = 'pending', 'native_context_not_ready'
    elif pv and cv:
        if pv != cv:
            status, reason = 'stale', 'formal_version_changed'
        elif personalized and not (personal_old and personal_new):
            status, reason = 'missing', 'personalization_not_published'
        elif personalized and personal_old != personal_new:
            status, reason = 'stale', 'personalization_version_changed'
        else:
            status, reason = 'current', 'versions_match'
    return {'status': status, 'reason': reason,
            'published_formal_version': pv, 'current_formal_version': cv,
            'published_personalization_version': personal_old,
            'current_personalization_version': personal_new,
            'native_status': current.get('status'),
            'scope': 'committed formal and personal context; pending quick packages are not formal history'}


def english_summary_freshness(published: dict, current: dict) -> dict:
    old, new = published.get('version'), current.get('version')
    if not old:
        status = 'not_published'
    elif current.get('status') != 'available' or not new:
        status = 'pending' if current.get('status') == 'pending' else 'unknown'
    else:
        status = 'current' if old == new else 'stale'
    return dict(personal_summary_freshness=status, published_personalization_version=old,
                current_personalization_version=new, projection_sha256=published.get('projection_sha256'),
                memory_generation=published.get('memory_generation'))
