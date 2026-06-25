#!/usr/bin/env python3
"""
Sincroniza FlowUp para flowup-data.json.
Auth: OAuth2 Password Grant em https://task.flowup.me.

Estratégia (compatível com bugs da API do FlowUp):
1. Lista projetos via /project/getall (page_size=20, paginação manual)
2. Para CADA projeto, busca TODAS as tarefas ABERTAS via querytasks
   (filter sem ShowFinished retorna só abertas — sempre cabe em <= 200)
3. Para CADA projeto, pega Count total via query mínima (page_size=1)
4. Para alguns projetos, pega até 4 páginas de finalizadas (até 800) para histórico

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

# Endpoints (do MCP oficial)
EP_TOKEN = '/token'
EP_LIST_PROJECTS = '/api/v1/public/project/getall'
EP_QUERY_TASKS = '/api/v1/public/task/querytasks'
EP_LIST_USERS = '/api/v1/public/user/getactiveusers'

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
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        err = e.read().decode('utf-8', errors='ignore')
        raise RuntimeError(f'HTTP {e.code} em {method} {path}: {err[:300]}')


def list_projects():
    """Lista projetos paginando com page_size=20 (workaround para bug do FlowUp)."""
    all_proj = []
    seen_ids = set()
    for page in range(1, 20):
        try:
            resp = api_call('POST', EP_LIST_PROJECTS, {
                'Filter': {}, 'CurrentPage': page, 'PageSize': 20
            })
        except Exception as e:
            print(f'  page {page}: ERRO {e}')
            break
        chunk = resp.get('Result') or []
        if not chunk:
            break
        novos = 0
        for p in chunk:
            if p.get('Id') and p['Id'] not in seen_ids:
                seen_ids.add(p['Id'])
                all_proj.append(p)
                novos += 1
        total = resp.get('Count', 0)
        print(f'  page {page}: {len(chunk)} retornados, {novos} novos, Count={total}, total acumulado={len(all_proj)}')
        if novos == 0:
            break
        if len(all_proj) >= total > 0:
            break
        time.sleep(0.2)
    return all_proj


def query_tasks_for_project(project_id):
    """Busca tarefas abertas + count total de um projeto."""
    # Tarefas ABERTAS — sem ShowFinished (default da API = só abertas)
    open_tasks = []
    for page in range(1, 5):  # max 4 páginas = 800 (mas abertas raramente >200)
        try:
            resp = api_call('POST', EP_QUERY_TASKS, {
                'Filter': {'Projects': [project_id]},
                'CurrentPage': page, 'PageSize': 200
            })
        except Exception:
            break
        chunk = resp.get('Result') or []
        if not chunk:
            break
        open_tasks.extend(chunk)
        if len(chunk) < 200:
            break
        time.sleep(0.3)

    # Total via Count
    try:
        resp_total = api_call('POST', EP_QUERY_TASKS, {
            'Filter': {'Projects': [project_id], 'ShowFinished': True, 'ShowArchived': False},
            'CurrentPage': 1, 'PageSize': 1
        })
        count_total = resp_total.get('Count', len(open_tasks))
    except Exception:
        count_total = len(open_tasks)

    count_open = len(open_tasks)
    count_finished = max(0, count_total - count_open)

    # Algumas finalizadas pra contexto (até 200 = 1 página)
    finished_sample = []
    try:
        resp_fin = api_call('POST', EP_QUERY_TASKS, {
            'Filter': {'Projects': [project_id], 'ShowFinished': True, 'ShowArchived': False},
            'CurrentPage': 1, 'PageSize': 200
        })
        finished_sample = resp_fin.get('Result') or []
        # Filtra só as finalizadas (excluindo as abertas que já temos)
        open_ids = {t.get('Id') for t in open_tasks}
        finished_sample = [t for t in finished_sample if t.get('Id') not in open_ids]
    except Exception:
        pass

    return {
        'open': open_tasks,
        'finished_sample': finished_sample,
        'count_total': count_total,
        'count_open': count_open,
        'count_finished': count_finished
    }


def main():
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] Iniciando sincronização FlowUp')
    print(f'  Base URL:   {BASE_URL}')
    print(f'  Subdomínio: {SUBDOMAIN}')

    print('Obtendo token...')
    get_access_token()
    print('  ✓ Token obtido')

    print('Listando projetos via /project/getall...')
    projects = list_projects()
    print(f'  Total projetos descobertos: {len(projects)}')

    print('Buscando usuários ativos...')
    users_resp = api_call('GET', EP_LIST_USERS)
    users = users_resp.get('Result') if isinstance(users_resp, dict) else users_resp
    if not isinstance(users, list):
        users = []
    print(f'  Usuários ativos: {len(users)}')

    print('Buscando tarefas por projeto...')
    all_tasks = {}
    project_stats = {}
    for i, p in enumerate(projects, 1):
        pid = p.get('Id')
        if not pid:
            continue
        try:
            data = query_tasks_for_project(pid)
            for t in data['open']:
                if t.get('Id'):
                    all_tasks[t['Id']] = t
            for t in data['finished_sample']:
                if t.get('Id') and t['Id'] not in all_tasks:
                    all_tasks[t['Id']] = t
            project_stats[pid] = {
                'total': data['count_total'],
                'open': data['count_open'],
                'finished': data['count_finished']
            }
            name = (p.get('Name') or '').strip()
            print(f'  [{i}/{len(projects)}] #{pid} {name[:40]:40} | abertas={data[\"count_open\"]:3} | total={data[\"count_total\"]:4}')
        except Exception as e:
            print(f'  [{i}/{len(projects)}] #{pid} ERRO: {e}')

    tasks_list = list(all_tasks.values())
    print(f'  Total de tarefas COLETADAS: {len(tasks_list)} (open detalhadas + finished sample)')

    grand_total = sum(s['total'] for s in project_stats.values())
    grand_open = sum(s['open'] for s in project_stats.values())
    print(f'  TOTAIS REAIS (via API Count): {grand_total} total, {grand_open} abertas, {grand_total - grand_open} finalizadas')

    output = {
        'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'totals': {
            'tasks': grand_total,
            'open': grand_open,
            'finished': grand_total - grand_open,
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
        'projects': [
            {
                'Id': p.get('Id'), 'Name': (p.get('Name') or '').strip(),
                'StatusName': p.get('StatusName'), 'StatusId': p.get('StatusId'),
                'OwnerName': p.get('OwnerName'), 'OwnerId': p.get('OwnerId'),
                'Active': p.get('Active'),
                'InitialDate': p.get('InitialDate'), 'FinalDate': p.get('FinalDate'),
                'TotalTasks': project_stats.get(p.get('Id'), {}).get('total', 0),
                'OpenTasks': project_stats.get(p.get('Id'), {}).get('open', 0),
                'FinishedTasks': project_stats.get(p.get('Id'), {}).get('finished', 0)
            } for p in projects
        ],
        'members': [
            {'Id': u.get('Id'), 'Name': u.get('Name'), 'Email': u.get('Email'),
             'JobName': u.get('JobName'), 'Profile': u.get('Profile'),
             'IsMaster': u.get('IsMaster'), 'IsActive': True}
            for u in users
        ]
    }

    with open(os.environ.get('OUTPUT_PATH', 'flowup-data.json'), 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, separators=(',', ':'))

    print(f'OK -> flowup-data.json')


if __name__ == '__main__':
    main()
