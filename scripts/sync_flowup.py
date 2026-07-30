#!/usr/bin/env python3
"""
Sincroniza FlowUp -> flowup-data.json via API REST direta (sem MCP).
OAuth2 Password Grant em https://task.flowup.me.
Tambem embute o bearer token no index.html (bypass de IP bloqueado no navegador).

Estrategia:
1. Busca COUNT de todas as tarefas abertas (sem filtro de projeto)
2. Busca todas as paginas em paralelo (PS=1, sem risco de truncagem)
3. Deriva projetos a partir dos dados das tarefas
4. Para cada projeto descoberto, busca count total (aberto + finalizado)
5. Embute token FlowUp no index.html via GitHub API
"""
import os, sys, json, time, base64, re, subprocess, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# Chave embutida como fallback caso FLOWUP_API_KEY no secret ainda tenha a chave velha
FALLBACK_KEY = '0f46646eb5d54286baeb3e55d6721db7'
API_KEY   = (os.environ.get('FLOWUP_API_KEY', '') or FALLBACK_KEY).strip()
SUBDOMAIN = os.environ.get('FLOWUP_SUBDOMAIN', 'organizementoring').strip()
BASE_URL  = os.environ.get('FLOWUP_BASE_URL', 'https://task.flowup.me').rstrip('/')

EP_TOKEN       = '/token'
EP_QUERY_TASKS = '/api/v1/public/task/querytasks'
EP_LIST_USERS  = '/api/v1/public/user/getactiveusers'

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
        raise RuntimeError(f'Falha ao obter token com todas as chaves: {last_err}')


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


def query_one(filter_obj, page, retries=2):
    """Retorna (task_or_None, count). Silencia erros apos retries."""
    for attempt in range(retries + 1):
        try:
            r = api_call('POST', EP_QUERY_TASKS, {
                'Filter': filter_obj, 'CurrentPage': page, 'PageSize': 1
            })
            chunk = r.get('Result') or []
            return (chunk[0] if chunk else None), r.get('Count', 0)
        except Exception:
            if attempt < retries:
                time.sleep(0.3 * (attempt + 1))
                continue
            return None, 0


def fetch_all_pages_parallel(filter_obj, total):
    """Busca paginas 0..total-1 em paralelo (PS=1, MAX_WORKERS threads)."""
    tasks = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(query_one, filter_obj, p) for p in range(0, total)]
        for fut in as_completed(futs):
            t, _ = fut.result()
            if t:
                tasks.append(t)
    return tasks


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
    Usa GITHUB_TOKEN do env ou extrai do git config (actions/checkout).
    Nao modifica arquivo local para nao conflitar com git pull --rebase.
    """
    if not _token:
        print('  AVISO: FlowUp _token vazio, pulando embed'); return False

    gh_token = (os.environ.get('GITHUB_TOKEN') or '').strip()
    if not gh_token:
        gh_token = (_get_github_token_from_git_config() or '').strip()
    if not gh_token:
        print('  AVISO: GITHUB_TOKEN nao disponivel (nem env nem git config)'); return False

    repo = os.environ.get('GITHUB_REPOSITORY', 'administrativo-ship-it/painel-organize-empresas')
    url  = f'https://api.github.com/repos/{repo}/contents/index.html'
    gh_headers = {
        'Authorization': f'Bearer {gh_token}',
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'sync-flowup',
        'X-GitHub-Api-Version': '2022-11-28',
    }

    # Busca conteudo atual do index.html
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
    m = re.search(DC_PATTERN, html)
    if not m:
        print('  AVISO: _DC nao encontrado em index.html'); return False

    try:
        dc = json.loads(base64.b64decode(m.group(1)).decode('utf-8'))
    except Exception as e:
        print(f'  AVISO: falha ao decodificar _DC: {e}'); return False

    if dc.get('fuToken') == _token:
        print('  index.html: token ja atualizado, nada a commitar'); return True

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
        print(f'  AVISO: erro ao commitar index.html (HTTP {e.code}): {err[:200]}')
        return False
    except Exception as e:
        print(f'  AVISO: erro ao commitar index.html: {e}'); return False


def main():
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] Sync FlowUp | Base: {BASE_URL} | Sub: {SUBDOMAIN}')

    # Autentica (tenta chave do env, fallback para chave embutida)
    get_token()
    print('  Token OK')

    # ─── Fase 1: conta total de tarefas abertas (sem filtro de projeto) ───
    print('\n[1] Obtendo count de tarefas abertas...')
    _, total_open = query_one({'ShowFinished': False, 'ShowArchived': False}, 1)
    print(f'  Count: {total_open}')

    tasks_list = []
    if total_open > 0:
        # ─── Fase 2: busca todas as paginas em paralelo (PS=1) ───────────────
        t0 = time.time()
        print(f'\n[2] Buscando {total_open} tarefas (MAX_WORKERS={MAX_WORKERS})...')
        raw = fetch_all_pages_parallel({'ShowFinished': False, 'ShowArchived': False}, total_open)
        seen = {}
        for t in raw:
            if t and t.get('Id'):
                seen[t['Id']] = t
        tasks_list = list(seen.values())
        print(f'  Coletadas: {len(tasks_list)} ({time.time()-t0:.1f}s)')
    else:
        print('  AVISO: 0 tarefas — possivel falha de autenticacao ou acesso')

    # ─── Fase 3: deriva projetos a partir das tarefas ────────────────────────
    print('\n[3] Derivando projetos...')
    pid_to_name  = {}
    pid_to_open  = {}
    for t in tasks_list:
        pid = t.get('ProjectId')
        if not pid: continue
        pid_to_name[pid] = (t.get('ProjectName') or '').strip()
        pid_to_open[pid] = pid_to_open.get(pid, 0) + 1

    project_counts = {}
    for pid in pid_to_name:
        _, total = query_one({'Projects': [pid], 'ShowFinished': True, 'ShowArchived': False}, 1)
        project_counts[pid] = {
            'total': total,
            'open':  pid_to_open.get(pid, 0),
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

    # ─── Usuarios ────────────────────────────────────────────────────────────
    print('\n[USUARIOS]')
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

    # ─── Embute token no index.html ──────────────────────────────────────────
    print('\n[TOKEN EMBED]')
    embed_token_in_index_html_via_api()

    # ─── Escreve saida ───────────────────────────────────────────────────────
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
