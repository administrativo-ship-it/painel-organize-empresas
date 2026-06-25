#!/usr/bin/env python3
"""
Sincroniza dados do FlowUp para flowup-data.json.
Auth: OAuth2 Password Grant (POST /token com password + subdomain).
URL base: https://task.flowup.me

Variáveis de ambiente:
- FLOWUP_API_KEY    (a senha de API gerada no painel FlowUp)
- FLOWUP_SUBDOMAIN  (default: organizementoring)
- FLOWUP_BASE_URL   (default: https://task.flowup.me)
"""
import os
import sys
import json
import time
import urllib.request
import urllib.parse
import urllib.error

API_KEY = os.environ.get('FLOWUP_API_KEY', '').strip()
SUBDOMAIN = os.environ.get('FLOWUP_SUBDOMAIN', 'organizementoring').strip()
BASE_URL = os.environ.get('FLOWUP_BASE_URL', 'https://task.flowup.me').rstrip('/')

if not API_KEY:
    print('ERRO: defina FLOWUP_API_KEY (secret no GitHub).', file=sys.stderr)
    sys.exit(1)

# Endpoints (extraídos do MCP server oficial)
EP_TOKEN = '/token'
EP_LIST_PROJECTS = '/api/v1/public/project/getall'
EP_QUERY_TASKS = '/api/v1/public/task/querytasks'
EP_LIST_USERS = '/api/v1/public/user/getactiveusers'

_access_token = None
_token_expires_at = 0


def get_access_token():
    """OAuth2 Password Grant — autentica e retorna o access_token (com cache de 1h)."""
    global _access_token, _token_expires_at
    if _access_token and _token_expires_at > time.time() + 60:
        return _access_token

    body = urllib.parse.urlencode({
        'password': API_KEY,
        'grant_type': 'password',
        'scope': 'api',
        'subdomain': SUBDOMAIN
    }).encode('utf-8')

    req = urllib.request.Request(
        f'{BASE_URL}{EP_TOKEN}',
        data=body,
        method='POST',
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json',
            'User-Agent': 'organize-painel-sync/1.0'
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        err = e.read().decode('utf-8', errors='ignore')
        raise RuntimeError(f'Token HTTP {e.code}: {err[:300]}')

    token = data.get('access_token')
    if not token:
        raise RuntimeError(f'Resposta sem access_token: {data}')

    _access_token = token
    _token_expires_at = time.time() + int(data.get('expires_in', 3600))
    return token


def api_call(method, path, body=None):
    """Chama um endpoint da API FlowUp já autenticado."""
    token = get_access_token()
    url = f'{BASE_URL}{path}'
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': 'organize-painel-sync/1.0'
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        err = e.read().decode('utf-8', errors='ignore')
        raise RuntimeError(f'HTTP {e.code} em {method} {path}: {err[:300]}')


def fetch_all_pages(method, path, body_fn=None, page_size=200, max_pages=50):
    """Paginação por CurrentPage/PageSize."""
    all_results = []
    for page in range(1, max_pages + 1):
        body = body_fn(page, page_size) if body_fn else None
        resp = api_call(method, path, body)
        chunk = resp.get('Result') or []
        all_results.extend(chunk)
        total = resp.get('Count', len(all_results))
        if not chunk or len(all_results) >= total:
            break
        time.sleep(0.3)
    return all_results


def main():
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] Iniciando sincronização FlowUp')
    print(f'  Base URL:   {BASE_URL}')
    print(f'  Subdomínio: {SUBDOMAIN}')

    # 1) Autentica
    print('Obtendo token de acesso...')
    get_access_token()
    print('  ✓ Token obtido')

    # 2) Primeiro busca ALL tasks (pode truncar — usado só pra descobrir projects)
    print('Buscando tarefas (descoberta de projetos)...')
    initial_tasks = fetch_all_pages(
        'POST',
        EP_QUERY_TASKS,
        lambda page, ps: {
            'Filter': {'ShowFinished': True, 'ShowArchived': False},
            'CurrentPage': page,
            'PageSize': 500
        },
        page_size=500
    )
    project_ids = set()
    for t in initial_tasks:
        pid = t.get('ProjectId')
        if pid:
            project_ids.add(pid)
    print(f'  Descoberta inicial: {len(initial_tasks)} tarefas, {len(project_ids)} projetos únicos')

    # 3) Para cada projeto, busca completa (evita truncamento global)
    print('Buscando tarefas projeto por projeto (mais confiável)...')
    all_tasks_by_id = {t['Id']: t for t in initial_tasks if t.get('Id')}
    for i, pid in enumerate(sorted(project_ids), 1):
        try:
            proj_tasks = fetch_all_pages(
                'POST',
                EP_QUERY_TASKS,
                lambda page, ps: {
                    'Filter': {'Projects': [pid], 'ShowFinished': True, 'ShowArchived': False},
                    'CurrentPage': page,
                    'PageSize': 500
                },
                page_size=500
            )
            antes = len(all_tasks_by_id)
            for t in proj_tasks:
                if t.get('Id'):
                    all_tasks_by_id[t['Id']] = t  # sobrescreve com versão mais completa
            ganho = len(all_tasks_by_id) - antes
            pname = next((t.get('ProjectName','?') for t in proj_tasks if t.get('ProjectName')), '?')
            print(f'  [{i}/{len(project_ids)}] Proj #{pid} ({pname.strip()[:40]}): {len(proj_tasks)} tarefas (+{ganho} novas)')
        except Exception as e:
            print(f'  [{i}/{len(project_ids)}] Proj #{pid} ERRO: {e}')

    tasks = list(all_tasks_by_id.values())
    print(f'  Tarefas TOTAL (deduplicadas): {len(tasks)}')

    # 3) Projetos — endpoint quebra com page_size grande, usa 25
    # E tenta múltiplos formatos de body (FlowUp é exigente)
    print('Buscando projetos...')
    projects = []
    body_variants = [
        ('Filter:{}', lambda page, ps: {'Filter': {}, 'CurrentPage': page, 'PageSize': ps}),
        ('sem Filter', lambda page, ps: {'CurrentPage': page, 'PageSize': ps}),
        ('Filter:Active=true', lambda page, ps: {'Filter': {'Active': True}, 'CurrentPage': page, 'PageSize': ps}),
        ('Filter:null', lambda page, ps: {'Filter': None, 'CurrentPage': page, 'PageSize': ps}),
    ]
    PROJECTS_PAGE_SIZE = 25  # FlowUp /project/getall só funciona com page_size pequeno
    for label, bf in body_variants:
        try:
            test = api_call('POST', EP_LIST_PROJECTS, bf(1, PROJECTS_PAGE_SIZE))
            cnt = test.get('Count', 0)
            results = test.get('Result') or []
            print(f'  [{label}] Count={cnt}, Result.length={len(results)}')
            if len(results) > 0:
                print(f'  ✓ Usando body "{label}" (Count: {cnt})')
                # Pagina manualmente com page_size pequeno
                projects = list(results)
                page = 2
                while len(projects) < cnt and page < 20:
                    resp = api_call('POST', EP_LIST_PROJECTS, bf(page, PROJECTS_PAGE_SIZE))
                    more = resp.get('Result') or []
                    if not more:
                        break
                    projects.extend(more)
                    page += 1
                    time.sleep(0.3)
                break
        except Exception as e:
            print(f'  [{label}] falhou: {e}')
    # Fallback: se o endpoint não retornou nada, deriva dos tasks
    if not projects:
        print('  ⚠ Endpoint de projetos retornou 0 — derivando de tarefas')
        seen = {}
        for t in tasks:
            pid = t.get('ProjectId')
            if pid and pid not in seen:
                seen[pid] = {
                    'Id': pid,
                    'Name': (t.get('ProjectName') or '').strip(),
                    'StatusName': 'Em Andamento',  # presumido
                    'Active': True
                }
        projects = list(seen.values())
        print(f'  Projetos derivados: {len(projects)}')
    print(f'  Projetos: {len(projects)}')

    # 4) Usuários ativos
    print('Buscando usuários...')
    users_resp = api_call('GET', EP_LIST_USERS)
    users = users_resp.get('Result') if isinstance(users_resp, dict) else users_resp
    if not isinstance(users, list):
        users = []
    print(f'  Usuários ativos: {len(users)}')

    # 5) Monta JSON compatível com o painel
    output = {
        'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'tasks': [
            {
                'Id': t.get('Id'),
                'Title': t.get('Title'),
                'ProjectName': t.get('ProjectName'),
                'ProjectId': t.get('ProjectId'),
                'BoardName': t.get('BoardName'),
                'BoardId': t.get('BoardId'),
                'UserName': t.get('UserName'),
                'UserId': t.get('UserId'),
                'StatusName': t.get('StatusName'),
                'StatusId': t.get('StatusId'),
                'EndDate': t.get('EndDate'),
                'StartDate': t.get('StartDate'),
                'FinalizationDate': t.get('FinalizationDate'),
                'CreationDate': t.get('CreationDate'),
                'Finished': t.get('Finished'),
                'Archived': t.get('Archived'),
                'ChecklistCount': t.get('ChecklistCount'),
                'ChecklistCompleted': t.get('ChecklistCompleted')
            } for t in tasks
        ],
        'projects': [
            {
                'Id': p.get('Id'),
                'Name': p.get('Name'),
                'StatusName': p.get('StatusName'),
                'StatusId': p.get('StatusId'),
                'OwnerName': p.get('OwnerName'),
                'OwnerId': p.get('OwnerId'),
                'Active': p.get('Active'),
                'InitialDate': p.get('InitialDate'),
                'FinalDate': p.get('FinalDate')
            } for p in projects
        ],
        'members': [
            {
                'Id': u.get('Id'),
                'Name': u.get('Name'),
                'Email': u.get('Email'),
                'JobName': u.get('JobName'),
                'Profile': u.get('Profile'),
                'IsMaster': u.get('IsMaster'),
                'IsActive': True
            } for u in users
        ]
    }

    out_path = os.environ.get('OUTPUT_PATH', 'flowup-data.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, separators=(',', ':'))

    print(f'OK -> {out_path}')
    print(f'  Total: {len(tasks)} tarefas, {len(projects)} projetos, {len(users)} usuários')


if __name__ == '__main__':
    main()
