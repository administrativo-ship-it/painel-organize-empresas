#!/usr/bin/env python3
"""
Sincroniza FlowUp -> flowup-data.json via API REST direta (sem MCP).
OAuth2 Password Grant em https://task.flowup.me.

Estratégia DEFINITIVA (cobertura máxima):
1. Busca geral via /task/querytasks (ShowFinished:true, ShowArchived:false)
   - Pagina com PageSize=200 até esgotar
2. Para CADA projectId de 1 a MAX_PROJECT_ID (varredura), faz query específica
   por Projects:[pid] — captura projetos que não aparecem na busca geral
3. Para CADA usuário ativo, faz query por Users:[uid] — captura tarefas
   atribuídas a usuários ocultos da busca geral
4. Mescla tudo pelo Id único (deduplicação)
5. Inclui também tarefas ARQUIVADAS para projetos antigos
6. Deriva projetos das próprias tarefas (cada task carrega ProjectId+ProjectName)
"""
import os, sys, json, time, urllib.request, urllib.parse, urllib.error

API_KEY = os.environ.get('FLOWUP_API_KEY', '').strip()
SUBDOMAIN = os.environ.get('FLOWUP_SUBDOMAIN', 'organizementoring').strip()
BASE_URL = os.environ.get('FLOWUP_BASE_URL', 'https://task.flowup.me').rstrip('/')

if not API_KEY:
    print('ERRO: defina FLOWUP_API_KEY', file=sys.stderr); sys.exit(1)

EP_TOKEN = '/token'
EP_QUERY_TASKS = '/api/v1/public/task/querytasks'
EP_LIST_USERS = '/api/v1/public/user/getactiveusers'

PAGE_SIZE = 200
MAX_PAGES = 30
MAX_PROJECT_ID = 40   # FlowUp mostra ate id ~28; varremos com folga
MAX_USER_ID = 30      # Cobre usuarios ativos + inativos

_token, _exp = None, 0


def get_token():
    global _token, _exp
    if _token and _exp > time.time() + 60: return _token
    body = urllib.parse.urlencode({
        'password': API_KEY, 'grant_type': 'password',
        'scope': 'api', 'subdomain': SUBDOMAIN
    }).encode('utf-8')
    req = urllib.request.Request(
        f'{BASE_URL}{EP_TOKEN}', data=body, method='POST',
        headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode('utf-8'))
    _token = d.get('access_token')
    if not _token: raise RuntimeError(f'Sem token: {d}')
    _exp = time.time() + int(d.get('expires_in', 3600))
    return _token


def api(method, path, body=None):
    tk = get_token()
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(
        f'{BASE_URL}{path}', data=data, method=method,
        headers={'Authorization': f'Bearer {tk}', 'Content-Type': 'application/json', 'Accept': 'application/json'}
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        err = e.read().decode('utf-8', errors='ignore')
        raise RuntimeError(f'HTTP {e.code} {method} {path}: {err[:300]}')


def paginate_query(filter_obj, max_pages=MAX_PAGES, label=''):
    """Pagina querytasks com filtro custom. Retorna (tasks, total_count_reported)."""
    out = []
    total = None
    for page in range(1, max_pages + 1):
        try:
            resp = api('POST', EP_QUERY_TASKS, {
                'Filter': filter_obj, 'CurrentPage': page, 'PageSize': PAGE_SIZE
            })
        except Exception as e:
            print(f'    {label} p{page}: ERRO {e}')
            break
        chunk = resp.get('Result') or []
        if total is None:
            total = resp.get('Count', 0)
        if not chunk: break
        out.extend(chunk)
        if total and len(out) >= total: break
        if len(chunk) < PAGE_SIZE: break
        time.sleep(0.15)
    return out, total or len(out)


def merge_tasks(target_dict, new_tasks):
    """Adiciona novas em target_dict (id -> task). Retorna novos adicionados."""
    novos = 0
    for t in new_tasks:
        tid = t.get('Id')
        if tid and tid not in target_dict:
            target_dict[tid] = t
            novos += 1
    return novos


def derive_projects(tasks):
    projs = {}
    for t in tasks:
        pid = t.get('ProjectId')
        if not pid: continue
        if pid not in projs:
            projs[pid] = {
                'Id': pid, 'Name': (t.get('ProjectName') or '').strip(),
                'TotalTasks': 0, 'OpenTasks': 0, 'FinishedTasks': 0, 'ArchivedTasks': 0
            }
        projs[pid]['TotalTasks'] += 1
        if t.get('Archived'): projs[pid]['ArchivedTasks'] += 1
        if t.get('Finished'): projs[pid]['FinishedTasks'] += 1
        else: projs[pid]['OpenTasks'] += 1
    return list(projs.values())


def main():
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] Iniciando sincronizacao FlowUp via API REST')
    print(f'  Base: {BASE_URL} | Subdominio: {SUBDOMAIN}')

    get_token()
    print('  Token OK')

    all_tasks = {}  # id -> task

    print('\n[FASE 1] Busca geral (ShowFinished:true)')
    g1, c1 = paginate_query({'ShowFinished': True, 'ShowArchived': False}, label='geral')
    novos = merge_tasks(all_tasks, g1)
    print(f'  Coletadas: {len(g1)} | Count API: {c1} | Acumulado: {len(all_tasks)}')

    print('\n[FASE 2] Busca geral incluindo arquivadas')
    g2, c2 = paginate_query({'ShowFinished': True, 'ShowArchived': True}, label='arquivadas')
    novos2 = merge_tasks(all_tasks, g2)
    print(f'  Novas (arquivadas): {novos2} | Acumulado: {len(all_tasks)}')

    print(f'\n[FASE 3] Varredura por ProjectId 1..{MAX_PROJECT_ID}')
    project_ids_found = set(t.get('ProjectId') for t in all_tasks.values() if t.get('ProjectId'))
    for pid in range(1, MAX_PROJECT_ID + 1):
        # SHOW FINISHED + ARCHIVED para cobrir tudo do projeto
        tks, cnt = paginate_query(
            {'Projects': [pid], 'ShowFinished': True, 'ShowArchived': True},
            label=f'p{pid}'
        )
        if tks:
            adicionados = merge_tasks(all_tasks, tks)
            project_ids_found.add(pid)
            if adicionados > 0:
                print(f'  pid={pid}: +{adicionados} novas (total projeto: {len(tks)}, Count={cnt})')

    print(f'\n[FASE 4] Varredura por UserId 1..{MAX_USER_ID}')
    for uid in range(1, MAX_USER_ID + 1):
        tks, cnt = paginate_query(
            {'Users': [uid], 'ShowFinished': True, 'ShowArchived': True},
            label=f'u{uid}'
        )
        if tks:
            adicionados = merge_tasks(all_tasks, tks)
            if adicionados > 0:
                print(f'  uid={uid}: +{adicionados} novas (Count={cnt})')

    print(f'\n[CONSOLIDACAO]')
    tasks_list = list(all_tasks.values())
    print(f'  Total UNIVERSO de tarefas: {len(tasks_list)}')

    projects = derive_projects(tasks_list)
    projects.sort(key=lambda p: -p['TotalTasks'])
    print(f'  Projetos descobertos: {len(projects)}')
    for p in projects:
        nome = p['Name'][:45]
        print(f"    #{p['Id']:3} {nome:45} | total={p['TotalTasks']:4} | abertas={p['OpenTasks']:3} | fin={p['FinishedTasks']:4} | arq={p['ArchivedTasks']:3}")

    print('\n[USUARIOS]')
    try:
        ur = api('GET', EP_LIST_USERS)
        users = ur.get('Result') if isinstance(ur, dict) else ur
        if not isinstance(users, list): users = []
    except Exception as e:
        print(f'  ERRO: {e}'); users = []
    print(f'  Ativos: {len(users)}')

    g_open = sum(p['OpenTasks'] for p in projects)
    g_fin = sum(p['FinishedTasks'] for p in projects)
    g_arq = sum(p['ArchivedTasks'] for p in projects)
    print(f'\n[TOTAIS] {len(tasks_list)} tarefas | abertas={g_open} | finalizadas={g_fin} | arquivadas={g_arq} | projetos={len(projects)}')

    output = {
        'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'totals': {
            'tasks': len(tasks_list),
            'open': g_open, 'finished': g_fin, 'archived': g_arq,
            'projects': len(projects)
        },
        'tasks': [
            {
                'Id': t.get('Id'), 'Title': t.get('Title'),
                'ProjectName': t.get('ProjectName'), 'ProjectId': t.get('ProjectId'),
                'BoardName': t.get('BoardName'), 'BoardId': t.get('BoardId'),
                'UserName': t.get('UserName'), 'UserId': t.get('UserId'),
                'StatusName': t.get('StatusName'), 'StatusId': t.get('StatusId'),
                'EndDate': t.get('EndDate'), 'StartDate': t.get('StartDate'),
                'FinalizationDate': t.get('FinalizationDate'),
                'CreationDate': t.get('CreationDate'),
                'Finished': t.get('Finished'), 'Archived': t.get('Archived'),
                'ChecklistCount': t.get('ChecklistCount'),
                'ChecklistCompleted': t.get('ChecklistCompleted')
            } for t in tasks_list
        ],
        'projects': projects,
        'members': [
            {'Id': u.get('Id'), 'Name': u.get('Name'), 'Email': u.get('Email'),
             'JobName': u.get('JobName'), 'Profile': u.get('Profile'),
             'IsMaster': u.get('IsMaster'), 'IsActive': True}
            for u in users
        ]
    }

    with open(os.environ.get('OUTPUT_PATH', 'flowup-data.json'), 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, separators=(',', ':'))
    print(f'\nOK -> flowup-data.json')


if __name__ == '__main__':
    main()
