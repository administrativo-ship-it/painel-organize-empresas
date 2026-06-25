# Sync Automático do FlowUp → flowup-data.json

Sincronização horária do FlowUp para o `flowup-data.json` que o painel lê.
Resolve a divergência entre os dados do painel e do FlowUp ao vivo.

## ⚙️ Setup inicial (uma única vez)

### 1. Adicionar os secrets no repositório

Acesse: **GitHub → Settings → Secrets and variables → Actions → New repository secret**

Crie dois secrets:

| Nome | Valor |
|------|-------|
| `FLOWUP_API_KEY` | `40632a543a7947ac88fa66a93e4dcf6c` |
| `FLOWUP_SUBDOMAIN` | `organizementoring` |

### 2. Criar o arquivo de workflow

Como o token automático não tem permissão de escrita em `.github/workflows/`, você precisa criar este arquivo **manualmente** uma única vez:

1. Vá em `https://github.com/administrativo-ship-it/painel-organize-empresas`
2. Clique em **Add file → Create new file**
3. Caminho do arquivo: `.github/workflows/sync-flowup.yml`
4. Cole o conteúdo abaixo:

```yaml
name: Sync FlowUp Data

on:
  schedule:
    - cron: '0 * * * *'
  workflow_dispatch:
  push:
    paths:
      - 'scripts/sync_flowup.py'
      - '.github/workflows/sync-flowup.yml'

permissions:
  contents: write

jobs:
  sync:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Sincronizar FlowUp
        env:
          FLOWUP_API_KEY: ${{ secrets.FLOWUP_API_KEY }}
          FLOWUP_SUBDOMAIN: ${{ secrets.FLOWUP_SUBDOMAIN }}
        run: python scripts/sync_flowup.py
      - name: Commit
        run: |
          git config user.name 'github-actions[bot]'
          git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
          git add flowup-data.json
          if git diff --cached --quiet; then
            echo "Sem alterações"
          else
            git commit -m "chore: auto-sync flowup-data.json [skip ci]"
            git push
          fi
```

5. Clique em **Commit new file**

### 3. Disparo manual (teste imediato)

1. Vá em **Actions** no repositório
2. Selecione **Sync FlowUp Data** no menu lateral
3. Clique em **Run workflow** → **Run workflow**
4. Acompanhe o log — em ~30 segundos termina

Se o teste passar, daí em diante roda automaticamente toda hora cheia.

## 🔍 Como verificar se está funcionando

- **No painel:** clique nos badges do topbar → modal de audit. O campo "JSON gerado" deve mostrar timestamp recente (< 1 hora).
- **No GitHub:** veja a aba Actions — o workflow "Sync FlowUp Data" deve aparecer rodando a cada hora cheia.
- **No commit log:** apareceem commits automáticos `chore: auto-sync flowup-data.json [skip ci]`.

## 🐛 Troubleshooting

**Erro 401/403 na execução:**
- Verifique se `FLOWUP_API_KEY` está correto no secret
- O script tenta 4 padrões de autenticação (Bearer, plain, X-Api-Key, Token)

**Erro 404 no endpoint:**
- O script usa por padrão `https://app.flowup.com.br/api/v3`
- Para alterar, adicione um secret extra `FLOWUP_BASE_URL` com a URL correta

**Sem alterações em todas as execuções:**
- O JSON só é commitado quando muda — comportamento esperado se nada novo no FlowUp

**Workflow não dispara automaticamente:**
- Verifique se o repositório está ativo (sem `[skip ci]` no último commit do default branch há > 60 dias). GitHub pausa schedules em repos inativos.

## 📁 Arquivos do sistema

- `scripts/sync_flowup.py` — script Python (já no repo)
- `.github/workflows/sync-flowup.yml` — workflow (criar manualmente)
- `flowup-data.json` — gerado automaticamente
