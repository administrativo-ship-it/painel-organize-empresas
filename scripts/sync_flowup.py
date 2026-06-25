#!/usr/bin/env python3
"""
Sincroniza dados do FlowUp para flowup-data.json.
Roda automaticamente via GitHub Actions (cron horário).

Variáveis de ambiente necessárias:
- FLOWUP_API_KEY   (obrigatório)
- FLOWUP_SUBDOMAIN (opcional, default: organizementoring)
- FLOWUP_BASE_URL  (opcional, default: https://app.flowup.com.br/api/v3)
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error

API_KEY = os.environ.get('FLOWUP_API_KEY', '').strip()
SUBDOMAIN = os.environ.get('FLOWUP_SUBDOMAIN', 'organizementoring').strip()
BASE_URL_ENV = os.environ.get('FLOWUP_BASE_URL', '').rstrip('/')

# Lista de URLs candidatas a tentar (o script descobre a correta automaticamente)
def _candidate_urls():
    if BASE_URL_ENV:
        return [BASE_URL_ENV]
    return [
        f'https://{SUBDOMAIN}.flowup.me/api/v3',
        f'https://{SUBDOMAIN}.flowup.me/api/v1',
        f'https://app.flowup.me/api/v3',
        f'https://api.flowup.me/v3',
        f'https://{SUBDOMAIN}.flowup.me/api',
        f'https://api.flowup.me/api/v3',
    ]

# Estado global da URL base resolvida (primeira que respondeu)
_resolved_base_url = None

if not API_KEY:
    print('ERRO: defina FLOWUP_API_KEY (secret no GitHub).', file=sys.stderr)
    sys.exit(1)


def _request(method, path, body=None, auth_variant=0, base_url=None):
    """Faz uma requisição HTTP com fallback para variações de autenticação."""
    bu = base_url or _resolved_base_url or _candidate_urls()[0]
    url = f'{bu}{path}'
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'X-Subdomain': SUBDOMAIN,
        'User-Agent': 'organize-painel-sync/1.0'
    }
    # Tenta variações comuns de header de auth do FlowUp
    if auth_variant == 0:
        headers['Authorization'] = f'Bearer {API_KEY}'
    elif auth_variant == 1:
        headers['Authorization'] = API_KEY
    elif auth_variant == 2:
        headers['X-Api-Key'] = API_KEY
    else:
        headers['Authorization'] = f'Token {API_KEY}'

    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode('utf-8'))


def _discover_base_url():
    """Testa as URLs candidatas até encontrar uma que responda (DNS válido + qualquer auth)."""
    global _resolved_base_url
    if _resolved_base_url:
        return _resolved_base_url
    for bu in _candidate_urls():
        for variant in range(4):
            try:
                # Tenta um GET simples no /user/list (endpoint leve)
                _request('GET', '/user/list', None, variant, base_url=bu)
                _resolved_base_url = bu
                print(f'  ✓ URL base resolvida: {bu} (auth variant {variant})')
                return bu
            except urllib.error.HTTPError as e:
                # Endpoint existe mas auth falhou — vamos tentar outra auth
                if e.code in (401, 403):
                    continue
                # Endpoint não existe — tenta próxima URL
                if e.code in (404, 405):
                    break
                continue
            except (urllib.error.URLError, OSError):
                # DNS / conexão falhou — próxima URL
                break
            except Exception:
                continue
    return None


def api_call(method, path, body=None):
    """Chama a API tentando até 4 variações de autenticação."""
    global _resolved_base_url
    # Garante que temos uma URL base resolvida
    if not _resolved_base_url:
        bu = _discover_base_url()
        if not bu:
            raise RuntimeError('Nenhuma URL base do FlowUp respondeu. Tentadas: ' + ', '.join(_candidate_urls()))
    last_err = None
    for variant in range(4):
        try:
            return _request(method, path, body, variant)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and variant < 3:
                last_err = e
                continue
            err_body = ''
            try:
                err_body = e.read().decode('utf-8', errors='ignore')
            except Exception:
                pass
            raise RuntimeError(f'HTTP {e.code} em {method} {path}: {err_body[:300]}')
        except Exception as e:
            raise RuntimeError(f'Erro em {method} {path}: {e}')
    raise RuntimeError(f'Falha de autenticação: {last_err}')


def fetch_all_pages(method, path, body_fn=None, page_size=200, max_pages=50):
    """Paginação genérica baseada em CurrentPage/PageSize."""
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
    print(f'  Subdomínio: {SUBDOMAIN}')
    print(f'  Candidatos: {", ".join(_candidate_urls())}')
    # Resolve URL antes de começar (mostra qual funcionou)
    _discover_base_url()

    # 1) Tarefas
    print('Buscando tarefas...')
    tasks = fetch_all_pages(
        'POST',
        '/task/querytasks',
        lambda page, ps: {
            'Filter': {'ShowFinished': True, 'ShowArchived': False},
            'CurrentPage': page,
            'PageSize': ps
        }
    )
    print(f'  Tarefas: {len(tasks)}')

    # 2) Projetos
    print('Buscando projetos...')
    projects = fetch_all_pages(
        'POST',
        '/project/list',
        lambda page, ps: {'Filter': {}, 'CurrentPage': page, 'PageSize': ps}
    )
    print(f'  Projetos: {len(projects)}')

    # 3) Usuários ativos
    print('Buscando usuários...')
    users_resp = api_call('GET', '/user/list')
    users = users_resp.get('Result') if isinstance(users_resp, dict) else users_resp
    if not isinstance(users, list):
        users = []
    print(f'  Usuários ativos: {len(users)}')

    # 4) Monta JSON compatível com o painel
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
