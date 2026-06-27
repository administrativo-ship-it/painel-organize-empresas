#!/usr/bin/env python3
"""
Sincroniza FlowUp -> flowup-data.json via API REST direta (sem MCP).
OAuth2 Password Grant em https://task.flowup.me.

ESTRATEGIA (testada e validada):
A API /task/querytasks trunca paginas com PageSize > 25, mas PageSize=1 pagina
sempre ate cobrir o Count. Usamos PS=1 com threading para evitar truncagem.

1. Descobre projetos varrendo pid 1..MAX_PID (1 call por pid)
2. Para cada projeto:
   - Pega 100% das ABERTAS via PS=1 paralelo (criticas: atrasadas/hoje)
   - Pega ate N_RECENT finalizadas paginas mais recentes (display/historico)
3. Conta totais com base no Count da API (nao recoleta tudo)
"""
import os, sys, json, time, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

API_KEY = os.environ.get('FLOWUP_API_KEY', '').strip()
SUBDOMAIN = os.environ.get('FLOWUP_SUBDOMAIN', 'organizementoring').strip()
BASE_URL = os.environ.get('FLOWUP_BASE_URL', 'https://task.flowup.me').rstrip('/')

if not API_KEY:
    print('ERRO: FLOWUP_API_KEY ausente', file=sys.stderr); sys.exit(1)

EP_TOKEN = '/token'
EP_QUERY_TASKS = '/api/v1/public/task/querytasks'
EP_LIST_USERS = '/api/v1/public/user/getactiveusers'

MAX_PID = 30
# OTIMIZACAO: nao coletar finalizadas detalhadas — somente Count. Reduz tempo de 8-10min para 2-3min.
# Painel mostra apenas KPIs (abertas, atrasadas, hoje) — finalizadas detalhadas nao sao necessarias.
N_RECENT_FINISHED = 0
MAX_WORKERS = 32             # threads simultaneas (paralelismo agressivo)

_token, _exp = None, 0
_token_lock = Lock()


def get_token():
    global _token, _exp
    with _token_lock:
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
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        err = e.read().decode('utf-8', errors='ignore')
        raise RuntimeError(f'HTTP {e.code} {method} {path}: {err[:200]}')
    except Exception as e:
        raise RuntimeError(f'NET {method} {path}: {e}')


def query_one(filter_obj, page, retries=2):
    """Retorna (task|None, count) para PS=1 page especifica. Com retry."""
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
    """Busca pages [start, end] em paralelo via PS=1."""
    tasks = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(query_one, filter_obj, p) for p in range(page_start, page_end + 1)]
        for fut in as_completed(futs):
            t, _ = fut.result()
            if t: tasks.append(t)
    return tasks


def merge_into(target, new_list):
    novos = 0
    for t in new_list:
        tid = t.get('Id')
        if tid and tid not in target:
            target[tid] = t
            novos += 1
    return novos


def derive_projects(tasks, project_counts):
    """Deriva projects, usando o Count real da API (nao len(tasks))."""
    projs = {}
    for t in tasks:
        pid = t.get('ProjectId')
        if not pid: continue
        if pid not in projs:
            projs[pid] = {
                'Id': pid, 'Name': (t.get('ProjectName') or '').strip(),
                'TotalTasks': project_counts.get(pid, {}).get('total', 0),
                'OpenTasks': project_counts.get(pid, {}).get('open', 0),
                'FinishedTasks': project_counts.get(pid, {}).get('finished', 0),
                'ArchivedTasks': 0
            }
    return list(projs.values())


def main():
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] Sync FlowUp (PS=1 paralelo, MAX_WORKERS={MAX_WORKERS})')
    print(f'  Base: {BASE_URL} | Sub: {SUBDOMAIN}')
    get_token()
    print('  Token OK')

    all_tasks = {}
    project_counts = {}

    # FASE 1: Descobrir projetos
    print(f'\n[1] Descobrindo projetos (pid 1..{MAX_PID})')
    valid_pids = []
    for pid in range(1, MAX_PID + 1):
        _, total = query_one({'Projects': [pid], 'ShowFinished': True, 'ShowArchived': False}, 1)
        _, total_open = query_one({'Projects': [pid], 'ShowFinished': False, 'ShowArchived': False}, 1)
        if total > 0:
            project_counts[pid] = {
                'total': total, 'open': total_open, 'finished': total - total_open
            }
            valid_pids.append(pid)
            print(f'  pid={pid}: total={total} abertas={total_open}')

    # FASE 2: Projetos serial (paralelismo INTERNO por projeto = MAX_WORKERS threads)
    # Tentativa anterior (4 projetos paralelos = 128 conexoes) quebrou — provavelmente rate-limit
    # do FlowUp. Voltando ao formato serial que funcionava (7min total, mas estavel).
    print(f'\n[2] Coletando tarefas ({len(valid_pids)} projetos serial, paralelismo interno {MAX_WORKERS})')
    for pid in valid_pids:
        t_start = time.time()
        n_open = project_counts[pid]['open']
        sys.stdout.flush()  # garante que log aparece em tempo real no GitHub Actions

        tks = fetch_pages_parallel(
            {'Projects': [pid], 'ShowFinished': False, 'ShowArchived': False},
            0, n_open - 1
        ) if n_open > 0 else []

        merge_into(all_tasks, tks)
        dur = time.time() - t_start
        print(f'  pid={pid}: open={len(tks)}/{n_open} | {dur:.1f}s | acumulado={len(all_tasks)}', flush=True)

    tasks_list = list(all_tasks.values())
    projects = derive_projects(tasks_list, project_counts)
    projects.sort(key=lambda p: -p['TotalTasks'])

    print(f'\n[CONSOLIDACAO] {len(tasks_list)} tarefas em {len(projects)} projetos')
    for p in projects:
        nome = p['Name'][:42]
        print(f"  #{p['Id']:3} {nome:42} | tot={p['TotalTasks']:4} | ab={p['OpenTasks']:3} | fin={p['FinishedTasks']:4}")

    print('\n[USUARIOS]')
    try:
        ur = api('GET', EP_LIST_USERS)
        users = ur.get('Result') if isinstance(ur, dict) else ur
        if not isinstance(users, list): users = []
    except Exception as e:
        print(f'  ERRO: {e}'); users = []
    print(f'  Ativos: {len(users)}')

    g_total = sum(p['TotalTasks'] for p in projects)
    g_open = sum(p['OpenTasks'] for p in projects)
    g_fin = sum(p['FinishedTasks'] for p in projects)
    print(f'\n[TOTAIS REAIS] tarefas={g_total} | abertas={g_open} | fin={g_fin} | projetos={len(projects)}')

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
