#!/usr/bin/env python3
"""
Sincroniza FlowUp -> flowup-data.json via API REST direta (sem MCP).
OAuth2 Password Grant em https://task.flowup.me.

ESTRATEGIA DEFINITIVA (testada e validada):
A API de /task/querytasks trunca em ~1500 itens GLOBAL — mas COM filtro DateRange
retorna o Count real (testado: ORGANIZE EMPRESAS jun/2026 = 359 tarefas, vs 815
total no JSON anterior). Solucao: paginar MES A MES usando DateRange.

1. Para cada mes entre START_YEAR e END_YEAR, query com DateRange do mes
2. Pagina ate cobrir o Count do mes
3. Mescla tudo pelo Id (deduplicacao)
4. Tambem inclui arquivadas
5. Deriva projetos das tarefas
"""
import os, sys, json, time, calendar, urllib.request, urllib.parse, urllib.error
from datetime import datetime

API_KEY = os.environ.get('FLOWUP_API_KEY', '').strip()
SUBDOMAIN = os.environ.get('FLOWUP_SUBDOMAIN', 'organizementoring').strip()
BASE_URL = os.environ.get('FLOWUP_BASE_URL', 'https://task.flowup.me').rstrip('/')

if not API_KEY:
    print('ERRO: FLOWUP_API_KEY ausente', file=sys.stderr); sys.exit(1)

EP_TOKEN = '/token'
EP_QUERY_TASKS = '/api/v1/public/task/querytasks'
EP_LIST_USERS = '/api/v1/public/user/getactiveusers'

PAGE_SIZE = 200
MAX_PAGES_PER_MONTH = 20

# Janela de meses: 4 anos pra tras + 2 pra frente cobre tudo
NOW = datetime.utcnow()
START_YEAR = NOW.year - 4
END_YEAR = NOW.year + 2

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
        raise RuntimeError(f'HTTP {e.code}: {err[:200]}')


def paginate(filter_obj, label=''):
    """Pagina querytasks com filtro. Retorna (tasks, count_reported)."""
    out, total = [], None
    for page in range(1, MAX_PAGES_PER_MONTH + 1):
        try:
            r = api('POST', EP_QUERY_TASKS, {
                'Filter': filter_obj, 'CurrentPage': page, 'PageSize': PAGE_SIZE
            })
        except Exception as e:
            print(f'    {label} p{page}: ERRO {e}')
            break
        chunk = r.get('Result') or []
        if total is None: total = r.get('Count', 0)
        if not chunk: break
        out.extend(chunk)
        if total and len(out) >= total: break
        if len(chunk) < PAGE_SIZE: break
        time.sleep(0.1)
    return out, total or len(out)


def merge_into(target, new_list):
    novos = 0
    for t in new_list:
        tid = t.get('Id')
        if tid and tid not in target:
            target[tid] = t
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


def month_iter(start_year, end_year):
    for y in range(start_year, end_year + 1):
        for m in range(1, 13):
            last_day = calendar.monthrange(y, m)[1]
            start = f'{y:04d}-{m:02d}-01T00:00:00'
            end = f'{y:04d}-{m:02d}-{last_day:02d}T23:59:59'
            yield y, m, start, end


def main():
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] Sync FlowUp via API REST (DateRange mensal)')
    print(f'  Base: {BASE_URL} | Sub: {SUBDOMAIN}')
    get_token()
    print('  Token OK')

    all_tasks = {}

    # FASE 1: Busca geral (cobre tarefas sem EndDate)
    print('\n[1] Busca geral (ShowFinished:true, ShowArchived:true)')
    g, c = paginate({'ShowFinished': True, 'ShowArchived': True}, 'global')
    novos = merge_into(all_tasks, g)
    print(f'  +{novos} (Count={c}) | total={len(all_tasks)}')

    # FASE 2: Janela mensal com DateRange (cobre o que a busca global trunca)
    print(f'\n[2] Varredura por mes (DateRange) — {START_YEAR}..{END_YEAR}')
    total_meses = 0
    meses_com_dados = 0
    for y, m, ds, de in month_iter(START_YEAR, END_YEAR):
        total_meses += 1
        tks, cnt = paginate({
            'ShowFinished': True, 'ShowArchived': True,
            'DateRange': {'Start': ds, 'End': de}
        }, f'{y}-{m:02d}')
        if tks:
            adicionados = merge_into(all_tasks, tks)
            if adicionados > 0 or cnt > 0:
                meses_com_dados += 1
                print(f'  {y}-{m:02d}: coletadas={len(tks)} Count={cnt} +{adicionados} novas | acumulado={len(all_tasks)}')

    print(f'\n[CONSOLIDACAO]')
    tasks_list = list(all_tasks.values())
    print(f'  Total UNIVERSO: {len(tasks_list)} tarefas (meses processados: {total_meses}, com dados: {meses_com_dados})')

    projects = derive_projects(tasks_list)
    projects.sort(key=lambda p: -p['TotalTasks'])
    print(f'  Projetos: {len(projects)}')
    for p in projects:
        nome = p['Name'][:42]
        print(f"    #{p['Id']:3} {nome:42} | tot={p['TotalTasks']:4} | ab={p['OpenTasks']:3} | fin={p['FinishedTasks']:4} | arq={p['ArchivedTasks']:3}")

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
    print(f'\n[TOTAIS] tarefas={len(tasks_list)} | abertas={g_open} | fin={g_fin} | arq={g_arq} | projetos={len(projects)}')

    output = {
        'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'totals': {
            'tasks': len(tasks_list), 'open': g_open,
            'finished': g_fin, 'archived': g_arq, 'projects': len(projects)
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
