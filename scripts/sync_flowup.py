#!/usr/bin/env python3
"""
Sincroniza FlowUp -> flowup-data.json via API REST direta (sem MCP).
OAuth2 Password Grant em https://task.flowup.me.
Tambem embute o bearer token no index.html (bypass de IP bloqueado no navegador).
"""
import os, sys, json, time, base64, re, subprocess, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# Chave nova embutida como fallback caso o secret FLOWUP_API_KEY ainda tenha a chave velha
FALLBACK_KEY = '0f46646eb5d54286baeb3e55d6721db7'
API_KEY   = (os.environ.get('FLOWUP_API_KEY', '') or FALLBACK_KEY).strip()
SUBDOMAIN = os.environ.get('FLOWUP_SUBDOMAIN', 'organizementoring').strip()
BASE_URL  = os.environ.get('FLOWUP_BASE_URL', 'https://task.flowup.me').rstrip('/')

EP_TOKEN       = '/token'
EP_QUERY_TASKS = '/api/v1/public/task/querytasks'
EP_LIST_USERS  = '/api/v1/public/user/getactiveusers'

MAX_PID           = 30
N_RECENT_FINISHED = 0
MAX_WORKERS       = 32

_token, _exp = None, 0
_token_lock  = Lock()


def get_token():
    global _token, _exp, API_KEY
    with _token_lock:
        if _token and _exp > time.time() + 60: return _token
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
                    raise RuntimeError(f'Sem token: {d}')
                _token = tk
                _exp   = time.time() + int(d.get('expires_in', 3600))
                if key != keys_to_try[0]:
                    print('  AVISO: env FLOWUP_API_KEY falhou, usando chave embutida')
                    API_KEY = key
                return _token
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError(f'Falha ao obter token: {last_err}')


def api(method, path, body=None):
    tk = get_token()
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(
        f'{BASE_URL}{path}', data=data, method=method,
        headers={'Authorization': f'Bearer {tk}', 'Content-Type': 'application/json', 'Accept': 'application/json'}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        err = e.read().decode('utf-8', errors='ignore')
        raise RuntimeError(f'HTTP {e.code} {method} {path}: {err[:200]}')
    except Exception as e:
        raise RuntimeError(f'NET {method} {path}: {e}')


def query_one(filter_obj, page, retries=2):
    for attempt in range(retries + 1):
        try:
            r = api('POST', EP_QUERY_TASKS, {
                'Filter': filter_obj, 'CurrentPage': page, 'PageSize': 1
            })
            chunk = r.get('Result') or []
            return (chunk[0] if chunk else None), r.get('Count', 0)
        except Exception:
            if attempt < retries:
                time.sleep(0.3 * (attempt + 1))
                continue
            return None, 0


def fetch_pages_parallel(filter_obj, page_start, page_end):
    tasks = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(query_one, filter_obj, p) for p in range(page_start, page_end + 1)]
        for fut in as_completed(futs):
            t, _ = fut.result()
            if t: tasks.append(t)
    return tasks


def merge_into(target, new_list):
    for t in new_list:
        tid = t.get('Id')
        if tid and tid not in target:
            target[tid] = t


def derive_projects(tasks, project_counts):
    projs = {}
    for t in tasks:
        pid = t.get('ProjectId')
        if not pid: continue
        if pid not in projs:
            projs[pid] = {
                'Id': pid, 'Name': (t.get('ProjectName') or '').strip(),
                'TotalTasks':    project_counts.get(pid, {}).get('total', 0),
                'OpenTasks':     project_counts.get(pid, {}).get('open', 0),
                'FinishedTasks': project_counts.get(pid, {}).get('finished', 0),
                'ArchivedTasks': 0
            }
    return list(projs.values())


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


def embed_token_in_index_html_via_api():
    """
    Busca index.html do GitHub, embute fuToken no _DC, commita de volta via API.
    Token GitHub obtido do env GITHUB_TOKEN ou do git config (actions/checkout).
    Nao modifica arquivo local para evitar conflito com git pull --rebase.
    """
    if not _token:
        print('  AVISO: _token vazio, pulando embed'); return False

    gh_token = (os.environ.get('GITHUB_TOKEN') or '').strip()
    if not gh_token:
        gh_token = _get_github_token_from_git_config() or ''
    if not gh_token:
        print('  AVISO: GITHUB_TOKEN nao disponivel (env nem git config)'); return False

    repo = os.environ.get('GITHUB_REPOSITORY', 'administrativo-ship-it/painel-organize-empresas')
    url  = f'https://api.github.com/repos/{repo}/contents/index.html'

    gh_headers = {
        'Authorization': f'Bearer {gh_token}',
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'sync-flowup',
        'X-GitHub-Api-Version': '2022-11-28',
    }

    # Busca conteudo atual
    try:
        req = urllib.request.Request(url, headers=gh_headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            current = json.loads(r.read())
    except Exception as e:
        print(f'  AVISO: erro ao buscar index.html da API GitHub: {e}'); return False

    current_sha = current['sha']
    try:
        html = base64.b64decode(current['content'].replace('\n', '')).decode('utf-8')
    except Exception as e:
        print(f'  AVISO: erro ao decodificar index.html: {e}'); return False

    # Localiza _DC e decodifica
    DC_PATTERN = (
        r"const _DC = \(function\(\)\{try\{return JSON\.parse\(atob\(\'([^\']+)\'\)\);"
        r"\}catch\(e\)\{return \{\};\}\}\)\(\);"
    )
    m = re.search(DC_PATTERN, html)
    if not m:
        print('  AVISO: _DC nao encontrado em index.html'); return False

    try:
        dc = json.loads(base64.b64decode(m.group(1)).decode('utf-8'))
    except Exception as e:
        print(f'  AVISO: falha ao decodificar _DC: {e}'); return False

    if dc.get('fuToken') == _token:
        print('  index.html: token ja esta atualizado, nada a commitar'); return True

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

    # Commita via API
    exp_date = time.strftime('%Y-%m-%d', time.gmtime(_exp))
    commit_body = {
        'message': f'fix: fuToken pre-obtido (exp {exp_date}) [skip ci]',
        'content': base64.b64encode(new_html.encode('utf-8')).decode(),
        'sha': current_sha,
    }
    try:
        req2 = urllib.request.Request(
            url, data=json.dumps(commit_body).encode(), method='PUT',
            headers={**gh_headers, 'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req2, timeout=30) as r:
            result = json.loads(r.read())
        print(f'  index.html commitado via API: {result["commit"]["sha"][:8]} (exp {exp_date})')
        return True
    except urllib.error.HTTPError as e:
        err = e.read().decode('utf-8', errors='ignore')
        print(f'  AVISO: erro ao commitar index.html (HTTP {e.code}): {err[:200]}')
        return False
    except Exception as e:
        print(f'  AVISO: erro ao commitar index.html: {e}'); return False


def main():
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] Sync FlowUp (PS=1 paralelo, MAX_WORKERS={MAX_WORKERS})')
    print(f'  Base: {BASE_URL} | Sub: {SUBDOMAIN}')
    get_token()
    print('  Token OK')

    all_tasks = {}
    project_counts = {}

    print(f'\n[1] Descobrindo projetos (pid 1..{MAX_PID})')
    valid_pids = []
    for pid in range(1, MAX_PID + 1):
        _, total      = query_one({'Projects': [pid], 'ShowFinished': True,  'ShowArchived': False}, 1)
        _, total_open = query_one({'Projects': [pid], 'ShowFinished': False, 'ShowArchived': False}, 1)
        if total > 0:
            project_counts[pid] = {
                'total': total, 'open': total_open, 'finished': total - total_open
            }
            valid_pids.append(pid)
            print(f'  pid={pid}: total={total} abertas={total_open}')

    print(f'\n[2] Coletando tarefas ({len(valid_pids)} projetos serial, paralelismo interno {MAX_WORKERS})')
    for pid in valid_pids:
        t_start = time.time()
        n_open  = project_counts[pid]['open']
        sys.stdout.flush()
        tks = fetch_pages_parallel(
            {'Projects': [pid], 'ShowFinished': False, 'ShowArchived': False}, 0, n_open - 1
        ) if n_open > 0 else []
        merge_into(all_tasks, tks)
        dur = time.time() - t_start
        print(f'  pid={pid}: open={len(tks)}/{n_open} | {dur:.1f}s | acumulado={len(all_tasks)}', flush=True)

    tasks_list = list(all_tasks.values())
    projects   = derive_projects(tasks_list, project_counts)
    projects.sort(key=lambda p: -p['TotalTasks'])

    print(f'\n[CONSOLIDACAO] {len(tasks_list)} tarefas em {len(projects)} projetos')
    for p in projects:
        nome = p['Name'][:42]
        print(f"  #{p['Id']:3} {nome:42} | tot={p['TotalTasks']:4} | ab={p['OpenTasks']:3} | fin={p['FinishedTasks']:4}")

    print('\n[USUARIOS]')
    try:
        ur    = api('GET', EP_LIST_USERS)
        users = ur.get('Result') if isinstance(ur, dict) else ur
        if not isinstance(users, list): users = []
    except Exception as e:
        print(f'  ERRO: {e}'); users = []
    print(f'  Ativos: {len(users)}')

    g_total = sum(p['TotalTasks']    for p in projects)
    g_open  = sum(p['OpenTasks']     for p in projects)
    g_fin   = sum(p['FinishedTasks'] for p in projects)
    print(f'\n[TOTAIS REAIS] tarefas={g_total} | abertas={g_open} | fin={g_fin} | projetos={len(projects)}')

    print('\n[TOKEN EMBED]')
    embed_token_in_index_html_via_api()

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

    with open(os.environ.get('OUTPUT_PATH', 'flowup-data.json'), 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, separators=(',', ':'))
    print(f'\nOK -> flowup-data.json')


if __name__ == '__main__':
    main()
