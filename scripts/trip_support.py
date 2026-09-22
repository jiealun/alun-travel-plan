"""Shared deterministic storage, validation and budget helpers (standard library)."""
import hashlib
import json
import os
import re
import sys
import tempfile


def configure_console():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')


def canonical_hash(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def inside(base, path):
    base = os.path.realpath(base)
    path = os.path.realpath(path)
    try:
        valid = os.path.normcase(os.path.commonpath([base, path])) == os.path.normcase(base)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('路径越出行程目录')
    return path


def atomic_text(path, content):
    parent = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix='.alun-', suffix='.tmp', dir=parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def artifact_path(base, path, source, kind):
    """An output must never replace source data, history, or another artifact kind."""
    target = inside(base, path)
    relative = os.path.relpath(target, os.path.realpath(base)).replace('\\', '/').lower()
    if (os.path.normcase(target) == os.path.normcase(os.path.realpath(source))
            or relative == 'trip.json' or relative.startswith('versions/')
            or relative == 'versions'):
        raise ValueError('产物路径不能覆盖行程数据或历史快照')
    name = os.path.basename(target).lower()
    expected = 'manifest.' if kind == 'manifest' else 'audit.'
    if not name.startswith(expected) or not name.endswith('.json'):
        raise ValueError(f'{kind} 文件名须以 {expected} 开头、以 .json 结尾')
    return target


def atomic_json(path, data):
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))


def secret_hits(obj):
    from validate_trip import SECRET_PATTERNS
    hits = []
    sensitive = re.compile(r'(?i)(?:^|_)(?:api_?key|access_?token|token|secret|password|passwd)$')
    def walk(node, path):
        if isinstance(node, str):
            if any(p.search(node) for p, _ in SECRET_PATTERNS):
                hits.append(path)
        elif isinstance(node, dict):
            for k, v in node.items():
                if sensitive.search(str(k)) and v:
                    hits.append(path + '.[凭据字段]')
                walk(v, path + '.' + str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f'{path}[{i}]')
    walk(obj, '$')
    return hits


def preflight(trip, semantic=False):
    """Reject before writing anything; errors never echo supplied secret values."""
    import validate_trip as vt
    if secret_hits(trip):
        raise ValueError('数据含疑似凭据，拒绝写入')
    with open(vt.SCHEMA_PATH, encoding='utf-8') as f:
        schema = json.load(f)
    ms = vt.MiniSchema(schema)
    if not ms.check(trip, schema, '$'):
        raise ValueError(f'数据契约校验失败（{len(ms.errors)} 处），请运行 validate_trip 查看字段位置')
    # JSON's NaN/Infinity extension is not valid trip data.
    try:
        canonical_hash(trip)
    except ValueError:
        raise ValueError('数据包含非有限数值') from None
    if semantic:
        a = vt.Audit(trip)
        vt.semantic_checks(trip, a)
        if a.summary()['blocking']:
            raise ValueError(f"行程有 {a.summary()['blocking']} 个阻断问题，请先运行 validate_trip 修复")


def checkpoint(base, trip):
    """Delivered snapshots are immutable, including when --force renders files."""
    preflight(trip)
    folder = inside(base, os.path.join(base, 'versions'))
    target = inside(base, os.path.join(folder, f"trip.v{trip['plan_version']}.json"))
    if os.path.exists(target):
        with open(target, encoding='utf-8') as f:
            saved = json.load(f)
        if canonical_hash(saved) != canonical_hash(trip):
            raise ValueError('同版本快照已有不同内容，请先 save 升版本；不能覆盖历史快照')
        return target
    os.makedirs(folder, exist_ok=True)
    atomic_json(target, trip)
    return target


def budget_limit(trip):
    b, req = trip['itinerary']['budget'], trip['request']['budget']
    if b.get('hard_limit') is not None:
        return b['hard_limit']
    if req.get('currency') != b['currency']:
        return None  # No implicit foreign exchange conversion.
    if req.get('mode') == 'total':
        return req.get('amount_max')
    # Per-person budgets require an explicit participant count; seniors may be
    # included in adults, and child pricing cannot be inferred from party data.
    count = req.get('budget_persons')
    if req.get('mode') == 'per_person' and count and req.get('amount_max') is not None:
        return count * req['amount_max']
    return None


def budget_verdict(lo, hi, paid, unknown, limit):
    if limit is None:
        return '预算口径或上限待确认'
    if paid + lo > limit:
        return '不成立'
    if paid + hi > limit:
        return '下界成立、上界超出'
    if unknown:
        return '已知费用未超限，含未知费用时尚不能确认'
    return '成立'
