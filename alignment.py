"""Small dispatch contract shared by alignment, execution and acceptance."""
def validate(value):
    if not isinstance(value,dict):raise ValueError('alignment must be an object')
    allowed={'mode','goal','deliverables','acceptance','constraints','decisions','open_questions','basis'}
    if set(value)-allowed:raise ValueError('unknown alignment field')
    if value.get('mode') not in ('grill-me','existing-contract'):raise ValueError('invalid alignment mode')
    for key in ('goal','basis'):
        if not isinstance(value.get(key),str) or not value[key].strip():raise ValueError('missing alignment '+key)
    for key in ('deliverables','acceptance','constraints','decisions','open_questions'):
        items=value.get(key)
        if not isinstance(items,list) or any(not isinstance(x,str) or not x.strip() for x in items):raise ValueError('invalid alignment '+key)
    if not value['deliverables'] or not value['acceptance']:raise ValueError('alignment needs deliverables and acceptance')
    if value['open_questions']:raise ValueError('resolve alignment questions before dispatch')
    import json
    if len(json.dumps(value,ensure_ascii=False))>10000:raise ValueError('alignment is too large; keep a compact decision brief')
    return value
