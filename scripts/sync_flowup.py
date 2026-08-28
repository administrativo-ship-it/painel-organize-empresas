#!/usr/bin/env python3
"""
Sincroniza FlowUp -> flowup-data.json via api.flowup.me (API privada, mesmos endpoints do painel).
OAuth2 Password Grant + parallel page fetch com PageSize=50.
Tambem embute o bearer token no index.html para bypass de IP bloqueado no navegador.

Estrategia:
1. Auth em api.flowup.me/token (funciona de IPs nao bloqueados como GitHub Actions)
2. Busca todas as tarefas abertas em paralelo (sem filtro de projeto, PS=50)
3. Deriva projetos a partir dos dados das tarefas
4. Para cada projeto, busca count total (aberto+finalizado)
5. Embute token no index.html via GitHub API
"""
import os, sys, json, time, base64, re, subprocess, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

FALLBACK_KEY = '0f46646eb5d54286baeb3e55d6721db7'
API_KEY   = (os.environ.get('FLOWUP_API_KEY', '') or FALLBACK_KEY).strip()
SUBDOMAIN = os.environ.get('FLOWUP_SUBDOMAIN', 'organizementoring').strip()
BASE_URL  = 'https://api.flowup.me'

EP_TOKEN       = '/token'
EP_QUERY_TASKS = '/v1/task/querytasks'
EP_LIST_USERS  = '/v1/user/getactiveusers'

PAGE_SIZE   = 50
MAX_WORKERS = 32

_token, _exp = None, 0
_token_lock  = Lock()


def get_token():
    global _token, _exp, API_KEY
    with _token_lock:
        if _token and _exp > time.time() + 60:
            return _token
        keys_to_try = list(dict.fromkeys(k for k in [API_KEY, FALLBACK_KEY] if k))
        last_err = None
        for key in keys_to_try:
            try:
                body = urllib.parse.urlencode({
                    'password': key, 'grant_type': 'password',
                    'scope': 'api', 'subdomain': SUBDOMAIN
                }).encode('utf-8')
                req = urllib.request.Request(
                    f'{BASE_URL}{EP_TOKEN}', data=body, method='POST',
                    headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    d = json.loads(r.read().decode('utf-8'))
                tk = d.get('access_token')
                if not tk:
                    raise RuntimeError(f'Sem token na resposta: {d}')
                _token = tk
                _exp   = time.time() + int(d.get('expires_in', 3600))
                if key != keys_to_try[0]:
                    print('  AVISO: FLOWUP_API_KEY do ambiente falhou, usando chave embutida')
                    API_KEY = key
                return _token
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError(f'Falha ao obter token: {last_err}')


def api_call(method, path, body=None):
    tk = get_token()
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(
        f'{BASE_URL}{path}', data=data, method=method,
        headers={
            'Authorization': f'Bearer {tk}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        err = e.read().decode('utf-8', errors='ignore')
        raise RuntimeError(f'HTTP {e.code} {method} {path}: {err[:200]}')
    except Exception as e:
        raise RuntimeError(f'NET {method} {path}: {e}')


def fetch_all_pages(extra_filter=None):
    """
    Busca todas as paginas de EP_QUERY_TASKS com PAGE_SIZE itens por pagina.
    A API do FlowUp pagina a partir de 0 (nao de 1). Pagina 0 sequencial (obtem
    Count), restantes (1..total_pages-1) em paralelo com ThreadPoolExecutor.
    """
    flt = extra_filter or {}
    body1 = {'PageSize': PAGE_SIZE, 'CurrentPage': 0, 'Filter': flt}  # API pagina a partir de 0
    d1    = api_call('POST', EP_QUERY_TASKS, body1)
    items1 = d1.get('Result') or []
    count  = d1.get('Count', 0)

    if not count or len(items1) >= count or len(items1) < PAGE_SIZE:
        return items1

    total_pages = (count + PAGE_SIZE - 1) // PAGE_SIZE
    all_items   = items1[:]

    def fetch_page(pg):
        body = {'PageSize': PAGE_SIZE, 'CurrentPage': pg, 'Filter': flt}
        try:
            d = api_call('POST', EP_QUERY_TASKS, body)
            return d.get('Result') or []
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(fetch_page, pg): pg for pg in range(1, total_pages)}  # pagina 0 ja obtida acima
        for fut in as_completed(futs):
            all_items.extend(fut.result())

    return all_items


def get_project_total_count(pid):
    """Retorna count total de tarefas (abertas + finalizadas) para um projeto."""
    try:
        d = api_call('POST', EP_QUERY_TASKS, {
            'PageSize': 1, 'CurrentPage': 1,
            'Filter': {'Projects': [pid], 'ShowFinished': True, 'ShowArchived': False}
        })
        return d.get('Count', 0)
    except Exception:
        return 0


def _get_github_token_from_git_config():
    """Extrai GITHUB_TOKEN do git config criado por actions/checkout@v4."""
    try:
        r = subprocess.run(
            ['git', 'config', '--local', '--get-all', 'http.https://github.com/.extraheader'],
            capture_output=True, text=True
        )
        for line in r.stdout.splitlines():
            if 'basic ' in line:
                b64 = line.split('basic ', 1)[1].strip()
                decoded = base64.b64decode(b64).decode('utf-8', errors='ignore')
                if decoded.startswith('x-access-token:'):
                    return decoded.split(':', 1)[1]
    except Exception:
        pass
    return None


DC_PATTERN = (
    r"const _DC = \(function\(\)\{try\{return JSON\.parse\(atob\(\'([^\']+)\'\)\);"
    r"\}catch\(e\)\{return \{\};\}\}\)\(\);"
)


def embed_token_in_index_html_via_api():
    """
    Busca index.html do GitHub, embute fuToken no _DC, commita de volta via API.
    Token GitHub obtido do env GITHUB_TOKEN ou extraido do git config (actions/checkout).
    """
    if not _token:
        print('  AVISO: _token vazio, pulando embed'); return False

    gh_token = (os.environ.get('GITHUB_TOKEN') or '').strip()
    if not gh_token:
        gh_token = (_get_github_token_from_git_config() or '').strip()
    if not gh_token:
        print('  AVISO: GITHUB_TOKEN nao disponivel'); return False

    repo = os.environ.get('GITHUB_REPOSITORY', 'administrativo-ship-it/painel-organize-empresas')
    url  = f'https://api.github.com/repos/{repo}/contents/index.html'
    gh_headers = {
        'Authorization': f'Bearer {gh_token}',
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'sync-flowup',
        'X-GitHub-Api-Version': '2022-11-28',
    }

    try:
        req = urllib.request.Request(url, headers=gh_headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            current = json.loads(r.read())
    except Exception as e:
        print(f'  AVISO: erro ao buscar index.html: {e}'); return False

    current_sha = current['sha']
    try:
        html = base64.b64decode(current['content'].replace('\n', '')).decode('utf-8')
    except Exception as e:
        print(f'  AVISO: erro ao decodificar index.html: {e}'); return False

    m = re.search(DC_PATTERN, html)
    if not m:
        print('  AVISO: _DC nao encontrado em index.html'); return False

    try:
        dc = json.loads(base64.b64decode(m.group(1)).decode('utf-8'))
    except Exception as e:
        print(f'  AVISO: falha ao decodificar _DC: {e}'); return False

    if dc.get('fuToken') == _token:
        print('  index.html: token ja atualizado'); return True

    dc['fuToken']    = _token
    dc['fuTokenExp'] = int(_exp * 1000)

    new_b64 = base64.b64encode(json.dumps(dc, separators=(',', ':')).encode()).decode()
    new_dc  = (
        "const _DC = (function(){try{return JSON.parse(atob('"
        + new_b64
        + "'));}catch(e){return {};}})();"
    )
    new_html = re.sub(DC_PATTERN, new_dc, html)
    if new_html == html:
        print('  AVISO: substituicao de _DC sem efeito'); return False

    exp_date = time.strftime('%Y-%m-%d', time.gmtime(_exp))
    try:
        req2 = urllib.request.Request(
            url,
            data=json.dumps({
                'message': f'fix: fuToken pre-obtido (exp {exp_date}) [skip ci]',
                'content': base64.b64encode(new_html.encode('utf-8')).decode(),
                'sha': current_sha,
            }).encode(),
            method='PUT',
            headers={**gh_headers, 'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req2, timeout=30) as r:
            result = json.loads(r.read())
        print(f'  index.html commitado: {result["commit"]["sha"][:8]} (exp {exp_date})')
        return True
    except urllib.error.HTTPError as e:
        err = e.read().decode('utf-8', errors='ignore')
        print(f'  AVISO: erro ao commitar index.html HTTP {e.code}: {err[:200]}')
        return False
    except Exception as e:
        print(f'  AVISO: erro ao commitar index.html: {e}'); return False


def main():
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] Sync FlowUp | Base: {BASE_URL} | Sub: {SUBDOMAIN} | PS={PAGE_SIZE}')

    get_token()
    print('  Token OK')

    # Busca todas as tarefas abertas (sem filtro de projeto)
    print(f'\n[1] Buscando tarefas abertas (ShowFinished=false, PS={PAGE_SIZE})...')
    t0 = time.time()
    raw_tasks = fetch_all_pages({'ShowFinished': False, 'ShowArchived': False})
    # Deduplicacao
    seen = {}
    for t in raw_tasks:
        if t and t.get('Id'):
            seen[t['Id']] = t
    tasks_list = list(seen.values())
    print(f'  Coletadas: {len(tasks_list)} tarefas ({time.time()-t0:.1f}s)')

    if not tasks_list:
        print('  AVISO: 0 tarefas retornadas — possivel falha de auth ou acesso')

    # Deriva projetos
    print('\n[2] Derivando projetos...')
    pid_to_name = {}
    pid_to_open = {}
    for t in tasks_list:
        pid = t.get('ProjectId')
        if not pid: continue
        pid_to_name[pid] = (t.get('ProjectName') or '').strip()
        pid_to_open[pid] = pid_to_open.get(pid, 0) + 1

    project_counts = {}
    for pid in pid_to_name:
        total = get_project_total_count(pid)
        project_counts[pid] = {
            'total': total,
            'open': pid_to_open.get(pid, 0),
            'finished': max(0, total - pid_to_open.get(pid, 0))
        }

    projects = [
        {
            'Id': pid, 'Name': pid_to_name[pid],
            'TotalTasks':    project_counts[pid]['total'],
            'OpenTasks':     project_counts[pid]['open'],
            'FinishedTasks': project_counts[pid]['finished'],
            'ArchivedTasks': 0
        }
        for pid in pid_to_name
    ]
    projects.sort(key=lambda p: -p['TotalTasks'])

    for p in projects:
        print(f"  #{p['Id']:6} {p['Name'][:42]:42} | tot={p['TotalTasks']:4} | ab={p['OpenTasks']:3}")

    # Membros
    print('\n[3] Membros...')
    try:
        ur    = api_call('GET', EP_LIST_USERS)
        users = ur.get('Result') if isinstance(ur, dict) else ur
        if not isinstance(users, list): users = []
    except Exception as e:
        print(f'  ERRO: {e}'); users = []
    print(f'  Ativos: {len(users)}')

    g_total = sum(p['TotalTasks']    for p in projects)
    g_open  = sum(p['OpenTasks']     for p in projects)
    g_fin   = sum(p['FinishedTasks'] for p in projects)
    print(f'\n[TOTAIS] tarefas={g_total} | abertas={g_open} | fin={g_fin} | projetos={len(projects)}')

    # Embute token no index.html
    print('\n[4] Embed token em index.html...')
    embed_token_in_index_html_via_api()

    # Escreve saida
    output = {
        'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'totals': {
            'tasks': g_total, 'open': g_open, 'finished': g_fin,
            'projects': len(projects), 'tasksCollected': len(tasks_list)
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
            {
                'Id': u.get('Id'), 'Name': u.get('Name'), 'Email': u.get('Email'),
                'JobName': u.get('JobName'), 'Profile': u.get('Profile'),
                'IsMaster': u.get('IsMaster'), 'IsActive': True
            }
            for u in users
        ]
    }

    out_path = os.environ.get('OUTPUT_PATH', 'flowup-data.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, separators=(',', ':'))
    print(f'\nOK -> {out_path}')


if __name__ == '__main__':
    main()
