#!/usr/bin/env python3
"""
Sincroniza FlowUp para flowup-data.json.
Auth: OAuth2 Password Grant em https://task.flowup.me.

Estratégia (DEFINITIVA — testada com o estado real da API do FlowUp):
1. /project/getall e /board/getactiveboards retornam VAZIO para esse subdomínio.
2. Mas /task/querytasks funciona: pagina até cobrir o Count real (1697).
3. Estratégia: pagina TODAS as tarefas (abertas + finalizadas + arquivadas)
   via querytasks com PageSize=200. Deriva a lista de projetos das próprias
   tarefas (cada task carrega ProjectId + ProjectName).
4. Estatísticas calculadas localmente: total/abertas/finalizadas por projeto.

Variáveis de ambiente:
- FLOWUP_API_KEY    (senha de API gerada no painel FlowUp)
- FLOWUP_SUBDOMAIN  (default: organizementoring)
- FLOWUP_BASE_URL   (default: https://task.flowup.me)
"""
import os, sys, json, time, urllib.request, urllib.parse, urllib.error

API_KEY = os.environ.get('FLOWUP_API_KEY', '').strip()
SUBDOMAIN = os.environ.get('FLOWUP_SUBDOMAIN', 'organizementoring').strip()
BASE_URL = os.environ.get('FLOWUP_BASE_URL', 'https://task.flowup.me').rstrip('/')

if not API_KEY:
    print('ERRO: defina FLOWUP_API_KEY (secret no GitHub).', file=sys.stderr)
    sys.exit(1)

EP_TOKEN = '/token'
EP_QUERY_TASKS = '/api/v1/public/task/querytasks'
EP_LIST_USERS = '/api/v1/public/user/getactiveusers'

PAGE_SIZE = 200
MAX_PAGES = 30  # seguro para até 6000 tarefas

_access_token = None
_token_expires_at = 0


def get_access_token():
    global _access_token, _token_expires_at
    if _access_token and _token_expires_at > time.time() + 60:
        return _access_token
    body = urllib.parse.urlencode({
        'password': API_KEY, 'grant_type': 'password',
        'scope': 'api', 'subdomain': SUBDOMAIN
    }).encode('utf-8')
    req = urllib.request.Request(
        f'{BASE_URL}{EP_TOKEN}', data=body, method='POST',
        headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode('utf-8'))
    token = data.get('access_token')
    if not token:
        raise RuntimeError(f'Sem access_token: {data}')
    _access_token = token
    _token_expires_at = time.time() + int(data.get('expires_in', 3600))
    return token


def api_call(method, path, body=None):
    token = get_access_token()
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(
        f'{BASE_URL}{path}', data=data, method=method,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json', 'Accept': 'application/json'}
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        err = e.read().decode('utf-8', errors='ignore')
        raise RuntimeError(f'HTTP {e.code} em {method} {path}: {err[:300]}')


def fetch_all_tasks(show_finished=True, show_archived=False):
    """Pagina o querytasks até esvaziar. Retorna lista de tarefas."""
    all_tasks = []
    seen_ids = set()
    total_count = None
    for page in range(1, MAX_PAGES + 1):
        try:
            resp = api_call('POST', EP_QUERY_TASKS, {
                'Filter': {'ShowFinished': show_finished, 'ShowArchived': show_archived},
                'CurrentPage': page, 'PageSize': PAGE_SIZE
            })
        except Exception as e:
            print(f'  page {page}: ERRO {e}')
            break
        chunk = resp.get('Result') or []
        if total_count is None:
            total_count = resp.get('Count', 0)
            print(f'  Count reportado pela API: {total_count}')
        if not chunk:
            print(f'  page {page}: vazio (fim da paginação)')
            break
        novos = 0
        for t in chunk:
            tid = t.get('Id')
            if tid and tid not in seen_ids:
                seen_ids.add(tid)
                all_tasks.append(t)
                novos += 1
        print(f'  page {page}: {len(chunk)} retornados, {novos} novos, acumulado={len(all_tasks)}/{total_count}')
        if novos == 0:
            break
        if total_count and len(all_tasks) >= total_count:
            break
        time.sleep(0.3)
    return all_tasks, total_count or len(all_tasks)


def derive_projects(tasks):
    """Deriva lista única de projetos a partir das tarefas."""
    projects = {}
    for t in tasks:
        pid = t.get('ProjectId')
        if not pid:
            continue
        if pid not in projects:
            projects[pid] = {
                'Id': pid,
                'Name': (t.get('ProjectName') or '').strip(),
                'TotalTasks': 0,
                'OpenTasks': 0,
                'FinishedTasks': 0,
                'ArchivedTasks': 0
            }
        projects[pid]['TotalTasks'] += 1
        if t.get('Archived'):
            projects[pid]['ArchivedTasks'] += 1
        if t.get('Finished'):
            projects[pid]['FinishedTasks'] += 1
        else:
            projects[pid]['OpenTasks'] += 1
    return list(projects.values())


def main():
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] Iniciando sincronização FlowUp')
    print(f'  Base URL:   {BASE_URL}')
    print(f'  Subdomínio: {SUBDOMAIN}')

    print('Obtendo token...')
    get_access_token()
    print('  ✓ Token obtido')

    print('\nBuscando TODAS as tarefas (finalizadas + abertas, exceto arquivadas)...')
    tasks, total_count = fetch_all_tasks(show_finished=True, show_archived=False)
    print(f'  Total coletado: {len(tasks)} (API reportou Count={total_count})')

    print('\nDerivando projetos a partir das tarefas...')
    projects = derive_projects(tasks)
    projects.sort(key=lambda p: -p['TotalTasks'])
    print(f'  Projetos descobertos: {len(projects)}')
    for p in projects:
        name = p['Name'][:42]
        print(f"    #{p['Id']:3} {name:42} | total={p['TotalTasks']:4} | abertas={p['OpenTasks']:3} | fin={p['FinishedTasks']:4}")

    print('\nBuscando usuários ativos...')
    try:
        users_resp = api_call('GET', EP_LIST_USERS)
        users = users_resp.get('Result') if isinstance(users_resp, dict) else users_resp
        if not isinstance(users, list):
            users = []
    except Exception as e:
        print(f'  ERRO ao buscar usuários: {e}')
        users = []
    print(f'  Usuários ativos: {len(users)}')

    grand_open = sum(p['OpenTasks'] for p in projects)
    grand_finished = sum(p['FinishedTasks'] for p in projects)
    print(f'\nTOTAIS GERAIS: {len(tasks)} tarefas | {grand_open} abertas | {grand_finished} finalizadas | {len(projects)} projetos')

    output = {
        'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'totals': {
            'tasks': len(tasks),
            'apiCount': total_count,
            'open': grand_open,
            'finished': grand_finished,
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
            } for t in tasks
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
